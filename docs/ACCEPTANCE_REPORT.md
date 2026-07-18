# OSIPI Perfusion Pipeline — Final Acceptance Test

Date: 2026-07-12 · Scope: full end-to-end acceptance after the UI, performance,
reporting, and modularity work, before official OSIPI scientific scoring is added.

No official scoring formulas, reference data, masks, ICC/repeatability rules,
rankings, or scientific thresholds were invented or modified. Synthetic Lena
float maps were treated as test fixtures only, never as ground truth.

---

## 1. Executive summary

The application is functionally solid. Every automated suite passes, and the
whole backend workflow — upload, detection, quick/deep validation with caching,
previews, QC statistics, and all six export formats (PDF, HTML, CSV, JSON,
blinded/unblinded) — was exercised for real, in-process, against the actual
FastAPI app and the real Lena-style float NIfTIs. Detection, result-only vs
reproducible handling, batch isolation, missing-map severity, blinding, offline
HTML, and PDF generation all behave correctly. Scoring-neutrality wording ("QC &
Preview", "Quality checks and generic reference comparisons", "not official
OSIPI scores") is present and consistent.

One real defect was found and fixed: the committed `tests/footer_logic_test.js`
was **stale** (it asserted against the pre-refactor single-footer design and the
removed "summary" step) and exited non-zero. It is now updated to the current
per-step action-row architecture and passes. No workflow-blocking (P0) or
demo-blocking (P1) application bugs were found.

## 2. Final verdict

**Ready for mentor demo** — after the single P1 test fix that is already
applied in this branch. The live Docker + browser leg (Section 3) still needs to
be run once on your Mac using the provided commands and Playwright spec to
confirm the rendering layer, since that cannot execute in the CI sandbox.

## 3. Environment and exact commands

**Where this ran.** All backend/API/validation/export testing ran *in-process*
against the real app (`fastapi.testclient.TestClient(main.app)`) on Linux,
Python 3.10.12, Node v22.22.3. There is **no Docker daemon and no rendering
browser in the CI sandbox**, so `docker compose up --build` and Playwright were
not executed here — they must be run on your machine.

Automated suite (run from repo root; all green):

```bash
export PYTHONPATH=backend:src
python -m compileall -q backend src tests      # OK
node --check frontend/app.js                   # OK
python -m pytest -q                            # 178 passed
node tests/frontend_smoke_test.js              # 958 passed, 0 failed
node tests/footer_logic_test.js                # 27 passed, 0 failed (was 21/5 before fix)
git diff --check                               # clean
```

Live leg to run on your Mac (documented workflow):

```bash
docker compose up --build                      # app at http://localhost:8000
# then, in a second shell:
npm i -D @playwright/test && npx playwright install chromium
BASE_URL=http://localhost:8000 npx playwright test tests/e2e/acceptance.spec.js --reporter=list
```

Confirm during that run: `http://localhost:8000` opens, container health is OK,
`GET /api/config` returns 200, frontend assets load, and no backend tracebacks
or browser-console errors appear. Record the browser and OS used.

## 4. Screenshots

The live browser screenshots are produced by the Playwright spec into
`tests/e2e/screenshots/` when you run it on your Mac (the sandbox has no display
to capture from). In place of browser screenshots, the actual generated
deliverables are attached: `lena01_report.html`, `lena01_report.pdf`,
`lena01_unblinded.csv`, `lena01_blinded.csv`, `batch.csv`.

## 5. Full workflow test results (in-process, real app)

31/31 API workflow checks passed. Highlights:

- `GET /api/health`, `/api/config` (3 challenges, 8 map types), `/` (serves
  index.html), `/api/execution-status`, `/api/performance/timings` — all 200.
- Upload `lena_01` → 200, batch=false, 5 files, **3 NIfTI detected**.
- Validate → challenge **ASL**, mode **result_only**, `run_readiness=result_only`,
  `passed=true`, one non-blocking warning `NO_RUN_INSTRUCTIONS` ("result maps
  only … add a Dockerfile"). `error_count`/`warning_count` match the lists.
- Quick-vs-cached validation: **first 7440 ms → second 15 ms** (cache reused).
- Previews: 2 maps, correct labels/units (ATT → "Arterial transit time",
  seconds), `finite_percent`, `negative_percent`, mean/std present.
- Exports: JSON, CSV, blinded + unblinded — all 200, non-empty; blinded CSV is
  smaller (PII columns dropped).
- HTML report 15.7 KB, no external CDN/script, 3 inline SVG charts, distinguishes
  generic vs official. PDF valid (`%PDF-`, 26 KB).

## 6. Lena ZIP compatibility results

Only `lena_01` shipped in the repo; `lena_02`–`lena_05` were **generated from
`lena_01`'s float NIfTIs** and are now in `submissions/incoming/`. 18/18 checks
passed:

| Package | Result |
|---|---|
| lena_01 exact single | 1 submission, ASL, 3 NIfTI (asl+Perfmap+ATT), no false missing-map error |
| lena_02 no-wrapper | grouped into **one** submission, 3 NIfTI, no artificial folder — matches wrapped |
| lena_03 missing ATT | reported as **warning** `EXPECTED_MAP_MISSING` (non-blocking), `passed=true`, exact submission identified, useful next-action message |
| lena_04 batch of three | batch=true, exactly 3 submissions, batch validation returns 3 isolated results, ids preserved (no leak/merge) |
| lena_05 original working | uploads, 3 NIfTI, validation passes — no regression |

Realistic bundle also exercised: DCE reproducible (4 NIfTI), DSC result-only,
batch mixed valid/invalid (batch=true), **corrupt NIfTI → 2 validation errors**,
large ASL (325 ms upload), many-files indexing (**1505 files, 337 ms**).

## 7. UI/UX findings

Reviewed against the source and the smoke/footer test guarantees (the rendered
review needs the live leg). The step model is Upload → Review/Index → Validate →
Run → QC & Preview → Export. Confirmed in source:

- The "buttons stay visible, only disabled" invariant holds via per-step
  `data-step-action-row` elements (`primaryBtn.disabled = !canProceed`; rows are
  hidden by `display`, never removed). Blocked steps keep the Continue button
  visible with a tooltip reason and `aria-label`.
- Warnings never block Continue; blocking is driven by `error_count`, and a mixed
  batch with one passing submission still lets the user proceed.
- Result-only vs reproducible is distinguished (`run_readiness`), and QC is
  clearly separated from official scoring in title, subtitle, and tooltips.

Minor, non-blocking wording/consistency items (P2, deferred — see §16):
`count` is `null` in the batch upload response, and `detected_map_types` is
blank in the batch CSV. Neither affects the workflow.

## 8. Backend / API findings

Safety and error handling are correct:

- Blank/missing `submission_id` → 400/422; malformed JSON → 422; bad email → 400.
- Unknown `job_id` → 404; missing export → 404; unsupported export format → 404.
- **Path-traversal** on the NIfTI download route → **403** (no file served).
- Private reference/mask maps are not downloadable (covered by
  `test_download_endpoint_does_not_serve_private_reference_maps`).
- Unsafe ZIP extraction is bounded by size/file-count limits; no raw stack traces
  are returned to the client (errors are wrapped as `HTTPException` detail).

One low-severity finding (P2, flagged, not changed): validation JSON and the
JSON export include absolute server paths in the `path`/`file_path` fields (e.g.
`/…/submissions/extracted/…`). These are **not shown in the UI** (the frontend
never reads them), so this is an information-tidiness issue, not a user-facing
leak. Relativizing them touches validation output broadly and risks the preview
matching and existing test expectations, so it is left for a scoped change.

## 9. Docker findings

Not executable in the sandbox (no daemon). `execution-status` correctly reports
Docker unavailable with a friendly message rather than crashing. The
docker-compose file, Dockerfile, DooD path-translation env vars, socket mount,
and restart policy are present and internally consistent. Execution service
behavior (build, run, logs, output detection, manifest refresh, resource/timeout
limits, failure/retry paths) is covered by `tests/test_execution.py`, which
passes. **The real Docker build/run path must still be validated once on your
Mac** — it is the main item that could not be exercised here.

## 10. Validation findings

Deep and quick validation both work, with caching. The pytest suite covers the
full edge matrix and passes: NaN → warning (not error), Inf handled, zero-byte →
error, corrupt/fake NIfTI → error, empty/missing folder → fail, missing expected
maps → warning, duplicate filename → warning, unknown challenge → fail,
shape/dimensionality warnings, **quick validation does not load the voxel array**
(`test_quick_validation_does_not_load_voxel_array`), editing a file invalidates
its cache, a config-fingerprint change invalidates the cache, the worker limit is
enforced, and parallel batch failures are isolated. Error/warning totals match
the detailed lists (verified live). Validation duration and cache-hit flags are
present in the payload (`validation_mode`, `cache_hit`, timestamps).

## 11. Performance measurements (in-process, real app)

| Operation | Measurement |
|---|---|
| Upload lena_01 (9 MB zip) | ~170 ms |
| Deep validation, first run (197×233×189×15 + two 3-D maps) | 7440 ms |
| Cached validation, second run | **15 ms** |
| Batch upload (3 submissions, 27 MB) | 472 ms |
| Large-ASL upload | 325 ms |
| Many-files indexing (1505 files) | 337 ms |
| HTML report generation | 15.7 KB, sub-second |
| PDF report generation | 26 KB, sub-second |

Manifest reuse, non-repeated recursive scans, worker limits, and cache reuse are
verified by `test_performance_optimizations.py` and the manifest/preview cache
tests (all pass). Duplicate-job prevention and job-status reporting exist via the
in-process `job_status`/`recent_timings` helpers.

## 12. PDF / HTML / export findings

All six formats generate and are non-empty. Blinding works both directions
(unblinded CSV contains team/contact; blinded CSV drops those columns entirely).
HTML report opens offline with **no external CDN/script/stylesheet** (only the
w3.org SVG namespace URI), inline SVG charts, and the correct challenge and
submission id. PDF is a valid `%PDF-` document. Batch percent aggregation is
`voxel_weighted` per `config/settings.yaml`. Generated files attached for review.
Fine-grained print/mobile layout and PDF page-break/clipping checks belong to the
live browser/PDF-viewer leg.

## 13. Accessibility findings

Source-level: disabled controls carry `title` + `aria-label` reason text, the
upload submit carries an `aria-label`, and blocked Continue buttons expose the
reason to assistive tech. Full keyboard-only navigation, focus-visible styles,
tab order, ARIA live regions, contrast, and chart text/table equivalents require
the live browser leg and an axe/Lighthouse pass — included as a to-do in the
Playwright hand-off.

## 14. Files changed for P0/P1 fixes

- **`tests/footer_logic_test.js`** (P1, test-only, applied): updated the stale
  assertions to the current per-step action-row architecture and removed the
  retired "summary" step. Now 27 passed / 0 failed (was 21 / 5, exit 1).
- **`tests/e2e/acceptance.spec.js`** + **`tests/e2e/README.md`** (new): ready-to-run
  Playwright acceptance spec for the live browser leg (startup, Lena upload,
  validate, result-only handling, preview, exports, missing-ATT warning, Start
  New, refresh/session restore, narrow viewport).
- **`submissions/incoming/lena_02..05_*.zip`** (new fixtures): generated from
  `lena_01` for the no-wrapper / missing-ATT / batch / baseline cases.

No application/runtime code was changed. No scoring logic, thresholds, reference
data, or rankings were touched.

## 15. Exact automated test results

```
python -m compileall -q backend src tests   → OK (exit 0)
node --check frontend/app.js                 → OK
python -m pytest -q                          → 178 passed, 1 warning
node tests/frontend_smoke_test.js            → 958 passed, 0 failed
node tests/footer_logic_test.js              → 27 passed, 0 failed (exit 0)
git diff --check                             → clean
In-process API acceptance harness            → 31 passed, 0 failed
Lena + realistic-bundle harness              → 18 passed, 0 failed
Export/report audit harness                  → 12 passed, 0 failed
```

## 16. Remaining items requiring mentor / OSIPI scientific input, or the live leg

1. **Official OSIPI scoring** — intentionally not added. Formulas, reference
   data, masks, ICC/repeatability/reproducibility rules, rankings, and thresholds
   are scientific decisions for the mentor/OSIPI team.
2. **Live Docker + browser leg** — run `docker compose up --build` and the
   Playwright spec on your Mac to confirm the rendering layer, real Docker
   execution (build/run/logs/limits/timeout/retry), print/mobile PDF layout, and
   an accessibility (axe/Lighthouse + keyboard) pass. Everything below the UI is
   already verified.
3. **P2 tidy-ups (optional, non-blocking)**: relativize absolute paths in
   validation JSON/exports; populate `count` in the batch upload response and
   `detected_map_types` in the batch CSV. Left out to avoid touching validation
   output shape without a scoped review.
```
