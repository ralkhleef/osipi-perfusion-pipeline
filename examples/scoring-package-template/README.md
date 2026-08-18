# Scoring-package template

Copy this directory when adding a trusted challenge-specific analysis. The
example only counts the supplied NIfTI files. It demonstrates the package
interface; it is not a scientific score or official OSIPI ranking.

1. Give `package_id` a new versioned id and update `version`.
2. Set `challenge_type`, `required_inputs`, `required_assets`, and `metrics`.
3. Implement the declared metrics in `scorer.py` and write them to
   `metrics.json`.
4. Add deterministic unit tests under `tests/`.
5. Review the code, ZIP the package contents, upload it in Scoring Setup, test
   the challenge configuration, and activate it explicitly.

The standard interface is:

```text
python scorer.py --submission-dir PATH --output-dir PATH [--reference-dir PATH]
```

Only trusted reviewer-approved packages should be installed because package
code runs on the local backend.
