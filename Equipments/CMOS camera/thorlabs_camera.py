"""Thin wrapper around Thorlabs Scientific Camera SDK (thorlabs_tsi_sdk).

Wraps a Thorlabs CMOS camera (e.g. Kiralux / Zelux) so the rest of the
ODMR application only has to deal with a small, simulator-friendly
interface: ``open()``, ``get_frame()``, ``set_exposure_ms()``, ``close()``.

Hardware setup (one-time, per PC):
    1. Install "ThorCam" from thorlabs.com (installs the TSI camera drivers).
    2. Install the Python SDK that ships inside the ThorCam installation
       directory, e.g.:
           pip install "C:/Program Files/Thorlabs/Scientific Imaging/
                        Scientific Camera Support/Scientific Camera
                        Interfaces/SDK/Python Toolkit/thorlabs_tsi_sdk-*.whl"
       (see camera-quick-start-guide-eng.pdf in this folder for the exact
       path shipped with your camera model).
    3. Copy the native DLLs from
       "...Scientific Camera Interfaces/SDK/Native Toolkit/dlls/Native_64_lib"
       next to this file, or add that folder to PATH, so
       ``TLCameraSDK`` can load them.

If ``thorlabs_tsi_sdk`` is not importable (e.g. developing away from the
lab PC), :class:`MockThorlabsCamera` below can be used as a drop-in
replacement -- see ``ODMR/odmr_app.py`` for how it is selected.
"""
from __future__ import annotations

import numpy as np


class ThorlabsCamera:
    """Live-view wrapper for a single Thorlabs TSI camera."""

    def __init__(self, exposure_ms: float = 20.0, roi=None):
        from thorlabs_tsi_sdk.tl_camera import TLCameraSDK  # imported lazily

        self._sdk = TLCameraSDK()
        camera_list = self._sdk.discover_available_cameras()
        if not camera_list:
            raise RuntimeError(
                "No Thorlabs camera found. Check the USB connection and "
                "that ThorCam is closed (the SDK cannot share the camera "
                "with the ThorCam GUI)."
            )
        self._camera = self._sdk.open_camera(camera_list[0])
        self._camera.exposure_time_us = int(exposure_ms * 1000)
        self._camera.frames_per_trigger_zero_for_unlimited = 0
        if roi is not None:
            self._camera.roi = roi
        self._camera.arm(2)
        self._camera.issue_software_trigger()

    def set_exposure_ms(self, exposure_ms: float) -> None:
        self._camera.exposure_time_us = int(exposure_ms * 1000)

    def get_frame(self, timeout_ms: int = 1000) -> np.ndarray:
        """Return the latest frame as a 2-D numpy array (mono, uint16)."""
        frame = self._camera.get_pending_frame_or_null()
        if frame is None:
            # Poll briefly instead of blocking forever so the GUI stays responsive.
            import time

            waited = 0
            poll_ms = 10
            while frame is None and waited < timeout_ms:
                time.sleep(poll_ms / 1000)
                waited += poll_ms
                frame = self._camera.get_pending_frame_or_null()
            if frame is None:
                raise TimeoutError("Timed out waiting for a camera frame")
        return np.copy(frame.image_buffer).reshape(
            self._camera.image_height_pixels, self._camera.image_width_pixels
        )

    def close(self) -> None:
        try:
            self._camera.disarm()
            self._camera.dispose()
        finally:
            self._sdk.dispose()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class MockThorlabsCamera:
    """Simulated camera for development without hardware attached.

    Produces a synthetic NV-diamond-like widefield image: a bright disk
    (the sample under green excitation) sitting on a dark background,
    with the mean brightness inside a configurable ROI dipping when the
    (simulated) microwave frequency is on ODMR resonance. This lets the
    whole GUI / sweep pipeline be exercised without any hardware.
    """

    def __init__(self, exposure_ms: float = 20.0, width: int = 640, height: int = 480):
        self.exposure_ms = exposure_ms
        self.width = width
        self.height = height
        self._mw_freq_mhz = None
        self._rng = np.random.default_rng(0)
        yy, xx = np.mgrid[0:height, 0:width]
        cx, cy, r = width / 2, height / 2, min(width, height) / 3
        self._sample_mask = (xx - cx) ** 2 + (yy - cy) ** 2 < r ** 2

    def set_exposure_ms(self, exposure_ms: float) -> None:
        self.exposure_ms = exposure_ms

    def set_mw_state(self, freq_mhz: float | None, power_dbm: float, rf_on: bool) -> None:
        """Used by the mock MW driver so the mock camera can react to it."""
        self._mw_freq_mhz = freq_mhz if rf_on else None

    def get_frame(self, timeout_ms: int = 1000) -> np.ndarray:
        base = np.full((self.height, self.width), 300.0)
        base[self._sample_mask] = 4000.0

        if self._mw_freq_mhz is not None:
            # Two synthetic NV ODMR dips (zero-field split by a bias field),
            # e.g. around 2820 MHz and 2920 MHz, ~10 MHz wide, ~20% contrast.
            for center in (2820.0, 2920.0):
                dip = 0.2 * np.exp(-0.5 * ((self._mw_freq_mhz - center) / 5.0) ** 2)
                base[self._sample_mask] *= (1.0 - dip)

        noisy = base + self._rng.normal(0, 15, size=base.shape)
        return np.clip(noisy, 0, 65535).astype(np.uint16)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
