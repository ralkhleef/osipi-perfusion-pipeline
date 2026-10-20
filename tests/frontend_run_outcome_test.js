#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");
const indexHtml = fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8");

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
check("it says how many ran", all.text.includes("5 of 5 submissions"), all.text);
check("it points at where the results are", all.text.includes("Results are below"), all.text);

const one = outcome(1, 0, 0, 1);
check("one submission is not called 'submissions'",
  one.text.includes("1 submission analysed"), one.text);

console.log("\nA partial run does not read as success");
const partial = outcome(3, 0, 2, 5);
check("tone is error when anything failed", partial.tone === "err", partial.tone);
check("it does not claim completion", !/complete\./.test(partial.text), partial.text);
check("it gives both numbers", partial.text.includes("3 of 5") && partial.text.includes("2 failed"),
  partial.text);
check("it says where to look for the reason", partial.text.includes("details below"), partial.text);

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
check("the outcome banner is declared in the page, not created on demand",
  /id="score-run-outcome"/.test(indexHtml), "score-run-outcome is not in index.html");
check("the outcome is announced to screen readers",
  /id="score-run-outcome"[^>]*aria-live="polite"/.test(indexHtml), "aria-live missing");
check("a new run clears the previous outcome first",
  appJs.includes("_clearRunOutcome()"));

check("a run also raises a popup that has to be dismissed",
  /^\s*_openRunResult\(tone, text, isOfficial\);/m.test(appJs),
  "_showRunOutcome does not open the popup");
check("the popup exists in the page",
  /id="run-result-modal"/.test(indexHtml), "run-result-modal is not in index.html");
check("the popup starts hidden",
  /id="run-result-modal"[^>]*\shidden/.test(indexHtml), "the popup would show on load");
check("the popup names the outcome rather than always saying it worked",
  /RUN_RESULT_HEADINGS\s*=\s*\{[\s\S]{0,240}err:/.test(appJs),
  "there is no distinct heading for a failed run");
check("the popup only offers Continue to Export when a run succeeded",
  /continueBtn\.style\.display = tone === "ok"/.test(appJs),
  "a failed run would still invite you onward");
check("the popup can be dismissed with Escape",
  /event\.key === "Escape"[\s\S]{0,60}_closeRunResult/.test(appJs),
  "Escape does not close the popup");
check("the outcome is reported even if the run throws",
  /finally\s*\{[\s\S]{0,400}_showRunOutcome/.test(appJs),
  "_showRunOutcome is not in the finally block");
check("a single score returns its status so a batch can be counted",
  /return String\(data\.status/.test(appJs));
check("an empty provider list hides the expander instead of showing an empty one",
  /providerDetails\.hidden = provs\.length === 0/.test(appJs));


console.log("\nThe button always does something");
check("the button is wired even when nothing is configured",
  /function _wireRunButton\(\{[^}]*live[^}]*\}\)[\s\S]{0,600}if \(!live\) \{[\s\S]{0,200}addEventListener\("click"/.test(appJs),
  "an unconfigured card leaves the button dead");
check("the button is wired in known places only (status card and new reference data)",
  (appJs.match(/_wireRunButton\(/g) || []).length === 3,
  "there is more than one way to wire the run button");
check("an unconfigured click is told apart from having no submissions",
  /tally\.unconfigured/.test(appJs), "the two cases share one message");
check("it says the comparison does not need a provider",
  /QC and reference comparisons remain available below/.test(appJs),
  "the message does not say what still works");
{
  const run = sandbox._runOutcomeText({ scored: 0, skipped: 0, failed: 0, total: 0, unconfigured: true }, false);
  check("the unconfigured message does not tell you to upload again",
    !/[Uu]pload/.test(run.text), `wrong remedy: ${run.text}`);
  check("the unconfigured message is a warning, not a success",
    run.tone === "warn", `tone was ${run.tone}`);
}

console.log("\nNo provider is not the same as nothing to run");
{
  const ran = sandbox._runOutcomeText(
    { scored: 0, skipped: 0, failed: 0, reference: 6, total: 6, unconfigured: true }, false);
  check("a comparison that ran is reported as a success", ran.tone === "ok", ran.tone);
  check("it says how many were compared",
    ran.text.includes("6 of 6 submissions with the reference data"), ran.text);
  check("it points to the results", /Results are below/.test(ran.text), ran.text);
  check("it does not report the absent provider as a failure",
    !/failed/i.test(ran.text), ran.text);

  const partial = sandbox._runOutcomeText(
    { scored: 0, skipped: 0, failed: 2, reference: 4, total: 6, unconfigured: true }, false);
  check("a comparison with failures is an error, not a success",
    partial.tone === "err", partial.tone);
  check("both counts are given", partial.text.includes("4 of 6") && partial.text.includes("2 failed"),
    partial.text);

  const why = sandbox._runOutcomeText(
    { scored: 0, skipped: 0, failed: 0, reference: 0, total: 0, unconfigured: true,
      reason: "no reference maps for this challenge were found in the reference data folder" }, false);
  check("with nothing to compare it says why, not just what still works",
    why.text.includes("no reference maps for this challenge"), why.text);
  check("and that is still a warning", why.tone === "warn", why.tone);
}

console.log("\nThe Score step runs on reference data alone");
check("reference readiness is computed from the status payloads",
  /function _referenceComparisonSummary\(/.test(appJs),
  "nothing works out whether a comparison is possible");
check("a comparison having run makes the button live",
  /const canCompare = !isConfigured && !!\(reference && reference\.possible\)/.test(appJs),
  "the button is still gated on a provider");
check("the run loop counts comparisons rather than calling them skipped",
  /_REFERENCE_COMPARED_STATUSES\.has\(_referenceStatusFor\(sid\)\)\) tally\.reference/.test(appJs),
  "a completed comparison would be tallied as nothing configured");
check("only a genuine comparison status counts",
  /_REFERENCE_COMPARED_STATUSES = new Set\(\["available", "partial_reference_scoring"\]\)/.test(appJs),
  "some other status could be mistaken for a completed comparison");
check("a compared row is not labelled as needing setup",
  /badgeTxt = referenceCompared \? "Compared to reference" : "Needs setup"/.test(appJs),
  "a finished comparison still reads as unconfigured");
check("a compared row can be exported",
  /if \(referenceCompared\) \{[\s\S]{0,300}_enableScoringExport\(\)/.test(appJs),
  "results exist but cannot leave the page");
check("an empty Score step says why rather than only what still works",
  /Reference comparison unavailable: \$\{reason\}/.test(appJs),
  "the reviewer is told what works, never what does not");

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
