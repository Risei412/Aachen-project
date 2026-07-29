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
   Drag the **Exposure** slider (logarithmic, 0.05–1000 ms by default) to set
   the exposure live. The title reports the peak pixel value and the
   **percentage of saturated pixels** — keep that at 0 %: a clipped pixel
   cannot get darker when the microwave is applied, so it carries no ODMR
   contrast at all, no matter how good the rest of the setup is. Exposure is
   locked during a sweep, since changing it mid-sweep would make the
   datacube's frequency points incomparable.
2. Set the sweep in the **Start / Stop / Step / Repeats** boxes (press Enter
   to apply each). The line beside them shows what you are committing to
   before anything runs — number of points, total frames, estimated duration
   and datacube size — so a range that would take an hour or exhaust memory
   is visible up front rather than discovered halfway through. Entries are
   validated against the generator's tuning range
   (`microwave.freq_limits_mhz`) and rejected with a reason if the stop is
   below the start, the step is non-positive, or the plan is absurdly large;
   a rejected box snaps back to its last accepted value. `config.yaml` still
   supplies the starting values.
3. Press **"MW check"** (see below) to confirm the NV centers are actually
   responding, before spending minutes on a full sweep.
4. Press **"Run sweep"**. Progress is shown as the frequency advances; press
   the same button (now "Abort") to stop early and keep the points measured
   so far. Sweep parameters are locked while it runs.
5. When the sweep finishes the image is displayed. **Click anywhere on it**
   to plot that spot's ODMR spectrum in the right-hand panel. The status
   line reports the deepest dip frequency and the contrast. Drag the
   **ROI ±px** slider to resize the averaging window; it re-reads the
   datacube already in memory, so exploring ROI sizes after a sweep is
   instant and costs no measurement time.
6. **"View"** cycles the left panel through the mean photoluminescence
   image, the per-pixel ODMR contrast map, and the MW-check difference map
   (whichever are available). The contrast map shows where the microwave
   actually modulates the PL, i.e. where the NV centers are — useful for
   finding the NV layer before picking readout spots.
7. **"Save raw"** writes the full datacube to `data/odmr_<timestamp>.npz`.
   Re-open it later with `python odmr_app.py --load data/odmr_....npz` to keep
   clicking around the data with no hardware attached. These files are large
   and stay local (git-ignored).
8. **"Export"** writes a small, shareable bundle to
   `results/<timestamp>/` — see *Publishing results* below.

## Running it in VS Code

Open the repository folder in VS Code (**File > Open Folder**, choose the
`Aachen-project` folder — not `ODMR/` on its own, since the app loads the
instrument drivers from `../Equipments/`).

1. Accept the recommended extensions when prompted (Python, Pylance).
2. Select the interpreter: **Ctrl+Shift+P** → *Python: Select Interpreter*.
   Point it at the virtual environment you installed the requirements into.
3. Install dependencies: **Ctrl+Shift+P** → *Tasks: Run Task* →
   **ODMR: install Python dependencies**.
4. Press **F5** and pick a configuration:

| Configuration | What it does |
| --- | --- |
| ODMR: real hardware (camera + SynthHD) | Runs against the Thorlabs camera and COM port in `config.yaml` |
| ODMR: simulated hardware | Runs with mocks, no instruments needed |
| ODMR: re-open a saved datacube | Prompts for an `.npz` and opens it for analysis |
| ODMR: publish results (dry run) | Shows what would be pushed, changes nothing |

The plot window needs a real GUI, so all configurations run in the
integrated terminal rather than the debug console.

## Publishing results to GitHub

Raw datacubes cannot go into version control: one sweep is hundreds of
megabytes, GitHub warns above 50 MB per file and rejects above 100 MB, and
a repository accumulating them becomes impractical to clone. So the split
is deliberate:

- **`ODMR/data/`** — full `.npz` datacubes from **"Save raw"**. Git-ignored,
  stays on the acquisition machine.
- **`ODMR/results/`** — compact bundles from **"Export"**. Tracked by git.

Each exported bundle is about 1–2 MB and contains:

| File | Contents |
| --- | --- |
| `metadata.json` | Every setting used, plus the **git commit** that produced the result (marked `-dirty` if the working tree had uncommitted changes), so a result can be traced to exact code and configuration |
| `spectrum_x*_y*_r*.csv` | The ROI spectrum: frequency, raw counts, normalised %, fitted baseline, repeat count, error |
| `spectrum.png` | Plotted spectrum with error band |
| `maps.png` | Mean PL and ODMR contrast map, with the ROI marked |
| `maps.npz` | Those two maps as float32 arrays, so the shared result carries real numbers and not only pictures |
| `README.md` | Human-readable summary with a dip/contrast table |

To publish, from `ODMR/`:

