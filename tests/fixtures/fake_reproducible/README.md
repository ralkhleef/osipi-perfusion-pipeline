# Fake Reproducible Submission

Integration test fixture for the OSIPI reproducible execution workflow.

## What it does

`run.py` generates three synthetic DCE perfusion maps using only the Python standard library (no nibabel, no numpy):

- `ktrans.nii.gz` — constant 0.15 min⁻¹
- `kep.nii.gz` — constant 0.42 min⁻¹
- `vp.nii.gz` — constant 0.05

All maps are 4×4×4 voxels, float32, written as gzip-compressed NIfTI-1 files.

## How to package

```bash
cd tests/fixtures
zip -r osipi_reproducible_test.zip fake_reproducible/
```

## Expected pipeline behaviour

1. **Validation (reproducible mode)**: passes — Dockerfile present, run_config.json present, no pre-existing output maps required.
2. **Execution**: Docker builds from `python:3.11-slim`, runs `python3 run.py`, writes three `.nii.gz` files to `/output`.
3. **Output validation**: passes — 3 NIfTI files found matching DCE expected maps (ktrans, kep, vp).
