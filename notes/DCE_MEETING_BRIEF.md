# DCE mentor meeting brief

## Opening script

> I built a local review workflow for DCE submissions. It imports finished maps
> or runnable code, validates the configured DCE structure, runs code in Docker
> when needed, shows map QC and regional results, and exports blinded reports.
> The generic measurements are working and tested. I have not invented an
> official accuracy score, deviance formula, threshold, or ranking. I need your
> decisions on those definitions and one approved example result for each.

## Five-minute demonstration

For the stable demonstration, use this already-tested synthetic submission:

`data/outputs/dce-provider-test/synthetic_dce_provider_complete.zip`

1. In **Upload**, choose DCE and upload the ZIP.
2. In **Review**, point out that Clinical and Synthetic folders remain one
   submission and that participant, repeat, and site identity is preserved.
3. In **Validate**, show required-content and NIfTI checks. Explain that warnings
   do not become failures.
4. In **Run**, explain that this is a result-only example, so no participant code
   needs to run. Code submissions use the Docker path.
5. In **QC & Preview**, show one Ktrans preview, its unit, finite-voxel result,
   ROI table if compatible masks are available, and the separate ICC rows if
   the dataset supplies enough repeated participants.
6. In **Export**, open the PDF and download CSV Results. Show that missing values
   say `Not available` rather than `0`.

If the live workflow is interrupted, use the existing example PDF and ROI CSV
from `docs/downloads/` and continue the discussion.

## DCE files received from the mentor

Keep these files local. Upload only `submission.zip` through the normal
submission screen; the other three are organiser-side test assets.

| Archive | Contents | Intended use |
|---|---|---|
| `submission.zip` | 10 participants × 3 sites × 2 scans. Each of the 60 scans has Ktrans, vp, and a 4-D Ct file: 180 NIfTIs total. | The result submission being evaluated. |
| `hidden_ground_truth.zip` | The same 60-scan tree and filenames, with 180 matching ground-truth NIfTIs. Every submitted/ground-truth pair has matching shape, voxel size, affine, orientation, and data type. | Private reference values. Never upload it as a participant submission or commit it. |
| `shared_masks.zip` | GM, hippocampus, and WM masks for each of the three site grids, plus the original-grid masks. | Private organiser masks for regional calculations. |
| `known_error.zip` | 60 scan-level and 30 site-level JSON answer keys. Each records the preset and achieved Ktrans bias/variance, vp bias/variance, Ct RSS, and ROI voxel counts. | Regression-test oracle. It should be compared with calculated results, not treated as a participant result. |

All four ZIPs are structurally safe: no absolute paths, parent-directory
traversal, or symbolic links were found. They contain many macOS `__MACOSX`
and `._` entries, which are metadata rather than study files.

### Mask definition that must be confirmed

The supplied GM mask overlaps the hippocampus mask. For P01, site 1, scan 1:

- supplied GM mask: 4,960 voxels;
- hippocampus mask: 262 voxels;
- known-error GM count: 4,698 voxels;
- `4,960 - 262 = 4,698`.

Using the supplied GM mask directly does not reproduce the GM answer key.
Using `GM AND NOT hippocampus` reproduces the Ktrans preset for that scan:

- bias: approximately `0.00005`;
- variance: approximately `6.4e-9`.

Do not subtract the hippocampus silently. Ask whether the official GM region is
exclusive of hippocampus. If yes, keep the supplied mask unchanged and create a
separately named, versioned derived mask with that transformation recorded in
the result provenance.

The answer key names `ktrans_var` and `vp_var`, while the current generic report
shows error SD and error CoV. Ask whether variance itself must also be a visible
official output or whether SD is preferred for the report and variance is only
used by the regression test.

## Current DCE rules

| Item | Current setting | Meaning |
|---|---|---|
| Required parameter map | Ktrans, 3-D | Every complete DCE submission must contain it. |
| Optional parameter maps | vp, ve, Kep | Accepted and identified, but absence does not block validation. |
| Required fitted signal | Modelled signal-time curve, 4-D | Names such as `modelled_st`, `fitted_signal`, and `Ct` are accepted through configuration. |
| Methods document | Optional | The upload records the submitter's answer. Saying one is included when it is missing is still reported. |
| Synthetic grid | 2 repeats, 3 sites, participant count pending | The app checks known counts and does not invent the unknown participant count. |
| Clinical grid | 5 participants, 2 repeats, 1 site | Used for configured completeness checks. |
| BIDS | Structural warnings; layout not required | BIDS-shaped data are checked, but the app does not claim full BIDS validation. |
| ICC | ICC(2,1) and ICC(3,1), 95% intervals, inter-repeat axis | Participants are targets, repeats are sessions, and site is held fixed. Missing sessions are excluded and counted, not filled in. |
| Thresholds | None | No advisory cutoff, pass/fail result, or ranking is produced. |
| Active local provider | DCE example package with masks | It calculates input-derived descriptive values and is explicitly not official. |
| Official TF6.2 provider | Adapter available; private assets not installed | It remains unavailable until the approved script, reference maps, and masks are supplied locally. |

