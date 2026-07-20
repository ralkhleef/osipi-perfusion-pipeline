# Progress summary — OSIPI perfusion pipeline

Status for the mentor meeting. Framing: the pipeline is a **safe, correct QC and
reference-comparison tool**. It intentionally does **not** invent any official
scoring — it prepares everything so the ASL/DCE/DSC teams can add official scoring
when it's defined.

## Done and verified

**Scientific correctness (aligned with Lena's answers)**
- **CBF and ATT are never averaged.** Metrics are reported per map type
  everywhere (tables, summaries, exports). Different units (mL/100g/min vs
  seconds), so no combined RMSE/MAE/Bias is produced.
- **CoV wording fixed.** The reference CoV is labeled **Error CoV** (spread of
  voxel errors); the QC map-variability CoV is **Spatial CoV**. Neither is called
  repeatability.
- **Repeatability CoV and ICC are shown as "unavailable"** until repeated
  noise-varied datasets exist — stated in the reports, not silently omitted.
- **Spatial alignment guard.** Before voxelwise comparison, shape + affine +
  voxel size must match; otherwise it returns `spatial_grid_mismatch` (two
  same-shape maps offset in space are refused, not scored as if aligned). No
  silent resampling. Difference maps preserve the original affine/header.
- **Dimension rules.** Perfmap and ATTmap must be exactly 3-D; a 4-D file is
  treated as ASL/model data ("4D ASL data"), kept for download, never scored as a
  parameter map.
- **Missing ≠ zero.** Missing metrics render as blank / "Not available"; a missing
  reference is never treated as a score of zero. No pass/fail scientific threshold.

**Reporting & exports**
- HTML + PDF reports group by challenge, show per-map CBF/ATT reference tables and
  ROI rows, previews, and clear "not official scoring" / repeatability-unavailable
  notes. HTML opens fully offline (no CDN).
- **Long-format researcher CSV** (blinded + unblinded): one row per submission ×
  subject × session × map × ROI × metric, with pipeline/config version and export
  date. Blinded CSV carries no identity or file paths.
- **Parameter Map Previews** show only real 3-D parameter maps — no "Unknown" 4-D
  card beside CBF and ATT.

**Workflow / usability**
- Full guided workflow: Upload & Detect → Review → Validate → Run → QC & Preview →
  Export. Result-only submissions correctly show "Execution not required."
- **Multi-ZIP upload** and **per-submission challenge detection**: a mixed ASL +
  DCE upload is grouped and validated per challenge, with **no cross-challenge
  totals**. Single-ZIP and single-challenge flows are unchanged.
- **Extensibility guide** (`docs/ADDING_SCORING_METRICS.md`) shows how to add an
  official metric, configure it, expose it in reports, and test it.

**Quality bar**
- Automated tests: **213 Python** (pytest) + **964 frontend smoke** + footer/nav
  checks, all green. Python/JS syntax checks pass. Full in-process end-to-end run
  across all six submission types passed (valid ASL, missing ATT, batch, mixed
  ASL/DCE, corrupt NIfTI, reproducible).

## Waiting on mentor input (not invented on purpose)

- Official ASL scoring formula, weighting, and any overall/composite score.
- ICC model choice and confidence-interval method.
- Repeated noise-varied datasets (for repeatability CoV / ICC).
- Reproducibility across sites.
- Ground-truth 4-D ASL series (for fitted-model comparison).
- DCE and DSC expected files, reference maps, masks, and metrics (from Olivia /
  the challenge teams).
- Official masks/ROIs (gray matter, white matter, tumour, additional ROI).

## Notes / caveats
- The application code is committed (branch is on the latest preview-filter
  commit); only the demo/testing docs are untracked and can be committed
  separately.
- **Reference data:** Lena's CBF/ATT ground-truth maps are installed in
  `data/reference_data/maps/`, so reference-dependent values (RMSE/MAE/Bias,
  correlation, voxel counts) now activate. Her 4-D ASL ground truth is set aside
  for the future fitted-model comparison. Masks (grey/white/tumour/ROI) are not
  yet provided, so scoring is whole-image only for now. Full-resolution comparison
  (8.6M voxels) takes ~30-40s per submission in the current path — fine, just not
  instant. (These reference files are gitignored and stay out of the repo.)
- Live Docker + browser run happens on the Mac; everything below the browser is
  verified by the automated tests.
