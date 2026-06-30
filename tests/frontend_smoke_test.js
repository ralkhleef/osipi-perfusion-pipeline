#!/usr/bin/env node
/**
 * DOM smoke test for frontend/index.html
 * Uses only Node.js stdlib — no external packages required.
 * Run: node tests/frontend_smoke_test.js
 */
"use strict";

const fs = require("fs");
const path = require("path");

const htmlPath = path.resolve(__dirname, "../frontend/index.html");
const jsPath = path.resolve(__dirname, "../frontend/app.js");
const cssPath = path.resolve(__dirname, "../frontend/styles.css");
const html = fs.readFileSync(htmlPath, "utf8");
const appJs = fs.readFileSync(jsPath, "utf8");
const css = fs.readFileSync(cssPath, "utf8");
let mainPy = "";
try { mainPy = fs.readFileSync(path.resolve(__dirname, "../backend/main.py"), "utf8"); } catch (_) {}

let passed = 0;
let failed = 0;

function hasId(id) {
  // Match id="..." or id='...' with possible surrounding whitespace
  return new RegExp(`id=["']${id}["']`).test(html);
}

function hasClass(cls) {
  return new RegExp(`class=["'][^"']*${cls}[^"']*["']`).test(html);
}

function check(desc, id) {
  if (hasId(id)) {
    console.log(`  ✓  ${desc}  (#${id})`);
    passed++;
  } else {
    console.error(`  ✗  MISSING: ${desc}  (#${id})`);
    failed++;
  }
}

function checkCls(desc, cls) {
  if (hasClass(cls)) {
    console.log(`  ✓  ${desc}  (.${cls})`);
    passed++;
  } else {
    console.error(`  ✗  MISSING: ${desc}  (.${cls})`);
    failed++;
  }
}

function checkContains(desc, haystack, needle) {
  if (haystack.includes(needle)) {
    console.log(`  ✓  ${desc}`);
    passed++;
  } else {
    console.error(`  ✗  MISSING: ${desc}`);
    failed++;
  }
}

function checkNotContains(desc, haystack, needle) {
  if (!haystack.includes(needle)) {
    console.log(`  ✓  ${desc}`);
    passed++;
  } else {
    console.error(`  ✗  UNEXPECTED: ${desc}`);
    failed++;
  }
}

function checkOrder(desc, haystack, needles) {
  let pos = -1;
  for (const needle of needles) {
    const next = haystack.indexOf(needle, pos + 1);
    if (next < 0) {
      console.error(`  ✗  ORDER: ${desc} missing ${needle}`);
      failed++;
      return;
    }
    if (next < pos) {
      console.error(`  ✗  ORDER: ${desc} out of order at ${needle}`);
      failed++;
      return;
    }
    pos = next;
  }
  console.log(`  ✓  ${desc}`);
  passed++;
}

function section(id) {
  const start = html.indexOf(`<section id="${id}"`);
  if (start < 0) return "";
  const next = html.indexOf("<section id=", start + 1);
  return html.slice(start, next < 0 ? html.length : next);
}

console.log("\n=== OSIPI Frontend DOM Smoke Test ===\n");

console.log("[ Step panels ]");
check("Upload step panel",          "step-upload");
check("Index step panel",           "step-index");
check("Validate step panel",        "step-validate");
check("Run step panel",             "step-run");
check("Score step panel",           "step-score");
check("Results Summary step panel", "step-summary");
check("Export step panel",          "step-export");

console.log("\n[ Upload form ]");
check("Submit button",       "submit-btn");
check("Team name input",     "team-name");
check("Contact email input", "contact-email");
check("Source tab: local",   "tab-local");
check("Source tab: zenodo",  "tab-zenodo");
check("Source tab: github",  "tab-github");
check("Drop zone",           "drop-zone");

