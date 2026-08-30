#!/usr/bin/env node
/**
 * The wizard running the steps that need no decision.
 *
 * Three of the six steps ask the reviewer for nothing: validation, the run
 * step and QC all compute from what was already uploaded. Clicking through
 * them is not review, it is just clicking.
 *
 * The whole risk of automating them is skipping something a person needed to
 * see, so `_autoAdvanceBlocker` is the function that matters and it is pure.
 * The rule it encodes:
 *
 *   errors   stop the wizard, because a failed submission is the thing a
 *            reviewer is here to look at
 *   warnings do not, because a warning is advisory by definition, and
 *            stopping on one would mean stopping on almost every real
 *            submission, which teaches people to ignore the interface
 *
 * Run: node tests/frontend_auto_advance_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");
const indexHtml = fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8");
const css = fs.readFileSync(path.resolve(__dirname, "../frontend/styles.css"), "utf8");

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
vm.runInContext(extractFunction("_autoAdvanceBlocker"), sandbox);
vm.runInContext(extractFunction("_autoAdvanceNote"), sandbox);

const ok = (n) => Array.from({ length: n }, () => ({ passed: true, warnings: [] }));
const bad = (n) => Array.from({ length: n }, () => ({ passed: false, warnings: [] }));
const warn = (n) => Array.from({ length: n }, () => ({ passed: true, warnings: ["x"] }));

console.log("\nA clean run carries on");
check("one clean submission does not stop it",
  sandbox._autoAdvanceBlocker(ok(1)) === null, sandbox._autoAdvanceBlocker(ok(1)));
check("many clean submissions do not stop it",
  sandbox._autoAdvanceBlocker(ok(12)) === null);

console.log("\nErrors stop it, and say how many");
check("a single failure stops it", sandbox._autoAdvanceBlocker(bad(1)) !== null);
check("a single failure is not called 'all 1 submissions'",
  !/All 1/.test(sandbox._autoAdvanceBlocker(bad(1))), sandbox._autoAdvanceBlocker(bad(1)));
check("every one failing says so",
  /All 3/.test(sandbox._autoAdvanceBlocker(bad(3))), sandbox._autoAdvanceBlocker(bad(3)));
{
  const mixed = ok(7).concat(bad(3));
  const msg = sandbox._autoAdvanceBlocker(mixed);
  check("a partial failure gives both numbers",
    /3 of 10/.test(msg), msg);
}

console.log("\nWarnings do NOT stop it");
/* This is the decision most likely to be reversed by someone who has not
   thought it through. The challenge lead's own ASL submission raises a
   warning for having no methods document, so stopping on warnings would stop
   on the one real submission there is. */
check("a warning alone does not stop it",
  sandbox._autoAdvanceBlocker(warn(1)) === null, sandbox._autoAdvanceBlocker(warn(1)));
check("many warnings still do not stop it",
  sandbox._autoAdvanceBlocker(warn(9)) === null);
check("but warnings are mentioned rather than swallowed",
  /warning/i.test(sandbox._autoAdvanceNote(warn(2))), sandbox._autoAdvanceNote(warn(2)));
check("one warning is not called 'warnings'",
  /One submission has warnings/.test(sandbox._autoAdvanceNote(warn(1))),
  sandbox._autoAdvanceNote(warn(1)));
check("no warnings produces no note",
  sandbox._autoAdvanceNote(ok(3)) === "");

console.log("\nNothing to validate is not success");
check("an empty result set stops it",
  sandbox._autoAdvanceBlocker([]) !== null);
check("a missing result set stops it",
  sandbox._autoAdvanceBlocker(undefined) !== null);

console.log("\nIt can always be stopped");
check("there is a stop control in the page",
  /id="auto-advance-stop"/.test(indexHtml), "no stop button");
check("pressing stop cancels the run in progress",
  /_autoAdvanceCancelled = true/.test(appJs), "stop does not cancel");
check("the cancel flag is honoured between steps",
  (appJs.match(/if \(_autoAdvanceCancelled\) return;/g) || []).length >= 2,
  "cancelling would not take effect until the end");
check("stopping is remembered rather than asked again every time",
  /_setAutoAdvance\(false\)/.test(appJs), "the preference is not saved");
