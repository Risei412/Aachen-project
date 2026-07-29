"""Write a compact, shareable bundle from a measured ODMR datacube.

The raw datacube is far too large for version control -- a 141-point sweep
on a 1440x1080 camera is 877 MB, and GitHub refuses files over 100 MB. So
the cube itself stays local (``ODMR/data/``, git-ignored) and this module
writes a small directory that *is* worth committing:

    results/<timestamp>/
        metadata.json      every setting used, plus the git commit that
                           produced it, so a result can be traced back to
                           the exact code and configuration
        spectrum_*.csv     the ODMR spectrum of each exported ROI
        spectrum.png       plotted spectra
        maps.png           mean photoluminescence and ODMR contrast map
        maps.npz           those two images as float32 arrays (~1 MB), so
                           the shared result carries real numbers and not
                           only pictures
        README.md          human-readable summary

Use ``publish_results.py`` to commit and push a bundle.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime

import numpy as np

from odmr_sweep import OdmrResult, Roi, contrast_map, roi_spectrum, roi_spectrum_error


def _git_commit() -> str:
    """Current commit hash, so a result can be tied to the code that made it."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5,
        )
        commit = out.stdout.strip()
        if out.returncode != 0 or not commit:
            return "unknown"
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=os.path.dirname(os.path.abspath(__file__)),
            capture_output=True,
            text=True,
            timeout=5,
        )
        # An uncommitted working tree means the commit hash alone does not
        # describe the code that ran; say so rather than implying it does.
        return commit + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def spectrum_table(
    result: OdmrResult, roi: Roi, baseline: np.ndarray | None = None
) -> dict[str, np.ndarray]:
    """Columns describing one ROI's ODMR spectrum."""
    raw = roi_spectrum(result.cube, roi)
    reference = baseline if baseline is not None else np.full(len(raw), float(np.max(raw)))
    error = roi_spectrum_error(result.sem, roi)

    table = {
        "frequency_mhz": result.frequencies_mhz,
        "pl_counts": raw,
        "pl_normalised_pct": 100.0 * raw / reference,
        "baseline_counts": reference,
        "repeats": result.counts,
    }
    if error is not None:
        table["error_pct"] = 100.0 * error / reference
    return table


def _write_csv(path: str, table: dict[str, np.ndarray]) -> None:
    columns = list(table)
    rows = np.column_stack([np.asarray(table[c], dtype=float) for c in columns])
    header = ",".join(columns)
    np.savetxt(path, rows, delimiter=",", header=header, comments="", fmt="%.6g")


