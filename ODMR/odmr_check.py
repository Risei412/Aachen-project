#!/usr/bin/env python3
"""Instant verdict on a saved sweep: did it actually measure ODMR?

Clicking around the viewer answers *what* the spectrum looks like at a
given spot, but not the first question after every sweep: **is there an
ODMR signal in this data at all, or should the setup be fixed before
measuring again?** This script answers that in a second or two, without
clicking anything, and prints one of

    DETECTED      significant resonance dip(s) found
    WEAK          something is there, but not convincingly above noise
    NOT DETECTED  no dip distinguishable from noise

together with the dip frequencies, contrast, significance, where in the
field of view the response is, and warnings about the data itself
(clipped pixels, resonance at the sweep edge, uneven repeats).

How the decision is made
------------------------
1. Every sufficiently bright pixel's spectrum is divided by its own mean,
   and those relative spectra are averaged. That uses the whole field of
   view, so a dip too shallow to see in any single ROI still shows up,
   and bright background (anvil reflections) cannot dominate the average.
2. The averaged spectrum is normalised by the same robust one-sided
   baseline fit the viewer uses (:func:`odmr_sweep.robust_baseline`).
3. The noise level is taken from the points *above* the baseline. A
   resonance can only darken the photoluminescence, so the upper side of
   the residual carries noise only -- the same self-calibrating argument
   as the "MW check". When the sweep has error bars (repeats > 1) the
   larger of that and the between-repeat error is used, because
   laser-power fluctuations are common to all pixels and do not average
   away over the field of view.
4. A dip counts as a detection when it is at least ``--sigma`` (default
   5) noise levels deep and at least ``--min-points`` (default 2)
   consecutive frequency points wide. The width requirement rejects a
   single-frame glitch (a laser flicker, a missed PLL lock), which is
   one point wide however deep it is.
5. Frames on and off the strongest dip are compared pixel by pixel with
   the MW-check statistics (:func:`odmr_sweep.summarize_difference`) to
   report how many pixels respond and to draw a response map.

Usage
-----
    python odmr_check.py                      # newest .npz in data/
    python odmr_check.py data/odmr_....npz    # a specific sweep
    python odmr_check.py --watch              # check each new "Save raw" as it lands
    python odmr_check.py --no-plot            # console verdict only
    python odmr_check.py --save-plot          # also write <file>_check.png

Exit status is 0 for DETECTED, 1 for WEAK, 2 for NOT DETECTED and 3 when
no file could be read, so the check can be scripted.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import time
from dataclasses import dataclass, field

import numpy as np

from odmr_sweep import (
    difference_map,
    load_cube,
    robust_baseline,
    summarize_difference,
)

HERE = os.path.dirname(os.path.abspath(__file__))

DETECTED = "DETECTED"
WEAK = "WEAK"
NOT_DETECTED = "NOT DETECTED"
EXIT_CODES = {DETECTED: 0, WEAK: 1, NOT_DETECTED: 2}

# Printed when nothing convincing was found. Ordered by how often each is
# the actual cause, same as the README's MW-check checklist.
TROUBLESHOOTING = [
    "Emission filter: a long-pass (~650 nm) or NV band-pass must sit in front "
    "of the camera, otherwise it images scattered green light with no ODMR contrast.",
    "Laser on the NV: with the filter in, the NV grains should still be visible.",
    "Microwave: antenna/loop close to the sample, SynthHD output enabled, power sensible.",
    "Sweep range: must contain a resonance with off-resonance baseline on both sides "
    "(2870 MHz at zero field).",
    "Noise: more repeats / frames_per_point, or larger binning, lower the noise floor.",
]


@dataclass
class Dip:
    freq_mhz: float           # frequency of the deepest point
    start_mhz: float
    stop_mhz: float
    n_points: int
    depth_pct: float          # contrast of the deepest point, relative to baseline
    significance: float       # depth / noise
    at_edge: bool             # touches the first or last sweep point

    def describe(self) -> str:
        return (
            f"{self.freq_mhz:8.1f} MHz  contrast {self.depth_pct:6.3f} %  "
            f"{self.significance:5.1f} sigma  "
            f"({self.start_mhz:g}-{self.stop_mhz:g} MHz, {self.n_points} pts)"
        )


@dataclass
class Assessment:
    verdict: str
    frequencies_mhz: np.ndarray
    spectrum_pct: np.ndarray          # field-averaged, baseline-normalised, in %
    noise_pct: float
    noise_source: str
    dips: list[Dip]                   # every run below the WEAK threshold, deepest first
    significant: list[Dip]
    bright_px: int
    response_map: np.ndarray | None   # fractional PL drop on vs off the best dip
    response_stats: dict | None
    mean_pl: np.ndarray
    warnings: list[str] = field(default_factory=list)
    roi_noise_pct: float | None = None  # typical noise of one ROI-sized average
    roi_half_size: int | None = None

    @property
    def best(self) -> Dip | None:
        return self.dips[0] if self.dips else None

    def headline(self) -> str:
        """One line suitable for a status bar."""
        best = self.best
        if self.verdict == DETECTED:
            freqs = ", ".join(f"{d.freq_mhz:.1f}" for d in sorted(self.significant, key=lambda d: d.freq_mhz))
            return (
                f"ODMR {DETECTED}: dip(s) at {freqs} MHz, "
                f"field-avg contrast {best.depth_pct:.2f} % ({best.significance:.0f} sigma)"
            )
        if self.verdict == WEAK and best is not None:
            return (
                f"ODMR {WEAK}: best dip {best.freq_mhz:.1f} MHz, "
                f"{best.depth_pct:.3f} % ({best.significance:.1f} sigma) -- not conclusive"
            )
        return f"ODMR {NOT_DETECTED} (noise {self.noise_pct:.3f} %)"

    def report(self) -> str:
        f = self.frequencies_mhz
        lines = [
            "=" * 72,
            f"  VERDICT: {self.verdict}",
            "=" * 72,
            f"  Sweep       : {f[0]:g}-{f[-1]:g} MHz, {len(f)} points",
            f"  Pixels used : {self.bright_px} bright pixels averaged "
            "(contrasts below are field averages; a single NV spot is deeper)",
            f"  Noise floor : {self.noise_pct:.4f} % ({self.noise_source})",
        ]
        if self.dips:
            lines.append("  Dips (deepest first):")
            for dip in self.dips[:6]:
                mark = "*" if dip in self.significant else " "
                lines.append(f"   {mark} {dip.describe()}")
            lines.append("    (* = significant)")
        else:
            lines.append("  Dips        : none below the noise threshold")
        if self.roi_noise_pct is not None:
            lines.append(
                f"  ROI noise   : {self.roi_noise_pct:.3f} % typical for a "
                f"+/-{self.roi_half_size} px ROI (what a clicked spot will show)"
            )
        if self.response_stats is not None:
            s = self.response_stats
            share = 100.0 * s["responding_px"] / max(self.bright_px, 1)
            lines.append(
                f"  Responding  : {s['responding_px']} px ({share:.1f} % of bright px) "
                f"at >5 sigma per pixel, peak drop {s['max_pct']:.2f} %"
            )
        for warning in self.warnings:
            lines.append(f"  WARNING: {warning}")
        if self.verdict != DETECTED:
            lines.append("  Things to check:")
            for i, hint in enumerate(TROUBLESHOOTING, 1):
                lines.append(f"    {i}. {hint}")
        lines.append("=" * 72)
        return "\n".join(lines)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """Inclusive (start, stop) index pairs of consecutive True values."""
    runs, start = [], None
    for i, flag in enumerate(mask):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            runs.append((start, i - 1))
            start = None
    if start is not None:
        runs.append((start, len(mask) - 1))
    return runs


def _frame_mean(cube: np.ndarray, indices) -> np.ndarray:
    """Mean of selected frames without fancy-indexing a copy of the cube."""
    total = np.zeros(cube.shape[1:], dtype=np.float64)
    for i in indices:
        total += cube[i]
    return total / max(len(indices), 1)


def roi_noise(
    frequencies_mhz: np.ndarray,
    cube: np.ndarray,
    half_size: int,
    min_signal_fraction: float = 0.15,
    reject_sigma: float = 2.5,
    iterations: int = 8,
) -> float | None:
    """Typical noise, as a fraction, of a spectrum averaged over one ROI.

    The field-averaged spectrum can be hundreds of sigma clean while a
    single clicked ROI is still too noisy to read, so this measures the
    ROI scale directly: the image is cut into ROI-sized tiles, each
    tile's spectrum gets the usual robust baseline, and the noise is
    taken from the scatter above it. Doing it on real tile averages
    rather than scaling per-pixel errors by 1/sqrt(n) keeps noise that is
    common to all pixels (laser power) in the number, since that part
    does not average away. Returns the median over the bright tiles.
    """
    size = 2 * int(half_size) + 1
    n, height, width = cube.shape
    ty, tx = height // size, width // size
    if ty == 0 or tx == 0:
        return None
    tiles = cube[:, : ty * size, : tx * size].reshape(n, ty, size, tx, size).mean(axis=(2, 4))
    level = tiles.mean(axis=0)
    bright = level >= min_signal_fraction * float(level.max())
    noises = []
    for y, x in zip(*np.nonzero(bright)):
        spectrum = tiles[:, y, x]
        residual = spectrum / robust_baseline(frequencies_mhz, spectrum, reject_sigma, iterations) - 1.0
        upper = residual[residual > 0]
        if upper.size >= 5:
            noises.append(1.4826 * float(np.median(upper)))
    return float(np.median(noises)) if noises else None


def assess_odmr(
    frequencies_mhz: np.ndarray,
    cube: np.ndarray,
    sem: np.ndarray | None = None,
    counts: np.ndarray | None = None,
    min_signal_fraction: float = 0.15,
    reject_sigma: float = 2.5,
    iterations: int = 8,
    detect_sigma: float = 5.0,
    weak_sigma: float = 3.0,
    min_points: int = 2,
    roi_half_size: int | None = None,
) -> Assessment:
    """Decide whether ``cube`` contains an ODMR signal. See the module docstring."""
    freqs = np.asarray(frequencies_mhz, dtype=float)
    n_freq = len(freqs)
    warnings: list[str] = []

    mean_pl = cube.mean(axis=0)
    peak_pl = float(np.max(mean_pl)) if mean_pl.size else 0.0
    if n_freq < 5 or peak_pl <= 0 or not np.isfinite(peak_pl):
        return Assessment(
            verdict=NOT_DETECTED,
            frequencies_mhz=freqs,
            spectrum_pct=np.full(n_freq, 100.0),
            noise_pct=float("nan"),
            noise_source="n/a",
            dips=[],
            significant=[],
            bright_px=0,
            response_map=None,
            response_stats=None,
            mean_pl=mean_pl,
            warnings=["Too few frequency points or no light in the image -- nothing to assess."],
        )

    bright = mean_pl >= min_signal_fraction * peak_pl
    n_bright = int(bright.sum())

    # Clipped pixels all sit at exactly the camera's ceiling, so they pile
    # up at the maximum; a saturated pixel cannot dim, so it has no ODMR.
    at_ceiling = int(np.count_nonzero(mean_pl >= 0.999 * peak_pl))
    if at_ceiling > max(3, 0.005 * n_bright):
        warnings.append(
            f"{at_ceiling} px sit at the image maximum -- likely saturated. "
            "Clipped pixels show no ODMR contrast; shorten the exposure."
        )

    # Field-averaged relative spectrum: each pixel weighted by 1/its mean.
    weights = np.where(bright, 1.0 / np.maximum(mean_pl, 1e-12), 0.0)
    spectrum = np.tensordot(cube, weights, axes=2) / n_bright

    baseline = robust_baseline(freqs, spectrum, reject_sigma, iterations)
    residual = spectrum / baseline - 1.0          # fractional, dips negative

    upper = residual[residual > 0]
    if upper.size >= 5:
        # Half-normal: median(|x|) = 0.6745 sigma, same constant as the MAD.
        noise = 1.4826 * float(np.median(upper))
        noise_source = "scatter above baseline"
    else:
        noise = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
        noise_source = "MAD of spectrum"

    if sem is not None and np.any(np.isfinite(sem)):
        per_point = np.empty(n_freq)
        w2 = weights ** 2
        for i in range(n_freq):
            per_point[i] = np.sqrt(np.nansum(sem[i] ** 2 * w2)) / n_bright
        sem_noise = float(np.nanmedian(per_point / baseline))
        if np.isfinite(sem_noise) and sem_noise > noise:
            noise, noise_source = sem_noise, "between-repeat error"
    noise = max(noise, 1e-9)

    # ---- dips: consecutive runs below the weak threshold ----
    dips: list[Dip] = []
    for start, stop in _runs(residual < -weak_sigma * noise):
        k = start + int(np.argmin(residual[start:stop + 1]))
        depth = -float(residual[k])
        dips.append(
            Dip(
                freq_mhz=float(freqs[k]),
                start_mhz=float(freqs[start]),
                stop_mhz=float(freqs[stop]),
                n_points=stop - start + 1,
                depth_pct=100.0 * depth,
                significance=depth / noise,
                at_edge=start == 0 or stop == n_freq - 1,
            )
        )
    dips.sort(key=lambda d: d.significance, reverse=True)
    significant = [d for d in dips if d.significance >= detect_sigma and d.n_points >= min_points]

    if significant:
        verdict = DETECTED
    elif dips and dips[0].n_points >= min_points:
        verdict = WEAK
    else:
        verdict = NOT_DETECTED

    if dips and all(d.n_points < min_points for d in dips) and dips[0].significance >= detect_sigma:
        warnings.append(
            f"Deep but single-point dip at {dips[0].freq_mhz:.1f} MHz -- looks like a glitch "
            "(laser flicker, PLL not locked), not a resonance. Real NV lines span several MHz; "
            "use a step finer than the linewidth."
        )
    for dip in significant:
        if dip.at_edge:
            warnings.append(
                f"Dip at {dip.freq_mhz:.1f} MHz touches the end of the sweep -- widen the range "
                "so the baseline is measured on both sides."
            )

    # ---- where in the image: frames on vs off the strongest dip ----
    response_map = response_stats = None
    target = significant[0] if significant else (dips[0] if dips else None)
    if target is not None:
        on_idx = np.flatnonzero((freqs >= target.start_mhz) & (freqs <= target.stop_mhz))
        in_any_dip = np.zeros(n_freq, dtype=bool)
        for d in dips:
            in_any_dip |= (freqs >= d.start_mhz) & (freqs <= d.stop_mhz)
        off_idx = np.flatnonzero(~in_any_dip & (residual > -noise))
        if len(on_idx) and len(off_idx) >= 3:
            response_map = difference_map(
                _frame_mean(cube, off_idx), _frame_mean(cube, on_idx), min_signal_fraction
            )
            response_stats = summarize_difference(response_map)

    if counts is not None and len(counts) and not np.all(counts == counts[0]):
        warnings.append(
            f"Uneven averaging ({int(counts.min())}-{int(counts.max())} repeats per point) -- "
            "the sweep was aborted mid-repeat."
        )

    roi_noise_frac = None
    if roi_half_size:
        roi_noise_frac = roi_noise(freqs, cube, roi_half_size, min_signal_fraction, reject_sigma, iterations)

    return Assessment(
        roi_noise_pct=None if roi_noise_frac is None else 100.0 * roi_noise_frac,
        roi_half_size=roi_half_size,
        verdict=verdict,
        frequencies_mhz=freqs,
        spectrum_pct=100.0 * (1.0 + residual),
        noise_pct=100.0 * noise,
        noise_source=noise_source,
        dips=dips,
        significant=significant,
        bright_px=n_bright,
        response_map=response_map,
        response_stats=response_stats,
        mean_pl=mean_pl,
        warnings=warnings,
    )


def resonance_windows(
    a: Assessment, depth_fraction: float = 0.1, margin_mhz: float = 5.0
) -> list[tuple[float, float]]:
    """Frequency ranges worth measuring finely, found from a coarse survey.

    Around each dip the window extends while the spectrum stays below
    ``depth_fraction`` of that dip's depth (and above the noise), then
    ``margin_mhz`` more on each side for the linewidth the coarse step
    could not resolve. Tying the edge to the dip's own depth rather than
    to the noise keeps a very strong resonance from claiming its whole
    far-off wing, which would leave nothing to save.

    Uses the significant dips, or failing that the weak multi-point ones,
    so a resonance that is only hinted at in a quick survey still gets a
    careful second look. Returns ``[]`` when there is nothing to zoom on.
    """
    candidates = a.significant or [d for d in a.dips if d.n_points >= 2]
    freqs = a.frequencies_mhz
    residual = a.spectrum_pct / 100.0 - 1.0
    windows = []
    for dip in candidates:
        level = -max(3.0 * a.noise_pct / 100.0, depth_fraction * dip.depth_pct / 100.0)
        k = int(np.argmin(np.abs(freqs - dip.freq_mhz)))
        low = high = k
        while low > 0 and residual[low - 1] < level:
            low -= 1
        while high < len(freqs) - 1 and residual[high + 1] < level:
            high += 1
        windows.append((float(freqs[low]) - margin_mhz, float(freqs[high]) + margin_mhz))
    return sorted(windows)


def assess_result(result, analysis_cfg: dict | None = None, **overrides) -> Assessment:
    """:func:`assess_odmr` on an :class:`odmr_sweep.OdmrResult`, with config.yaml settings."""
    cfg = analysis_cfg or {}
    kwargs = dict(
        min_signal_fraction=float(cfg.get("min_signal_fraction", 0.15)),
        reject_sigma=float(cfg.get("baseline_reject_sigma", 2.5)),
        iterations=int(cfg.get("baseline_iterations", 8)),
    )
    kwargs.update(overrides)
    return assess_odmr(result.frequencies_mhz, result.cube, result.sem, result.counts, **kwargs)


# ------------------------------------------------------------------ plotting

VERDICT_COLOURS = {DETECTED: "tab:green", WEAK: "tab:orange", NOT_DETECTED: "tab:red"}


def plot_assessment(a: Assessment, title: str = ""):
    from matplotlib import pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    fig.canvas.manager.set_window_title("ODMR check")
    fig.suptitle(
        f"{a.headline()}\n{title}", color=VERDICT_COLOURS[a.verdict], fontsize=11, fontweight="bold"
    )
    ax_pl, ax_map, ax_spec, ax_hist = axes.ravel()

    handle = ax_pl.imshow(a.mean_pl, cmap="gray")
    ax_pl.set_title("Mean PL over sweep")
    fig.colorbar(handle, ax=ax_pl, fraction=0.046)

    if a.response_map is not None:
        limit = max(float(np.percentile(np.abs(a.response_map), 99.5)), 1e-6) * 100.0
        handle = ax_map.imshow(100.0 * a.response_map, cmap="RdBu_r", vmin=-limit, vmax=limit)
        best = a.significant[0] if a.significant else a.best
        ax_map.set_title(f"PL drop on {best.freq_mhz:.1f} MHz dip vs off-resonance (%)")
        fig.colorbar(handle, ax=ax_map, fraction=0.046)
    else:
        ax_map.text(0.5, 0.5, "No dip to map", ha="center", va="center", transform=ax_map.transAxes)
        ax_map.set_axis_off()

    f = a.frequencies_mhz
    ax_spec.plot(f, a.spectrum_pct, "o-", markersize=3, color="tab:blue")
    ax_spec.axhline(100.0, color="gray", linewidth=0.8)
    ax_spec.axhspan(100.0 - 5 * a.noise_pct, 100.0 + 5 * a.noise_pct, color="gray", alpha=0.15,
                    label="±5σ noise")
    for dip in a.dips:
        colour = "tab:green" if dip in a.significant else "tab:orange"
        ax_spec.axvspan(dip.start_mhz, dip.stop_mhz, color=colour, alpha=0.2)
    ax_spec.set_xlabel("MW frequency (MHz)")
    ax_spec.set_ylabel("Field-averaged PL (%)")
    ax_spec.set_title(f"Spectrum over {a.bright_px} bright px (green = significant dip)")
    ax_spec.grid(True, alpha=0.3)
    ax_spec.legend(fontsize=8, loc="lower right")

    if a.response_map is not None:
        values = 100.0 * a.response_map[a.response_map != 0.0]
        bins = np.linspace(-np.max(np.abs(values)), np.max(np.abs(values)), 121) if values.size else 50
        ax_hist.hist(values, bins=bins, color="tab:blue", alpha=0.6, label="PL drop")
        ax_hist.hist(-values, bins=bins, histtype="step", color="k", label="mirrored (noise reference)")
        ax_hist.set_yscale("log")
        ax_hist.set_xlabel("Per-pixel PL drop (%)")
        ax_hist.set_title("Excess on the right of the mirror = pixels responding")
        ax_hist.legend(fontsize=8)
    else:
        ax_hist.set_axis_off()
        ax_hist.text(0.02, 0.98, a.report(), family="monospace", fontsize=6.5, va="top",
                     transform=ax_hist.transAxes)

    fig.tight_layout()
    return fig


# ------------------------------------------------------------------------ CLI

def _load_analysis_cfg(path: str) -> dict:
    try:
        import yaml
    except ImportError:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            return (yaml.safe_load(f) or {}).get("analysis", {}) or {}
    except OSError:
        return {}


def _newest_npz(directory: str) -> str | None:
    files = glob.glob(os.path.join(directory, "*.npz"))
    return max(files, key=os.path.getmtime) if files else None


def _wait_until_written(path: str, timeout_s: float = 120.0) -> None:
    """Wait for a file being saved by the viewer to stop growing."""
    last, deadline = -1, time.time() + timeout_s
    while time.time() < deadline:
        size = os.path.getsize(path)
        if size == last and size > 0:
            return
        last = size
        time.sleep(0.5)


def check_file(path: str, args, analysis_cfg: dict) -> Assessment:
    result, metadata = load_cube(path)
    overrides = {"detect_sigma": args.sigma, "min_points": args.min_points,
                 "roi_half_size": args.roi}
    assessment = assess_result(result, analysis_cfg, **overrides)
    print(f"\n{os.path.basename(path)}  ({result.repeats_completed} repeat(s), "
          f"cube {result.cube.shape[0]}x{result.cube.shape[1]}x{result.cube.shape[2]})")
    print(assessment.report())
    return assessment


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Instantly judge whether a saved ODMR sweep contains an ODMR signal."
    )
    parser.add_argument("file", nargs="?", default=None,
                        help="Saved .npz datacube (default: newest file in the data directory)")
    parser.add_argument("--data-dir", default=os.path.join(HERE, "data"),
                        help="Where to look for the newest .npz (default: ODMR/data)")
    parser.add_argument("--config", default=os.path.join(HERE, "config.yaml"),
                        help="config.yaml to take analysis settings from")
    parser.add_argument("--sigma", type=float, default=5.0,
                        help="Dip depth, in noise levels, needed for DETECTED (default 5)")
    parser.add_argument("--min-points", type=int, default=2,
                        help="Consecutive points a dip must span (default 2)")
    parser.add_argument("--roi", type=int, default=15,
                        help="ROI half-size (px) for the typical-ROI-noise figure (default 15)")
    parser.add_argument("--no-plot", action="store_true", help="Print the verdict only")
    parser.add_argument("--save-plot", action="store_true", help="Write <file>_check.png")
    parser.add_argument("--watch", action="store_true",
                        help="Keep running and check every new .npz saved into the data directory")
    args = parser.parse_args()

    analysis_cfg = _load_analysis_cfg(args.config)
    # Verdicts must appear the moment they are made, also when the output
    # goes to a pipe or log file rather than a terminal.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    if args.watch:
        import matplotlib

        if not args.save_plot:
            args.no_plot = True
        matplotlib.use("Agg")
        seen = set(glob.glob(os.path.join(args.data_dir, "*.npz")))
        print(f"Watching {args.data_dir} for new sweeps (press Ctrl+C to stop)...")
        try:
            while True:
                for path in sorted(set(glob.glob(os.path.join(args.data_dir, "*.npz"))) - seen,
                                   key=os.path.getmtime):
                    seen.add(path)
                    _wait_until_written(path)
                    try:
                        assessment = check_file(path, args, analysis_cfg)
                    except Exception as exc:  # a half-written or foreign file must not stop the watch
                        print(f"Could not check {path}: {exc}")
                        continue
                    if args.save_plot:
                        _save_plot(assessment, path)
                time.sleep(1.0)
        except KeyboardInterrupt:
            return 0

    path = args.file or _newest_npz(args.data_dir)
    if path is None or not os.path.isfile(path):
        print(f"No datacube found ({args.file or args.data_dir}). "
              "Press 'Save raw' in the viewer first, or pass a .npz path.")
        return 3

    assessment = check_file(path, args, analysis_cfg)
    if args.save_plot:
        _save_plot(assessment, path)
    if not args.no_plot:
        from matplotlib import pyplot as plt

        plot_assessment(assessment, os.path.basename(path))
        plt.show()
    return EXIT_CODES[assessment.verdict]


def _save_plot(assessment: Assessment, path: str) -> None:
    from matplotlib import pyplot as plt

    fig = plot_assessment(assessment, os.path.basename(path))
    out = os.path.splitext(path)[0] + "_check.png"
    fig.savefig(out, dpi=120)
    plt.close(fig)
    print(f"Plot written to {out}")


if __name__ == "__main__":
    sys.exit(main())
