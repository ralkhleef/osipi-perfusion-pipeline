# Scientific requirements awaiting confirmation

The prototype implements metrics that can be defined independently of final
challenge decisions. Generic QC and reference comparisons are not official
OSIPI ranking, and the pipeline does not invent missing scientific rules.

The following decisions still require confirmation from the challenge leads:

- the exact definition of a single official DCE accuracy metric;
- the exact statistical definition of deviance;
- whether DCE signal-time RSS should remain raw or be normalized;
- whether repeat/site variability should be voxelwise, ROI-median based, or both;
- whether formal ICC is required and, if so, which ICC model;
- any formal CoV threshold, including a possible `<15%` rule;
- overall pass/fail thresholds;
- an overall participant score or ranking method;
- the final number of synthetic participants and scans;
- final ASL 4-D fitted-model comparison requirements;
- the final ROI/mask set;
- exact required sections in the methods document.

## Implemented provisional analyses

- DCE Ktrans ROI median, population SD, and spatial CoV;
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
  compatible whole-image and ROI error metrics.

RSS is not called deviance. Grouped statistics are
not called repeatability, reproducibility, or ICC. No official OSIPI challenge
ranking is currently configured.
