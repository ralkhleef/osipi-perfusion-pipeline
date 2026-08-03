# OSIPI Perfusion Pipeline

A web app for reviewing OSIPI perfusion-MRI challenge submissions. You upload a submission as a ZIP, and the app checks the NIfTI files, figures out what kind of submission it is, runs it in Docker if it's reproducible, compares the maps to reference data when that's available, and gives you CSV, HTML, and PDF reports.

GitHub: https://github.com/ralkhleef/osipi-perfusion-pipeline

---

## For mentors (no coding needed)

You upload a submission (a ZIP of NIfTI maps). The app checks the files, shows previews, compares them to reference data if you've added any, and produces reports you can download.

1. **Run it.** Install Docker Desktop and start it. Then, in this folder, run `docker compose up --build`. Once it's up, open http://localhost:8000 in your browser. Stop it with `docker compose down`.

2. **Try it.** Upload `submissions/incoming/lena_01_exact_single_submission.zip` and click through the six steps (Upload, Review, Validate, Run, QC & Preview, Export). It should come up as ASL with CBF and ATT detected, show QC statistics, and let you download the reports. The RMSE and ROI comparison numbers only appear once you add reference maps (step 3).

3. **Change the scoring.** `docs/UPDATING_SCORING.md` walks through the four ways to do it, from editing one settings file (no coding) to plugging in your own scoring script.

---

## Quick start

You need Docker Desktop installed and running.

```bash
docker build -t osipi-pipeline .

docker run --rm -p 8000:8000 \
  -v "$PWD/data:/app/data" \
  -v "$PWD/submissions:/app/submissions" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e PYTHONPATH=/app/backend:/app/src \
  -e HOST_SUBMISSIONS_DIR="$PWD/submissions" \
  -e HOST_OUTPUTS_DIR="$PWD/data/outputs" \
  -e HOST_REFERENCE_DATA_DIR="$PWD/data/reference_data" \
  osipi-pipeline
```

Then open http://localhost:8000. Stop it with Ctrl-C.

The Docker socket mount and the three `HOST_*` variables are what let the
container start a second container to run a participant's code. The backend
runs inside Docker but calls the host daemon, so the paths it passes to
`docker run` have to be host paths. Without them, upload, validation, QC and
export still work; the Run step does not.

`docker-compose.yml` holds the same configuration and expands `$PWD` for you:

```bash
docker compose up --build      # add -d for the background
docker compose down
```

---

## The six steps

```
Upload → Review → Validate → Run → QC & Preview → Export
```

