#!/usr/bin/env node
/**
 * The Configuration Manager maps and artifacts panels.
 *
 * These are edited by challenge organisers, who are researchers rather than
 * developers, so the panel was rebuilt around the one decision they actually
 * make: whether a map is required. Filename aliases became chips instead of a
 * comma separated textarea, and the reference detail collapses.
 *
 * The risk in a rewrite like that is silent: the panel can look correct and
 * save nothing. `_collectConfigurationDraft` reads `.config-map-state`,
 * `.config-map-dimensions` and `.config-map-aliases` by class name and
 * expects aliases as a comma joined string. Those survive as hidden inputs,
 * and the first group below asserts on exactly the shape the save path reads.
 *
 * No DOM library is used, in keeping with the other suites here. The markup
 * is checked as markup, and the fiddly part, deciding which typed names are
 * actually new, lives in `_newAliases`, which takes and returns plain arrays
 * so it can be exercised directly.
 *
 * Run: node tests/frontend_config_maps_test.js
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
  check(desc, JSON.stringify(actual) === JSON.stringify(expected),
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

function extractConst(name, opener) {
  const start = appJs.indexOf(`const ${name} ${opener}`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  const close = opener.endsWith("[") ? "];" : "};";
  return appJs.slice(start, appJs.indexOf(close, start) + 2);
}

const sandbox = {
  console,
  _configurationMapUnit: (id) => (id === "cbf" ? "mL/100g/min" : "min^-1"),
};
vm.createContext(sandbox);
vm.runInContext([
  extractFunction("escapeHtml"),
  extractConst("MAP_STATES", "= ["),
  extractConst("ARTIFACT_NOTES", "= {"),
  extractFunction("_aliasChip"),
  extractFunction("_newAliases"),
  extractFunction("_configurationMapRow"),
  extractFunction("_configurationMapsMarkup"),
  extractFunction("_configurationArtifactsMarkup"),
].join("\n"), sandbox);

const MAPS = [
  { id: "cbf", display: "CBF", label: "Cerebral blood flow", state: "unused",
    dimensions: 3, aliases: ["cbf", "perfusion"] },
  { id: "ktrans", display: "Ktrans", label: "Volume transfer constant", state: "required",
    dimensions: 3, aliases: ["ktrans", "k-trans"] },
  { id: "ve", display: "ve", label: "Extravascular volume fraction", state: "optional",
    dimensions: null, aliases: [] },
];

const markup = sandbox._configurationMapsMarkup(MAPS);

// ── The save path still finds what it reads ──────────────────────────────
console.log("\nThe save path still finds what it reads");

const hidden = (cls) => (markup.match(new RegExp(`class="${cls}"`, "g")) || []).length;
check("every row keeps a state input", hidden("config-map-state") === 3, String(hidden("config-map-state")));
check("every row keeps an aliases input", hidden("config-map-aliases") === 3);
check("every row keeps a dimensions input",
  (markup.match(/class="config-map-dimensions"/g) || []).length === 3);
check("rows still carry the id the save path groups by",
  ["cbf", "ktrans", "ve"].every((id) => markup.includes(`data-map-id="${id}"`)));
check("the state input carries the map's current state, not a default",
  /data-map-id="ktrans"[\s\S]*?class="config-map-state" value="required"/.test(markup));
check("aliases are stored comma joined, as the save path expects",
  markup.includes('class="config-map-aliases" value="ktrans, k-trans"'),
  markup.slice(markup.indexOf("ktrans"), markup.indexOf("ktrans") + 600));
check("a map with no aliases stores an empty string rather than breaking",
  markup.includes('class="config-map-aliases" value=""'));
check("a null dimension renders as empty rather than the word null",
  !markup.includes('value="null"'));

// ── The requirement control ──────────────────────────────────────────────
console.log("\nRequirement control");

const rowFor = (id) => {
  const start = markup.indexOf(`data-map-id="${id}"`);
  const next = markup.indexOf("<div class=\"config-map-row", start + 10);
  return markup.slice(start, next === -1 ? undefined : next);
};

check("all three choices are offered", ["required", "optional", "unused"]
  .every((state) => rowFor("cbf").includes(`data-state="${state}"`)));
check("exactly one segment is checked per row",
  (rowFor("cbf").match(/aria-checked="true"/g) || []).length === 1);
check("the checked segment is the map's actual state",
  /data-state="required"[^>]*aria-checked="true"/.test(rowFor("ktrans")));
check("each choice explains its consequence",
  rowFor("cbf").includes("rejected") && rowFor("cbf").includes("Ignored"));
check("the group is announced to a screen reader",
  rowFor("cbf").includes('role="radiogroup"') && rowFor("cbf").includes('role="radio"'));
check("the row records its state for styling",
  rowFor("cbf").includes('data-state="unused"'));

// ── Summary and search ───────────────────────────────────────────────────
console.log("\nSummary and search");

check("the summary counts the required maps", markup.includes('data-count="required">1<'));
check("the summary counts the optional maps", markup.includes('data-count="optional">1<'));
check("the summary counts the unused maps", markup.includes('data-count="unused">1<'));
check("each row carries searchable text",
  rowFor("ktrans").includes("volume transfer constant"), rowFor("ktrans").slice(0, 300));
check("search text includes the aliases, not just the name",
  rowFor("cbf").toLowerCase().includes("perfusion"));
check("there is an empty state for a search that matches nothing",
  markup.includes("data-no-results"));

// ── Detail disclosure ────────────────────────────────────────────────────
console.log("\nDetail disclosure");

check("detail starts collapsed", rowFor("cbf").includes('class="cfg-map-detail" hidden'));
check("the toggle starts collapsed too", rowFor("cbf").includes('aria-expanded="false"'));
check("units are shown from the challenge definition",
  rowFor("cbf").includes("mL/100g/min"));
check("dimensions are explained rather than left bare",
  rowFor("cbf").includes("3 for a single map"));
check("aliases are explained by what they do, naming the map in question",
  rowFor("cbf").includes("treated as CBF"), rowFor("cbf").slice(-700));

// ── Which names a typed entry adds ───────────────────────────────────────
console.log("\nAlias entry rules");

checkEqual("a new name is added", sandbox._newAliases(["cbf"], "perfmap"), ["perfmap"]);
checkEqual("a pasted list becomes several names",
  sandbox._newAliases([], "one, two , three"), ["one", "two", "three"]);
checkEqual("a name already present adds nothing",
  sandbox._newAliases(["cbf"], "cbf"), []);
checkEqual("case is not a distinction, since matching ignores it",
  sandbox._newAliases(["cbf"], "CBF"), []);
checkEqual("a repeat inside one paste is added once",
  sandbox._newAliases([], "a, a, b"), ["a", "b"]);
checkEqual("empty entries and stray commas are dropped",
  sandbox._newAliases([], " , ,, "), []);
checkEqual("surrounding whitespace is trimmed",
  sandbox._newAliases([], "  spaced  "), ["spaced"]);
checkEqual("existing names are compared after trimming too",
  sandbox._newAliases([" cbf "], "cbf"), []);

// ── Artifacts ────────────────────────────────────────────────────────────
console.log("\nArtifacts");

const artifacts = sandbox._configurationArtifactsMarkup([
  { id: "modelled_st", label: "Modelled signal-time curve", required: true },
  { id: "methods", label: "Methods document", required: false },
  { id: "mystery", label: "Something undocumented", required: false },
]);
check("each artifact keeps its id for the save path",
  (artifacts.match(/data-artifact-id=/g) || []).length === 3);
check("a required artifact stays checked",
  /data-artifact-id="modelled_st"[\s\S]*?checked/.test(artifacts));
check("an unrequired artifact is not checked",
  !/data-artifact-id="methods"[\s\S]*?checked/.test(artifacts.slice(artifacts.indexOf("methods"))));
check("a known artifact explains what requiring it does",
  artifacts.includes("compare the model against the measurement"));
check("an artifact with no note still renders its label",
  artifacts.includes("Something undocumented"));
check("the consequence of ticking anything is stated once",
  artifacts.includes("rejected at validation"));

// ── Safety and empty states ──────────────────────────────────────────────
console.log("\nSafety and empty states");

const hostile = sandbox._configurationMapsMarkup([{
  id: "x", display: "<script>alert(1)</script>", label: "l", state: "unused",
  dimensions: 3, aliases: ['" onerror="alert(1)'],
}]);
check("map names are escaped", !hostile.includes("<script>alert(1)</script>"));
check("an alias cannot break out of the hidden input",
  !/value="[^"]*"\s+onerror=/.test(hostile), hostile.slice(0, 300));
check("an alias cannot break out of a chip's remove button",
  !/data-alias="[^"]*"\s+onerror=/.test(hostile));
// Asserting on data-state alone is not enough: every segment button carries
// one too, so a bad row state still matches somewhere in the markup. Check
// the row's own attribute, the hidden input the save path reads, and that a
// segment is still selected.
const odd = sandbox._configurationMapsMarkup([{ id: "a", display: "A", label: "l",
  state: "nonsense", aliases: [] }]);
check("an unrecognised state does not reach the row",
  /class="config-map-row cfg-map" data-map-id="a" data-state="unused"/.test(odd),
  odd.slice(odd.indexOf("config-map-row"), odd.indexOf("config-map-row") + 160));
check("an unrecognised state does not reach the save path",
  odd.includes('class="config-map-state" value="unused"'));
check("an unrecognised state still leaves one segment selected",
  (odd.match(/aria-checked="true"/g) || []).length === 1);
check("no maps renders a message, not an empty box",
  sandbox._configurationMapsMarkup([]).includes("No maps are defined"));
check("no artifacts renders a message",
  sandbox._configurationArtifactsMarkup([]).includes("No artifact types are configured"));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
