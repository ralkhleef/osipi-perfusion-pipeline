# Configuration Guide

This app separates challenge structure from scientific analysis. YAML defines requirements such as maps, artifacts, dimensions, dataset structure, aliases, and filename rules. New or revised scientific formulas belong in tested, versioned analysis/scoring code or a trusted custom package rather than being embedded in YAML. Generic reference comparison is built into the analysis pipeline; the legacy TF6.2 provider hook and trusted custom packages are separate provider-specific mechanisms.

Configuration is validated when first loaded and whenever **Reload rules** is used. Invalid YAML, duplicate ids, missing required fields, unsafe relative paths, bad numeric limits, unknown expected-map references, and default challenge/map mismatches are rejected with exact YAML paths such as `challenges.example.expected_maps[0]`; the previously valid rules remain active after a failed reload.

> **Designed for future challenge updates.** Organisers can revise structural rules, install a new versioned analysis package, and add private assets later without changing an existing validated configuration until the replacement is ready.

## In-app Configuration Manager

Routine organiser updates do not require VS Code, a terminal, or direct YAML
editing. Open **QC & Preview → Reviewer / Admin: Scoring Setup → Challenge
Configuration Manager**, select DCE, ASL or DSC, and edit map requirements,
dimensions, filename aliases, required documents/outputs, dataset counts, code
execution policy, reference-dataset version, and provider-specific scoring
selection.

Use the guarded sequence:

1. **Test Configuration** checks schema, challenge/map definitions, filename
   aliases, the selected scorer, and readable local assets without changing
   active state.
2. **Preview Changes** shows field-by-field before/after values.
3. **Save as New Version** creates an inactive immutable version under ignored
   local data.
4. **Activate / Restore** re-tests the saved version, then atomically updates
   active rules and scoring selection. A failure restores the previous state.

The manager shows private reference maps, masks and measured signals stored
locally under ignored `data/reference_data/` paths. Configuration ZIP exports
exclude those files, package code and other private organiser data; imports
always create an inactive version. It also presents a capability matrix so
generic QC/reference analysis, provider-specific analysis, pending scientific
definitions and official ranking are not conflated.

Every PDF, HTML and JSON result includes analysis provenance: challenge,
active challenge-configuration version, scoring package, pipeline version,
reference-dataset version and analysis date. Set a stable
`reference_dataset_version` when organiser assets are released internally;
reports store the label, not private paths.

For future scoring-package development, copy
`examples/scoring-package-template/`. Its deterministic file-count example
demonstrates the package contract but is not a scientific score.

## 1. Changing Challenge Requirements

1. Add every parameter map type used by the challenge under `map_types` in `config/validation_rules.yaml`.
2. Add the challenge under `challenges` in `config/validation_rules.yaml`.
3. Set `required_maps` for blocking requirements and `optional_maps` for accepted
   non-blocking maps. `expected_maps` is retained as a legacy warning list and
   is not a replacement for those fields.
4. Set `keywords` to terms that should identify this challenge during ingestion.
5. If needed, update `config/settings.yaml`:
   - `defaults.challenge_type` and `defaults.scoring_map_type`
   - `paths.output_map_subdirs` if submitted maps live outside `results/maps/`, `results/`, or the extracted root
   - `ingestion.structural_subdirs` if a single-submission ZIP uses additional internal folder names
   - `paths.private_path_parts` and `paths.mask_name_patterns` if reference or mask files use different folder/name conventions
   - `paths.mask_label_rules` if common mask filename aliases should display as reviewer-friendly labels
6. For challenge-specific scoring, upload a custom scoring package whose `manifest.json` has a matching `challenge_type`.
7. Press **Reload rules** in Scoring Setup after editing mounted YAML. The UI re-reads the files and replaces the cached rules only when the new configuration is valid. If the container uses configuration baked into its image instead of a mounted `config/` directory, rebuild the image first.

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

