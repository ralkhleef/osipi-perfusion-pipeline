#!/usr/bin/env node
/**
 * Executes the ROI renderer against a minimal DOM stub.
 *
 * The main smoke suite inspects source text. This one actually *runs*
 * renderRoiDescriptiveStatistics() and inspects the DOM it produces, so
 * formatting and clearing behaviour are verified rather than assumed.
 *
 * Only the ROI block and its escaping helper are evaluated — app.js as a
 * whole performs browser-only work at load time.
 *
 * Run: node tests/frontend_roi_dom_test.js
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
  check(desc, actual === expected, `expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
}

// Extract the pieces under test.
function sliceFrom(startMarker) {
  const start = appJs.indexOf(startMarker);
  if (start < 0) throw new Error(`marker not found: ${startMarker}`);
  return appJs.slice(start);
}

/** Extract one top-level function by matching its closing brace. */
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

const escapeSrc = extractFunction("escapeHtml");
const roiSrc = sliceFrom("/* ── ROI Ktrans statistics");

// Minimal DOM stub.
function makeElement(id) {
  return {
    id,
    innerHTML: "",
    textContent: "",
    style: { display: "" },
  };
}

function makeDom() {
  const nodes = {};
  for (const id of ["roi-descriptive-card", "roi-descriptive-table",
                    "roi-descriptive-body", "roi-descriptive-empty",
                    "roi-descriptive-count", "roi-descriptive-method",
                    "roi-descriptive-scope"]) {
    nodes[id] = makeElement(id);
  }
  return nodes;
}

/* The module registers its filter listeners at load time, so the sandbox needs
   somewhere to register them. */
function makeDocumentStub() {
  const handlers = {};
  return {
    handlers,
    addEventListener(type, fn) { (handlers[type] = handlers[type] || []).push(fn); },
  };
}

function run(records, status, nodes, extraNodes = {}) {
  const sandbox = {
    document: { addEventListener() {} },
    el: (id) => (id in extraNodes ? extraNodes[id] : nodes[id] || null),
    console,
    document: makeDocumentStub(),
  };
  vm.createContext(sandbox);
  vm.runInContext(`${escapeSrc}\n${roiSrc}\n`, sandbox);
  sandbox.renderRoiDescriptiveStatistics(records, status);
  nodes.__sandbox = sandbox;
  return nodes;
}

function rowCount(nodes) {
  // Rows carry the text the filter searches, so the opening tag has attributes.
  const html = nodes["roi-descriptive-body"].innerHTML;
  return (html.match(/<tr[\s>]/g) || []).length;
}

// ── Fixtures matching the real canonical record shape ────────────────────
const AVAILABLE = {
  dataset: "synthetic", participant: "1", repeat: "1", site: "1",
  map_type: "ktrans", roi_id: "tumour", roi_label: "Tumour",
  roi_mean: 0.3, roi_minimum: 0.1, roi_maximum: 0.5, roi_range: 0.4,
  roi_median: 0.25, roi_within_scan_sd: 0.111803, roi_within_scan_cov: 0.447214,
  voxel_count: 4, mask_voxel_count: 4, units: "min^-1",
  status: "available", unavailable_reason: null,
};

const CLINICAL_NO_SITE = { ...AVAILABLE, dataset: "clinical", site: null };

const UNAVAILABLE = {
  ...AVAILABLE, roi_id: "necrosis", roi_label: "Necrosis",
  roi_median: null, roi_within_scan_sd: null, roi_within_scan_cov: null,
  voxel_count: 0, status: "empty_roi", unavailable_reason: "empty_roi",
};

const MEAN_NEAR_ZERO = {
  ...AVAILABLE, roi_id: "edge", roi_label: "Edge",
  roi_within_scan_cov: null, status: "available",
  unavailable_reason: "mean_near_zero",
};

console.log("\n=== ROI renderer: executed DOM behaviour ===\n");

// ── Populated render ─────────────────────────────────────────────────────
{
  const nodes = run([AVAILABLE], "available", makeDom());
  checkEqual("one row rendered", rowCount(nodes), 1);
  const html = nodes["roi-descriptive-body"].innerHTML;
  check("CoV displayed as 44.72%", html.includes("44.72%"), html);
  check("median displayed", html.includes("0.2500"));
  check("mean displayed", html.includes("0.3000"));
  check("range displayed", html.includes("0.1000 to 0.5000"));
  check("map type displayed", html.includes("KTRANS"));
  check("ROI label displayed", html.includes("Tumour"));
  // Identity that is the same on every row is stated once above the table
  // rather than repeated down a column. With a single row that is always all
  // of it, so the dataset moves to the scope line but must still be shown.
  check("dataset title-cased for display",
        nodes["roi-descriptive-scope"].innerHTML.includes("Synthetic"),
        nodes["roi-descriptive-scope"].innerHTML);
  check("lifted identity is not also repeated in the row",
        !html.includes("Synthetic"));
  checkEqual("card visible", nodes["roi-descriptive-card"].style.display, "");
  checkEqual("table visible", nodes["roi-descriptive-table"].style.display, "");
  checkEqual("row count shown", nodes["roi-descriptive-count"].textContent, "1");
  check("methodology text present",
        nodes["roi-descriptive-method"].textContent.includes("population definition"));
  check("methodology states CoV storage",
        nodes["roi-descriptive-method"].textContent.includes("stored as a ratio"));
}

