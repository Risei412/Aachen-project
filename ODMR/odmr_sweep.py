"""Core ODMR sweep logic, independent of any GUI.

Given a camera object (``get_frame() -> np.ndarray``) and a microwave
generator object (``set_frequency_mhz``, ``set_power_dbm``, ``enable_rf``),
sweep the microwave frequency across a range and record the mean pixel
intensity inside a region of interest (ROI) at each step, i.e. a
widefield ODMR spectrum for whatever is in that ROI.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np


@dataclass
class Roi:
    """Square region of interest, in pixel coordinates."""

    x_center: int
    y_center: int
    half_size: int

    def slice(self):
        x0 = max(self.x_center - self.half_size, 0)
        x1 = self.x_center + self.half_size
        y0 = max(self.y_center - self.half_size, 0)
        y1 = self.y_center + self.half_size
        return slice(y0, y1), slice(x0, x1)

    def mean_intensity(self, frame: np.ndarray) -> float:
        ys, xs = self.slice()
        return float(np.mean(frame[ys, xs]))


def run_odmr_sweep(
    camera,
    generator,
    roi: Roi,
    start_mhz: float,
    stop_mhz: float,
    step_mhz: float,
    power_dbm: float,
    settle_ms: float = 5.0,
    frames_per_point: int = 1,
    on_point=None,
) -> tuple[np.ndarray, np.ndarray]:
    """Sweep frequency and record mean ROI intensity at each point.

    ``on_point(freq_mhz, intensity)`` is called after every frequency
    point, if provided, so a GUI can update its plot live.

    Returns ``(frequencies_mhz, intensities)`` as numpy arrays.
    """
    n_points = int(round((stop_mhz - start_mhz) / step_mhz)) + 1
    freqs = start_mhz + step_mhz * np.arange(n_points)
    intensities = np.zeros(n_points)

    generator.set_power_dbm(power_dbm)
    generator.enable_rf(True)
    try:
        for i, freq in enumerate(freqs):
            generator.set_frequency_mhz(float(freq))
            if settle_ms > 0:
                time.sleep(settle_ms / 1000)
            samples = [roi.mean_intensity(camera.get_frame()) for _ in range(frames_per_point)]
            intensities[i] = float(np.mean(samples))
            if on_point is not None:
                on_point(float(freq), intensities[i])
    finally:
        generator.enable_rf(False)

    return freqs, intensities
