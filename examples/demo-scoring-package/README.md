# Demo DCE Scoring Package

**Demo only. This is not official OSIPI scoring.**

This package generates synthetic metrics so developers can test the custom
package interface without private reference data.

Only trusted reviewers or administrators should upload scoring packages. The
package runs Python code on the local server, so review `scoring.py` first.

## Files

| File | Purpose |
|---|---|
| `manifest.json` | Package name, version, challenge, and interface |
| `scoring.py` | Demo scorer |
| `reference/` | Empty demo reference folder |
| `masks/` | Empty demo mask folder |

## Interface

```bash
python scoring.py \
  --submission-dir PATH \
  --output-dir PATH \
  [--reference-dir PATH]
```

The package writes `metrics.json` and `scoring_log.txt` to the output folder.

For a new package, start with
[`examples/scoring-package-template/`](../scoring-package-template/)
and use a new versioned package id. Upload and activation are separate actions
in Scoring Setup.
