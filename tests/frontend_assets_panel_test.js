#!/usr/bin/env node
/**
 * One hundred and eighty files with three names between them.
 *
 * The DCE reference set is Ct, Ktrans and vp repeated once per scan. The
 * organiser panel listed all one hundred and eighty flat, and printed sixty
 * folder paths above them to compensate. Neither half answered the only
 * question a reader has -- which scan is this file from -- and the paths were
 * a wall rather than an answer.
 *
 * The folder is now stated once, files are grouped by the scan their folder
 * names, unreadable files are called out where they cannot be scrolled past,
 * and anything long enough to need one gets a filter.
 *
 * Run: node tests/frontend_assets_panel_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const appJs = fs.readFileSync(path.resolve(__dirname, "../frontend/app.js"), "utf8");
const css = fs.readFileSync(path.resolve(__dirname, "../frontend/styles.css"), "utf8");

let passed = 0;
let failed = 0;

function check(desc, cond, extra = "") {
  if (cond) { console.log(`  OK  ${desc}`); passed++; }
  else { console.error(`  FAIL  ${desc}${extra ? ` — ${extra}` : ""}`); failed++; }
}

function extract(name) {
  const start = appJs.indexOf(`function ${name}(`);
  if (start < 0) throw new Error(`not found: ${name}`);
  let depth = 0, seen = false;
  for (let i = start; i < appJs.length; i += 1) {
    if (appJs[i] === "{") { depth += 1; seen = true; }
    else if (appJs[i] === "}") { depth -= 1; if (seen && depth === 0) return appJs.slice(start, i + 1); }
  }
  throw new Error(`unbalanced braces in ${name}`);
}

function extractConst(name) {
  const start = appJs.indexOf(`const ${name} =`);
  const end = appJs.indexOf("\n];", start);
  return appJs.slice(start, end + 3);
}

/** A scalar `const NAME = value;` on one line. */
function extractScalar(name) {
  const start = appJs.indexOf(`const ${name} =`);
  if (start < 0) throw new Error(`const not found: ${name}`);
  return appJs.slice(start, appJs.indexOf(";", start) + 1);
}

const host = { innerHTML: "" };
const sandbox = {
  console,
  el: (id) => (id === "config-manager-assets-status" ? host : null),
  document: { addEventListener() {} },
};
vm.createContext(sandbox);
[
  extract("escapeHtml"),
  extractConst("ASSET_KINDS"),
  extractScalar("_ASSET_COLLAPSE_ABOVE"),
  extract("_assetScanLabel"),
  extract("_assetCommonRoot"),
  extract("_assetFileRow"),
  extract("_renderConfigurationAssets"),
].forEach((code) => vm.runInContext(code, sandbox));

// ── The label ──────────────────────────────────────────────────────────────
console.log("\nA folder path becomes a scan name");
{
  const label = sandbox._assetScanLabel;
  check("the real DCE layout resolves",
    label("data/reference_data/maps/P01/site_2/scan_1/") === "P1 · Site 2 · Repeat 1",
    label("data/reference_data/maps/P01/site_2/scan_1/"));
  check("leading zeros do not survive into the label",
    label("maps/P007/site_03/scan_02/") === "P7 · Site 3 · Repeat 2",
    label("maps/P007/site_03/scan_02/"));
  check("visit is the same thing as scan",
    label("maps/P01/site_1/visit_2/") === "P1 · Site 1 · Repeat 2");
  check("a partial path says only what it knows",
    label("maps/P01/") === "P1", label("maps/P01/"));
  check("a folder that names no scan produces no label",
    label("data/reference_data/masks/") === null);
  check("an empty folder does not throw", label("") === null);
  check("a stray 'p' in a word is not a participant",
    label("data/preview/site_1/") === "Site 1", label("data/preview/site_1/"));
}

console.log("\nSixty sibling paths share one root");
{
  const root = sandbox._assetCommonRoot;
  check("the shared prefix is found",
    root(["a/b/P01/site_1/", "a/b/P01/site_2/", "a/b/P02/site_1/"]) === "a/b/",
    root(["a/b/P01/site_1/", "a/b/P01/site_2/"]));
  check("one folder is its own root", root(["a/b/c/"]) === "a/b/c/");
  check("nothing in common produces nothing", root(["a/x/", "b/y/"]) === "");
  check("no folders at all does not throw", root([]) === "");
}

// ── The render ─────────────────────────────────────────────────────────────
function referenceSet() {
  const items = [];
  for (let p = 1; p <= 10; p += 1) {
    for (let site = 1; site <= 3; site += 1) {
      for (let scan = 1; scan <= 2; scan += 1) {
        const folder = `data/reference_data/maps/P${String(p).padStart(2, "0")}/site_${site}/scan_${scan}/`;
        ["Ct.nii.gz", "Ktrans.nii.gz", "vp.nii.gz"].forEach((name) => {
          items.push({ kind: "reference", name, folder, readable: true, shape: [121, 145, 91] });
        });
      }
    }
  }
  return items;
}

console.log("\nThe panel, on the real reference set");
{
  const items = referenceSet();
  check("the fixture is the real size", items.length === 180, String(items.length));
  sandbox._renderConfigurationAssets({ challenge_type: "dce", items });
  const html = host.innerHTML;

  const paths = (html.match(/<code>/g) || []).length;
  check("the folder is printed once, not once per scan", paths === 3, `${paths} code chips`);
  check("and it is the shared root",
    html.includes("<code>data/reference_data/maps/</code>"), "root not shown");
  check("files are grouped by scan",
    (html.match(/class="cfg-asset-scan"/g) || []).length === 60,
    String((html.match(/class="cfg-asset-scan"/g) || []).length));
  check("each group names its scan", html.includes("P1 · Site 2 · Repeat 1"));
  check("and says how many files are in it", html.includes(">3 files</span>"));
  check("the groups start collapsed, so 180 rows are not dumped on the page",
    !/<details class="cfg-asset-scan"[^>]*\sopen/.test(html));
  check("a set this size gets a filter", html.includes('data-asset-filter="reference"'));
  check("the filter is labelled for screen readers", html.includes("visually-hidden"));
  check("every group is searchable by scan and by file name",
    (html.match(/data-asset-search=/g) || []).length === 60);
}

