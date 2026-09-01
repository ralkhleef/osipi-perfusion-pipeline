# Adding a scoring metric to the pipeline

This guide is for OSIPI maintainers who want to extend the pipeline after the
GSoC project. It shows how to add **one** new voxelwise comparison metric and
surface it everywhere (JSON, CSV, HTML/PDF reports), plus how map types, ROIs,
and dimensionality rules are configured. It does **not** define any
official OSIPI score, weighting, threshold, or ranking — those remain scientific
decisions for the challenge teams.

## Mental model

For each submitted parameter map the pipeline:

1. detects its **map type** (e.g. `Perfmap` → CBF, `ATTmap` → ATT) from the
   filename patterns in `config/validation_rules.yaml`;
2. finds the matching **reference** map (and any masks/ROIs);
3. checks the maps are comparable — same **dimensionality** (CBF/ATT must be
   3-D), same shape, and the same **physical grid** (affine + voxel size);
4. computes **per-voxel metrics** over the whole image and over each ROI/mask;
5. aggregates results **per map type** (never averaging CBF with ATT — different
   units), and writes them to the reports/exports.

Metrics live in `backend/scoring.py`. Map types and ROIs live in
`config/validation_rules.yaml` and `config/settings.yaml`.

## Where the numbers are computed

`_comparison_metrics(submitted_values, reference_values, selector=None)` in
`backend/scoring.py` returns the dictionary of metrics for one region (whole
image when `selector` is `None`, otherwise a boolean mask). This is the single
place to add a metric so it flows to both whole-image and every ROI.

Current keys include `rmse`, `mae`, `bias`, `standard_deviation_error`,
`error_coefficient_of_variation` (spread of voxel errors ÷ reference mean — **not**
a repeatability CoV), `correlation`, `voxel_count`, and
`negative_voxel_percent`.

## Step 1 — add the metric function

Add a small pure helper near the other math helpers in `scoring.py`:

```python
def _median_absolute_error(errors: list[float]) -> float | None:
    finite = sorted(abs(e) for e in errors if math.isfinite(e))
    if not finite:
        return None
    mid = len(finite) // 2
    if len(finite) % 2:
        return finite[mid]
    return (finite[mid - 1] + finite[mid]) / 2
```

## Step 2 — return it from `_comparison_metrics`

Inside the "compared" return dict in `_comparison_metrics`, add your key:

```python
        "mae": _json_float(mae),
        "median_absolute_error": _json_float(_median_absolute_error(errors)),
```

Also add the key (set to `None`) to the early "no finite overlap" return dict in
the same function, so the shape of the result is consistent.

## Step 3 — expose it in the CSV artifact

In `_write_reference_scoring_artifacts`, add the column name to the
`csv.DictWriter(fieldnames=[...])` list and to both `writer.writerow({...})`
calls (whole map and mask rows):

```python
"median_absolute_error": whole.get("median_absolute_error"),
```

## Step 4 — add a human label and definition

- Add a display label in `backend/main.py` `_LABELS` (and the mirror in
  `frontend/app.js`) so reports show a friendly name.
- Add a one-line definition to the `metric_definitions` dict built in
  `_score_reference_maps` so the report's methodology section can explain it.
- Missing values must render as **"Not available"**, never blank or `0`
  (the report/export helpers already do this when the value is `None`).

## Step 5 — choose which maps and ROIs use it

Map types and their units, filename patterns, and dimensionality live in
`config/validation_rules.yaml`:

```yaml
map_types:
  cbf:
    display: CBF
    label: Cerebral blood flow
    units: mL/100g/min
    dimensions: 3            # parameter maps are single-volume 3-D images
    patterns: [cbf, perfmap, perfusion, perf]
  att:
    display: ATT
    label: Arterial transit time
    units: seconds
    dimensions: 3
    patterns: [att, attmap, arterial_transit_time]
```

ROIs/masks are discovered from a `masks/` folder under **any** reference root,
not only the one holding the ground-truth maps, so a shared mask folder and a
per-challenge map folder both work. Drop a `gm_mask.nii.gz` into any of:

```text
data/reference_data/masks/            # shared across challenges
data/reference_data/<challenge>/masks/
data/scoring/reference/masks/
submissions/extracted/<id>/reference/masks/   # for local testing
```

A mask must be on the same grid as the map it is applied to: same shape, voxel
size and affine. A mask on a different grid is reported per map with
`shape_mismatch` rather than skipped, so a mismatch is visible instead of
looking like an empty region.

Friendly labels come from `paths.mask_label_rules` in `config/settings.yaml`
(gray matter, white matter, lesion, ROI). Add a label rule there to standardise
a new ROI name; `gm_mask.nii.gz` already reads as "gray matter".

