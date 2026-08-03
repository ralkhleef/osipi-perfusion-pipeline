# Project Structure

**GitHub:** https://github.com/ralkhleef/osipi-perfusion-pipeline

## Folders

| Folder | What it contains |
|--------|-----------------|
| `backend/` | FastAPI server, API routes, and pipeline services |
| `config/` | YAML validation rules, challenge/map definitions, defaults, limits, and reporting settings |
| `frontend/` | Web UI — HTML, CSS, and JavaScript |
| `src/` | Python package for CLI modules plus shared config/validation/ingestion/execution code |
| `data/reference_data/` | Reference datasets used for scoring (e.g. downloaded from Zenodo) |
| `data/sample_submissions/` | Small demo submissions for testing |
| `data/outputs/` | Generated validation results, manifests, and execution logs |
| `submissions/incoming/` | Uploaded ZIP files |
| `submissions/extracted/` | Extracted submissions ready for validation |
| `submissions/validated/` | Reserved for future use |
| `docker/` | Example Dockerfile for submissions |
| `scripts/` | Start, stop, and release scripts |
| `tests/` | Automated tests |
| `docs/` | Documentation and notes |

## Root Files

| File | Purpose |
|------|---------|
| `README.md` | Project overview and setup instructions |
| `docs/configuration.md` | Challenge/config format and reference-scoring guide |
| `Dockerfile` | Docker image for the local app |
| `docker-compose.yml` | Docker Compose config for the local app |
| `.dockerignore` | Files excluded from the Docker build |
| `requirements.txt` | Python dependencies |
| `Dockerfile` / `docker-compose.yml` | Build and run the app |

The contents of `data/outputs/`, `submissions/incoming/`, and `submissions/extracted/` are generated at runtime and should not be committed to git.
