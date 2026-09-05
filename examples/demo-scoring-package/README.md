# DCE map summary example

This is a working example, not official OSIPI scoring.

It opens the submitted NIfTI files and reports file count, readable file count,
finite voxels, negative voxels, and the mean of the individual map means. Every
reported number is calculated from the supplied files. It has no overall score,
ranking, pass/fail result, or acceptance limit.

Only trusted reviewers or administrators should upload scoring packages. The
package runs Python code on the local server, so review `scoring.py` first.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Package name, version, challenge, and interface |
| `scoring.py` | Calculates the map summaries |

## Interface

```bash
python scoring.py \
  --submission-dir PATH \
  --output-dir PATH \
  [--reference-dir PATH]
```

The package writes `metrics.json` and `results.json` to the output folder.

For ASL or DSC, use `scripts/make_example_scoring_package.py`; it reads the
required maps directly from the challenge configuration. For a different
analysis, start with
[`examples/scoring-package-template/`](../scoring-package-template/) and use a
new versioned package id. Upload and activation are separate actions in Scoring
Setup.
