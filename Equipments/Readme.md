# Equipments

- `CMOS camera/` — Thorlabs CMOS camera driver (`thorlabs_camera.py`) used for
  widefield NV imaging. See the quick start guide PDF in this folder for the
  camera model's connection details.
- `Microwave generator/` — driver (`synthhd.py`) for the
  [Windfreak Technologies SynthHD](https://windfreaktech.com/product/microwave-signal-generator-synthhd/)
  microwave signal generator used to drive ODMR transitions.

Both drivers are tied together by the click-to-measure ODMR app in
`../ODMR/` — see `../ODMR/README.md` for setup and usage.
