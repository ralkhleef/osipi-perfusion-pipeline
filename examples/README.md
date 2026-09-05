# Analysis package examples

The app calls these scoring packages because that is the package interface
name. An example does not need to produce a score.

## Ready examples

- `demo-scoring-package/` is a tracked DCE example. It calculates descriptive
  values from the supplied NIfTI files.
- `scoring-package-template/` is the smallest package to copy when starting a
  separate analysis.
- `scripts/make_example_scoring_package.py` creates ready-to-upload ASL, DCE,
  and DSC ZIP files.

```bash
python3 scripts/make_example_scoring_package.py --challenge asl
python3 scripts/make_example_scoring_package.py --challenge dce
python3 scripts/make_example_scoring_package.py --challenge dsc
```

The ZIP files are written under `data/scoring/examples/`, which is ignored by
Git. Each manifest gets its required map names from
`config/validation_rules.yaml` when the package is generated.

The included examples report only values calculated from their inputs. They do
not define a score, rank, pass/fail result, or acceptance limit. Add those only
when the challenge team supplies an approved definition and test data.
