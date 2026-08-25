#!/usr/bin/env node
/**
 * What the interface says after a scoring or analysis run.
 *
 * The run used to end in silence. The button went back to reading "Run
 * Analysis", which is what it read before, and the only evidence of anything
 * having happened was a table further down the page. A run that failed on
 * every submission looked identical to one that succeeded, and both looked
 * identical to never having pressed the button.
 *
 * `_runOutcomeText` turns the tally into one sentence and a tone. It is pure,
 * so the wording can be pinned here directly. The cases that matter are the
 * partial ones: "3 of 5 analysed" and "5 of 5 analysed" are different results
 * and the sentence has to make that impossible to miss.
 *
 * Run: node tests/frontend_run_outcome_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");

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

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext(extractFunction("_runOutcomeText"), sandbox);

const outcome = (scored, skipped, failedCount, total, official = false) =>
  sandbox._runOutcomeText({ scored, skipped, failed: failedCount, total }, official);

console.log("\nA clean run says so plainly");
const all = outcome(5, 0, 0, 5);
check("tone is success", all.tone === "ok", all.tone);
check("it says the run completed", all.text.includes("Analysis complete"), all.text);
check("it says how many ran", all.text.includes("All 5 submissions"), all.text);
check("it points at where the results are", all.text.includes("table"), all.text);

const one = outcome(1, 0, 0, 1);
check("one submission is not called 'submissions'",
  one.text.includes("1 submission analysed"), one.text);

console.log("\nA partial run does not read as success");
const partial = outcome(3, 0, 2, 5);
check("tone is error when anything failed", partial.tone === "err", partial.tone);
check("it does not claim completion", !/complete\./.test(partial.text), partial.text);
check("it gives both numbers", partial.text.includes("3 of 5") && partial.text.includes("2 failed"),
  partial.text);
check("it says where to look for the reason", partial.text.includes("table"), partial.text);

console.log("\nNothing configured is not the same as failure");
const skipped = outcome(0, 4, 0, 4);
check("tone is a warning, not an error", skipped.tone === "warn", skipped.tone);
check("it says the run did not happen", skipped.text.includes("did not run"), skipped.text);
check("it explains why", skipped.text.includes("nothing configured"), skipped.text);

const mixed = outcome(3, 1, 0, 4);
check("a partly skipped run is a warning", mixed.tone === "warn", mixed.tone);
check("a partly skipped run reports both counts",
  mixed.text.includes("3 of 4") && mixed.text.includes("1 skipped"), mixed.text);

console.log("\nNothing to run at all");
const empty = outcome(0, 0, 0, 0);
check("tone is a warning", empty.tone === "warn", empty.tone);
check("it says what to do next", empty.text.includes("Upload"), empty.text);
check("it does not report a count of zero as a result",
  !empty.text.includes("0 of 0"), empty.text);

console.log("\nOfficial scoring is named as such");
const official = outcome(2, 0, 0, 2, true);
check("official runs say scoring, not analysis",
  official.text.startsWith("Scoring complete"), official.text);
check("unofficial runs say analysis",
  outcome(2, 0, 0, 2, false).text.startsWith("Analysis complete"));
check("an official failure is also named correctly",
  outcome(1, 0, 1, 2, true).text.startsWith("Scoring finished with problems"),
  outcome(1, 0, 1, 2, true).text);

console.log("\nWiring");
check("the outcome is announced to screen readers",
  appJs.includes('setAttribute("aria-live"'), "aria-live missing");
check("a new run clears the previous outcome first",
  appJs.includes("_clearRunOutcome()"));
check("the outcome is reported even if the run throws",
  /finally\s*\{[\s\S]{0,400}_showRunOutcome/.test(appJs),
  "_showRunOutcome is not in the finally block");
check("a single score returns its status so a batch can be counted",
  /return String\(data\.status/.test(appJs));
check("an empty provider list hides the expander instead of showing an empty one",
  /providerDetails\.hidden = provs\.length === 0/.test(appJs));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
