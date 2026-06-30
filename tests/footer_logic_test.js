#!/usr/bin/env node
/**
 * Runtime test for the wizard footer / next-step gating.
 * Extracts the real _isStepReady() and _stepBlockedReason() from frontend/app.js
 * and exercises them, so the "warnings must not block Continue" rule is verified
 * against the actual source (not a copy). Pure Node, no packages.
 * Run: node tests/footer_logic_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");

const src = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");

function extract(name) {
  const start = src.indexOf("function " + name + "(");
  if (start < 0) throw new Error("function not found: " + name);
  let i = src.indexOf("{", start), depth = 0, end = -1;
  for (let j = i; j < src.length; j++) {
    const c = src[j];
    if (c === "{") depth++;
    else if (c === "}") { depth--; if (depth === 0) { end = j + 1; break; } }
  }
  return src.slice(start, end);
}

let passed = 0, failed = 0;
function ok(desc, cond) {
  if (cond) { console.log("  ✓  " + desc); passed++; }
  else { console.error("  ✗  " + desc); failed++; }
}

// ── Stubs the extracted functions depend on ────────────────────────────────
let batchState = { validationData: null, uploadData: null, isBatch: false, selectedIds: new Set() };
let state = { submissionId: null };
function issueCount(r, f) {
  if (!r) return 0;
  if (Array.isArray(r[f])) return r[f].length;
  return Number(r[f === "warnings" ? "warning_count" : "error_count"] || 0);
}
// Worst case: pretend run-readiness can't be inferred. The validate gate must
// NOT depend on this — it must rely on error count only.
function inferredRunReadiness() { return "not_runnable"; }

// Stubs for the upload-readiness gate (_canUpload).
let __source = "local";
const __inputs = { "zenodo-input": "", "github-url": "" };
function getSourceType() { return __source; }
function el(id) { return (id in __inputs) ? { value: __inputs[id] } : null; }

// Wrap in parens so eval returns the function expression (declarations don't
// leak out of eval under "use strict"). The returned fns close over the stubs
// (batchState, state, issueCount, getSourceType, el) defined in this scope.
// eslint-disable-next-line no-eval
const _canUpload = eval("(" + extract("_canUpload") + ")");
// eslint-disable-next-line no-eval
const _isStepReady = eval("(" + extract("_isStepReady") + ")");
// eslint-disable-next-line no-eval
const _stepBlockedReason = eval("(" + extract("_stepBlockedReason") + ")");

console.log("\n=== Footer / next-step gating ===\n");

// A — validation warnings + 0 errors still enables Continue to Run
batchState.validationData = { results: [{ passed: true, error_count: 0, warning_count: 2, errors: [], warnings: ["w1", "w2"] }] };
ok("warnings + 0 errors enables Continue to Run", _isStepReady("validate") === true);

// B — "No code files detected" warning does not block when result maps exist
batchState.validationData = { results: [{ passed: true, error_count: 0, errors: [], warnings: ["No code files detected"], has_result_maps: true, run_readiness: "result_only" }] };
ok("'No code files detected' warning does not block with result maps", _isStepReady("validate") === true);

// B2 — result-only that passed with warnings but readiness can't be inferred
batchState.validationData = { results: [{ passed: true, error_count: 0, errors: [], warnings: ["Parameter map type could not be determined"], run_readiness: null }] };
ok("passed result-only (unknown readiness) still enables Continue", _isStepReady("validate") === true);

// C — every submission has blocking errors → blocked (button stays, disabled)
batchState.validationData = { results: [{ passed: false, error_count: 1, errors: ["bad"], warnings: [] }] };
ok("all-errors blocks Continue", _isStepReady("validate") === false);
ok("blocked reason mentions errors + that warnings don't block", /error/i.test(_stepBlockedReason("validate")) && /warning/i.test(_stepBlockedReason("validate")));

// C2 — mixed batch: one passes, one fails → enabled (continue with the good one)
batchState.validationData = { results: [
  { passed: false, error_count: 2, errors: ["e1", "e2"], warnings: [] },
  { passed: true,  error_count: 0, errors: [], warnings: ["w"] },
] };
ok("mixed batch with one passing enables Continue", _isStepReady("validate") === true);

// D — no validation results → blocked with run-validation reason
batchState.validationData = { results: [] };
ok("no results blocks with 'Run validation' reason", _isStepReady("validate") === false && /run validation/i.test(_stepBlockedReason("validate")));

// E — downstream steps never trap the user
["run", "score", "summary"].forEach((s) => ok(s + " Continue always enabled", _isStepReady(s) === true));
ok("export has no next", _isStepReady("export") === false);

// ── Upload readiness: footer CTA enables only when a source is selected ─────
console.log("\n=== Upload step footer gating ===\n");
__source = "local"; state.pendingLocalFiles = null; state.mode = "new"; state.submissionId = null;
ok("upload disabled with no file/source selected", _isStepReady("upload") === false);
state.pendingLocalFiles = [{}];
ok("upload enabled once a local file is chosen", _isStepReady("upload") === true);
state.pendingLocalFiles = null; __source = "zenodo"; __inputs["zenodo-input"] = "";
ok("upload disabled with empty Zenodo input", _isStepReady("upload") === false);
__inputs["zenodo-input"] = "10.5281/zenodo.123";
ok("upload enabled with a Zenodo value", _isStepReady("upload") === true);
__source = "github"; __inputs["github-url"] = "";
ok("upload disabled with empty GitHub URL", _isStepReady("upload") === false);
__inputs["github-url"] = "https://github.com/org/repo";
ok("upload enabled with a GitHub URL", _isStepReady("upload") === true);
__source = "local"; state.pendingLocalFiles = null; state.mode = "edit"; state.submissionId = "sub_1";
ok("upload enabled in edit mode (re-validate existing)", _isStepReady("upload") === true);
state.mode = "new"; state.submissionId = null;

// ── Footer visibility + disabled-not-hidden (source-level guarantees) ───────
console.log("\n=== Footer visibility rules ===\n");
ok("footer is shown on the upload step (shared footer everywhere)", /if \(step === "upload"\)/.test(src) && /footer\.style\.display = "flex"/.test(src));
ok("footer is never display:none in JS (only [hidden] toggling)", !/footer\.style\.display = "none"/.test(src));
ok("upload Back hidden via visibility (keeps CTA right-aligned)", /backBtn\.style\.visibility = "hidden"/.test(src));
ok("upload Next drives the upload action", /if \(!nextBtn\.disabled\) handleSubmit\(\)/.test(src));
ok("blocked step keeps button visible but disabled (never hidden)", /nextBtn\.disabled\s*=\s*!canProceed/.test(src));
ok("validate gate uses error count, not run-readiness", extract("_isStepReady").includes('issueCount(r, "errors") === 0'));
ok("validate gate (in _isStepReady) does not call inferredRunReadiness", !extract("_isStepReady").includes("inferredRunReadiness"));
ok("footer refreshed after async steps (_refreshWizardFooter present)", /function _refreshWizardFooter/.test(src) && (src.match(/_refreshWizardFooter\(\)/g) || []).length >= 3);

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
