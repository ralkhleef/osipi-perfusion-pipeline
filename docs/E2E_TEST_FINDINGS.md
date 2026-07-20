# Real-researcher E2E — test findings & report/CSV gap audit

Date: 2026-07-18. Purpose: exercise the app as a researcher across the six
submission types and check it against the acceptance checklist and the detailed
PDF / HTML / CSV specification, before building anything new.

## Environment note (read first)

The Docker + live-browser leg (Terminal 1/2, Chrome console) **must run on your
Mac** — the automation sandbox has no Docker daemon and no browser, so I cannot
run `docker compose up` or open Chrome for you. Everything below the browser was
run for real, in-process, against the actual FastAPI app (same code paths the
container serves). Run the live leg with the commands in the last section.

## What was tested for real (in-process): 27/27 passed

All six submissions were driven through Upload & Detect → Validate → (Run) →
QC/Preview data → Export against the real app.

| Acceptance check | Result |
|---|---|
| Perfmap detected as CBF | ✅ |
| ATTmap detected as ATT | ✅ |
| Valid ASL passes, result-only (`run_readiness=result_only`) | ✅ |
| Missing ATT → `EXPECTED_MAP_MISSING` **warning**, not a blocking error, not a fake result | ✅ |
| CBF and ATT stay separate everywhere (`by_map_type`; combined RMSE is `None`, `aggregate="mixed"`) | ✅ |
| Report shows separate CBF/ATT rows, "Error CoV", repeatability-unavailable note | ✅ |
| Batch ZIP → 3 isolated submissions/results | ✅ |
| Mixed ASL+DCE → each detected & validated under its own challenge; report has "no cross-challenge totals" and no pooled RMSE row | ✅ |
| Corrupt NIfTI → validation error, `passed=False` (blocks progression) | ✅ |
| Reproducible (Dockerfile) → recognized runnable (`has_run_instructions=True`) | ✅ |
| No stale data (ASL report excludes other submissions' ids) | ✅ |
| PDF is a valid `%PDF`; HTML/JSON/CSV (blinded+unblinded) download non-empty | ✅ |
| Blinded CSV does not leak team name or archive/folder name | ✅ |

### Checklist items that are browser-only (verify during the live run)

These can't be asserted in-process; they are covered by automated tests and the
Playwright spec, but confirm them live:

- Back/Continue buttons remain visible, disabled-not-hidden — covered by
  `tests/footer_logic_test.js` (27/27) and the per-step action-row design.
- Refresh restores the correct step; Start New clears the session — covered by
  `tests/frontend_smoke_test.js` (958/958) and `tests/e2e/acceptance.spec.js`.
- No red JS console errors — check DevTools during the live run.

## Report / CSV gap audit vs the detailed spec

The functional behavior is correct and scientifically safe. The **format** of the
researcher-facing deliverables does not yet match the detailed spec. These are
build items for the next pass (not done here, per "test before adding anything").

### CSV — biggest gap: format is WIDE, spec wants LONG

Current researcher-facing combined CSV is **one row per submission** with metric
columns:

```
blinded_submission_id, challenge_type, map_types, map_count, warning_count,
error_count, reference_status, finite_voxels_percent, nan_count, inf_count,
negative_voxels_percent, mean_cbf, mean_att, ... , rmse, mae, bias, cov, icc, notes
```

The spec wants **long format — one row per (submission × map × ROI × metric)**:

```
blinded_submission_id, challenge, subject_id, session_or_repeat_id, map_type,
map_display_name, units, roi, metric_name, metric_value, metric_status,
valid_voxel_count, excluded_voxel_count, finite_voxel_percent, nan_voxel_count,
inf_voxel_count, negative_voxel_percent, reference_status, validation_status,
warning_codes, pipeline_version, configuration_version, export_date
```

Gaps to close:
- Reshape to long format (per map + per ROI + per metric rows). The underlying
  per-map/per-ROI data already exists internally (the report's reference-metric
  rows and the `reference_scoring.csv` artifact), so this is a re-shape, not new
  science.
- Add missing columns: `subject_id`, `session_or_repeat_id`, `roi`,
  `metric_name`/`metric_value`, `map_display_name`, `units`,
  `valid_voxel_count`, `excluded_voxel_count`, `warning_codes`,
  `pipeline_version`, `configuration_version`, `export_date`.
- Blinded id: use neutral `SUB-0001` style (currently `submission_001`).
- Unblinded extras still to add: `contact_name`, `institution`,
  `submission_source`, `repository_url`, `submitted_at`.

Good today: blinded CSV already omits team/contact/archive/folder/paths — no PII
leak observed.

### PDF — missing metadata & per-map structure

Present: challenge name, blinded status, submission id, export date, QC summary,
per-map QC table, separate CBF/ATT reference rows, repeatability-unavailable note,
"not official OSIPI score" note, limitations, previews.

Missing vs spec:
- `pipeline_version` and `configuration/rules version` in the header.
- Per-map "Submitted outputs" sections with **units, dimensions, shape, voxel
  size** (currently QC table has finite/NaN/negative but not units/dims/voxel).
- Reference section: `valid overlapping voxel count` and `excluded voxel count`
  columns (RMSE/MAE/Bias/Error CoV/Correlation are present).
- Difference-map preview (submitted/reference previews exist; add difference).

### HTML — mostly there; add interactive niceties

Present and offline (no CDN), with per-map CBF/ATT tables, previews, collapsible
map details, limitations. Spec extras not yet present: filters by subject/map/ROI,
searchable per-map tables, difference-map download links, a dedicated technical
provenance section, and the fixed first-screen order (overview → key QC → CBF →
ATT → warnings).

## Recommended next build order (after your live run confirms the workflow)

1. **Long-format CSV** (blinded + unblinded) with the spec columns — highest
   researcher value, pure re-shape of existing data.
2. **PDF header** pipeline/config version + per-map units/dimensions/voxel-size
   section + valid/excluded voxel counts + difference-map preview.
3. **HTML** filters, searchable tables, difference-map links, provenance section.

## Live run commands (your Mac)

```bash
# Terminal 1
cd /Users/ralkhleef/Desktop/osipi-perfusion-pipeline
docker compose up --build -d
docker compose ps
curl http://localhost:8000/api/health          # -> {"status":"ok"}
docker compose logs -f                          # watch for tracebacks

# Terminal 2
open -a "Google Chrome" http://localhost:8000   # keep DevTools → Console open

# Optional automated browser pass
npm i -D @playwright/test && npx playwright install chromium
BASE_URL=http://localhost:8000 npx playwright test tests/e2e/acceptance.spec.js --reporter=list
```

Lead the mentor demo with a single Lena ASL ZIP: Upload → Validate → QC & Preview
→ separate CBF and ATT RMSE/bias with ROI rows → difference map → export. Show the
mixed ASL/DCE upload only as a brief bonus.