console.log("\nUnreadable files are not something to scroll past");
{
  const items = referenceSet();
  items[7].readable = false;
  sandbox._renderConfigurationAssets({ challenge_type: "dce", items });
  const html = host.innerHTML;
  check("the count is stated on the group header",
    html.includes('<span class="cfg-asset-bad">1 unreadable</span>'));
  check("and on the scan it is in", (html.match(/1 unreadable/g) || []).length >= 2);
  check("the file itself is marked", html.includes("cfg-asset-file-bad"));
}

console.log("\nSmall and empty sets are left alone");
{
  sandbox._renderConfigurationAssets({
    challenge_type: "dce",
    items: [{ kind: "mask", name: "GM_mask.nii.gz", folder: "data/reference_data/masks/",
              readable: true, shape: [121, 145, 91] }],
  });
  const html = host.innerHTML;
  /* Reference maps, masks and measured signals are the same kind of thing --
     organiser files grouped by the scan they belong to -- and giving one of
     them grouping, a filter and a collapse while the others got a flat list
     made three unrelated-looking panels. A filter on nine files is not
     needed; a panel that behaves differently from the one above it is
     worse. */
  check("every kind carries the same controls",
    html.includes('data-asset-filter="mask"'));
  check("and the file is still shown", html.includes("GM_mask.nii.gz"));

  sandbox._renderConfigurationAssets({ challenge_type: "dce", items: [] });
  check("nothing at all says where to put files",
    host.innerHTML.includes("data/reference_data/dce/maps/"));
  check("and does not print an empty list", host.innerHTML.includes("cfg-asset-empty"));
}

console.log("\nEvery kind behaves the same way");
{
  const masks = [];
  for (let site = 1; site <= 3; site += 1) {
    for (const region of ["GM", "WM", "Hipp"]) {
      masks.push({ kind: "mask", name: `site_${site}_${region}_mask.nii.gz`,
                   folder: "data/reference_data/masks/", readable: true,
                   shape: [121, 145, 91] });
    }
  }
  sandbox._renderConfigurationAssets({ challenge_type: "dce", items: masks });
  const html = host.innerHTML;

  /* Masks put the site in the file name rather than the path, so they used to
     fall through to a flat list while reference maps grouped by scan. */
  check("masks group by the site their filename names",
    (html.match(/class="cfg-asset-scan"/g) || []).length === 3,
    String((html.match(/class="cfg-asset-scan"/g) || []).length));
  check("and each group is named", html.includes("Site 2"));
  check("with three files in it", html.includes(">3 files</span>"));
  check("masks get the same filter", html.includes('data-asset-filter="mask"'));

  /* Nine files is not worth hiding behind a click; a hundred and eighty is. */
  check("a small group starts open",
    /<details class="cfg-asset-group" data-asset-kind="mask" open>/.test(html), html.slice(0, 160));
}

{
  sandbox._renderConfigurationAssets({ challenge_type: "dce", items: referenceSet() });
  const html = host.innerHTML;
  check("every kind is collapsible",
    (html.match(/<details class="cfg-asset-group"/g) || []).length === 3,
    String((html.match(/<details class="cfg-asset-group"/g) || []).length));
  check("a large group starts closed, with its count on the header",
    /data-asset-kind="reference"(?! open)/.test(html));
  check("the count is readable without opening it", html.includes(">180</span>"));
}

console.log("\nA filename can name a site without a folder saying so");
{
  const label = sandbox._assetScanLabel;
  check("a mask filename resolves", label("masks/", "site_1_GM_mask.nii.gz") === "Site 1",
    label("masks/", "site_1_GM_mask.nii.gz"));
  check("the folder still wins when it says more",
    label("maps/P01/site_2/scan_1/", "Ktrans.nii.gz") === "P1 · Site 2 · Repeat 1");
  /* Without a token boundary "composite_1" would report site 1. */
  check("a word that merely contains 'site' is not a site",
    label("masks/", "composite_1.nii.gz") === null,
    String(label("masks/", "composite_1.nii.gz")));
  check("and a two-digit number is not truncated",
    label("masks/", "site_12_GM.nii.gz") === "Site 12",
    label("masks/", "site_12_GM.nii.gz"));
  check("a name with nothing in it produces nothing",
    label("masks/", "brain_mask.nii.gz") === null);
}

console.log("\nWiring");
{
  check("filtering is delegated, so it survives a re-render",
    /addEventListener\("input"[\s\S]{0,200}data-asset-filter/.test(appJs));
  check("a match inside a collapsed scan opens it",
    /if \(hit && needle && row\.tagName === "DETAILS"\) row\.open = true/.test(appJs));
  check("clearing the box collapses them again",
    /if \(!needle && row\.tagName === "DETAILS"\) row\.open = false/.test(appJs));
  check("an empty result says so rather than showing a blank panel",
    /empty\.hidden = shown !== 0/.test(appJs));
  check("the search box is styled", css.includes(".cfg-asset-search input"));
  check("its focus ring matches the rest of the app",
    /\.cfg-asset-search input:focus[\s\S]{0,120}--purple/.test(css));
}

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
process.exit(failed ? 1 : 0);
