# GSoC 2026 Work Product

**Project:** Python Pipeline for Evaluating OSIPI Perfusion Imaging Challenge Submissions

**Contributor:** Ranya Al-khleef

**Organisation:** OSIPI, ISMRM Perfusion Study Group

**Mentors:** Lena Václavů, Olivia Jones, and Puneet Kumar

**Repository:** https://github.com/ralkhleef/osipi-perfusion-pipeline

**Documentation:** https://ralkhleef.github.io/osipi-perfusion-pipeline/

## Project goal

OSIPI challenges compare perfusion MRI analysis methods. I built a local web
application that helps organisers review submissions in one repeatable workflow:

**Upload → Review → Validate → Run → QC & Preview → Export**

The app accepts completed result maps or submissions containing code. It keeps
submission data, private reference maps, and masks on the reviewer's machine.

## What I built

- Import from a ZIP, folder/files, a public GitHub repository, or a Zenodo record.
- Detect single and batch submissions and index their files.
- Configure DCE, ASL, and DSC map and dataset requirements.
- Validate NIfTI files, required maps, artifacts, dimensions, and dataset structure.
- Run participant code in Docker when needed. Result-only submissions skip execution.
- Create map QC and previews for readable maps.
- Calculate configurable ASL CBF/ATT and DCE Ktrans ROI descriptive statistics
  when compatible masks are present.
- Compare submitted maps with compatible reference maps using bias, MAE, RMSE,
  correlation, error spread, overlap count, and difference NIfTI files.
- Calculate raw DCE signal RSS and summaries when measured and modelled 4-D
  signals are available.
- Support built-in and trusted custom analysis packages without treating them as
  official by default.
- Export blinded PDF, HTML, CSV, and JSON results, ROI statistics, and an
  organiser-only unblinded CSV.
- Manage challenge rules through tested, versioned configurations. Saving a
  version does not activate it automatically.

## Current state

The six-step workflow works locally for DCE, ASL, and DSC submissions. QC and
previews are available for readable maps. ROI statistics, reference comparison,
RSS, and provider analysis appear only when their required inputs are available.

Official OSIPI challenge ranking is not currently configured. Missing scientific
definitions are reported as unavailable instead of being replaced with guessed
values.

## Work still to be confirmed

The following items need decisions or private challenge data from the OSIPI team:

- the final definition of DCE accuracy and deviance;
- whether RSS should be normalised;
- the repeatability and reproducibility method, including any ICC model;
- pass/fail thresholds and participant ranking rules;
- final private reference maps and ROI masks;
- the final ASL fitted-model comparison; and
- final ASL and DSC participant, repeat, and site grids.

BIDS validation is not implemented. Current validation checks NIfTI readability,
dimensions, finite values, required content, and configured dataset structure.

See [Scientific requirements pending](SCIENTIFIC_REQUIREMENTS_PENDING.md) for the
full handoff list.

## Testing

The project has automated Python and frontend tests for ingestion, validation,
execution, configuration, analysis, reports, exports, and blinding. I also ran
real DCE and ASL examples through the rebuilt Docker application.

Manual end-to-end testing found problems that unit tests had missed, including
batch splitting, lost top-level files, unsafe submission lookup, duplicate masks
on case-insensitive filesystems, and identity leakage in blinded reports. These
cases now have regression tests.

## Main code areas

| Path | Purpose |
|---|---|
| `config/validation_rules.yaml` | Challenge rules and enabled built-in analysis |
| `src/osipi_pipeline/` | Configuration, ingestion, validation, and analysis library |
| `backend/` | FastAPI routes, services, reports, and exports |
| `frontend/` | Six-step web interface and Configuration Manager |
| `tests/` | Python and frontend regression tests |
| `docs/` | GitHub Pages documentation |
| `CODE_WALKTHROUGH.md` | Maintainer guide to the current code |

## Significant commits

| Commit | Work |
|---|---|
| `931273a` | Configurable challenge checks and identity resolution |
| `ce5a874` | ROI statistics and report alignment |
| `f563acb` | Validation and submission status display |
| `dab924e` | GitHub Pages documentation |
| `d529724` | Documentation design and private-data protection |

## What I learned

- Dataset folders cannot be used as batch boundaries without checking the
  configured challenge structure.
- Blinding must be tested across the whole report, including paths and metadata.
- Undefined scientific metrics should stay unavailable until the challenge team
  confirms their formulas.
- Configuration changes need validation, version history, and explicit activation.

## Acknowledgements

Thank you to Lena Václavů, Olivia Jones, and Puneet Kumar for their guidance and
for reviewing the scientific requirements throughout the project.
