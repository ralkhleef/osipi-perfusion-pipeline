#!/usr/bin/env node
/**
 * The Score step runs on reference data alone.
 *
 * A scoring provider and a comparison against the organisers' ground truth are
 * two different things, and only the first one needs configuring. The DCE
 * challenge has reference maps and masks and no provider, and in that state
 * the pipeline still produces bias, RMSE, error CoV and the ROI descriptive
 * tables. The Score step used to hide its card, hide its table, and answer a
 * press of Run Analysis with "there is nothing for this button to run" -- a
 * statement that was simply false, and that a reviewer had no way to see past.
 *
 * The payload in tests/fixtures_no_provider_status.json is not invented: it
 * was recorded from the real API by scripts/verify_run_without_provider.py,
 * which drives an upload through validation and scoring with mode="none" and
 * checks that the injected bias comes back out.
 *
 * Run: node tests/frontend_reference_run_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");
const realPayload = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, "fixtures_no_provider_status.json"), "utf8"));

let passed = 0;
let failed = 0;

function check(desc, cond, extra = "") {
  if (cond) { console.log(`  OK  ${desc}`); passed++; }
  else { console.error(`  FAIL  ${desc}${extra ? ` — ${extra}` : ""}`); failed++; }
}

function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function not found: ${name}`);
  let depth = 0, seen = false;
  for (let i = start; i < appJs.length; i += 1) {
    if (appJs[i] === "{") { depth += 1; seen = true; }
    else if (appJs[i] === "}") { depth -= 1; if (seen && depth === 0) return appJs.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

function extractConst(name) {
  const start = appJs.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  const end = appJs.indexOf(";", start);
  return appJs.slice(start, end + 1);
}

const sandbox = { console, _scoreCache: {} };
vm.createContext(sandbox);
[
  extractConst("_REFERENCE_COMPARED_STATUSES"),
  extractConst("_REFERENCE_REASONS"),
  extractFunction("_scorePayload"),
  extractFunction("_referenceScoringOf"),
  extractFunction("_referenceReasonText"),
  extractFunction("_referenceComparisonSummary"),
].forEach((src) => vm.runInContext(src, sandbox));

console.log("\nThe real unconfigured payload is recognised as runnable");
{
  const summary = sandbox._referenceComparisonSummary([realPayload]);
  check("the comparison is seen as possible", summary.possible === true,
    JSON.stringify(summary));
  check("it counts the submission as compared", summary.compared === 1, String(summary.compared));
  check("it carries the mask count through for the card", summary.masks === 2,
    String(summary.masks));
  check("it has nothing to complain about", summary.reasons.length === 0,
    summary.reasons.join("; "));
}

console.log("\nA missing reference is reported as a reason, not a blank");
{
  const summary = sandbox._referenceComparisonSummary([
    { status: "not_configured", reference_scoring: { status: "reference_not_available" } },
  ]);
  check("the comparison is not possible", summary.possible === false);
  check("the reason names the reference data folder",
    /reference data folder/.test(summary.reasons[0] || ""), summary.reasons.join("; "));
  check("it is one reason, not a list of statuses", summary.reasons.length === 1);
}

console.log("\nPartial coverage is stated rather than averaged away");
{
  const summary = sandbox._referenceComparisonSummary([
    realPayload,
    { status: "not_configured", reference_scoring: { status: "reference_not_available" } },
  ]);
  check("a single match still makes the run worth offering", summary.possible === true);
  check("both halves are counted",
    summary.compared === 1 && summary.unavailable === 1 && summary.total === 2,
    JSON.stringify(summary));
  check("the shortfall keeps its reason", summary.reasons.length === 1);
}

console.log("\nOnly a genuine comparison counts");
{
  /* An error status must never be mistaken for a completed comparison; that
     would light up the card and export a table that does not exist. */
  ["scoring_error", "not_compared", "", "pending"].forEach((status) => {
    const summary = sandbox._referenceComparisonSummary([
      { status: "not_configured", reference_scoring: { status } },
    ]);
    check(`${status || "(empty)"} is not treated as compared`, summary.possible === false);
  });
  const partial = sandbox._referenceComparisonSummary([
    { status: "not_configured", reference_scoring: { status: "partial_reference_scoring" } },
  ]);
  check("a partial comparison is still a comparison", partial.possible === true);
}

console.log("\nThe payload is read wherever the API puts it");
{
  /* /api/scoring-status puts it at the top level; a custom-package result
     nests it under score_result. Both are real shapes. */
  const nested = sandbox._referenceComparisonSummary([
    { status: "scored", score_result: { reference_scoring: { status: "available", mask_count: 3 } } },
  ]);
  check("a nested score_result is read", nested.possible === true);
  check("and its mask count comes through", nested.masks === 3, String(nested.masks));
  check("an empty payload does not throw",
    sandbox._referenceComparisonSummary([{}]).possible === false);
  check("no payloads at all does not throw",
    sandbox._referenceComparisonSummary([]).possible === false);
  check("undefined does not throw",
    sandbox._referenceComparisonSummary(undefined).possible === false);
}

console.log("\nThe step footer says which button comes first");
{
  /* Run Analysis and Continue to Export sit side by side, and Continue is the
     one that reads as the next step. Without an order stated, it was pressed
     first and the export described a run that had not happened. */
  const guidance = extractFunction("_scoreStepGuidance");
  check("nothing runnable does not demand a run",
    /Nothing here needs running/.test(guidance), guidance);
  check("a runnable step names the order",
    /Run Analysis first, then Continue to Export/.test(guidance), guidance);
  check("it stops saying so once the run has happened",
    /_scoreAlreadyRan\(\)/.test(guidance), guidance);
  check("having run is judged by results, not by a flag someone forgot to clear",
    /reference_scoring/.test(extractFunction("_scoreAlreadyRan")));
}

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
