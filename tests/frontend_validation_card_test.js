// The validation card reports what the submission contains, once.
//
// Two visible defects motivated this: the card said "Result maps provided"
// twice — the submission-type chip and the run-readiness chip carry the same
// words for a result-only submission — and it labelled a complete DCE
// submission "Mixed/Other", which only meant that more than one map type was
// found. For a challenge that defines Ktrans, vp and ve, finding three is the
// expected state, not an ambiguity.
//
// The helpers are executed in a sandbox rather than read as text, so a wrong
// key path or a mislabelled count fails here.

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const SOURCE = fs.readFileSync(
  path.resolve(__dirname, "../frontend/app.js"), "utf8");

let passed = 0;
let failed = 0;

function check(label, condition) {
  if (condition) { passed += 1; console.log(`  OK  ${label}`); }
  else { failed += 1; console.log(`  FAIL ${label}`); }
}

function checkEqual(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (!ok) console.log(`       expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  check(label, ok);
}

// ── Extract the helpers under test, brace-matched ────────────────────────
function extractFunction(name) {
  const start = SOURCE.indexOf(`function ${name}(`);
  if (start === -1) throw new Error(`function ${name} not found in app.js`);
  let depth = 0;
  let seenBrace = false;
  for (let i = start; i < SOURCE.length; i += 1) {
    const ch = SOURCE[i];
    if (ch === "{") { depth += 1; seenBrace = true; }
    else if (ch === "}") {
      depth -= 1;
      if (seenBrace && depth === 0) return SOURCE.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced braces while extracting ${name}`);
}

const context = { console };
vm.createContext(context);
for (const name of ["submissionCounts", "datasetDisplay", "submissionCountSummary",
                    "submissionTypeInfo", "hasRunInstructions", "hasResultMaps"]) {
  vm.runInContext(extractFunction(name), context);
}
const { submissionCounts, datasetDisplay, submissionCountSummary,
        submissionTypeInfo } = context;

// The demo submission: 16 scans across two datasets.
const DCE_CLEAN = {
  submission_id: "DCE_Test_Clean",
  challenge_type: "dce",
  map_type: "Mixed/Other",
  nifti_count: 67,
  has_result_maps: true,
  has_run_instructions: false,
  counts: {
    parameter_maps: 48,
    parameter_maps_by_type: { ktrans: 16, ve: 16, vp: 16 },
    fitted_signals: 16,
    methods_documents: 1,
    unclassified: 0,
    scans: 16,
    scans_by_dataset: { clinical: 10, synthetic: 6 },
    datasets: ["clinical", "synthetic"],
  },
};

// ── Counts ───────────────────────────────────────────────────────────────
console.log("\nCounts");
{
  const c = submissionCounts(DCE_CLEAN);
  checkEqual("parameter maps come from counts, not the file total", c.parameterMaps, 48);
  checkEqual("fitted signals are separate", c.fittedSignals, 16);
  checkEqual("methods documents are separate", c.methodsDocuments, 1);
  checkEqual("scans", c.scans, 16);
  check("the raw 67-file count is not used as a map count", c.parameterMaps !== 67);
}

{
  // A result stored before counts existed must not throw or invent numbers.
  const c = submissionCounts({ submission_id: "legacy", nifti_count: 12 });
  checkEqual("absent counts default to zero", c.parameterMaps, 0);
  checkEqual("absent scans default to zero", c.scans, 0);
  checkEqual("absent dataset map defaults to empty", c.scansByDataset, {});
}

checkEqual("null counts are tolerated", submissionCounts({ counts: null }).scans, 0);
checkEqual("a missing item is tolerated", submissionCounts(undefined).scans, 0);

// ── Dataset display ──────────────────────────────────────────────────────
console.log("\nDataset display");
checkEqual("two datasets render as a coverage label",
           datasetDisplay(DCE_CLEAN), "Clinical + Synthetic");
checkEqual("one dataset renders alone",
           datasetDisplay({ counts: { scans_by_dataset: { clinical: 10 } } }), "Clinical");
checkEqual("datasets are ordered stably, not by insertion",
           datasetDisplay({ counts: { scans_by_dataset: { synthetic: 6, clinical: 10 } } }),
           "Clinical + Synthetic");
checkEqual("no dataset information yields no label",
           datasetDisplay({ counts: { scans_by_dataset: {} } }), "");
check("the label never says Mixed/Other",
      !datasetDisplay(DCE_CLEAN).includes("Mixed"));

// ── Count summary line ───────────────────────────────────────────────────
console.log("\nCount summary");
{
  const summary = submissionCountSummary(DCE_CLEAN);
  checkEqual("every count is present and labelled", summary,
             "16 scans · 48 parameter maps · 16 modelled S-t volumes · 1 methods document");
  check("the summary does not report the raw file count", !summary.includes("67"));
}

checkEqual("singulars are not pluralised",
           submissionCountSummary({ counts: {
             scans: 1, parameter_maps: 1, fitted_signals: 1, methods_documents: 1 } }),
           "1 scan · 1 parameter map · 1 modelled S-t volume · 1 methods document");

checkEqual("absent categories are omitted rather than shown as zero",
           submissionCountSummary({ counts: { scans: 3, parameter_maps: 9 } }),
           "3 scans · 9 parameter maps");

checkEqual("a submission with no counts produces no line",
           submissionCountSummary({}), "");

// ── The duplicate status ─────────────────────────────────────────────────
console.log("\nStatus duplication");
{
  // submissionTypeInfo and the run-readiness text agree for a result-only
  // submission; the card must not print the same words twice.
  const info = submissionTypeInfo(DCE_CLEAN);
  checkEqual("submission type is the result-only label",
             info.label, "Result maps provided");

  // Slice from the chip construction, not from detailChips: typeChipHtml is
  // defined above it, and a slice that starts too late silently passes.
  const chipStart = SOURCE.indexOf("const typeChipHtml");
  const cardSource = SOURCE.slice(chipStart, SOURCE.indexOf("const detailsHtml", chipStart));
  if (chipStart === -1) throw new Error("typeChipHtml anchor not found");
  check("the type chip is suppressed when it repeats the run status",
        /subType\.label === runTxt \? "" :/.test(cardSource));
  check("the run-readiness chip is always rendered",
        cardSource.includes("statusPill(runTxt, runState)"));
}

{
  // A reproducible submission still shows both, because they differ.
  const reproducible = { has_run_instructions: true, has_dockerfile: true };
  checkEqual("reproducible submissions keep their own type label",
             submissionTypeInfo(reproducible).label, "Reproducible code provided");
}

{
  const noteSource = SOURCE.slice(SOURCE.indexOf("const resultOnlyNote"),
                                  SOURCE.indexOf("const noIssueHtml"));
  check("the result-only note carries only the consequence",
        noteSource.includes("No processing run is needed."));
  check("the note no longer repeats that result maps are included",
        !noteSource.includes("already includes result maps"));
}

// ── The meta line ────────────────────────────────────────────────────────
console.log("\nMeta line");
{
  const metaStart = SOURCE.indexOf("const metaHtml = [");
  const metaSource = SOURCE.slice(metaStart, SOURCE.indexOf("const typeChipHtml", metaStart));
  check("dataset coverage is preferred over the detected map label",
        metaSource.includes("datasets ? escapeHtml(datasets) : safeMap"));
  check("the meta line counts parameter maps", metaSource.includes("parameter map"));
  check("the meta line no longer uses the raw NIfTI count directly",
        !/\$\{escapeHtml\(rNiftiCount\)\} map/.test(metaSource));
}

// ── Technical details stay expandable ────────────────────────────────────
console.log("\nTechnical details");
{
  const detailsSource = SOURCE.slice(SOURCE.indexOf("const detailsHtml"),
                                     SOURCE.indexOf("const wrap = _worklistRowEl"));
  check("technical checks remain in a collapsed disclosure",
        SOURCE.includes('<details class="tech-checks-toggle"'));
  check("the technical reference disclosure is retained",
        detailsSource.includes('class="validation-technical-detail"'));
}

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
