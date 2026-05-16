# Validation Notes

Validation checks whether an ingested submission looks ready for later pipeline steps.

It does not score results, run Docker, or inspect real image contents yet.

## What Validation v1.1 Checks

| Check | Result |
| --- | --- |
| Submission folder exists | Error if missing |
| Submission folder has files | Error if empty |
| At least one `.nii` or `.nii.gz` file exists | Error if missing |
| README or metadata file exists | Error if missing |
| Challenge type is `asl` or `dce` | Error if unknown |
| Dockerfile exists | Warning if missing |
| Code files exist | Warning if missing |
| Expected DCE maps: `Ktrans`, `kep`, `vp` | Warning if missing |
| Expected ASL maps: `CBF`, `ATT` | Warning if missing |
| NIfTI files are not empty | Warning if size is 0 |
| Filenames are unique | Warning if duplicated |

## Errors vs Warnings

| Type | Meaning |
| --- | --- |
| Error | The submission fails validation. |
| Warning | The submission can still pass, but something may need attention. |

Warnings are useful for early development because challenge submissions may not all follow the same structure yet.

## What Is Not Validated Yet

- Real NIfTI image contents
- Image dimensions, affine matrices, or headers
- BIDS structure
- Docker build or execution
- Scoring against reference data
- Report generation

## Future Ideas

- Read NIfTI headers without loading full image data
- Add challenge-specific required folder layouts
- Check metadata schemas
- Validate Dockerfile and runtime requirements
- Connect validation results to scoring and reporting