console.log("\n[ Wizard navigation ]");
check("Legacy wizard footer holder", "wizard-footer");
check("Legacy wizard back button",   "wf-back-btn");
check("Legacy wizard next button",   "wf-next-btn");
if (!hasId("compact-progress")) {
  console.log("  ✓  compact-progress nav absent (removed by request)");
  passed++;
} else {
  console.error("  ✗  compact-progress nav still present — should have been removed");
  failed++;
}
checkCls("Mirrored step shell", "step-shell");
checkContains("Sticky footer hidden on every step", css, 'body[data-step] .wizard-footer');
checkContains("Legacy footer cannot display", css, '.wizard-footer:not([hidden])');
checkContains("Upload in-card submit button visible", css, 'body[data-step="upload"] .upload-form-card .form-actions .submit-btn');
checkContains("Upload uses in-card CTA handler", appJs, 'submitBtn.addEventListener("click", handleSubmit)');
checkContains("Upload readiness gated on selected source", appJs, "function _canUpload");
checkContains("Upload local source gates on file selection", appJs, "return !!state.pendingLocalFiles");
checkContains("Upload Zenodo source gates on input", appJs, 'source === "zenodo"');
checkContains("Upload GitHub source gates on input", appJs, 'source === "github"');
checkContains("Upload CTA disabled until ready", appJs, "submitBtn.disabled = !canUpload");
checkContains("Challenge type tooltip text", html, "Select the OSIPI challenge type for this submission.");
checkContains("Parameter map type tooltip text", html, "auto-detect CBF, ATT, Ktrans, ve, vp, or Kep");
checkNotContains("Duplicate global Start New wrapper removed", html, "global-start-new");
checkNotContains("Global Start New styling removed", css, "global-new-btn");
checkNotContains("Duplicate step hint cards removed", html, "step-hint-card");
checkNotContains("Step hint card styling removed", css, "step-hint-card");
checkContains("Reusable in-card action rows", appJs, "function _ensureStepActionRow");
checkContains("Action rows have data-step hook", appJs, "data-step-action-row");
checkContains("Action row CSS", css, ".step-action-row");
checkContains("Action row disabled tooltip", css, ".step-action-row[data-disabled-reason]");
checkContains("Review action label", appJs, 'nextLabel: "Validate Submission"');
checkContains("Validate action label", appJs, 'nextLabel: "Continue to Run"');
checkContains("Run action label", appJs, 'nextLabel: "Continue to Score"');
checkContains("Score action label", appJs, 'nextLabel: "Continue"');
checkContains("Summary action label", appJs, 'nextLabel: "Continue to Export"');
checkContains("Export action label", appJs, 'nextLabel: "Finish"');
checkContains("Blocked-step reason helper", appJs, "function _stepBlockedReason");
checkContains("Validate Continue gates on error count (warnings don't block)", appJs, 'issueCount(r, "errors") === 0');
checkContains("Action row disabled when blocked", appJs, "primaryBtn.disabled = !canProceed");
checkContains("Restored sessions sync in-card actions", appJs, "_syncStepActionRow(step)");
checkContains("Progress sync helper", appJs, "function _syncCompactProgress");
checkContains("Step shell card styling", css, ".step-shell {");
checkContains("Step shell header styling", css, ".step-shell-header");

console.log("\n[ Hidden state-holder buttons ]");
check("WF state holder",     "wf-state-holder");
check("WF btn: upload",      "wf-btn-upload");
check("WF btn: index",       "wf-btn-index");
check("WF btn: validate",    "wf-btn-validate");
check("WF btn: run",         "wf-btn-run");
check("WF btn: score",       "wf-btn-score");
check("WF btn: summary",     "wf-btn-summary");
check("WF btn: export",      "wf-btn-export");

console.log("\n[ Index step ]");
check("Batch validate all",  "batch-validate-all-btn");
checkContains("Review title is singular-capable", html, 'id="batch-index-title"');
checkContains("Review renders submission cards", appJs, 'guided-sub-card');
checkContains("Single-submission review card mode", appJs, 'sub-card--single');
checkNotContains("Index step has no old table markup", section("step-index"), "<table");
checkContains("Continue validates selected submissions", appJs, "runBatchValidation(selected)");
checkContains("Review action row appends inside card", appJs, 'el("step-index")?.querySelector(".pg-card")');
checkContains("Review submission type tooltip", appJs, "Result maps provided help");
checkContains("Review has multi-submission filter bar", appJs, "function _renderIndexFilterBar");
checkContains("Review search filters submissions", appJs, "index-search");
checkContains("Review map type dropdown filter", appJs, '"index-map"');
checkContains("Review status dropdown filter", appJs, '"index-status"');
checkContains("Review sort dropdown filter", appJs, '"index-sort"');
checkContains("Review clear filters button", appJs, "index-clear-filters");
checkContains("Review list is collapsible", appJs, '_renderCollapsibleSection("index"');
checkContains("Review show all submissions", appJs, "index-show-all-btn");
checkContains("Detected submission cards show metadata chips", appJs, "sub-card-tags");

