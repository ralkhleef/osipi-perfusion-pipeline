# Proposal Alignment

This document compares the proposal, "Python Pipeline for Evaluating OSIPI Perfusion Imaging Challenge Submissions," with the current implementation.

---

## Implemented

### Submission Handling
- ZIP upload through the web UI, including single-submission and batch archives.
- Zenodo and GitHub import paths.
- Archive extraction into controlled working directories.
- Batch detection and single-wrapper unwrapping.
- Structural ASL-style layouts such as `input/` plus `results/maps/` are treated as one submission, not split into separate pseudo-submissions.
- Submission mode detection for result-only submissions versus reproducible Docker submissions.

### Config-Driven Rules
- `config/validation_rules.yaml` defines challenge types, expected maps, file suffixes, metadata/readme names, code indicators, and map-type detection patterns.
- `config/settings.yaml` defines defaults, upload/extraction limits, and reporting behavior.
- Configuration is validated on first load, including required sections, unknown keys where safe to reject, duplicate identifiers, referenced map ids, default challenge/map consistency, list types, numeric limits, relative paths, and precise YAML-path error messages.
- Backend validation, ingestion, scoring summaries, PDF/HTML reports, and frontend map controls read from the shared config loader instead of fixed challenge/map lists.
- Submitted-map search paths, private preview exclusions, mask-name patterns, ingestion structural directories, ZIP skip rules, and file suffix/code/README indicators are YAML-backed.
- Mask display aliases for common ROI names are YAML-backed, while arbitrary mask filenames are still accepted with cleaned filename-derived labels.
- `/api/config` exposes the configured defaults and rules needed by the UI.

### Validation
- NIfTI presence checks using configured suffixes.
- NIfTI readability checks through nibabel, including headers, dimensions, affine metadata, finite values, zero-byte files, NaN, and Inf detection.
- Expected map-name checks per configured challenge type.
- README/metadata presence checks.
- Dockerfile/code/run-instruction checks for reproducible submissions.
- Duplicate filename detection across subdirectories.
- Clear errors versus warnings, with errors blocking only when the pipeline cannot proceed.

### Execution
- Docker-based isolated execution for reproducible submissions.
- Docker availability detection, with graceful UI behavior when Docker is unavailable.
- Result-only submissions skip execution and use submitted maps directly.
- Runtime logs, execution metadata, and generated-map validation are persisted.

### Evaluation And Scoring
- Modular scoring-provider framework with `none`, `builtin`, and `custom` modes.
- Built-in TF6.2 DCE Ktrans provider hook, requiring OSIPI-owned scoring code/reference maps/masks to be installed locally.
- Trusted custom scoring packages via ZIP manifest and entry-point script.
- Numeric metrics are flattened for UI tables while full nested metric details remain available.
- NIfTI QC analysis is performed from submitted/generated maps even when official reference scoring is not configured.
- Reference-based metrics such as RMSE, MAE, bias, correlation, and finite overlap are computed when reference maps are available.
- ROI/mask metrics are computed when masks are present, without assuming any specific required mask set.

### Reporting And Export
- Raw validation JSON and CSV exports.
- Execution CSV exports.
- Scoring CSV exports.
- Combined blinded and unblinded CSV exports with one row per submission.
- HTML report (`GET /api/report`) with validation, execution, QC/scoring summaries, per-submission rows, technical details, and explicit reference-availability status.
- PDF report (`GET /api/export/report/pdf`) generated from the same report model.
- PDF reports include export date, submission metadata, challenge name, QC summary, scoring summary, and cached map previews when previews have been generated.
- Aggregate percentages are voxel-weighted rather than averaged per submission.
- Blinded reports strip team/contact fields; unblinded reports include team/contact fields.

### Web UI
- Six-step guided workflow:

```text
Upload -> Index -> Validate -> Run -> Score & Preview -> Export
```

- Score & Preview combines scoring/QC status, ranked/scored rows, key metrics, NIfTI previews, and technical details.
- Export presents the four reviewer-facing deliverables: HTML report, PDF report, blinded CSV, and unblinded CSV.
- Session restore and Start New flows clear persisted state cleanly without saving a blank restored session.
- Mobile-friendly single-card workflow layout.

