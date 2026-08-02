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
| `required_maps` | Optional. Map-type ids a submission must provide. |
| `optional_maps` | Optional. Map-type ids accepted but not required. |
| `required_artifacts` | Optional. Artifact ids (from `artifact_types`) a submission must provide. |
| `datasets` | Optional. Expected dataset structure — see below. |
| `filename_identity_patterns` | Optional. Ordered regexes used as a fallback when the directory layout does not supply identity — see below. |

### Non-map artifacts

`artifact_types` is an optional top-level section describing submitted files
that are **not** parameter maps. Each entry carries a semantic `role`, so a
4-D fitted signal is never mistaken for a 3-D parameter map.

| Field | Purpose |
|---|---|
| `role` | Required. Semantic role, for example `fitted_signal` or `methods`. |
| `suffixes` | Required. Accepted file extensions, such as `.docx` and `.txt`. |
| `patterns` | Required. Lowercase filename tokens used to detect the artifact. |
| `dimensions` | Optional. Required dimensionality, 2–7. Omit for non-image artifacts. |
| `label` | Optional. Human-facing label. |

```yaml
artifact_types:
  modelled_st:
    role: fitted_signal
    dimensions: 4
    suffixes: [.nii, .nii.gz]
    patterns: [modelled_st, modeled_st, fitted_signal]
```

### Dataset structure

`challenges.<id>.datasets` describes how many images a complete submission
should contain. Dataset names are organiser-chosen; `synthetic` and
`clinical` are simply what DCE-2026 uses.

| Field | Purpose |
|---|---|
| `participants` | Positive integer, or `null` when the count is not yet finalised. |
| `repeats` | Required positive integer. |
| `sites` | Required positive integer. |

`participants: null` means the organiser has **not decided** the cohort size.
It is deliberately distinct from a placeholder number, which would read as a
decision that has not been made. `repeats` and `sites` may not be null.

### Backward compatibility

Every field in this section is optional. A configuration that omits
`artifact_types`, `required_maps`, `optional_maps`, `required_artifacts`, and
`datasets` loads and behaves exactly as before — which is why ASL and DSC are
unaffected. `expected_maps` is unchanged and is **not** migrated or replaced
by `required_maps`; both may be present, and the accessors return them
independently.

### Filename identity patterns

Used only as a **fallback**, for fields the directory layout did not supply.
Patterns are applied to the filename stem with `.nii`/`.nii.gz` removed, in
declaration order, and the first match wins.

Only these named groups are permitted: `dataset`, `participant`, `repeat`,
`site`. Any other group, an uncompilable expression, or a pattern capturing
none of them fails configuration validation at startup rather than silently
matching nothing at upload time.

```yaml
filename_identity_patterns:
  - '^(?P<dataset>Synthetic|Clinical)_P(?P<participant>\d+)_Visit(?P<repeat>\d+)_Site(?P<site>\d+)$'
  - '^(?P<dataset>Synthetic|Clinical)_P(?P<participant>\d+)_Visit(?P<repeat>\d+)$'
```

Patterns are **not** lowercased — regexes are case-sensitive by construction.

### Normalized submission artifacts

Every discovered file becomes a `SubmissionArtifact` alongside the existing
manifest lists (which are unchanged):

| Field | Meaning |
|---|---|
| `path` | Submission-relative POSIX path. |
| `role` | `parameter_map`, `fitted_signal`, `methods`, `metadata`, `code`, `readme`, or `unknown`. |
| `map_type` | Map id — set only for `parameter_map`, otherwise `None`. |
| `artifact_type` | Configured artifact id such as `modelled_st`, otherwise `None`. |
| `challenge`, `dataset`, `participant`, `repeat`, `site` | Resolved identity; `None` when not determinable. |
| `dimensions` | NIfTI dimensionality from the header, or `None`. |

A **parameter map** is a 3-D image scored against a reference. A **fitted
signal** is the 4-D modelled S-t; it is deliberately *not* a parameter map
and carries `map_type: None`.

#### Identity precedence

1. **Directory structure** — authoritative.
2. **Configured filename patterns** — fills only the gaps.
3. Otherwise the field stays `None`.

