#!/usr/bin/env node
/**
 * The Private Reference Assets panel.
 *
 * It showed a count line and a flat list of filenames, which left the obvious
 * question unanswered: where do these files live, and where does mine go. The
 * panel is now one group per kind, each naming its purpose and its folder, so
 * an empty group still tells an organiser something.
 *
 * The folder path is the part that has to be exactly right, because someone
 * will copy files into it by hand. It is asserted per kind below, including
 * that it follows the challenge rather than being hardcoded.
 *
 * Run: node tests/frontend_config_assets_test.js
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

function extractConst(name) {
  const start = appJs.indexOf(`const ${name} = [`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  return appJs.slice(start, appJs.indexOf("];", start) + 2);
}

let html = "";
const sandbox = {
  console,
  el: () => ({ set innerHTML(v) { html = v; }, get innerHTML() { return html; } }),
};
vm.createContext(sandbox);
vm.runInContext([
  extractFunction("escapeHtml"),
  extractConst("ASSET_KINDS"),
  // The renderer now groups files by the scan their folder names and prints
  // the shared root once instead of sixty sibling paths, so it leans on these.
  // The renderer decides its own open state from this threshold.
  (() => {
    const start = appJs.indexOf("const _ASSET_COLLAPSE_ABOVE =");
    return appJs.slice(start, appJs.indexOf(";", start) + 1);
  })(),
  extractFunction("_assetScanLabel"),
  extractFunction("_assetCommonRoot"),
  extractFunction("_assetFileRow"),
  extractFunction("_renderConfigurationAssets"),
].join("\n"), sandbox);

const render = (assets) => { html = ""; sandbox._renderConfigurationAssets(assets); return html; };

console.log("\nEvery kind is shown, even with nothing in it");
const empty = render({ challenge_type: "asl", items: [] });
["Reference maps", "ROI masks", "Measured signal-time curves"].forEach((title) => {
  check(`${title} has its own group`, empty.includes(title));
});
check("an empty kind says so rather than vanishing",
  (empty.match(/None yet/g) || []).length === 3);
check("an empty kind still shows its count as zero",
  (empty.match(/cfg-asset-count">0</g) || []).length === 3, empty.slice(0, 200));

console.log("\nThe folder is stated, and follows the challenge");
check("reference maps name their folder",
  empty.includes("data/reference_data/asl/maps/"), empty);
check("masks name their folder",
  empty.includes("data/reference_data/asl/masks/"));
check("signals name their folder",
  empty.includes("data/reference_data/asl/signals/"));
const dce = render({ challenge_type: "dce", items: [] });
check("the path follows the challenge, not a hardcoded one",
  dce.includes("data/reference_data/dce/maps/") && !dce.includes("/asl/"));
check("an empty group still tells you where to put a file",
  empty.includes("copy files") || empty.includes("this folder"));

console.log("\nEach kind explains what it is for");
check("reference maps explain themselves", empty.includes("Ground truth"));
check("masks explain themselves", empty.includes("Regions to report statistics"));
check("signals explain themselves", empty.includes("measured curve"));

console.log("\nFiles are listed under the right kind");
const filled = render({
  challenge_type: "asl",
  items: [
    { kind: "mask", name: "gm_mask.nii.gz", readable: true, shape: [197, 233, 189] },
    { kind: "mask", name: "wm_mask.nii.gz", readable: true, shape: [197, 233, 189] },
    { kind: "reference", name: "GT_Perf.nii.gz", readable: false },
  ],
});
const groupFor = (kind) => {
  const start = filled.indexOf(`data-asset-kind="${kind}"`);
  const next = filled.indexOf('<details class="cfg-asset-group"', start + 10);
  return filled.slice(start, next === -1 ? undefined : next);
};
check("a mask appears under masks", groupFor("mask").includes("gm_mask.nii.gz"));
check("a mask does not appear under reference maps",
  !groupFor("reference").includes("gm_mask.nii.gz"));
check("the count reflects what is there",
  groupFor("mask").includes('cfg-asset-count cfg-asset-count-has">2<'), groupFor("mask").slice(0, 300));
check("a readable file says so", groupFor("mask").includes("Readable"));
check("shape is shown when known", groupFor("mask").includes("197 × 233 × 189"));
check("an unreadable file is called out, not silently listed",
  groupFor("reference").includes("Cannot be read")
  && groupFor("reference").includes("cfg-asset-bad"));
check("a file with no shape does not print undefined",
  !groupFor("reference").includes("undefined"));

console.log("\nSafety and odd input");
const hostile = render({
  challenge_type: "asl",
  items: [{ kind: "mask", name: "<img src=x onerror=alert(1)>", readable: true }],
});
check("filenames are escaped", !hostile.includes("<img src=x"));
check("a missing challenge does not print undefined in the path",
  !render({ items: [] }).includes("undefined"),
  render({ items: [] }).slice(0, 200));
check("an unknown kind does not break the render",
  render({ challenge_type: "asl", items: [{ kind: "mystery", name: "x.nii" }] })
    .includes("Reference maps"));
// Counted on the opening tag, not the class name: cfg-asset-groups is the
// wrapper and contains cfg-asset-group as a substring, so a loose count says
// four and is wrong for a reason that is invisible. Each kind is a <details>
// so that a long list can be collapsed to its count.
const noPayload = render({});
check("no payload at all still renders the three groups",
  (noPayload.match(/<details class="cfg-asset-group"/g) || []).length === 3,
  String((noPayload.match(/<details class="cfg-asset-group"/g) || []).length));
check("a missing challenge reads as a template, not a broken path",
  noPayload.includes("data/reference_data/&lt;challenge&gt;/maps/")
  && !noPayload.includes("reference_data//"),
  (noPayload.match(/data\/reference_data\/[^<]*/) || ["none"])[0]);

console.log("\nThe add control explains where the file goes");
const indexHtml = fs.readFileSync(path.resolve(__dirname, "../frontend/index.html"), "utf8");
check("the add row is labelled", indexHtml.includes("Add a file"));
check("it says the file is copied into the folder for the chosen type",
  indexHtml.includes("copied into the folder"));
check("it repeats that nothing leaves the machine",
  indexHtml.includes("Nothing leaves this machine"));
check("the type select has a visible label, not only aria-label",
  indexHtml.includes('class="cfg-asset-field">Type'));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
