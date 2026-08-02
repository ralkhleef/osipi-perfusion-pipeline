# Changelog

Notable changes to the OSIPI perfusion submission pipeline. Newest first.

## Unreleased — DCE-2026 support, report redesign, seven production fixes

### Added

**DCE-2026 challenge support** (Phases 1–4D)

- Challenge configuration schema: `required_maps`, `optional_maps`,
  `required_artifacts`, `artifact_types`, `datasets`, and
  `filename_identity_patterns`, with strict validation and unknown-key
  rejection (`src/osipi_pipeline/config/rules.py`).
- Normalised `SubmissionArtifact` model and directory-first identity
  resolution for dataset / participant / repeat / site, with a configurable
  filename-pattern fallback. Identity is never inferred from file ordering,
  and disagreements are reported as conflicts rather than silently resolved
  (`ingestion/identity_parser.py`, `artifact_classifier.py`, `models.py`).
- Completeness validation with eleven distinct issue codes, covering missing
  maps and artifacts, dimension mismatches, duplicates, incomplete identity,
  dataset-count mismatches, and identity conflicts
  (`validation/completeness.py`).
- Within-ROI descriptive statistics for one map, one ROI, one scan: median,
  population SD (`ddof=0`) and CoV (`SD / |mean|`), with explicit
  unavailability rather than misleading zeros
  (`scoring/descriptive_statistics.py`, `services/roi_descriptive_service.py`).
  Computed once per scoring run and read by the API, report model, CSV export
  and Results Summary alike.
- ROI statistics CSV export (`/api/export-roi-descriptive`) and a Results
  Summary section wired into the canonical render path.

**Reports**

- Journal-register HTML and PDF reports sharing one branding module: serif
  typography, `booktabs` rules, small-caps sections, status as a coloured dot.
  Fonts are restricted to the PDF base-14 set so a standalone report renders
  identically offline (`services/report_branding.py`).
- Bland–Altman and identity figures rendered from one geometry description by
  both an SVG and a ReportLab backend, so the two formats cannot diverge
  (`services/report_figures.py`).
- `scripts/preview_reports.py` renders four scenarios to HTML and PDF,
  including a deliberate stress case.

**Interface**

- Issue lists, status chips and section headings restyled to match the
  reports: rules instead of fills, no pills, severity as a dot.
- `scripts/demo_evidence.py` regenerates the full DCE_Test_Clean evidence
  bundle — inputs, exports, both reports, and a checked evidence log.

### Fixed

Seven production defects, none of which the automated suite could reach.
Found by running a real DCE-2026 submission through a live server; each is
documented with reproduction in `CODE_WALKTHROUGH.md`.

- **A valid DCE submission was split into two.** `Synthetic/` and `Clinical/`
  are dataset partitions of one submission, but batch detection read them as
  separate teams. The dataset name then sat above each submission root where
  identity resolution could never see it: 41 spurious
  `INCOMPLETE_ARTIFACT_IDENTITY` errors, dataset-count validation silently
  disabled, and the team listed twice. Dataset names declared by a challenge
  are now treated as structural (§B1).
- **Files beside the batch directories were destroyed.** The carve moved only
  the contents of each batch directory and then deleted the staging area, so a
  shared methods document or README was lost and the submitter was blamed for
  its absence. Shared root files now reach every carved submission, and a
  submission's own file of the same name wins (§B2).
- **An unknown submission id returned another submission's data.** Validation
  results were matched by substring, so any id that was a prefix of a real one
  resolved to that submission — and batch carving manufactures exactly such
  prefixes. Lookup is now exact, and an id with nothing on disk is a 404 (§B3).
- **Every ROI statistic was computed twice on macOS.** `masks/` and `Masks/`
  are the same directory on a case-insensitive filesystem but compare as
  different `Path` objects, so every mask was admitted twice and every ROI row
  duplicated — indistinguishably, since ROI identity comes from the filename.
  Deduplication is now by physical file identity (§B4).
- **Blinded reports leaked the team name.** The "Affected" column rendered the
  basename of an absolute issue path, which for submission-level issues is the
  submission directory — that is, the team name. Fixed in the shared report
  model so HTML, PDF, the plain-text PDF fallback, and download filenames all
  consume one blinding decision (§B5).
- **`DUPLICATE_FILENAME` fired on every valid DCE submission.** The check keyed
  on basename alone, but the DCE layout requires the same standard names in
  every scan directory. Duplicates are now scoped by resolved scan identity
  (§B6).
- **Reference data was counted as submission content.** Reference maps and ROI
  masks staged inside the extracted submission were recorded as parameter maps
  and inflated the map count. They are now excluded from artifacts (§3.7).

### Testing

- Three new suites closing the specific blind spots that let those defects
  ship: `test_dce_submission_integrity.py` (24) routes a submission through the
  real uploader; `test_reference_dedup.py` (12) covers case-insensitive
  filesystem behaviour, simulating macOS case-folding with a symlink so it
  reproduces on Linux; `test_blinded_identity.py` (45) asserts a hostile team
  name is absent from the whole of every blinded output, not just the visible
  table cell.
- 27 mutations were run across the seven fixes; all are caught. Two initially
  escaped and both exposed a weak test rather than a weak fix.
- Suite: **618 passed, 1 skipped** Python; 1014 + 53 + 27 frontend.

### Known limitations

- Grouped statistics, accuracy, deviance and RSS remain unimplemented.
- ROI statistics are within-scan spatial variability only — not repeatability,
  reproducibility, or inter-participant variability.
- Statistical conventions remain subject to confirmation by OSIPI.
- `identity_tokens` ignores tokens under four characters, so a very short team
  name relies entirely on structural selection with no backstop.
