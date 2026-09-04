# Scientific requirements awaiting confirmation

The prototype implements metrics that can be defined independently of final
challenge decisions. Generic QC and reference comparisons are not official
OSIPI ranking, and the pipeline does not invent missing scientific rules.

The user confirmed ICC(2,1) and ICC(3,1) for DCE, ASL and DSC. Each challenge
now uses `grouped_statistics.icc.models: [icc2_1, icc3_1]`, with separate results.
The existing `inter_repeat` grouping and 95% confidence level are unchanged:
participants are targets, repeat sessions are columns, and site is held fixed.
This is not an official challenge endorsement or a pass/fail rule.
Use `models: []` to disable ICC; legacy `model: none` also remains supported.
Do not set both `model` and `models`. Threshold placeholders remain
`analysis.thresholds: {}`: no advisory cutoff is applied.

The following decisions still require confirmation from the challenge leads:

- the exact definition of a single official DCE accuracy metric;
- the exact statistical definition of deviance;
- whether DCE signal-time RSS should remain raw or be normalized;
- whether repeat/site variability should be voxelwise, ROI-median based, or both;
- whether the current repeat-based ROI-median grouping should also include
  site comparisons, or use another study design;
- any formal CoV threshold, including a possible `<15%` rule;
- confirmation that the provisional error CoV denominator should be the
  absolute mean ground-truth value within the same region;
- confirmation that within-scan spatial CoV should remain population SD
  divided by the absolute submitted-map ROI mean;
- overall pass/fail thresholds;
- an overall participant score or ranking method;
- the final number of synthetic participants and scans;
- final ASL 4-D fitted-model comparison requirements;
- the final ROI/mask set;
- whether overlapping masks should be used as supplied or made exclusive.
  The supplied DCE GM mask includes hippocampal voxels, whereas the example
  answer key excludes them. Those regions produce different, valid arithmetic
  results; the app must not silently subtract one mask from another. Confirm
  the intended regions before treating the answer key as an acceptance test;
- exact required sections in the methods document.

## Implemented provisional analyses

- ASL CBF/ATT and DCE Ktrans ROI mean, median, population SD, observed range,
  and spatial CoV for every compatible organiser-provided mask;
- compatible reference-map bias, MAE, RMSE, Pearson correlation, error SD,
  error CoV, valid voxel count, and difference NIfTI, for the whole image and
  compatible ROIs;
- conditional DCE **Residual Sum of Squares (RSS)** when one measured and one
  modelled 4-D signal can be matched to the same scan, summarized by median,
  mean, population SD, and voxel count for the whole image and compatible ROIs;
- descriptive participant/repeat/site grouping of scan-level ROI medians with
  mean, population SD, CoV, and a signed paired difference for exactly two
  clearly matched repeats or sites;
- ASL CBF/Perfmap and ATT/ATTmap generic reference comparison with the same
  compatible whole-image and ROI error metrics;
- intraclass correlation for a participant x session table, in any of
  ICC(1,1), ICC(2,1), ICC(3,1), ICC(1,k), ICC(2,k) and ICC(3,k), with exact
  F-based intervals, verified against Shrout & Fleiss (1979). The two requested
  models are configured. A participant missing any session is excluded from the
  table and counted, never imputed.

Submitted maps, private references, and masks must have compatible shape,
voxel size, and affine/orientation before voxelwise comparison or masking.
QC previews include orientation labels and a selectable derived middle-slice
overlay for every compatible mask. The private mask and ground truth files,
and their server paths, are never exposed by browser NIfTI or scoring
endpoints. Report descriptive columns can be selected per challenge with
`analysis.roi_descriptive.report_metrics`.

The currently implemented CoV conventions are therefore explicit but
provisional: error CoV is `population SD(submitted - ground truth) /
abs(mean(ground truth))` within the scored region, and spatial CoV is
`population SD(submitted ROI values) / abs(mean(submitted ROI values))`.
Both are ratios in stored data and percentages only at presentation time.

RSS is not called deviance. Grouped statistics are
not called repeatability, reproducibility, or ICC: ICC is a separate,
explicitly modelled statistic that appears only when a challenge configures a
model. No official OSIPI challenge ranking is currently configured.

BIDS structural validation is enabled for every challenge at `warning`
severity with `require_layout: false`, so a submission that is already
BIDS-shaped is checked and one that is not is left alone. Cross-scan grouping
is enabled for ASL and DSC as well as DCE: it needs resolved identity, which
comes from directory structure and prefixed filename tokens, not from a
challenge-specific filename pattern.
