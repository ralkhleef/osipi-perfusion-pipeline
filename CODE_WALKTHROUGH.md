# OSIPI Perfusion Pipeline Code Walkthrough

This walkthrough follows the current application from configuration through
export. It is meant for maintainers who want to verify where behavior comes
from, which parts are configurable, and which scientific decisions are still
deliberately pending.

## 1. Architecture at a glance

```text
Browser UI (frontend/)
        |
        v
FastAPI routes (backend/main.py)
        |
        +-- ingestion and validation services
        +-- optional Docker execution
        +-- map QC, ROI and reference analysis
        +-- optional provider/package analysis
        +-- shared report model -> HTML / PDF
        +-- CSV / JSON exports
        |
        v
Local ignored data (data/) + mounted rules (config/)
```

The application is local-first. Uploaded submissions, private references,
masks, scoring packages, generated results, and saved configuration versions
remain under ignored local data paths.

## 2. Configuration sources of truth

### `config/validation_rules.yaml`

This file owns challenge-facing structure:

- challenge ids, names and detection keywords;
- map labels, units, dimensions and filename aliases;
- required and optional maps;
- required non-map artifacts;
- dataset/participant/repeat/site expectations;
- filename identity patterns;
- built-in analysis enablement and input mappings;
- grouped descriptive-analysis settings; and
- reference-dataset provenance labels.

The loader in `src/osipi_pipeline/config/rules.py` validates the schema before
the rules become active. Unknown fields, duplicate ids, invalid regular
expressions, and references to unknown map/artifact ids are rejected.

The current DCE rules enable Ktrans ROI descriptive statistics and map the RSS
analysis to `modelled_st` and `measured_st`. This removes challenge-name checks
from the analysis services while keeping the formulas in tested Python.

### `config/settings.yaml`

This file owns operational settings such as upload limits, paths, reporting
defaults, private path patterns, mask labels, and execution limits.

### Configuration Manager

The Configuration Manager calls the same validation layer as direct YAML use.
Its guarded flow is:

1. Test the draft without changing active state.
2. Preview the exact differences.
3. Save an inactive immutable version.
4. Activate or restore explicitly.

Private assets and installed scoring packages are not included in a challenge
configuration export.

## 3. Upload and ingestion

Entry points live in `backend/main.py`; extraction and import behavior lives in
`backend/services/ingest_service.py`.

The pipeline accepts a ZIP, local folder/files, a public GitHub repository, or
a Zenodo record. Ingestion creates a manifest and normalized
`SubmissionArtifact` records. Each artifact records its role, map/artifact type,
and available dataset/participant/repeat/site identity.

Challenge and map detection use configuration-derived labels and aliases. Batch
boundaries are detected without treating configured structural dataset folders
as separate submissions.

## 4. Validation

`backend/services/validation_service.py` orchestrates validation; reusable
checks live under `src/osipi_pipeline/validation/`.

Validation is separated into:

- file checks: readability, zero-byte files, NIfTI dimensions and finite data;
- challenge completeness: required maps and artifacts;
- scan identity: dataset, participant, repeat and site resolution;
- configured grid checks; and
- execution prerequisites for reproducible submissions.

Required/optional status, dimensions and expected artifact roles come from
YAML. Validation does not invent scientific pass/fail thresholds.

## 5. Run step

`backend/services/execution_service.py` and `backend/docker_runner.py` handle
participant code.

- Result-map-only submissions keep their submitted maps and skip execution.
- Reproducible submissions require a supported Dockerfile.
- Participant containers run with configured resource and timeout limits and
  no network access.
- Generated output maps return to the same validation and analysis path as
  submitted result maps.

## 6. QC and scientific analysis

### Generic map QC

Readable NIfTI maps receive finite-voxel, NaN/Inf, negative-voxel and basic
descriptive checks. QC does not require reference data and is not an official
OSIPI score.

### ROI descriptive statistics

