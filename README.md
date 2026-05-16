# OSIPI Perfusion Pipeline

This repository is the start of a modular Python pipeline for OSIPI perfusion
MRI challenge submissions.

Current phase: **ingestion only**.

The ingestion stage accepts a submission folder, `.zip`, or GitHub repository
URL, detects the challenge type with simple rule-based ASL/DCE heuristics,
normalizes the submission into a local workspace, writes JSON and CSV manifests,
and prints a concise summary.

Later phases may add validation, Docker execution, scoring, and reporting, but
those are intentionally out of scope for now.

## Supported Inputs

- Submission folders
- `.zip` submission archives
- GitHub repository URLs
- ASL and DCE challenge submissions
- Unknown challenge submissions, which are still ingested under `unknown`

Future source types can be added without changing the user-facing command:
OSF download links, Google Drive links, and direct HTTP download links.

## Example Commands

```bash
python -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip
```

```bash
python -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha --challenge dce
```

```bash
python -m osipi_pipeline.ingestion.ingest --input https://github.com/osipi/team-alpha-submission
```

GitHub ingestion uses a shallow local clone to keep the working copy small.

## Storage Policy

This project should stay free and lightweight. Store code, configs, docs, small
examples, and manifests in the repository. Do not commit large MRI datasets,
challenge archives, extracted submissions, or downloaded working copies.

Use these folders for local work only:

- `submissions/incoming/`
- `submissions/extracted/`
- `data/reference/`

The repository keeps placeholder files so the folder structure is visible, but
large data files are ignored by `.gitignore`.

## Challenge Detection

Challenge type detection is configured in
`src/osipi_pipeline/config/challenge_types.py`.

- DCE keywords: `Ktrans`, `kep`, `vp`, `DCE`, `Dynamic Contrast Enhanced`
- ASL keywords: `CBF`, `ATT`, `ASL`, `arterial spin labeling`

If no rule matches, the submission is marked as `unknown`.

## Expected Output Structure

```text
submissions/
  incoming/
  extracted/
    dce/
      team_alpha/
    asl/
    unknown/

outputs/
  manifests/
    dce_team_alpha_manifest.json
    dce_team_alpha_manifest.csv
```

Each manifest includes:

- `submission_id`
- `challenge_type`
- `original_path`
- `extracted_path`
- `file_count`
- `nifti_files`
- `metadata_files`
- `code_files`
- `docker_files`
- `readme_files`
- `timestamp`

## Development

Run tests with:

```bash
python -m pip install -e ".[dev]"
pytest
```
