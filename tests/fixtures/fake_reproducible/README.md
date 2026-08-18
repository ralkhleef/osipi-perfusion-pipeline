# Fake reproducible submission

This fixture tests Docker execution without scientific dependencies.

`run.py` writes three 4×4×4 float32 DCE maps:

- `ktrans.nii.gz`: 0.15 min⁻¹
- `kep.nii.gz`: 0.42 min⁻¹
- `vp.nii.gz`: 0.05

Package it with:

```bash
cd tests/fixtures
zip -r osipi_reproducible_test.zip fake_reproducible/
```

Validation checks the Docker files, execution writes the maps to `/output`, and
the output check confirms that all three files are present.