// ── Implicit clinical site ───────────────────────────────────────────────
{
  const nodes = run([CLINICAL_NO_SITE], "available", makeDom());
  const html = nodes["roi-descriptive-body"].innerHTML;
  const scope = nodes["roi-descriptive-scope"].innerHTML;
  // An absent site is dropped rather than announced. The point of the original
  // check stands: it must never surface as null or undefined anywhere.
  check("absent site is not listed above the table", !scope.includes("Site"), scope);
  check("null site never renders the word null",
        !html.includes(">null<") && !scope.includes(">null<"));
  check("null site never renders undefined",
        !html.includes("undefined") && !scope.includes("undefined"));
}

// ── Unavailable values ───────────────────────────────────────────────────
{
  const nodes = run([UNAVAILABLE], "available", makeDom());
  const html = nodes["roi-descriptive-body"].innerHTML;
  check("unavailable median shown as Unavailable", html.includes("Unavailable"));
  check("unavailable never rendered as 0.0000", !html.includes("0.0000"));
  check("unavailable CoV never rendered as 0.00%", !html.includes("0.00%"));
  check("reason surfaced", html.includes("Empty ROI"), html);
}

// ── mean_near_zero keeps median and SD ───────────────────────────────────
{
  const nodes = run([MEAN_NEAR_ZERO], "available", makeDom());
  const html = nodes["roi-descriptive-body"].innerHTML;
  check("median still shown when only CoV is unavailable", html.includes("0.2500"));
  check("SD still shown when only CoV is unavailable", html.includes("0.1118"));
  check("CoV alone marked unavailable", html.includes("Unavailable"));
  check("reason is mean near zero", html.includes("Mean near zero"));
}

// ── Stale rows cleared on re-render ──────────────────────────────────────
{
  const nodes = makeDom();
  run([AVAILABLE, UNAVAILABLE], "available", nodes);
  checkEqual("two rows before replacement", rowCount(nodes), 2);

  run([], "no_eligible_maps", nodes);
  checkEqual("stale rows cleared", rowCount(nodes), 0);
  check("previous ROI label gone", !nodes["roi-descriptive-body"].innerHTML.includes("Tumour"));
  checkEqual("table hidden when empty", nodes["roi-descriptive-table"].style.display, "none");
  check("empty message explains why",
        nodes["roi-descriptive-empty"].textContent.includes("No valid configured parameter maps"));
}

// ── Every canonical status maps to a message ─────────────────────────────
{
  const cases = {
    no_roi_configured: "no ROI masks were configured",
    no_eligible_maps: "No valid configured parameter maps",
    calculation_error: "could not be calculated",
  };
  for (const [status, fragment] of Object.entries(cases)) {
    const nodes = run([], status, makeDom());
    check(`status '${status}' explained`,
          nodes["roi-descriptive-empty"].textContent.includes(fragment),
          nodes["roi-descriptive-empty"].textContent);
  }
  // An unknown status must fall back, not throw.
  let threw = false;
  let nodes;
  try { nodes = run([], "something_new", makeDom()); } catch (e) { threw = true; }
  check("unknown status does not crash", !threw);
  check("unknown status uses the neutral fallback",
        !threw && nodes["roi-descriptive-empty"].textContent.includes("No ROI parameter-map statistics are available"));
}

// ── A challenge with no configured ROI payload: no section ───────────────
{
  const nodes = run([], null, makeDom());
  checkEqual("section hidden with no records and no status",
             nodes["roi-descriptive-card"].style.display, "none");
}
{
  // Populated challenge, then another result carrying nothing.
  const nodes = makeDom();
  run([AVAILABLE], "available", nodes);
  run([], null, nodes);
  checkEqual("section hidden after switching to ASL/DSC",
             nodes["roi-descriptive-card"].style.display, "none");
}

// ── Escaping, executed ───────────────────────────────────────────────────
{
  const hostile = {
    ...AVAILABLE,
    roi_label: "<script>alert(1)</script>",
    dataset: 'x"><img src=x onerror=alert(1)>',
    participant: "<b>1</b>",
  };
  const nodes = run([hostile], "available", makeDom());
  const html = nodes["roi-descriptive-body"].innerHTML;
  check("script tag escaped", !html.includes("<script>") && html.includes("&lt;script&gt;"));
  check("img onerror escaped", !html.includes("<img src=x"));
  check("bold tag escaped", !html.includes("<b>1</b>"));
}