Identity is never inferred from file ordering or neighbouring files. When
both sources supply a field and disagree, **the directory wins** and the
disagreement is recorded in `identity_conflicts`; Phase 2 does not fail an
upload over it.

Recognised directory prefixes (case-insensitive, each requiring an
identifier after it):

- **participant** — `participant001`, `sub-001`, `subject001`, `patient-004`, `p001`
- **repeat** — `repeat1`, `visit-1`, `ses-1`, `scan02`, `session-1`, plus the words `test`, `retest`, `baseline`, `followup`
- **site** — `site1`, `site-01`, `center1`, `centre1`, `scanner1`
- **dataset** — the dataset names configured for that challenge

Matching is conservative: `processed` is not a participant despite starting
with "p", and an unconfigured dataset name is left as `None` rather than
being coerced into `synthetic`.

#### Example layouts

```text
Synthetic/
  Participant001/
    Site1/
      Repeat1/
        Ktrans.nii.gz        role=parameter_map  map_type=ktrans  dimensions=3
        vp.nii.gz            role=parameter_map  map_type=vp      dimensions=3
        modelled_st.nii.gz   role=fitted_signal  map_type=None    dimensions=4
methods.docx                 role=methods        map_type=None    dimensions=None
```

```text
Clinical/
  Participant005/
    Repeat2/
      Ktrans.nii.gz          dataset=clinical participant=5 repeat=2 site=None
```

Duplicates are preserved: two files resolving to the same identity and map
type produce two artifact records. Deciding whether that is valid belongs to
a later phase, and discarding one here would hide it.

### Completeness validation

Phase 3 enforces the configuration above. A challenge that declares none of
these fields is not affected at all.

**Scan level** — one `(dataset, participant, repeat, site)` combination. Every
observed scan must carry each `required_maps` entry and each scan-level
`required_artifacts` entry (`modelled_st`).

**Submission level** — the methods document is required once for the whole
submission, regardless of scan count, and needs no scan identity.

| Situation | Result |
|---|---|
| Required map missing for a scan | `REQUIRED_MAP_MISSING` — error |
| Scan-level artifact missing | `REQUIRED_ARTIFACT_MISSING` — error |
| Methods document missing | `REQUIRED_ARTIFACT_MISSING` — error |
| Optional map absent | **nothing** — neutral, not a warning |
| Optional map present but malformed | validated exactly like a required map |
| Wrong dimensionality | `MAP_DIMENSION_MISMATCH` / `ARTIFACT_DIMENSION_MISMATCH` — error |
| Unreadable header | no dimension issue; the NIfTI validator reports it once |
| Two maps of one type in one scan | `DUPLICATE_PARAMETER_MAP` — error |
| Two `modelled_st` in one scan | `DUPLICATE_REQUIRED_ARTIFACT` — error |
| Several methods documents | `DUPLICATE_METHODS_DOCUMENT` — warning |
| Directory/filename identity disagree | `IDENTITY_CONFLICT` — error |
| Scan identity incomplete | `INCOMPLETE_ARTIFACT_IDENTITY` — error |
| Dataset not configured | `UNKNOWN_DATASET` — error |
| Wrong participant/repeat/site count | `DATASET_COUNT_MISMATCH` — error |

Counts compare **unique identifiers**, not numeric sequences: repeats
labelled 1 and 3 satisfy a count of 2. `participants: null` enforces no
total.

A dataset configured for one site (clinical) treats an absent site label as
the implicit single site — no `Site1` directory is required — but explicit
sites must stay consistent with that count.

Issues carry structured context (`dataset`, `participant`, `repeat`, `site`,
`map_type`, `artifact_type`, `expected`, `actual`) alongside the existing
`severity`/`code`/`message`/`path`, so consumers can group by scan without
parsing prose.

#### Valid DCE layout

