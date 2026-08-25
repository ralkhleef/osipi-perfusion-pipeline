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
const logoPath = path.resolve(__dirname, "../frontend/assets/logo.svg");
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
    console.log(`  OK  ${desc}  (#${id})`);
    passed++;
  } else {
    console.error(`  FAIL  MISSING: ${desc}  (#${id})`);
    failed++;
  }
}

function checkCls(desc, cls) {
  if (hasClass(cls)) {
    console.log(`  OK  ${desc}  (.${cls})`);
    passed++;
  } else {
    console.error(`  FAIL  MISSING: ${desc}  (.${cls})`);
    failed++;
  }
}

function checkContains(desc, haystack, needle) {
  if (haystack.includes(needle)) {
    console.log(`  OK  ${desc}`);
    passed++;
  } else {
    console.error(`  FAIL  MISSING: ${desc}`);
    failed++;
  }
}

function checkNotContains(desc, haystack, needle) {
  if (!haystack.includes(needle)) {
    console.log(`  OK  ${desc}`);
    passed++;
  } else {
    console.error(`  FAIL  UNEXPECTED: ${desc}`);
    failed++;
  }
}

function checkCondition(desc, condition, detail = "") {
  if (condition) {
    console.log(`  OK  ${desc}`);
    passed++;
  } else {
    console.error(`  FAIL  ${desc}${detail ? ` (${detail})` : ""}`);
    failed++;
  }
}

function checkOrder(desc, haystack, needles) {
  let pos = -1;
  for (const needle of needles) {
    const next = haystack.indexOf(needle, pos + 1);
    if (next < 0) {
      console.error(`  FAIL  ORDER: ${desc} missing ${needle}`);
      failed++;
      return;
    }
    if (next < pos) {
      console.error(`  FAIL  ORDER: ${desc} out of order at ${needle}`);
      failed++;
      return;
    }
    pos = next;
  }
  console.log(`  OK  ${desc}`);
  passed++;
}

function countOccurrences(haystack, needle) {
  return haystack.split(needle).length - 1;
}

function checkEqual(desc, actual, expected) {
  if (actual === expected) {
    console.log(`  OK  ${desc}`);
    passed++;
  } else {
    console.error(`  FAIL  ${desc}: expected ${expected}, got ${actual}`);
    failed++;
  }
}

function checkNoDecorativeGlyphs(desc, haystack) {
  const decorativeGlyphPattern = /[\u{1F300}-\u{1FAFF}\u{2300}-\u{23FF}\u{2600}-\u{27BF}]/u;
  const match = decorativeGlyphPattern.exec(haystack);
  if (!match) {
    console.log(`  OK  ${desc}`);
    passed++;
  } else {
    console.error(`  FAIL  ${desc}: found U+${match[0].codePointAt(0).toString(16).toUpperCase()}`);
    failed++;
  }
}

function section(id) {
  const start = html.indexOf(`<section id="${id}"`);
  if (start < 0) return "";
  const next = html.indexOf("<section id=", start + 1);
  return html.slice(start, next < 0 ? html.length : next);
}

const renderBatchBlock = appJs.slice(
  appJs.indexOf("function renderBatchTable"),
  appJs.indexOf("function _syncBatchHeaderCheckbox")
);
const indexFilterBarBlock = appJs.slice(
  appJs.indexOf("function _renderIndexFilterBar"),
  appJs.indexOf("function _filterIndexSubmissions")
);
const validationFilterBarBlock = appJs.slice(
  appJs.indexOf("function _renderValidationFilterBar"),
  appJs.indexOf("function _wireValidationFilterBar")
);
const runFilterBarBlock = appJs.slice(
  appJs.indexOf("function _renderRunFilterBar"),
  appJs.indexOf("function _wireRunFilterBar")
);
const leaderboardFilterBarBlock = appJs.slice(
  appJs.indexOf("function _renderLeaderboardFilterBar"),
  appJs.indexOf("function _filteredLeaderboardEntries")
);
const filterDropdownBlock = appJs.slice(
  appJs.indexOf("function _renderFilterDropdown"),
  appJs.indexOf("function _renderSearchBox")
);
const scoreNotConfiguredBlock = appJs.slice(
  appJs.indexOf('if (activeMode === "none")'),
  appJs.indexOf("// ── 2. Scoring is configured")
);

console.log("\n=== OSIPI Frontend DOM Smoke Test ===\n");

console.log("[ Step panels ]");
check("Upload step panel",          "step-upload");
check("Index step panel",           "step-index");
check("Validate step panel",        "step-validate");
check("Run step panel",             "step-run");
check("Score step panel",           "step-score");
check("Export step panel",          "step-export");
checkNotContains("Standalone Results Summary step removed", html, 'id="step-summary"');
checkEqual("Wizard has 6 hidden state steps", countOccurrences(html, 'class="wf-step'), 6);

console.log("\n[ Upload form ]");
check("Submit button",       "submit-btn");
check("Team name input",     "team-name");
check("Contact email input", "contact-email");
check("Source tab: local",   "tab-local");
check("Source tab: zenodo",  "tab-zenodo");
check("Source tab: github",  "tab-github");
check("Drop zone",           "drop-zone");

console.log("\n[ Config-driven challenge controls ]");
checkContains("Static HTML challenge placeholder", html, "Configured challenges will appear here.");
checkNotContains("Static HTML has no ASL option", html, 'value="asl"');
checkNotContains("Static HTML has no DCE option", html, 'value="dce"');
checkNotContains("Static HTML has no DSC option", html, 'value="dsc"');
checkNotContains("Static HTML has no Ktrans fallback", section("step-upload"), "Ktrans");
checkContains("Challenge options fetched from config endpoint", appJs, "/api/config");
checkContains("Challenge options render from config array", appJs, "options.map((challenge)");
checkContains("Unknown configured challenge IDs render generically", appJs, "challenge.label || id.toUpperCase()");
checkContains("Map options derive from expected map config", appJs, "challenge.expected_maps");
checkContains("Config failure shows neutral message", appJs, "Configuration could not be loaded. Challenge options are unavailable.");
checkContains("No silent static fallback after config failure", appJs, "_showConfigLoadError");

console.log("\n[ Decorative imagery / emoji cleanup ]");
checkNoDecorativeGlyphs("No decorative emoji/glyphs in HTML UI", html);
checkNoDecorativeGlyphs("No decorative emoji/glyphs in app UI strings", appJs);
checkNoDecorativeGlyphs("No decorative emoji/glyphs in CSS generated content", css);
checkCondition("OSIPI branding logo asset exists", fs.existsSync(logoPath), logoPath);
checkContains("Headers use restored OSIPI logo", html, 'src="/static/assets/logo.svg" alt="OSIPI logo" class="brand-logo"');
checkEqual("Static HTML image tags are the six shared branding logos", countOccurrences(html, "<img"), 6);
checkNotContains("No plain OS placeholder in headers", html, "brand-mark");
checkNotContains("No broken image src", html, 'src=""');
// 2 NIfTI preview thumbnails + 1 shared submission IMG icon.
checkEqual("Functional image tags in JS (2 preview + 1 submission icon)", countOccurrences(appJs, "<img"), 3);
checkContains("The extra image tag is the submission IMG icon", appJs, '<img src="/static/assets/submission-img-icon.png"');
checkContains("Functional NIfTI thumbnail image remains", appJs, 'src="${escapeHtml(item.thumbnail_url)}"');
checkContains("Functional preview modal image remains", appJs, 'class="nifti-preview-modal-image"');
// Parameter Map Previews: gallery filtered to 3-D recognized maps; no "Unknown"
checkContains("Preview gallery filters to parameter maps", appJs, "function _isParameterMapPreview");
checkContains("Preview section renamed to Parameter Map Previews", appJs, "<h3>Parameter Map Previews</h3>");
checkNotContains("Old 'Map Preview' heading removed", appJs, "<h3>Map Preview</h3>");
checkContains("Empty state uses parameter-map wording", appJs, "No parameter-map previews are available.");
checkContains("4D ASL data label kept for non-parameter files", appJs, "4D ASL data");
checkContains("Non-parameter files listed under collapsed details", appJs, "submitted-files-details");
checkNotContains("No decorative CSS url assets", css, "url(");
checkNotContains("No decorative CSS background-image", css, "background-image");

console.log("\n[ Wizard navigation ]");
check("Legacy wizard footer holder", "wizard-footer");
check("Legacy wizard back button",   "wf-back-btn");
check("Legacy wizard next button",   "wf-next-btn");
// The top session-summary card and numbered stepper were removed entirely
// (HTML + JS render logic + CSS), not just hidden.
checkNotContains("Compact progress nav removed from HTML", html, 'id="compact-progress"');
checkNotContains("Session summary card removed from HTML", html, "workflow-session-summary");
checkNotContains("Session-card Start New removed from HTML", html, "workflow-start-new-btn");
checkNotContains("Workflow shell render helper removed", appJs, "function _syncWorkflowShell");
checkNotContains("Compact progress builder removed", appJs, "function _ensureCompactProgress");
checkNotContains("Compact progress CSS removed", css, ".compact-progress-item {");
check("Internal step-state holders preserved", "wf-state-holder");
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
checkContains("Challenge type tooltip text", html, "Select a challenge defined by the active pipeline configuration.");
checkContains("Parameter map type tooltip text", html, "Use this only when automatic detection needs a hint.");
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
const footerConfigBlock = appJs.slice(
  appJs.indexOf("const _WF_FOOTER_CONFIG"),
  appJs.indexOf("function _selectedSubmissionCount")
);
checkContains("Run action label", footerConfigBlock, 'nextLabel: "Continue to QC & Preview"');
checkContains("Score action label", footerConfigBlock, 'nextLabel: "Continue to Export"');
checkNotContains("Summary action config removed", footerConfigBlock, 'summary:');
checkContains("Export action label", footerConfigBlock, 'nextLabel: "Start New Submission"');
checkNotContains("Step 6 no longer shows generic Finish label", footerConfigBlock, 'nextLabel: "Finish"');
checkContains("Blocked-step reason helper", appJs, "function _stepBlockedReason");
checkContains("Validate Continue gates on error count (warnings don't block)", appJs, 'issueCount(r, "errors") === 0');
checkContains("Action row disabled when blocked", appJs, "primaryBtn.disabled = !canProceed");
checkContains("Blocked reason stays on disabled action instead of visible footer copy", appJs, "guidance.hidden = !!blockedReason");
checkContains("Restored sessions sync in-card actions", appJs, "_syncStepActionRow(step)");
checkContains("Progress sync helper", appJs, "function _syncCompactProgress");
checkContains("Step shell card styling", css, ".step-shell {");
checkContains("Step shell header styling", css, ".step-shell-header");

console.log("\n[ Clinical workbench design system ]");
checkContains("Clinical workbench CSS replaces version stack", css, "Clinical workbench design system for Steps 2-6");
checkNotContains("Old v64-v73 override headers removed", css, "v73");
checkNotContains("Old dashboard hero card CSS removed", css, "summary-hero-card");
checkNotContains("Old dashboard metric-card CSS removed", css, "summary-metric-card");
checkContains("Design system: step-section", css, ".step-section {");
checkContains("Design system: shared section headers", css, ".step-section-header,\n.collapsible-section-header,\n.sdc-head");
checkContains("Design system: worklist rows", css, ".worklist-row,");
checkContains("Design system: worklist icons", css, ".worklist-icon,");
checkContains("Design system: worklist actions", css, ".worklist-actions,");
checkContains("Design system: unified status chips", css, ".status-chip,");
checkContains("Design system: compact filter bars", css, ".filter-bar,\n.compact-filter-bar");
checkContains("Design system: clinical results table", css, "#step-score .qc-results-table");
checkContains("Design system: compact export worklist", css, "#step-export .export-file-list");
checkContains("Steps flatten inner cards into one contained card", css, ".step-shell .step-body > .pg-card,");
checkContains("Score status card uses workbench section", css, "#step-score .score-main-card");
checkContains("Run skipped panel is calm workbench row", css, ".run-skipped-notice {");
checkNotContains("Result-only Processing card can be hidden", css, "#step-run .run-settings-card {\n  display: block !important;");
checkNotContains("Run notice has no success status badge", section("step-run"), "status-badge-pass");
checkNotContains("Run step has no Execution skipped wording", html, "Execution skipped");
checkContains("Run notice uses researcher wording", html, "Result maps were included in these submissions. No code run was needed.");

