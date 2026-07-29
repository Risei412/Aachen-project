# Click-to-measure ODMR viewer

Live-view a Thorlabs CMOS camera in VS Code, click on a spot in the
image (e.g. an NV-diamond ensemble under green excitation), and the
app sweeps a Windfreak SynthHD microwave signal generator across a
frequency range while recording the mean camera brightness inside a
small ROI around the click — i.e. a widefield ODMR spectrum for that
spot.

```
Equipments/CMOS camera/thorlabs_camera.py        camera driver (+ mock)
Equipments/Microwave generator/synthhd.py        SynthHD driver (+ mock)
ODMR/odmr_sweep.py                               frequency-sweep / ROI logic
ODMR/odmr_app.py                                 GUI: live view + click + spectrum plot
ODMR/config.yaml                                 sweep range, power, exposure, ROI size
```

## 1. Try it without hardware first

Everything above ships with `mock: true` in `config.yaml`, which
substitutes `MockThorlabsCamera` / `MockSynthHD` (simulated NV ODMR
dips). This lets you develop/debug the GUI, ROI selection, and sweep
logic in VS Code without anything plugged in.

```bash
cd ODMR
python -m venv .venv && source .venv/bin/activate   # optional but recommended
pip install -r requirements.txt
python odmr_app.py            # or: F5 in VS Code -> "ODMR: click-to-measure (mock hardware)"
```

A window opens with the live camera view on the left and an empty
spectrum plot on the right. Click on the bright disk (simulated
sample) — the app draws a red ROI box and sweeps the frequency,
plotting the two synthetic dips as they come in.

## 2. Wiring up real hardware

### Thorlabs CMOS camera

1. Install **ThorCam** from thorlabs.com — this installs the camera's
   USB driver and the TSI SDK.
2. Install the matching Python wheel from the ThorCam install
   directory, e.g.:
   ```
   pip install "C:/Program Files/Thorlabs/Scientific Imaging/Scientific Camera Support/Scientific Camera Interfaces/SDK/Python Toolkit/thorlabs_tsi_sdk-*.whl"
   ```
3. Make sure the native DLLs from
   `.../SDK/Native Toolkit/dlls/Native_64_lib` are on `PATH` (or copied
   next to `thorlabs_camera.py`).
4. **Close ThorCam itself** before running this app — the SDK cannot
   share the camera with the ThorCam GUI at the same time.
5. See `Equipments/CMOS camera/camera-quick-start-guide-eng.pdf` for
   your specific camera model's connection details.

### Windfreak SynthHD

1. Connect the SynthHD to the PC over USB; it enumerates as a virtual
   COM port (Windows: `COMx`; Linux: `/dev/ttyACM0` or similar).
2. Note the port name (Device Manager on Windows, `ls /dev/tty*` /
   `dmesg` on Linux) and set it in `config.yaml` under
   `microwave.port`.
3. Connect the SynthHD's RF output to the microwave antenna/loop that
   drives your NV sample.
4. **Verify the serial command set** against your unit's manual before
   trusting the frequencies (`Equipments/Microwave generator/synthhd.py`
   documents the command characters used and where they came from —
   firmware revisions occasionally differ). A quick sanity check: open
   a serial terminal at 9600 baud and send `C0`, `f2870.000`, `W0.00`,
   `E1` — the output should jump to 2.870 GHz at 0 dBm.

Once both are wired up, edit `ODMR/config.yaml`:

```yaml
mock: false
microwave:
  port: "COM7"          # or /dev/ttyACM0
sweep:
  start_mhz: 2800.0      # tune to bracket your expected NV resonances
  stop_mhz: 2940.0
  step_mhz: 1.0
```

and run:

```bash
python odmr_app.py --no-mock
```

(or use the "ODMR: click-to-measure (real hardware)" launch
configuration in VS Code).

## Notes / things to tune per setup

- **ROI size** (`roi.half_size_px`): bigger averages over more NV
  centers (less shot noise) but can wash out spatial contrast if your
  sample is heterogeneous.
- **Sweep resolution** (`sweep.step_mhz`, `sweep.start_mhz` /
  `stop_mhz`): NV ODMR linewidths are typically a few MHz at zero
  field; use a comparable or finer step near the dips.
- **`sweep.settle_ms`**: time given to the SynthHD's PLL to lock after
  each frequency step before a frame is captured. Increase if spectra
  look noisy near frequency jumps.
- **`sweep.frames_per_point`**: averages multiple camera frames per
  frequency point to reduce shot noise at the cost of sweep speed.
- A sweep runs in a background thread so the live camera view timer is
  paused (not stopped) during acquisition; clicking again while a
  sweep is running is ignored until it finishes.
