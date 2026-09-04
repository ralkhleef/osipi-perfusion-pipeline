# OSIPI Perfusion Pipeline Code Walkthrough

This guide shows where the main behavior lives and which parts are configurable.

## 1. Application flow

```text
Browser UI (frontend/)
        |
        v
FastAPI API (backend/main.py)
        |
        +-- ingestion and validation
        +-- optional Docker execution
        +-- QC and analysis
        +-- reports and exports
        |
        v
Local data (data/) and challenge rules (config/)
```

The application runs locally. Submissions, private references, masks, scoring
packages, generated results, and saved configuration versions are excluded from
Git.

## 2. Configuration

### Challenge rules

`config/validation_rules.yaml` defines:

- challenge names and detection keywords;
- required and optional maps;
- map aliases, labels, units, and dimensions;
- required non-map files;
- dataset, participant, repeat, and site structure;
- filename identity patterns; and
- enabled built-in analysis and its input mappings.

`src/osipi_pipeline/config/rules.py` checks the schema before rules are used.
Invalid fields, duplicate ids, bad regular expressions, and unknown map or
artifact references are rejected.

`config/settings.yaml` contains operational settings such as paths, upload
limits, reporting defaults, private path patterns, and execution limits.

### Configuration Manager

The Configuration Manager uses the same validation code as the YAML loader. Its
safety flow is:

1. Test the draft.
2. Preview the changes.
3. Save an inactive version.
4. Activate or restore a version explicitly.

Closing an editor or saving a version does not change the active configuration.
Private assets and scoring packages are not included in configuration exports.

## 3. Upload and review

Routes are in `backend/main.py`. Import and extraction code is in
`backend/services/ingest_service.py`.

The app accepts a ZIP, folder/files, a public GitHub repository, or a Zenodo
record. Ingestion creates a manifest and normalised `SubmissionArtifact`
records. Challenge and map detection use labels and aliases from configuration.

Batch detection also checks the configured dataset structure so dataset folders
are not mistaken for separate teams.

## 4. Validation

`backend/services/validation_service.py` coordinates checks implemented under
`src/osipi_pipeline/validation/`.

Validation covers:

- file readability and zero-byte files;
- NIfTI dimensions and finite values;
- required maps and artifacts;
- dataset, participant, repeat, and site identity;
- configured dataset grids; and
- Docker prerequisites for reproducible submissions.

Required content and dimensions come from YAML. The validator does not create
scientific pass/fail thresholds.

## 5. Run

`backend/services/execution_service.py` and
`src/osipi_pipeline/execution/docker_runner.py` run participant code.

- Result-only submissions keep their maps and skip execution.
- Reproducible submissions need a supported Dockerfile.
- Containers use configured resource and time limits and run without network
  access, including a process-count cap so a fork bomb cannot exhaust the host.
- The build is bounded by its own timeout, and the image is removed after the
  run so a challenge round does not fill the disk.
- Generated maps return to the same validation and analysis path as submitted
  maps.

## 6. QC and analysis

### Map QC

Readable NIfTI maps receive finite-voxel, NaN/Inf, negative-voxel, and basic
descriptive checks. QC does not need reference data and is not an official
OSIPI score.

### ROI descriptive statistics

`backend/services/roi_descriptive_service.py` reads the enabled map types from
the challenge configuration. With compatible masks, it reports per-scan and
per-ROI median, population SD, CoV, and voxel count.

### Intraclass correlation

`src/osipi_pipeline/scoring/icc.py` implements all six Shrout & Fleiss models
with exact F-based confidence intervals, from a participants x sessions table
built out of the same per-scan ROI rows the grouping uses. The user-confirmed
`challenges.<id>.grouped_statistics.icc.models` list contains `icc2_1` and
`icc3_1` for ASL, DCE and DSC. Each result is labelled by model. A
participant missing any session is excluded and counted, never imputed.

### Generic reference comparison

`backend/scoring.py` compares compatible submitted and reference maps. It can
report bias, MAE, RMSE, error SD, error CoV, Pearson correlation, valid overlap,
and a difference NIfTI for the whole map and compatible ROIs.

### DCE signal RSS

When configured measured and modelled 4-D signals are present, the same module
calculates raw voxelwise Residual Sum of Squares across time and summarises it
for the whole image and compatible ROIs. It is not labelled deviance.

### Provider analysis

Provider analysis is separate from generic QC and reference comparison:

- `none` disables provider analysis only;
- `builtin` uses a compatible built-in provider; and
- `custom` uses an installed trusted package with a valid manifest.

The current built-in provider is the legacy TF6.2 DCE Ktrans adapter. A package
is not official simply because it is installed. Official OSIPI challenge
ranking is not currently configured.

## 7. Reports and exports

`backend/services/pdf_report_service.py` builds the shared report model. The
HTML and PDF use the same results but different layouts:

- PDF: short reviewer summary, main results, issues when present, and provenance.
- HTML: summary and key results first, with detailed sections that can be opened.

CSV and JSON keep the machine-readable detail. Missing values stay unavailable;
they are not changed to zero. Blinded outputs remove team, contact, submission,
archive, and local-path identity.

The Export screen provides blinded PDF, HTML, CSV Results, and JSON Results,
the ROI Ktrans Statistics CSV, and an unblinded CSV for organisers.

## 8. Configuration and code boundaries

Configuration owns challenge rules, required inputs, aliases, dimensions,
dataset structure, enabled built-in analysis, active provider mode, and reference
provenance labels.

Code owns extraction safety, NIfTI arithmetic, metric formulas, Docker isolation,
blinding, report rendering, and provider adapters. A new scientific formula still
needs code and tests.

The following items still need mentor decisions or private data:

- final DCE accuracy and deviance definitions;
- RSS normalisation;
- repeatability and reproducibility method and any change to the current
  repeat-based ICC grouping;
- thresholds, pass/fail, and ranking rules;
- final private references and masks; and
- final ASL fitted-model comparison.

## 9. Verification

```bash
OSIPI_REQUIRE_FULL_TESTS=1 PYTHONPATH=.:backend:src .venv/bin/pytest -q
for suite in tests/*_test.js; do node "$suite"; done
PYTHONPATH=.:backend:src .venv/bin/pytest -q tests/test_documentation_accuracy.py
PYTHONPATH=.:backend:src .venv/bin/pytest -q examples/scoring-package-template/tests/test_scorer.py
PYTHONPATH=backend:src .venv/bin/python scripts/preview_reports.py
docker compose config --quiet
git diff --check
```