console.log("\n[ Researcher-facing language ]");
// No developer/technical wording in the visible researcher UI.
checkNotContains("No 'Run code in Docker' button", html, "Run code in Docker");
checkNotContains("No Docker-ready status text", appJs, "Docker ready");
checkNotContains("No 'Execution succeeded' wording", appJs, "Execution succeeded");
checkNotContains("No 'Execution complete' progress text", appJs, "Execution complete");
checkNotContains("No 'result-only (skipped)' wording", appJs, "result-only (skipped)");
checkNotContains("Run meta not 'Docker run complete'", appJs, "Docker run complete");
checkNotContains("Score details drop provider line", appJs, "<strong>Provider:</strong>");
checkNotContains("Score details drop export readiness line", appJs, "<strong>Export readiness:</strong>");
checkNotContains("Score details drop artifacts line", appJs, "<strong>Artifacts:</strong>");
// Researcher-facing wording present.
checkContains("Run step header is researcher-facing", html, "Prepare submission maps");
checkContains("Run processing button label", html, "Run processing");
checkContains("Run meta says Maps ready for review", appJs, '"Maps ready for review"');
checkContains("Run complete label is Processing complete", appJs, '"Processing complete"');
checkContains("Run status Needs review (not Cannot run)", appJs, 'rs-badge rs-cannot">Needs review');
checkContains("Processing availability wording (not Docker)", appJs, "Processing available");
checkContains("Validate uses Items to review", appJs, "Items to review");
checkContains("Validate uses Blocking errors heading", appJs, "Blocking errors");
checkContains("Run details use Included maps wording", appJs, "Included maps:");
checkContains("Run details say Code run needed", appJs, "Code run needed:");
checkContains("Score details plain QC-only wording", appJs, "Reference maps were not available, so this is QC only.");
// Calm neutral styling — items-to-review is not a loud yellow warning box.
checkContains("Items-to-review list is neutral gray", css, ".issue-list li.review-item");
checkContains("Run notice hides success icon", css, "#step-run .run-skipped-notice .rsn-icon { display: none; }");
checkContains("Run completion banner stays neutral", css, "#run-completion-banner.step-completion-banner");
checkNotContains("Validate subtitle no longer repeats run readiness", section("step-validate"), "run readiness, and output-map");
checkContains("CSS version bumped for Configuration Manager cleanup", html, "styles.css?v=102");
checkNotContains("Step 5 capability strip removed", html, "analysis-scope-bar");
checkNotContains("Step 5 capability strip CSS removed", css, ".analysis-scope-bar");
checkContains("JS version bumped for preview-modal cleanup", html, "app.js?v=91");
checkContains("Official provider status comes from API metadata", appJs, "activeOfficial = entry.official === true");
checkContains("Official UI state uses provider metadata", appJs, "const activeIsOfficial = activeOfficial");
checkNotContains("Built-in mode is not assumed official", appJs, 'const activeIsOfficial = activeMode === "builtin"');

console.log("\n[ Filter dropdown never clipped ]");
checkContains("Filter menu opens as fixed overlay", css, ".filter-menu.filter-menu--floating");
checkContains("Floating menu uses fixed position (escapes clipping)", css, "position: fixed !important;");
checkContains("Floating menu high z-index", css, "z-index: 1400 !important;");
checkContains("Floating menu clamps width to viewport", css, "max-width: min(280px, calc(100vw - 16px)) !important;");
checkContains("Floating menu caps height with internal scroll", css, "max-height: var(--fm-max-h, 260px) !important;");
checkContains("Floating menu overflow scrolls internally", css, "overflow-y: auto !important;");
checkContains("Menu positioner helper exists", appJs, "function _positionFilterMenu");
checkContains("Positioner clamps to right edge", appJs, "left = window.innerWidth - 8 - menuW");
checkContains("Positioner clamps to left edge", appJs, "if (left < 8) left = 8;");
checkContains("Positioner sets viewport max-height", appJs, "window.innerHeight - r.bottom - 12");
checkContains("Open handler positions the menu", appJs, "_positionFilterMenu(menuBtn, menu);");
checkContains("Close resets floating state", appJs, 'menu.classList.remove("filter-menu--floating")');
checkContains("Scroll closes floating menu", appJs, 'window.addEventListener("scroll", _closeFilterMenus, true)');
checkContains("Resize closes floating menu", appJs, 'window.addEventListener("resize", _closeFilterMenus)');

console.log("\n[ Shared submission document icon (Steps 2-5) ]");
checkContains("Shared SVG document icon helper", appJs, "function submissionFileIconHtml");
checkContains("Submission rows use the IMG icon asset", appJs, "/static/assets/submission-img-icon.png");
checkNotContains("Icon is not an emoji", appJs, "📄");
const iconUses = (appJs.match(/iconHtml: submissionFileIconHtml\(\)/g) || []).length;
if (iconUses >= 4) {
  console.log(`  ✓  Shared doc icon wired into ${iconUses} row builders (Review/Validate/Run/Score)`);
  passed++;
} else {
  console.error(`  ✗  Shared doc icon should be wired into >=4 builders, found ${iconUses}`);
  failed++;
}
checkContains("Icon CSS sized for the row", css, ".worklist-icon.submission-file-icon");

console.log("\n[ Design tokens ]");
checkContains("Row padding tokens defined", css, "--wl-row-pad-y:");
checkContains("Row radius token defined", css, "--wl-row-radius:");
checkContains("Row icon token defined", css, "--wl-icon:");
checkContains("Chip height token defined", css, "--chip-h:");
checkContains("Typography scale tokens defined", css, "--fs-intro-title:");
checkContains("Shared title weight token defined", css, "--fw-title:");
checkContains("Worklist rows use padding token", css, "padding: var(--wl-row-pad-y) var(--wl-row-pad-x) !important;");
checkContains("Worklist rows use radius token", css, "border-radius: var(--wl-row-radius) !important;");
checkContains("Worklist icons use size token", css, "width: var(--wl-icon) !important;");
checkContains("Row titles use type token", css, "font-size: var(--fs-row-title) !important;");
checkContains("Section titles use type token", css, "font-size: var(--fs-section) !important;");
checkContains("Chips use height token", css, "min-height: var(--chip-h);");
// Intro titles unified (no 0.92rem / weight 850 drift left in the intro tier)
checkContains("Card intro title uses token", css, ".step-shell .pg-card-title {\n  font-size: var(--fs-intro-title) !important;\n  font-weight: var(--fw-title) !important;");
checkContains("Export intro title uses token", css, "#step-export .esp-title {\n  color: var(--text);\n  font-size: var(--fs-intro-title);\n  font-weight: var(--fw-title);");
checkContains("Score case-bar name uses token", css, "#step-score .sch-name {\n  margin: 0;\n  color: var(--text);\n  font-size: var(--fs-intro-title);\n  font-weight: var(--fw-title);");
checkNotContains("Score case-bar name no longer at 0.92rem", css, "#step-score .sch-name {\n  margin: 0;\n  color: var(--text);\n  font-size: 0.92rem;");

console.log("\n[ Map Files popover ]");
checkContains("renderSubmissionStructure helper", appJs, "function renderSubmissionStructure");
checkContains("toggleStructurePopover helper", appJs, "function toggleStructurePopover");
checkContains("closeStructurePopovers helper", appJs, "function closeStructurePopovers");
checkContains("File tree builder helper", appJs, "function _buildFileTree");
checkContains("Reusable structure control markup", appJs, "function _structureControlHtml");
checkNotContains("Review collapsed row does not expose Map Files action", renderBatchBlock, "actionsHtml: _structureControlHtml(sub.submission_id)");
checkContains("Review Details still exposes Map Files action", renderBatchBlock, "sub-detail-files");
checkContains("Validate rows include structure control", appJs, "${_structureControlHtml(r.submission_id)}");
checkContains("Structure trigger button rendered", appJs, 'class="structure-trigger"');
checkContains("Structure trigger labelled Map Files", appJs, "Map Files");
checkNotContains("No misleading Submission Structure label", appJs, "Submission Structure\n");
checkContains("Structure popover container rendered", appJs, 'class="structure-popover"');
checkContains("Structure trigger carries submission id", appJs, "data-structure-id");
checkContains("Outside click closes popovers", appJs, 'if (!e.target.closest(".structure-popover")) closeStructurePopovers();');
checkContains("Escape closes structure popovers", appJs, 'if (e.key === "Escape") closeStructurePopovers();');
checkContains("Tree renders folder/file items", appJs, "structure-tree-item");
checkContains("Tree shows text folder/file labels", appJs, 'e.isFile ? "File" : "Folder"');
checkNotContains("Tree does not use folder emoji", appJs, "\\u{1F4C1}");
checkNotContains("Tree does not use file emoji", appJs, "\\u{1F4C4}");
checkContains("Tree indentation element", appJs, "structure-tree-indent");
checkContains("Tree file name element", appJs, "structure-file-name");
checkContains("Best-effort tree from known file paths", appJs, "/api/nifti-files/");
checkContains("Empty state when no files", appJs, "No map files available for this submission.");
checkContains("Structure trigger CSS", css, ".structure-trigger {");
checkContains("Structure popover CSS", css, ".structure-popover {");
checkContains("Structure tree CSS", css, ".structure-tree {");
checkContains("Structure tree item CSS", css, ".structure-tree-item {");
checkContains("Structure file name CSS (monospace)", css, ".structure-file-name {");
checkContains("Structure control passed into shared details slot", appJs, 'detailsClass: "sub-row-detail"');

console.log("\n[ Contained one-screen layout (Steps 2-6) ]");
// Step 1 Upload must be untouched: no .step-body wrapper, original card intact
checkNotContains("Upload step has no step-body wrapper", section("step-upload"), 'class="step-body"');
checkContains("Upload card structure intact", section("step-upload"), 'class="form-card upload-form-card"');
checkContains("Upload keeps its own in-card submit row", section("step-upload"), 'class="form-actions"');
checkContains("Upload drop zone untouched", section("step-upload"), 'id="drop-zone"');
// Steps 2-6 each wrap content in an internal scroll region
["step-index", "step-validate", "step-run", "step-score", "step-export"].forEach((id) => {
  checkContains(`${id} uses internal scroll region`, section(id), 'class="step-body"');
});
checkContains("App locked to viewport on Steps 2-6", css, 'body:not([data-step="upload"]) .app');
checkContains("Content area does not scroll on Steps 2-6", css, 'body:not([data-step="upload"]) .content');
checkContains("Step card is a flex column capped to viewport", css, 'body:not([data-step="upload"]) .step-shell:not([hidden])');
checkContains("Internal scroll region styling", css, ".step-shell .step-body {");
checkContains("Internal region scrolls vertically", css, "overflow-y: auto");
checkContains("Action row pinned below scroll region", css, ".step-shell > .step-action-row {");
checkContains("Action rows attach to the step card bottom", appJs, "return el(`step-${step}`);");
checkContains("Step change resets internal scroll region", appJs, "#step-${step} .step-body");
checkContains("Inner section selectors follow step-body", css, ".step-shell .step-body > .pg-card");
checkContains("Upload card width remains 720px", css, ".upload-form-card {\n  max-width: 720px;");
checkContains("Steps 2-6 use the Upload card width", css, "max-width: var(--card-w, 720px) !important");
checkContains("Steps 2-6 use Upload-sized contained height", css, "--wizard-card-h: 760px;");
checkContains("Visible step shell has fixed contained height", css, "height: min(var(--wizard-card-h), calc(100vh - (var(--wizard-card-top-gap) * 2))) !important;");
checkContains("Steps 2-6 header matches Upload spacing", css, ".step-shell-header {\n  gap: 20px !important;\n  padding: 26px 32px 24px !important;");
checkContains("Steps 2-6 logo matches Upload size", css, ".step-shell-header .brand-logo {\n  width: 62px !important;\n  height: 62px !important;");
checkContains("Steps 2-6 footer matches Upload action rhythm", css, ".step-shell > .step-action-row {\n  padding: 20px 32px 28px !important;");

