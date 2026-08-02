# Full Walkthrough and Validation Report

Date: 2026-08-01 · Scope: whole repository, every automated suite, plus a real
DCE-2026 submission driven through a live server.

---

## Summary

Everything passes the automated verification suite — 512 Python tests, 1014
frontend smoke checks, 53 DOM tests, 27 footer/integration tests, and 4 report
preview scenarios rendering to both HTML and PDF. The pipeline is stable under
everything it currently tests.

Running a real DCE-2026 submission through a live server uncovered **six
production issues that the automated tests do not exercise**. All six are
reproduced and attributed to specific lines; none require an architectural
change.

---

## 1. Automated test results

| Suite | Command | Result |
|---|---|---|
| Python | `OSIPI_REQUIRE_FULL_TESTS=1 pytest -q` | **512 passed**, 0 failed, **0 skipped** |
| Frontend smoke | `node tests/frontend_smoke_test.js` | **1014 passed**, 0 failed |
| Frontend ROI DOM | `node tests/frontend_roi_dom_test.js` | **53 passed**, 0 failed |
| Footer logic | `node tests/footer_logic_test.js` | **27 passed**, 0 failed |
| Syntax | `node --check frontend/app.js` | clean |
| Report previews | `python3 scripts/preview_reports.py` | 4 scenarios × HTML + PDF, all rendered |

`tests/e2e/acceptance.spec.js` (Playwright) was **not** run — it requires Docker
and a rendering browser. Everything else ran.

---

## 2. Live end-to-end run

Passing tests only prove the paths the tests cover, so I built a DCE-2026
submission matching `config/validation_rules.yaml` — 16 scans across
`Synthetic/` and `Clinical/`, each with `Ktrans`, `vp`, `ve` and a 4-D
`modelled_st`, plus `methods.txt` and a reference tree containing two masks —
zipped it, and drove it through a live uvicorn instance: upload → validate →
score → five CSV exports → HTML report → PDF report → nine read endpoints →
three negative paths.

**What was confirmed correct:** participant/site/repeat identity resolution,
artifact classification and roles, ROI arithmetic (median, population SD and CoV
all matched hand calculations to float32 precision), the CSV emitting raw ratios
while reports render percentages, JSON round-tripping, PDF blinding,
path-traversal rejection, and every ancillary endpoint.

Driver retained at `outputs/e2e_driver.py`.

---

## 3. Confirmed production bugs

### B1 — A valid DCE-2026 submission is split into two independent submissions at upload · high

**Impact.** A correctly-structured DCE-2026 submission is incorrectly split into
two separate submissions during upload. This prevents dataset identity from
being resolved at all, generates **41 spurious identity errors**, disables
dataset-count validation entirely, duplicates the team across rankings and
reports, and causes the required methods document to be reported missing
because it is discarded during the split.

**Cause.** `detect_batch_boundaries()` treats every top-level directory as a
separate team unless *all* of them appear in `settings.yaml →
ingestion.structural_subdirs` (`backend/services/ingest_service.py:553`). That
list contains `input`, `results`, `maps`, `reference` — but **not the dataset
names** `synthetic` and `clinical` that the DCE challenge config itself defines.

**Reproduction.** The layout the DCE config describes:

```
team_gamma/
    Synthetic/Participant1/Site1/Repeat1/Ktrans.nii.gz
    Clinical/Participant1/Site1/Repeat1/Ktrans.nii.gz
    methods.txt
```

was ingested as two submissions, `team_gamma_Clinical` and
`team_gamma_Synthetic`. The `dataset` level now sits *above* each submission
root, so it can never be recovered — every artifact returned `dataset=None`, and
validation raised 41 × `INCOMPLETE_ARTIFACT_IDENTITY` against a valid
submission. `DATASET_COUNT_MISMATCH` cannot fire either, because the grid was
halved before counting: the completeness check built in Phase 3 is effectively
disabled for the exact layout it was built for.

The identity parser is not at fault — it never sees the dataset directory.

### B2 — Files at the submission root are silently destroyed during batch carve · high

