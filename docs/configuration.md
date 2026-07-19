# Configuration Guide

This app is designed so new challenge validation, map discovery, previews, exports, and custom scoring setup can be added through YAML configuration. Built-in official scoring providers are still provider-specific; new official scoring logic or organiser-owned assets must be installed separately.

Configuration is validated when first loaded. Invalid YAML, duplicate ids, missing required fields, unsafe relative paths, bad numeric limits, unknown expected-map references, and default challenge/map mismatches raise startup errors with exact YAML paths such as `challenges.example.expected_maps[0]`.

## Add a New Challenge

1. Add every parameter map type used by the challenge under `map_types` in `config/validation_rules.yaml`.
2. Add the challenge under `challenges` in `config/validation_rules.yaml`.
3. Set `expected_maps` to the map-type ids that should be present for a complete result-only submission.
4. Set `keywords` to terms that should identify this challenge during ingestion.
5. If needed, update `config/settings.yaml`:
   - `defaults.challenge_type` and `defaults.scoring_map_type`
   - `paths.output_map_subdirs` if submitted maps live outside `results/maps/`, `results/`, or the extracted root
   - `ingestion.structural_subdirs` if a single-submission ZIP uses additional internal folder names
   - `paths.private_path_parts` and `paths.mask_name_patterns` if reference or mask files use different folder/name conventions
   - `paths.mask_label_rules` if common mask filename aliases should display as reviewer-friendly labels
6. For challenge-specific scoring, upload a custom scoring package whose `manifest.json` has a matching `challenge_type`.
7. Restart the backend after editing YAML. The shared config loader is cached in-process.

Example:

```yaml
map_types:
  tmax:
    display: Tmax
    label: Time to maximum
    units: seconds
    patterns:
      - tmax
      - time_to_maximum

challenges:
  example:
    label: EXAMPLE
    description: Example perfusion challenge
    expected_maps:
      - tmax
    keywords:
      - example
      - tmax
```

With that change, the UI challenge controls, `/api/config`, validation expected-map checks, preview map labels, combined CSV mean columns, and report summaries can all use the new challenge without code edits.

## Config Format

`config/validation_rules.yaml` owns challenge and file-type validation rules.

| Field | Purpose |
|---|---|
| `version` | Config schema/version marker. |
| `default_challenge_type` | Fallback challenge id used by shared validation helpers. |
| `nifti_suffixes` | File suffixes treated as NIfTI maps. |
| `metadata_suffixes` | File suffixes treated as submission metadata. |
| `readme_names` | README/SOP/metadata filenames that satisfy README checks. |
| `code_file_names` | Exact filenames treated as code/run indicators. |
| `code_extensions` | Source-code suffixes treated as code indicators. |
| `code_folder_names` | Folder names treated as code indicators. |
| `map_types` | Map definitions used for detection, labels, units, UI, CSV, and reports. |
| `challenges` | Challenge definitions used for ingestion, validation, UI, scoring setup, and exports. |

Each `map_types.<id>` entry supports:

| Field | Purpose |
|---|---|
| `display` | Human-facing label, such as `Ktrans` or `CBF`. |
| `label` | Longer scientific label. |
| `units` | Optional display units. Leave blank if unitless or unknown. |
| `patterns` | Lowercase filename tokens/phrases used to detect the map type. |

Each `challenges.<id>` entry supports:

| Field | Purpose |
|---|---|
| `label` | Human-facing challenge label. |
| `description` | Short UI/API description. |
| `expected_maps` | Map-type ids that should be present. Missing maps are warnings, not fabricated failures. |
| `keywords` | Terms used by ingestion to guess the challenge type. |

`config/settings.yaml` owns pipeline defaults and operational rules.