console.log("\n[ Shared worklist contract ]");
// ONE reusable renderer produces every row/section — not just shared classes.
checkContains("Shared renderWorklistRow helper exists", appJs, "function renderWorklistRow(");
checkContains("Shared renderFileRow helper exists", appJs, "function renderFileRow(");
checkContains("Shared renderSection helper exists", appJs, "function renderSection(");
checkContains("Shared row element helper exists", appJs, "function _worklistRowEl(");
checkContains("Collapsible section delegates to renderSection", appJs, "return renderSection({");
checkContains("Review builder calls shared renderer", appJs, "const card = _worklistRowEl({");
checkContains("Validate builder calls shared renderer", appJs, "const wrap = _worklistRowEl({");
checkContains("Score builder calls shared renderer", appJs, "return renderWorklistRow({");
checkContains("Preview builder calls shared file renderer", appJs, "return renderFileRow({");
checkContains("Export builder calls shared file renderer", appJs, "const renderRows = (items) => items.map((r) => renderFileRow({");
checkContains("Export groups blinded reviewer outputs", appJs, "Blinded reviewer outputs");
checkContains("Export separates organiser-only output", appJs, "Organiser-only output");
checkContains("Renderer emits canonical row skeleton", appJs, "${checkbox}${icon}${main}${actions}${chevron}");
// ── ONE shared Details / Hide details control everywhere ──
checkContains("Renderer auto-appends shared Details button", appJs, 'class="details-toggle" aria-expanded="false">Details</button>');
checkContains("Shared details toggle handler exists", appJs, 'const btn = e.target.closest(".details-toggle");');
checkContains("Toggle swaps to Hide details when open", appJs, 'isOpen ? "Hide details" : "Details"');
checkContains("Details toggle opt-out supported", appJs, "o.detailsToggle !== false");
checkNotContains("No per-step chevron-only sub-row expander", appJs, "sub-row-expand");
checkNotContains("No per-step validate Details button", appJs, "vr-details-btn worklist-chevron");
checkNotContains("No per-step run Details button", appJs, "er-detail-btn worklist-chevron");
checkNotContains("No per-step leaderboard Details button", appJs, 'data-leaderboard-detail="${safeSid}"');
checkContains("Details toggle CSS is shared", css, ".details-toggle");
checkContains("List section default is always open", appJs, "function _collapsibleDefaultOpen(key, count) {\n  _collapseState[key] = true;\n  return true;");
checkContains("Section open helper forces open lists", appJs, "// Step list sections stay open; only each row's Details area collapses.");
checkContains("Step change reopens the active list", appJs, "function _openStepListSection(step)");
checkContains("Section click cannot collapse list", appJs, "_setCollapsibleSectionOpen(key, true);");
checkContains("Rendered sections do not hide list body", appJs, `<div id="\${escapeHtml(key)}-list-body" class="collapsible-section-body">\${o.bodyHtml || ""}</div>`);
checkContains("Only row Details receive hidden default", appJs, "const hideAttr = o.detailsHidden === false ? \"\" : (o.detailsHiddenAttr || \" hidden\");");
checkNotContains("Static sections have no collapse toggle", html, "data-collapse-toggle=");
checkContains("Review list body visible by default", section("step-index"), '<div id="index-list-body" class="collapsible-section-body">');
checkContains("Validate list body visible by default", section("step-validate"), '<div id="validation-list-body" class="collapsible-section-body">');
checkContains("Run list body visible by default", section("step-run"), '<div id="run-list-body" class="collapsible-section-body">');
checkContains("Score list body visible by default", section("step-score"), '<div id="leaderboard-section-body" class="collapsible-section-body">');
checkContains("Export rows visible by default", section("step-export"), 'id="export-main-list" aria-label="Main export options"></div>');
// Collapsed rows are trimmed; secondary info lives in Details
checkContains("Validate chips moved into Details", appJs, "validation-detail-chips");
checkContains("Validate meta uses items-to-review wording", appJs, "warnings.length ? `${warnings.length} item");
checkContains("Run output count moved into Details", appJs, "run-card-outputs er-outputs-cell");
checkNotContains("Run row drops always-visible output chip in actions", appJs, 'run-card-outputs er-outputs-cell"><span class="rs-na">${escapeHtml(fileCount)} file${Number(fileCount) === 1 ? "" : "s"}</span></div>${actionsHtml}');
checkNotContains("Review Map Files is not a collapsed row action", renderBatchBlock, "actionsHtml:");
checkContains("Status helper emits unified chip", appJs, "status-chip status-pill");
checkContains("Status helper maps semantic chip tones", appJs, "function statusChipTone");
["status-chip-success", "status-chip-warning", "status-chip-danger", "status-chip-neutral", "status-chip-info"].forEach((cls) => {
  checkContains(`Unified status chip tone ${cls}`, css, `.${cls}`);
});
["worklist", "worklist-row", "worklist-icon", "worklist-main", "worklist-title", "worklist-meta", "worklist-status", "worklist-actions", "worklist-details", "worklist-checkbox"].forEach((cls) => {
  checkContains(`Shared worklist class .${cls}`, css, `.${cls}`);
});
["section-row", "section-title", "section-count", "section-actions"].forEach((cls) => {
  checkContains(`Shared section header class .${cls}`, css, `.${cls}`);
});
checkContains("Static validation section uses shared section row", section("step-validate"), "collapsible-section-header section-row");
checkContains("Static run section uses shared section row", section("step-run"), "collapsible-section-header section-row");
checkContains("Leaderboard section uses shared section row", section("step-score"), "collapsible-section-header section-row leaderboard-section-toggle");
checkContains("Review rows use shared renderer", appJs, 'extraClass: "sub-row guided-sub-card"');
checkContains("Validate rows use shared renderer", appJs, 'extraClass: "br-row-wrap validation-card"');
checkContains("Run rows use shared renderer", appJs, 'extraClass: "run-sub-card"');
checkContains("Score rows use shared renderer", appJs, 'extraClass: "leaderboard-row"');
checkContains("Export rows use shared renderer", appJs, 'extraClass: "export-main-row export-file-row"');
checkContains("Review list uses worklist container", appJs, "worklist sub-row-list");
checkContains("Validate list uses worklist container", section("step-validate"), 'id="batch-submissions-list" class="worklist"');
checkContains("Run list uses worklist container", section("step-run"), 'id="run-submissions-list" class="worklist run-cards-list"');
checkContains("Score list uses worklist container", section("step-score"), 'id="leaderboard-list" class="worklist leaderboard-list"');
checkContains("Export list uses worklist container", section("step-export"), "worklist export-main-list export-file-list");
checkNotContains("Review collapsed rows omit readiness chip", appJs, "statusHtml: statusPill(statusLabel, statusState)");
checkNotContains("Validate collapsed rows omit warning/status chip", appJs, "statusHtml: statusPill(pillText, pillState)");
checkContains("Validate warning/status chip moved into Details", appJs, '<span class="validation-meta-with-help">${statusPill(pillText, pillState)}</span>');
checkContains("Run collapsed rows use plain meta status", appJs, 'metaHtml: _erRunMetaText(initExecStatus, runnable), metaClass: "run-card-status-row"');
checkNotContains("Score collapsed rows omit status chip slot", appJs, 'statusClass: "leaderboard-row-badges"');
checkContains("Details use shared worklist details class", appJs, "worklist-details");
checkNotContains("Details controls do not use chevron-only class", appJs, "worklist-chevron");
checkContains("Review checkboxes expose worklist checkbox class", appJs, "worklist-checkbox sub-card-check sub-row-check");
checkContains("Shared neutral submission icon helper", appJs, "function submissionFileIconHtml");
checkContains("Submission icon renders an img element", appJs, '<img src="/static/assets/submission-img-icon.png"');
checkContains("Neutral submission icon styled once", css, ".worklist-icon.submission-file-icon");
checkContains("Neutral submission icon uses small rounded square", css, "flex: 0 0 28px !important;");
checkContains("Submission icon img sizing rule", css, ".submission-file-icon img {");
checkContains("Submission icon img is 28px and not stretched", css, "width: 28px;\n  height: 28px;\n  object-fit: contain;");
checkNotContains("Submission icon no longer uses CSS pseudo decoration", css, ".worklist-icon.submission-file-icon::before");
checkNotContains("Inline SVG submission icon removed from rows", appJs, "submission-file-svg");
checkNotContains("Submission icon has no text shape span", appJs, "file-icon-shape");
checkContains("Validate rows use neutral submission icon", appJs, "iconHtml: submissionFileIconHtml()");
checkContains("Run rows use neutral submission icon", appJs, "iconHtml: submissionFileIconHtml()");
checkContains("Score rows use neutral submission icon", appJs, "iconHtml: submissionFileIconHtml()");
checkNotContains("Validate rows no longer use WARN/OK text icon", appJs, 'icon: (pillState === "error" ? "ERR" : pillState === "warning" ? "WARN" : "OK")');
checkNotContains("Run rows no longer use MAP/RUN/HOLD text icon", appJs, 'icon: (isResultOnly ? "MAP" : runnable ? "RUN" : "HOLD")');
checkNotContains("Score rows no longer use QC text icon", appJs, 'icon: "QC", iconClass: "leaderboard-row-icon"');
checkContains("Row actions are secondary/outline", appJs, 'class="btn btn-secondary btn-sm er-run-btn"');
checkContains("Primary buttons reserved for page CTA", css, ".worklist-actions .btn,");

console.log("\n[ Hidden state-holder buttons ]");
check("WF state holder",     "wf-state-holder");
check("WF btn: upload",      "wf-btn-upload");
check("WF btn: index",       "wf-btn-index");
check("WF btn: validate",    "wf-btn-validate");
check("WF btn: run",         "wf-btn-run");
check("WF btn: score",       "wf-btn-score");
check("WF btn: export",      "wf-btn-export");
checkNotContains("WF summary state button removed", html, 'id="wf-btn-summary"');

console.log("\n[ Index step ]");
check("Batch validate all",  "batch-validate-all-btn");
checkContains("Review title is singular-capable", html, 'id="batch-index-title"');
checkContains("Review renders submission cards", appJs, 'guided-sub-card');
checkContains("Single-submission review card mode", appJs, 'sub-card--single');
checkNotContains("Index step has no old table markup", section("step-index"), "<table");
checkContains("Continue validates selected submissions", appJs, "runBatchValidation(selected)");
checkContains("Review action row pinned to step card", appJs, "function _stepActionHost");
checkContains("Review submission type tooltip", appJs, "Result maps provided help");
checkContains("Review has multi-submission filter bar", appJs, "function _renderIndexFilterBar");
checkContains("Review search filters submissions", appJs, "index-search");
checkContains("Review map type dropdown filter", appJs, '"index-map"');
checkContains("Review status dropdown filter", appJs, '"index-status"');
checkNotContains("Review Sort removed from visible toolbar", indexFilterBarBlock, '"index-sort"');
checkNotContains("Review toolbar omits separate clear-filters button", indexFilterBarBlock, "index-clear-filters");
checkNotContains("Review list no longer uses nested collapsible section", appJs, '_renderCollapsibleSection("index"');
checkContains("Review summary line uses card subtitle like Validate", appJs, "desc.textContent = summaryText");
checkContains("Review summary is simple detected/selected copy", renderBatchBlock, '`${safeSubmissions.length} submissions · ${selectedCount} selected`');
checkContains("Review single summary is simple detected copy", renderBatchBlock, '`${safeSubmissions.length} submission detected`');
checkNotContains("Review summary does not show misleading ready count", renderBatchBlock, "ready ·");
checkNotContains("Review summary does not show misleading warning count", renderBatchBlock, "warning${warnings");
checkNotContains("Review summary does not show misleading error count", renderBatchBlock, "error${errors");
checkContains("Review show all submissions", appJs, "index-show-all-btn");
checkContains("Detected submission rows show metadata line", appJs, "sub-row-meta");

