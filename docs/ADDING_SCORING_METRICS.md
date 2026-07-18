# Adding a scoring metric to the pipeline

This guide is for OSIPI maintainers who want to extend the pipeline after the
GSoC project. It shows how to add **one** new voxelwise comparison metric and
surface it everywhere (JSON, CSV, HTML/PDF reports), plus how map types, ROIs,
and dimensionality rules are configured. It deliberately does **not** define any
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

ROIs/masks are discovered from the reference `masks/` folder; their friendly
labels come from `paths.mask_label_rules` in `config/settings.yaml` (e.g. gray
matter, white matter, tumour, additional ROI). Add a label rule there to
standardise a new ROI name.

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

- **Repeatability CoV / ICC** — require repeated (noise-varied) datasets and a
  chosen ICC model + confidence-interval method. The pipeline reports
  `repeatability_status: unavailable_requires_repeated_datasets` until then.
- **Reproducibility across sites** — requires multi-site acquisitions.
- **4-D fitted-model comparison** against the ground-truth ASL series — needs the
  4-D reference file; 4-D submissions are currently tagged as a fitted-model role
  and not scored as parameter maps.
- **Overall/composite score, weighting, and ranking** — the ASL team has not
  approved a formula; ASL results are shown per-metric and per-map, not ranked.
- **DCE / DSC rules** — expected files, references, masks, and metrics come from
  the respective challenge teams.