| Field | Purpose |
|---|---|
| `defaults.challenge_type` | UI/API default challenge. |
| `defaults.scoring_map_type` | UI/API default scoring map display. |
| `defaults.validation_mode` | Default validation mode. |
| `limits.*` | Upload/extraction safety limits. |
| `reporting.*` | Report defaults such as blinded export and PDF availability. |
| `paths.output_map_subdirs` | Ordered submitted-map search paths under an extracted submission. |
| `paths.private_path_parts` | Folder names excluded from public previews/downloads. |
| `paths.mask_name_patterns` | Filename patterns treated as masks/private preview files. |
| `paths.mask_label_rules` | Optional display labels and filename patterns for known mask/ROI aliases. Unknown masks still get cleaned filename labels. |
| `ingestion.skip_prefixes` | ZIP path prefixes skipped during extraction. |
| `ingestion.skip_names` | Junk/system filenames skipped during extraction. |
| `ingestion.structural_subdirs` | Folder names that indicate one internal submission layout, not a batch. |

## Reference Scoring

The app has three scoring modes per challenge type:

| Mode | Behavior |
|---|---|
| `none` | Default. Validation, QC, previews, CSV, HTML, and PDF still work. Reference scores are reported as unavailable. |
| `builtin` | Uses bundled provider hooks. The current built-in official hook is OSIPI TF6.2 DCE Ktrans and requires organiser-owned assets installed locally. |
| `custom` | Runs an uploaded trusted scoring package with a `manifest.json` whose `challenge_type` matches the configured challenge id. |

Reference-based QC compares submitted/generated NIfTI maps with matching reference maps when references exist. It searches:

- `reference/` inside the extracted submission
- `data/reference_data/`
- `data/reference_data/reference/`
- `data/reference_data/<challenge>/`
- `data/scoring/reference/`
- `data/scoring/<challenge>/reference/`
- the active custom scoring package's `reference/` folder

Masks are read from `masks/`, `Masks/`, or files matching `paths.mask_name_patterns`. Whole-map metrics and mask-level metrics include RMSE, MAE, bias, correlation, finite overlap, and related QC fields when the data can be compared.

The built-in TF6.2 provider is specific to the DCE Ktrans challenge assets listed in the README. Other official scorers should be installed as trusted custom packages or implemented as new provider hooks once mentors provide the official scripts, references, masks, metric definitions, and accepted outputs.

The pipeline never invents official scores. If references, masks, or official scoring assets are missing, reports say reference scoring is not available and show QC metrics only. Generic reference metrics are not a substitute for official OSIPI accuracy, ICC, repeatability, or reproducibility definitions unless those definitions are supplied.

## Custom Scoring Packages

Custom packages are ZIP files installed by trusted reviewers/admins:

```text
my_scoring_package.zip
├── manifest.json
├── scoring.py
├── reference/
├── masks/
└── README.md
```

Minimal `manifest.json`:

```json
{
  "package_id": "example_scoring",
  "name": "Example Scoring",
  "version": "1.0.0",
  "challenge_type": "example",
  "map_type": "tmax",
  "entry_point": "scoring.py",
  "call_mode": "standard",
  "metrics": ["rmse", "bias"]
}
```

`call_mode` values:

| Value | Behavior |
|---|---|
| `standard` | Runs `python scoring.py --submission-dir <dir> --output-dir <dir> [--reference-dir <dir>]`. |
| `osipi_cwd` | Runs the entry point with `cwd` set to the package directory for legacy scripts that expect fixed relative paths. |

The scoring script should write `metrics.json` or `results.json` to the output directory. Numeric metrics are flattened for UI tables and exports; nested details are preserved for technical review.

## Verification

Useful checks after adding a challenge:

```bash
python -m pytest tests/test_api.py -q
node tests/frontend_smoke_test.js
python -m py_compile backend/main.py backend/scoring.py backend/services/*.py src/osipi_pipeline/config/*.py src/osipi_pipeline/validation/validate.py
```

For manual smoke testing, call `/api/config` and confirm the new challenge and map types appear before uploading a submission.