## What each DCE result means

| Result | Exact function | Why show it | Important limit |
|---|---|---|---|
| Map QC | Reads the submitted NIfTI and reports geometry, orientation, type, finite values, NaN/Inf values, negative values, and descriptive values. | Finds damaged or unexpected maps without reference data. | QC is not accuracy. |
| ROI statistics | Applies each compatible mask to Ktrans and reports mean, median, population SD, observed range, CoV, and finite voxel count. | Gives readable regional summaries. | Results depend on the supplied masks and their overlap policy. |
| Bias | Mean of submitted minus reference values. | Shows the direction of average error. | Positive and negative errors can cancel. |
| MAE | Mean absolute submitted-reference difference. | Shows typical error size without cancellation. | It does not show direction. |
| RMSE | Square root of mean squared submitted-reference difference. | Gives more weight to large errors. | It can be dominated by outliers. |
| Correlation | Pearson correlation across valid overlapping voxels. | Shows whether spatial patterns change together. | High correlation does not mean close numerical agreement. |
| Error SD | Population SD of submitted-reference differences. | Shows error spread after separating average bias. | It is not an overall score. |
| Error CoV | Error SD divided by the absolute mean reference value in the same region. | Expresses error spread relative to reference scale. | This denominator is provisional and needs confirmation. |
| Difference NIfTI | Submitted map minus reference map for compatible grids. | Lets reviewers locate error spatially. | No automatic resampling is performed. |
| Signal RSS | For matched measured and modelled 4-D curves, sums `(measured - modelled)^2` over time per voxel and summarises it. | Measures residual size from the fitted curve. | It is raw RSS, not deviance, and grows with scale and time points. |
| Grouped CoV | Groups scan-level ROI medians across configured repeats, sites, or participants. | Shows variation across study structure. | The final grouping design remains a mentor decision. |
| ICC(2,1) | Two-way random-effects, absolute-agreement, single-measurement ICC. | Tests agreement when sessions are treated as sampled from a wider population. | Requires a complete participant-by-session table. |
| ICC(3,1) | Two-way mixed-effects, consistency, single-measurement ICC. | Tests relative consistency across the specific sessions in this study. | A systematic session offset affects it differently from ICC(2,1). |

## Questions to ask, in order

1. **Official accuracy:** Which formula and aggregation should be called the
   official DCE accuracy result? Ask for units, scope, and one small expected
   input/output example.
2. **Deviance:** What exact statistical model and formula define deviance? If
   deviance only means RSS in this challenge, ask them to confirm that wording.
3. **RSS normalisation:** Keep raw RSS, divide by time points, divide by signal
   scale, or report more than one clearly named value?
4. **Repeat/site design:** For ICC and variability, what are the targets,
   sessions or raters, and fixed factors? Should site be analysed separately?
5. **CoV:** Confirm both denominators and what happens when the mean is zero or
   near zero. Is the possible 15% value only advisory or a pass/fail rule?
6. **Masks:** What is the final ROI set? Should overlapping masks be used as
   supplied or converted into explicitly versioned exclusive regions? In
   particular, confirm whether GM must exclude the hippocampus to match the
   supplied known-error files.
7. **Dataset grid:** How many synthetic participants are expected? Are the
   current clinical and synthetic repeat/site counts final?
8. **Official package:** Who supplies and approves the final scorer, references,
   masks, version label, score aggregation, and ranking rules?
9. **Methods document:** Is it optional? If required, what exact sections must
   validation check?
10. **Known-error output:** Should the final output expose error variance as the
    answer key does, error SD as the current generic report does, or both with
    distinct names?

## Recommended implementation after the meeting

- Keep the generic component metrics because they remain useful for review.
- Put approved challenge-specific scoring and ranking in a versioned analysis
  package rather than the generic QC code.
- Require an approved small test case with expected values before calling any
  new formula complete.
- Keep raw RSS even if a separate normalised result is added.
- Keep ICC models as separate rows; do not average them into one value.
- Keep dataset counts, grouping axes, and advisory limits in validated YAML.
- Keep final reference maps and masks local and Git-ignored.
- Record package version, configuration version, reference version, and analysis
  date in every result.

## Phrases to avoid

- Do not call QC an accuracy score.
- Do not call RSS deviance until its definition is confirmed.
- Do not say the current example package is official scoring.
- Do not call structural warnings full BIDS validation.
- Do not describe an unavailable value as zero.
- Do not promise pass/fail or ranking before the rules are approved.

## Decision notes

For every agreed item, record:

| Decision | Formula or rule | Inputs and units | Grouping or ROI | Expected test result | Approver |
|---|---|---|---|---|---|
|  |  |  |  |  |  |
