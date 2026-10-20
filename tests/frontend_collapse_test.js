#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");
const html = fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8");
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

const listeners = [];
const sandbox = {
  console,
  document: {
    addEventListener(type, fn, capture) { listeners.push({ type, fn, capture }); },
  },
};
vm.createContext(sandbox);
vm.runInContext(
  `const _collapsedFolds = new Set();
   ${extractFunction("_foldAttrs")}
   ${extractFunction("_watchFolds")}
   _watchFolds();
   this._collapsedFolds = _collapsedFolds;
   this._foldAttrs = _foldAttrs;`,
  sandbox);

const toggle = listeners.find((l) => l.type === "toggle");

console.log("\nThe listener is wired the only way that works");
check("something listens for toggle", !!toggle, "no toggle listener registered");
check("the module registers it on load",
  /^_watchFolds\(\);$/m.test(appJs),
  "the handler exists but nothing ever hooks it up");
/* toggle does not bubble. Registered without capture it would never fire for
   a section nested inside the panel, and every fold would silently forget. */
check("it listens in the capture phase", toggle && toggle.capture === true,
  "toggle does not bubble, so a listener on document only sees it in capture");

const fire = (fold, open) => toggle.fn({ target: { dataset: { fold }, open } });

console.log("\nClosing a section is remembered, opening it forgets again");
check("a section starts open", / open$/.test(sandbox._foldAttrs("qc-results")),
  sandbox._foldAttrs("qc-results"));
fire("qc-results", false);
check("after closing it, the next render omits open",
  !/ open/.test(sandbox._foldAttrs("qc-results")), sandbox._foldAttrs("qc-results"));
check("the key is still emitted so the listener keeps matching it",
  /data-fold="qc-results"/.test(sandbox._foldAttrs("qc-results")),
  sandbox._foldAttrs("qc-results"));
fire("qc-results", true);
check("reopening it forgets the collapse",
  / open$/.test(sandbox._foldAttrs("qc-results")), sandbox._foldAttrs("qc-results"));

console.log("\nSections are remembered separately");
fire("map-previews", false);
check("closing one does not close the others",
  / open$/.test(sandbox._foldAttrs("qc-results")) &&
  !/ open/.test(sandbox._foldAttrs("map-previews")),
  `qc=${sandbox._foldAttrs("qc-results")} maps=${sandbox._foldAttrs("map-previews")}`);
fire("reference-comparison", false);
check("a third is independent again", sandbox._collapsedFolds.size === 2,
  [...sandbox._collapsedFolds].join(","));

console.log("\nUnrelated details on the page are left alone");
const before = sandbox._collapsedFolds.size;
toggle.fn({ target: { dataset: {}, open: false } });
toggle.fn({ target: null });
toggle.fn({ target: { open: false } });
check("a details with no fold key is ignored",
  sandbox._collapsedFolds.size === before, `${before} -> ${sandbox._collapsedFolds.size}`);

console.log("\nEvery foldable section actually asks to be remembered");
const foldTags = appJs.match(/<details[^>]*\bsdc-fold\b[^>]*>/g) || [];
check("the Score & Preview folds exist", foldTags.length >= 4, `${foldTags.length} found`);
foldTags.forEach((tag) => {
  const key = (tag.match(/class="([^"]*)"/) || [])[1] || tag.slice(0, 40);
  check(`a fold carries a key: ${key.split(" ").slice(-2).join(" ")}`,
    /_foldAttrs\(/.test(tag), "hardcoded open state would reset on the next render");
});
["qc-results", "map-previews", "reference-comparison"].forEach((key) => {
  check(`section keyed '${key}'`, appJs.includes(`_foldAttrs("${key}")`));
});
check("both renders of the preview section use the same key",
  (appJs.match(/_foldAttrs\("map-previews"\)/g) || []).length === 2,
  "the error path would reopen a section the reviewer closed");

console.log("\nThe headers are summaries, not divs with a click handler");
const sdcHeadDivs = (appJs.match(/<div class="sdc-head">/g) || []).length;
check("no folding section left a div header behind", sdcHeadDivs === 0,
  `${sdcHeadDivs} still present`);
check("the static cards fold too",
  (html.match(/<details[^>]*\bqc-fold\b/g) || []).length >= 2,
  "the Submissions and ROI tables are the tallest things on the step");
check("the ROI table folds", /id="roi-descriptive-card"[^>]*>/.test(html) &&
  /<details[^>]*id="roi-descriptive-card"/.test(html));
check("the submissions table folds",
  /<details[^>]*id="score-table-card"/.test(html));
check("both start open, because they are the point of the step",
  (html.match(/<details class="pg-card qc-fold"[^>]*\sopen\s/g) || []).length === 2);

console.log("\nPresentation");
check("the default triangle is suppressed",
  /qc-fold > summary::-webkit-details-marker \{ display: none; \}/.test(css) &&
  /sdc-fold > summary\.sdc-head::-webkit-details-marker \{ display: none; \}/.test(css),
  "Safari would draw its own marker beside the drawn one");
check("the arrow rotates when open",
  /qc-fold\[open\] > summary::after \{ transform: rotate\(90deg\); \}/.test(css));

const closedCard = (css.match(
  /\.qc-fold:not\(\[open\]\) > summary\.pg-card-header \{[^}]*\}/) || [""])[0];
check("the closed card cancels the rule under its header",
  /border-bottom: none !important/.test(closedCard), closedCard);
check("and the gap the margin would leave",
  /margin-bottom: 0 !important/.test(closedCard), closedCard);
const foldSummary = (css.match(/\.qc-fold > summary \{[^}]*\}/) || [""])[0];
check("the fold does not restate the card header's padding",
  !/padding/.test(foldSummary), foldSummary);
check("padding is scoped to summaries that have none of their own",
  /\.qc-fold > summary:not\(\.pg-card-header\) \{[^}]*padding/.test(css));
/* #step-score .sdc-head sets margin, and an unscoped .sdc-fold rule loses to
   it on specificity, so the margin rules have to be scoped the same way. */
check("the margin rules outrank the ones they override",
  /#step-score \.sdc-fold > summary\.sdc-head \{/.test(css),
  "an unscoped rule would lose to #step-score .sdc-head and do nothing");
check("the arrow animation respects prefers-reduced-motion",
  /prefers-reduced-motion[\s\S]{0,200}sdc-fold > summary\.sdc-head::after/.test(css));
check("the summary can be focused visibly",
  /qc-fold > summary:focus-visible/.test(css) &&
  /sdc-fold > summary\.sdc-head:focus-visible/.test(css),
  "keyboard users would not see where they are");

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
