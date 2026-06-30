# Proposal Alignment

This document compares the original project proposal ("Python Pipeline for Evaluating OSIPI Perfusion Imaging Challenge Submissions") to what is implemented in the current codebase.

---

## ✅ Implemented Now

### Submission Handling
- Upload via ZIP archive through the web UI (single and batch)
- Extraction of archives into a working directory
- Batch detection: ZIP files containing multiple sub-folders are split automatically
- Submission type auto-detection: result-only (NIfTI maps only) vs. reproducible (has Dockerfile + code)

### Validation
- File format checks: at least one `.nii` or `.nii.gz` present
- NIfTI readability via **nibabel** — confirms headers, dimensions (≥3D), and affine matrix
- NaN/Inf value detection in NIfTI data
- Zero-byte NIfTI file detection
- Expected map name checks per challenge type (DCE: Ktrans/kep/vp; ASL: CBF/ATT)
- Duplicate filename detection across sub-directories
- README/metadata file presence check
- Dockerfile and code file presence check (reproducible submissions)
- Challenge type validation (`dce`, `asl`, `dsc`)
- Errors vs. warnings distinction (errors fail the submission; warnings pass with notes)
- Clear error messages returned to the user for each failed check

### Execution (Docker)
- Docker-based isolated execution for reproducible submissions (`Dockerfile` detected)
- Docker availability check at runtime — Run step disabled gracefully when Docker is not running
- Result-only submissions skip execution automatically
- Runtime log capture and execution status tracking
- Docker-outside-of-Docker (DooD) architecture: host Docker socket is mounted into the pipeline container

### Export / Reporting
- Validation results exported as JSON
- Validation results exported as CSV
- Execution results exported as CSV (single + batch)
- Scoring results exported as CSV
- **Combined summary CSV** (`/api/export-combined`) — one row per submission with validation + execution + scoring together
- **Self-contained HTML report** (`/api/report`) — validation/execution/scoring summary, metric table, and an honest note when official scoring is not configured
- CSV/report blinded mode (strips team names and emails) for peer review
- Per-submission validation detail endpoint

### Web UI
- **7-step guided workflow**: Upload → Review Detected Submissions → Validate → Run → Score → Results Summary → Export
- **Results Summary step** (Step 6): shows validation counts (total/passed/warnings/failed), execution counts (ran/skipped/failed/cannot run), scoring metrics table, and export reminder — all in simple reviewer-friendly cards
- Execution output logic clarified in UI: result-only shows "Execution skipped — result maps were already provided", Docker run shows "Execution complete — generated output maps were collected", no-code-no-maps shows "Cannot continue — no runnable code or result maps were found"
- Batch and single submission flows both supported
- Session state persisted in `localStorage` (24-hour expiry)
- Clear next-step button at each stage
- Mobile-friendly layout

