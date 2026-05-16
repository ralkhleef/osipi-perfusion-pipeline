# Ingestion Notes

Ingestion is the first step of the OSIPI perfusion pipeline.

It brings a submitted folder, zip file, or GitHub repository into a standard local workspace so later pipeline steps can find it.

## Supported Sources

| Source | What it means |
| --- | --- |
| Local folder | A submission folder already on your computer |
| ZIP archive | A packaged submission file ending in `.zip` |
| GitHub repo URL | A repository URL that can be cloned locally |

## What Ingestion Does

| Step | Description |
| --- | --- |
| Accepts input | Takes a folder path, zip path, or GitHub repository URL |
| Copies or extracts | Copies folders, extracts zip files, or clones GitHub repos |
| Creates a workspace | Places files under `submissions/extracted/{challenge_type}/{submission_id}` |
| Detects challenge type | Guesses whether the submission is ASL, DCE, or unknown |
| Creates manifests | Writes JSON and CSV inventory files |

## Output Location

Ingested submissions are organized like this:

```text
submissions/
  extracted/
    dce/
      team_alpha/
    asl/
      team_beta/
    unknown/
      team_gamma/
```

The exact output path follows this pattern:

```text
submissions/extracted/{challenge_type}/{submission_id}
```

## What a Manifest Is

A manifest is a small inventory file created during ingestion.

It records:

| Field | Meaning |
| --- | --- |
| `submission_id` | The name used for the ingested submission |
| `challenge_type` | `asl`, `dce`, or `unknown` |
| `original_path` | Where the submission came from |
| `extracted_path` | Where the local working copy was placed |
| `file_count` | How many files were found |
| `nifti_files` | Files ending in `.nii` or `.nii.gz` |
| `metadata_files` | Files like `.json`, `.yaml`, `.csv`, or `.tsv` |
| `code_files` | Files that look like source code or scripts |
| `docker_files` | Docker-related files |
| `readme_files` | README files |
| `timestamp` | When ingestion happened |

Manifests matter because later validation, scoring, and reporting steps can use them to understand what was submitted.

## Challenge Detection

Challenge detection is a simple filename and text check.

| Challenge | Example keywords |
| --- | --- |
| DCE | `Ktrans`, `kep`, `vp`, `DCE` |
| ASL | `CBF`, `ATT`, `ASL`, `arterial spin labeling` |
| Unknown | Used when no keyword matches |

This is only a first guess. Validation later checks whether the submission looks acceptable.

## What Ingestion Does Not Do

Ingestion does not:

- Validate real NIfTI image contents
- Score submitted results
- Run Docker
- Confirm that the submission is scientifically correct
- Check whether maps match reference answers

## Common Commands

Ingest a local folder:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha
```

Ingest a zip archive:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip
```

Ingest a GitHub repository:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input https://github.com/osipi/team-alpha-submission
```

Optionally, provide the challenge type yourself:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip --challenge dce
```

## Current Limitations

| Limitation | Why it matters |
| --- | --- |
| Large GitHub repos may timeout | GitHub clone has a timeout so ingestion does not hang forever |
| Detection is filename/text based | It does not inspect image contents or scientific meaning |
| Large MRI data should not be committed | The repo should stay lightweight and free to use |
| GitHub repos are cloned locally | Users still need enough disk space for the working copy |