```bash
python publish_results.py --dry-run   # check what would be committed
python publish_results.py             # commit + push the newest bundle
python publish_results.py --all       # every unpublished bundle
python publish_results.py -m "culet centre, 2 GPa"
```

or use **Tasks: Run Task** → *ODMR: publish results to GitHub*.

This is a deliberate manual step, not something the acquisition GUI does on
its own — pushing is outward-facing and should not happen as a side effect
of pressing a button mid-experiment. The script refuses to commit any file
over 100 MB (and warns above 50 MB) rather than letting the push fail after
the commit is already made, and retries transient network failures with
exponential backoff.

## Repeated sweeps and error bars

Set `sweep.repeats` to average several full sweeps. Noise falls as
√repeats while total time grows linearly, so 4 repeats halves the noise
and 16 repeats quarters it.

Two details make the averaging trustworthy:

**Alternating sweep direction** (`sweep.alternate_direction`, on by
default) runs every second repeat from high to low frequency. Without it,
any slow drift — laser power wandering, NV bleaching, the sample creeping
under pressure — correlates with frequency, because frequency is always
visited in the same time order. A steadily dimming sample would tilt the
whole spectrum and could be mistaken for, or could hide, a real
resonance. Alternating makes the drift symmetric about the middle of the
sweep so averaging largely cancels it instead of baking it into the
lineshape.

**Error bars** (`sweep.estimate_errors`) come from the scatter *between*
repeats, not from a noise model, so they reflect whatever is actually
fluctuating in your setup. The shaded band around the spectrum is ±1
standard error, and the status line reports the typical value. This is the
only way to tell a shallow real dip from a noise excursion. It doubles the
memory used while acquiring (a sum-of-squares accumulator alongside the
sum) and does nothing when `repeats` is 1, since one measurement has no
scatter.

Aborting mid-way is safe: each frequency point is divided by the number of
times it was actually measured, and points never reached are dropped. The
status line says so when the repeats came out uneven.

## Noise and systematics

Several defaults exist specifically to keep slow drift and outliers out of
the result. They cost measurement time, so they are all adjustable.

**Discarded frames after each change** (`sweep.discard_frames`,
`diagnostic.discard_frames`). A free-running camera can already be
mid-exposure when the microwave frequency or RF state changes, so that
frame straddles two conditions. Keeping it blurs adjacent frequency points
together and adds comb-like structure to the noise floor. One discarded
frame is usually enough; `sweep.settle_ms` should also be at least as long
as the exposure.

**ABBA ordering in the MW check.** Each cycle runs OFF-ON-ON-OFF rather
than a plain OFF/ON pair. With a plain pair the two states sit a fixed time
apart, so a *linear* drift biases every cycle identically and survives
averaging as a false contrast. Under ABBA the mean acquisition time of the
OFF frames equals that of the ON frames, so linear drift cancels within
each cycle. Measured on a simulated camera whose laser decays 0.1 % per
frame, with the microwave parked off resonance (true contrast exactly 0):

| drift per frame | plain OFF/ON | ABBA |
| --- | --- | --- |
| 0 | +0.0009 % | +0.0004 % |
| 0.02 % | +0.0407 % | +0.0004 % |
| 0.05 % | +0.1021 % | +0.0005 % |
| 0.10 % | +0.2054 % | +0.0008 % |

A 0.2 % false contrast is easily mistaken for a weak real NV signal.

**Robust baseline normalisation** (`analysis.baseline_correction`). The
spectrum is divided by a straight line fitted to the off-resonance level,
not by its single brightest point. Dividing by the maximum lets one upward
noise spike define 100 %, and leaves any drift-induced tilt in the
lineshape. The fit iteratively rejects points lying *below* it — one-sided,
because a resonance can only darken the photoluminescence, so downward
outliers are signal and upward ones are noise. That means it finds the
baseline wherever the dips happen to sit, including near the ends of the
sweep. (A fixed "fit the two ends" rule fails on the default 2800–2940 MHz
range, since NV resonances near 2820/2920 land inside the end windows and
drag the fit down.) Measured against a known 8.080 % contrast:

| spectrum | divide-by-max | robust fit |
| --- | --- | --- |
| clean | +0.010 pp | −0.006 pp |
| one +3 % noise spike | +1.684 pp | +0.120 pp |
| 4 % linear tilt | +3.147 pp | −0.006 pp |

**Significance flag.** When a dip is smaller than 3× its own error bar the
status line says `(below 3x error — not significant)`, so a number is not
quoted as a measurement when it is a fluctuation.

## "MW check" — is anything actually working?

Before committing to a full sweep, this button answers the question *are
the NV centers responding at all?* in a few seconds.

It parks the microwave at one frequency and **interleaves** RF off / RF on
frames, averaging many of each. Interleaving matters: measuring all the
"off" frames and then all the "on" frames would let a slow drift — laser
power wandering, NV bleaching, sample creep — masquerade as ODMR contrast.
The app also discards one frame after each RF switch, since a free-running
camera may be part-way through an exposure when the microwave changes.

