#!/usr/bin/env node
/**
 * What the Configuration Manager says you changed.
 *
 * The panel used to acknowledge nothing. You could switch a map from required
 * to unused, close the section, and the page looked exactly as before; the
 * only way to confirm a click had registered was to save a version and read it
 * back. The comparison below is what replaced that, and its whole value is in
 * being specific: "CBF: Not used to Required" answers "did I click the right
 * thing", where a count of changes does not.
 *
 * `_configurationChanges` reads two module-level snapshots, so the two
 * snapshot helpers are stubbed here and the diff is driven directly. That
 * keeps the assertions on the wording an organiser actually reads.
 *
 * Run: node tests/frontend_config_changes_test.js
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

function extractConst(name, opener) {
  const start = appJs.indexOf(`const ${name} ${opener}`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  return appJs.slice(start, appJs.indexOf(opener.endsWith("[") ? "];" : "};", start) + 2);
}

const sandbox = { console };
vm.createContext(sandbox);
vm.runInContext([
  extractConst("MAP_STATES", "= ["),
  extractConst("CONFIG_SECTIONS", "= ["),
  appJs.slice(appJs.indexOf("const _mapStateWord ="), appJs.indexOf(";", appJs.indexOf("const _mapStateWord =")) + 1),
  extractFunction("_listDiff"),
  extractFunction("_configurationChanges"),
  "let _configurationBaseline = null;",
  "let _draft = null;",
  "function _configurationSnapshot() { return _draft; }",
  "function setPair(before, after) { _configurationBaseline = before; _draft = after; }",
  // const declarations stay in the script scope rather than landing on the
  // sandbox object, so the section list is handed over deliberately.
  "function sections() { return CONFIG_SECTIONS; }",
].join("\n"), sandbox);

const CONFIG = (overrides = {}) => ({
  maps: [
    { id: "cbf", state: "unused", dimensions: "3", aliases: ["cbf", "perfusion"] },
    { id: "ktrans", state: "required", dimensions: "3", aliases: ["ktrans"] },
  ],
  required_artifacts: [
    { id: "modelled_st", required: true },
    { id: "methods", required: false },
  ],
  code_execution_required: false,
  datasets: { clinical: { participants: "5", repeats: "2", sites: "1" } },
  scoring: { mode: "none", package_id: null },
  reference_dataset_version: "",
  ...overrides,
});

function diff(mutate) {
  const before = CONFIG();
  const after = JSON.parse(JSON.stringify(before));
  mutate(after);
  sandbox.setPair(before, after);
  return sandbox._configurationChanges();
}

const texts = (changes) => changes.map((c) => c.text);
// A mutation that stops a change being detected leaves this empty. Returning
// "" rather than undefined keeps the suite reporting every remaining case
// instead of dying on the first one.
const first = (changes) => texts(changes)[0] || "";
const sectionOf = (changes) => (changes[0] || {}).section || "";

console.log("\nNothing changed means nothing reported");
check("an untouched draft reports no changes", diff(() => {}).length === 0);
sandbox.setPair(null, CONFIG());
// Guarded: without the null check inside _configurationChanges this throws
// rather than returning, and a throw would take the rest of the suite with it.
let noBaseline;
try { noBaseline = sandbox._configurationChanges().length === 0; }
catch (err) { noBaseline = false; }
check("no baseline yet reports nothing rather than everything", noBaseline);

console.log("\nMap requirement");
const req = diff((c) => { c.maps[0].state = "required"; });
check("one change is reported", req.length === 1, JSON.stringify(texts(req)));
check("it names the map", first(req).includes("CBF"), first(req));
check("it gives the old and the new value, in words",
  first(req) === "CBF: Not used to Required", first(req));
check("it is filed under the section that owns it", sectionOf(req) === "maps");

console.log("\nFilenames");
const added = diff((c) => { c.maps[0].aliases.push("perfmap"); });
check("an added name is named, not counted",
  first(added) === "CBF filenames: added perfmap", first(added));
const removed = diff((c) => { c.maps[0].aliases = ["cbf"]; });
check("a removed name is named too",
  first(removed) === "CBF filenames: removed perfusion", first(removed));
const swapped = diff((c) => { c.maps[0].aliases = ["cbf", "perfmap"]; });
check("one added and one removed reads as two changes",
  swapped.length === 2, JSON.stringify(texts(swapped)));
check("reordering the same names is not a change",
  diff((c) => { c.maps[0].aliases = ["perfusion", "cbf"]; }).length === 0);

console.log("\nDimensions");
check("a dimension change reports both values",
  first(diff((c) => { c.maps[0].dimensions = "4"; })) === "CBF dimensions: 3 to 4");
check("clearing a dimension reads as any, not as empty",
  first(diff((c) => { c.maps[0].dimensions = ""; })) === "CBF dimensions: 3 to any");

console.log("\nArtifacts");
check("requiring an artifact says so",
  first(diff((c) => { c.required_artifacts[1].required = true; })) === "methods: now required");
check("unrequiring one says the opposite",
  first(diff((c) => { c.required_artifacts[0].required = false; })) === "modelled_st: no longer required");
check("the code execution switch is reported",
  first(diff((c) => { c.code_execution_required = true; })).includes("code must now run"));
check("artifacts are filed under artifacts",
  sectionOf(diff((c) => { c.code_execution_required = true; })) === "artifacts");

console.log("\nDataset counts");
check("a count change reports both numbers",
  first(diff((c) => { c.datasets.clinical.participants = "6"; })) === "clinical participants: 5 to 6");
check("an unset count reads as pending",
  first(diff((c) => { c.datasets.clinical.repeats = null; })) === "clinical repeats: 2 to pending");
check("a whole new dataset is noticed",
  diff((c) => { c.datasets.synthetic = { participants: "3", repeats: null, sites: null }; }).length === 1);

console.log("\nScoring");
check("changing the provider is reported",
  first(diff((c) => { c.scoring.mode = "builtin"; })) === "analysis provider: none to builtin");
check("changing the package is reported",
  first(diff((c) => { c.scoring.package_id = "pkg-1"; })) === "scoring package: none to pkg-1");
check("scoring is filed under scoring",
  sectionOf(diff((c) => { c.scoring.mode = "builtin"; })) === "scoring");

console.log("\nSeveral at once");
const many = diff((c) => {
  c.maps[0].state = "optional";
  c.maps[1].aliases.push("k-trans");
  c.datasets.clinical.sites = "2";
  c.scoring.mode = "builtin";
});
check("every change is listed", many.length === 4, JSON.stringify(texts(many)));
check("they carry the section they belong to",
  new Set(many.map((c) => c.section)).size === 3,
  JSON.stringify(many.map((c) => c.section)));

console.log("\nThings that are not changes");
check("a map missing from the baseline is not reported as changed",
  diff((c) => { c.maps.push({ id: "ve", state: "optional", dimensions: "3", aliases: [] }); }).length === 0);
check("a number typed as a string is not a change",
  diff((c) => { c.datasets.clinical.participants = 5; }).length === 0);

console.log("\nEvery change is filed somewhere the panel renders");
// _refreshConfigurationChanges only lists sections present in CONFIG_SECTIONS,
// so a change filed under an unknown name is counted and never shown. That is
// worse than not detecting it, because the count says something is pending and
// the list cannot say what.
const KNOWN = new Set(sandbox.sections().map(([id]) => id));
const everySection = [
  (c) => { c.maps[0].state = "required"; },
  (c) => { c.maps[0].aliases.push("x"); },
  (c) => { c.maps[0].dimensions = "4"; },
  (c) => { c.required_artifacts[1].required = true; },
  (c) => { c.code_execution_required = true; },
  (c) => { c.datasets.clinical.participants = "9"; },
  (c) => { c.datasets.clinical.repeats = null; },
  (c) => { c.scoring.mode = "builtin"; },
  (c) => { c.scoring.package_id = "pkg-1"; },
  (c) => { c.reference_dataset_version = "v2"; },
].flatMap((mutate) => diff(mutate));
const stray = everySection.filter((change) => !KNOWN.has(change.section));
check("no change is filed under an unknown section",
  stray.length === 0, JSON.stringify(stray));
check("the sweep actually produced changes to check",
  everySection.length >= 10, String(everySection.length));

console.log("\nWiring");
check("the baseline is taken after the panel renders",
  appJs.includes("_setConfigurationBaseline()"));
check("the list is announced to screen readers",
  fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8")
    .includes('id="config-manager-changes"'));
check("changed cards are marked, not only counted",
  appJs.includes("config-card-changed"));
check("a discard path exists",
  appJs.includes("config-manager-discard"));
check("clicking discard does not itself count as a change",
  /if \(event\.target\.closest\("#config-manager-discard"\)\) return;/.test(appJs));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