console.log("\n[ Run step ]");
check("Run submissions list","run-submissions-list");
check("Batch exec all btn",  "batch-exec-all-btn");
checkContains("Result-only run shows skipped card", html, "Execution skipped");
checkContains("Result-only run hides per-submission list", appJs, 'list.innerHTML = ""');
checkContains("Result-only run hides duplicate continue", appJs, 'skippedContinueBtn.style.display = "none"');
checkContains("Run readiness tooltip", html, "Runnable submissions include executable code. Result-only submissions skip execution and go directly to scoring.");
checkContains("Run button says Docker", html, "Run code in Docker");
check("Run collapsible section", "run-list-section");
checkContains("Run has polished filter bar", appJs, "function _renderRunFilterBar");
checkContains("Run compact filter search", appJs, "run-search");
checkContains("Run status dropdown", appJs, '"run-status"');
checkContains("Run map dropdown", appJs, '"run-map"');
checkContains("Run sort dropdown", appJs, '"run-sort"');
checkContains("Run skipped filter chip", appJs, '"skipped"');
checkContains("Run filter keeps Continue logic separate", appJs, "_applyRunFilters();");

console.log("\n[ Score step ]");
check("Score not-configured card", "score-not-configured-card");
check("Score status card",         "score-status-card");
check("Score table card",          "score-table-card");
check("Run Scoring button",        "btn-score-all");
checkContains("Score configured shows Run Scoring", html, "Run Scoring");
checkContains("Score duplicate continue hidden", css, "#btn-score-continue");
checkContains("Score not configured is one card", appJs, "if (notConfiguredCard) notConfiguredCard.style.display = \"\"");
checkContains("Score table hidden until useful", appJs, 'tableCard.style.display = "none"');
checkContains("Score metric preview present", html, 'id="score-metric-preview"');
checkContains("QC metrics tooltip", html, "QC metrics describe map validity and statistics. They are not official OSIPI scores.");
checkContains("Reference scoring status tooltip", html, "Reference metrics are calculated only when a matching private ground-truth map is available.");
checkContains("Leaderboard professional status badges", appJs, "leaderboard-status-badge");
checkContains("Leaderboard long-name truncation", css, ".leaderboard-submission-cell span");
checkContains("Leaderboard timestamp formatting", appJs, "function _formatLeaderboardTimestamp");
check("Leaderboard filter bar", "leaderboard-filter-bar");
check("Leaderboard list", "leaderboard-list");
check("Leaderboard count", "leaderboard-count");
check("Leaderboard collapsible summary", "leaderboard-section-summary");
checkContains("Leaderboard can collapse and expand", html, 'data-collapse-toggle="leaderboard"');
checkContains("Leaderboard has custom filter dropdown helper", appJs, "function _renderFilterDropdown");
checkContains("Leaderboard status dropdown", appJs, '"leaderboard-status"');
checkContains("Leaderboard map dropdown", appJs, '"leaderboard-map"');
checkContains("Leaderboard sort dropdown", appJs, '"leaderboard-sort"');
checkNotContains("Leaderboard date dropdown removed", appJs, '"leaderboard-date"');
checkNotContains("Leaderboard challenge dropdown removed", appJs, '"leaderboard-challenge"');
checkContains("Dropdown opens from filter pill", appJs, "data-filter-menu");
checkContains("Dropdown selected option checkmark", appJs, "filter-option-check");
checkContains("Only one dropdown stays open", appJs, "function _closeFilterMenus");
checkContains("Escape closes dropdowns", appJs, 'e.key === "Escape"');
checkContains("Leaderboard search filters rows", appJs, "leaderboard-search");
checkContains("Review search restores focus after filtering", appJs, '_restoreSearchFocus("index-search"');
checkContains("Leaderboard clear filters restores rows", appJs, "leaderboard-clear-filters");
checkContains("Leaderboard empty state for filters", appJs, "No submissions match these filters.");
checkContains("Leaderboard show more works", appJs, "leaderboard-show-all-btn");
checkContains("Leaderboard loading state", appJs, "leaderboard-loading");
checkContains("Leaderboard error retry state", appJs, "leaderboard-retry-btn");
checkContains("Leaderboard row cards render", appJs, "leaderboard-row");
checkContains("Leaderboard row actions include View results", appJs, "View results");
checkContains("Leaderboard row actions include Export", appJs, "Export</a>");
checkContains("Leaderboard row actions include Details", appJs, "data-leaderboard-detail");
checkContains("Leaderboard status supports scored", css, ".leaderboard-status-scored");
checkContains("Leaderboard status supports failed", css, ".leaderboard-status-failed");
checkContains("Leaderboard status supports not configured", css, ".leaderboard-status-not_configured");
checkContains("Leaderboard reference unavailable badge", css, ".leaderboard-status-reference_not_available");
checkContains("Leaderboard partial reference badge", css, ".leaderboard-status-partial_reference_scoring");
checkContains("Leaderboard long-name card truncation", css, ".leaderboard-submission-name");
checkContains("Reusable filter bar CSS", css, ".filter-bar");
checkContains("Reusable compact filter bar CSS", css, ".compact-filter-bar");
checkContains("Reusable collapsible section CSS", css, ".collapsible-section");
checkContains("Reusable collapsible section header CSS", css, ".collapsible-section-header");
checkContains("Reusable collapse chevron CSS", css, ".collapse-chevron");
checkContains("Reusable show more row CSS", css, ".show-more-row");
checkContains("Reusable list summary strip CSS", css, ".list-summary-strip");
checkContains("Filter menu CSS", css, ".filter-menu");
checkContains("Filter pill CSS", css, ".filter-pill");