def export_measurement(
    out_root: str,
    result: OdmrResult,
    config: dict,
    rois: list[Roi],
    baselines: list[np.ndarray] | None = None,
    min_signal_fraction: float = 0.15,
    notes: str = "",
) -> str:
    """Write the bundle described in the module docstring. Returns its path."""
    import matplotlib

    matplotlib.use("Agg", force=False)
    from matplotlib import pyplot as plt

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = os.path.join(out_root, stamp)
    os.makedirs(out_dir, exist_ok=True)

    mean_pl = result.cube.mean(axis=0)
    contrast = contrast_map(result.cube, min_signal_fraction)

    # ---- metadata: everything needed to reproduce or audit the result ----
    metadata = {
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "git_commit": _git_commit(),
        "frequencies_mhz": {
            "start": float(result.frequencies_mhz[0]),
            "stop": float(result.frequencies_mhz[-1]),
            "n_points": int(len(result.frequencies_mhz)),
        },
        "repeats_completed": int(result.repeats_completed),
        "repeats_per_point": {
            "min": int(result.counts.min()),
            "max": int(result.counts.max()),
        },
        "cube_shape": list(result.cube.shape),
        "has_error_bars": result.sem is not None,
        "config": config,
        "rois": [
            {"x": r.x_center, "y": r.y_center, "half_size_px": r.half_size} for r in rois
        ],
        "notes": notes,
    }
    with open(os.path.join(out_dir, "metadata.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False, default=str)

    # ---- per-ROI spectra as CSV, plus a combined plot ----
    fig, ax = plt.subplots(figsize=(7, 4.5))
    summary_rows = []
    for index, roi in enumerate(rois):
        baseline = baselines[index] if baselines is not None else None
        table = spectrum_table(result, roi, baseline)
        name = f"spectrum_x{roi.x_center}_y{roi.y_center}_r{roi.half_size}"
        _write_csv(os.path.join(out_dir, name + ".csv"), table)

        normalised = table["pl_normalised_pct"]
        label = f"({roi.x_center}, {roi.y_center}) ±{roi.half_size} px"
        ax.plot(table["frequency_mhz"], normalised, "o-", markersize=3, label=label)
        if "error_pct" in table:
            ax.fill_between(
                table["frequency_mhz"],
                normalised - table["error_pct"],
                normalised + table["error_pct"],
                alpha=0.25,
                linewidth=0,
            )

        dip = int(np.argmin(normalised))
        summary_rows.append(
            {
                "roi": label,
                "dip_mhz": float(table["frequency_mhz"][dip]),
                "contrast_pct": 100.0 - float(normalised[dip]),
                "error_pct": float(np.nanmedian(table["error_pct"]))
                if "error_pct" in table
                else float("nan"),
            }
        )

    ax.set_xlabel("MW frequency (MHz)")
    ax.set_ylabel("Normalised PL (%)")
    ax.set_title("ODMR spectra")
    ax.grid(True, alpha=0.3)
    if len(rois) > 1:
        ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "spectrum.png"), dpi=150)
    plt.close(fig)

    # ---- images: mean PL and contrast map ----
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    for axis, image, title in (
        (axes[0], mean_pl, "Mean PL over sweep"),
        (axes[1], 100.0 * contrast, "ODMR contrast (%)"),
    ):
        handle = axis.imshow(image, cmap="gray", origin="upper")
        axis.set_title(title)
        axis.set_xlabel("x (px)")
        axis.set_ylabel("y (px)")
        fig.colorbar(handle, ax=axis, fraction=0.046)
        for roi in rois:
            axis.add_patch(
                plt.Rectangle(
                    (roi.x_center - roi.half_size - 0.5, roi.y_center - roi.half_size - 0.5),
                    2 * roi.half_size + 1,
                    2 * roi.half_size + 1,
                    edgecolor="red",
                    facecolor="none",
                    linewidth=1.2,
                )
            )
    fig.tight_layout()
    fig.savefig(os.path.join(out_dir, "maps.png"), dpi=150)
    plt.close(fig)

    # The two maps as real arrays: small enough to version, unlike the cube.
    np.savez_compressed(
        os.path.join(out_dir, "maps.npz"),
        mean_pl=mean_pl.astype(np.float32),
        contrast=contrast.astype(np.float32),
        frequencies_mhz=result.frequencies_mhz,
    )

    # ---- human-readable summary ----
    sweep = config.get("sweep", {})
    camera = config.get("camera", {})
    lines = [
        f"# ODMR measurement {stamp}",
        "",
        f"- Frequencies: {metadata['frequencies_mhz']['start']:g} – "
        f"{metadata['frequencies_mhz']['stop']:g} MHz, "
        f"{metadata['frequencies_mhz']['n_points']} points "
        f"(step {sweep.get('step_mhz', '?')} MHz)",
        f"- Repeats completed: {result.repeats_completed}",
        f"- Exposure: {camera.get('exposure_ms', '?')} ms, "
        f"{sweep.get('frames_per_point', '?')} frames/point, binning {camera.get('binning', 1)}",
        f"- MW power: {config.get('microwave', {}).get('power_dbm', '?')} dBm",
        f"- Code version: `{metadata['git_commit']}`",
        "",
        "## ROI results",
        "",
        "| ROI | Deepest dip (MHz) | Contrast (%) | Typical error (%) |",
        "| --- | --- | --- | --- |",
    ]
    for row in summary_rows:
        error = "n/a" if np.isnan(row["error_pct"]) else f"{row['error_pct']:.3f}"
        significance = ""
        if not np.isnan(row["error_pct"]) and row["error_pct"] > 0:
            if row["contrast_pct"] < 3.0 * row["error_pct"]:
                significance = " (below 3× error)"
        lines.append(
            f"| {row['roi']} | {row['dip_mhz']:.1f} | "
            f"{row['contrast_pct']:.2f}{significance} | {error} |"
        )
    if notes:
        lines += ["", "## Notes", "", notes]
    lines += [
        "",
        "The raw datacube is not included here (too large for version control); "
        "it stays in `ODMR/data/` on the acquisition machine.",
        "",
    ]
    with open(os.path.join(out_dir, "README.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return out_dir
