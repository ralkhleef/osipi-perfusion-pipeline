#!/usr/bin/env node
/**
 * Executes the header and orientation check renderer.
 *
 * The Python side proves the check is computed and that both report formats
 * render it. This proves the third surface, the interface, does too. All
 * three were added together because the check existed and was invisible for
 * a while, which is the failure worth guarding against.
 *
 * The functions are extracted and run rather than grepped for, so what is
 * asserted is the HTML a reviewer would actually see.
 *
 * Run: node tests/frontend_header_check_test.js
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

function checkEqual(desc, actual, expected) {
  check(desc, actual === expected,
    `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

function extractFunction(name) {
  const start = appJs.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`function not found: ${name}`);
  let depth = 0;
  let seenBody = false;
  for (let i = start; i < appJs.length; i += 1) {
    const ch = appJs[i];
    if (ch === "{") { depth += 1; seenBody = true; }
    else if (ch === "}") {
      depth -= 1;
      if (seenBody && depth === 0) return appJs.slice(start, i + 1);
    }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

function extractConst(name) {
  const start = appJs.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  const end = appJs.indexOf("};", start);
  return appJs.slice(start, end + 2);
}

// statusPill is defined elsewhere in app.js and does browser work; a stub
// keeps this suite on the renderer under test while still proving the
// verdict text reaches it.
const sandbox = {
  console,
  statusPill: (label, tone) => `<span data-tone="${tone}">${label}</span>`,
};
vm.createContext(sandbox);
vm.runInContext([
  extractFunction("escapeHtml"),
  extractConst("HEADER_CHECK_VERDICTS"),
  extractFunction("_headerFieldText"),
  extractFunction("_renderHeaderCheck"),
].join("\n"), sandbox);

const field = (submitted, reference, matches) => ({ submitted, reference, matches });

const MATCHING = {
  status: "matches",
  fields: {
    shape: field([64, 64, 20], [64, 64, 20], true),
    voxel_size: field([3.0, 3.0, 5.0], [3.0, 3.0, 5.0], true),
    orientation: field(["L", "A", "S"], ["L", "A", "S"], true),
    dtype: field("float32", "float32", true),
  },
};

const FLIPPED = {
  status: "geometry_mismatch",
  fields: {
    shape: field([64, 64, 20], [64, 64, 20], true),
    voxel_size: field([3.0, 3.0, 5.0], [3.0, 3.0, 5.0], true),
    orientation: field(["R", "A", "S"], ["L", "A", "S"], false),
    dtype: field("float32", "float32", true),
  },
};

console.log("\nField text");
checkEqual("a matching field shows its value once",
  sandbox._headerFieldText(field([64, 64, 20], [64, 64, 20], true)), "64 x 64 x 20");
checkEqual("a differing field shows both values",
  sandbox._headerFieldText(field([3, 3, 5], [2, 2, 5], false)), "3 x 3 x 5 vs 2 x 2 x 5");
checkEqual("axis codes join without a separator",
  sandbox._headerFieldText(field(["L", "A", "S"], ["L", "A", "S"], true), ""), "LAS");
checkEqual("an unverified field is not shown as a pass",
  sandbox._headerFieldText(field(null, ["L", "A", "S"], null)), "Not verified");
checkEqual("a missing field object is not shown as a pass",
  sandbox._headerFieldText(undefined), "Not verified");
checkEqual("a value against a missing one says so",
  sandbox._headerFieldText(field("float32", null, false)), "float32 vs not declared");

console.log("\nRendering");
const nothing = sandbox._renderHeaderCheck(null);
checkEqual("no check renders nothing at all", nothing, "");

const matchingHtml = sandbox._renderHeaderCheck(MATCHING);
check("a matching check reports the verdict",
  matchingHtml.includes("Matches reference"), matchingHtml.slice(0, 160));
check("a matching check shows the orientation joined",
  matchingHtml.includes("LAS"));
check("a matching check does not warn",
  !matchingHtml.includes("not reliable"));
check("a matching check stays collapsed",
  !matchingHtml.includes("<details") || !/<details[^>]*\sopen/.test(matchingHtml));

const flippedHtml = sandbox._renderHeaderCheck(FLIPPED);
check("a flipped check reports a geometry difference",
  flippedHtml.includes("Geometry differs"));
check("a flipped check shows both orientations",
  flippedHtml.includes("RAS vs LAS"), flippedHtml);
check("a flipped check warns that the metrics are unreliable",
  flippedHtml.includes("not reliable"));
check("a flipped check opens by default so it is not missed",
  /<details[^>]*\sopen/.test(flippedHtml));
check("a flipped check is toned as a warning",
  flippedHtml.includes('data-tone="warning"'));

console.log("\nSafety");
const hostile = sandbox._renderHeaderCheck({
  status: "matches",
  fields: { shape: field(["<img src=x onerror=alert(1)>"], ["a"], false) },
});
check("field values are escaped",
  !hostile.includes("<img src=x"), hostile.slice(0, 200));

const unknown = sandbox._renderHeaderCheck({ status: "brand_new_status", fields: {} });
check("an unrecognised status is shown rather than called a pass",
  unknown.includes("brand_new_status") && !unknown.includes("Matches reference"));

const empty = sandbox._renderHeaderCheck({ status: "not_verified", fields: {} });
check("a check with no fields still renders without throwing",
  empty.includes("Not verified"));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