// ── Many rows stay one table ─────────────────────────────────────────────
{
  const many = Array.from({ length: 120 }, (_, i) => ({ ...AVAILABLE, participant: String(i + 1) }));
  const nodes = run(many, "available", makeDom());
  checkEqual("120 rows rendered", rowCount(nodes), 120);
  checkEqual("count reflects rows", nodes["roi-descriptive-count"].textContent, "120");
  const html = nodes["roi-descriptive-body"].innerHTML;
  check("no card markup per scan", !html.includes("pg-card"));
  checkEqual("methodology written once",
             (nodes["roi-descriptive-method"].textContent.match(/population definition/g) || []).length, 1);
}

// ── Voxel counts ─────────────────────────────────────────────────────────
{
  const partial = { ...AVAILABLE, voxel_count: 1250, mask_voxel_count: 1300 };
  const nodes = run([partial], "available", makeDom());
  check("shows finite of mask when they differ",
        nodes["roi-descriptive-body"].innerHTML.includes("1250 of 1300"));
}

// ── Canonical payload helper, executed ───────────────────────────────────
// Static assertions alone let a wrong key path or a dropped status slip
// through, because nothing was running this function.
const payloadSrc = extractFunction("_roiDescriptivePayload");

function runPayload(analyses) {
  const sandbox = {
    _niftiAnalysisEntries: () => analyses,
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${payloadSrc}\n`, sandbox);
  return sandbox._roiDescriptivePayload();
}

console.log("\n=== Canonical payload helper ===\n");

{
  // Exactly the shape the real API returns.
  const analyses = [{
    reference_scoring: {
      roi_descriptive_statistics: [AVAILABLE],
      roi_descriptive_status: "available",
    },
  }];
  const [rows, status] = runPayload(analyses);
  checkEqual("reads records from reference_scoring", rows.length, 1);
  checkEqual("reads the canonical status", status, "available");
  checkEqual("passes the raw CoV ratio through unchanged",
             rows[0].roi_within_scan_cov, AVAILABLE.roi_within_scan_cov);
  check("CoV is still a ratio below 1", rows[0].roi_within_scan_cov < 1);
}

{
  // Status must survive even when there are no records — it explains why.
  const [rows, status] = runPayload([{
    reference_scoring: {
      roi_descriptive_statistics: [],
      roi_descriptive_status: "no_roi_configured",
    },
  }]);
  checkEqual("no records", rows.length, 0);
  checkEqual("status still reported", status, "no_roi_configured");
}

{
  // ASL/DSC: analysis present, but no ROI block at all.
  const [rows, status] = runPayload([{ reference_scoring: { maps: [] } }]);
  checkEqual("no rows for a non-DCE result", rows.length, 0);
  checkEqual("no status for a non-DCE result", status, null);
}

{
  const [rows] = runPayload([{}, { reference_scoring: null }]);
  checkEqual("missing reference_scoring is tolerated", rows.length, 0);
}

{
  // Rows accumulate across submissions in a batch.
  const [rows] = runPayload([
    { reference_scoring: { roi_descriptive_statistics: [AVAILABLE], roi_descriptive_status: "available" } },
    { reference_scoring: { roi_descriptive_statistics: [UNAVAILABLE], roi_descriptive_status: "available" } },
  ]);
  checkEqual("rows collected across submissions", rows.length, 2);
}

// ── Payload feeds the renderer end to end ────────────────────────────────
{
  const analyses = [{
    reference_scoring: {
      roi_descriptive_statistics: [AVAILABLE],
      roi_descriptive_status: "available",
    },
  }];
  const [rows, status] = runPayload(analyses);
  const nodes = run(rows, status, makeDom());
  check("payload -> renderer produces the formatted percentage",
        nodes["roi-descriptive-body"].innerHTML.includes("44.72%"));
  checkEqual("payload -> renderer produces one row", rowCount(nodes), 1);
}



/* ── Finding one row in a table of hundreds ───────────────────────────────
   One participant of the DCE set is six scans; ten across three sites is
   sixty, each contributing a row per map per region. The table is correct and
   unreadable. The filter narrows what is on screen and nothing else -- the
   count chip keeps stating the real total and the CSV is built from the
   records, so a filtered view can never become a partial export. */
console.log("\n=== ROI filter ===\n");

function makeFilterDom(records) {
  const rows = [];
  const body = {
    innerHTML: "",
    style: { display: "" },
    querySelectorAll: () => rows,
  };
  const nodes = makeDom();
  nodes["roi-descriptive-body"] = body;
  const extra = {
    "roi-descriptive-filter": { hidden: true },
    "roi-descriptive-search": { value: "" },
    "roi-descriptive-shown": { textContent: "" },
    "roi-descriptive-nomatch": { hidden: true },
  };
  const sandbox = {
    el: (id) => (id in extra ? extra[id] : nodes[id] || null),
    console,
    document: makeDocumentStub(),
  };
  vm.createContext(sandbox);
  vm.runInContext(`${escapeSrc}\n${roiSrc}\n`, sandbox);
  // The rows the filter walks, built from the same records the renderer uses.
  records.forEach((record) => {
    rows.push({ hidden: false, dataset: { roiSearch: sandbox._roiSearchText(record) } });
  });
  return { sandbox, extra, rows, nodes };
}

function scanRecords(count) {
  const out = [];
  for (let i = 0; i < count; i += 1) {
    out.push({
      ...AVAILABLE,
      participant: String((i % 10) + 1),
      site: String((i % 3) + 1),
      repeat: String((i % 2) + 1),
      map_type: "ktrans",
      roi_label: i % 2 ? "gray matter" : "white matter",
    });
  }
  return out;
}

{
  const { sandbox } = makeFilterDom([]);
  const text = sandbox._roiSearchText({
    ...AVAILABLE, participant: "7", site: "2", repeat: "1",
    map_type: "ktrans", roi_label: "gray matter",
  });
  check("the row is searchable by what the table shows", text.includes("gray matter"));
  check("and by the map type", text.includes("ktrans"));
  /* The table prints a participant as "7", but somebody looking for it types
     "P7", so both are in the haystack. */
  checkEqual("participant is findable as typed", Number(text.includes("p7")), 1);
  check("so is a site", text.includes("site 2"));
  check("so is a repeat", text.includes("repeat 1"));
  check("everything is lower case, so the search can be too",
    text === text.toLowerCase());
}

{
  const { sandbox, extra, rows } = makeFilterDom(scanRecords(60));
  sandbox._syncRoiFilter();
  check("a long table offers a filter", extra["roi-descriptive-filter"].hidden === false);
  checkEqual("nothing is hidden before anything is typed",
    rows.filter((r) => r.hidden).length, 0);
  check("and the count states the real total",
    extra["roi-descriptive-shown"].textContent === "60 rows",
    extra["roi-descriptive-shown"].textContent);

  extra["roi-descriptive-search"].value = "site 2";
  sandbox._applyRoiFilter();
  const shown = rows.filter((r) => !r.hidden).length;
  checkEqual("filtering hides the rest", shown, 20);
  check("and says how many of how many",
    extra["roi-descriptive-shown"].textContent === "20 of 60 rows",
    extra["roi-descriptive-shown"].textContent);
  check("a match is not an empty state", extra["roi-descriptive-nomatch"].hidden === true);

  extra["roi-descriptive-search"].value = "GRAY MATTER";
  sandbox._applyRoiFilter();
  check("the search is case insensitive",
    rows.filter((r) => !r.hidden).length === 30,
    String(rows.filter((r) => !r.hidden).length));

  extra["roi-descriptive-search"].value = "p3";
  sandbox._applyRoiFilter();
  check("a participant is findable the way a reader writes it",
    rows.filter((r) => !r.hidden).length === 6,
    String(rows.filter((r) => !r.hidden).length));

  extra["roi-descriptive-search"].value = "lesion";
  sandbox._applyRoiFilter();
  checkEqual("no match hides every row", rows.filter((r) => !r.hidden).length, 0);
  check("and says so rather than showing an empty table",
    extra["roi-descriptive-nomatch"].hidden === false);

  extra["roi-descriptive-search"].value = "";
  sandbox._applyRoiFilter();
  checkEqual("clearing the box brings every row back",
    rows.filter((r) => r.hidden).length, 0);
  check("and the empty message goes with it",
    extra["roi-descriptive-nomatch"].hidden === true);
}

{
  const { sandbox, extra } = makeFilterDom(scanRecords(4));
  sandbox._syncRoiFilter();
  check("a short table is left alone", extra["roi-descriptive-filter"].hidden === true);
}

{
  /* A filter that survived a re-render would silently hide rows of the next
     submission. Hiding the control clears it. */
  const { sandbox, extra } = makeFilterDom(scanRecords(4));
  extra["roi-descriptive-search"].value = "site 2";
  sandbox._syncRoiFilter();
  check("hiding the filter clears what was typed in it",
    extra["roi-descriptive-search"].value === "");
}

console.log("\nThe filter changes the view and nothing else");
check("the export reads records, not the visible rows",
  !/roi-descriptive-body[\s\S]{0,200}export-roi-descriptive/.test(appJs));
check("the count chip is set from the records",
  /count\.textContent = String\(rows\.length\)/.test(appJs));

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