console.log("\n[ Compact worklist rows (Step 2) ]");
checkContains("Compact row markup via renderer", appJs, 'extraClass: "sub-row guided-sub-card"');
checkContains("Row list container", appJs, "sub-row-list");
checkContains("Row shows submission name", appJs, "sub-row-name");
checkContains("Row uses neutral submission icon", renderBatchBlock, "iconHtml: submissionFileIconHtml()");
checkNotContains("Row no longer uses ASL text icon", renderBatchBlock, "icon: safeChallenge");
checkNotContains("Submission rows do not use decorative image icons", renderBatchBlock, "<img");
checkNotContains("Submission rows do not use ZIP icon text", renderBatchBlock, ">ZIP<");
checkNotContains("Submission rows do not use folder icon text", renderBatchBlock, ">Folder<");
checkContains("Row uses shared worklist title", appJs, 'titleClass: "sub-row-name"');
checkContains("Row uses shared worklist meta", appJs, 'metaClass: "sub-row-meta"');
checkContains("Row meta line builder", appJs, "function _subRowMetaLine");
checkContains("Meta line joins with middots", appJs, 'parts.join(" · ")');
checkContains("Review meta shows compact map count", appJs, "map${Number(niftiCount) === 1 ? \"\" : \"s\"}");
checkContains("Full metadata grid moved into collapsed details", appJs, 'detailsClass: "sub-row-detail"');
checkContains("Rows use shared Details toggle", appJs, 'class="details-toggle"');
checkContains("Step 2 has Step 3-style section", html, 'id="index-list-section"');
checkContains("Step 2 section has shared section header", section("step-index"), 'class="collapsible-section-header section-row"');
checkContains("Step 2 section count mirrors Validate", section("step-index"), 'id="index-section-count"');
checkContains("Step 2 uses toolbar slot like Validate", section("step-index"), 'id="index-toolbar" class="vr-toolbar"');
checkContains("Step 2 renderer updates section count", appJs, 'const countEl = el("index-section-count")');
checkContains("Step 2 renderer updates section summary", appJs, 'const summaryEl = el("index-section-summary")');
checkContains("Step 2 renderer fills toolbar", appJs, 'toolbar.innerHTML = !isSingle ? _renderIndexFilterBar(safeSubmissions) : ""');
checkOrder("Step 2 toolbar matches Step 3 filter order", indexFilterBarBlock, [
  '_renderSearchBox("index-search"',
  '_renderFilterDropdown("index-status"',
  '_renderFilterDropdown("index-map"',
  "${selectionControls}",
]);
checkOrder("Step 3 toolbar keeps same filter order", validationFilterBarBlock, [
  '_renderSearchBox("batch-search"',
  '_renderFilterDropdown("validation-status"',
  '_renderFilterDropdown("validation-map"',
]);
checkNotContains("Step 2 selection count moved out of toolbar", indexFilterBarBlock, 'class="index-selection-meta"');
checkContains("Step 2 inline Select all text control", indexFilterBarBlock, 'class="index-selection-link" id="index-select-all-btn"');
checkContains("Step 2 inline Clear text control", indexFilterBarBlock, 'class="index-selection-link" id="index-deselect-all-btn"');
checkContains("Step 2 selection controls stay inside toolbar", indexFilterBarBlock, "${selectionControls}");
checkNotContains("Step 2 selection controls are not large filter buttons", indexFilterBarBlock, "filter-clear-btn index-selection-control");
checkContains("Step 2 toolbar prevents second-row whitespace on desktop", css, "#step-index .review-filter-bar,\n.validation-filter-bar,\n.run-filter-bar,\n.leaderboard-filter-bar {\n  display: flex !important;\n  flex-wrap: nowrap !important;");
checkContains("Step 2 selection group is compact text", css, ".index-selection-group {\n  flex: 0 0 auto;");
checkContains("Step 2 selection links are compact inline buttons", css, ".index-selection-link {\n  display: inline-flex !important;");
checkContains("Step 2 selection group does not push to a new row", css, "margin-left: 0 !important;");
checkNotContains("Old oversized Step 2 selection class removed", css, "index-selection-control");
checkContains("Old oversized Select All area is hidden in markup", section("step-index"), '<div class="batch-controls" hidden>');
checkContains("Display name helper exists", appJs, "function getSubmissionDisplayName");
checkContains("Display name helper finds subject IDs", appJs, "sub[-_]?");
checkContains("Subject IDs become short display names", appJs, "return match ? `sub-${match[1]}`");
checkContains("Long fallback names are middle-truncated", appJs, "function _middleTruncate");
checkContains("Step 2 row title uses display name helper", appJs, "const safeName = escapeHtml(submissionDisplayName(sub");
checkNotContains("Step 2 collapsed row does not title with original full name", appJs, 'titleAttrs: `title="${safeOriginalName}"');
checkContains("Original full name remains in Details", appJs, "Original submission name");
checkContains("Original name has semantic class", appJs, "original-submission-name");
checkContains("Display name editor exists inside Details", appJs, "display-name-editor");
checkContains("Display name input exists", appJs, "data-display-name-input");
checkContains("Display name save button exists", appJs, "data-save-display-name");
checkContains("Display name aliases are frontend-only state", appJs, "const _displayAliases = {}");
checkContains("Display name alias setter", appJs, "function _setSubmissionDisplayAlias");
checkContains("Display name alias updates DOM targets", appJs, "function _refreshDisplayNameDom");
checkContains("Editing display name persists in session state", appJs, "displayAliases:     { ..._displayAliases }");
checkContains("Editing display name persists in wizard state", appJs, "displayAliases: { ..._displayAliases }");
checkContains("Display aliases restore from saved session", appJs, "_hydrateDisplayAliases(saved)");
checkContains("Rows across steps share display-name target", appJs, "data-display-name-for");
checkContains("Validation rows use display names", appJs, 'titleAttrs: `title="${safeName}" data-display-name-for="${safeSubId}"`');
checkContains("Run rows use display names", appJs, 'const displayName = submissionDisplayName(r, r.submission_id || "Submission")');
checkNotContains("Run rows no longer use raw source folder as collapsed title", appJs, "const safeName = r.source_folder ? escapeHtml(r.source_folder) : safeSubId");
checkContains("Score rows use display names", appJs, "const name = getSubmissionDisplayName(sub, sid);");
checkContains("Leaderboard rows use display names", appJs, "const displayName = getSubmissionDisplayName(entry, sid);");
checkContains("Map Files control remains inside Step 2 Details", appJs, "sub-detail-files");
checkContains("Map Files is rendered from Step 2 details HTML", renderBatchBlock, '${_structureControlHtml(sub.submission_id)}');
checkContains("Step 2 row uses same centered action alignment as Step 3", css, "#step-index .sub-row .worklist-actions {\n  align-items: center;");
checkContains("Expansion keeps detection warnings", appJs, "sub-row-warning");
checkContains("Checkbox selection preserved", appJs, "sub-card-check sub-row-check");
checkContains("Row click still toggles selection", appJs, "setSelected(!cb?.checked)");
checkContains("Select All still wired", appJs, "batch-select-all-btn");
checkContains("Deselect All still wired", appJs, "batch-deselect-all-btn");
// Useful map labels from configured detected filenames — never invented
checkContains("Map label resolver", appJs, "function _subMapTypesLabel");
checkContains("Resolves configured labels from real filenames", appJs, "function _mapTypesFromFilenames");
checkContains("Resolver uses existing backend file list", appJs, "/api/nifti-files/");
checkContains("Resolved labels join dynamically", appJs, 'sub._resolvedMapTypes.join(", ")');
checkContains("Mixed/Other kept in details, not row", appJs, "(detected as Mixed/Other)");
checkContains("Challenge value has its own field hook", appJs, "sub-field-challenge-type");
checkContains("Map value has its own field hook", appJs, "sub-field-map-types");
checkContains("Review suggests challenge from configured expected maps", appJs, "function _reviewChallengeSuggestion");
checkContains("Review challenge change requires an explicit click", appJs, "data-use-review-challenge");
checkContains("Review refreshes after configuration is available", appJs, 'if (wf.step === "index" && batchState.submissions.length) renderBatchTable();');
checkContains("Confirmed batch challenge is used for validation", appJs, "confirmed_challenge_type || byId[id]?.detected_challenge_type");
checkContains("Review challenge suggestion styling", css, ".review-challenge-suggestion {");
checkContains("Structure popover can open upward", css, ".structure-popover.opens-upward {");
checkContains("Structure popover positioning helper", appJs, "function _positionStructurePopover");
// CSS
checkContains("Shared worklist row CSS", css, ".worklist-row,");
checkContains("Selected row = purple left border, not full purple", css, ".sub-row.is-selected");
checkContains("Row hover state", css, ".sub-row:hover");
checkContains("Details area CSS", css, ".sub-row-detail");
checkContains("Display name editor CSS", css, ".display-name-editor");
checkContains("Original name wraps in Details", css, ".original-submission-name");
checkContains("Step 2 keeps pg-card header visible", css, "#step-index .batch-controls {\n  display: none !important;\n}");
checkNotContains("Step 2 no longer hides its card header", css, "#step-index .pg-card-header,\n#step-index .batch-controls");
checkContains("Step 2 section body matches Validate spacing", css, "#step-index #index-list-body {\n  padding: 16px 24px !important;");
checkContains("Step 2 nested table chrome removed", css, "#step-index .batch-table-wrap {\n  margin-top: 0 !important;\n  max-width: 100% !important;\n  border: 0 !important;");
checkContains("Slim header buttons", css, ".step-shell .batch-controls .btn");
checkContains("Slim review filter bar", css, ".review-filter-bar");

console.log("\n[ Run step ]");
check("Run submissions list","run-submissions-list");
check("Batch exec all btn",  "batch-exec-all-btn");
checkContains("Result-only run notice is researcher-facing", html, "Maps ready for review");
checkContains("Result-only run keeps list section visible", appJs, 'if (runListSection) runListSection.style.display = "";');
checkContains("Result-only run keeps per-submission list visible", appJs, 'if (list) list.style.display = "";');
checkNotContains("Result-only run no longer clears rows", appJs, 'if (list) list.innerHTML = "";');
checkContains("Result-only run hides duplicate continue", appJs, 'skippedContinueBtn.style.display = "none"');
checkContains("Result-only run status is neutral Maps ready", appJs, 'status-chip status-chip-neutral rs-badge rs-skipped">Maps ready');
checkContains("Run complete status uses Processing complete", appJs, 'rs-badge rs-pass">Processing complete');
checkNotContains("Run result-only badge no longer says Skipped", appJs, 'rs-badge rs-skipped">Skipped');
checkContains("Run readiness tooltip", html, "Runnable submissions include executable code. Result-only submissions skip execution and go directly to scoring.");
checkNotContains("Run button no longer says Docker", html, "Run code in Docker");
checkContains("Run button uses plain processing wording", html, "Run processing");
check("Run collapsible section", "run-list-section");
checkContains("Run has polished filter bar", appJs, "function _renderRunFilterBar");
checkContains("Run compact filter search", appJs, "run-search");
checkContains("Run status dropdown", appJs, '"run-status"');
checkContains("Run map dropdown", appJs, '"run-map"');
checkNotContains("Run Sort removed from visible toolbar", runFilterBarBlock, '"run-sort"');
checkContains("Run skipped filter chip", appJs, '"skipped"');
checkContains("Run filter keeps Continue logic separate", appJs, "_applyRunFilters();");