**Impact.** Any file sitting beside the detected batch directories is deleted
without a warning, and the submitter is then blamed for its absence. This is the
mechanism behind the missing methods document in B1, but it applies to *any*
batch upload, not only DCE.

**Cause.** The carve loop moves only the children of each detected batch
directory (`backend/services/ingest_service.py:503`); the staging directory is
then removed wholesale in the `finally`.

**Reproduction.** `methods.txt` — a required artifact under `required_artifacts:
[modelled_st, methods]` — sat at the team root and reached neither submission.
Both reported `REQUIRED_ARTIFACT_MISSING`.

### B3 — An unknown submission id silently returns another submission's data · high

**Impact.** Requesting a report for an id that does not exist returns HTTP 200
and a fully rendered report containing a *different* submission's findings,
labelled with the requested id. A fully unknown id returns a clean, plausible,
entirely empty report rather than a 404.

**Cause.** `_collect_export_ids()` accepts any string without an existence check
(`backend/main.py:2096`), and `_find_validation_files()` filters by **substring**
and takes the newest match (`backend/main.py:709`).

**Reproduction.**

```
_load_validation("team_gamma")  -> team_gamma_Clinical   (42 errors)
_gather_summary("team_gamma")   -> sid: team_gamma, errors: 42, challenge: DCE
```

`team_gamma` does not exist. Note that B1 makes this materially more likely: it
appends `_Clinical` / `_Synthetic` suffixes, so the original team name becomes a
prefix of two real submissions.

### B4 — ROI statistics are duplicated on case-insensitive filesystems (macOS, Windows) · high

**Impact.** Every ROI statistic is computed and reported twice. Because `roi_id`
is derived from the mask filename, the duplicate rows carry *identical*
identity — they are indistinguishable in the CSV export, the HTML ROI table, the
PDF table, and to anything that aggregates them. Ten eligible Ktrans maps
produced **40 ROI records instead of 20**, with 20 exact duplicate
`(path, roi_id)` pairs.

**Cause.** `_reference_masks()` scans both `root/"masks"` and `root/"Masks"`
(`backend/scoring.py:1032`) and de-duplicates by comparing `Path` objects. On a
case-insensitive filesystem both names resolve to the same directory, but the
two `Path` objects are unequal, so every mask is admitted twice.
`_reference_maps_by_type()` has the same flaw at `backend/scoring.py:883`
(`[root/"maps", root/"Maps", root]`). `Path.resolve()` does not normalise case
on macOS, so the `seen` set in `_reference_roots()` offers no protection either.

**Reproduction.** Isolated side by side on two filesystems:

```
case-SENSITIVE  (/tmp, Linux):     masks discovered: 1   reference Ktrans files: 1
case-INSENSITIVE (macOS-backed):   masks discovered: 4   reference Ktrans files: 2
```

This is invisible on Linux, so Docker runs and CI are unaffected — but the
pipeline is documented to run locally, and locally that generally means macOS.

### B5 — The blinded HTML report leaks the team name · medium

**Impact.** A report generated with `blinded=true` displays the team identity,
defeating the purpose of blinded review. Blinding is also inconsistent between
formats: the PDF was clean in the same run.

**Cause.** `_issue_rows_html()` blinds the submission label correctly, but the
"Affected" column renders `Path(affected).name` unblinded
(`backend/main.py:2885`). For submission-level issues, `path` is the
submission's absolute directory, whose basename **is** the submission id, which
is derived from the uploaded filename.

**Reproduction.** From the generated blinded report:

```
<td>No README or SOP file was found…</td><td>team_gamma_Clinical</td>
```

Two occurrences in one report. Separately, the stored `path` is a full local
filesystem path, so reviewer directory structure travels into exported reports.

### B6 — `DUPLICATE_FILENAME` fires on every valid DCE submission · medium

**Impact.** A correct DCE-2026 submission earns one spurious warning per map
type — four here — scaling with map count rather than with anything wrong.
This trains reviewers to ignore the warning channel.

**Cause.** `_duplicate_filename_warnings()` keys on `file_path.name.lower()`
across the whole submission
(`src/osipi_pipeline/validation/validate.py:285`). The DCE-2026 layout
*requires* the same basename in each scan directory. The check predates the
multi-scan layout.

