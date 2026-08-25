#!/usr/bin/env node
/**
 * How Preview Changes reads.
 *
 * It used to print `JSON.stringify(before) → JSON.stringify(after)` for each
 * changed field. Since the comparison treated lists as opaque, one changed map
 * meant a row containing both entire map arrays, and the only way to find out
 * what had changed was to read serialized objects. The server now diffs by id,
 * so each row is one setting; these two helpers turn the field path and the
 * values into something a reviewer reads rather than parses.
 *
 * Run: node tests/frontend_config_preview_test.js
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
  let depth = 0, seen = false;
  for (let i = start; i < appJs.length; i += 1) {
    if (appJs[i] === "{") { depth += 1; seen = true; }
    else if (appJs[i] === "}") { depth -= 1; if (seen && depth === 0) return appJs.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

function extractConst(name) {
  const start = appJs.indexOf(`const ${name} = {`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  return appJs.slice(start, appJs.indexOf("};", start) + 2);
}

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext([
  extractConst("PREVIEW_FIELD_LABELS"),
  extractConst("PREVIEW_LEAF_LABELS"),
  extractFunction("_previewFieldLabel"),
  extractFunction("_previewValue"),
].join("\n"), sandbox);

const label = (f) => sandbox._previewFieldLabel(f);
const value = (v) => sandbox._previewValue(v);

console.log("\nField names read as settings, not paths");
checkEqual("a map state names the map", label("maps.ktrans.state"), "KTRANS requirement");
checkEqual("a map dimension names the map", label("maps.cbf.dimensions"), "CBF dimensions");
checkEqual("aliases are called what the interface calls them",
  label("maps.cbf.aliases"), "CBF recognised filenames");
checkEqual("an artifact names the artifact",
  label("required_artifacts.methods.required"), "methods required");
checkEqual("a dataset count names the dataset",
  label("datasets.clinical.participants"), "clinical participants");
checkEqual("a known top-level field is spelled out",
  label("code_execution_required"), "Participant code must run");
checkEqual("scoring mode is spelled out", label("scoring.mode"), "Analysis provider");
checkEqual("scoring package is spelled out", label("scoring.package_id"), "Scoring package");

console.log("\nAn unknown field is still reported");
check("an unrecognised path is tidied, not dropped",
  label("some_new_setting") === "some new setting", label("some_new_setting"));
check("an unrecognised nested path still names its parts",
  label("maps.cbf.brand_new") === "CBF brand new", label("maps.cbf.brand_new"));

console.log("\nValues read as words");
checkEqual("true reads as yes", value(true), "yes");
checkEqual("false reads as no, not empty", value(false), "no");
checkEqual("null reads as none", value(null), "none");
checkEqual("undefined reads as none", value(undefined), "none");
checkEqual("an empty string reads as none", value(""), "none");
checkEqual("a string is not quoted", value("custom"), "custom");
checkEqual("a number is a number", value(3), "3");
checkEqual("zero is not mistaken for empty", value(0), "0");
checkEqual("a list is comma separated, not JSON",
  value(["cbf", "perfusion"]), "cbf, perfusion");
checkEqual("an empty list reads as none", value([]), "none");

console.log("\nNo raw JSON in the common cases");
const commonCases = [true, false, null, "", "none", "custom", 3, 0, ["a", "b"], []];
check("no rendered value contains a brace or a quote",
  commonCases.every((v) => !/[{}"\[\]]/.test(value(v))),
  JSON.stringify(commonCases.map(value)));

console.log("\nWiring");
check("the empty case says so instead of printing a count of zero",
  appJs.includes("This draft matches the configuration currently in force"));
check("the count is worded, not '5 change(s)'",
  !appJs.includes("change(s)"));
check("one change is not called '1 changes'",
  appJs.includes('changes.length === 1 ? "1 change"'));
check("long value pairs get their own line",
  appJs.includes("cfg-preview-row-long"));
check("preview values are escaped",
  appJs.includes("escapeHtml(before)") && appJs.includes("escapeHtml(after)"));
check("the provider expander starts hidden in the markup",
  fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8")
    .includes('id="score-provider-details" hidden'));
check("the provider expander is hidden before the early return",
  /providerDetails\.hidden = true;[\s\S]{0,120}providerDetails\.open = false;/.test(appJs));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