console.log("\n[ Score step ]");
check("Score not-configured compatibility mount", "score-not-configured-card");
check("Score status card",         "score-status-card");
check("Score table card",          "score-table-card");
check("Run analysis button",        "btn-score-all");
checkContains("Configured analysis action", html, "Run Analysis");
checkContains("Score duplicate continue hidden", css, "#btn-score-continue");
checkContains("Score not-configured mount is hidden in markup", section("step-score"), '<div id="score-not-configured-card" hidden style="display:none"></div>');
checkNotContains("Score not configured text removed from visible UI", section("step-score"), "Scoring not configured");
checkNotContains("QC/export available copy removed from visible UI", section("step-score"), "QC/export remains available");
checkNotContains("No separate not-configured continue button", section("step-score"), "btn-score-continue-nc");
checkContains("Score not-configured branch keeps mount hidden", scoreNotConfiguredBlock, 'notConfiguredCard.style.display = "none";');
checkContains("Score not-configured branch still renders preview/QC state", scoreNotConfiguredBlock, "renderScorePreviewPanel();");
checkContains("Score not-configured card has CSS safety hide", css, "#step-score #score-not-configured-card {\n  display: none !important;");
checkContains("Score table hidden until useful", appJs, 'tableCard.style.display = "none"');
checkContains("Score metric preview present", html, 'id="score-metric-preview"');
checkContains("QC metrics tooltip", html, "QC metrics describe map validity and statistics. They are not official OSIPI scores.");
checkContains("Reference scoring status tooltip", appJs, "Reference metrics are calculated only when a matching private ground-truth map is available.");
checkContains("Configuration Manager UI", html, "Challenge Configuration Manager");
checkContains("Configuration test action", html, "1. Test Configuration");
checkContains("Configuration preview action", html, "2. Preview Changes");
checkContains("Configuration version save action", html, "3. Save as New Version");
checkContains("Configuration summary cards", html, "config-manager-summary-grid");
checkContains("Configuration rules group", html, "Challenge rules");
checkContains("Configuration analysis group", html, "Analysis and data");
checkContains("Configuration history group", html, "Version history");
checkContains("Configuration section actions are consistent", html, 'class="config-manager-open-button"');
checkContains("Configuration review controls follow summaries", html, "config-manager-review-bar");
checkContains("Configuration cards use compact height", css, "min-height: 72px;");
checkNotContains("Oversized configuration cards removed", css, "min-height: 104px;");
checkContains("Challenge details modal", html, 'id="config-modal-challenge"');
checkContains("Maps edit modal", html, 'id="config-modal-maps"');
checkContains("Dataset edit modal", html, 'id="config-modal-datasets"');
checkContains("Artifacts edit modal", html, 'id="config-modal-artifacts"');
checkContains("Analysis edit modal", html, 'id="config-modal-scoring"');
checkContains("Private assets details modal", html, 'id="config-modal-assets"');
checkContains("Capabilities details modal", html, 'id="config-modal-capabilities"');
checkContains("Versions details modal", html, 'id="config-modal-versions"');
checkContains("Modal open behavior", appJs, "function _openConfigurationModal");
checkContains("Modal close behavior", appJs, "function _closeConfigurationModal");
checkContains("Modal close does not save", html, "Saving creates an inactive version; activation remains a separate explicit action.");
checkContains("Responsive modal width", css, ".config-manager-modal-dialog-wide { width: min(1040px, 100%); }");
checkContains("Modal body vertical scroll", css, "overflow-y: auto;");
checkContains("Configuration values wrap", css, ".config-manager dd { overflow-wrap: anywhere; }");
checkContains("Map units shown in manager", appJs, "_configurationMapUnit(item.id)");
// Aliases used to be a comma separated textarea, which wrapped but asked an
// organiser to punctuate a list correctly. They are chips now; the original
// concern was that a long list must not overflow its container, so that is
// what is checked, along with the hidden input the save path still reads.
checkContains("Long map aliases wrap rather than overflow", css, ".cfg-chips {");
checkContains("Long map aliases wrap rather than overflow (flex-wrap)", css, "flex-wrap: wrap");
checkContains("Map aliases still reach the save path", appJs, 'class="config-map-aliases"');
checkContains("Long package names get wrapping detail text", html, 'id="config-manager-package-detail"');
checkContains("Private assets local-only warning", html, "These files remain local and are not included in GitHub or configuration exports.");
checkContains("Private asset file chooser is visible", html, "Choose NIfTI file");
checkContains("Private asset filename feedback", html, 'id="config-manager-asset-file-name"');
checkContains("Long private asset filenames wrap", css, ".config-manager-selected-file {");
checkContains("Configuration Manager API", appJs, "/api/configuration-manager");
checkContains("Official ranking capability disclaimer", html, "Official OSIPI challenge ranking is not currently configured.");
checkContains("Built-in compatibility comes from provider registry", appJs, "function _builtinProviderForChallenge");
checkContains("Configuration Manager reads compatible built-ins", appJs, "state.builtin_providers");
checkContains("Incompatible built-in option is disabled", appJs, "builtinOption.disabled = !builtin");
checkNotContains("Configuration Manager does not hard-code TF6.2", section("config-manager-panel"), "TF6.2");
checkContains("Active provider summary card", html, "scoring-provider-summary-card");
checkContains("Provider controls moved into modal", html, 'id="config-modal-provider"');
checkContains("Provider modal trigger", html, 'data-config-modal-open="provider"');
checkContains("No-provider option retained", html, 'id="scoring-mode-none"');
checkContains("Built-in provider option retained", html, 'id="scoring-mode-builtin"');
checkContains("Custom provider option retained", html, 'id="scoring-mode-custom"');
checkContains("Package upload retained", html, 'id="scoring-pkg-input"');
checkContains("Package selector retained", html, 'id="scoring-pkg-select"');
checkContains("Explicit apply control retained", html, 'id="scoring-setup-save-btn"');
checkContains("Provider summary rendering", appJs, "function _renderScoringProviderSummary");
checkContains("Full package descriptions remain visible", appJs, 'escapeHtml(pkg.description)');
checkNotContains("Package descriptions are not clipped in JavaScript", appJs, "pkg.description.slice(0, 80)");
checkNotContains("Legacy default OSIPI label removed from provider code", appJs, "Default OSIPI scoring");
checkNotContains("Provider readiness does not claim scoring", appJs, "ready to score");
checkContains("Provider names wrap", css, ".scoring-pkg-name { font-weight: 600; color: var(--text); overflow-wrap: anywhere; }");
checkContains("Provider modal is responsive", css, ".scoring-provider-summary-card { flex-direction: column; }");
checkNotContains("Legacy default OSIPI label removed from provider modal", section("config-modal-provider"), "Default OSIPI scoring");
checkContains("Provider details use actual missing requirements", appJs, "const missing = Array.isArray(p.missing)");
checkContains("Official provider badge is explicit", appJs, '"Official provider"');
checkContains("Custom packages have a distinct badge", appJs, '"Custom package"');
checkContains("Custom provider card styling", css, ".score-provider-card.spc-custom::before");
checkContains("Provider details use one clean column", css, "grid-template-columns: minmax(0, 1fr);");
checkNotContains("Provider details no longer fabricate generated-output readiness", appJs, '{ label: "Generated output maps"');
checkContains("Leaderboard professional status badges", appJs, "leaderboard-status-badge");
checkContains("Leaderboard long-name truncation", css, ".leaderboard-submission-cell span");
checkContains("Leaderboard timestamp formatting", appJs, "function _formatLeaderboardTimestamp");
check("Leaderboard filter bar", "leaderboard-filter-bar");
check("Leaderboard list", "leaderboard-list");
check("Leaderboard count", "leaderboard-count");
check("Leaderboard collapsible summary", "leaderboard-section-summary");
checkNotContains("Leaderboard section cannot collapse", section("step-score"), 'data-collapse-toggle="leaderboard"');
checkContains("Leaderboard rows become visible when rendered", appJs, 'card.style.display = "";');
checkContains("Leaderboard has custom filter dropdown helper", appJs, "function _renderFilterDropdown");
checkContains("Leaderboard status dropdown", appJs, '"leaderboard-status"');
checkContains("Leaderboard map dropdown", appJs, '"leaderboard-map"');
checkNotContains("Leaderboard Sort removed from visible toolbar", leaderboardFilterBarBlock, '"leaderboard-sort"');
checkNotContains("Leaderboard date dropdown removed", appJs, '"leaderboard-date"');
checkNotContains("Leaderboard challenge dropdown removed", appJs, '"leaderboard-challenge"');
checkContains("Dropdown opens from filter pill", appJs, "data-filter-menu");
checkContains("Dropdown selected option checkmark", appJs, "filter-option-check");
checkContains("Dropdown option label renders before selected badge", filterDropdownBlock, '<span class="filter-option-label">${escapeHtml(opt.label)}</span>');
checkContains("Dropdown selected marker renders as separate badge", filterDropdownBlock, '<span class="filter-option-check" aria-hidden="true">${opt.value === value ? "Selected" : ""}</span>');
checkNotContains("Dropdown selected marker no longer precedes option label", filterDropdownBlock, '<span class="filter-option-check" aria-hidden="true">${opt.value === value ? "Selected" : ""}</span>\n        <span>${escapeHtml(opt.label)}</span>');
checkContains("Dropdown buttons separate label/value without overlap", css, ".filter-pill-label,\n.filter-pill-value,\n.filter-pill-chevron");
checkContains("Dropdown value is ellipsized instead of overlapping", css, ".filter-pill-value {\n  flex: 0 1 auto;\n  max-width: 76px;");
checkContains("Dropdown menu appears below trigger", css, ".filter-menu {\n  top: calc(100% + 6px) !important;");
checkContains("Dropdown menu has high z-index", css, "z-index: 1300 !important;");
checkContains("Dropdown items keep readable spacing", css, ".filter-option {\n  min-height: 34px !important;\n  display: flex !important;\n  align-items: center !important;\n  justify-content: space-between !important;");
checkContains("Dropdown selected badge hides when empty", css, ".filter-option-check:empty {\n  display: none !important;");
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
checkContains("Leaderboard main status uses analysis wording", appJs, 'case "scored":         return "Analysis complete";');
checkContains("Leaderboard reference status is explicit", appJs, 'case "scored": return "Reference comparison available";');
checkContains("Leaderboard reference unavailable is plain language", appJs, "Reference unavailable");
const leaderboardEntryBlock = appJs.slice(
  appJs.indexOf("function _renderLeaderboardEntry"),
  appJs.indexOf("function _wireLeaderboardFilterControls")
);
checkNotContains("Score collapsed rows define no extra action buttons", leaderboardEntryBlock, "actionsHtml:");
checkNotContains("Score collapsed rows do not show per-row Export", leaderboardEntryBlock, "Export</a>");
checkContains("Score collapsed rows use shared Details action", appJs, 'class="details-toggle" aria-expanded="false">Details</button>');
checkContains("Preview Maps moved inside Score Details", leaderboardEntryBlock, "leaderboard-detail-actions");
checkContains("Preview Maps remains wired inside Details", leaderboardEntryBlock, 'data-leaderboard-view="${safeSid}"');
checkContains("Score refresh button hidden for demo", css, "#step-score #leaderboard-refresh-btn {\n  display: none !important;");
checkContains("Step 5 scoring status uses compact grid", css, "#step-score .smc-body {\n  flex: 1 1 auto;\n  display: grid;");
checkContains("Step 5 scoring status actions stay inline", css, "#step-score .smc-actions {\n  grid-column: 2;");
checkContains("Step 5 scoring status hides bulky preview/hint", css, "#step-score .smc-hint,\n#step-score .score-metric-preview {\n  display: none !important;");
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

console.log("\n[ Score & Preview panel ]");
const scoreSection = section("step-score");
const scorePreviewBlock = appJs.slice(
  appJs.indexOf("function renderScorePreviewPanel"),
  appJs.indexOf("// Direct \"Continue to Export\"")
);
checkContains("Step 5 label is QC & Preview", scoreSection, "Step 5 of 6: QC &amp; Preview");
checkContains("Step 5 title is QC & Preview", scoreSection, '<h1 class="card-title" id="score-step-title">QC &amp; Preview</h1>');
checkContains("Step 5 generic QC wording", scoreSection, "QC and previews are available for readable maps");
checkContains("Dynamic official scoring title helper", appJs, "function _setScoreStepCopy");
checkContains("Step 5 preview panel mount exists", scoreSection, 'id="score-preview-panel"');
checkContains("Score preview renderer exists", appJs, "function renderScorePreviewPanel");
checkContains("Score preview panel hidden from default Step 5 flow", css, "#step-score #score-preview-panel {\n  display: none !important;");
checkContains("Step 5 unlocks Export", scorePreviewBlock, 'unlockStep("export")');
checkContains("Step 5 Continue routes directly to Export", appJs, "function _goToExport()");
checkContains("Step 5 Continue syncs Export", appJs, "_syncExportStep();");
checkContains("Step 5 Continue copy is Export", scoreSection, "Continue to Export");
checkNotContains("Continue to Summary copy removed", html, "Continue to Summary");
checkNotContains("No standalone Summary section", html, 'id="step-summary"');
checkNotContains("No Summary nav state holder", html, 'id="wf-btn-summary"');
checkNotContains("No renderSummaryStep helper remains", appJs, "renderSummaryStep");
checkNotContains("No navigation to removed summary step", appJs, 'goToStep("summary")');
checkNotContains("No Summary action config remains", footerConfigBlock, 'summary:');
checkContains("Score preview uses compact workbench", appJs, "score-preview-workbench");
checkContains("Score preview has submission case header", appJs, "score-case-bar submission-case-header");
checkContains("Score preview has compact QC panel", appJs, "compact-review-panel qc-results-panel");
checkContains("Score preview has QC Results section", appJs, "QC Results");
checkContains("Score preview has Map Preview section", appJs, "Map Preview");
checkContains("Score preview has Reference Comparison status", appJs, "Reference Comparison");
checkOrder("Score preview sections render in review order", appJs, [
  "finalOutputHtml",
  "qcSummaryHtml",
  "imagePreviewHtml",
  "referenceReportHtml",
  "detailsHtml",
]);
checkContains("Score preview reference unavailable note", appJs, "Reference maps were not available, so this run shows QC metrics only.");
checkContains("Score preview null metrics are not zeroed", appJs, "function _metricOrUnavailable");
checkContains("Score preview scientific metric tooltip helper", appJs, "function _summaryMetricTooltip");
checkContains("Score preview finite voxels tooltip", appJs, "Percent of voxels that are valid numbers, excluding NaN and Inf.");
checkContains("Score preview negative voxels tooltip", appJs, "Percent of voxels below zero.");
checkContains("Score preview CoV tooltip", appJs, "Standard deviation divided by mean.");
checkContains("Score preview RMSE tooltip", appJs, "Root mean squared error between the submitted map and reference map.");
checkContains("Score preview supports partial reference scoring", appJs, "partial_reference_scoring");
checkContains("Score preview caches NIfTI analysis", appJs, "niftiAnalysis");
checkContains("Score preview has NIfTI technical table", appJs, "summary-nifti-table");
checkContains("Score preview has reference technical table", appJs, "summary-reference-table");
checkContains("Technical details collapsed in Step 5", appJs, 'class="summary-details technical-details-drawer"');
checkContains("Technical details title", appJs, "Technical Details");
checkContains("Technical details include QC JSON", appJs, "QC summary JSON");
const finalBlock = appJs.slice(
  appJs.indexOf("const finalOutputHtml"),
  appJs.indexOf("const qcRows")
);
checkContains("Final Result renders exactly one status badge", finalBlock, "statusPill(overall.label, overall.state)");
checkNotContains("Final Result has no duplicate QC-only badge", finalBlock, 'statusPill("QC only"');
checkNotContains("Final Result hides raw reference status row", finalBlock, "Reference scoring status");
checkNotContains("Main Final Result never prints reference_not_available", finalBlock, "reference_not_available");
const kmBlock = appJs.slice(
  appJs.indexOf("const qcRows"),
  appJs.indexOf("const imagePreviewHtml")
);
checkContains("Key results show Finite voxels", kmBlock, '"Finite voxels"');
checkContains("Key results show NaN / Inf", kmBlock, '"NaN / Inf"');
checkContains("Key results show Negative voxels", kmBlock, '"Negative voxels"');
checkContains("Key results render configured mean map rows", kmBlock, "Object.entries(mapSummary.meansByType || {})");
checkContains("Key results label configured map means", kmBlock, "`Mean ${display}`");
checkNotContains("Key results hide coefficient of variation", kmBlock, "Coefficient of variation");
checkNotContains("Key results hide standard deviation", kmBlock, "Standard deviation");
checkNotContains("Key results hide raw voxel counts", kmBlock, "finiteVoxelCount");
checkContains("Hidden metrics moved to Technical Details", appJs, "Full QC metrics");
checkContains("Technical Details keeps CoV", appJs, '_summaryMetric("Coefficient of variation"');
checkContains("Technical Details keeps std dev", appJs, '_summaryMetric("Standard deviation"');
checkContains("Technical Details keeps voxel counts", appJs, '"Finite voxel count"');
checkContains("Technical Details keeps per-map reference status", appJs, '"Reference status per map"');
checkContains("Reference scoring is a collapsed panel", appJs, 'details class="summary-details summary-reference-report"');
checkContains("Reference card shows calm Unavailable chip", appJs, 'statusPill("Unavailable", "pending")');
checkContains("Reference card gives plain-language reason", appJs, "No matching reference maps were found for this submission.");
checkNotContains("Export readiness panel moved out of Step 5", appJs, "summary-export-checklist");
checkContains("Map preview section targets Step 5", appJs, 'id="score-image-preview-section"');
checkContains("Map preview renders compact cards", appJs, "nifti-preview-card imaging-preview-item");
checkContains("Map preview rows use shared file renderer", appJs, 'extraClass: "nifti-preview-card imaging-preview-item"');
checkContains("Map preview list uses shared worklist container", appJs, "worklist nifti-preview-list imaging-preview-strip");
checkContains("Map preview renders compact strip", appJs, "imaging-preview-strip");
checkContains("Map preview thumbnail opens modal", appJs, "data-open-preview-map");
checkContains("Map preview Preview button opens modal", appJs, "preview-open-btn");
checkContains("Map preview keeps Download NIfTI", appJs, "Download NIfTI");
checkContains("Leaderboard Preview Maps uses direct preview helper", appJs, "function _openSubmissionPreviewFromDetails");
checkContains("Leaderboard Preview Maps opens modal directly", appJs, "_openNiftiPreview(firstPreview);");
checkContains("Leaderboard Preview Maps handler calls helper", appJs, "_openSubmissionPreviewFromDetails(sid);");
checkNotContains("Leaderboard preview is not gated on the session score cache", appJs, "sid && _scoreCache[sid]");
checkContains("Preview ignores placeholder challenge labels", appJs, '"not provided", "not_provided"');
checkContains("Preview falls back to a configured challenge", appJs, "configured.has(value)");
checkNotContains("Leaderboard Preview Maps no longer scrolls hidden panel", appJs, "score-preview-panel\")?.scrollIntoView");
checkContains("Preview modal exists", appJs, "nifti-preview-modal");
checkContains("Preview modal close on Escape", appJs, 'e.key === "Escape"');
checkContains("Preview modal shows file stats", appJs, "nifti-preview-modal-meta");
checkContains("Preview modal uses Map Preview heading", appJs, "Map Preview</div>");
checkContains("Preview has clear new-tab action", appJs, "Open image in new tab");
checkContains("Preview has clear original-file download", appJs, "Download original NIfTI");
checkContains("Preview reads all available mask overlays", appJs, "item?.mask_overlays || []");
checkContains("Preview creates one tab per mask overlay", appJs, "...overlayPlanes");
checkContains("Preview labels each mask-specific overlay", appJs, "`${label} overlay`");
checkContains("Preview image information lists all overlay masks", appJs, 'map((overlay) => overlay.label).join(", ")');
checkContains("Preview labels image information", appJs, "Image information");
checkContains("Preview actions use a dedicated footer", appJs, "nifti-preview-modal-footer");
checkNotContains("Ambiguous full-preview wording removed", appJs, "Open full preview");
checkNotContains("Over-specific ITK-SNAP button wording removed", appJs, "Download NIfTI for ITK-SNAP");
checkContains("Full viewer guidance mentions external NIfTI viewers", appJs, "ITK-SNAP, FSLeyes, or 3D Slicer");
checkContains("Preview cards styled", css, ".nifti-preview-card");
checkContains("Preview modal styled", css, ".nifti-preview-modal-backdrop");

