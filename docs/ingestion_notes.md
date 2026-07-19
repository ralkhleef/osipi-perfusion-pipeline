# Ingestion Notes

Ingestion brings a submitted folder, ZIP file, Zenodo record, or GitHub repository into a local workspace so later pipeline steps can validate, execute, preview, score, and export it.

## Supported Sources

| Source | What it means |
|---|---|
| Local folder | A submission folder already on disk |
| ZIP archive | A packaged submission file ending in `.zip` |
| GitHub repo URL | A repository URL that can be cloned locally |
| Zenodo link | A record imported through the backend import path |

## What Ingestion Does

| Step | Description |
|---|---|
| Accepts input | Takes a folder path, ZIP path, GitHub repository URL, or backend-imported source |
| Copies or extracts | Copies folders, safely extracts ZIP files, or clones GitHub repos |
| Creates a workspace | Places files under `submissions/extracted/{challenge_type}/{submission_id}` for CLI ingestion or under the backend extracted submission root |
| Detects challenge type | Scores configured challenge keywords from `config/validation_rules.yaml` |
| Detects batches | Splits multi-submission ZIPs while preserving single-submission structural layouts |
| Creates manifests | Writes JSON and CSV inventory files for CLI ingestion |

## Output Location

CLI-ingested submissions are organized like this:

```text
submissions/
  extracted/
    dce/
      team_alpha/
    asl/
      team_beta/
    dsc/
      team_gamma/
    unknown/
      team_delta/
```

The exact CLI output path follows this pattern:

```text
submissions/extracted/{challenge_type}/{submission_id}
```

The backend upload flow stores extracted submissions by safe submission id and keeps challenge metadata in the API/session records.

## What a Manifest Is

A manifest is a small inventory file created during CLI ingestion.

| Field | Meaning |
|---|---|
| `submission_id` | The name used for the ingested submission |
| `challenge_type` | Configured challenge id or `unknown` |
| `original_path` | Where the submission came from |
| `extracted_path` | Where the local working copy was placed |
| `file_count` | How many files were found |
| `nifti_files` | Files ending in configured `nifti_suffixes` |
| `metadata_files` | Files ending in configured `metadata_suffixes` |
| `code_files` | Files matching configured code extensions |
| `docker_files` | Docker-related files |
| `readme_files` | Files matching configured README/SOP/metadata names |
| `timestamp` | When ingestion happened |

Manifests matter because validation, scoring, and reporting can use them to understand what was submitted.

## Challenge Detection

Challenge detection is filename and folder-name based. The detector reads `challenges.<id>.keywords` from `config/validation_rules.yaml`, counts matches for each configured challenge, and returns the best match. If nothing matches, the challenge is `unknown` until the reviewer chooses one.

This is only a first guess. Validation later checks whether the selected challenge is configured and whether expected maps are present.

## Structural Layouts vs Batches

Some single submissions contain internal folders such as `input/`, `results/`, `maps/`, `code/`, or `scripts/`. Those names are configured in `settings.yaml` under `ingestion.structural_subdirs`. If all top-level directories look structural, ingestion treats the ZIP as one submission rather than splitting it as a batch.

If a ZIP contains team-named folders such as `Team_A/` and `Team_B/`, ingestion treats it as a batch.

## What Ingestion Does Not Do

Ingestion does not:

- Validate NIfTI image contents
- Score submitted results
- Run Docker
- Confirm scientific correctness
- Check maps against reference answers

## Common Commands

Ingest a local folder:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha
```

Ingest a ZIP archive:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip
```

Ingest a GitHub repository:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input https://github.com/osipi/team-alpha-submission
```

Optionally, provide any configured challenge type yourself:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip --challenge dce
```

## Current Limitations

| Limitation | Why it matters |
|---|---|
| Large GitHub repos may timeout | GitHub clone has a timeout so ingestion does not hang forever |
| Detection is filename/text based | It does not inspect image contents or scientific meaning |
| Large MRI data should not be committed | The repo should stay lightweight and free to use |
| GitHub repos are cloned locally | Users still need enough disk space for the working copy |
