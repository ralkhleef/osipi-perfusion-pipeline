#!/usr/bin/env bash
# Remove files nothing references, nothing covers, or that are actively wrong.
#
# Every removal below was checked first: what references the file, what covers
# its content now, and whether it still works. Files that hold content the
# documentation site does not carry were deliberately kept — see the note at
# the bottom.
#
#     bash scripts/tidy_repo.sh
#
# Nothing is pushed. Review `git status` afterwards.

set -euo pipefail
cd "$(dirname "$0")/.."

echo "── Scaffolding, now that the thing it described is built ──"
# The prompt used to specify the documentation website. The site exists;
# nothing references this file.
git rm -q PROMPT_gsoc_website.md

echo "── Superseded by GSoC_WORK_PRODUCT.md ──"
# Compared the proposal against the implementation. Referenced by nothing, and
# the work product now covers what shipped and what did not.
git rm -q docs/PROPOSAL_ALIGNMENT.md

echo "── Broken: targets a directory the application abandoned ──"
# Writes fixtures to data/extracted/. The application reads from
# submissions/extracted/, so this script has been producing submissions the
# app cannot see. src/osipi_pipeline/testing/ replaced it and is used by four
# callers including the demo evidence script.
git rm -q create_test_submission.py

echo "── Placeholder READMEs (0, 22 and 28 bytes) ──"
git rm -q data/sample_submissions/dce_team_alpha/README.md
git rm -q data/sample_submissions/demo_broken_submission/README.md
git rm -q data/sample_submissions/demo_valid_submission/README.md
git rm -q data/sample_submissions/sample_valid_submission/README.md

echo "── Fully covered by the documentation site ──"
# All 11 issue codes and both headings appear on the How it works page.
git rm -q docs/validation_notes.md

echo "── Template screenshots, unused since the gallery was removed ──"
# These came from a template and do not show this application. The Interface
# gallery that displayed them is gone, so nothing references them.
git rm -q docs/assets/images/pipeline-export.png
git rm -q docs/assets/images/pipeline-index.png
git rm -q docs/assets/images/pipeline-scoring.png
git rm -q docs/assets/images/pipeline-upload.png
git rm -q docs/assets/images/pipeline-validation.png
git rm -q docs/assets/images/pipeline-logo.svg

echo
echo "Removed. Remaining developer notes were kept on purpose:"
echo
echo "  docs/execution_notes.md    CLI and API usage, and the Docker security"
echo "                             constraints table (--network none,"
echo "                             --security-opt no-new-privileges, :ro mounts)"
echo "                             — none of which the site documents."
echo
echo "  docs/ingestion_notes.md    States what ingestion does NOT do, which is"
echo "                             the kind of scope boundary that stops a"
echo "                             future developer looking in the wrong layer."
echo
echo "  docs/configuration.md      The backward-compatibility guarantee: every"
echo "                             DCE-2026 field is optional, and expected_maps"
echo "                             is not migrated or replaced. That is why ASL"
echo "                             and DSC were unaffected."
echo
echo "Run the suite, then review:"
echo "  python3 -m pytest -q"
echo "  git status --short"