check("the preference can be off",
  /localStorage\.getItem\(AUTO_ADVANCE_KEY\) !== "off"/.test(appJs));
check("storage being unavailable does not disable the feature",
  /catch \(_\) \{ return true; \}/.test(appJs),
  "a browser blocking storage would silently turn this off");

console.log("\nIt cannot strand the reviewer");
check("a failure mid-run returns to the validate step",
  /catch \(error\)[\s\S]{0,400}goToStep\("validate"\)/.test(appJs),
  "an error would leave a half-built step on screen");
/* Scoped to the function itself. Checking the whole file passed even with the
   await removed here, because another call site elsewhere also awaits it. */
const autoAdvanceBody = (() => {
  const start = appJs.indexOf("async function _autoAdvanceToQc(");
  if (start < 0) throw new Error("_autoAdvanceToQc not found");
  let depth = 0, seen = false;
  for (let i = start; i < appJs.length; i += 1) {
    if (appJs[i] === "{") { depth += 1; seen = true; }
    else if (appJs[i] === "}") { depth -= 1; if (seen && depth === 0) return appJs.slice(start, i + 1); }
  }
  throw new Error("unbalanced braces in _autoAdvanceToQc");
})();

check("the run step is awaited before the next one starts",
  /await renderRunStep\(\)/.test(autoAdvanceBody), "steps could render out of order");
check("QC is awaited too",
  /await renderScoreStep\(\)/.test(autoAdvanceBody));

/* Reloading a page is not an instruction to run anything. renderValidateStep
   is also how saved state is rebuilt on reload, and carrying the wizard
   forward from there fought the restore's own navigation: two things deciding
   which step you land on, from a state that is only a summary. */
console.log("\nReloading does not re-run the wizard");
check("the restore path is told it is a restore",
  /renderValidateStep\(synthData, \/\*isSingleMode=\*\/undefined, \/\*fromRestore=\*\/true\)/.test(appJs),
  "session restore still looks like a fresh validation");
check("auto-advance is skipped when restoring",
  /if \(!fromRestore\) \{[\s\S]{0,120}_autoAdvanceToQc/.test(appJs),
  "a page reload would advance the wizard again");
check("a fresh validation still advances",
  /_autoAdvanceToQc\(results\)/.test(appJs));


/* ── Run Analysis sits beside Continue to Export ───────────────────────────
   It lived in the status card at the top of QC and Preview, far from where a
   reviewer looks for the next action, and pressing it changed nothing visible
   up there, so it read as broken even when it had run. */
console.log("\nRun Analysis is in the step footer");
{
  const body = (() => {
    const start = appJs.indexOf("function _placeRunAnalysisInFooter(");
    if (start < 0) return "";
    let depth = 0, seen = false;
    for (let i = start; i < appJs.length; i += 1) {
      if (appJs[i] === "{") { depth += 1; seen = true; }
      else if (appJs[i] === "}") { depth -= 1; if (seen && depth === 0) return appJs.slice(start, i + 1); }
    }
    return "";
  })();
  check("the placement helper exists", body.length > 0);
  check("it applies to the score step only",
    /step !== "score"/.test(body), "every step would grow a Run button");
  check("it goes before the primary action",
    /right\.insertBefore\(button, primary\)/.test(body),
    "Run Analysis would sit after Continue to Export");
  check("the button is moved, not copied",
    !/cloneNode/.test(body),
    "a copy would leave a dead button with no handler behind");
  check("leaving the step returns it to the card",
    /home\.insertBefore\(button/.test(body),
    "navigating away would lose the button entirely");
  check("the footer wiring calls it",
    /_placeRunAnalysisInFooter\(step, row\);/.test(appJs));
}

console.log("\nPresentation");
check("the toast is declared in the page, not created on demand",
  /id="auto-advance"/.test(indexHtml));
check("it starts hidden",
  /id="auto-advance"[^>]*\shidden/.test(indexHtml), "it would flash on load");
check("it is announced to screen readers",
  /id="auto-advance"[^>]*aria-live="polite"/.test(indexHtml));
check("the animation respects prefers-reduced-motion",
  /prefers-reduced-motion[\s\S]{0,160}animation: none/.test(css),
  "a pulsing dot with no way to turn it off");

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
