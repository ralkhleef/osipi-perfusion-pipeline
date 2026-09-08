# Mentor meeting guide

For a DCE-only discussion, use
[`DCE_MEETING_BRIEF.md`](DCE_MEETING_BRIEF.md).

## What the application currently does

| Function | What it does | Why it is useful | Output |
|---|---|---|---|
| Import | Accepts a ZIP, selected files or folder, public GitHub repository, or Zenodo record. It separates teams in a batch and keeps configured dataset folders together. | Reviewers can use the source they already have without reorganising it first. | A local submission manifest and file inventory. |
| Challenge and map detection | Uses names and aliases from `config/validation_rules.yaml` to suggest DCE, ASL, or DSC and identify parameter maps. Ambiguous cases require reviewer confirmation. | It reduces typing without silently guessing. | Detected challenge, map roles, and any conflicts. |
| Validation | Checks required maps and artifacts, scan identity, configured dataset counts, NIfTI readability, dimensions, finite values, and Docker readiness. Optional BIDS checks report structural warnings. | It finds incomplete or unreadable submissions before analysis. | Issues with a code, severity, file, and readable message. Warnings do not become failures. |
| Docker execution | Runs submitted code without network access and with configured time, memory, CPU, and process limits. Result-only submissions skip this step. | It supports reproducible submissions while limiting the effect of untrusted code. | Execution status, log, duration, and generated output maps. |
| Map QC | Reads each NIfTI and reports shape, voxel size, orientation, data type, voxel count, finite values, NaN/Inf values, negative values, and descriptive values. | It catches damaged or unexpected maps without needing reference data. | Per-map QC records and a middle-slice preview. |
| Header comparison | Compares submitted and reference shape, voxel size, affine, and orientation before voxelwise analysis. | Voxel values should not be compared when the images do not describe the same grid. | A field-by-field match or mismatch with the submitted and reference values. |
| ROI statistics | Applies each compatible configured mask to a map and calculates mean, median, population SD, observed range, CoV, and finite voxel count. | It provides understandable regional summaries without claiming an official score. | One record per scan, map, and ROI, plus a CSV export. |
| Reference comparison | On compatible submitted and reference maps, calculates bias, MAE, RMSE, Pearson correlation, error SD, error CoV, valid overlap, and a difference map. It can repeat this inside compatible ROIs. | It shows the size, direction, and pattern of disagreement instead of hiding them inside one number. | Whole-map and ROI records plus a downloadable difference NIfTI. |
| DCE signal RSS | Matches measured and modelled 4-D signals for the same scan and sums squared residuals across time for each voxel. It then gives whole-image and ROI summaries. | It measures model residual size while the definition of official deviance remains undecided. | Raw RSS map summaries. It is labelled RSS, not deviance. |
| Grouped summaries | Groups scan-level ROI medians across repeat, site, or participant axes defined in configuration. It reports mean, population SD, CoV, and a signed paired difference when exactly two matched observations exist. | It exposes variation across the study structure without filling in missing observations. | One record per group, map, ROI, and configured axis. |
| ICC(2,1) | Uses a two-way random-effects, absolute-agreement, single-measurement model on a participant-by-session table, with an exact F-based confidence interval. | It asks whether a single measurement agrees across sessions when those sessions are treated as sampled from a wider population. | ICC value, confidence interval, participant count, session count, exclusions, and status. |
| ICC(3,1) | Uses a two-way mixed-effects, consistency, single-measurement model on the same table, with an exact F-based confidence interval. | It asks whether participants keep the same relative ordering across the specific sessions in the study. Systematic session offsets do not count against consistency in the same way as ICC(2,1). | The same fields as ICC(2,1), reported as a separate row. |
| Analysis packages | Runs a trusted, versioned package whose manifest names its challenge, required inputs, and output metrics. Installing a package does not automatically make it active or official. | Challenge-specific formulas can change without putting private reference data or provisional rules in the main code. | Package results, version, status, and provenance. |
| Reports and exports | Builds HTML and PDF from one report model and provides blinded CSV/JSON summaries, ROI CSV, and an organiser-only unblinded CSV. Missing values remain unavailable. | Reviewers get readable reports and machine-readable data without identity leaking into blinded files. | PDF, HTML, CSV, JSON, ROI CSV, and unblinded CSV. |

## Decisions to ask the mentors for

| Decision | Why it matters | Recommended implementation after confirmation |
|---|---|---|
| Official accuracy definition | Bias, MAE, RMSE, correlation, and percentage error answer different questions. Choosing one changes interpretation and ranking. | Keep the existing component metrics. Add the approved aggregate only in a versioned analysis package, with fixed test cases supplied or approved by the challenge team. |
| Deviance definition | RSS is not automatically statistical deviance. Deviance depends on the assumed likelihood and noise model. | Keep the current output labelled `RSS`. Implement `deviance` only after the mentors provide its formula, inputs, units, and expected result for a small test case. |
| Raw or normalised RSS | Raw RSS grows with signal scale and number of time points. Different normalisations answer different questions. | Preserve raw RSS for traceability. If approved, add a separately named normalised value rather than replacing the raw result. |
| Variability source | ROI medians are compact and robust; voxelwise analysis keeps spatial detail but creates much larger output and requires stronger alignment assumptions. | Keep ROI medians as the default. Add voxelwise analysis as a separate optional result only if the study requires it. Do not combine both into one unnamed number. |
| Repeat and site grouping | Current ICC uses repeats while holding site fixed. A site analysis represents a different design. | Keep axes in YAML. Add `inter_site` only after confirming what is a target, what is a session/rater, and which factor must remain fixed. |
| CoV definition and threshold | CoV changes depending on its denominator, and a proposed 15% cutoff has not been approved. | Confirm the numerator, denominator, zero-mean handling, and whether the limit is advisory. Store an approved advisory limit in YAML; keep pass/fail logic out unless explicitly requested. |
| Final masks and overlaps | Overlapping masks can both be valid, but making them exclusive changes every regional result. | Use supplied masks unchanged by default. If exclusive regions are required, create versioned derived masks outside participant data and record that transformation in provenance. |
| Dataset grids | Completeness cannot be checked without expected participant, repeat, and site counts. | Put approved counts in `validation_rules.yaml`. Leave unknown counts unset rather than inserting placeholders. |
| ASL fitted-signal comparison | The required fitted output and comparison method are not final. | Add the artifact name, dimensions, pairing rule, and formula only after a small approved input/output example is available. |
| Overall score and ranking | Weighting and aggregation can change challenge results even when every component metric is correct. | Implement them in a versioned package, not in the generic QC path. Require an explicit `official: true` release approved by the challenge team. |
| Methods document | The app can record whether one was provided, but its required sections are undecided. | Keep it optional until the mentors provide the section list. Then express the requirement in configuration and validate only those agreed sections. |

## Output rules to keep

- Show the value, unit, scope, input count, and status together.
- Keep ICC(2,1) and ICC(3,1) on separate rows.
- Keep `0` different from `Not available`.
- Include a reason when a value is unavailable.
- Keep per-map and per-ROI records in CSV/JSON even when the PDF uses a shorter summary.
- Include challenge configuration, analysis package version, reference version,
  pipeline version, and analysis date in provenance.
- Never convert a descriptive result into pass/fail or ranking unless an approved
  configuration or official package defines that step.

## Suggested meeting order

1. Demonstrate Upload through Export with one known test submission.
2. Show one value in the interface and the same value in CSV or JSON.
3. Show ICC(2,1) and ICC(3,1) as separate results.
4. Ask the decision-table questions above and record the agreed formula, inputs,
   units, grouping, and one expected example result for each decision.
