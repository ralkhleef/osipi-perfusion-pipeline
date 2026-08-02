# Updating the scoring — your options

There are four ways to change how submissions are scored, from easiest (no
coding) to most technical (editing code). Pick the lowest-numbered one that does
what you need.

The app never invents scores: if reference data or an official scorer isn't
installed, it clearly shows "reference not available" instead of a fake number.

---

## Option 1 — Change which maps/challenges are expected (no coding)

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
`patterns` (the words the app matches in filenames). Save the file and restart
the app. That's it — no code.

---

## Option 2 — Install official reference maps and masks (no coding)

To turn on the RMSE / bias / ROI comparison, put the challenge's ground-truth
files into these folders (create them if missing):

```
data/reference_data/maps/     ← the reference CBF/ATT/... NIfTI files
data/reference_data/masks/    ← the ROI masks (grey matter, white matter, tumour, ...)
```

The app matches a submitted map to the reference of the same type automatically
(e.g. a submitted Perfmap is compared to the reference CBF). ROI rows appear for
each mask you drop in `masks/`. No code, just files. Restart the app after
adding them.

> These files are large and challenge-owned, so they are intentionally **not**
> stored in this repository. They are ignored by git and stay on your machine.

---

## Option 3 — Add a whole custom scoring script (no edits to this app)

If your team has its own scoring program, package it as a ZIP and upload it in
the app (Score step → **Scoring Setup**). The core app is not modified.

The ZIP looks like:

```
my_scoring_package.zip
├── manifest.json     ← name, challenge_type, map_type, entry_point
├── scoring.py        ← your scoring program (Python)
├── reference/        ← optional reference maps
└── masks/            ← optional masks
```

`manifest.json` example:

```json
{
  "package_id": "asl_official",
  "name": "Official ASL scoring",
  "version": "1.0.0",
  "challenge_type": "asl",
  "map_type": "cbf",
  "entry_point": "scoring.py",
  "call_mode": "standard",
  "metrics": ["rmse", "bias"]
}
```

Only trusted people should upload packages — a package is a program that runs on
the server. There are two demo packages in the repo you can practise with:
`data/sample_submissions/demo_scoring_package.zip` and the ASL QC demo in
`submissions/incoming/`.

---

## Option 4 — Change a metric formula in the app itself (needs a developer)

If you want to change how an existing metric (RMSE, MAE, bias, correlation) is
computed, or add a brand-new one to the built-in comparison, that lives in
**`backend/scoring.py`**. A developer follows the step-by-step guide in
**`docs/ADDING_SCORING_METRICS.md`**: add the metric function, expose it in the
reports, and add a test. Roughly a day's work per metric, no rewrite.

---

## What still needs the challenge team's decisions

These are deliberately left as configurable placeholders until you provide the
definitions/data — the app does not guess them:

- The official ASL/DCE/DSC scoring formula, weighting, and any overall score.
- The ICC model and confidence-interval method (repeatability CoV and ICC show
  as "unavailable" until repeated, noise-varied datasets are supplied).
- The official reference maps and masks (Option 2 above).
- DCE/DSC expected files and metrics.