console.log("\n[ Results Summary step ]");
check("Summary report container",   "summary-cards");
checkContains("Summary has numeric metric filter", appJs, "function _numericMetricEntries");
checkContains("Summary Continue is always enabled once reached", appJs, 'case "summary": return true;');
checkContains("Summary action routes to Export", appJs, 'summary:  { back: "score",    next: "export"');
checkContains("Summary renders mini-report", appJs, 'container.className = "summary-report"');
checkContains("Summary has Final Output section", appJs, "Final Output");
checkContains("Summary has Key QC Summary section", appJs, "Key QC Summary");
checkContains("Summary has Image Preview section", appJs, "Image Preview");
checkContains("Summary has Reference-Based Scoring section", appJs, "Reference-Based Scoring");
checkContains("Summary has Export Readiness section", appJs, "Export Readiness");
checkOrder("Summary sections render in report order", appJs, [
  "finalOutputHtml",
  "qcSummaryHtml",
  "imagePreviewHtml",
  "referenceReportHtml",
  "checklistHtml",
  "detailsHtml",
]);
checkContains("Summary reference unavailable text", appJs, "Reference scoring unavailable — showing QC metrics only.");
checkContains("Summary null metrics are not zeroed", appJs, "function _metricOrUnavailable");
checkContains("Summary sections stack vertically", css, "flex-direction: column;");
checkContains("Summary scientific metric tooltip helper", appJs, "function _summaryMetricTooltip");
checkContains("Summary finite voxels tooltip", appJs, "Percent of voxels that are valid numbers, excluding NaN and Inf.");
checkContains("Summary negative voxels tooltip", appJs, "Percent of voxels below zero.");
checkContains("Summary CoV tooltip", appJs, "Standard deviation divided by mean.");
checkContains("Summary RMSE tooltip", appJs, "Root mean squared error between the submitted map and reference map.");
checkContains("Summary MAE tooltip", appJs, "Mean absolute error between the submitted map and reference map.");
checkContains("Summary Bias tooltip", appJs, "Mean signed difference between submitted and reference values.");
checkContains("Summary has NIfTI technical table", appJs, "summary-nifti-table");
checkContains("Summary has reference technical table", appJs, "summary-reference-table");
checkContains("Summary shows RMSE when reference available", appJs, "RMSE");
checkContains("Summary supports partial reference scoring", appJs, "partial_reference_scoring");
checkContains("Summary shows per-map reference status", appJs, "referenceMapStatusText");
checkContains("Summary has export checklist", appJs, "summary-export-checklist");
checkContains("Summary shows finite voxels label", appJs, "Finite voxels");
checkContains("Summary shows coefficient of variation label", appJs, "Coefficient of variation");
checkContains("Summary shows standard deviation label", appJs, "Standard deviation");
checkContains("Summary shows map count label", appJs, "Map count");
checkContains("Summary caches NIfTI analysis", appJs, "niftiAnalysis");
checkContains("Summary details collapsed", appJs, 'class="summary-details"');
checkContains("Summary technical details title", appJs, "Technical Details");
checkContains("Summary technical details include QC JSON", appJs, "QC summary JSON");
checkContains("Image Preview renders cards", appJs, "nifti-preview-card");
checkContains("Image Preview thumbnail opens modal", appJs, "data-open-preview-map");
checkContains("Image Preview Preview button opens modal", appJs, "preview-open-btn");
checkContains("Preview modal exists", appJs, "nifti-preview-modal");
checkContains("Preview modal close on Escape", appJs, 'e.key === "Escape"');
checkContains("Preview modal shows file stats", appJs, "nifti-preview-modal-meta");
checkContains("Open full preview link exists", appJs, "Open full preview");
checkContains("Download for ITK-SNAP action exists", appJs, "Download NIfTI for ITK-SNAP");
checkContains("Full viewer guidance mentions external NIfTI viewers", appJs, "ITK-SNAP, FSLeyes, 3D Slicer");
checkContains("Preview cards styled", css, ".nifti-preview-card");
checkContains("Preview modal styled", css, ".nifti-preview-modal-backdrop");
checkContains("Preview routes exposed by backend", mainPy, "/api/submissions/{submission_id}/previews");
checkContains("Preview PNG route exposed by backend", mainPy, "/api/submissions/{submission_id}/previews/{map_id}/{plane}.png");
checkContains("Preview download route exposed by backend", mainPy, "/api/submissions/{submission_id}/maps/{map_id}/download");
checkContains("Full preview route exposed by backend", mainPy, "/preview/{submission_id}/{map_id}");
checkNotContains("Summary no longer assembles old dashboard grid", appJs, "summary-nifti-grid");
checkNotContains("Summary does not show package line as metric", appJs, "summary-pkg-line");
checkNotContains("Summary no longer uses old four-card output", appJs, "valCard + execCard + scoreCard + exportCard");
checkNotContains("Summary has no visible demo footnote", appJs, "demo/QC, not official OSIPI scoring");
checkNotContains("Report has no visible demo-only banner", mainPy, "Demo / QC scoring only");

