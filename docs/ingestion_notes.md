# Ingestion notes

Ingestion imports a local folder, ZIP file, public GitHub repository, or Zenodo
record into the local workspace.

## What it does

- copies folders or safely extracts ZIP files;
- clones public GitHub repositories;
- detects a likely challenge from configured names and keywords;
- separates batch submissions while preserving normal internal folders; and
- creates JSON and CSV file inventories for CLI imports.

Challenge detection is a first guess. The reviewer confirms it before
validation.

CLI imports are stored under:

```text
submissions/extracted/{challenge_type}/{submission_id}
```

## Commands

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest \
  --input submissions/incoming/team_alpha.zip
```

Use `--challenge dce`, `--challenge asl`, or `--challenge dsc` to choose the
challenge explicitly.

## Boundaries

Ingestion does not inspect scientific correctness, run Docker, or compare maps
with references. Those actions happen in later steps. GitHub imports also need
enough local disk space and may time out for large repositories.