---

## 4. Additional observations

Real, but lower priority or not strictly defects.

**Reference data inside the submission root is counted as submission content.**
`submissions/extracted/<sid>/reference` is production's first reference root
(`backend/scoring.py:849`), but the manifest scans the whole tree, so
`reference/maps/Ktrans.nii.gz` was recorded as an 11th ktrans parameter map and
the two masks as `role="unknown"`. `eligible_artifacts()` correctly excludes
them from ROI statistics, so nothing is *miscalculated* — but the file and map
counts shown to the user are inflated by reference data the team never
submitted.

**Test-suite blind spots.** All six bugs above sit outside the suite's reach for
three structural reasons: (1) nothing tests case-insensitive filesystem
behaviour — `test_roi_end_to_end.py` asserts that records exist and that the
first one is correct, but never *how many*, so on a developer's Mac it passes
while silently producing double the rows; (2) no test ingests a multi-dataset
DCE submission through the uploader — `test_roi_end_to_end.py` writes the
extracted tree directly to disk, bypassing `detect_batch_boundaries` entirely,
which is where B1 and B2 live; (3) no test requests an id that does not exist,
so B3 has never been exercised. The highest-value additions are: assert exact
ROI row counts, add a mask directory under an alternate case, and route one
submission through the real upload endpoint instead of hand-building the tree.

**Missing test dependency.** `trio` was absent although `requirements-test.txt`
specifies `anyio[trio]`. Installing it did not change the count (512 either
way), but without it the async-backend parametrisation silently degrades.

**Code hygiene.** `pyflakes` reports 13 unused imports and 1 unused local. Two
worth naming: `roi_definitions_from_masks` is imported but unused in
`_attach_roi_descriptives` (`backend/scoring.py:942`), and `result_only` is
assigned but never used (`backend/services/validation_service.py:343`) — the
latter is in validation logic and may indicate a dropped branch rather than
dead code.

---

## 5. Not considered code defects

**Float32 precision.** The ROI median returned `0.1249999962747097` against a
hand-computed `0.125`. That is the exact float32 representation of the stored
data, confirming values are read faithfully. My test tolerance was wrong, not
the code.

**Leftover `batch_temp_*` directories and an HTTP 500 during upload.** Both
appear to be artifacts of the sandbox used for this run: the workspace mount
blocks `unlink`, so `shutil.rmtree(..., ignore_errors=True)` fails silently and
`shutil.move` raises. Verified directly — `rm` on a probe file returns
`Operation not permitted`. Not product bugs. One caveat worth checking locally:
`batch_temp_lena_01_exact_single_submission` is dated 2026-07-12, predating this
session, and I could not test deletion here to rule it in or out.

**Path traversal.** `GET /api/nifti/<sid>/../../../../etc/passwd` returns 404
with no file contents. `safe_relative_path()` is the single chokepoint and it
holds.

**Blinding outside B5.** Every other call site uses
`_submission_display_name(..., blinded=blinded)` correctly, and the unblinded
report names the team as expected.

---

## 6. Remediation plan

None of these issues require architectural changes; each appears to be localized
and independently fixable. Proposed order:

1. **B4** — a one-line change in two functions: de-duplicate by
   `os.path.normcase(os.path.realpath(p))` in `_reference_masks` and
   `_reference_maps_by_type`. Lands immediately and removes wrong numbers from
   every macOS run. Pair it with an exact-row-count assertion.
2. **B1 + B2** — same function, and the largest correctness problem. Treat a
   top-level directory set matching configured dataset names as a single
   submission, and refuse to discard root-level files silently.
3. **B3** — anchor submission lookup to an exact id and return 404 on miss.
4. **B5, B6**, then the section 4 observations — small and independent.

Each fix ships with the regression test that would have caught it. I plan to
work in this order unless you'd prefer a different priority.

---

## 7. Fix status

Confirmed independently by a manual upload through the running UI on 2026-08-01
("2 submissions · 67 errors", `DCE Test Clean Clinical` / `DCE Test Clean
Synthetic`). The findings above are retained as the pre-fix evidence record.