`backend/services/roi_descriptive_service.py` reads the enabled map types from
the challenge's YAML `analysis.roi_descriptive` block. It reports per-scan,
per-ROI median, population SD, CoV and voxel counts when masks are compatible.

### Generic reference comparison

`backend/scoring.py` compares compatible submitted/reference maps for bias,
MAE, RMSE, error SD, error CoV, Pearson correlation, valid overlap, and a
difference NIfTI. Whole-map and compatible ROI results stay separate by map
type and units.

### DCE signal RSS

The same module checks `analysis.signal_rss` to identify configured measured and
modelled 4-D artifacts. It computes raw voxelwise Residual Sum of Squares across
time, then produces whole-image and compatible ROI summaries. It is not called
deviance and is not an official score.

### Provider-specific analysis

Provider analysis is separate from generic QC/reference comparison:

- `none` disables provider scoring only;
- `builtin` refers to a compatible built-in provider from the provider registry;
- `custom` runs an installed trusted package with a validated manifest.

An installed package is not automatically official. Official status comes from
provider/package metadata, not from the UI mode name. Official OSIPI challenge
ranking is not currently configured.

## 7. Reports and exports

`backend/services/pdf_report_service.py` builds the format-neutral report model.
`backend/main.py` renders the self-contained HTML report from that model, while
the PDF service renders the concise printable version.

Both formats share:

- review status and key values;
- analysis availability;
- applicable limitations;
- provider/configuration/reference provenance; and
- the same preformatted ROI and prototype-analysis records.

The presentation is intentionally different by medium:

- PDF: an executive reviewer summary and four status fields on page one,
  map/ROI results on page two, and an issues page only when review items exist;
- HTML: Submission Summary and Key Results open by default, with ROI results,
  reference comparison, additional analysis, issues/limitations and provenance
  in collapsible sections.

Long methodology explanations and full submitted-file inventories stay out of
the reviewer reports. Structured CSV/JSON exports retain the machine-readable
detail.

Unavailable values are omitted or labelled unavailable; they are not converted
to zero. Blinded exports remove team/contact/submission identity and local paths.

The Export screen exposes blinded PDF, HTML, CSV Results and JSON Results, the
ROI Ktrans Statistics CSV, and an explicit Unblinded CSV for organisers.

## 8. What is and is not hard-coded

The audit target is not “zero constants.” Scientific software needs stable,
reviewed formulas and schemas. The important boundary is whether organiser
requirements can be changed without editing unrelated application logic.

Configuration-owned:

- challenge/map/artifact definitions and aliases;
- required/optional inputs and dimensions;
- dataset structure and filename identity;
- built-in analysis enablement and input ids;
- grouped-analysis axes and minimum size;
- active scoring mode/package; and
- reference provenance labels.

Intentionally code-owned and tested:

- extraction safety and path validation;
- NIfTI arithmetic and metric formulas;
- Docker isolation;
- blinding rules;
- report rendering; and
- the built-in legacy TF6.2 provider adapter/registry entry.

Still awaiting challenge-lead decisions or private assets:

- any final definition of DCE accuracy or deviance;
- whether RSS should be normalized;
- formal repeatability/reproducibility and ICC model;
- thresholds, pass/fail and participant aggregation/ranking;
- final private reference/mask sets; and
- final ASL fitted-model comparison requirements.

## 9. Verification commands

```bash
PYTHONPATH=. .venv/bin/pytest -q
node tests/frontend_smoke_test.js
node tests/footer_logic_test.js
node tests/validation_card_dom_test.js
node tests/frontend_roi_dom_test.js
PYTHONPATH=. .venv/bin/pytest -q tests/test_documentation_accuracy.py
PYTHONPATH=. .venv/bin/pytest -q tests/test_scoring_package_template.py
PYTHONPATH=backend:src .venv/bin/python scripts/preview_reports.py
docker compose config --quiet
git diff --check
```

For report QA, render every generated PDF page to images and inspect the clean,
no-reference, mixed-batch and long-content stress scenarios. Automated text
checks do not catch clipping, poor page breaks or unreadable figure labels.