console.log("\n[ Map Preview one-at-a-time + modal gallery ]");
checkContains("Map Preview shows one selected item at a time", appJs, "map-preview-single");
checkContains("Map tabs rendered when multiple maps", appJs, "map-preview-tabs");
checkContains("Map tab switch handler", appJs, "data-preview-tab");
checkContains("Selected tab tracked in state", appJs, "_previewSelectedMapId");
checkContains("Switching tab re-renders single preview", appJs, "section.outerHTML = _renderImagePreviewSection");
checkContains("Modal gallery nav rendered when >1 map", appJs, "nifti-preview-modal-nav");
checkContains("Modal gallery counter (N of M)", appJs, "${galleryPos + 1}</strong> of ${galleryIds.length} maps");
checkContains("Modal prev/next controls", appJs, 'data-preview-nav="prev"');
checkContains("Gallery step helper wraps around", appJs, "function _stepNiftiPreview");
checkContains("Left arrow navigates previous map", appJs, 'e.key === "ArrowLeft"');
checkContains("Right arrow navigates next map", appJs, 'e.key === "ArrowRight"');
checkContains("Gallery uses available previews only", appJs, "function _previewGalleryIds");
checkContains("Map tab CSS", css, "#step-score .map-preview-tab");
checkContains("Single preview CSS", css, "#step-score .map-preview-single");
checkContains("Modal nav CSS", css, ".nifti-preview-modal-nav");
checkContains("Modal counter CSS", css, ".nifti-preview-counter");
checkNotContains("No giant stacked preview cards (single strip only)", appJs, "maps.map(_renderPreviewCard).join");
checkContains("Preview routes exposed by backend", mainPy, "/api/submissions/{submission_id}/previews");
checkContains("Preview PNG route exposed by backend", mainPy, "/api/submissions/{submission_id}/previews/{map_id}/{plane}.png");
checkContains("Preview download route exposed by backend", mainPy, "/api/submissions/{submission_id}/maps/{map_id}/download");
checkContains("Full preview route exposed by backend", mainPy, "/preview/{submission_id}/{map_id}");
const viewerNoteCount = (appJs.match(/Open full NIfTI files in ITK-SNAP/g) || []).length;
if (viewerNoteCount === 1) {
  console.log("  OK  Viewer guidance defined once (top note, not per card)");
  passed++;
} else {
  console.error(`  FAIL  Viewer guidance should be defined once, found ${viewerNoteCount}`);
  failed++;
}
checkContains("Step 5 scroll body clears action row", css, "#step-score .step-body");
checkContains("Step 5 contained bottom padding present", css, "padding-bottom: 22px !important");
checkContains("Compact key metric CSS", css, "#step-score .score-final-metrics");
checkContains("Compact preview card CSS", css, "#step-score .nifti-preview-card");
checkContains("QC results table CSS", css, "#step-score .qc-results-table");
checkContains("QC result row CSS", css, "#step-score .qc-result-row");
checkContains("Imaging strip CSS", css, "#step-score .imaging-preview-strip");
checkContains("Status row CSS", css, "#step-score .reference-export-row");
checkContains("Case bar CSS", css, "#step-score .score-case-bar");
checkContains("Imaging thumb dark frame CSS", css, "#step-score .imaging-thumb");
checkNotContains("Old summary clinical screen class removed", appJs, "summary-clinical-screen");
checkNotContains("Old summary case bar class removed", appJs, "summary-case-bar");
checkNotContains("Report has no visible demo-only banner", mainPy, "Demo / QC scoring only");

console.log("\n[ Score & Preview soft palette ]");
// Pipeline diagram + workflow chip row both fully removed
checkNotContains("No pipeline-flow diagram in JS", appJs, "pipeline-flow");
checkNotContains("No pipeline nodes in JS", appJs, "pipeline-node");
checkNotContains("No workflow chip row in JS", appJs, "workflow-status-chips");
checkNotContains("No workflow chips in JS", appJs, "wf-status-chip");
checkNotContains("No chip sync helper left", appJs, "_setPreviewStatusChip");
checkNotContains("No pipeline-flow styling in CSS", css, "pipeline-flow");
checkNotContains("No chip styling in CSS", css, "workflow-status-chips");
// Step 5 sizing matches the shared step shell (no special wide canvas)
checkNotContains("Step 5 wide max-width removed", css, "#step-score.step-shell { max-width");
checkNotContains("No 980px Step 5 canvas", css, "max-width: 980px !important");
checkContains("Shared step-shell width applies to all steps", css, "max-width: var(--card-w, 720px) !important");
// Clinical layout
checkContains("Score preview workbench rendered", appJs, "score-preview-workbench");
checkContains("Slim case bar rendered", appJs, "score-case-bar");
checkContains("QC results table rendered", appJs, "qc-results-table");
checkContains("QC result rows rendered", appJs, "qc-result-row");
checkContains("QC table has Metric/Result/Status head", appJs, 'qc-cell-status">Status');
checkContains("QC quality bar kept (real percent)", appJs, "qc-quality-bar");
checkContains("QC bar width uses real finite percent", appJs, 'style="width:${finitePct}%"');
checkNotContains("No fake time-series charts", appJs, "qc-chart");
// Compact imaging strip, not huge cards
checkContains("Imaging preview panel rendered", appJs, "imaging-preview-panel");
checkContains("Imaging previews render as compact strip", appJs, "imaging-preview-strip");
checkContains("Imaging strip items rendered", appJs, "imaging-preview-item");
checkContains("Imaging items have map labels", appJs, "imaging-item-label");
checkContains("Imaging thumbs smaller (72px)", css, "width: 72px !important");
checkContains("Imaging thumb dark frame CSS", css, "#step-score .imaging-thumb");
// Status panels + drawer
checkContains("Reference status row rendered", appJs, "reference-export-row");
checkContains("Compact review panels rendered", appJs, "compact-review-panel");
checkContains("Reference status card rendered", appJs, "reference-status-card");
checkContains("Technical details drawer rendered", appJs, "technical-details-drawer");
checkContains("Reference details collapsed by default", appJs, "<summary>View details</summary>");
// Supporting CSS scoped to Step 5
checkContains("QC results table CSS", css, "#step-score .qc-results-table");
checkContains("QC result row CSS", css, "#step-score .qc-result-row");
checkContains("Imaging strip CSS", css, "#step-score .imaging-preview-strip");
checkContains("Status row CSS", css, "#step-score .reference-export-row");
checkContains("Case bar CSS", css, "#step-score .score-case-bar");
checkContains("Step 5 soft lavender canvas", css, "background: var(--surface-panel);");

console.log("\n[ Validation stat/dashboard removal ]");
checkNotContains("Validation stat tile markup removed", appJs, "val-stat-tile");
checkNotContains("Validation checked tile removed", appJs, "is-checked");
checkNotContains("Validation passed tile removed", appJs, "is-passed");
checkNotContains("Validation title no longer adds a chip", appJs, "statusPill(chipLabel, chipState)");
checkContains("Validation header uses one compact summary line", appJs, '`${checkedCount} submission${checkedCount === 1 ? "" : "s"}`');
checkContains("Validation stats element is hidden", appJs, "statsEl.hidden = true;");
checkNotContains("Stat tile CSS removed", css, ".val-stat-tile {");
checkContains("Validation stats/dashboard hidden in CSS", css, "#step-validate .validation-summary-stats");
checkContains("Global surfaces shift to soft lavender", css, "--surface: #f7f5fc;");
checkContains("Count badge as soft purple pill", css, ".collapsible-count {");

console.log("\n[ Soft app background ]");
checkContains("Soft gradient applied to page", css, "linear-gradient(160deg, #fbf8fc");
checkNotContains("No decorative blob pseudo-elements", css, "body::before");
checkNotContains("No decorative radial blob gradients", css, "radial-gradient(circle");
checkContains("App content stacks above background", css, ".app {\n  position: relative;\n  z-index: 1;\n}");
checkContains("Content layer transparent over canvas", css, "background: transparent !important;");

console.log("\n[ Upload selected-file card ]");
checkContains("formatFileSize helper", appJs, "function formatFileSize");
checkContains("renderSelectedUploadFile helper", appJs, "function renderSelectedUploadFile");
checkContains("clearSelectedUploadFile helper", appJs, "function clearSelectedUploadFile");
checkContains("setUploadStatus helper", appJs, "function setUploadStatus");
checkContains("Card renders after selection", appJs, "renderSelectedUploadFile();");
checkContains("Card shows filename", appJs, "ufc-name");
checkContains("Card shows file size", appJs, "ufc-size");
checkContains("Card shows Ready to upload state", appJs, "Ready to upload");
checkContains("Remove button clears selection", appJs, 'removeBtn.addEventListener("click", clearSelectedUploadFile)');
checkContains("Remove disabled while uploading", appJs, "remove.disabled = busy");
checkContains("Status transitions to uploading", appJs, 'setUploadStatus("uploading", null)');
checkContains("Status transitions to completed", appJs, 'setUploadStatus("completed", 100)');
checkContains("Status transitions to failed with message", appJs, 'setUploadStatus("failed", null, err.message');
checkContains("Detecting state after transfer completes", appJs, 'setUploadStatus("detecting")');
// Real progress only — XHR upload progress, indeterminate when size unknown
checkContains("Upload uses XMLHttpRequest for real progress", appJs, "new XMLHttpRequest()");
checkContains("Progress uses real loaded/total", appJs, "e.lengthComputable && e.total > 0");
checkContains("Unknown size falls back to indeterminate", appJs, "onProgress(null)");
checkContains("Indeterminate bar styled, not fake percent", css, ".ufc-progress--indeterminate");
// Kind tags
checkContains("ZIP tag for zip files", appJs, 'return "ZIP"');
checkContains("NIfTI tag for .nii files", appJs, 'return "NIfTI"');
checkContains("Folder tag for folder selections", appJs, '"Folder" : "Files"');
// CSS
checkContains("Upload card CSS", css, ".upload-file-card");
checkContains("Upload card icon CSS", css, ".ufc-icon");
checkContains("Purple gradient progress fill", css, ".ufc-progress-fill");
checkContains("Completed state green", css, '.upload-file-card[data-status="completed"] .ufc-status');
checkContains("Failed state red", css, '.upload-file-card[data-status="failed"]');
checkContains("Remove icon compact", css, ".ufc-remove");
// Upload and Detect flow unchanged
checkContains("Submit still gated on selection", appJs, "return !!state.pendingLocalFiles");
checkContains("Upload endpoints unchanged (zip)", appJs, "/api/upload-batch");
checkContains("Upload endpoints unchanged (folder)", appJs, "/api/upload-folder-batch");

