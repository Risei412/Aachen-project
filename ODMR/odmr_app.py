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
from matplotlib.widgets import Button, Slider, TextBox

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Equipments", "CMOS camera"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Equipments", "Microwave generator"))

from odmr_sweep import (  # noqa: E402
    Roi,
    acquire_odmr_cube,
    contrast_map,
    difference_map,
    estimate_cube_bytes,
    frequency_axis,
    OdmrResult,
    load_cube,
    mw_on_off_check,
    normalize_spectrum,
    roi_spectrum,
    roi_spectrum_error,
    save_cube,
    summarize_difference,
)
from export_results import export_measurement  # noqa: E402
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
        roi_limits = config["roi"].get("half_size_limits", [1, 60])
        self.roi_half_size_limits = (int(roi_limits[0]), int(roi_limits[1]))
        analysis_cfg = config.get("analysis", {}) or {}
        self.min_signal_fraction = float(analysis_cfg.get("min_signal_fraction", 0.15))
        self.baseline_correction = bool(analysis_cfg.get("baseline_correction", True))
        self.baseline_reject_sigma = float(analysis_cfg.get("baseline_reject_sigma", 2.5))
        self.baseline_iterations = int(analysis_cfg.get("baseline_iterations", 8))
        self.diag_cfg = config.get("diagnostic", {}) or {}
        freq_limits = config["microwave"].get("freq_limits_mhz", [54.0, 13600.0])
        self.freq_limits_mhz = (float(freq_limits[0]), float(freq_limits[1]))
        self._updating_params = False
        self._frame_shape: tuple[int, int] | None = None
        self.exposure_ms = float(config["camera"]["exposure_ms"])
        limits = config["camera"].get("exposure_limits_ms", [0.05, 1000.0])
        self.exposure_limits_ms = (float(limits[0]), float(limits[1]))
        self.exposure_ms = float(
            np.clip(self.exposure_ms, *self.exposure_limits_ms)
        )

        # Measurement results, filled in once a sweep has run (or been loaded).
        self.frequencies_mhz: np.ndarray | None = None
        self.cube: np.ndarray | None = None
        self.sem: np.ndarray | None = None
        self.counts: np.ndarray | None = None
        self.repeats_completed = 0
        self._last_roi: Roi | None = None
        self._error_band = None
        self._contrast: np.ndarray | None = None
        self._diff_map: np.ndarray | None = None
        self._diff_freq_mhz: float | None = None
        self._view_mode = "pl"
        self._sweeping = False
        self._abort = False
        self.roi_patch = None

        self.camera = self.generator = None
        if cube_file is None:
            self.camera, self.generator = self._build_hardware(config)

        self._build_figure()

        if cube_file is not None:
            self.slider_exposure.set_active(False)
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
        cam = ThorlabsCamera(
            exposure_ms=config["camera"]["exposure_ms"],
            dll_dir=config["camera"].get("dll_dir"),
            sdk_source_dir=config["camera"].get("sdk_source_dir"),
        )
        gen = SynthHD(port=config["microwave"]["port"], channel=config["microwave"]["channel"])
        return cam, gen

    # ------------------------------------------------------------------- figure

    def _build_figure(self) -> None:
        self.fig, (self.ax_img, self.ax_spec) = plt.subplots(1, 2, figsize=(13, 5.5))
        self.fig.canvas.manager.set_window_title("Widefield ODMR viewer")
        self.fig.subplots_adjust(bottom=0.30, wspace=0.25)

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

        self.btn_sweep = Button(self.fig.add_axes([0.045, 0.03, 0.105, 0.06]), "Run sweep")
        self.btn_sweep.on_clicked(self._on_sweep_clicked)
        self.btn_mwcheck = Button(self.fig.add_axes([0.16, 0.03, 0.105, 0.06]), "MW check")
        self.btn_mwcheck.on_clicked(self._on_mwcheck_clicked)
        self.btn_view = Button(self.fig.add_axes([0.275, 0.03, 0.125, 0.06]), "View: PL")
        self.btn_view.on_clicked(self._on_view_clicked)
        self.btn_save = Button(self.fig.add_axes([0.41, 0.03, 0.085, 0.06]), "Save raw")
        self.btn_save.on_clicked(self._on_save_clicked)
        self.btn_export = Button(self.fig.add_axes([0.505, 0.03, 0.085, 0.06]), "Export")
        self.btn_export.on_clicked(self._on_export_clicked)

        # Sweep parameters are editable before the run rather than only via
        # config.yaml, so the range can be narrowed onto a resonance found by
        # a previous sweep without restarting the app.
        self.param_boxes = {}
        specs = [
            ("start_mhz", "Start MHz", [0.080, 0.175, 0.070, 0.042]),
            ("stop_mhz", "Stop", [0.205, 0.175, 0.070, 0.042]),
            ("step_mhz", "Step", [0.320, 0.175, 0.050, 0.042]),
            ("repeats", "Repeats", [0.440, 0.175, 0.040, 0.042]),
        ]
        for key, label, rect in specs:
            box = TextBox(self.fig.add_axes(rect), label, initial=self._param_text(key))
            box.on_submit(lambda text, k=key: self._on_param_submit(k, text))
            self.param_boxes[key] = box

        self.plan_text = self.fig.text(0.53, 0.196, "", fontsize=8.5, va="center")

        # Exposure is set on a logarithmic scale: usable values span three
        # decades (a bright reflection needs tens of microseconds, a dim NV
        # ensemble hundreds of milliseconds), which a linear slider cannot
        # resolve at both ends.
        self.slider_exposure = Slider(
            self.fig.add_axes([0.09, 0.115, 0.28, 0.022]),
            "Exposure",
            np.log10(self.exposure_limits_ms[0]),
            np.log10(self.exposure_limits_ms[1]),
            valinit=np.log10(self.exposure_ms),
        )
        self.slider_exposure.on_changed(self._on_exposure_changed)
        self._update_exposure_label(self.exposure_ms)

        # ROI size is re-applied to the already-measured datacube, so it can
        # be explored freely after the sweep without re-acquiring anything.
        self.slider_roi = Slider(
            self.fig.add_axes([0.56, 0.115, 0.28, 0.022]),
            "ROI ±px",
            self.roi_half_size_limits[0],
            self.roi_half_size_limits[1],
            valinit=self.roi_half_size,
            valstep=1,
        )
        self.slider_roi.on_changed(self._on_roi_size_changed)

        self.status = self.fig.text(0.605, 0.06, "", fontsize=8, va="center")
        self._update_plan()

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

    # --------------------------------------------------------- sweep parameters

    def _param_text(self, key: str) -> str:
        value = self.sweep_cfg.get(key, 1)
        return str(int(value)) if key == "repeats" else f"{float(value):g}"

    def _validate_params(self, start, stop, step, repeats) -> str | None:
        """Return a human-readable reason the plan is invalid, or None if OK."""
        low, high = self.freq_limits_mhz
        if step <= 0:
            return "Step must be greater than 0."
        if stop <= start:
            return "Stop must be greater than Start."
        if repeats < 1:
            return "Repeats must be at least 1."
        if start < low or stop > high:
            return f"Frequencies must lie within {low:g}-{high:g} MHz (generator range)."
        if (stop - start) / step > 20000:
            return "Too many points; increase Step or narrow the range."
        return None

    def _on_param_submit(self, key: str, text: str) -> None:
        if self._updating_params:
            return
        if self._sweeping:
            self._revert_param(key)
            self._set_status("Cannot change sweep parameters while a sweep is running.")
            return

        try:
            value = int(float(text)) if key == "repeats" else float(text)
        except ValueError:
            self._revert_param(key)
            self._set_status(f"'{text}' is not a number.")
            return

        candidate = {
            "start_mhz": float(self.sweep_cfg["start_mhz"]),
            "stop_mhz": float(self.sweep_cfg["stop_mhz"]),
            "step_mhz": float(self.sweep_cfg["step_mhz"]),
            "repeats": int(self.sweep_cfg.get("repeats", 1)),
        }
        candidate[key] = value

        problem = self._validate_params(**{k.replace("_mhz", ""): v for k, v in candidate.items()})
        if problem:
            self._revert_param(key)
            self._set_status(problem)
            return

        self.sweep_cfg[key] = value
        # Echo back the canonical form ("2850.0" -> "2850"), so the boxes
        # always show exactly what the sweep will use.
        self._revert_param(key)
        self._update_plan()
        self._set_status("Sweep plan updated.")

    def _revert_param(self, key: str) -> None:
        """Restore a text box to the last accepted value without re-triggering."""
        self._updating_params = True
        try:
            self.param_boxes[key].set_val(self._param_text(key))
        finally:
            self._updating_params = False
        self._frame_shape: tuple[int, int] | None = None

    def _update_plan(self) -> None:
        """Show what the current settings commit to, before the run starts."""
        start = float(self.sweep_cfg["start_mhz"])
        stop = float(self.sweep_cfg["stop_mhz"])
        step = float(self.sweep_cfg["step_mhz"])
        repeats = int(self.sweep_cfg.get("repeats", 1))
        n_points = int(round((stop - start) / step)) + 1

        # Discarded frames still cost an exposure each, so they belong in the
        # time estimate even though they never reach the datacube.
        frames = int(self.sweep_cfg.get("frames_per_point", 1))
        discarded = int(self.sweep_cfg.get("discard_frames", 1))
        settle = float(self.sweep_cfg.get("settle_ms", 0.0))
        seconds = n_points * repeats * (settle + (frames + discarded) * self.exposure_ms) / 1000.0

        # Frame shape is cached rather than re-grabbed: this runs on every
        # keystroke-submit, and pulling a frame from the camera each time
        # would contend with the live view for the acquisition queue.
        if self._frame_shape is None and self.camera is not None:
            try:
                self._frame_shape = self.camera.get_frame().shape[:2]
            except Exception:
                self._frame_shape = None

        megabytes = 0.0
        if self._frame_shape is not None:
            megabytes = estimate_cube_bytes(
                n_points, self._frame_shape[0], self._frame_shape[1], self.binning
            ) / 1e6
            if self.sweep_cfg.get("estimate_errors", True) and repeats > 1:
                megabytes *= 2  # sum-of-squares accumulator runs alongside the sum

        duration = f"{seconds:.0f} s" if seconds < 120 else f"{seconds / 60:.1f} min"
        text = f"→ {n_points} points × {repeats} = {n_points * repeats} frames, ~{duration}"
        if megabytes:
            text += f", ~{megabytes:.0f} MB"
        self.plan_text.set_text(text)
        self.fig.canvas.draw_idle()

    # ----------------------------------------------------------------- exposure

    def _update_exposure_label(self, exposure_ms: float) -> None:
        self.slider_exposure.valtext.set_text(f"{exposure_ms:.2f} ms")

    def _on_exposure_changed(self, log_value: float) -> None:
        """Apply a new exposure to the camera as the slider moves."""
        if self.camera is None:
            return
        if self._sweeping:
            # Changing exposure mid-sweep would make the datacube's frequency
            # points incomparable, so refuse and snap the slider back.
            self.slider_exposure.eventson = False
            self.slider_exposure.set_val(np.log10(self.exposure_ms))
            self.slider_exposure.eventson = True
            self._set_status("Cannot change exposure during a sweep.")
            return

        requested = float(10.0 ** log_value)
        # The camera quantises and clamps the request; show what it actually took.
        applied = self.camera.set_exposure_ms(requested)
        self.exposure_ms = float(applied) if applied else requested
        self.config["camera"]["exposure_ms"] = self.exposure_ms
        self._update_exposure_label(self.exposure_ms)
        self._update_plan()

    def _saturation_report(self, frame: np.ndarray) -> str:
        """Fraction of pixels at (or within 1 % of) full well.

        A saturated pixel carries no ODMR contrast -- its value cannot drop
        when the microwave is applied -- so this is the number to watch when
        setting exposure, not just how bright the picture looks.
        """
        level = getattr(self.camera, "saturation_level", 65535)
        saturated = float(np.count_nonzero(frame >= 0.99 * level)) / frame.size
        return f"{frame.max():.0f} peak, {100.0 * saturated:.2f} % saturated"

    # ---------------------------------------------------------------- live view

    def _start_live_view(self) -> None:
        self._live_timer = self.fig.canvas.new_timer(interval=100)
        self._live_timer.add_callback(self._update_live_view)
        self._live_timer.start()
        self._set_status("Live view — 'MW check' to verify NV response, 'Run sweep' to measure.")

    def _update_live_view(self) -> None:
        if self._sweeping or self.camera is None or self.cube is not None:
            return
        try:
            frame = self.camera.get_frame(timeout_ms=100)
        except TimeoutError:
            return
        self._show_image(
            frame,
            f"Live view — {self.exposure_ms:.2f} ms, {self._saturation_report(frame)}",
        )

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
        repeats = max(1, int(self.sweep_cfg.get("repeats", 1)))
        estimate_errors = bool(self.sweep_cfg.get("estimate_errors", True))
        probe = self.camera.get_frame()
        cube_mb = estimate_cube_bytes(len(freqs), probe.shape[0], probe.shape[1], self.binning) / 1e6
        # The sum-of-squares accumulator doubles the working set while a
        # repeated sweep with error estimation is running.
        working_mb = cube_mb * (2 if (estimate_errors and repeats > 1) else 1)
        print(
            f"Sweeping {len(freqs)} points x {repeats} repeat(s), "
            f"{self.sweep_cfg['start_mhz']}-{self.sweep_cfg['stop_mhz']} MHz. "
            f"Datacube ~{cube_mb:.0f} MB (working set ~{working_mb:.0f} MB, "
            f"binning={self.binning})."
        )

        total_points = len(freqs) * repeats

        def on_progress(repeat: int, index: int, freq_mhz: float, frame: np.ndarray) -> None:
            done = repeat * len(freqs) + index + 1
            self._show_image(frame, f"Sweeping... {freq_mhz:.1f} MHz")
            self._set_status(
                f"Repeat {repeat + 1}/{repeats} - {freq_mhz:.1f} MHz "
                f"({done}/{total_points} frames)"
            )

        try:
            result = acquire_odmr_cube(
                self.camera,
                self.generator,
                freqs,
                power_dbm=self.mw_cfg["power_dbm"],
                settle_ms=self.sweep_cfg["settle_ms"],
                frames_per_point=self.sweep_cfg["frames_per_point"],
                discard_frames=int(self.sweep_cfg.get("discard_frames", 1)),
                binning=self.binning,
                repeats=repeats,
                alternate_direction=bool(self.sweep_cfg.get("alternate_direction", True)),
                estimate_errors=estimate_errors,
                on_progress=on_progress,
                should_abort=lambda: self._abort,
            )
            self._adopt_result(result)

            averaging = (
                f"{result.repeats_completed} repeat(s) averaged"
                if result.fully_averaged
                else f"partial: {int(self.counts.min())}-{int(self.counts.max())} repeats per point"
            )
            self._set_status(
                f"Sweep done: {len(self.frequencies_mhz)} points, {averaging}. "
                "Click the image to read ODMR."
            )
        except Exception as exc:  # surface hardware errors instead of freezing silently
            print(f"ODMR sweep failed: {exc}")
            self._set_status(f"Sweep failed: {exc}")
        finally:
            self._sweeping = False
            self.btn_sweep.label.set_text("Run sweep")
            self.fig.canvas.draw_idle()

    def _adopt_result(self, result) -> None:
        """Install a freshly measured or loaded result as the current data."""
        self.frequencies_mhz = result.frequencies_mhz
        self.cube = result.cube
        self.sem = result.sem
        self.counts = result.counts
        self.repeats_completed = result.repeats_completed
        self._contrast = contrast_map(self.cube, self.min_signal_fraction)
        self._view_mode = "pl"
        self._refresh_image_view()

    # -------------------------------------------------- MW on/off diagnostic check

    def _on_mwcheck_clicked(self, event) -> None:
        if self.camera is None:
            self._set_status("No hardware attached (opened from a saved file).")
            return
        if self._sweeping:
            self._abort = True
            return
        threading.Thread(target=self._run_mw_check, daemon=True).start()

    def _check_frequency(self) -> float:
        """Frequency to park the microwave at for the on/off check.

        If a sweep has already been measured, use the deepest dip it found
        -- that is by definition where the response is strongest. Otherwise
        fall back to the configured guess.
        """
        if self.cube is not None and self.frequencies_mhz is not None:
            whole_frame = Roi(
                x_center=self.cube.shape[2] // 2,
                y_center=self.cube.shape[1] // 2,
                half_size=max(self.cube.shape[1], self.cube.shape[2]),
            )
            spectrum = roi_spectrum(self.cube, whole_frame)
            return float(self.frequencies_mhz[int(np.argmin(spectrum))])
        return float(self.diag_cfg.get("check_freq_mhz", 2870.0))

    def _run_mw_check(self) -> None:
        self._sweeping = True
        self._abort = False
        self.btn_mwcheck.label.set_text("Abort")

        freq = self._check_frequency()
        n_cycles = int(self.diag_cfg.get("cycles", 10))
        print(f"MW on/off check at {freq:.1f} MHz, {n_cycles} interleaved cycles.")

        def on_progress(done: int, total: int) -> None:
            self._set_status(f"MW check at {freq:.1f} MHz — cycle {done}/{total}")

        try:
            mean_off, mean_on = mw_on_off_check(
                self.camera,
                self.generator,
                freq_mhz=freq,
                power_dbm=self.mw_cfg["power_dbm"],
                n_cycles=n_cycles,
                settle_ms=float(self.diag_cfg.get("settle_ms", 30.0)),
                discard_frames=int(self.diag_cfg.get("discard_frames", 1)),
                binning=self.binning,
                on_progress=on_progress,
                should_abort=lambda: self._abort,
            )
            self._diff_map = difference_map(mean_off, mean_on, self.min_signal_fraction)
            self._diff_freq_mhz = freq
            stats = summarize_difference(self._diff_map)

            self._view_mode = "mwcheck"
            self._refresh_image_view()

            if stats["detected"]:
                message = (
                    f"NV response detected at {freq:.1f} MHz — "
                    f"{stats['responding_px']} px responding, peak drop "
                    f"{stats['max_pct']:.2f} %, 99th pct {stats['p99_pct']:.2f} %, "
                    f"noise {stats['noise_pct']:.2f} %."
                )
            else:
                message = (
                    f"NO clear response at {freq:.1f} MHz (noise "
                    f"{stats['noise_pct']:.2f} %). Check: laser on the NV spot? "
                    f"emission filter passing NV PL? MW antenna coupled? "
                    f"is {freq:.1f} MHz actually a resonance?"
                )
            print(message)
            self._set_status(message)
        except Exception as exc:
            print(f"MW check failed: {exc}")
            self._set_status(f"MW check failed: {exc}")
        finally:
            self._sweeping = False
            self.btn_mwcheck.label.set_text("MW check")
            self.fig.canvas.draw_idle()

    # ------------------------------------------------------------ image display

    def _available_views(self) -> list[str]:
        views = []
        if self.cube is not None:
            views += ["pl", "contrast"]
        if self._diff_map is not None:
            views.append("mwcheck")
        return views

    def _refresh_image_view(self) -> None:
        if self._view_mode == "mwcheck" and self._diff_map is not None:
            self._show_image(
                100.0 * self._diff_map,
                f"MW on/off PL drop (%) at {self._diff_freq_mhz:.1f} MHz",
            )
        elif self._view_mode == "contrast" and self.cube is not None:
            self._show_image(100.0 * self._contrast, "ODMR contrast map (%) — click to read ODMR")
        elif self.cube is not None:
            self._view_mode = "pl"
            self._show_image(self.cube.mean(axis=0), "Mean PL over sweep — click to read ODMR")
        else:
            return
        self.btn_view.label.set_text(f"View: {self._view_mode}")
        if self.roi_patch is not None:
            self.ax_img.add_patch(self.roi_patch)

    def _on_view_clicked(self, event) -> None:
        views = self._available_views()
        if not views:
            self._set_status("Run a sweep or an MW check first.")
            return
        current = views.index(self._view_mode) if self._view_mode in views else -1
        self._view_mode = views[(current + 1) % len(views)]
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
        self._last_roi = roi
        self._draw_roi(roi)
        self._plot_spectrum(roi)

    def _on_roi_size_changed(self, value) -> None:
        """Resize the ROI and re-read the spectrum from the existing datacube."""
        self.roi_half_size = int(value)
        self.config["roi"]["half_size_px"] = self.roi_half_size
        if self.cube is None or self._last_roi is None:
            return
        roi = Roi(self._last_roi.x_center, self._last_roi.y_center, self.roi_half_size)
        self._last_roi = roi
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

    def _spectrum_baseline(self, raw: np.ndarray) -> np.ndarray:
        """Off-resonance baseline the spectrum is normalised against.

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
        """
        n = len(raw)
        fallback = np.full(n, float(np.max(raw)))
        if not self.baseline_correction or n < 5:
            return fallback

        x = np.asarray(self.frequencies_mhz, dtype=float)
        mask = np.ones(n, dtype=bool)
        baseline = fallback
        for _ in range(self.baseline_iterations):
            if mask.sum() < max(4, int(0.2 * n)):
                break  # too few points left to trust the fit
            try:
                coefficients = np.polyfit(x[mask], raw[mask], 1)
            except (np.linalg.LinAlgError, ValueError):
                return fallback
            baseline = np.polyval(coefficients, x)

            residual = raw - baseline
            spread = 1.4826 * float(np.median(np.abs(residual - np.median(residual))))
            if spread <= 0:
                break
            keep = residual > -self.baseline_reject_sigma * spread
            if np.array_equal(keep, mask):
                break
            mask = keep

        if not np.all(np.isfinite(baseline)) or np.any(baseline <= 0):
            return fallback
        return baseline

    def _plot_spectrum(self, roi: Roi) -> None:
        raw = roi_spectrum(self.cube, roi)
        if float(np.max(raw)) <= 0:
            self._set_status("ROI has no signal.")
            return

        baseline = self._spectrum_baseline(raw)
        normalised = 100.0 * raw / baseline

        self.spectrum_line.set_data(self.frequencies_mhz, normalised)

        # Error band from the scatter between repeats, divided by the same
        # per-point baseline as the spectrum so both share one axis.
        if self._error_band is not None:
            self._error_band.remove()
            self._error_band = None
        error = roi_spectrum_error(self.sem, roi)
        has_error = error is not None and np.any(np.isfinite(error))
        if has_error:
            error_pct = 100.0 * error / baseline
            self._error_band = self.ax_spec.fill_between(
                self.frequencies_mhz,
                normalised - error_pct,
                normalised + error_pct,
                color="tab:blue",
                alpha=0.25,
                linewidth=0,
            )

        self.ax_spec.relim()
        self.ax_spec.autoscale_view()
        n_pixels = (2 * roi.half_size + 1) ** 2
        self.ax_spec.set_title(
            f"ODMR ROI centred at ({roi.x_center}, {roi.y_center}) — "
            f"{n_pixels} px averaged, {self.repeats_completed} repeat(s)"
        )

        dip_index = int(np.argmin(normalised))
        dip_freq = float(self.frequencies_mhz[dip_index])
        contrast_pct = 100.0 - float(normalised[dip_index])
        message = (
            f"ROI ({roi.x_center}, {roi.y_center}) ±{roi.half_size} px: "
            f"deepest dip {dip_freq:.1f} MHz, contrast {contrast_pct:.2f} %"
        )
        if has_error:
            typical_error = float(np.nanmedian(100.0 * error / baseline))
            message += f", error ±{typical_error:.3f} %"
            # A dip smaller than a few times its own error bar is not a
            # measurement, it is a fluctuation -- say so rather than letting
            # the number stand on its own.
            if typical_error > 0 and contrast_pct < 3.0 * typical_error:
                message += " (below 3x error — not significant)"
        self._set_status(message)
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
            OdmrResult(
                frequencies_mhz=self.frequencies_mhz,
                cube=self.cube,
                counts=self.counts,
                sem=self.sem,
                repeats_completed=self.repeats_completed,
            ),
            metadata={
                "binning": self.binning,
                "power_dbm": self.mw_cfg["power_dbm"],
                "exposure_ms": self.exposure_ms,
                "frames_per_point": self.sweep_cfg["frames_per_point"],
            },
        )
        print(f"Saved datacube to {path}")
        self._set_status(f"Saved to {path}")

    def _on_export_clicked(self, event) -> None:
        """Write a small, publishable bundle (not the raw cube) for git."""
        if self.cube is None:
            self._set_status("Nothing to export — run a sweep first.")
            return

        rois = [self._last_roi] if self._last_roi is not None else [
            Roi(self.cube.shape[2] // 2, self.cube.shape[1] // 2, self.roi_half_size)
        ]
        baselines = [self._spectrum_baseline(roi_spectrum(self.cube, roi)) for roi in rois]

        results_root = self.config.get("output", {}).get(
            "results_directory", os.path.join(os.path.dirname(__file__), "results")
        )
        try:
            out_dir = export_measurement(
                results_root,
                OdmrResult(
                    frequencies_mhz=self.frequencies_mhz,
                    cube=self.cube,
                    counts=self.counts,
                    sem=self.sem,
                    repeats_completed=self.repeats_completed,
                ),
                config=self.config,
                rois=rois,
                baselines=baselines,
                min_signal_fraction=self.min_signal_fraction,
            )
        except Exception as exc:
            print(f"Export failed: {exc}")
            self._set_status(f"Export failed: {exc}")
            return

        relative = os.path.relpath(out_dir, os.path.dirname(__file__))
        print(f"Exported results to {out_dir}")
        print("Publish with:  python publish_results.py")
        self._set_status(f"Exported to {relative} — run publish_results.py to push.")

    def _load_measurement(self, path: str) -> None:
        result, metadata = load_cube(path)
        self.binning = int(metadata.get("binning", self.binning))
        self._adopt_result(result)
        self._set_status(f"Loaded {path} — click the image to read ODMR.")
        print(
            f"Loaded {path}: {self.cube.shape[0]} frequency points, "
            f"{result.repeats_completed} repeat(s), metadata={metadata}"
        )

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