```text
Synthetic/Participant001/Site1/Repeat1/{Ktrans,modelled_st}.nii.gz
Synthetic/Participant001/Site1/Repeat2/{Ktrans,modelled_st}.nii.gz
Synthetic/Participant001/Site2/Repeat1/{Ktrans,modelled_st}.nii.gz
Synthetic/Participant001/Site2/Repeat2/{Ktrans,modelled_st}.nii.gz
Synthetic/Participant001/Site3/Repeat1/{Ktrans,modelled_st}.nii.gz
Synthetic/Participant001/Site3/Repeat2/{Ktrans,modelled_st}.nii.gz
methods.docx
```

#### Invalid layouts

```text
Synthetic/Participant001/Site1/Repeat1/modelled_st.nii.gz
  -> REQUIRED_MAP_MISSING (no Ktrans for that scan)

Synthetic/Participant001/Site1/Repeat1/{Ktrans,Ktrans_copy}.nii.gz
  -> DUPLICATE_PARAMETER_MAP

Synthetic/Participant001/Site1/Repeat1/Ktrans.nii.gz   (4-D file)
  -> MAP_DIMENSION_MISMATCH (expected 3, found 4)

Synthetic/Participant001/Repeat1/...        (only 1 site, 3 configured)
  -> DATASET_COUNT_MISMATCH on the sites axis

Ktrans.nii.gz                                (flat, no identity)
  -> INCOMPLETE_ARTIFACT_IDENTITY
```

### Legacy compatibility

For challenges declaring `required_maps`/`optional_maps`, the legacy
`EXPECTED_MAP_MISSING` warning is **suppressed** for those map ids so the two
systems cannot contradict each other — a map marked optional must not
generate a "missing" warning. ASL and DSC declare neither field, so their
warning behaviour is unchanged.

Legacy manifest fields remain. `detected_parameter_map_id` is now produced by
the same boundary-safe classifier as `SubmissionArtifact.map_type`, so
`curve.nii.gz` no longer matches `ve` in either place.

> Completeness validation answers whether a submission is *structurally*
> valid. It computes no ROI statistics, no grouped variability, and no
> accuracy, deviance, or RSS — those arrive in later phases.

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

### ROI descriptive statistics (Phase 4)

Within-ROI statistics for submitted Ktrans maps, computed per scan.

**ROI source.** The masks already discovered by reference scoring
(`masks/` or `Masks/` under the reference root, falling back to mask-like
NIfTI files). ROI ids derive from the mask filename and labels from the
configured `mask_label_rules` — no ROI name is hardcoded in Python. If no
masks are configured, ROI statistics are reported unavailable; whole-image
statistics are **never** substituted and relabelled as ROI statistics.

**Included voxels.** Finite values only. NaN, +inf and -inf are excluded and
counted. Finite negatives and zeros are **retained** — negative Ktrans is
physically implausible but OSIPI has not declared it invalid, so discarding
it would alter a submission's statistics on our own authority. Negative and
zero counts are recorded as QC metadata.

**Formulas.**

| Statistic | Definition |
|---|---|
| `roi_median` | median of finite voxels in the ROI |
| `roi_within_scan_sd` | population SD, `sqrt(Σ(x−mean)²/N)`, i.e. `ddof=0` |
| `roi_within_scan_cov` | `SD / abs(mean)` — the **arithmetic mean**, not the median |

CoV is stored as a ratio (`0.2295`); rendering it as `22.95%` is a display
concern. When `abs(mean) <= 1e-12` the CoV is **unavailable** with reason
`mean_near_zero` — never infinity, never a clamped denominator. Median and
SD remain available in that case.

**Unavailable cases** are reported honestly, never as zero: `empty_roi`,
`no_finite_values`, `geometry_mismatch`, `mask_unreadable`,
`map_unreadable`.

**Geometry.** A mask must match the map's shape exactly. Nothing is
resampled. A mismatch yields one unavailable ROI result and does not prevent
other ROIs or other scans from computing.

**Naming.** These are *within-scan spatial* statistics for one ROI. They are
not repeatability, reproducibility, or inter-participant variability, and
the field names say so. Do not confuse them with whole-image SD, spatial
CoV, or the error SD against a reference — those remain separate fields.

> The population-SD and CoV conventions remain subject to final confirmation
> by OSIPI. This phase computes no grouped statistics, accuracy, deviance,
> or RSS.
