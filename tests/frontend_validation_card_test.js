
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
for (const name of ["submissionCounts", "datasetDisplay", "validationScopeDisplay", "submissionCountSummary",
                    "submissionTypeInfo", "hasRunInstructions", "hasResultMaps"]) {
  vm.runInContext(extractFunction(name), context);
}
const { submissionCounts, datasetDisplay, validationScopeDisplay, submissionCountSummary,
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
checkEqual("an unscoped multi-map result omits the detector placeholder",
           validationScopeDisplay({ map_type: "Mixed/Other", counts: {} }), "");
checkEqual("an unscoped specific map keeps its useful label",
           validationScopeDisplay({ map_type: "CBF", counts: {} }), "CBF");

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
  check("the resolved scope label is used in the meta line",
        metaSource.includes("scopeLabel ? escapeHtml(scopeLabel) : null"));
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


console.log("\nMap counts are not file counts");
{
  const fn = (() => {
    const start = SOURCE.indexOf("function _mapCountLabel(");
    let depth = 0, seen = false;
    for (let i = start; i < SOURCE.length; i += 1) {
      if (SOURCE[i] === "{") { depth += 1; seen = true; }
      else if (SOURCE[i] === "}") { depth -= 1; if (seen && depth === 0) return SOURCE.slice(start, i + 1); }
    }
  })();
  const box = { console };
  require("vm").createContext(box);
  require("vm").runInContext(fn, box);

  const dce = { counts: { parameter_maps: 12, fitted_signals: 6 }, nifti_count: 18 };
  check("it reports maps, not the file count",
    /12 parameter maps/.test(box._mapCountLabel(dce)), box._mapCountLabel(dce));
  check("it does not call 18 files 18 maps",
    !/18 maps/.test(box._mapCountLabel(dce)), box._mapCountLabel(dce));
  check("fitted signals are named separately",
    /6 fitted signals/.test(box._mapCountLabel(dce)), box._mapCountLabel(dce));
  check("one map is singular",
    /1 parameter map$/.test(box._mapCountLabel({ counts: { parameter_maps: 1, fitted_signals: 0 } })));
  check("no fitted signals are not mentioned",
    !/fitted/.test(box._mapCountLabel({ counts: { parameter_maps: 4, fitted_signals: 0 } })));
  /* Old cached session state has no counts. Falling back to the file count is
     fine; calling it maps is not, so the fallback says "files". */
  check("without counts it says files rather than guessing",
    box._mapCountLabel({ nifti_count: 18 }) === "18 files",
    box._mapCountLabel({ nifti_count: 18 }));
  check("nothing at all produces no label",
    box._mapCountLabel({}) === null);
}

/* Testing the helper alone passed with the row still using the raw count.
   The row has to be shown to call it. */
check("the review row uses that label rather than the raw file count",
  /_mapCountLabel\(sub\),/.test(SOURCE),
  "_subRowMetaLine no longer uses _mapCountLabel");
check("the raw nifti count is no longer labelled as maps anywhere",
  !/\$\{niftiCount\} map/.test(SOURCE),
  "a raw file count is still being called maps");

check("the single-submission path carries the backend counts through",
  /counts:\s+data\.counts \|\| null/.test(SOURCE),
  "a batch of one would fall back to the raw file count again");

const notes = { console };
vm.createContext(notes);
for (const name of ["_isNote", "_warningsOnly", "issueCount"]) vm.runInContext(extractFunction(name), notes);
const inferred = { severity: "info", code: "DATASET_INFERRED", message: "Treated as synthetic." };
const realWarning = { severity: "warning", code: "MISSING_SCAN", message: "A scan is missing." };
checkEqual("an info note is not counted as a warning",
  notes.issueCount({ warnings: [inferred] }, "warnings"), 0);
checkEqual("real warnings are still counted",
  notes.issueCount({ warnings: [inferred, realWarning, "plain text warning"] }, "warnings"), 2);
check("notes are listed separately in the submission details",
  /<div class="vp-section-heading">Notes<\/div>/.test(SOURCE));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
