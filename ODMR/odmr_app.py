#!/usr/bin/env python3
"""Click-to-measure ODMR viewer.

Shows a live view of the Thorlabs CMOS camera. Click anywhere on the
image to select a region of interest (a small square around the click
point); the app then sweeps the Windfreak SynthHD microwave frequency
across the configured range and plots the resulting ODMR spectrum
(mean ROI brightness vs. MW frequency) in the right-hand panel.

Usage (from VS Code: Run > Run Without Debugging, or a terminal):

    python odmr_app.py                      # uses ODMR/config.yaml, mock hardware
    python odmr_app.py --config config.yaml
    python odmr_app.py --no-mock            # use real camera + SynthHD

See ODMR/README.md for full setup instructions.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading

import matplotlib
import numpy as np
import yaml
from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Equipments", "CMOS camera"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Equipments", "Microwave generator"))

from odmr_sweep import Roi, run_odmr_sweep  # noqa: E402
from thorlabs_camera import MockThorlabsCamera, ThorlabsCamera  # noqa: E402
from synthhd import MockSynthHD, SynthHD  # noqa: E402


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class OdmrApp:
    def __init__(self, config: dict):
        self.config = config
        self.roi_half_size = int(config["roi"]["half_size_px"])
        self.sweep_cfg = config["sweep"]
        self.mw_cfg = config["microwave"]

        self.camera, self.generator = self._build_hardware(config)

        self.fig, (self.ax_cam, self.ax_odmr) = plt.subplots(1, 2, figsize=(11, 5))
        self.fig.canvas.manager.set_window_title("ODMR click-to-measure viewer")

        frame = self.camera.get_frame()
        self.im = self.ax_cam.imshow(frame, cmap="gray", vmin=0, vmax=frame.max() or 1)
        self.ax_cam.set_title("CMOS live view — click a spot to measure ODMR there")
        self.roi_patch = None

        (self.spectrum_line,) = self.ax_odmr.plot([], [], "o-", markersize=3)
        self.ax_odmr.set_xlabel("MW frequency (MHz)")
        self.ax_odmr.set_ylabel("Mean ROI intensity (counts)")
        self.ax_odmr.set_title("ODMR spectrum")
        self.ax_odmr.grid(True, alpha=0.3)

        self._sweep_lock = threading.Lock()
        self._sweeping = False
        self._live_timer = self.fig.canvas.new_timer(interval=50)
        self._live_timer.add_callback(self._update_live_view)
        self._live_timer.start()

        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("close_event", self._on_close)

    def _build_hardware(self, config: dict):
        if config.get("mock", True):
            gen = MockSynthHD(channel=config["microwave"]["channel"])
            cam = MockThorlabsCamera(exposure_ms=config["camera"]["exposure_ms"])
            gen._on_state_change = lambda freq, power, rf_on: cam.set_mw_state(freq, power, rf_on)
            return cam, gen
        cam = ThorlabsCamera(exposure_ms=config["camera"]["exposure_ms"])
        gen = SynthHD(port=config["microwave"]["port"], channel=config["microwave"]["channel"])
        return cam, gen

    def _update_live_view(self) -> None:
        if self._sweeping:
            return  # sweep thread owns the camera timing while it runs
        try:
            frame = self.camera.get_frame(timeout_ms=100)
        except TimeoutError:
            return
        self.im.set_data(frame)
        self.fig.canvas.draw_idle()

    def _on_click(self, event) -> None:
        if event.inaxes is not self.ax_cam or event.xdata is None:
            return
        if self._sweeping:
            print("Sweep already running, ignoring click until it finishes.")
            return

        x, y = int(round(event.xdata)), int(round(event.ydata))
        roi = Roi(x_center=x, y_center=y, half_size=self.roi_half_size)
        self._draw_roi(roi)
        threading.Thread(target=self._run_sweep, args=(roi,), daemon=True).start()

    def _draw_roi(self, roi: Roi) -> None:
        if self.roi_patch is not None:
            self.roi_patch.remove()
        s = roi.half_size
        self.roi_patch = Rectangle(
            (roi.x_center - s, roi.y_center - s), 2 * s, 2 * s,
            edgecolor="red", facecolor="none", linewidth=1.5,
        )
        self.ax_cam.add_patch(self.roi_patch)
        self.fig.canvas.draw_idle()

    def _run_sweep(self, roi: Roi) -> None:
        with self._sweep_lock:
            self._sweeping = True
            freqs_done, ints_done = [], []

            def on_point(freq_mhz: float, intensity: float) -> None:
                freqs_done.append(freq_mhz)
                ints_done.append(intensity)
                self.spectrum_line.set_data(freqs_done, ints_done)
                self.ax_odmr.relim()
                self.ax_odmr.autoscale_view()
                self.fig.canvas.draw_idle()

            self.ax_odmr.set_title(
                f"ODMR spectrum — ROI @ ({roi.x_center}, {roi.y_center})"
            )
            self.spectrum_line.set_data([], [])
            try:
                run_odmr_sweep(
                    self.camera,
                    self.generator,
                    roi,
                    start_mhz=self.sweep_cfg["start_mhz"],
                    stop_mhz=self.sweep_cfg["stop_mhz"],
                    step_mhz=self.sweep_cfg["step_mhz"],
                    power_dbm=self.mw_cfg["power_dbm"],
                    settle_ms=self.sweep_cfg["settle_ms"],
                    frames_per_point=self.sweep_cfg["frames_per_point"],
                    on_point=on_point,
                )
            except Exception as exc:  # surface hardware errors instead of a silent freeze
                print(f"ODMR sweep failed: {exc}")
            finally:
                self._sweeping = False

    def _on_close(self, event) -> None:
        self._live_timer.stop()
        self.camera.close()
        self.generator.close()

    def run(self) -> None:
        plt.show()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        default=os.path.join(os.path.dirname(__file__), "config.yaml"),
        help="Path to config.yaml (default: ODMR/config.yaml)",
    )
    parser.add_argument("--mock", dest="mock", action="store_true", default=None)
    parser.add_argument("--no-mock", dest="mock", action="store_false")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mock is not None:
        config["mock"] = args.mock

    app = OdmrApp(config)
    app.run()


if __name__ == "__main__":
    main()