### Infrastructure
- FastAPI backend, single-container Docker deployment
- `docker compose build && docker compose up` one-command start
- Convenience start scripts: `start.sh`, `stop.sh`, `start.bat`, `start.command`
- `pyproject.toml` with pytest configuration
- Automated tests: 60+ API/service tests covering upload, validate, execute, export (validation/execution/scoring/**combined**), **HTML report**, scoring-status (GET + POST), leaderboard, all scoring package endpoints, plus proposal-coverage tests (blinded/unblinded columns, NIfTI readability, validation persistence, result-only run_readiness, reproducible runnable, honest not_configured, single-submission unwrap, no-wrapper structural detection, stray `.gz` rejection, zero-byte NIfTI, case-insensitive ASL matching)
- Frontend smoke test: 47 checks covering all 7 step panels, wizard state-holder buttons, run/score/export elements (incl. combined CSV + report buttons), Results Summary container, Run Scoring button, and tooltip presence

---

## ⚠️ Partially Implemented

### Reporting Module
**Proposal:** "Structured tables, rankings, and visualizations. Outputs as raw result files and a summary report in HTML or PDF format."

**Current state:** CSV and JSON result exports are complete, plus a **combined summary CSV** and a downloadable **HTML report** (`/api/report`). The report covers validation, execution, and scoring/QC with a metric table and an explicit note when official scoring is not configured. PDF and statistical plots are intentionally deferred (the proposal says HTML is preferred for now and warns against overbuilding charts before the CSV/workflow pass). The **Results Summary step** (Step 6 of 7) provides the same overview in-app.

### Evaluation Module
**Proposal:** "Compare submitted outputs against reference datasets. Compute RMSE, accuracy, repeatability, reproducibility."

**Current state:** A modular scoring package system is implemented (`backend/services/scoring_package_service.py`). Three modes are supported: `none` (disabled), `builtin` (OSIPI TF6.2 DCE Ktrans), and `custom` (user-uploaded ZIP package). The active mode is configured per challenge type via an admin panel in the Score step, with case-insensitive challenge-type matching (ASL/asl/Asl). The bundled **ASL QC Demo Scoring Package** computes genuine QC metrics from the real submitted NIfTI maps (finite %, CoV, negative fraction; RMSE/bias when a reference is supplied) and labels itself non-official. The official OSIPI reference data is not included (see next section). The custom-package result exposes a flat **numeric-only** metric view (`metrics`) plus the full nested structure (`metrics_detail`).

### Config / Validation Rules
**Proposal:** `config/validation_rules.yaml` and `config/settings.yaml` for rule-driven configuration.

**Current state:** Validation rules are defined as constants in `src/osipi_pipeline/validation/validate.py` (e.g., `EXPECTED_MAPS`, `KNOWN_CHALLENGE_TYPES`). No YAML config files are present. Changing challenge types or map names requires editing the Python source.

---

## 🔑 Depends on Official OSIPI Scoring / Reference Data

### Scoring Step
**Proposal:** "Integrate existing challenge scoring logic. Compute RMSE, accuracy, repeatability, reproducibility. Validate scoring correctness using sample submissions."

**Current state:** The pipeline **never fabricates scores**. The Score step defaults to "Scoring not configured" until a scoring mode is actively selected. To enable official scoring:

1. Obtain `challengeScoring.py`, `DROKtransNifti/`, and `Masks/` from the OSIPI TF6.2 challenge repository.
2. Place them in `data/scoring/providers/osipi_tf62_dce_ktrans/`.
3. In the Score step, expand Scoring Setup, select "Default OSIPI scoring package", and click Apply.

To test the pipeline end-to-end without official data: upload `data/sample_submissions/demo_scoring_package.zip` via the Custom scoring package option. The demo package returns clearly-labeled synthetic metrics — not real scientific scores.

> ⚠ **Security:** Only trusted reviewers/admins should upload custom scoring packages. A scoring package is Python code that runs on the server.

### Leaderboard and Rankings
The `/leaderboard` and `/rankings/{challenge}` endpoints return ranked results. These are populated only once scoring has run against at least one submission with reference data present.

### Sample Submissions
**Proposal:** `data/sample_submissions/` with example datasets for testing.

**Current state:** The directory now includes `demo_scoring_package/` and `demo_scoring_package.zip` — a complete working scoring package for pipeline testing (synthetic metrics, no real imaging data needed). Actual sample NIfTI submissions are not included; they require OSIPI-owned imaging data.

---

## 🔮 Future Work

### PDF Summary Report
The HTML report is implemented (`GET /api/report`). A PDF rendering (e.g. WeasyPrint over the same HTML) is deferred per the proposal's "HTML preferred over PDF for now".

### Visualization / Plots
The proposal includes performance visualizations (scatter plots, bar charts of RMSE by team). The report and CSVs carry the data; chart rendering (Chart.js in the HTML report, or Matplotlib/Plotly over `data/outputs/` CSVs) is the next increment. Deliberately deferred until the CSV/workflow path is solid, as the proposal advised.

### YAML-Driven Validation Rules
Moving `EXPECTED_MAPS` and `KNOWN_CHALLENGE_TYPES` into `config/validation_rules.yaml` would let challenge organisers add new challenge types without touching Python source code.

### DSC Challenge Support
DSC is listed in `KNOWN_CHALLENGE_TYPES` but no expected map names are defined for it. The proposal treats DSC as a first-class challenge type.

### Security Hardening
The proposal flags "running user-submitted code introduces security risks" as a known risk. Current mitigation: Docker isolation. Additional hardening (resource limits, network isolation for submission containers, seccomp profiles) is future work.

### Scalability
The proposal notes "large perfusion imaging datasets can result in increased processing time and higher memory usage." The current pipeline runs serially. A task queue (e.g., Celery + Redis) would support concurrent submissions.

### Compatibility Testing
The proposal mitigation plan includes "test early and continuously with sample datasets." Full end-to-end testing with real OSIPI submissions has not been performed due to the absence of reference data in this repository.

---

## Summary Table

| Proposal Requirement | Status |
|---|---|
| Accept ZIP archives / submission folders | ✅ Implemented |
| Validate NIfTI format (nibabel) | ✅ Implemented |
| Validate file structure and naming conventions | ✅ Implemented |
| Clear error messages and feedback | ✅ Implemented |
| Docker-based isolated execution | ✅ Implemented |
| Capture logs and execution status | ✅ Implemented |
| Skip execution for result-only submissions | ✅ Implemented — ASL `results/maps/` treated as execution output; run step shows "Execution skipped — result maps already provided" |
| CSV / JSON result exports | ✅ Implemented |
| Blinded CSV export for peer review | ✅ Implemented |
| Web UI with guided workflow | ✅ Implemented — 7-step wizard: Upload → Index → Validate → Run → Score → Results Summary → Export |
| Results Summary step | ✅ Implemented — validation/execution/scoring cards with metrics table |
| Batch submission handling | ✅ Implemented |
| ASL result-only workflow | ✅ Implemented — structural `input/`+`results/maps/` ZIP detected as single submission |
| Scoring provider framework | ✅ Implemented — modular scoring package system with none/builtin/custom modes; case-insensitive challenge matching |
| ASL demo/QC scoring package | ✅ Implemented — real QC metrics from the submitted maps, scored without Docker (NOT official OSIPI scoring) |
| Numeric-only metrics table | ✅ Implemented — string metadata (e.g. package name) excluded from metric columns |
| Combined summary CSV | ✅ Implemented — `/api/export-combined` |
| Honest not_configured status | ✅ Implemented — scoring never fakes results; defaults to "Scoring is not configured" |
| RMSE / accuracy / repeatability metrics | 🔑 Needs official OSIPI reference data |
| Official builtin DCE Ktrans scoring | 🔑 Needs OSIPI TF6.2 `challengeScoring.py` + `DROKtransNifti/` + `Masks/` |
| Leaderboard / rankings | 🔑 Needs OSIPI reference data |
| HTML summary report | ✅ Implemented — `/api/report` (blinded by default) |
| PDF summary report | 🔮 Future work (HTML preferred for now per proposal) |
| Visualizations / plots | 🔮 Future work (data available in CSV/report) |
| YAML config for validation rules | 🔮 Future work |
| Sample submissions in repo | ⚠️ Partial — demo scoring package included; real NIfTI submissions need OSIPI-owned data |
| DSC challenge map definitions | 🔮 Future work |
