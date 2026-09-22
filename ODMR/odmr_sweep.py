"""Widefield ODMR acquisition and analysis, independent of any GUI.

The measurement is a *single* microwave frequency sweep during which the
**whole camera frame** is stored at every frequency point. The result is
a datacube

    cube[i, y, x] = photoluminescence at pixel (x, y) with the microwave
                    set to frequencies_mhz[i]

so every pixel in the field of view carries its own ODMR spectrum. Once
the cube is in memory, extracting the ODMR curve for any spot the user
clicks on is just a slice -- no further hardware access needed.

Typical use:

    freqs = frequency_axis(2800, 2940, 1.0)
    freqs, cube = acquire_odmr_cube(camera, generator, freqs, power_dbm=0.0)
    roi = Roi(x_center=512, y_center=300, half_size=15)
    spectrum = roi_spectrum(cube, roi)
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass
class Roi:
    """Square region of interest, in (binned) pixel coordinates."""

    x_center: int
    y_center: int
    half_size: int

    def slices(self, shape=None):
        x0 = max(self.x_center - self.half_size, 0)
        y0 = max(self.y_center - self.half_size, 0)
        x1 = self.x_center + self.half_size + 1
        y1 = self.y_center + self.half_size + 1
        if shape is not None:
            height, width = shape[-2], shape[-1]
            x1, y1 = min(x1, width), min(y1, height)
        return slice(y0, y1), slice(x0, x1)

    def mean_intensity(self, frame: np.ndarray) -> float:
        ys, xs = self.slices(frame.shape)
        return float(np.mean(frame[ys, xs]))


def frequency_axis(start_mhz: float, stop_mhz: float, step_mhz: float) -> np.ndarray:
    """Frequency points of the sweep, inclusive of ``stop_mhz`` when it lands on a step."""
    n_points = int(round((stop_mhz - start_mhz) / step_mhz)) + 1
    return start_mhz + step_mhz * np.arange(n_points)


def zoomed_axis(
    start_mhz: float,
    stop_mhz: float,
    step_mhz: float,
    windows: list[tuple[float, float]],
    baseline_step_mhz: float,
) -> np.ndarray:
    """Fine points inside ``windows``, sparse points everywhere else.

    Most of an NV sweep is flat baseline: finely sampling it costs as much
    time as the resonances but only pins down one straight line. This keeps
    the full ``step_mhz`` resolution inside each ``(low, high)`` window
    around a resonance, and elsewhere only every ``baseline_step_mhz`` --
    enough for the robust baseline fit to still see the off-resonance level
    on both sides. All points lie on the same grid as the full sweep, so
    the result is directly comparable with an unzoomed measurement.
    """
    full = frequency_axis(start_mhz, stop_mhz, step_mhz)
    stride = max(1, int(round(baseline_step_mhz / step_mhz)))
    keep = np.zeros(len(full), dtype=bool)
    keep[::stride] = True
    keep[-1] = True  # always anchor the baseline at both ends
    for low, high in windows:
        keep |= (full >= low - 1e-9) & (full <= high + 1e-9)
    return full[keep]


def bin_frame(frame: np.ndarray, binning: int) -> np.ndarray:
    """Average ``binning`` x ``binning`` pixel blocks together.

    Reduces the datacube size by ``binning**2`` and improves the
    signal-to-noise of each (now larger) effective pixel. The frame is
    cropped to a whole multiple of ``binning`` first.
    """
    if binning <= 1:
        return frame.astype(np.float32)
    height, width = frame.shape
    h_crop, w_crop = (height // binning) * binning, (width // binning) * binning
    cropped = frame[:h_crop, :w_crop].astype(np.float32)
    return cropped.reshape(h_crop // binning, binning, w_crop // binning, binning).mean(axis=(1, 3))


def estimate_cube_bytes(n_freq: int, height: int, width: int, binning: int = 1) -> int:
    """Memory a float32 datacube of this shape will occupy."""
    return n_freq * (height // binning) * (width // binning) * 4


@dataclass
class OdmrResult:
    """A completed (or aborted) widefield ODMR measurement."""

    frequencies_mhz: np.ndarray
    cube: np.ndarray                  # (n_freq, h, w), mean over repeats
    counts: np.ndarray                # (n_freq,), times each point was measured
    sem: np.ndarray | None            # (n_freq, h, w), standard error of the mean
    repeats_completed: int

    @property
    def fully_averaged(self) -> bool:
        """True when every frequency point got the same number of repeats."""
        return bool(np.all(self.counts == self.counts[0])) if self.counts.size else False


def acquire_odmr_cube(
    camera,
    generator,
    frequencies_mhz: np.ndarray,
    power_dbm: float,
    settle_ms: float = 5.0,
    frames_per_point: int = 1,
    discard_frames: int = 1,
    binning: int = 1,
    repeats: int = 1,
    alternate_direction: bool = True,
    estimate_errors: bool = True,
    on_progress=None,
    should_abort=None,
    after_repeat=None,
) -> OdmrResult:
    """Sweep the microwave, storing a full camera frame per frequency point.

    With ``repeats > 1`` the whole sweep is repeated and the frames at each
    frequency are averaged, improving signal-to-noise as sqrt(repeats).

    ``alternate_direction`` runs every second repeat from high to low
    frequency. This matters more than it looks: any slow drift during the
    measurement -- laser power wandering, NV bleaching, the sample creeping
    under pressure -- otherwise correlates with frequency, because
    frequency is always visited in the same time order. A downward-drifting
    baseline would then tilt the spectrum and could be mistaken for (or
    could hide) a real resonance. Alternating the direction makes the
    drift symmetric about the middle of the sweep instead, so averaging
    largely cancels it rather than baking it into the lineshape.

    ``estimate_errors`` additionally accumulates the sum of squares so a
    per-point standard error can be computed from the scatter *between*
    repeats. That doubles the memory used during acquisition, but it is
    what lets the plotted spectrum carry error bars, which is the only way
    to tell a shallow real dip from a noise excursion. It has no effect
    when ``repeats`` is 1, since a single measurement has no scatter.

    ``discard_frames`` throws away the first camera frame(s) after every
    frequency change. With a free-running camera that frame can straddle
    the old and the new microwave frequency -- especially when the exposure
    is longer than ``settle_ms`` -- so keeping it mixes adjacent frequency
    points together and adds a comb-like structure to the noise floor.

    ``after_repeat(repeats_completed, result_so_far)`` is called after each
    full repeat with the average so far; returning True ends the
    measurement there. That lets the caller stop as soon as the signal is
    good enough instead of always running every repeat.

    ``on_progress(repeat, index, freq_mhz, frame)`` is called after each
    frequency point. ``should_abort()`` is polled between points; returning
    True stops early. Points measured a different number of times are still
    averaged correctly -- each is divided by its own count -- and points
    never reached are dropped.
    """
    frequencies_mhz = np.asarray(frequencies_mhz, dtype=float)
    n_freq = len(frequencies_mhz)
    repeats = max(1, int(repeats))

    probe = bin_frame(camera.get_frame(), binning)
    shape = (n_freq, probe.shape[0], probe.shape[1])
    sums = np.zeros(shape, dtype=np.float32)
    sums_sq = np.zeros(shape, dtype=np.float32) if (estimate_errors and repeats > 1) else None
    counts = np.zeros(n_freq, dtype=np.int32)

    generator.set_power_dbm(power_dbm)
    generator.enable_rf(True)
    repeats_completed = 0
    aborted = False
    try:
        for repeat in range(repeats):
            order = range(n_freq)
            if alternate_direction and repeat % 2 == 1:
                order = range(n_freq - 1, -1, -1)

            for i in order:
                if should_abort is not None and should_abort():
                    aborted = True
                    break
                freq = float(frequencies_mhz[i])
                generator.set_frequency_mhz(freq)
                if settle_ms > 0:
                    time.sleep(settle_ms / 1000)
                for _ in range(max(0, int(discard_frames))):
                    camera.get_frame()

                accumulator = bin_frame(camera.get_frame(), binning)
                for _ in range(frames_per_point - 1):
                    accumulator += bin_frame(camera.get_frame(), binning)
                frame = accumulator / frames_per_point

                sums[i] += frame
                if sums_sq is not None:
                    sums_sq[i] += frame.astype(np.float32) ** 2
                counts[i] += 1

                if on_progress is not None:
                    on_progress(repeat, i, freq, frame)

            if aborted:
                break
            repeats_completed += 1
            if (
                after_repeat is not None
                and repeat < repeats - 1
                and after_repeat(
                    repeats_completed,
                    _assemble(frequencies_mhz, sums, sums_sq, counts, repeats_completed),
                )
            ):
                break
    finally:
        generator.enable_rf(False)

    return _assemble(frequencies_mhz, sums, sums_sq, counts, repeats_completed)


def _assemble(frequencies_mhz, sums, sums_sq, counts, repeats_completed) -> OdmrResult:
    """Average the accumulators into a result, dropping unmeasured points."""
    measured = counts > 0
    if not np.any(measured):
        raise RuntimeError("Sweep aborted before any frequency point was measured")

    freqs_out = frequencies_mhz[measured]
    counts_out = counts[measured]
    sums_out = sums[measured]
    divisor = counts_out[:, None, None].astype(np.float32)
    cube = sums_out / divisor

    sem = None
    if sums_sq is not None:
        sq_out = sums_sq[measured]
        with np.errstate(invalid="ignore", divide="ignore"):
            # Unbiased variance between repeats, then the error on their mean.
            variance = (sq_out - sums_out ** 2 / divisor) / np.maximum(divisor - 1.0, 1.0)
            sem = np.sqrt(np.maximum(variance, 0.0) / divisor).astype(np.float32)
        # A point measured only once has no scatter to estimate an error from.
        sem[counts_out < 2] = np.nan

    return OdmrResult(
        frequencies_mhz=freqs_out,
        cube=cube,
        counts=counts_out,
        sem=sem,
        repeats_completed=repeats_completed,
    )


def mw_on_off_check(
    camera,
    generator,
    freq_mhz: float,
    power_dbm: float,
    n_cycles: int = 10,
    settle_ms: float = 30.0,
    binning: int = 1,
    discard_frames: int = 1,
    on_progress=None,
    should_abort=None,
) -> tuple[np.ndarray, np.ndarray]:
    """Quick diagnostic: is the microwave modulating the photoluminescence at all?

    Parks the microwave at one frequency (pick a known resonance) and
    alternates RF off / RF on, averaging many frames of each. If NV centers
    are being excited by the laser *and* driven by the microwave, the
    "on" frames are measurably darker than the "off" frames. This takes
    seconds instead of a full sweep, so it is the fastest way to confirm
    the optical and microwave paths are working before committing to a
    measurement.

    Off and on frames are **interleaved** rather than measured in two
    blocks, so slow drifts -- laser power wandering, NV bleaching, sample
    creep -- affect both averages equally instead of masquerading as
    contrast. Each cycle further uses a symmetric OFF-ON-ON-OFF (ABBA)
    order: a plain OFF/ON pair leaves the two states separated by a fixed
    time offset, so a *linear* drift biases every cycle the same way and
    survives averaging as a false contrast. With ABBA the mean acquisition
    time of the OFF frames equals that of the ON frames, so a linear drift
    cancels exactly within each cycle.

    ``discard_frames`` frames are thrown away after each RF state change:
    a free-running camera may already be part-way through an exposure when
    the microwave switches, so that frame would be a mix of both states.

    Returns ``(mean_off_frame, mean_on_frame)``.
    """
    generator.set_power_dbm(power_dbm)
    generator.set_frequency_mhz(freq_mhz)

    off_sum = on_sum = None
    completed = 0
    try:
        for cycle in range(n_cycles):
            if should_abort is not None and should_abort():
                break
            off_frames, on_frames = [], []
            for state in (False, True, True, False):
                generator.enable_rf(state)
                if settle_ms > 0:
                    time.sleep(settle_ms / 1000)
                for _ in range(max(0, int(discard_frames))):
                    camera.get_frame()
                frame = bin_frame(camera.get_frame(), binning)
                (on_frames if state else off_frames).append(frame)

            cycle_off = 0.5 * (off_frames[0] + off_frames[1])
            cycle_on = 0.5 * (on_frames[0] + on_frames[1])
            if off_sum is None:
                off_sum, on_sum = cycle_off, cycle_on
            else:
                off_sum = off_sum + cycle_off
                on_sum = on_sum + cycle_on
            completed += 1
            if on_progress is not None:
                on_progress(completed, n_cycles)
    finally:
        generator.enable_rf(False)

    if completed == 0:
        raise RuntimeError("MW check aborted before any frames were acquired")
    return off_sum / completed, on_sum / completed


def difference_map(
    mean_off: np.ndarray, mean_on: np.ndarray, min_signal_fraction: float = 0.15
) -> np.ndarray:
    """Fractional PL drop caused by the microwave, ``(off - on) / off``.

    Positive where the microwave darkens the photoluminescence, i.e. where
    NV centers are responding. As in :func:`contrast_map`, pixels dimmer
    than ``min_signal_fraction`` of the brightest pixel are blanked so
    that shot noise on a near-zero background cannot fake a large signal.
    """
    with np.errstate(divide="ignore", invalid="ignore"):
        diff = (mean_off - mean_on) / np.maximum(mean_off, 1e-9)
    threshold = min_signal_fraction * float(mean_off.max())
    return np.where(mean_off >= threshold, np.nan_to_num(diff), 0.0)


def summarize_difference(diff: np.ndarray, sigma_threshold: float = 5.0) -> dict:
    """Decide whether a MW on/off check actually saw an NV response.

    A real ODMR response can only *darken* the photoluminescence, so the
    negative side of the difference distribution is pure measurement
    noise. That gives a self-calibrating test: count pixels darker than
    ``+threshold`` and brighter than ``-threshold`` for the same
    threshold. Noise alone produces the two counts in equal numbers, while
    a genuine response piles up only on the positive side. The *excess*
    of positives over negatives is therefore the number of pixels really
    responding, and it needs no absolute contrast cutoff -- important
    because per-pixel noise varies strongly with brightness across the
    frame, so any fixed "> 0.5 %" rule would misfire on the dim pixels.

    The noise width itself is estimated robustly (via the median absolute
    deviation of the negative tail) so that a large real signal cannot
    inflate it.
    """
    signal = diff[diff != 0.0]
    empty = {
        "max_pct": 0.0,
        "p99_pct": 0.0,
        "noise_pct": 0.0,
        "responding_px": 0,
        "false_positive_px": 0,
        "detected": False,
    }
    if signal.size == 0:
        return empty

    negative_tail = signal[signal < 0]
    if negative_tail.size < 10:
        return empty
    # For zero-centred noise, median(|x|) = 0.6745 sigma.
    noise = 1.4826 * float(np.median(np.abs(negative_tail)))
    if noise <= 0:
        return empty

    threshold = sigma_threshold * noise
    n_positive = int(np.count_nonzero(signal > threshold))
    n_negative = int(np.count_nonzero(signal < -threshold))
    excess = max(n_positive - n_negative, 0)

    # Require the positive tail to clearly dominate the (noise-only)
    # negative tail, and the excess to cover a non-negligible patch of the
    # frame rather than a handful of stray pixels.
    detected = n_positive > 3 * max(n_negative, 1) and excess > 0.0005 * signal.size

    return {
        "max_pct": 100.0 * float(np.max(signal)),
        "p99_pct": 100.0 * float(np.percentile(signal, 99)),
        "noise_pct": 100.0 * noise,
        "responding_px": excess,
        "false_positive_px": n_negative,
        "detected": detected,
    }


def roi_spectrum(cube: np.ndarray, roi: Roi) -> np.ndarray:
    """ODMR spectrum (mean PL vs. frequency index) for one ROI of the cube."""
    ys, xs = roi.slices(cube.shape)
    return cube[:, ys, xs].mean(axis=(1, 2))


def roi_spectrum_error(sem: np.ndarray | None, roi: Roi) -> np.ndarray | None:
    """Standard error of the ROI-averaged spectrum.

    Averaging ``n`` pixels whose individual standard errors are ``s_i``
    gives an error on the mean of ``sqrt(sum s_i^2) / n`` -- the errors add
    in quadrature, not linearly, so a larger ROI tightens the error bars as
    well as smoothing the image.
    """
    if sem is None:
        return None
    ys, xs = roi.slices(sem.shape)
    patch = sem[:, ys, xs]
    n_pixels = patch.shape[1] * patch.shape[2]
    if n_pixels == 0:
        return None
    return np.sqrt(np.nansum(patch ** 2, axis=(1, 2))) / n_pixels


def robust_baseline(
    frequencies_mhz: np.ndarray,
    spectrum: np.ndarray,
    reject_sigma: float = 2.5,
    iterations: int = 8,
) -> np.ndarray:
    """Off-resonance baseline a spectrum should be normalised against.

    Dividing by the single brightest point (the obvious choice) is
    fragile: one upward noise spike then defines 100 %, pushing the
    whole curve down and inflating the apparent contrast. It also
    leaves any linear baseline tilt from slow drift in the lineshape.

    So fit a straight line instead -- but fit it *robustly*. Fitting
    the two ends of the sweep only works if the resonances happen to
    sit in the middle; with the default 2800-2940 MHz range and NV
    resonances near 2820/2920 the dips fall inside the end windows,
    drag the fit down, and the contrast comes out badly under-reported.

    Instead fit all points and iteratively reject those lying well
    *below* the fit. Rejection is one-sided because an ODMR resonance
    can only darken the photoluminescence: downward outliers are
    signal, upward ones are noise. The scale is set by the median
    absolute deviation, which the dips cannot inflate as long as they
    occupy a minority of the sweep. The fit therefore converges onto
    the off-resonance baseline wherever the resonances happen to lie.

    Each point is weighted by the frequency span it stands for (half the
    gap to each neighbour). On an evenly spaced sweep that changes
    nothing; on a zoomed sweep (:func:`zoomed_axis`), where the resonances
    are sampled finely and the baseline sparsely, it stops the densely
    packed dip points from outvoting the baseline -- by point count the
    dips are then the majority, by frequency span they are not.

    Falls back to the spectrum's maximum when there are too few points
    to fit or the fit comes out non-positive.
    """
    spectrum = np.asarray(spectrum, dtype=float)
    n = len(spectrum)
    fallback = np.full(n, float(np.max(spectrum)))
    if n < 5:
        return fallback

    x = np.asarray(frequencies_mhz, dtype=float)
    span = np.abs(np.gradient(x)) if n > 1 else np.ones(n)
    weight = span / span.mean() if span.mean() > 0 else np.ones(n)
    mask = np.ones(n, dtype=bool)
    baseline = fallback
    for _ in range(iterations):
        if mask.sum() < 4 or weight[mask].sum() < 0.2 * weight.sum():
            break  # too little of the sweep left to trust the fit
        try:
            # polyfit weights multiply the residuals, hence the square root.
            coefficients = np.polyfit(x[mask], spectrum[mask], 1, w=np.sqrt(weight[mask]))
        except (np.linalg.LinAlgError, ValueError):
            return fallback
        baseline = np.polyval(coefficients, x)

        residual = spectrum - baseline
        centre = _weighted_median(residual, weight)
        spread = 1.4826 * _weighted_median(np.abs(residual - centre), weight)
        if spread <= 0:
            break
        keep = residual > -reject_sigma * spread
        if np.array_equal(keep, mask):
            break
        mask = keep

    if not np.all(np.isfinite(baseline)) or np.any(baseline <= 0):
        return fallback
    return baseline


def _weighted_median(values: np.ndarray, weights: np.ndarray) -> float:
    if np.allclose(weights, weights[0]):
        return float(np.median(values))  # evenly spaced sweep: the plain median
    order = np.argsort(values)
    cumulative = np.cumsum(weights[order])
    return float(values[order][np.searchsorted(cumulative, 0.5 * cumulative[-1])])


def normalize_spectrum(spectrum: np.ndarray, reference: str = "max") -> np.ndarray:
    """Convert raw counts to normalised PL in percent.

    ``reference='max'`` divides by the brightest (off-resonance) point;
    ``reference='median'`` divides by the median, which is more robust
    when the sweep barely extends past the resonances.
    """
    if reference == "median":
        ref = float(np.median(spectrum))
    else:
        ref = float(np.max(spectrum))
    if ref == 0:
        return np.zeros_like(spectrum)
    return 100.0 * spectrum / ref


def contrast_map(cube: np.ndarray, min_signal_fraction: float = 0.15) -> np.ndarray:
    """Per-pixel ODMR contrast, ``(max - min) / max`` over the sweep.

    Bright regions of this map are where the microwave actually modulates
    the photoluminescence, i.e. where the NV centers are -- useful for
    locating the NV layer inside a diamond anvil cell before picking a
    spot to read out.

    Dark pixels (outside the sample, or in the shadow of the gasket)
    collect almost no light, so their shot noise divided by a near-zero
    mean produces a large *spurious* contrast that would otherwise
    dominate the colour scale and hide the real signal. Pixels whose mean
    photoluminescence is below ``min_signal_fraction`` of the brightest
    pixel are therefore forced to zero. Lower the fraction if a genuinely
    dim part of the sample is being masked away.
    """
    peak = cube.max(axis=0)
    trough = cube.min(axis=0)
    contrast = np.where(peak > 0, (peak - trough) / np.maximum(peak, 1e-9), 0.0)

    mean_pl = cube.mean(axis=0)
    threshold = min_signal_fraction * float(mean_pl.max())
    return np.where(mean_pl >= threshold, contrast, 0.0)


def save_cube(path: str, result: OdmrResult, metadata: dict | None = None) -> None:
    """Save a measurement (uncompressed, so saving stays fast mid-experiment)."""
    import json

    arrays = {
        "frequencies_mhz": result.frequencies_mhz,
        "cube": result.cube,
        "counts": result.counts,
        "metadata": json.dumps({**(metadata or {}), "repeats_completed": result.repeats_completed}),
    }
    if result.sem is not None:
        arrays["sem"] = result.sem
    np.savez(path, **arrays)


def load_cube(path: str) -> tuple[OdmrResult, dict]:
    """Load a measurement saved by :func:`save_cube`."""
    import json

    data = np.load(path, allow_pickle=False)
    metadata = json.loads(str(data["metadata"])) if "metadata" in data else {}
    frequencies_mhz = data["frequencies_mhz"]
    counts = (
        data["counts"]
        if "counts" in data
        else np.ones(len(frequencies_mhz), dtype=np.int32)
    )
    result = OdmrResult(
        frequencies_mhz=frequencies_mhz,
        cube=data["cube"],
        counts=counts,
        sem=data["sem"] if "sem" in data else None,
        repeats_completed=int(metadata.get("repeats_completed", 1)),
    )
    return result, metadata
