# Demo DCE Scoring Package

> ⚠ **DEMO / TEST ONLY — NOT OFFICIAL OSIPI SCORING**
>
> This package outputs synthetic, randomly-generated metrics for
> pipeline testing and demonstration purposes only.  It does NOT
> perform any real scientific evaluation.

---

## Purpose

This package demonstrates the scoring package interface for the
OSIPI Perfusion Pipeline.  Reviewers can use it to:

- Test that the scoring pipeline wires up correctly end-to-end
- Develop and validate custom scoring packages before using real data
- Demo the Scoring Setup admin panel without needing official reference data

---

## ⚠ Security Warning

**Only trusted reviewers and administrators should upload scoring packages.**
A scoring package is Python code that runs on the server.  You must review
the `scoring.py` script before uploading it to confirm it is safe.

Never upload scoring packages from untrusted sources.

---

## Package Contents

| File | Purpose |
|---|---|
| `manifest.json` | Package metadata and interface configuration |
| `scoring.py` | Demo scoring script (outputs synthetic metrics) |
| `README.md` | This file |
| `reference/` | *(empty)* — put reference NIfTI maps here for real scoring |
| `masks/` | *(empty)* — put mask NIfTI files here for real scoring |

---

## Interface

The script is called with the `standard` call_mode:

```bash
python scoring.py \
    --submission-dir  <path to execution output NIfTIs> \
    --output-dir      <path where scoring results are written> \
    [--reference-dir  <optional: path to reference data>]
```

**Required outputs** (written to `--output-dir`):

- `metrics.json` — metric values, e.g. `{"demo_rmse": 0.12, "demo_score": 87.3}`
- `scoring_log.txt` — human-readable summary

---

## Replacing with Real OSIPI Scoring

To use official OSIPI TF6.2 DCE Ktrans scoring instead:

1. Obtain `challengeScoring.py` from the OSIPI TF6.2 challenge repository.
2. Obtain `DROKtransNifti/` (reference Ktrans NIfTI maps) and `Masks/` from the same source.
3. Place them in `data/scoring/providers/osipi_tf62_dce_ktrans/`.
4. In the Scoring Setup admin panel, select **"Default OSIPI scoring package"**.
5. Apply the configuration.

Alternatively, package a custom scoring script as a ZIP with a `manifest.json`
and upload it via the **"Custom scoring package"** option.

---

## Manifest Fields

```json
{
  "package_id":     "demo_dce_scoring",
  "name":           "Demo DCE Scoring Package",
  "version":        "1.0.0",
  "challenge_type": "dce",
  "map_type":       "ktrans",
  "entry_point":    "scoring.py",
  "call_mode":      "standard",
  "metrics":        ["demo_rmse", "demo_bias", "demo_cv", "demo_score"]
}
```

`call_mode` options:
- `"standard"` — script called with `--submission-dir`, `--output-dir`, optional `--reference-dir`
- `"osipi_cwd"` — script run with `cwd=package_dir`; reads hardcoded paths (legacy TF6.2 style)
