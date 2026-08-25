# Updating the scoring — your options

Challenge requirements and scientific analysis are separate.
YAML describes which inputs are valid; versioned analysis/scoring code defines
scientific formulas. Use the section that matches the kind of change you need.

The app never invents scores: if reference data or an official scorer isn't
installed, it clearly shows "reference not available" instead of a fake number.

---

## 1 — Change challenge requirements (no coding)

Edit the text file **`config/validation_rules.yaml`**. This controls the
challenges (ASL, DCE, DSC), the parameter maps the app looks for, and the names
it recognises in filenames.

Example — the ASL parameter maps already defined:

```yaml
map_types:
  cbf:
    display: CBF
    label: Cerebral blood flow
    units: mL/100g/min
    dimensions: 3          # parameter maps are single-volume 3-D images
    patterns: [cbf, perfmap, perfusion]
  att:
    display: ATT
    label: Arterial transit time
    units: seconds
    dimensions: 3
    patterns: [att, attmap, arterial_transit_time]
```

To add a new map, copy one of these blocks and change the name, units, and the
`patterns` (the words the app matches in filenames). Save the file, then use
**Reload rules** in the running app. That's it — no code or backend restart.

---

## 2 — Add private reference maps and masks (no coding)

To turn on the RMSE / bias / ROI comparison, put the challenge's ground-truth
files into these folders (create them if missing):

```
data/reference_data/maps/     ← the reference CBF/ATT/... NIfTI files
data/reference_data/masks/    ← the ROI masks (grey matter, white matter, tumour, ...)
```

The app matches a submitted map to the reference of the same type automatically
(e.g. a submitted Perfmap is compared to the reference CBF). ROI rows appear for
each compatible mask in `masks/`. No code is required; newly added reference
assets are used by the next compatible analysis.

> These files are large and challenge-owned, so they are **not**
> stored in this repository. Keep source copies outside the repository or in
> `private_scoring_assets/`; the configured `data/reference_data/` locations are
> also ignored by git.

---

## 3 — Add or update a trusted scoring package

If your team has its own scoring program, package it as a ZIP and upload it from
**QC & Preview → Scoring Setup**. The core app is not modified.

The ZIP looks like:

```
my_scoring_package.zip
├── manifest.json     ← name, challenge_type, map_type, entry_point
├── scoring.py        ← your scoring program (Python)
├── requirements.txt  ← optional dependency declaration
├── reference/        ← optional reference maps
└── masks/            ← optional masks
```

`manifest.json` example:

```json
{
  "package_id": "asl_analysis_v1_0",
  "name": "Local ASL analysis",
  "version": "1.0.0",
  "challenge_type": "asl",
  "map_type": "cbf",
  "required_inputs": ["cbf", "att"],
  "required_assets": ["reference/cbf.nii.gz", "reference/att.nii.gz"],
  "entry_point": "scoring.py",
  "requirements_file": "requirements.txt",
  "call_mode": "standard",
  "official": false,
  "metrics": ["rmse", "bias"]
}
```

Before installation, the app checks the manifest and version, challenge id,
configured inputs, metric names, safe paths, entry point, declared assets,
optional requirements file, and scorer syntax/importability. It validates in a
staging directory. Activation is separate and checks package readiness and
challenge compatibility; a failure leaves the previous active configuration in
place. The selected package version is stored in the active configuration and
in its results.

Use a new versioned `package_id` for each release. Only trusted people should
upload packages because a package is a program that runs on the server. The
tracked `examples/demo-scoring-package/` directory is a safe
demo source that can be zipped locally for practice.

---

## 4 — Extend generic built-in analysis (needs a developer)

Cross-challenge capabilities such as generic compatible-map comparison live in
the built-in analysis code and require implementation, reporting integration,
and tests. New challenge-specific or not-yet-final scientific definitions
should normally be delivered as a new versioned package instead of being added
to YAML or hard-coded into the generic pipeline.

---

## What still needs the challenge team's decisions

These items need challenge-team definitions or data. The app does not guess them:

- The official ASL/DCE/DSC scoring formula, weighting, and any overall score.
- The ICC model and confidence-interval method (repeatability CoV and ICC show
  as "unavailable" until repeated, noise-varied datasets are supplied).
- The official reference maps and masks (Option 2 above).
- Final challenge-specific DCE/ASL/DSC metrics that have not yet been agreed.

Unimplemented items remain unavailable or not configured; they do not block
validation, QC, previews, exports, compatible ROI descriptions, or compatible
generic reference comparisons. Official OSIPI challenge ranking is not
currently configured.