| Issue | Status | Change |
|---|---|---|
| B1 dataset split | **Fixed** | `_is_structural_layout()` now also treats configured dataset names as structural, so `Clinical/` + `Synthetic/` stay one submission. Names come from `datasets_by_challenge()`, not a hardcoded list. |
| B2 root files destroyed | **Fixed** | The batch carve copies files beside the batch directories into every carved submission; a submission's own file of the same name wins. |
| §3.7 reference counted as content | **Fixed** | `is_reference_path()` excludes `reference/`, `masks/` etc. from `_build_artifacts()`, using the already-configured `paths.private_path_parts`. File counts elsewhere are unchanged. |
| B6 duplicate false positive | **Fixed** | `duplicate_filename_groups()` keys on resolved scan identity plus filename. The backend validator now calls the same helper instead of keeping its own copy. Flat submissions keep the original behaviour. |
| B4 macOS ROI duplication | **Fixed** | `canonical_path_key()` keys on `(st_dev, st_ino)` — the filesystem's own file identity — with a case-normalised real path as fallback. Used by `_reference_masks()`, `_reference_maps_by_type()` and `_reference_roots()`. Also collapses symlinks and hard links. |
| B3 unknown submission id | **Fixed** | `_find_validation_files()` matches the exact stem instead of a substring, and `_collect_export_ids()` 404s on an id with nothing on disk. The unfiltered listing mode `/api/outputs` depends on is unchanged. |
| B5 blinded HTML leak | **Fixed** | `affected_display()` selects the Affected value structurally in the shared model — safe relative path, else the blinded label — and both renderers call it. `report_filename_tag()` neutralises blinded download filenames. `identity_tokens()`/`reveals_identity()` re-blind anything that survives in a derived form. |

Verified end state for the specified fixture:

```
1 submission: DCE_Test_Clean
├── Clinical      10 scans
├── Synthetic      6 scans
└── methods.txt

16 Ktrans · 16 modelled S(t) · 1 methods document
INCOMPLETE_ARTIFACT_IDENTITY 0 · DUPLICATE_FILENAME 0
REQUIRED_ARTIFACT_MISSING 0 · DATASET_COUNT_MISMATCH 0
```

And after B4, with both mask directory spellings present:

```
masks discovered                2
ROI rows (16 scans x 2 masks)  32     distinct (scan, ROI) pairs  32
```

Guarded by three new test files, each closing the specific blind spot that let
the bug ship:

| File | Tests | Closes |
|---|---|---|
| `test_dce_submission_integrity.py` | 24 | No test routed a submission through the **real uploader** |
| `test_reference_dedup.py` | 12 | No test covered case-insensitive filesystem behaviour |
| `test_submission_lookup.py` | 26 | No test requested an id that does not exist |
| `test_blinded_identity.py` | 45 | No test asserted against the **whole** blinded output |

`test_reference_dedup.py` simulates macOS case-folding with a **symlink** —
two paths, one inode, which is exactly the condition the bug turned on — so it
reproduces on the case-sensitive filesystem CI runs on. It also asserts the
opposite case: two genuinely separate directories must not collapse.

Twenty-seven mutations were run across the eight fixes and all twenty-seven are
now caught. Two initially escaped, and both revealed a weak test rather than a
weak fix:

- Deleting the directory-level deduplication still passed, because the
  file-level pass caught the duplicates anyway. A test now asserts the aliased
  directory is scanned only once, making that guard meaningful.
- Forcing every submission label to blind still passed, because the unblinded
  assertions were satisfied by the metadata table printing team and contact
  separately. A test now pins both directions of the label decision.

Suite is now **618 passed, 1 skipped** (the skip is a POSIX-only branch of the
case-normalisation fallback, which cannot execute on Linux). Report table
numbering is unchanged at Table 1–4 in every preview scenario.

---

## 8. Test residue

Deletion was blocked from the sandbox used for this run. All of the following
are gitignored and safe to remove:

```
submissions/extracted/team_gamma_Clinical/
submissions/extracted/team_gamma_Synthetic/
submissions/extracted/batch_temp_team_gamma/
submissions/incoming/team_gamma.zip
data/outputs/validation/team_gamma_*_validation.json
```