console.log("\n[ Export step ]");
check("Batch export blinded btn",    "batch-export-blinded-btn");
check("Batch export unblinded btn",  "batch-export-unblinded-btn");
check("Exec export blinded btn",     "batch-export-exec-blinded-btn");
check("Scoring export blinded btn",  "export-scoring-blinded-btn");
check("Combined export blinded btn", "export-combined-blinded-btn");
check("Combined export unblinded btn","export-combined-unblinded-btn");
check("HTML report button",          "export-report-btn");
checkContains("Export has Combined CSV label", html, "Combined CSV");
checkContains("Export has Validation CSV button", html, "Download Validation CSV");
checkContains("Export has Execution CSV button", html, "Download Execution CSV");
checkContains("Export has Scoring CSV button", html, "Download Scoring CSV");
checkContains("Export has Combined CSV button", html, "Download Combined CSV");
checkContains("Export has HTML report button", html, "Open HTML Report");
checkContains("Export has Blinded Export label", html, "Download Blinded Export");
checkContains("Export has Unblinded Export buttons", html, "Download Unblinded Export");

console.log("\n[ Validate step cards ]");
checkContains("Validate title is status-driven", html, 'id="validate-card-title"');
checkContains("Validate totals strip exists", html, 'id="validate-summary-stats"');
check("Validate collapsible section", "validation-list-section");
checkContains("Validate renders cards", appJs, 'validation-card');
checkContains("Validate details collapsed by default", appJs, 'class="vr-row-detail" style="display:none"');
checkContains("Validate edit controls are header-local", html, 'class="validation-header-actions"');
checkNotContains("Validate edit controls no longer float below card", html, 'id="single-result-actions" class="step-action-bar"');
checkContains("Validate details button still inside cards", appJs, "vr-details-btn");
checkContains("Validate has polished filter bar", appJs, "function _renderValidationFilterBar");
checkContains("Validate filter all", appJs, '"validation-status"');
checkContains("Validate filter passed", appJs, '"passed"');
checkContains("Validate filter warnings", appJs, '"warnings"');
checkContains("Validate filter errors", appJs, '"errors"');
checkContains("Validate filter ready", appJs, '"ready"');
checkContains("Validate filter skipped", appJs, '"skipped"');
checkContains("Validate map dropdown", appJs, '"validation-map"');
checkContains("Validate sort dropdown", appJs, '"validation-sort"');
checkContains("Validation filters preserve error visibility", appJs, "case \"errors\"");