### Infrastructure And Tests
- FastAPI backend and static frontend in a Docker Compose local deployment.
- CLI modules under `src/osipi_pipeline/`.
- Start/stop scripts for macOS/Linux and Windows.
- Documentation for adding challenge configurations, the YAML format, and reference/custom scoring in `docs/configuration.md`.
- Automated tests cover DCE/DSC config-driven validation, invalid submissions, missing maps, a fictional config-only PET perfusion challenge, custom scoring-package activation, reference/mask scoring edge cases, and PDF preview export.
- Frontend smoke checks verify challenge controls are populated from `/api/config` and do not duplicate ASL/DCE/DSC fallback options in static HTML.
- Syntax checks cover backend modules and `frontend/app.js`.

---

## Data-Dependent

These proposal items are implemented as framework behavior but require OSIPI-owned data or organiser-supplied reference assets to produce official scientific results.

| Requirement | Current state |
|---|---|
| Official OSIPI references and masks | Not bundled; must be provided by mentors/OSIPI organisers. |
| Official ASL/DCE/DSC scoring definitions | Framework exists; official definitions and scripts still need confirmation. |
| Official DCE Ktrans scoring | Provider hook is implemented; requires OSIPI TF6.2 `challengeScoring.py`, `DROKtransNifti/`, and `Masks/`. |
| ICC model, accuracy, repeatability, reproducibility | Generic metric plumbing exists where applicable; official formulas/models require mentor confirmation. |
| Accepted scientific test outputs | Not bundled; needed for end-to-end official validation. |
| Required BIDS compliance level | Not defined in repository. Current checks are basic NIfTI/layout checks, not full BIDS compliance. |
| Challenge-specific parameter ranges and units | Config supports labels/units; official ranges and unit rules require confirmation. |
| Leaderboard/rankings | Endpoints and UI support ranked results once scored submissions with reference metrics exist. |
| Full sample imaging submissions | Demo packages and test fixtures exist; real OSIPI imaging data is not redistributed in this repository. |

The pipeline intentionally never fabricates official scores. When references are missing, it reports QC metrics and clearly labels reference scoring as unavailable.

---

## Remaining Work

| Area | Notes |
|---|---|
| Statistical visualizations | Reports contain tables and technical details. Proposal-style plots such as RMSE bar charts or scatter plots can be added once official scored datasets are available. |
| Production security | Docker isolation is implemented. Production deployment should add authentication for scoring-package endpoints, tighter container resource limits, network restrictions, and seccomp/AppArmor profiles. |
| Scalability | Current execution is serial. A task queue such as Celery/RQ plus worker containers would support concurrent submissions. |
| End-to-end official validation | Needs organiser-provided OSIPI submissions, reference maps, masks, and accepted scoring outputs. |

---

## Summary Table

| Proposal Requirement | Status |
|---|---|
| Accept ZIP archives / submission folders | Implemented |
| Batch submission handling | Implemented |
| Validate NIfTI format with nibabel | Implemented |
| Validate structure and naming conventions | Implemented |
| Full BIDS compliance | Data-dependent; required level needs mentor confirmation |
| Rule-driven validation config | Implemented via `config/validation_rules.yaml` |
| Pipeline settings config | Implemented via `config/settings.yaml` |
| Clear error messages and feedback | Implemented |
| Docker-based isolated execution | Implemented |
| Capture logs and execution status | Implemented |
| Skip execution for result-only submissions | Implemented |
| Compare outputs with references | Implemented when reference maps are installed |
| CSV / JSON result exports | Implemented |
| Blinded exports | Implemented |
| HTML report | Implemented |
| PDF report | Implemented |
| Web UI guided workflow | Implemented as 6-step app |
| Rankings/leaderboard | Implemented, data-dependent |
| Official OSIPI scores | Data-dependent; official assets are not bundled |
| Visual plots | Remaining work |
| Production hardening | Remaining work |
