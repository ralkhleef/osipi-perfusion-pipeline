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
                    "roi-descriptive-count", "roi-descriptive-method"]) {
    nodes[id] = makeElement(id);
  }
  return nodes;
}

function run(records, status, nodes) {
  const sandbox = { el: (id) => nodes[id] || null, console };
  vm.createContext(sandbox);
  vm.runInContext(`${escapeSrc}\n${roiSrc}\n`, sandbox);
  sandbox.renderRoiDescriptiveStatistics(records, status);
  return nodes;
}

function rowCount(nodes) {
  const html = nodes["roi-descriptive-body"].innerHTML;
  return (html.match(/<tr>/g) || []).length;
}

// ── Fixtures matching the real canonical record shape ────────────────────
const AVAILABLE = {
  dataset: "synthetic", participant: "1", repeat: "1", site: "1",
  roi_id: "tumour", roi_label: "Tumour",
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
  check("ROI label displayed", html.includes("Tumour"));
  check("dataset title-cased for display", html.includes("Synthetic"));
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
  check("null site renders as a dash", html.includes("<td>—</td>"), html);
  check("null site never renders the word null", !html.includes(">null<"));
  check("null site never renders undefined", !html.includes("undefined"));
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
        nodes["roi-descriptive-empty"].textContent.includes("No valid Ktrans scans"));
}

// ── Every canonical status maps to a message ─────────────────────────────
{
  const cases = {
    no_roi_configured: "no ROI masks were configured",
    no_eligible_maps: "No valid Ktrans scans",
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
        !threw && nodes["roi-descriptive-empty"].textContent.includes("No ROI Ktrans statistics are available"));
}

// ── ASL / DSC: no payload, no section ────────────────────────────────────
{
  const nodes = run([], null, makeDom());
  checkEqual("section hidden with no records and no status",
             nodes["roi-descriptive-card"].style.display, "none");
}
{
  // Populated DCE, then an ASL result carrying nothing.
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

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