With that change, the UI challenge controls, `/api/config`, validation expected-map checks, preview map labels, combined CSV mean columns, and report summaries can all use the new challenge without code edits. A new scientific formula still requires an implemented and tested analysis/scoring module or package.

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
| `code_execution_required` | Optional boolean. When true, validation requires an executor-supported Dockerfile rather than accepting a result-map-only submission. Defaults to false. |
| `reference_dataset_version` | Optional provenance label for the organiser reference dataset; it does not expose private file paths. |

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
  measured_st:
    role: measured_signal
    dimensions: 4
    suffixes: [.nii, .nii.gz]
    patterns: [measured_st, measured_signal, observed_st]
```

`modelled_st` remains required for DCE. `measured_st` is optional: when one
measured and one modelled 4-D signal can be matched to the same scan, the
pipeline computes raw voxelwise Residual Sum of Squares (RSS) across time and
summarizes it for the whole image and compatible ROIs. RSS is not deviance.

### Provisional grouped ROI descriptions

```yaml
grouped_statistics:
  enabled: true
  axes: [inter_repeat, inter_site, inter_participant]
  source: roi_median
  minimum_group_size: 2
```

This DCE setting groups scan-level ROI medians and reports mean, population SD
and CoV. Exactly two clearly matched repeats or sites also receive a signed
second-minus-first difference. These are descriptive prototype results, not
formal repeatability, reproducibility, ICC, pass/fail, or ranking.

### Dataset structure

`challenges.<id>.datasets` describes how many images a complete submission
should contain. Dataset names are organiser-chosen; `synthetic` and
`clinical` are simply what DCE-2026 uses.

| Field | Purpose |
|---|---|
| `participants` | Positive integer, or `null` when the count is not yet finalised. |
| `repeats` | Positive integer, or `null` while the count is awaiting confirmation. |
| `sites` | Positive integer, or `null` while the count is awaiting confirmation. |

Any `null` count means the organiser has **not decided** that part of the grid.
It is deliberately distinct from a placeholder number, which would read as a
decision that has not been made.

### Backward compatibility

Every field in this section is optional for a newly defined challenge. A
configuration that omits `artifact_types`, `required_maps`, `optional_maps`,
`required_artifacts`, and `datasets` retains the legacy expected-map behavior.
The current built-in rules do not rely on that fallback: ASL requires CBF and
ATT, while DSC requires CBV, CBF and MTT. `expected_maps` remains separate
from `required_maps`; both may be present, and the accessors return them
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
generate a "missing" warning. ASL requires CBF and ATT; DSC requires CBV, CBF
and MTT. Missing required maps are blocking `REQUIRED_MAP_MISSING` errors.

Legacy manifest fields remain. `detected_parameter_map_id` is now produced by
the same boundary-safe classifier as `SubmissionArtifact.map_type`, so
`curve.nii.gz` no longer matches `ve` in either place.

> Completeness validation answers whether a submission is *structurally*
> valid. Scientific analysis is separate: compatible ROI statistics and
> generic reference comparisons run when their inputs exist. DCE can also
> compute raw RSS when measured and modelled 4-D signals can be matched.

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

## 2. Adding or Updating Analysis and Scoring

The app has three scoring modes per challenge type:

| Mode | Behavior |
|---|---|
| `none` | Default. Disables provider/official scoring. Validation, generic QC, previews, exports, compatible DCE Ktrans ROI descriptive statistics, and compatible generic reference comparisons still work. |
| `builtin` | Uses the built-in legacy OSIPI TF6.2 DCE Ktrans provider hook. It produces approved provider output only when its required organiser-owned assets are installed and configured. |
| `custom` | Runs an uploaded trusted scoring package with a `manifest.json` whose `challenge_type` matches the configured challenge id. |

Generic reference comparison is independent of those provider modes. It compares submitted/generated NIfTI maps with compatible reference maps when they exist, including while mode is `none`. It searches:

- `reference/` inside the extracted submission
- `data/reference_data/`
- `data/reference_data/reference/`
- `data/reference_data/<challenge>/`
- `data/scoring/reference/`
- `data/scoring/<challenge>/reference/`
- the active custom scoring package's `reference/` folder

Masks are read from `masks/`, `Masks/`, or files matching `paths.mask_name_patterns`. Whole-map metrics and mask-level metrics include RMSE, MAE, bias, correlation, finite overlap, and related QC fields when the data can be compared.

The built-in legacy TF6.2 provider is specific to the DCE Ktrans challenge assets listed in the README. Trusted custom scoring packages are a separate extension point. Other official scorers require approved scripts, references, masks, metric definitions, and accepted outputs.

QC and previews remain available for readable maps. DCE Ktrans ROI descriptive statistics can appear when compatible ROI masks are available, generic reference comparisons when compatible reference maps are available, and provider-specific analysis or scoring when configured with its required assets. The pipeline never relabels generic comparison metrics as official scores, and official OSIPI challenge ranking is not currently configured. Generic reference metrics are not a substitute for official OSIPI accuracy, ICC, repeatability, or reproducibility definitions unless those definitions are supplied.

### Trusted custom scoring packages

Custom packages are ZIP files installed by trusted reviewers/admins:

```text
my_scoring_package.zip
├── manifest.json
├── scoring.py
├── requirements.txt
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
  "required_inputs": ["tmax"],
  "required_assets": ["reference/tmax.nii.gz"],
  "entry_point": "scoring.py",
  "requirements_file": "requirements.txt",
  "call_mode": "standard",
  "metrics": ["rmse", "bias"]
}
```

`call_mode` values:

| Value | Behavior |
|---|---|
| `standard` | Runs `python scoring.py --submission-dir <dir> --output-dir <dir> [--reference-dir <dir>]`. |
| `osipi_cwd` | Runs the entry point with `cwd` set to the package directory for legacy scripts that expect fixed relative paths. |

The scoring script must write `metrics.json` or `results.json` to the output directory and produce every metric declared in the manifest. Numeric metrics are flattened for UI tables and exports; nested details are preserved for technical review. A declared `requirements_file` is checked for presence but is not installed automatically; a trusted organiser must make those dependencies available in the application environment.

Before installation, the app validates the manifest, known challenge id, version, declared inputs, metric names, safe paths, entry-point presence, required assets, optional requirements file, and Python syntax/importability of the scorer in an isolated subprocess. Extraction happens in a staging directory. A failed package never replaces an installed package.

An already-active legacy package can continue running, but its manifest must be updated to declare `required_inputs` before it can be uploaded or activated again under the stricter contract.

Use a distinct versioned `package_id` for each release, for example `dce_accuracy_v1_0` and `dce_accuracy_v1_1`. Activation is a separate action in **Scoring Setup**. The app checks that the selected package is ready and belongs to the selected challenge, then records its `package_id`, name, and version. Results also record the package version. If activation fails, the previous active configuration remains unchanged.

Unimplemented future analyses are reported as unavailable or not configured. They do not prevent validation, QC, previews, exports, compatible ROI statistics, or compatible generic reference comparisons.

## 3. Adding Private Reference Data

Reference maps, ROI masks, answer keys, and unreleased challenge data are organiser-owned inputs. Keep source copies outside version control or under the gitignored `private_scoring_assets/` workspace. Generic comparison assets may be placed under the gitignored `data/reference_data/` paths. Package-specific assets should be copied into the package's declared relative paths before the trusted ZIP is uploaded; installed packages live under gitignored `data/scoring/packages/`.

Do not add private data to GitHub. The repository ignores common medical-image formats and the private/reference/scoring data directories, but organisers should still inspect `git status` before committing.

## 4. Safe Updates

- For mounted YAML, edit `config/validation_rules.yaml` or `config/settings.yaml`, open **Scoring Setup**, and press **Reload rules**. Invalid rules are rejected and the previously valid in-memory rules stay active. A rebuild is needed only when the container uses configuration baked into its image rather than the Compose `./config` mount.
- For scientific analysis/scoring, upload a new versioned trusted package, review its validation status, and activate it for the matching challenge. Installation and active-config writes are transactional; a failed validation or activation leaves the previous package/configuration in place.
- For private assets, copy them locally into the configured gitignored directory or package and validate readiness before activation.

Generic reference comparison remains separate from provider-specific or official scoring. Mode `none` disables provider/official scoring only. Official OSIPI challenge ranking is not currently configured.

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
> by OSIPI. DCE currently enables provisional descriptive grouping of
> scan-level ROI medians. This is not formal repeatability, ICC, accuracy,
> deviance, pass/fail, or ranking. Raw RSS is conditional on a matched measured
> and modelled 4-D signal and is intentionally not labelled deviance.
