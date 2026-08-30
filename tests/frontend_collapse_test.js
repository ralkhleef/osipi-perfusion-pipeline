#!/usr/bin/env node
/**
 * Collapsible sections on Score & Preview.
 *
 * Step 5 is the longest page in the app: two tables, a preview grid, a
 * reference comparison and a technical drawer. A reviewer who has read a
 * section wants it out of the way.
 *
 * The part worth testing is not that the sections fold, HTML does that. It
 * is that folding sticks. The panel is rebuilt with innerHTML every time an
 * async status resolves, so a plain <details> forgets its state and springs
 * back open on its own a second or two after being closed. That reads as the
 * app fighting the reviewer, and it is invisible in any static check of the
 * markup, so _foldAttrs and the toggle listener are exercised for real here.
 *
 * Run: node tests/frontend_collapse_test.js
 */
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

/* ── The remembering, run for real ────────────────────────────────────────
   A stub document that records the one capture-phase listener the module
   registers, so a toggle can be replayed against the genuine handler. */
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
/* The sandbox above calls _watchFolds itself, so it proves the function
   works without proving anything calls it. Deleting the call left every
   check here passing and every fold in the app forgetful. */
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
/* Technical Details and the per-map mask tables are plain <details> with no
   fold key. Recording them would be harmless but tracking a growing set of
   things nobody asked to persist is how this kind of state rots. */
const before = sandbox._collapsedFolds.size;
toggle.fn({ target: { dataset: {}, open: false } });
toggle.fn({ target: null });
toggle.fn({ target: { open: false } });
check("a details with no fold key is ignored",
  sandbox._collapsedFolds.size === before, `${before} -> ${sandbox._collapsedFolds.size}`);

console.log("\nEvery foldable section actually asks to be remembered");
/* A section converted to <details> but rendered without _foldAttrs would
   look right and quietly reset on every redraw, which is the whole bug. */
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
/* The preview section is rendered from two places, the normal path and the
   error path. Only one of them having a key means the fold survives success
   and forgets on failure. */
check("both renders of the preview section use the same key",
  (appJs.match(/_foldAttrs\("map-previews"\)/g) || []).length === 2,
  "the error path would reopen a section the reviewer closed");

console.log("\nThe headers are summaries, not divs with a click handler");
/* A <summary> is keyboard reachable and announced as a disclosure for free.
   A div with a click handler is neither, and that is the usual way this gets
   built. */
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

/* Found by rendering it, not by reading it. `.step-shell .pg-card-header`
   sets the rule and the margin with !important, so the closed-state reset
   lost silently and a folded card kept a line along its bottom edge with a
   band of empty card under it. Two things follow, and both are load-bearing. */
const closedCard = (css.match(
  /\.qc-fold:not\(\[open\]\) > summary\.pg-card-header \{[^}]*\}/) || [""])[0];
check("the closed card cancels the rule under its header",
  /border-bottom: none !important/.test(closedCard), closedCard);
check("and the gap the margin would leave",
  /margin-bottom: 0 !important/.test(closedCard), closedCard);
/* The other half of the same lesson: whatever this file says about the
   header's padding either loses to that !important rule or has to fight it,
   and either way a folding card ends up inset differently from a plain one.
   So it says nothing, and the two line up by construction. */
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
