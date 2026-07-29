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


def acquire_odmr_cube(
    camera,
    generator,
    frequencies_mhz: np.ndarray,
    power_dbm: float,
    settle_ms: float = 5.0,
    frames_per_point: int = 1,
    binning: int = 1,
    on_progress=None,
    should_abort=None,
) -> tuple[np.ndarray, np.ndarray]:
    """Sweep the microwave once, storing a full camera frame per frequency.

    ``on_progress(index, freq_mhz, frame)`` is called after each frequency
    point so a GUI can show the sweep advancing. ``should_abort()`` is
    polled between points; return True from it to stop early (the cube is
    then truncated to the points actually measured).

    Returns ``(frequencies_mhz, cube)`` where ``cube`` has shape
    ``(n_freq, height, width)`` and dtype float32.
    """
    frequencies_mhz = np.asarray(frequencies_mhz, dtype=float)
    n_freq = len(frequencies_mhz)

    probe = bin_frame(camera.get_frame(), binning)
    cube = np.zeros((n_freq, probe.shape[0], probe.shape[1]), dtype=np.float32)

    generator.set_power_dbm(power_dbm)
    generator.enable_rf(True)
    measured = 0
    try:
        for i, freq in enumerate(frequencies_mhz):
            if should_abort is not None and should_abort():
                break
            generator.set_frequency_mhz(float(freq))
            if settle_ms > 0:
                time.sleep(settle_ms / 1000)

            accumulator = bin_frame(camera.get_frame(), binning)
            for _ in range(frames_per_point - 1):
                accumulator += bin_frame(camera.get_frame(), binning)
            frame = accumulator / frames_per_point

            cube[i] = frame
            measured = i + 1
            if on_progress is not None:
                on_progress(i, float(freq), frame)
    finally:
        generator.enable_rf(False)

    return frequencies_mhz[:measured], cube[:measured]


def roi_spectrum(cube: np.ndarray, roi: Roi) -> np.ndarray:
    """ODMR spectrum (mean PL vs. frequency index) for one ROI of the cube."""
    ys, xs = roi.slices(cube.shape)
    return cube[:, ys, xs].mean(axis=(1, 2))


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


def save_cube(path: str, frequencies_mhz: np.ndarray, cube: np.ndarray, metadata: dict | None = None) -> None:
    """Save a measured datacube (uncompressed, so saving stays fast mid-experiment)."""
    import json

    np.savez(
        path,
        frequencies_mhz=frequencies_mhz,
        cube=cube,
        metadata=json.dumps(metadata or {}),
    )


def load_cube(path: str) -> tuple[np.ndarray, np.ndarray, dict]:
    """Load a datacube saved by :func:`save_cube`."""
    import json

    data = np.load(path, allow_pickle=False)
    metadata = json.loads(str(data["metadata"])) if "metadata" in data else {}
    return data["frequencies_mhz"], data["cube"], metadata