`_comparison_metrics` already runs for the whole image and for every discovered
ROI, so a new metric automatically appears for all of them — no per-ROI wiring.

## Step 6 — add a test

Add a case to `tests/test_asl_scoring_rules.py` (or
`tests/test_reference_scoring.py`) using the small synthetic-NIfTI helpers there:

```python
def test_median_absolute_error(workspace):
    _submit(workspace, [3, 4, 5, 6], "sub-001_Perfmap.nii.gz")
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    row = _row(_score(workspace), "CBF")
    assert row["whole_map"]["median_absolute_error"] == 2.0
```

Run `python -m pytest -q` to confirm.

## What NOT to add without challenge data/decisions

These are placeholders on purpose and need mentor-provided data or a
challenge-approved definition first:

- **Repeatability CoV** — requires repeated (noise-varied) datasets. The
  pipeline reports `repeatability_status: unavailable_requires_repeated_datasets`
  until they exist.
- **Overall/composite score, weighting, and ranking** — see below.
- **Overall/composite score, weighting, and ranking** — the ASL team has not
  approved a formula; ASL results are shown per-metric and per-map, not ranked.
- **Unconfirmed DCE / ASL / DSC rules** — references, masks, thresholds, and
  official metrics still need challenge-team approval where noted in
  `SCIENTIFIC_REQUIREMENTS_PENDING.md`.


## Things you can now turn on yourself

These needed code when the list above was written. They no longer do: each is
one configuration block in `config/validation_rules.yaml`, and each ships off
so that turning it on is a recorded decision rather than an assumption.

### ICC

All six Shrout & Fleiss models are implemented with exact F-based confidence
intervals. Choosing the model is the scientific decision, so no model is
applied by default and ICC keeps reporting "not configured":

```yaml
challenges:
  asl:
    grouped_statistics:
      icc:
        model: icc2_1        # or icc1_1, icc3_1, icc1_k, icc2_k, icc3_k
        axes: [inter_repeat] # inter_site for reproducibility across sites
        confidence_level: 0.95   # null to report the estimate with no interval
```

Which model to pick, in one line each:

| Model | Use when |
|---|---|
| `icc1_1` | each participant is measured by a *different* set of sessions |
| `icc2_1` | same sessions for everyone; a systematic session offset should count against agreement; result should generalise to other sessions |
| `icc3_1` | same sessions for everyone; those sessions are the only ones of interest, so a systematic offset does not count against agreement |
| `_k` forms | you are quoting the reliability of the *mean* of k measurements rather than one |

`icc3_1` is never lower than `icc2_1` on the same data, because it forgives
session offsets. Participants are the targets ICC measures over, so
`inter_participant` is not a valid axis and is rejected by the schema.

### The 4-D fitted-model comparison

Enabled for ASL and DCE. Participants submit what they fitted; it is compared
voxel by voxel against the ground-truth 4-D series and summarised as RSS for
the whole image and for every compatible mask:

```yaml
challenges:
  asl:
    analysis:
      signal_rss:
        enabled: true
        modelled_artifact: modelled_st
        measured_artifact: measured_st
```

It stays inert until the files exist: a submission with no 4-D model reports
`modelled_signal_not_available` rather than failing. To make the 4-D model
**mandatory**, add it to that challenge's `required_artifacts`:

```yaml
    required_artifacts: [modelled_st, methods]
```

The filename patterns that identify these files are in `artifact_types` at the
top of the same file — add a pattern there if your naming differs.

### Advisory thresholds

To mark rows for a reviewer to look at, without any pass/fail:

```yaml
challenges:
  asl:
    analysis:
      thresholds:
        roi_within_scan_cov:
          warn_above: 0.15
          note: Rough guide only; not a pass/fail criterion.
```

Thresholds use **stored units**, so a CoV threshold is the ratio `0.15`, not
`15`. Writing `15` is rejected with a message telling you what to write,
because it would otherwise load cleanly and never fire.

A flagged row keeps every one of its values; flagging annotates, it never
blanks, excludes or orders anything. There is deliberately no `fail_above`.

### Which statistics the report shows

Per challenge, per map type:

```yaml
challenges:
  asl:
    analysis:
      roi_descriptive:
        enabled: true
        map_types: [cbf, att]
        report_metrics: [mean, median, standard_deviation, range,
                         coefficient_of_variation]
```

Every metric in `report_metrics` is computed inside each discovered mask as
well as over the whole image.

## Still needing a decision, not code

- **An overall or composite score, weighting, and ranking.** Nothing is
  configured and nothing is implemented, because "reproducibility matters more
  than accuracy" needs a weighting before it can be arithmetic.
- **Pass/fail.** Deliberately absent throughout; thresholds are advisory only.
- **The expected cohort grid for ASL and DSC** (`datasets:`), which drives
  completeness checking.
