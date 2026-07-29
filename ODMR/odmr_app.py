#!/usr/bin/env python3
"""Widefield ODMR viewer: sweep once, then click any pixel to read its spectrum.

Workflow:

  1. The app opens on a live view of the Thorlabs CMOS camera so you can
     focus and position the sample.
  2. Press "Run sweep". The microwave frequency is swept **once** across
     the configured range while a full camera frame is stored at every
     frequency point, producing a datacube (frequency x height x width).
  3. When the sweep finishes the camera image is displayed. **Click
     anywhere on it** and the ODMR spectrum of that spot is plotted
     instantly -- it is just a slice through the datacube, so no further
     hardware access is needed and you can probe as many points as you
     like.
  4. Toggle "View: contrast" to display the per-pixel ODMR contrast map,
     which shows where the microwave actually modulates the
     photoluminescence -- i.e. where the NV centers are.
  5. "Save" writes the datacube to an .npz file; re-open it later with
     ``python odmr_app.py --load run001.npz`` to keep clicking around the
     data without any hardware attached.

Usage:

    python odmr_app.py                      # mock hardware (see config.yaml)
    python odmr_app.py --no-mock            # real camera + SynthHD
    python odmr_app.py --load run001.npz    # re-analyse a saved measurement
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
from datetime import datetime

import numpy as np
import yaml
from matplotlib import pyplot as plt
from matplotlib.patches import Rectangle
from matplotlib.widgets import Button

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Equipments", "CMOS camera"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Equipments", "Microwave generator"))

from odmr_sweep import (  # noqa: E402
    Roi,
    acquire_odmr_cube,
    contrast_map,
    estimate_cube_bytes,
    frequency_axis,
    load_cube,
    normalize_spectrum,
    roi_spectrum,
    save_cube,
)
from thorlabs_camera import MockThorlabsCamera, ThorlabsCamera  # noqa: E402
from synthhd import MockSynthHD, SynthHD  # noqa: E402


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class OdmrApp:
    def __init__(self, config: dict, cube_file: str | None = None):
        self.config = config
        self.sweep_cfg = config["sweep"]
        self.mw_cfg = config["microwave"]
        self.binning = int(config["camera"].get("binning", 1))
        self.roi_half_size = int(config["roi"]["half_size_px"])
        self.min_signal_fraction = float(
            config.get("analysis", {}).get("min_signal_fraction", 0.15)
        )

        # Measurement results, filled in once a sweep has run (or been loaded).
        self.frequencies_mhz: np.ndarray | None = None
        self.cube: np.ndarray | None = None
        self._contrast: np.ndarray | None = None
        self._show_contrast = False
        self._sweeping = False
        self._abort = False
        self.roi_patch = None

        self.camera = self.generator = None
        if cube_file is None:
            self.camera, self.generator = self._build_hardware(config)

        self._build_figure()

        if cube_file is not None:
            self._load_measurement(cube_file)
        else:
            self._start_live_view()

    # ---------------------------------------------------------------- hardware

    def _build_hardware(self, config: dict):
        if config.get("mock", True):
            gen = MockSynthHD(channel=config["microwave"]["channel"])
            cam = MockThorlabsCamera(exposure_ms=config["camera"]["exposure_ms"])
            gen._on_state_change = lambda freq, power, rf_on: cam.set_mw_state(freq, power, rf_on)
            return cam, gen
        cam = ThorlabsCamera(exposure_ms=config["camera"]["exposure_ms"])
        gen = SynthHD(port=config["microwave"]["port"], channel=config["microwave"]["channel"])
        return cam, gen

    # ------------------------------------------------------------------- figure

    def _build_figure(self) -> None:
        self.fig, (self.ax_img, self.ax_spec) = plt.subplots(1, 2, figsize=(13, 5.5))
        self.fig.canvas.manager.set_window_title("Widefield ODMR viewer")
        self.fig.subplots_adjust(bottom=0.18, wspace=0.25)

        placeholder = np.zeros((10, 10))
        self.im = self.ax_img.imshow(placeholder, cmap="gray", origin="upper")
        self.ax_img.set_title("Live view")
        self.ax_img.set_xlabel("x (px)")
        self.ax_img.set_ylabel("y (px)")

        (self.spectrum_line,) = self.ax_spec.plot([], [], "o-", markersize=3, color="tab:blue")
        self.ax_spec.set_xlabel("MW frequency (MHz)")
        self.ax_spec.set_ylabel("Normalised PL (%)")
        self.ax_spec.set_title("ODMR spectrum — run a sweep, then click the image")
        self.ax_spec.grid(True, alpha=0.3)

        self.btn_sweep = Button(self.fig.add_axes([0.08, 0.04, 0.14, 0.07]), "Run sweep")
        self.btn_sweep.on_clicked(self._on_sweep_clicked)
        self.btn_view = Button(self.fig.add_axes([0.24, 0.04, 0.16, 0.07]), "View: PL")
        self.btn_view.on_clicked(self._on_view_clicked)
        self.btn_save = Button(self.fig.add_axes([0.42, 0.04, 0.12, 0.07]), "Save")
        self.btn_save.on_clicked(self._on_save_clicked)

        self.status = self.fig.text(0.58, 0.06, "", fontsize=9, va="center")

        self.fig.canvas.mpl_connect("button_press_event", self._on_click)
        self.fig.canvas.mpl_connect("close_event", self._on_close)

    def _set_status(self, text: str) -> None:
        self.status.set_text(text)
        self.fig.canvas.draw_idle()

    def _show_image(self, image: np.ndarray, title: str) -> None:
        self.im.set_data(image)
        self.im.set_clim(float(np.min(image)), float(np.max(image)) or 1.0)
        self.im.set_extent((-0.5, image.shape[1] - 0.5, image.shape[0] - 0.5, -0.5))
        self.ax_img.set_xlim(-0.5, image.shape[1] - 0.5)
        self.ax_img.set_ylim(image.shape[0] - 0.5, -0.5)
        self.ax_img.set_title(title)
        self.fig.canvas.draw_idle()

    # ---------------------------------------------------------------- live view

    def _start_live_view(self) -> None:
        self._live_timer = self.fig.canvas.new_timer(interval=100)
        self._live_timer.add_callback(self._update_live_view)
        self._live_timer.start()
        self._set_status("Live view — press 'Run sweep' to measure.")

    def _update_live_view(self) -> None:
        if self._sweeping or self.camera is None or self.cube is not None:
            return
        try:
            frame = self.camera.get_frame(timeout_ms=100)
        except TimeoutError:
            return
        self._show_image(frame, "Live view — press 'Run sweep' to measure")

    # -------------------------------------------------------------------- sweep

    def _on_sweep_clicked(self, event) -> None:
        if self.camera is None:
            self._set_status("No hardware attached (opened from a saved file).")
            return
        if self._sweeping:
            self._abort = True
            self._set_status("Aborting sweep after the current point…")
            return
        threading.Thread(target=self._run_sweep, daemon=True).start()

    def _run_sweep(self) -> None:
        self._sweeping = True
        self._abort = False
        self.btn_sweep.label.set_text("Abort")

        freqs = frequency_axis(
            self.sweep_cfg["start_mhz"], self.sweep_cfg["stop_mhz"], self.sweep_cfg["step_mhz"]
        )
        probe = self.camera.get_frame()
        n_mb = estimate_cube_bytes(len(freqs), probe.shape[0], probe.shape[1], self.binning) / 1e6
        print(
            f"Sweeping {len(freqs)} points, {self.sweep_cfg['start_mhz']}–"
            f"{self.sweep_cfg['stop_mhz']} MHz. Datacube ≈ {n_mb:.0f} MB "
            f"(binning={self.binning})."
        )

        def on_progress(index: int, freq_mhz: float, frame: np.ndarray) -> None:
            self._show_image(frame, f"Sweeping… {freq_mhz:.1f} MHz")
            self._set_status(f"Sweep {index + 1}/{len(freqs)} — {freq_mhz:.1f} MHz")

        try:
            self.frequencies_mhz, self.cube = acquire_odmr_cube(
                self.camera,
                self.generator,
                freqs,
                power_dbm=self.mw_cfg["power_dbm"],
                settle_ms=self.sweep_cfg["settle_ms"],
                frames_per_point=self.sweep_cfg["frames_per_point"],
                binning=self.binning,
                on_progress=on_progress,
                should_abort=lambda: self._abort,
            )
            self._contrast = contrast_map(self.cube, self.min_signal_fraction)
            self._refresh_image_view()
            self._set_status(
                f"Sweep done: {len(self.frequencies_mhz)} points. Click the image to read ODMR."
            )
        except Exception as exc:  # surface hardware errors instead of freezing silently
            print(f"ODMR sweep failed: {exc}")
            self._set_status(f"Sweep failed: {exc}")
        finally:
            self._sweeping = False
            self.btn_sweep.label.set_text("Run sweep")
            self.fig.canvas.draw_idle()

    # ------------------------------------------------------------ image display

    def _refresh_image_view(self) -> None:
        if self.cube is None:
            return
        if self._show_contrast:
            self._show_image(100.0 * self._contrast, "ODMR contrast map (%) — click to read ODMR")
        else:
            self._show_image(self.cube.mean(axis=0), "Mean PL over sweep — click to read ODMR")
        if self.roi_patch is not None:
            self.ax_img.add_patch(self.roi_patch)

    def _on_view_clicked(self, event) -> None:
        if self.cube is None:
            self._set_status("Run a sweep first.")
            return
        self._show_contrast = not self._show_contrast
        self.btn_view.label.set_text("View: contrast" if self._show_contrast else "View: PL")
        self._refresh_image_view()

    # --------------------------------------------------------- click -> spectrum

    def _on_click(self, event) -> None:
        if event.inaxes is not self.ax_img or event.xdata is None:
            return
        if self.cube is None:
            self._set_status("Run a sweep first — then clicking shows that spot's ODMR.")
            return
        if self._sweeping:
            return

        roi = Roi(
            x_center=int(round(event.xdata)),
            y_center=int(round(event.ydata)),
            half_size=self.roi_half_size,
        )
        self._draw_roi(roi)
        self._plot_spectrum(roi)

    def _draw_roi(self, roi: Roi) -> None:
        if self.roi_patch is not None:
            self.roi_patch.remove()
        size = 2 * roi.half_size + 1
        self.roi_patch = Rectangle(
            (roi.x_center - roi.half_size - 0.5, roi.y_center - roi.half_size - 0.5),
            size, size, edgecolor="red", facecolor="none", linewidth=1.5,
        )
        self.ax_img.add_patch(self.roi_patch)

    def _plot_spectrum(self, roi: Roi) -> None:
        raw = roi_spectrum(self.cube, roi)
        normalised = normalize_spectrum(raw, reference="max")

        self.spectrum_line.set_data(self.frequencies_mhz, normalised)
        self.ax_spec.relim()
        self.ax_spec.autoscale_view()
        self.ax_spec.set_title(f"ODMR at pixel ({roi.x_center}, {roi.y_center})")

        dip_freq = float(self.frequencies_mhz[int(np.argmin(normalised))])
        contrast_pct = 100.0 - float(np.min(normalised))
        self._set_status(
            f"ROI ({roi.x_center}, {roi.y_center}): deepest dip {dip_freq:.1f} MHz, "
            f"contrast {contrast_pct:.2f} %"
        )
        self.fig.canvas.draw_idle()

    # --------------------------------------------------------------- save / load

    def _on_save_clicked(self, event) -> None:
        if self.cube is None:
            self._set_status("Nothing to save — run a sweep first.")
            return
        out_dir = self.config.get("output", {}).get("directory", ".")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"odmr_{datetime.now():%Y%m%d_%H%M%S}.npz")
        save_cube(
            path,
            self.frequencies_mhz,
            self.cube,
            metadata={
                "binning": self.binning,
                "power_dbm": self.mw_cfg["power_dbm"],
                "exposure_ms": self.config["camera"]["exposure_ms"],
                "frames_per_point": self.sweep_cfg["frames_per_point"],
            },
        )
        print(f"Saved datacube to {path}")
        self._set_status(f"Saved to {path}")

    def _load_measurement(self, path: str) -> None:
        self.frequencies_mhz, self.cube, metadata = load_cube(path)
        self.binning = int(metadata.get("binning", self.binning))
        self._contrast = contrast_map(self.cube, self.min_signal_fraction)
        self._refresh_image_view()
        self._set_status(f"Loaded {path} — click the image to read ODMR.")
        print(f"Loaded {path}: {self.cube.shape[0]} frequency points, metadata={metadata}")

    # ------------------------------------------------------------------ teardown

    def _on_close(self, event) -> None:
        timer = getattr(self, "_live_timer", None)
        if timer is not None:
            timer.stop()
        if self.camera is not None:
            self.camera.close()
        if self.generator is not None:
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
    parser.add_argument("--load", default=None, help="Open a saved .npz datacube instead of measuring")
    parser.add_argument("--mock", dest="mock", action="store_true", default=None)
    parser.add_argument("--no-mock", dest="mock", action="store_false")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mock is not None:
        config["mock"] = args.mock

    OdmrApp(config, cube_file=args.load).run()


if __name__ == "__main__":
    main()
