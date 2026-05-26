# OSIPI Perfusion Pipeline

A Python pipeline for organizing, validating, running, scoring, and reporting OSIPI perfusion MRI challenge submissions.

The goal is to make challenge evaluation more reproducible, automated, and easy to extend.

---

## Pipeline Flow

```mermaid
flowchart LR
    A[Submission] --> B[Ingestion]
    B --> C[Validation]
    C --> D[Execution]
    D --> E[Scoring]
    E --> F[Reporting]
```

---

## Supported Submission Sources

| Source       | Purpose                                   |
| ------------ | ----------------------------------------- |
| Local folder | Use a submission already on your computer |
| ZIP archive  | Use a compressed submission               |
| GitHub repo  | Pull a submission from GitHub             |

---

## Commands

```bash
python -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip
```

```bash
PYTHONPATH=src python3 -m osipi_pipeline.validation.validate --input submissions/extracted/dce/dce_team_alpha --challenge dce
```

---

## NIfTI Validation

The validation step uses [nibabel](https://nipy.org/nibabel/) to actually open each `.nii` / `.nii.gz` file, not just check its filename.

What is checked:
- The file can be loaded by nibabel (if not, validation fails with `NIFTI_UNREADABLE`)
- The image shape exists and has at least 3 dimensions (fewer triggers a warning)
- The affine matrix exists and is 4×4
- Basic stats are recorded: shape, dtype, min, max, mean, NaN count, inf count
- NaN or infinite values are reported as warnings, not errors, since some parameter maps use NaN for masked voxels

What this does **not** do:
- No scientific scoring, RMSE, bias, CoV, or ICC
- No full BIDS compliance check — only basic readability
- No comparison against reference data

The NIfTI results are saved under the `nifti_summary` key in the JSON output file.

---

## Run Tests

```bash
PYTHONPATH=src python3 -m pytest
```
