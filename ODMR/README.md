# Widefield ODMR viewer

Measure an ODMR spectrum at **every pixel** of a Thorlabs CMOS camera
image in a single microwave frequency sweep, then click anywhere on the
resulting image to read out that spot's ODMR waveform instantly.

Intended for NV-diamond sensing inside a diamond anvil cell: sweep once,
then explore the field of view interactively to find where the NV signal
is and how the resonances shift across the culet.

```
Equipments/CMOS camera/thorlabs_camera.py        camera driver (+ mock)
Equipments/Microwave generator/synthhd.py        SynthHD driver (+ mock)
ODMR/odmr_sweep.py                               datacube acquisition + analysis
ODMR/odmr_app.py                                 GUI
ODMR/config.yaml                                 sweep range, power, exposure, binning, ROI
```

## How the measurement works

The microwave is swept across the configured frequency range **once**,
and a full camera frame is stored at every frequency point. That gives a
datacube

```
cube[i, y, x] = photoluminescence at pixel (x, y) with MW at frequencies[i]
```

so every pixel carries its own ODMR spectrum. Clicking a spot afterwards
is just a slice through the cube — no extra hardware access, no re-sweep,
and you can probe as many points as you like from one measurement.

## Using the app

1. The app opens on a **live view** — use it to focus and position the sample.
2. Press **"Run sweep"**. Progress is shown as the frequency advances; press
   the same button (now "Abort") to stop early and keep the points measured
   so far.
3. When the sweep finishes the image is displayed. **Click anywhere on it**
   to plot that spot's ODMR spectrum in the right-hand panel. The status
   line reports the deepest dip frequency and the contrast.
4. **"View: PL" / "View: contrast"** toggles between the mean
   photoluminescence image and the per-pixel ODMR contrast map. The
   contrast map shows where the microwave actually modulates the PL, i.e.
   where the NV centers are — useful for finding the NV layer before
   picking readout spots.
5. **"Save"** writes the datacube to `data/odmr_<timestamp>.npz`. Re-open it
   later with `python odmr_app.py --load data/odmr_....npz` to keep clicking
   around the data with no hardware attached.

## 1. Try it without hardware first

`config.yaml` ships with `mock: true`, which substitutes simulated
camera/generator classes. The mock camera renders a DAC-like scene (bright
anvil ring, dark culet interior, speckled NV grains in the middle) with two
NV resonances near 2820 and 2920 MHz, plus a magnetic-field-like gradient
so different spots give visibly different spectra. This exercises the full
pipeline — sweep, datacube, click-to-readout, contrast map, save/load —
with nothing plugged in.

```bash
cd ODMR
python -m venv .venv
.venv\Scripts\activate          # Windows  (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt
python odmr_app.py
```

> If `pip` is not recognised on Windows, use `py -m pip install -r requirements.txt`
> and `py odmr_app.py`. If `py` is missing too, reinstall Python from python.org
> with **"Add python.exe to PATH"** ticked.

> Run the app from a clone of this repository with its folder layout intact —
> `odmr_app.py` locates the drivers at `../Equipments/...`, so copying the
> script somewhere on its own will fail with `ModuleNotFoundError`.

## 2. Wiring up real hardware

### Thorlabs CMOS camera

1. Install **ThorCam** from thorlabs.com — this installs the camera's USB
   driver and the TSI SDK.
2. Install the matching Python wheel from the ThorCam install directory:
   ```
   pip install "C:/Program Files/Thorlabs/Scientific Imaging/Scientific Camera Support/Scientific Camera Interfaces/SDK/Python Toolkit/thorlabs_tsi_sdk-*.whl"
   ```
3. Make sure the native DLLs from `.../SDK/Native Toolkit/dlls/Native_64_lib`
   are on `PATH` (or copied next to `thorlabs_camera.py`).
4. **Close ThorCam itself** before running this app — the SDK cannot share
   the camera with the ThorCam GUI.
5. See `Equipments/CMOS camera/camera-quick-start-guide-eng.pdf` for your
   camera model's connection details.

### Windfreak SynthHD

1. Connect over USB; it enumerates as a virtual COM port (Windows `COMx`,
   Linux `/dev/ttyACM0`). Set it in `config.yaml` under `microwave.port`.
2. Connect the RF output to the microwave antenna/loop driving the sample.
3. **Verify the serial command set** against your unit's manual before
   trusting the frequencies — firmware revisions differ slightly, and
   `synthhd.py` documents which command characters it uses. Quick check:
   open a serial terminal at 9600 baud and send `C0`, `f2870.000`, `W0.00`,
   `E1`; the output should sit at 2.870 GHz / 0 dBm.

Then set `mock: false` in `config.yaml` and run `python odmr_app.py --no-mock`.

## Memory and sweep-time budget

The datacube is `n_freq × height × width × 4` bytes. A 141-point sweep on a
1440×1080 camera is **877 MB** at full resolution — set `camera.binning: 2`
(or 4) to cut that by 4× (or 16×); the app prints the estimate before each
sweep. Binning also raises per-pixel SNR, which matters because a single
pixel's ODMR contrast is often only a few percent.

Sweep time is roughly
`n_freq × (settle_ms + frames_per_point × exposure_ms)`.

## Parameters worth tuning

| Setting | Effect |
| --- | --- |
| `sweep.start_mhz` / `stop_mhz` | Bracket the expected resonances. Keep some off-resonance baseline at both ends — the normalisation divides by the brightest point. |
| `sweep.step_mhz` | NV linewidths are a few MHz; use a comparable or finer step. |
| `sweep.settle_ms` | PLL lock time after each frequency step. Increase if spectra look noisy. |
| `sweep.frames_per_point` | Frames averaged per frequency; trades sweep time for SNR. |
| `camera.binning` | Cuts datacube size by N² and raises per-pixel SNR. |
| `roi.half_size_px` | Larger ROI averages more pixels (smoother spectrum) but blurs spatial detail. |
| `analysis.min_signal_fraction` | Contrast-map threshold. Dark pixels have near-zero mean PL, so their shot noise produces huge spurious "contrast"; pixels below this fraction of the brightest pixel are blanked. Lower it if a genuinely dim part of the sample is being masked. |
