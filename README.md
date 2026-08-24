# OSIPI Perfusion Pipeline

A local web app for reviewing DCE, ASL, and DSC perfusion MRI challenge submissions.

**Upload → Review → Validate → Run → QC & Preview → Export**

The app accepts a ZIP, folder/files, public GitHub repository, or Zenodo record.
It validates submitted files, runs participant code in Docker when needed, and
creates QC, previews, reports, and structured exports. Additional analysis is
shown when compatible masks, references, or trusted analysis packages are
available.

Official OSIPI challenge ranking is not currently configured.

## Run locally

```bash
docker compose up --build
```

Open http://localhost:8000.

Stop the app with:

```bash
docker compose down
```

The `scripts/start/` and `scripts/stop/` folders also contain macOS, Linux, and
Windows launchers for reviewers who prefer not to type Docker commands.

## Repository guide

| If you want to… | Go to… |
|---|---|
| Change challenge requirements | `config/validation_rules.yaml` or the Configuration Manager in the app |
| Change runtime limits or paths | `config/settings.yaml` |
| Change API routes or report exports | `backend/` |
| Change ingestion, validation, execution, or analysis logic | `src/osipi_pipeline/` |
| Change the web interface | `frontend/` |
| Add or update tests | `tests/` |
| Change the GitHub Pages site | `docs/` |
| Work with a custom scoring package | `examples/scoring-package-template/` |
| Review pending scientific decisions | `notes/SCIENTIFIC_REQUIREMENTS_PENDING.md` |

The [code walkthrough](notes/CODE_WALKTHROUGH.md) explains how these areas connect.
More focused maintainer notes are available for
[configuration](notes/configuration.md),
[ingestion](notes/ingestion_notes.md),
[Docker execution](notes/execution_notes.md),
[updating scoring](notes/UPDATING_SCORING.md), and
[adding a metric](notes/ADDING_SCORING_METRICS.md).

## Run tests

Install the development dependencies once:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -r backend/requirements.txt -r requirements-test.txt
```

Run the Python and frontend suites:

```bash
OSIPI_REQUIRE_FULL_TESTS=1 PYTHONPATH=.:backend:src .venv/bin/pytest -q
node tests/frontend_smoke_test.js
node tests/footer_logic_test.js
node tests/frontend_validation_card_test.js
node tests/frontend_roi_dom_test.js
node tests/frontend_header_check_test.js
```

The trusted scoring-package example is in `data/sample_submissions/`; test inputs
are in `tests/fixtures/`. Generated outputs, uploaded submissions, private
reference assets, and installed scoring packages are ignored by Git.

## More information

- [Documentation](https://ralkhleef.github.io/osipi-perfusion-pipeline/)
- [Installation](https://ralkhleef.github.io/osipi-perfusion-pipeline/install.html)
- [Configuration](https://ralkhleef.github.io/osipi-perfusion-pipeline/configuration.html)
- [GSoC work product](notes/GSoC_WORK_PRODUCT.md)
