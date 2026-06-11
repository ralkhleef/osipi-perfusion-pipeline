# OSIPI Perfusion Pipeline

A Python pipeline for evaluating OSIPI perfusion MRI challenge submissions. It handles ingestion, validation, execution, scoring, and reporting.

**GitHub:** https://github.com/ralkhleef/osipi-perfusion-pipeline

---

## Running the App

The app runs locally using Docker. You do not need to write any code.

**Requirements:** Docker Desktop installed and running.

**Mac** — double-click `start.command`

**Windows** — double-click `start.bat`

Then open http://localhost:8000 in your browser.

To stop the app, double-click `stop.sh` (Mac) or `stop.bat` (Windows).

### Running without Docker (development)

```bash
cd backend
~/Desktop/osipi-perfusion-pipeline/.venv/bin/python3 -m uvicorn main:app --reload --port 8000
```

---

## Pipeline Steps

```
Submission → Ingestion → Validation → Execution → Scoring → Reporting
```

| Step | Status |
|------|--------|
| Ingestion | Done — accepts ZIP, folder, GitHub repo, or Zenodo link |
| Validation | Done — checks NIfTI files, README, code, map types |
| Execution | Done — runs submission in Docker container |
| Scoring | In progress — rankings by pass/fail and error count |
| Reporting | Done — CSV export, JSON export, HTML validation report, rankings table |

---

## Project Layout

```
backend/          FastAPI server and pipeline services
frontend/         Web UI (HTML, CSS, JS)
src/              Python package for CLI pipeline use
data/
  reference_data/ Reference datasets for scoring
  sample_submissions/ Small demo submissions for testing
  outputs/        Generated validation results and manifests
submissions/
  incoming/       Uploaded ZIPs
  extracted/      Extracted submissions ready for validation
docker/           Example Dockerfile for submissions
docs/             Notes and documentation
tests/            Automated tests
scripts/          Start, stop, and release scripts
```

---

## Submission Sources

| Source | How to use |
|--------|-----------|
| Local ZIP | Upload a .zip file in the app |
| Local folder | Select a folder or files |
| GitHub repo | Paste a GitHub URL |
| Zenodo | Paste a Zenodo URL, DOI, or record ID |

---

## Validation Checks

| Check | Type |
|-------|------|
| Submission folder exists and has files | Error |
| At least one .nii or .nii.gz file | Error |
| README or metadata file present | Error |
| Challenge type is asl or dce | Error |
| Dockerfile present | Warning |
| Code files present | Warning |
| Expected map names present (Ktrans/kep/vp for DCE, CBF/ATT for ASL) | Warning |
| NIfTI files are not empty | Warning |
| No duplicate filenames | Warning |

NIfTI files are opened with nibabel to check that they load, have at least 3 dimensions, and have a valid affine matrix.

---

## CLI Commands

Ingest a submission:
```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest --input submissions/incoming/team_alpha.zip
```

Validate a submission:
```bash
PYTHONPATH=src python3 -m osipi_pipeline.validation.validate --input submissions/extracted/dce/dce_team_alpha --challenge dce
```

Run tests:
```bash
PYTHONPATH=src python3 -m pytest
```
