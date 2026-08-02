#!/usr/bin/env bash
# Four commits, grouped so that each contains whole files.
#
# A stale .git/index.lock from 2026-07-29 blocks all git writes. Clear it first
# (verify no git process is actually running):
#
#     rm -f .git/index.lock
#
# Then run this from the repository root:
#
#     bash scripts/commit_plan.sh
#
# The changes are interleaved within backend/main.py and the library modules,
# so a per-hunk split would risk a non-building commit. Grouping by whole file
# means commits 1 and 2 carry the library-side halves of two fixes; the commit
# messages say so rather than pretending otherwise.

set -euo pipefail
cd "$(dirname "$0")/.."

git rev-parse --git-dir > /dev/null

# ── 1. DCE-2026 library support ──────────────────────────────────────────────
git add src/osipi_pipeline config/validation_rules.yaml \
        backend/services/roi_descriptive_service.py
git commit -m "feat(dce): DCE-2026 config, identity, completeness and ROI statistics

Challenge configuration gains required/optional maps, required artifacts,
artifact types, dataset grids and filename identity patterns, all strictly
validated.

Adds the normalised SubmissionArtifact model, directory-first identity
resolution with a configurable filename fallback, completeness validation
with eleven issue codes, and within-ROI descriptive statistics (median,
population SD, CoV) scoped to one map, one ROI, one scan.

Also carries the library-side halves of two fixes that live in these files:
reference and mask directories are excluded from submission artifacts, and
duplicate-filename detection is scoped by resolved scan identity rather than
by basename alone."

# ── 2. Report and interface presentation ─────────────────────────────────────
git add backend/services/report_branding.py backend/services/report_figures.py \
        frontend/styles.css frontend/index.html frontend/app.js \
        frontend/assets/logo-lockup.png
git commit -m "feat(report): journal-register reports and ROI results section

HTML and PDF reports share one branding module: serif typography, booktabs
rules, small-caps sections, status as a coloured dot, and the official OSIPI
lockup. Fonts are restricted to the PDF base-14 set so a standalone report
renders identically offline.

Bland-Altman and identity figures are described once as geometry and rendered
by both an SVG and a ReportLab backend, so the two formats cannot diverge.

The interface adopts the same register for issue lists, status chips and
section headings, and gains the ROI Ktrans statistics section wired into the
canonical Results Summary render path."

# ── 3. Production fixes ──────────────────────────────────────────────────────
git add backend/main.py backend/scoring.py \
        backend/services/ingest_service.py \
        backend/services/pdf_report_service.py \
        backend/services/validation_service.py
git commit -m "fix: seven production defects found by live manual testing

None of these were reachable by the automated suite. Each is documented with
reproduction in CODE_WALKTHROUGH.md.

- Dataset directories no longer split one submission into two, which had made
  dataset identity unresolvable and silently disabled dataset-count checks.
- Files beside the batch directories reach every carved submission instead of
  being deleted with the staging area.
- Submission lookup is exact; an id that is a prefix of a real one no longer
  returns that other submission's results, and an unknown id is a 404.
- Reference maps and ROI masks are deduplicated by physical file identity, so
  case-insensitive filesystems no longer double every ROI statistic.
- Blinded reports select the Affected value in the shared model, so HTML, PDF,
  the plain-text fallback and download filenames share one blinding decision.
- Reference data staged inside a submission is no longer counted as submitted
  content."

# ── 4. Tests, tooling and documentation ──────────────────────────────────────
git add tests scripts README.md docs \
        CODE_WALKTHROUGH.md CHANGELOG.md PROMPT_gsoc_website.md
git commit -m "test: regression and mutation coverage for all seven fixes

Three new suites, each closing the specific blind spot that let a defect ship:

- test_dce_submission_integrity.py routes a submission through the real
  uploader rather than writing the extracted tree by hand.
- test_reference_dedup.py covers case-insensitive filesystem behaviour,
  simulating macOS case-folding with a symlink so it reproduces on Linux.
- test_blinded_identity.py asserts a hostile team name is absent from the
  whole of every blinded output, not only the visible table cell.

27 mutations run across the seven fixes; all caught. Two initially escaped and
both exposed a weak test rather than a weak fix.

Adds scripts/demo_evidence.py, which regenerates the DCE_Test_Clean evidence
bundle, and scripts/preview_reports.py for report rendering checks. Removes
three superseded test-findings documents and the demo folder; CODE_WALKTHROUGH
and CHANGELOG replace them."

echo
git --no-pager log --oneline -4
echo
git status --short | head