| Step | What it does |
|------|-------------|
| Upload | Takes a ZIP file. GitHub URLs and Zenodo links work too. |
| Review | Works out whether the submission is result-only (just NIfTI maps) or reproducible (has a Dockerfile and code), and which challenge it is (ASL/DCE/DSC). Batch ZIPs with several sub-folders are split into separate submissions, and you can upload several ZIPs at once. A ZIP laid out as `input/` + `results/maps/` is treated as one submission, not split. |
| Validate | Checks the NIfTI files are readable (nibabel), checks map names and dimensions (parameter maps must be 3D), and looks for a README, Dockerfile, and code. Problems are either errors (which stop you) or warnings (which don't). |
| Run | Reproducible submissions run in Docker. Result-only submissions already have their maps, so they skip this and show "Execution not required." If there's no code and no maps, it says "Cannot continue." |
| QC & Preview | Shows QC statistics and reference comparisons for each parameter map, with CBF and ATT kept separate (never averaged), plus previews. These are not official OSIPI scores: ASL results aren't ranked and there's no pass/fail. Repeatability CoV and ICC show as unavailable until repeated datasets exist. If no reference maps are set up, it just shows QC and says the reference isn't available. |
| Export | Downloads an HTML report, a PDF report, and a long-format CSV (blinded and unblinded, one row per map/ROI/metric). The raw validation, execution, and scoring exports are still available through the API. |

### Which maps get scored

The NIfTI files used for scoring are picked in this order:

1. Docker execution output (`data/outputs/execution/<id>/`) — maps produced by running the team's container.
2. The submitted-map folders from `config/settings.yaml` (by default `results/maps/`, then `results/`, then the top of the extracted folder).

So a result-only challenge can be scored straight from the submitted maps without running Docker.

---

## Project layout

```
backend/             FastAPI server and pipeline services
  main.py            API entry point (all endpoints)
  scoring.py         Scoring / QC comparison logic
  services/
    pdf_report_service.py       ← PDF report generation
    scoring_package_service.py  ← scoring package management
    path_config.py              ← all filesystem path constants
config/
  settings.yaml                 ← defaults, limits, reporting settings
  validation_rules.yaml         ← challenge types, expected maps, detection rules
frontend/            Web UI (HTML + plain JS, no build step)
src/osipi_pipeline/  Python package you can also run from the command line
  config/            Shared YAML config loader
  validation/        Validation logic + nibabel NIfTI checks
  ingestion/         ZIP extraction, batch detection, Zenodo/GitHub fetch
  execution/         Docker runner for reproducible submissions
data/
  reference_data/    ← put reference maps + masks here to turn on comparison
  scoring/
    packages/        ← uploaded scoring packages land here
    active.json      ← which scoring is active per challenge
    providers/       ← built-in provider data (OSIPI TF6.2)
  sample_submissions/
    demo_scoring_package.zip    ← a demo package you can upload to test
submissions/
  incoming/          uploaded ZIPs land here
  extracted/         extracted submissions ready for validation
docker/              example Dockerfile for a reproducible submission
docs/                UPDATING_SCORING.md, configuration.md, the proposal, dev notes
tests/               automated tests (pytest)
scripts/             start / stop scripts
```

---

## Challenges are config-driven

Challenge types, map types, expected maps, file suffixes, and layout rules live in `config/validation_rules.yaml` and `config/settings.yaml`. You can add or change a challenge by editing those files, no code needed. `docs/configuration.md` has the full format.

The config is checked when the app starts. If something is wrong it tells you the exact spot, like `challenges.dsc.expected_maps[2]`.

Official scoring depends on data that OSIPI owns. The built-in TF6.2 integration is specific to DCE Ktrans; other official scorers get added through a custom package or a new provider hook, with the organizer's files installed locally.

### What's here today

- Config-driven challenge and map definitions for ASL/DCE/DSC.
- NIfTI readability checks and basic QC with nibabel.
- Docker execution for reproducible submissions.
- Reference comparison metrics when a matching reference is available.
- ROI/mask metrics, with configurable mask labels.
- Custom scoring packages.
- CSV, JSON, HTML, and PDF reports.

### What's waiting on OSIPI/mentor input

- The official reference maps and masks.
- The official ASL/DCE/DSC scoring definitions.
- The ICC model, accuracy definition, repeatability, and reproducibility.
- Whether BIDS compliance is required.
- Challenge-specific parameter ranges and units.

The current checks are basic NIfTI and layout checks, not full BIDS validation.

---

## Scoring setup

Start with `docs/UPDATING_SCORING.md` — it explains the four ways to update scoring in plain language. This section is the detailed version.

There are three scoring modes, set per challenge in the Scoring Setup panel on the Score step (collapsed by default). With nothing configured the app still validates, previews, and exports fine; scoring just shows "Scoring not configured." It never makes up a score.

| Mode | What it does |
|---|---|
| No scoring | Default. Skips reference scoring. Everything else still works. |
| Default OSIPI scoring package | Uses the built-in OSIPI TF6.2 DCE Ktrans scoring. Needs the official reference data (below). |
| Custom scoring package | Upload a ZIP with your own scoring script and reference data. |

### Turning on the default OSIPI package

1. Get `challengeScoring.py`, `DROKtransNifti/` (reference Ktrans maps), and `Masks/` from the OSIPI TF6.2 challenge repo.
2. Put them in `data/scoring/providers/osipi_tf62_dce_ktrans/`.
3. On the Score step, open Scoring Setup, pick "Default OSIPI scoring package," and click Apply Configuration.

### Custom scoring packages

A scoring package is a ZIP like this:

```
my_scoring_package.zip
├── manifest.json       ← required: package info
├── scoring.py          ← required: the script that runs
├── reference/          ← optional: reference NIfTI maps
├── masks/              ← optional: masks
└── README.md           ← optional
```

`manifest.json`:

```json
{
  "package_id":     "my_pkg",
  "name":           "My Scoring Package",
  "version":        "1.0.0",
  "challenge_type": "dce",
  "map_type":       "ktrans",
  "entry_point":    "scoring.py",
  "call_mode":      "standard",
  "metrics":        ["rmse", "bias"]
}
```

`call_mode`:
- `"standard"` — the script is called with `--submission-dir`, `--output-dir`, and optionally `--reference-dir`.
- `"osipi_cwd"` — the script runs with `cwd=package_dir` (the legacy TF6.2 `challengeScoring.py` style).

There are two demo packages you can practice with. Neither produces official scores:

- `data/sample_submissions/demo_scoring_package.zip` — a small DCE demo that writes placeholder metrics.
- `asl_qc_demo_scoring_package.zip` in `submissions/incoming/` — reads the real ASL maps with nibabel and computes actual QC metrics (finite %, coefficient of variation, negative fraction, and RMSE/bias only when a reference folder is present). It marks itself non-official in every output (`official_osipi_scoring: false`).

A scoring package is Python code that runs on the server, so only trusted reviewers should upload one. Look at the `scoring.py` before uploading, and never upload a package from a source you don't trust.

### Official vs. demo scoring

| | Official OSIPI scoring | Demo / QC package |
|---|---|---|
| Metrics | Official challenge metrics (accuracy, repeatability, reproducibility, OSIPI silver/gold) | QC metrics (finite %, CoV, and RMSE/bias only if a reference is supplied) |
| Reference data | OSIPI reference NIfTI maps + masks | None needed (RMSE/bias appear only if you add a reference) |
| Use | Challenge evaluation | Testing / sanity QC |
| Status | Requires OSIPI-owned data | Included in the repo, clearly marked non-official |

---

## Validation checks

Errors only stop you when the pipeline genuinely can't go on. Warnings never block.

| Check | Severity |
|-------|----------|
| Submission folder exists and isn't empty | Error |
| At least one `.nii` or `.nii.gz` file (result-only mode) | Error |
| Challenge type is in `config/validation_rules.yaml` | Error |
| NIfTI files are readable by nibabel | Error |
| Reproducible mode: a Dockerfile / run instructions present | Error (reproducible mode only) |
| README or metadata file present | Warning (Error if it was marked as included) |
| NIfTI files have at least 3 dimensions and a valid affine | Warning |
| Dockerfile present (result-only mode) | Warning |
| Code files present | Warning |
| Expected map names from `config/validation_rules.yaml` | Warning |
| NIfTI files aren't zero bytes | Warning |
| No duplicate filenames across sub-folders | Warning |
| NaN or Inf values in the data | Warning |

A stray `.gz` archive like `notes.tar.gz` doesn't count as a NIfTI file. Only `.nii` and `.nii.gz` do.

---

## Running the tests

Install the dependencies, then run pytest:

```bash
pip install -r requirements.txt -r backend/requirements.txt -r requirements-test.txt

# everything
python -m pytest

# just the validation tests (no FastAPI needed)
PYTHONPATH=src python -m pytest tests/test_validation.py tests/test_nifti_validator.py -v
```

Quick checks that don't need the Python deps:

```bash
node --check frontend/app.js          # JS syntax
node tests/frontend_smoke_test.js     # frontend smoke checks
python -m py_compile backend/main.py backend/scoring.py backend/services/*.py tests/test_api.py
```

The tests cover NIfTI validation and nibabel, ingestion and extraction (single, batch, wrapper-unwrap, `input/`+`results/` detection, stray `.gz`, zero-byte files), execution skip logic, config, all the API endpoints (upload, validate, batch validate, the exports, HTML/PDF reports), and the scoring-package endpoints.

---

## Command line

Validate a submission:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.validation.validate \
  --input submissions/extracted/dce/dce_team_alpha \
  --challenge dce
```

Ingest a ZIP:

```bash
PYTHONPATH=src python3 -m osipi_pipeline.ingestion.ingest \
  --input submissions/incoming/team_alpha.zip
```

---

## Tips

- **No scoring data?** The app validates, previews, and exports correctly. Official reference scoring is disabled until OSIPI reference data is installed under the selected provider directory.
- **Docker not running?** The Run step is disabled automatically. Validation and export still work.
- **Adding a new challenge type** (or changing expected maps): edit `config/validation_rules.yaml`. Defaults, submitted-map search paths, preview exclusions, and ingestion layout rules live in `config/settings.yaml`. See `docs/configuration.md`.
- **Changing ports**: edit the `ports` entry in `docker-compose.yml`.
- **All generated outputs** (validation JSONs, CSVs, reports, execution logs) live in `data/outputs/` on the host, mounted into the container.
