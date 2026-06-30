# Handoff Summary

## Latest pass — submission detection, scoring display, combined export & report

This pass fixed real bugs and closed two proposal gaps. All changes verified by
direct invocation of the backend modules against the real ASL test ZIP (the
sandbox could not `pip install` FastAPI/nibabel or run Docker, so `pytest` and
`docker compose` were not executed here — see "Commands" below for what to run).

**Submission detection (`backend/services/ingest_service.py`)**

- *Bug:* a ZIP whose top level was `input/` + `results/` (no wrapper folder) was
  split into two bogus submissions (`<name>_input`, `<name>_results`). The
  structural-layout check only ran in the wrapper branch. Fixed by applying
  `_is_structural_layout()` in `detect_batch_boundaries()` too.
- *Bug:* a single-submission ZIP with one wrapper folder (e.g.
  `Lena_ASL_osipi_named/input/…`) kept a redundant nesting level, so
  `results/maps/` was not at the submission root. Added `_redundant_wrapper()`
  which promotes a single non-structural wrapper up to the submission root, so
  the ASL test ZIP now extracts to `<id>/input/` and `<id>/results/maps/`.

**Validation (`backend/services/validation_service.py`)**

- `has_result_maps` no longer treats a stray `.gz` archive as a NIfTI map — it
  now requires a real `.nii`/`.nii.gz` file located under `results/`/`maps/`.

**Scoring display (`backend/services/scoring_package_service.py`, `frontend/app.js`)**

- Custom packages (e.g. the ASL QC demo) emit nested JSON
  (`{summary:{…}, per_file:[…]}`). Added `_flatten_metrics()` so the API also
  returns a flat dict of **numeric** values (the full structure is kept under
  `metrics_detail`). The score table and Results Summary now display real QC
  numbers (file count, finite %, CoV, …) instead of nothing, and never show
  string metadata such as the package name as a metric.
- `manifest.official` is now parsed and surfaced so demo/QC packages are clearly
  flagged non-official.

**API (`backend/main.py`)**

- Added `POST /api/scoring-status` (JSON body) delegating to the existing GET
  handler — the frontend and tests use POST.
- Added `GET /api/export-combined` — one combined summary CSV per session
  (validation + execution + scoring in one row; blinded/unblinded).
- Added `GET /api/report` — a self-contained HTML evaluation report (blinded by
  default) that states honestly when official scoring is not configured.

**Docs / run scripts**

- Fixed README quick-start paths (`scripts/start/start.sh`, not `scripts/start.sh`).

---

## Earlier pass — bug fixes, summary step, modular scoring

### Bug Fixes (frontend/app.js)

**Bug 1 — Score step table broken for all batch sessions**
`_getKnownSubmissions()` treated `batchState.validationData` (a `{results:[], batch_id:...}` object) as an array and called `.length` and `.map()` directly on it. Both always fail silently. Fixed by accessing `.results` properly.

**Bug 2 — No Run button for single submissions**
The Run step had a "Run All" button only for batch mode. A single reproducible submission had no button to trigger execution. Fixed by adding a per-row `.er-run-btn` for each runnable submission in the validation table.

**Bug 3 — Score nav step not reset on resetAll()**
`resetAll()` disabled nav buttons `["index", "validate", "run", "export"]` but missed `"score"`. Fixed.

**Bug 4 — Wrong button label (frontend/index.html)**
`run-skipped-continue-btn` said "Continue to Export →" but the JS handler routes to Score step. Fixed to "Continue to Score →".

### Results Summary Step (Step 6 of 7)

A new Results Summary step was added between Score and Export, completing the proposal workflow:

**Upload → Review Detected Submissions → Validate → Run → Score → Results Summary → Export**

The step shows four simple reviewer-friendly cards:
- **Validation**: total / passed / warnings / failed counts
- **Execution**: ran / skipped (maps provided) / failed / cannot run counts, with a plain-English message explaining which path was taken
- **Scoring/QC**: scored count, failed count, and a metrics table populated from any scoring results. If the ASL QC demo package ran, CBF/ATT metric values appear here.
- **Export reminder**: static prompt to download CSV files for handoff

The `renderSummaryStep()` JS function reads from `batchState.validationData`, `_execSummaries`, and the new `_scoreCache` object (populated by `_applyScoreStatus()` when scoring completes).