The result is displayed as a **PL drop map** and summarised in the status
line, e.g.

```
NV response detected at 2820.0 MHz — 36042 px responding, peak drop 13.96 %, ...
NO clear response at 2870.0 MHz (noise 0.14 %). Check: laser on the NV spot? ...
```

The detection test is self-calibrating rather than a fixed contrast cutoff.
A real ODMR response can only *darken* the photoluminescence, so the
negative side of the difference distribution is pure noise. Counting
pixels beyond `+threshold` and beyond `-threshold` and taking the excess
gives the number genuinely responding — necessary because per-pixel noise
varies strongly with brightness, so a fixed "> 0.5 %" rule reports false
positives on the dim pixels.

**Set `diagnostic.check_freq_mhz` to a frequency you expect to be a
resonance** (2870 MHz at zero field; a split value if you have a bias
field). Parking off-resonance correctly reports "no response" — that is
the test working, not a fault. Once a sweep has been measured the app
ignores the config value and uses the deepest dip it actually found.

If it reports no response, work through:

1. **Emission filter** — a long-pass (≈650 nm) or NV band-pass must sit in
   front of the camera. Without it you are imaging scattered green
   excitation light, which carries no ODMR contrast no matter what the
   microwave does.
2. **Laser actually on the NV** — with the filter in, the NV grains should
   still be visible. If everything goes dark, you were seeing only
   reflection and the laser is not exciting NV.
3. **Microwave coupling** — is the antenna/loop close enough to the sample,
   is the SynthHD output enabled, is the power reasonable?
4. **Frequency** — the parked frequency must be a genuine resonance.

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
3. Set `camera.dll_dir` in `config.yaml` to the folder with the native DLLs,
   e.g. `.../SDK/Native Toolkit/dlls/Native_64_lib`. This step is easy to
   miss: `pip install`ing `thorlabs_tsi_sdk` only gets you the Python
   wrapper, and importing it fails with
   `Could not find module 'thorlabs_tsi_camera_sdk.dll'` unless this
   folder is explicitly registered — Python 3.8+ no longer searches plain
   `PATH` entries for a module's native dependencies, so just adding the
   folder to your system PATH is not enough on its own (the app calls
   `os.add_dll_directory()` for you once `dll_dir` is set). Match
   `Native_64_lib` / `Native_32_lib` to your Python interpreter's bitness.
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
| `camera.exposure_ms` | Starting exposure; adjust live with the GUI slider. Longer collects more photons (better SNR) but slows the sweep and risks saturation. |
| `camera.exposure_limits_ms` | Range of the exposure slider. The camera clamps to its own hardware limits anyway. |
| `roi.half_size_px` | Starting ROI size; adjust live with the slider. Larger averages more pixels — smoother spectrum and tighter error bars (errors add in quadrature) — but blurs spatial detail. |
| `sweep.start_mhz` / `stop_mhz` / `step_mhz` | Starting sweep range; editable live in the GUI. |
| `microwave.freq_limits_mhz` | Generator tuning range used to validate GUI entries. SynthHD 54 MHz–13.6 GHz, SynthHD PRO 10 MHz–15 GHz — check your unit. |
| `sweep.repeats` | Sweeps averaged together. Noise falls as √repeats, time grows linearly. |
| `sweep.alternate_direction` | Reverses every 2nd repeat so slow drift cancels instead of tilting the lineshape. |
| `sweep.estimate_errors` | Error bars from between-repeat scatter. Doubles acquisition memory; no effect at `repeats: 1`. |
| `sweep.discard_frames` | Frames dropped after each frequency change so no frame straddles two frequencies. |
| `sweep.settle_ms` | PLL lock time after a frequency step; set at least as long as `camera.exposure_ms`. |
| `analysis.baseline_correction` | Robust line fit for normalisation instead of divide-by-max. |
| `analysis.baseline_reject_sigma` | How far below the fit a point must lie to be treated as signal and excluded. |
| `diagnostic.cycles` | ABBA cycles averaged by the MW check (4 frames each). |
| `diagnostic.check_freq_mhz` | Frequency the "MW check" parks at. Must be a real resonance or the check correctly reports nothing. Ignored once a sweep has run. |
| `diagnostic.cycles` | Interleaved off/on pairs averaged by the MW check; more cycles = lower noise floor. |
| `diagnostic.settle_ms` | Wait after each RF switch during the MW check. Set at least as long as `camera.exposure_ms`. |
| `analysis.min_signal_fraction` | Contrast-map threshold. Dark pixels have near-zero mean PL, so their shot noise produces huge spurious "contrast"; pixels below this fraction of the brightest pixel are blanked. Lower it if a genuinely dim part of the sample is being masked. |
