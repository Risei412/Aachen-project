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
    contrast.

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
            frames = {}
            for state in (False, True):
                generator.enable_rf(state)
                if settle_ms > 0:
                    time.sleep(settle_ms / 1000)
                for _ in range(discard_frames):
                    camera.get_frame()
                frames[state] = bin_frame(camera.get_frame(), binning)

            if off_sum is None:
                off_sum = frames[False]
                on_sum = frames[True]
            else:
                off_sum = off_sum + frames[False]
                on_sum = on_sum + frames[True]
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
