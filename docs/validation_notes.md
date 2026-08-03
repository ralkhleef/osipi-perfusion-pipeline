# Validation Notes

Validation checks whether an ingested submission can continue through the pipeline. It does not fabricate scores or require official reference data.

These are basic repository-owned NIfTI/layout checks. They should not be described as full BIDS compliance until mentors define the required BIDS level and challenge-specific metadata requirements.

## What Validation Checks

| Check | Result |
|---|---|
| Submission folder exists and has files | Error if missing or empty |
| Challenge type exists in `config/validation_rules.yaml` | Error if unknown |
| Result-only submissions contain configured NIfTI map suffixes | Error if missing |
| Reproducible submissions contain run instructions | Error if Docker/run instructions are required but missing |
| README/SOP/metadata files from `readme_names` are present | Warning unless explicitly required |
| Code indicators from `code_file_names`, `code_extensions`, or `code_folder_names` are present | Warning when expected but missing |
| Expected maps for the configured challenge are present | Warning if missing |
| NIfTI files are readable by nibabel | Error if unreadable |
| NIfTI dimensions, affine/header metadata, finite values, NaN/Inf, and zero-byte files | Error or warning depending on severity |
| Duplicate filenames across subdirectories | Warning |

Expected maps are configured under `challenges.<id>.expected_maps` in `config/validation_rules.yaml`. DCE, ASL, and DSC are just configured challenge entries; adding another challenge follows the same format.

Challenge-specific parameter ranges, unit rules, accepted outputs, and official metric thresholds are not enforced here unless they are added to config or supplied by an official scoring package.

## Structural completeness (DCE-2026)

A challenge may additionally declare what a *complete* submission looks like.
These are enforced — a missing required map or artifact is a blocking error —
and they are read from configuration, so changing the rules for a future
challenge does not require a code change.

| Key | Meaning |
|---|---|
| `required_maps` | Parameter maps every scan must provide. Missing is an error. |
| `optional_maps` | Accepted but not required. Missing is not reported. |
| `required_artifacts` | Non-map files the submission must include, e.g. the modelled signal-time curve and the methods document. |
| `artifact_types.<id>.dimensions` | Expected NIfTI dimensionality, checked against the file header. |
| `datasets.<name>` | Expected participants × repeats × sites. A `null` count means "not yet decided by OSIPI" and is not checked. |
| `filename_identity_patterns` | Fallback identity parsing, used only where the directory layout does not already supply an identifier. |

Issue codes: `REQUIRED_MAP_MISSING`, `REQUIRED_ARTIFACT_MISSING`,
`MAP_DIMENSION_MISMATCH`, `ARTIFACT_DIMENSION_MISMATCH`,
`DUPLICATE_PARAMETER_MAP`, `DUPLICATE_REQUIRED_ARTIFACT`,
`DUPLICATE_METHODS_DOCUMENT`, `INCOMPLETE_ARTIFACT_IDENTITY`,
`DATASET_COUNT_MISMATCH`, `IDENTITY_CONFLICT`, `UNKNOWN_DATASET`.

Duplicate-filename warnings are scoped by resolved scan identity, so the same
standard filename appearing once per scan directory — which the DCE-2026
layout requires — is not reported.

## Errors vs Warnings

| Type | Meaning |
|---|---|
| Error | The submission cannot safely continue until fixed. |
| Warning | The submission can continue, but reviewers should inspect the issue. |

Warnings are intentionally non-blocking because challenge submissions may have valid layout differences.

## Related Config

- `config/validation_rules.yaml`: challenge ids, expected maps, map detection patterns, file suffixes, README names, and code indicators.
- `config/settings.yaml`: defaults, safety limits, submitted-map search paths, private preview paths, mask patterns, and ingestion structural folders.
- `docs/configuration.md`: full guide for adding challenges and configuring reference/custom scoring.
