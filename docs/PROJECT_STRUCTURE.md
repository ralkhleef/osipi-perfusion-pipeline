# Project Structure

This project is organized so mentors and users can quickly see what each folder is for.

[GitHub Repository](https://github.com/ralkhleef/osipi-perfusion-pipeline)

## Main Folders

| Folder | Purpose |
| --- | --- |
| `backend/` | FastAPI backend and web-app pipeline logic. |
| `frontend/` | Web UI files served by the backend. |
| `data/` | Reference data, sample submissions, and generated output data. |
| `data/reference_data/` | Downloaded reference or challenge data, such as Zenodo files. |
| `data/sample_submissions/` | Small sample or demo submissions for testing and demonstrations. |
| `data/outputs/` | Generated validation results, manifests, and execution logs. |
| `submissions/` | Uploaded and extracted participant submissions used by the local app. |
| `submissions/incoming/` | Uploaded ZIP files or incoming submission material. |
| `submissions/extracted/` | Extracted submissions ready for validation. |
| `submissions/validated/` | Reserved space for validated submissions or future workflow outputs. |
| `scripts/` | Startup, stop, release, and developer utility scripts. |
| `tests/` | Automated tests for ingestion, validation, and execution logic. |
| `docs/` | Project notes and documentation. |

## Root Files

| File | Purpose |
| --- | --- |
| `README.md` | General project overview. |
| `README_DOCKER.md` | Simple Docker app setup instructions. |
| `docker-compose.yml` | Local Docker Compose configuration. |
| `Dockerfile` | Docker image definition for the local app. |
| `.dockerignore` | Files excluded from Docker build context. |
| `requirements.txt` | Python dependencies for the app and pipeline. |
| `start.command`, `start.bat`, `stop.sh`, `stop.bat` | Easy launch and stop wrappers for nontechnical users. |

Runtime folders such as `data/outputs/`, `submissions/incoming/`, and `submissions/extracted/` are intentionally kept in the structure, but their generated contents should not be committed.