console.log("\n[ Wizard reload persistence ]");
checkContains("saveWizardState helper", appJs, "function saveWizardState");
checkContains("loadWizardState helper", appJs, "function loadWizardState");
checkContains("clearWizardState helper", appJs, "function clearWizardState");
checkContains("canRestoreStep helper", appJs, "function canRestoreStep");
checkContains("restoreWizardState helper", appJs, "function restoreWizardState");
checkContains("Wizard state uses sessionStorage", appJs, "sessionStorage.setItem(WIZARD_KEY");
checkContains("Wizard storage key", appJs, "osipi_wizard_state_v1");
checkContains("Session keeps detected challenge per submission", appJs, "detected_challenge_type:     s.detected_challenge_type || null");
checkContains("Session keeps confirmed challenge per submission", appJs, "confirmed_challenge_type:    s.confirmed_challenge_type || null");
checkContains("Single upload keeps reviewer-selected challenge", appJs, "confirmed_challenge_type: getChallengeType()");
// Step is saved on every navigation and lifecycle save point
checkContains("Every session save refreshes wizard state", appJs, "saveWizardState();");
checkContains("Step change syncs URL hash", appJs, "const hash = STEP_TO_HASH[step];");
// Reload restore + safe fallback
checkContains("Auto-restore runs at startup", appJs, "await restoreWizardState()");
checkContains("Fallback walks back to a valid step", appJs, "function _fallbackRestoreStep");
checkContains("Fallback bottoms out at Upload", appJs, 'return "upload";');
checkContains("Review restore requires submissions", appJs, 'case "index":    return hasSubmissions;');
checkContains("Later steps require validation state", appJs, 'case "export":   return hasSubmissions && hasValidation;');
checkContains("Restore failure shows calm message", appJs, "Could not restore the previous session. Starting from Upload.");
// Hash navigation
checkContains("Hash navigation listener", appJs, 'addEventListener("hashchange"');
checkContains("Hash #review maps to index step", appJs, 'review: "index"');
checkContains("Hash #summary maps safely to Score & Preview", appJs, 'summary: "score"');
checkContains("Locked steps cannot be reached via hash", appJs, "btn && !btn.disabled");
// Clearing state
const clearBlock = appJs.slice(
  appJs.indexOf("function clearSessionState"),
  appJs.indexOf("const WIZARD_KEY")
);
checkContains("Start New / reset clears wizard state too", clearBlock, "clearWizardState();");
// Removed Summary restore lands on Score & Preview and re-fetches live data.
const restoreBlock = appJs.slice(
  appJs.indexOf("async function restoreWizardState"),
  appJs.indexOf("function updateMapTypePills")
);
checkContains("Saved summary state is normalized", appJs, 'const restoredStep = _normalizeWorkflowStep(saved.step, "score");');
checkContains("Summary fallback lands on Score & Preview", appJs, 'if (step === "summary") return fallback === "export" ? "export" : "score";');
checkNotContains("Restore never renders removed summary", restoreBlock, "renderSummaryStep()");
checkContains("Score restore re-fetches score state", restoreBlock, "renderScoreStep()");
// Storage safety: lightweight state only
const wizardSaveBlock = appJs.slice(
  appJs.indexOf("function saveWizardState"),
  appJs.indexOf("function loadWizardState")
);
checkNotContains("Wizard state stores no files", wizardSaveBlock, "pendingLocalFiles");
checkNotContains("Wizard state stores no validation payloads", wizardSaveBlock, "validationData");
checkNotContains("Wizard state stores no score results", wizardSaveBlock, "_scoreCache");

console.log("\n[ Export step ]");
const exportSection = section("step-export");
const mainExportStart = appJs.indexOf("function _renderExportRows");
const mainExportEnd = appJs.indexOf("_renderExportRows();", mainExportStart);
const mainExportOptions = mainExportStart >= 0 && mainExportEnd > mainExportStart
  ? appJs.slice(mainExportStart, mainExportEnd)
  : "";
const exportResetBlock = appJs.slice(
  appJs.indexOf("function _startNewSubmissionFromExport"),
  appJs.indexOf("function _advanceWizardStep")
);
const advanceWizardBlock = appJs.slice(
  appJs.indexOf("function _advanceWizardStep"),
  appJs.indexOf("function _syncStepActionRow")
);
checkNotContains("Redundant blinded export row removed", mainExportOptions, 'id="export-combined-blinded-btn"');
checkContains("Combined export unblinded btn", appJs, 'id="export-combined-unblinded-btn"');
checkContains("HTML report button", appJs, 'id="export-report-btn"');
checkContains("PDF report button", appJs, 'id="export-pdf-report-btn"');
checkContains("JSON report button", appJs, 'id="export-combined-json-btn"');
checkContains("Export summary panel exists", html, "export-summary-panel");
checkContains("Export summary title", exportSection, "Final review summary");
checkContains("Export disclaimer moved into a tooltip", exportSection, "Generic QC metrics are not official OSIPI scoring");
checkContains("Export main list exists", exportSection, "export-main-list");
checkContains("Export step label is Step 6 of 6", exportSection, "Step 6 of 6: Export");
checkEqual("Step 6 main UI shows six main export options", countOccurrences(mainExportOptions, '{ id: "export-'), 6);
checkContains("Main HTML Report option", mainExportOptions, "HTML Report");
checkContains("Main PDF Report option", mainExportOptions, "PDF Report");
checkContains("Main CSV Results option", mainExportOptions, "CSV Results");
checkContains("Main JSON Results option", mainExportOptions, "JSON Results");
checkNotContains("Redundant Blinded CSV option removed", mainExportOptions, "Blinded CSV");
checkContains("Main Unblinded CSV option", mainExportOptions, "Unblinded CSV");
checkContains("Main report description", mainExportOptions, "Self-contained report");
checkContains("Main PDF report description", mainExportOptions, "Concise shareable report");
checkContains("Main unblinded CSV description", mainExportOptions, "CSV with team, contact");
checkNotContains("Main copy avoids reviewer-safe wording", mainExportOptions, "reviewer-safe");
checkNotContains("Main copy avoids external evaluation wording", mainExportOptions, "external evaluation");
checkNotContains("Main copy avoids internal organizer wording", mainExportOptions, "internal organizer");
checkContains("Main copy explains identifier visibility", mainExportOptions, "original submission identifiers");
checkContains("Main report button label", mainExportOptions, ">Open Report</button>");
checkContains("Main PDF report button label", mainExportOptions, ">Download PDF</button>");
checkContains("Main JSON button label", mainExportOptions, ">Download JSON</button>");
checkEqual("Main CSV buttons share Download CSV label", countOccurrences(mainExportOptions, ">Download CSV</button>"), 3);
checkContains("Main report reuses existing report ID", mainExportOptions, 'id="export-report-btn"');
checkContains("Main PDF report uses PDF export ID", mainExportOptions, 'id="export-pdf-report-btn"');
checkNotContains("Main redundant blinded CSV ID removed", mainExportOptions, 'id="export-combined-blinded-btn"');
checkContains("Main unblinded CSV reuses combined export ID", mainExportOptions, 'id="export-combined-unblinded-btn"');
checkContains("Export file list exists", html, "export-file-list");
checkContains("Export file rows exist", appJs, "export-main-row export-file-row");
checkContains("Export row icon class exists", appJs, "export-file-icon");
checkContains("Export row main class exists", css, ".export-file-main");
checkContains("Export row actions class exists", appJs, "export-file-actions");
checkContains("Export compact button class exists", appJs, "export-compact-btn");
checkContains("Main rows use shared file renderer", mainExportOptions, 'extraClass: "export-main-row export-file-row"');
checkNotContains("Advanced raw exports removed from UI", exportSection, "Advanced raw exports");
checkNotContains("Advanced raw export details removed", exportSection, 'id="advanced-raw-exports"');
checkNotContains("Validation CSV raw UI removed", exportSection, "Validation CSV");
checkNotContains("Execution CSV raw UI removed", exportSection, "Execution CSV");
checkNotContains("Scoring CSV raw UI removed", exportSection, "Scoring CSV");
checkNotContains("Combined raw CSV UI removed", exportSection, "Combined CSV");
checkNotContains("Batch validation raw button removed from UI", exportSection, "batch-export-blinded-btn");
checkNotContains("Batch execution raw button removed from UI", exportSection, "batch-export-exec-blinded-btn");
checkNotContains("Single validation raw button removed from UI", exportSection, "export-val-blinded-btn");
checkNotContains("Single execution raw button removed from UI", exportSection, "exec-export-blinded-btn");
checkNotContains("Scoring raw button removed from UI", exportSection, "export-scoring-blinded-btn");
checkContains("Export keeps full standard CSV aria label", appJs, "Download CSV results");
checkContains("Export keeps full report aria label", appJs, "Open HTML report");
checkContains("Export keeps full PDF aria label", appJs, "Download PDF report");
checkContains("Step 1 Upload remains intact", section("step-upload"), 'id="drop-zone"');
checkContains("Step 6 CSS scoped under step-export", css, "#step-export .export-file-list");
checkContains("Step 6 main list is styled", css, "#step-export .export-main-list");
checkContains("Step 6 file list is vertical flex", css, "flex-direction: column !important");
checkNotContains("Step 6 has no scoped two-column export grid", css, "#step-export .export-groups {\n  display: grid");
checkNotContains("Old export-groups CSS removed", css, "export-groups");
checkNotContains("Old export pair CSS removed", css, "export-pair");
checkNotContains("Old advanced export CSS removed", css, "export-advanced");
checkNotContains("Old raw export list CSS removed", css, "export-raw-list");
checkContains("Step 6 prevents horizontal overflow", css, "#step-export .step-body {\n  padding: 24px 32px 36px !important;\n  overflow-x: hidden !important");
checkContains("Step 6 rows prevent overflow", css, "#step-export .export-file-row");
checkContains("Step 6 buttons do not clip", css, "text-overflow: clip !important");
checkContains("Step 6 buttons wrap when needed", css, "white-space: normal;");
checkContains("Step 6 Back action returns to Score & Preview", appJs, 'export:   { back: "score"');
checkContains("Step 6 primary label is Start New Submission", footerConfigBlock, 'nextLabel: "Start New Submission"');
checkNotContains("Step 6 primary label is not Finish", footerConfigBlock, 'nextLabel: "Finish"');
checkContains("Step 6 export click uses reset helper", advanceWizardBlock, "_startNewSubmissionFromExport();");
checkContains("Step 6 reset helper exists", exportResetBlock, "function _startNewSubmissionFromExport()");
checkContains("Step 6 reset clears wizard/session state", exportResetBlock, "clearSessionState();");
checkContains("Step 6 reset clears reload persistence", exportResetBlock, "clearWizardState();");
checkContains("Step 6 reset clears selected/frontend state through resetAll", exportResetBlock, "resetAll();");
checkContains("Step 6 reset restores upload submit label", exportResetBlock, "syncSubmitLabel();");
checkContains("Step 6 reset returns to Step 1 Upload", exportResetBlock, 'goToStep("upload");');
checkContains("Upload hash mapping remains #upload", appJs, 'upload: "upload"');
checkOrder("Step 6 reset clears persistence after returning to Upload", exportResetBlock, [
  "resetAll();",
  "syncSubmitLabel();",
  'goToStep("upload");',
  "clearSessionState();",
  "clearWizardState();",
]);
checkContains("Validation export endpoint preserved", appJs, "/api/export-batch?");
checkContains("Single validation export endpoint preserved", appJs, "/api/export-validation?");
checkContains("Execution export endpoint preserved", appJs, "/api/export-batch-execution?");
checkContains("Single execution export endpoint preserved", appJs, "/api/export-execution?");
checkContains("Scoring export endpoint preserved", appJs, "/api/export-scoring?");
checkContains("Combined export endpoint preserved", appJs, "/api/export-combined?");
checkContains("HTML report endpoint preserved", appJs, "/api/report?");
checkContains("PDF report endpoint wired", appJs, "/api/export/report/pdf?");
checkContains("Backend validation export endpoint preserved", mainPy, '@app.get("/api/export-validation")');
checkContains("Backend batch validation export endpoint preserved", mainPy, '@app.get("/api/export-batch")');
checkContains("Backend execution export endpoint preserved", mainPy, '@app.get("/api/export-execution")');
checkContains("Backend batch execution export endpoint preserved", mainPy, '@app.get("/api/export-batch-execution")');
checkContains("Backend scoring export endpoint preserved", mainPy, '@app.get("/api/export-scoring")');

console.log("\n[ Validate step cards ]");
checkContains("Validate title is status-driven", html, 'id="validate-card-title"');
checkContains("Validate totals strip exists", html, 'id="validate-summary-stats"');
check("Validate collapsible section", "validation-list-section");
checkContains("Validate renders cards", appJs, 'validation-card');
checkContains("Validate details collapsed by default", appJs, 'detailsClass: "vr-row-detail"');
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
checkNotContains("Validate Sort removed from visible toolbar", validationFilterBarBlock, '"validation-sort"');
checkContains("Validation filters preserve error visibility", appJs, "case \"errors\"");

