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
