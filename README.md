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
| ZIP archive  | Use a packaged submission                 |
| GitHub repo  | Pull a submission or example from GitHub  |

---

## Example Command

```bash
python -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip
```

```bash
PYTHONPATH=src python3 -m osipi_pipeline.validation.validate --input submissions/extracted/dce/dce_team_alpha --challenge dce
```

---

## Run Tests

```bash
PYTHONPATH=src python3 -m pytest
```
