"""Serial driver for the Windfreak Technologies SynthHD microwave signal generator.

Product page:
https://windfreaktech.com/product/microwave-signal-generator-synthhd/

The SynthHD enumerates as a USB virtual COM port and is controlled with
short ASCII commands (single letter + parameter, no line terminator
required). This wraps the subset of commands needed for an ODMR
frequency sweep: channel select, frequency, power and RF on/off.

NOTE: exact command characters/ranges can differ slightly between
SynthHD / SynthHD PRO firmware revisions. Verify against the "API"
section of the manual that shipped with your unit (or open a terminal
program at 9600-115200 baud, 'C0' + 'f2870' + 'W0' + 'E1' style commands,
and check the echoed reply) before relying on this for real measurements.
The command characters below match the commonly published SynthHD API:

    C<0|1>   select channel A (0) or B (1)
    f<MHz>   set frequency in MHz, e.g. "f2870.0"
    W<dBm>   set RF power in dBm, e.g. "W0.0"
    E<0|1>   RF output enable (power amplifier) for selected channel
    e<0|1>   PLL/channel enable
"""
from __future__ import annotations

import time


class SynthHD:
    """Minimal serial control of a Windfreak SynthHD."""

    def __init__(self, port: str, channel: int = 0, baudrate: int = 9600, timeout: float = 1.0):
        import serial  # imported lazily so mock usage doesn't need pyserial

        self._ser = serial.Serial(port=port, baudrate=baudrate, timeout=timeout)
        time.sleep(0.1)  # let the USB-CDC port settle
        self.select_channel(channel)

    def _send(self, command: str) -> None:
        self._ser.write(command.encode("ascii"))
        self._ser.flush()

    def select_channel(self, channel: int) -> None:
        if channel not in (0, 1):
            raise ValueError("channel must be 0 (A) or 1 (B)")
        self._send(f"C{channel}")

    def set_frequency_mhz(self, freq_mhz: float) -> None:
        self._send(f"f{freq_mhz:.3f}")

    def set_power_dbm(self, power_dbm: float) -> None:
        self._send(f"W{power_dbm:.2f}")

    def enable_rf(self, on: bool) -> None:
        self._send(f"E{1 if on else 0}")
        self._send(f"e{1 if on else 0}")

    def close(self) -> None:
        try:
            self.enable_rf(False)
        finally:
            self._ser.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class MockSynthHD:
    """In-memory stand-in for :class:`SynthHD`, for development without hardware.

    Accepts an optional ``on_state_change`` callback so a mock camera can be
    told the current (frequency, power, rf_on) state and react to it
    (see ``MockThorlabsCamera.set_mw_state``), which lets the whole ODMR
    pipeline be exercised end-to-end without any instruments attached.
    """

    def __init__(self, channel: int = 0, on_state_change=None):
        self.channel = channel
        self.freq_mhz = None
        self.power_dbm = None
        self.rf_on = False
        self._on_state_change = on_state_change

    def _notify(self) -> None:
        if self._on_state_change is not None:
            self._on_state_change(self.freq_mhz, self.power_dbm, self.rf_on)

    def select_channel(self, channel: int) -> None:
        self.channel = channel

    def set_frequency_mhz(self, freq_mhz: float) -> None:
        self.freq_mhz = freq_mhz
        self._notify()

    def set_power_dbm(self, power_dbm: float) -> None:
        self.power_dbm = power_dbm
        self._notify()

    def enable_rf(self, on: bool) -> None:
        self.rf_on = on
        self._notify()

    def close(self) -> None:
        self.enable_rf(False)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