console.log("\n[ Tooltips for key terms ]");
function countMatches(re) { return (html.match(re) || []).length; }
const tooltipCount = countMatches(/class=["'][^"']*help-tooltip[^"']*["']/g);
if (tooltipCount >= 3) {
  console.log(`  OK  Help tooltips present (${tooltipCount} found)`);
  passed++;
} else {
  console.error(`  FAIL  Expected >=3 help tooltips, found ${tooltipCount}`);
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
// The application no longer uses a topbar.
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
  console.log("  OK  top-step-nav absent (correct -- removed by design)");
  passed++;
} else {
  console.error("  FAIL  top-step-nav found in HTML -- should have been removed");
  failed++;
}
checkNotContains("Old sidebar container absent", html, 'id="sidebar"');

console.log("\n[ Upload-sized wizard parity ]");
checkContains("Shared card width remains Upload width", css, "--card-w: 720px;");
checkContains("Shared contained step height defined", css, "--wizard-card-h: 760px;");
checkContains("Non-upload content top placement matches Upload", css, "padding: var(--wizard-card-top-gap) 20px !important;");
checkContains("Non-upload cards use Upload-width shell", css, "body:not([data-step=\"upload\"]) .step-shell:not([hidden]) {\n  width: 100% !important;\n  max-width: var(--card-w, 720px) !important;");
checkContains("Non-upload cards use one-screen body height", css, "max-height: calc(100vh - (var(--wizard-card-top-gap) * 2)) !important;");
checkContains("Step shell header uses Upload spacing", css, ".step-shell-header {\n  gap: 20px !important;\n  padding: 26px 32px 24px !important;");
checkContains("Step shell logo uses Upload dimensions", css, ".step-shell-header .brand-logo {\n  width: 62px !important;\n  height: 62px !important;");
checkContains("Step footer uses Upload action spacing", css, ".step-shell > .step-action-row {\n  padding: 20px 32px 28px !important;");
checkContains("Section body keeps bottom clearance for pinned action row", css, ".collapsible-section-body,\n.collapsible-section-body[hidden] {\n  display: block !important;\n  padding: 16px 32px 32px !important;");
checkContains("Review list body keeps bottom clearance", css, "#step-index #index-list-body,\n#leaderboard-section-body {\n  padding: 16px 32px 32px !important;");
checkContains("Export body keeps bottom clearance", css, "#step-export .step-body {\n  padding: 24px 32px 36px !important;");
checkContains("Step body has scroll padding for expanded details", css, "scroll-padding-bottom: 32px;");
checkContains("Validation stat/dashboard area hidden instead of taking vertical space", css, "#step-validate .validation-summary-stats");

console.log("\n[ Unification regression guards ]");
// Every non-upload step uses the shared step-shell (same width/height/accent)
["step-index", "step-validate", "step-run", "step-score", "step-export"].forEach((id) => {
  const sec = section(id);
  if (/<section id="[^"]*"[^>]*class="[^"]*step-shell/.test(sec) || sec.includes('class="step-panel step-shell')) {
    console.log(`  OK  ${id} uses shared step-shell`);
    passed++;
  } else {
    console.error(`  FAIL  ${id} does NOT use shared step-shell`);
    failed++;
  }
});
// Only Upload keeps its bespoke panel; no step reintroduces a wide/custom canvas
checkNotContains("No step-specific 980px canvas remains", css, "max-width: 980px !important");
checkContains("Shared card width variable drives every shell", css, "max-width: var(--card-w, 720px) !important");

// Export exposes ONLY the four consolidated worklist rows
const exportSec = mainExportOptions;
["HTML Report", "PDF Report", "CSV Results", "JSON Results", "ROI Parameter-map Statistics CSV", "Unblinded CSV"].forEach((label) => {
  checkContains(`Export shows ${label} row`, exportSec, `title: "${label}"`);
});
checkNotContains("Export hides raw Validation CSV", exportSec, "Validation CSV");
checkNotContains("Export hides raw Execution CSV", exportSec, "Execution CSV");
checkNotContains("Export hides raw Scoring CSV", exportSec, "Scoring CSV");
checkNotContains("Export hides raw Combined CSV row", exportSec, "Combined CSV");
checkNotContains("Export drops old two-column export-pair grid", exportSec, "export-pair");
checkNotContains("Export drops old export-groups grid", exportSec, "export-groups");
checkNotContains("Export drops advanced/raw export UI", exportSec, "export-advanced");
checkContains("Export keeps HTML report handler id", exportSec, 'id="export-report-btn"');
checkContains("Export keeps PDF report handler id", exportSec, 'id="export-pdf-report-btn"');
checkNotContains("Export removes redundant blinded CSV handler id", exportSec, 'id="export-combined-blinded-btn"');
checkContains("Export keeps unblinded CSV handler id", exportSec, 'id="export-combined-unblinded-btn"');

// One shared status-chip system — no bespoke pill markup bypasses it
checkContains("statusPill emits shared status-chip", appJs, 'class="status-chip status-pill');
checkContains("statusPill maps to a chip tone", appJs, "status-chip-${statusChipTone(state)}");
checkContains("Chip tone resolver exists", appJs, "function statusChipTone");
["success", "warning", "danger", "neutral", "info"].forEach((tone) => {
  checkContains(`Status chip variant .status-chip-${tone}`, css, `.status-chip-${tone}`);
});
checkNotContains("Score collapsed row does not show QC complete chip", leaderboardEntryBlock, 'statusPill("QC complete", "complete")');
checkNotContains("Score collapsed row does not show reference scored chip", leaderboardEntryBlock, 'statusPill("Reference scored", "complete")');
checkNotContains("Score collapsed row does not show reference unavailable chip", leaderboardEntryBlock, 'statusPill("Reference unavailable", "pending")');
checkContains("Score details use plain reference wording", leaderboardEntryBlock, "Reference maps were not available, so this is QC only.");

console.log("\n[ Visible layout consistency ]");
checkContains("Old Score table hidden in flow", css, "#step-score #score-table-card {\n  display: none !important;");
checkContains("Configuration Manager admin panel visible", css, "#step-score #scoring-admin-panel {\n  display: block !important;");
checkContains("Configuration Manager defaults to the current workflow challenge", appJs, "challengeType || _getSessionChallengeType() || select.value");
checkContains("Configuration Manager uses restored challenge after reload", appJs, "getChallengeType() || defaultChallengeType()");
checkNotContains("Score row drops multi-badge cluster", appJs, "list-chip list-chip-strong");
checkNotContains("Score row drops always-visible metric row extraMain", appJs, "extraMain: metricRow");
checkContains("Score row meta is one compact line", appJs, 'refScored ? "Reference comparison available" : "QC only"');
checkContains("Score metrics moved into details", appJs, "leaderboard-metric-row");
checkContains("Score head is a row, not a column", css, ".leaderboard-row-title {\n  display: flex;\n  flex-direction: row;");
checkNotContains("Score collapsed status slot removed from renderer", leaderboardEntryBlock, "leaderboard-row-badges");
checkContains("Validation stats/dashboard hidden", css, "#step-validate .validation-summary-stats,\n#step-validate .validation-summary-stats.val-stat-tiles {\n  display: none !important;");
checkNotContains("Validation stat pills do not remain", css, ".val-stat-tile {");

// Map Preview is a compact worklist-style selected row (not a stacked card grid)
checkContains("Map Preview row uses shared file renderer", appJs, "return renderFileRow({");
checkContains("Map Preview shows one selected map at a time", appJs, "map-preview-single");

// No stale removed systems anywhere in shipped HTML/JS
["pipeline-flow", "pipeline-node", "summary-dashboard", "workflow-status-chips", "export-grid"].forEach((cls) => {
  checkNotContains(`Stale system .${cls} absent from HTML`, html, cls);
  checkNotContains(`Stale system .${cls} absent from JS`, appJs, cls);
});

// Start New / reset returns to Upload from every entry point without persisting a blank session.
checkContains("Start New reset helper returns to Upload", appJs, "function _resetToUploadAndClearPersistence()");
checkContains("Start New reset suppresses blank session save", appJs, "_suppressSessionSave = true;");
checkContains("Reset clears wizard + session state", appJs, "clearWizardState();");


// ROI parameter-map statistics (within-scan descriptive values)
// Export row
checkContains("ROI export row present", appJs, "ROI Parameter-map Statistics CSV");
checkContains("ROI export uses the dedicated endpoint", appJs, "/api/export-roi-descriptive");
checkContains("ROI export reuses the session query helper", appJs, "export-roi-descriptive?${q}");
checkContains("ROI export handles a missing filename header", appJs, '"roi_descriptive_statistics.csv"');
checkContains("ROI export reports errors through the existing status element", appJs, 'el("export-combined-status")');
checkNotContains("ROI export is not mislabelled as accuracy", appJs, "ROI Accuracy CSV");
checkNotContains("ROI export is not mislabelled as repeatability", appJs, "ROI Repeatability CSV");

// Results Summary section
// check() asserts; hasId() only returns a boolean and would silently pass.
check("ROI card present", "roi-descriptive-card");
check("ROI table present", "roi-descriptive-table");
check("ROI table body present", "roi-descriptive-body");
check("ROI empty-state element present", "roi-descriptive-empty");
checkContains("ROI section titled correctly", html, "ROI Parameter-map Statistics");
checkContains("ROI table has a Map column", html, "<th>Map</th>");
checkContains("ROI table has a Mean column", html, '<th class="num">Mean</th>');
checkContains("ROI table has a Median column", html, '<th class="num">Median</th>');
checkContains("ROI table has a Range column", html, '<th class="num">Range</th>');
checkContains("ROI table has a CoV column", html, '<th class="num">CoV</th>');
checkContains("ROI renderer exists", appJs, "function renderRoiDescriptiveStatistics(");

// Display rules
checkContains("CoV displayed as a percentage", appJs, "(n * 100).toFixed(2)}%");
checkContains("Numeric unavailable guard covers NaN and null", appJs,
  'if (value === null || value === undefined || Number.isNaN(value)) return "Unavailable";');
checkNotContains("Unavailable numeric never becomes a zero string", appJs,
  'if (value === null || value === undefined) return "0";');
checkContains("Implicit clinical site renders as a dash", appJs, 'value === "" ? "—"');
checkContains("Empty-state message for missing masks", appJs, "no ROI masks were configured");
checkContains("Empty-state message for no eligible maps", appJs, "No valid configured parameter maps were available");
checkContains("Empty-state message for calculation failure", appJs, "could not be calculated");
checkContains("Neutral fallback when status is unknown", appJs, "No ROI parameter-map statistics are available.");
checkContains("Empty-state text is actually looked up by status", appJs,
  "ROI_UNAVAILABLE_MESSAGES[status]");
checkContains("Methodology text present", appJs, "SD uses the population definition");
checkContains("States CoV is stored as a ratio", appJs, "stored as a ratio in exports");

// Safety: every dynamic field escaped. The identity cells are emitted through
// one shared reader now that they can be dropped per submission, so that path
// is checked once rather than per field.
["r.roi_label || r.roi_id", "_roiNumber(r.roi_median)",
 "_roiPercent(r.roi_within_scan_cov)"].forEach((expr) => {
  checkContains(`ROI field escaped: ${expr}`, appJs, `escapeHtml(${expr}`);
});
checkContains("ROI identity cells escaped through the shared reader", appJs,
  "escapeHtml(read(r))");
checkContains("ROI scope values escaped", appJs, "escapeHtml(value)");

// Performance: one table, not a card per scan
checkContains("ROI rows built as one joined table body", appJs, "}).join(\"\");");
checkNotContains("ROI does not render a card per scan", appJs, "roi-descriptive-card-per-scan");

// Must not imply grouped statistics. Bound the slice to the ROI card;
// an unbounded slice picked up unrelated later sections.
const roiCardStart = html.indexOf('id="roi-descriptive-card"');
const roiCardHtml = html.slice(roiCardStart, html.indexOf("leaderboard-card", roiCardStart));
["Repeatability", "Reproducibility", "Inter-participant", "Inter-site"].forEach((term) => {
  checkNotContains(`ROI section avoids '${term}'`, roiCardHtml, term);
});


// ROI renderer integration with the Results Summary lifecycle
// The call must live inside the central renderer, not merely exist somewhere.
// Bounded by the NEXT top-level function after it. An end-marker search is
// unsafe: the obvious sentinels appear earlier in the file, so the slice
// silently ran to end-of-file and matched unrelated code.
const previewPanelStart = appJs.indexOf("function renderScorePreviewPanel()");
const previewPanelEnd = appJs.indexOf("\nfunction ", previewPanelStart + 1);
const previewPanelSrc = appJs.slice(
  previewPanelStart, previewPanelEnd > 0 ? previewPanelEnd : appJs.length);
checkContains("Central Results Summary renderer calls the ROI renderer",
  previewPanelSrc, "renderRoiDescriptiveStatistics(");
// Spread, so BOTH rows and status reach the renderer. Passing only
// `_roiDescriptivePayload()[0]` would drop the status that explains an
// empty table, and the static check would otherwise still pass.
checkContains("ROI call passes rows AND status",
  previewPanelSrc, "renderRoiDescriptiveStatistics(..._roiDescriptivePayload())");

// The payload helper must read the real canonical path, not a guess.
checkContains("Payload helper exists", appJs, "function _roiDescriptivePayload()");
checkContains("Payload reads reference_scoring", appJs, "analysis.reference_scoring");
checkContains("Payload reads the canonical records key", appJs, "ref.roi_descriptive_statistics");
checkContains("Payload reads the canonical status key", appJs, "ref.roi_descriptive_status");
checkContains("Payload sources rows from the score cache", appJs, "_niftiAnalysisEntries()");

// It must pass the raw ratio through, not a pre-formatted percentage.
checkNotContains("Lifecycle does not pre-format CoV", previewPanelSrc, "* 100");

// Rendering must not issue a network request or re-score.
checkNotContains("ROI payload helper performs no fetch", 
  appJs.slice(appJs.indexOf("function _roiDescriptivePayload()"),
              appJs.indexOf("function _niftiAnalysisEntries()")), "fetch(");
checkNotContains("Central renderer does not call the CSV endpoint",
  previewPanelSrc, "/api/export-roi-descriptive");
checkNotContains("Central renderer does not re-trigger scoring",
  previewPanelSrc, "/api/score");

// Empty renders must clear stale rows, not just hide the table.
checkContains("Empty render clears the table body", appJs, 'if (body) body.innerHTML = "";');

console.log(`\n=== Results: ${passed} passed, ${failed} failed ===\n`);
if (failed > 0) process.exit(1);