### New Files
- `requirements-test.txt` — test-only pip deps (pytest, httpx, anyio)
- `tests/test_api.py` — 44+ FastAPI integration tests (including scoring package tests and proposal-coverage tests)
- `docs/PROPOSAL_ALIGNMENT.md` — proposal vs implementation comparison
- `docs/HANDOFF.md` — this file
- `backend/services/scoring_package_service.py` — modular scoring package management
- `data/sample_submissions/demo_scoring_package/` — demo scoring package (synthetic metrics, NOT official OSIPI scoring)
- `data/sample_submissions/demo_scoring_package.zip` — ready-to-upload demo package ZIP

### Modular Scoring System

The scoring step now supports three modes configured via an admin panel in the UI:

| Mode | Description |
|---|---|
| `none` | Scoring disabled (default). App shows "Scoring not configured." |
| `builtin` | OSIPI TF6.2 DCE Ktrans scoring (requires official reference data). |
| `custom` | Custom scoring package uploaded as a ZIP by a trusted reviewer/admin. |

**New API endpoints (all require admin/reviewer access in production):**

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/scoring/packages` | List installed packages |
| `POST` | `/api/scoring/packages/upload` | Install a new scoring package ZIP |
| `DELETE` | `/api/scoring/packages/{id}` | Remove an installed package |
| `GET` | `/api/scoring/active-config` | Get active scoring mode per challenge type |
| `POST` | `/api/scoring/set-active` | Set active scoring mode |

**Scoring package ZIP format:**
```
my_package.zip
├── manifest.json    ← required (package_id, challenge_type, entry_point, call_mode, metrics)
├── scoring.py       ← required entry point (or challengeScoring.py for osipi_cwd mode)
├── reference/       ← optional reference NIfTI maps
├── masks/           ← optional mask files
└── README.md        ← recommended
```

`call_mode` values: `"standard"` (CLI args) or `"osipi_cwd"` (legacy TF6.2 cwd-based).

**Security warning:** Scoring packages are Python code that run on the server. Only trusted reviewers/admins should upload packages. Always review `scoring.py` before uploading.

### Repo Cleaned
- Removed git-tracked stale outputs: 3 validation JSONs + 2 manifest files from `data/outputs/`
- Updated `.gitignore` to properly exclude all generated output subdirectories (`execution/`, `execution_results/`, `manifests/`, `validation/`) with `.gitkeep` exception patterns
- Added `*.zip` ignore (with `tests/**/*.zip` exception for test fixtures)
- Added `tests/**/*.nii` and `tests/**/*.nii.gz` exceptions so test fixtures stay tracked
- Removed duplicate entries at the bottom of `.gitignore`

Note: `__pycache__` directories and stale ZIPs/extracted folders in `submissions/` exist on disk but are now properly gitignored. To remove them from disk run:
```bash
find . -type d -name '__pycache__' | xargs rm -rf
rm -rf submissions/incoming/*.zip
find submissions/extracted -mindepth 1 -maxdepth 1 -not -name '.gitkeep' | xargs rm -rf
find data/outputs -not -name '.gitkeep' -not -type d -delete
find data/outputs -mindepth 2 -type d -not -name '.gitkeep' | xargs rm -rf
```

---

## Commands That Pass

```bash
# Syntax / static checks (no Python deps needed) — verified in this pass
python3 -m py_compile backend/main.py backend/scoring.py backend/services/*.py tests/test_api.py  # OK
node --check frontend/app.js                   # OK
node tests/frontend_smoke_test.js              # 47/47 passed

# Full test suite (requires deps installed in Docker or a venv)
pip install -r requirements.txt -r backend/requirements.txt -r requirements-test.txt
python -m pytest                               # FastAPI + service tests (incl. new combined/report/detection tests)

# Docker
docker compose build --no-cache               # builds the backend image
docker compose up -d                          # starts on http://localhost:8000
```

> Note: this pass was verified in a sandbox **without** network pip access,
> Docker, or pytest. The backend logic was instead exercised directly against
> the real ASL ZIP (ingest → validate → install ASL QC package → configure ASL
> → score → combined CSV → HTML report) using a temporary in-process harness.
> Run the `pytest`/`docker compose` commands above in your own environment to
> reproduce the full suite.

---

## Pipeline Output Definition

"Pipeline output maps" — the NIfTI files used for scoring — are resolved in priority order:

1. **Execution output directory** (`data/outputs/execution/<submission_id>/`) — maps generated by running the team's Docker container.
2. **`results/maps/` inside the submitted ZIP** — pre-computed maps submitted directly (common for ASL and result-only entries).
3. **`results/` inside the submitted ZIP** — fallback if `results/maps/` is absent.
4. **Extracted root** — final fallback if no `results/` subdirectory exists.

A submission is considered **result-only** (run step skipped) when it contains NIfTI maps in one of the above locations but has no Dockerfile or `docker-compose.yml`. The scoring step can run against result-only submissions without ever executing Docker.

This means an ASL ZIP structured as:
```
team_asl/
├── input/        ← input data (structural, not a batch boundary)
└── results/
    └── maps/     ← pre-computed CBF/ATT maps — treated as execution output
```
is ingested as a single submission and its `results/maps/` NIfTIs are used for scoring directly.

---

## Remaining Limitations

| Limitation | Notes |
|---|---|
| **No official scoring data** | Requires official OSIPI TF6.2 DCE Ktrans reference data + masks for real evaluation. Use the ASL QC demo package to test the pipeline end-to-end (QC metrics only, not official). |
| **No auth on scoring endpoints** | In production, `/api/scoring/packages/upload` and `/api/scoring/set-active` should be protected by auth middleware. Anyone with network access can currently upload a package. |
| **HTML report only (no PDF)** | `GET /api/report` produces a self-contained HTML report; PDF is intentionally deferred per the proposal ("HTML preferred over PDF for now"). |
| **Charts not yet rendered** | Combined CSV + report tables exist; bar charts of RMSE/CV are future work (the report is plot-free for now, as the proposal advised not to overbuild charts until CSV/workflow pass). |
| **DSC maps minimal** | DSC is a recognised challenge type with `EXPECTED_MAPS` entries (cbv/cbf/mtt) but no official DSC scoring provider is bundled. |
| **Serial execution** | Submissions run one at a time; no job queue for concurrency. |
| **`__pycache__` on disk** | Cannot be deleted via sandbox due to file permissions; properly gitignored. |

---

## 3-Minute Demo Instructions (real ASL submission)

**Prerequisites:** Docker Desktop running, `docker compose up -d` started, browser open at http://localhost:8000.

1. **Upload** — Enter a team name + email, choose **ASL**, then upload
   `submissions/incoming/lena_realistic_asl_osipi_named.zip`. Click **Upload and Detect**.

2. **Review Detected Submissions** — Confirm **exactly one** submission appears
   (`lena_realistic_asl_osipi_named`), not separate `_input`/`_results` rows.
   Click **Continue**.

3. **Validate** — Validate the submission. It passes with warnings only (result
   maps present, no Dockerfile). Open **Details** to see per-file NIfTI checks.
   Click **Continue**.

4. **Run** — The step shows **"Execution skipped — result maps already provided"**
   because the ZIP already contains `results/maps/`. No Docker run is needed.
   Click **Continue to Score**.

5. **Score** — Expand **Reviewer / Admin: Scoring Setup**, select **Custom
   scoring package**, upload `submissions/incoming/asl_qc_demo_scoring_package.zip`,
   pick it as the active ASL package, and **Apply Configuration**. The status
   card now reads **"Scoring is ready — ASL QC Demo Scoring Package is active."**
   Click **Run Scoring**. Real QC metrics (file count, finite %, mean CoV) appear
   — clearly labelled demo/QC, not official OSIPI scoring.

6. **Results Summary** — Review the 2×2 cards: validation, execution (skipped),
   scoring/QC metrics, export.

7. **Export** — Download the validation CSV, the **combined summary CSV**, and
   **Open HTML Report** (blinded). The report states that scoring was demo/QC,
   not official.

---

## What to Send as Handoff

The full repo at `/Users/ralkhleef/Desktop/osipi-perfusion-pipeline` minus the gitignored runtime files. From git's perspective the clean commit would include:

- All source code (`backend/`, `frontend/`, `src/`, `tests/`)
- Config files (`docker-compose.yml`, `Dockerfile`, `pyproject.toml`, `.gitignore`)
- Scripts (`scripts/`)
- Docs (`README.md`, `docs/PROPOSAL_ALIGNMENT.md`, `docs/HANDOFF.md`)
- `.gitkeep` sentinels for all runtime directories

To create a clean ZIP for sharing:
```bash
git archive --format=zip --output=osipi-pipeline-handoff.zip HEAD
```
This produces a ZIP of exactly what git tracks — no stale outputs, no `__pycache__`, no uploaded ZIPs.