console.log("\n[ Tooltips for key terms ]");
function countMatches(re) { return (html.match(re) || []).length; }
const tooltipCount = countMatches(/class=["'][^"']*help-tooltip[^"']*["']/g);
if (tooltipCount >= 3) {
  console.log(`  ✓  Help tooltips present (${tooltipCount} found)`);
  passed++;
} else {
  console.error(`  ✗  Expected >=3 help tooltips, found ${tooltipCount}`);
  failed++;
}
checkCls("Tooltip text spans", "tooltip-text");
checkContains("Reusable dynamic tooltip helper", appJs, "function helpTooltip");
checkContains("Keyboard tooltip focus-visible", css, ".help-tooltip:focus-visible .tooltip-text");
checkContains("Mobile tooltip tap via focus", css, ".help-tooltip:focus-within .tooltip-text");
checkNotContains("Old long challenge tooltip removed", html, "The OSIPI challenge category: ASL");
checkNotContains("Old long QC tooltip removed", html, "A QC/demo package computes quality-control metrics");

console.log("\n[ Topbar / session ]");
check("Topbar new-session btn",    "sidebar-new-session-btn");
check("Session chip",              "session-chip");
check("Restore banner",            "restore-banner");
checkNotContains("Old sidebar collapse JS removed", appJs, "initSidebarCollapse");

console.log("\n[ CSS class presence ]");
// .app-topbar intentionally removed (topbar removed in v53)
checkCls("Upload form card",  "upload-form-card");
checkContains("Button base class", css, ".btn,");
checkContains("Button primary class", css, ".btn-primary");
checkContains("Button secondary class", css, ".btn-secondary");
checkContains("Button ghost class", css, ".btn-ghost");
checkContains("Button danger class", css, ".btn-danger");
checkContains("Button success class", css, ".btn-success");
checkContains("Button icon class", css, ".btn-icon");
checkContains("Button loading class", css, ".btn-loading");
checkContains("Button full-width class", css, ".btn-full");
checkContains("Button small class", css, ".btn-sm");
checkContains("Focus-visible styles", css, ":focus-visible");
checkContains("Loading class toggled in JS", appJs, "classList.add(\"btn-loading\")");

console.log("\n[ Top step nav must be absent ]");
// The top step nav was removed — verify it is NOT in the HTML
if (!hasId("top-step-nav")) {
  console.log("  ✓  top-step-nav absent (correct — removed by design)");
  passed++;
} else {
  console.error("  ✗  top-step-nav found in HTML — should have been removed");
  failed++;
}
checkNotContains("Old sidebar container absent", html, 'id="sidebar"');

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
