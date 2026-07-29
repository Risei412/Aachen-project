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
    3. Point ``dll_dir`` (constructor arg / ``camera.dll_dir`` in
       config.yaml) at the folder containing the native DLLs, e.g.
       "...Scientific Camera Interfaces/SDK/Native Toolkit/dlls/Native_64_lib".
       Installing the pip package alone is *not* enough: importing
       ``thorlabs_tsi_sdk`` only pulls in the Python wrapper, and it will
       fail with "Could not find module 'thorlabs_tsi_camera_sdk.dll'"
       unless that native DLL folder is explicitly added, because Python
       3.8+ no longer searches plain ``PATH`` entries for DLL dependencies
       (see ``_add_dll_directory`` below). Match the DLL bitness (32/64-bit)
       to your Python interpreter's.

If ``thorlabs_tsi_sdk`` is not importable (e.g. developing away from the
lab PC), :class:`MockThorlabsCamera` below can be used as a drop-in
replacement -- see ``ODMR/odmr_app.py`` for how it is selected.
"""
from __future__ import annotations

import os

import numpy as np


def _add_dll_directory(dll_dir: str) -> None:
    """Make the Thorlabs native DLLs discoverable by the loader.

    Since Python 3.8, DLL dependency search no longer honours ``PATH`` by
    default (a Windows security hardening change) -- ``os.add_dll_directory``
    must be called explicitly, which is why the SDK's own DLL folder has to
    be passed in here rather than just relying on the user's system PATH.
    """
    if not os.path.isdir(dll_dir):
        raise FileNotFoundError(
            f"Thorlabs DLL directory not found: {dll_dir!r}. Set camera.dll_dir "
            "in config.yaml to the 'Native Toolkit/dlls/Native_64_lib' (or "
            "Native_32_lib, matching your Python interpreter's bitness) folder "
            "from the ThorCam SDK installation."
        )
    if hasattr(os, "add_dll_directory"):  # Windows, Python >= 3.8
        os.add_dll_directory(dll_dir)
    os.environ["PATH"] = dll_dir + os.pathsep + os.environ.get("PATH", "")


class ThorlabsCamera:
    """Live-view wrapper for a single Thorlabs TSI camera."""

    def __init__(self, exposure_ms: float = 20.0, roi=None, dll_dir: str | None = None):
        if dll_dir:
            _add_dll_directory(dll_dir)

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
        self._camera.frames_per_trigger_zero_for_unlimited = 0
        if roi is not None:
            self._camera.roi = roi

        self._exposure_ms = exposure_ms
        self._apply_exposure(exposure_ms)

        # A deeper queue than the 2 frames used previously: during a long
        # sweep the acquisition thread can be held up (plot redraws, disk
        # I/O), and a shallow queue drops frames the moment that happens.
        self._camera.arm(10)
        self._camera.issue_software_trigger()

    def _apply_exposure(self, exposure_ms: float) -> float:
        """Set the exposure, clamped to what this camera actually supports."""
        requested_us = int(exposure_ms * 1000)
        low = getattr(self._camera, "exposure_time_range_us", None)
        if low is not None:
            requested_us = int(np.clip(requested_us, low.min, low.max))
        self._camera.exposure_time_us = requested_us
        # Read back: the camera quantises the value, so the effective
        # exposure (which sets the frame timeout) may differ from the request.
        self._exposure_ms = self._camera.exposure_time_us / 1000
        return self._exposure_ms

    @property
    def exposure_ms(self) -> float:
        return self._exposure_ms

    @property
    def saturation_level(self) -> int:
        """Pixel value corresponding to a fully saturated (clipped) pixel."""
        bit_depth = getattr(self._camera, "bit_depth", 16)
        return (1 << bit_depth) - 1

    def set_exposure_ms(self, exposure_ms: float) -> float:
        return self._apply_exposure(exposure_ms)

    def get_frame(self, timeout_ms: int | None = None) -> np.ndarray:
        """Return the latest frame as a 2-D numpy array (mono, uint16).

        The default timeout scales with the exposure time: a 500 ms
        exposure cannot possibly deliver a frame within a fixed 1 s budget
        once a couple of frames are already in flight, so a constant
        timeout would spuriously fail exactly when long exposures are
        needed for a dim sample.
        """
        if timeout_ms is None:
            timeout_ms = max(1000, int(5 * self._exposure_ms + 500))

        frame = self._camera.get_pending_frame_or_null()
        if frame is None:
            # Poll briefly instead of blocking forever so the GUI stays responsive.
            import time

            waited = 0
            poll_ms = 5
            while frame is None and waited < timeout_ms:
                time.sleep(poll_ms / 1000)
                waited += poll_ms
                frame = self._camera.get_pending_frame_or_null()
            if frame is None:
                raise TimeoutError(
                    f"Timed out after {timeout_ms} ms waiting for a camera frame "
                    f"(exposure {self._exposure_ms:.1f} ms). The camera stopped "
                    "delivering frames: check the USB connection, and that no "
                    "other program (ThorCam) has taken the camera."
                )
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

    Produces a synthetic image roughly matching what the camera sees
    looking down a diamond anvil cell: a bright ring (light scattered off
    the anvil/gasket edge), a darker culet interior, and a speckled blob
    of NV-diamond microparticles near the centre.

    Only the NV speckle responds to the microwave, so the photoluminescence
    there dips when the (simulated) frequency crosses a resonance. The two
    resonances are given a small spatial gradient across the field of view,
    imitating a magnetic field gradient -- so clicking different spots
    yields visibly different ODMR spectra, which is exactly what the
    widefield datacube workflow is meant to reveal.
    """

    # Zero-field-ish splitting of the two simulated NV resonances, in MHz.
    _RESONANCE_LOW = 2820.0
    _RESONANCE_HIGH = 2920.0
    _LINEWIDTH_MHZ = 5.0
    _MAX_CONTRAST = 0.18

    def __init__(self, exposure_ms: float = 20.0, width: int = 1000, height: int = 651):
        self.exposure_ms = exposure_ms
        self.width = width
        self.height = height
        self._mw_freq_mhz = None
        self._rng = np.random.default_rng(0)

        yy, xx = np.mgrid[0:height, 0:width]
        cx, cy = width / 2, height / 2
        radius = np.hypot(xx - cx, yy - cy)
        culet_r = min(width, height) * 0.45

        # Bright annulus at the anvil edge, dark culet interior, dark surround.
        background = 250.0 + 3400.0 * np.exp(-0.5 * ((radius - culet_r) / (culet_r * 0.12)) ** 2)
        background += 600.0 * (radius < culet_r)

        # Speckled NV-diamond powder near the centre of the culet.
        nv_envelope = np.exp(-0.5 * (radius / (culet_r * 0.35)) ** 2)
        speckle = self._rng.random((height, width)) ** 6  # sparse bright grains
        self._nv_pl = 5000.0 * nv_envelope * speckle
        self._background = background

        # Magnetic-field-like gradient: shifts both resonances by up to
        # +/-8 MHz from one side of the field of view to the other.
        self._detuning_mhz = 8.0 * (xx - cx) / (width / 2)

    # Exposure at which the scene brightnesses above are calibrated.
    _REFERENCE_EXPOSURE_MS = 20.0

    @property
    def saturation_level(self) -> int:
        return 65535

    def set_exposure_ms(self, exposure_ms: float) -> float:
        self.exposure_ms = exposure_ms
        return self.exposure_ms

    def set_mw_state(self, freq_mhz: float | None, power_dbm: float, rf_on: bool) -> None:
        """Used by the mock MW driver so the mock camera can react to it."""
        self._mw_freq_mhz = freq_mhz if rf_on else None

    def get_frame(self, timeout_ms: int | None = None) -> np.ndarray:
        nv_pl = self._nv_pl

        if self._mw_freq_mhz is not None:
            dip = np.zeros_like(nv_pl)
            for center in (self._RESONANCE_LOW, self._RESONANCE_HIGH):
                detuned = self._mw_freq_mhz - (center + self._detuning_mhz)
                dip += self._MAX_CONTRAST * np.exp(-0.5 * (detuned / self._LINEWIDTH_MHZ) ** 2)
            nv_pl = nv_pl * (1.0 - np.clip(dip, 0.0, 1.0))

        # Collected signal scales with exposure, so the simulated frame
        # brightens (and eventually clips at saturation) as the exposure
        # slider is moved -- otherwise the slider would appear to do
        # nothing in mock mode.
        gain = self.exposure_ms / self._REFERENCE_EXPOSURE_MS
        frame = (self._background + nv_pl) * gain
        # Photon shot noise grows as the square root of the collected signal,
        # so longer exposures improve SNR rather than just scaling everything.
        frame = frame + self._rng.normal(0, 12 * np.sqrt(gain), size=frame.shape)
        return np.clip(frame, 0, self.saturation_level).astype(np.uint16)

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
