"use strict";
// ─────────────────────────────────────────────────────────────────────────────
// OSIPI Perfusion Challenge — Review System
// app.js  v28
// ─────────────────────────────────────────────────────────────────────────────

const API = window.location.origin;

// ── Map type options per challenge ────────────────────────────────────────────

const MAP_OPTIONS = {
  asl:   ["CBF", "ATT", "Other"],
  dce:   ["Ktrans", "ve", "Kep", "Vp", "Other"],
  dsc:   ["CBF", "CBV", "MTT", "Other"],
  other: ["CBF", "ATT", "Ktrans", "ve", "Kep", "Vp", "CBV", "MTT", "Other"],
};

// ── Workflow state ─────────────────────────────────────────────────────────────

const wf = {
  step: "upload",  // current active step id
};

// ── Per-submission state ─────────────────────────────────────────────────────

const state = {
  mode:              "new",    // "new" | "edit" | "replace"
  submissionId:      null,
  validationResult:  null,
  pendingLocalFiles: null,
  selectedMapType:   null,
  detection: {
    nifti_count:                 null,
    detected_parameter_map_type: "Unknown",
  },
};

// Request guard — prevents concurrent submits from double-clicks
let requestInProgress = false;

// ── Batch state ───────────────────────────────────────────────────────────────

const batchState = {
  uploadData:      null,   // full /api/upload-batch response
  selectedIds:     new Set(),
  batchId:         null,   // returned by /api/validate-batch
  validationData:  null,   // full /api/validate-batch response
  isBatch:         false,  // true = multi-submission, false = single normalized as batch-of-1
};

// ── Session persistence config ────────────────────────────────────────────────

const SESSION_KEY        = "osipi_pipeline_session_v1";
const SESSION_VERSION    = 1;
const SESSION_EXPIRY_MS  = 24 * 60 * 60 * 1000;   // 24 hours

// Execution result summaries populated by _updateRunRow() — keyed by submission_id
const _execSummaries = {};

// Scoring result cache populated by _applyScoreStatus() — keyed by submission_id
const _scoreCache = {};

// NIfTI preview manifest cache populated by Results Summary.
const _previewManifestCache = {};
const _previewItemsById = {};
let _activePreviewMapId = null;
let _activePreviewPlane = "axial";

// Frontend-only list filters for review/validation/run/score history.
const _indexFilter = { search: "", challenge: "all", type: "all", status: "all", map: "all" };
const _leaderboardFilter = {
  entries: [],
  loading: false,
  error: "",
  search: "",
  date: "all",
  status: "all",
  challenge: "all",
  map: "all",
  sort: "newest",
};

// ── Docker availability ───────────────────────────────────────────────────────

const dockerStatus = { available: null, version: "", message: "" };

async function checkDockerAvailability() {
  if (dockerStatus.available !== null) return dockerStatus;
  try {
    const res  = await fetch(`${API}/api/execution-status`);
    const data = await res.json();
    dockerStatus.available = !!data.docker_available;
    dockerStatus.version   = data.docker_version || "";
    dockerStatus.message   = data.message || "";
  } catch (err) {
    dockerStatus.available = false;
    dockerStatus.message   = "Could not reach backend to check Docker availability.";
  }
  return dockerStatus;
}

// ── Tiny helpers ──────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function getRadio(name) {
  const checked = document.querySelector(`input[name="${name}"]:checked`);
  return checked ? checked.value : null;
}

function setLoading(btn, loading, label) {
  if (!btn) return;
  const nextLabel = label || btn.dataset.idleLabel || btn.textContent.trim();
  if (loading) {
    if (!btn.dataset.idleHtml) btn.dataset.idleHtml = btn.innerHTML;
    btn.dataset.idleLabel = nextLabel;
    btn.disabled = true;
    btn.classList.add("btn-loading");
    btn.setAttribute("aria-busy", "true");
    btn.innerHTML = `<span class="spinner" aria-hidden="true"></span>${escapeHtml(nextLabel)}…`;
    return;
  }

  btn.disabled = false;
  btn.classList.remove("btn-loading");
  btn.removeAttribute("aria-busy");
  if (btn.dataset.idleHtml && (!label || label === btn.dataset.idleLabel)) {
    btn.innerHTML = btn.dataset.idleHtml;
  } else if (nextLabel) {
    btn.textContent = nextLabel;
  }
  delete btn.dataset.idleHtml;
  delete btn.dataset.idleLabel;
}

function triggerDownload(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a   = document.createElement("a");
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click();
  document.body.removeChild(a); URL.revokeObjectURL(url);
}

function msgText(item) {
  if (item && typeof item === "object") return item.message || JSON.stringify(item);
  return String(item || "");
}

function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
}

function basenameOnly(value) {
  if (!value) return "";
  const parts = String(value).split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : String(value);
}

function cleanSubmissionName(value, fallback) {
  const raw = basenameOnly(value || fallback || "Submission");
  const noArchive = raw.replace(/\.(zip|tar|gz|tgz)$/i, "");
  return noArchive || "Submission";
}

function submissionDisplayName(sub, fallback) {
  return cleanSubmissionName(
    sub?.display_name || sub?.name || sub?.original_filename || sub?.source_folder,
    fallback || sub?.submission_id || "Submission"
  );
}

function challengeLabel(value) {
  return String(value || getChallengeType() || "dce").toUpperCase();
}

function hasRunInstructions(item) {
  return !!(item?.has_run_instructions ?? item?.has_dockerfile);
}

function hasResultMaps(item) {
  return !!item?.has_result_maps || Number(item?.nifti_count || 0) > 0 || item?.run_readiness === "result_only";
}

function inferredRunReadiness(item) {
  if (!item) return "not_runnable";
  if (item.run_readiness) return item.run_readiness;
  if (item.passed && hasRunInstructions(item)) return "runnable";
  if (item.passed && hasResultMaps(item)) return "result_only";
  if (!hasRunInstructions(item) && hasResultMaps(item)) return "result_only";
  return "not_runnable";
}

function submissionTypeInfo(item) {
  if (hasRunInstructions(item)) {
    return { label: "Reproducible code provided", state: "ready" };
  }
  if (hasResultMaps(item)) {
    return { label: "Result maps provided", state: "skipped" };
  }
  return { label: "Needs attention", state: "warning" };
}

function statusPill(label, state) {
  return `<span class="status-pill status-${escapeHtml(state || "pending")}">${escapeHtml(label)}</span>`;
}

function helpTooltip(text, label = "More information") {
  return `<button type="button" class="help-tooltip" aria-label="${escapeHtml(label)}">?<span class="tooltip-text">${escapeHtml(text)}</span></button>`;
}

function _filterOptionLabel(options, value) {
  const found = (options || []).find((opt) => opt.value === value);
  return found ? found.label : "All";
}

function _renderFilterDropdown(group, label, value, options) {
  const active = value && value !== "all";
  const selectedLabel = _filterOptionLabel(options, value);
  return `<div class="filter-dropdown" data-filter-dropdown="${escapeHtml(group)}">
    <button type="button" class="filter-pill${active ? " is-active" : ""}" data-filter-menu="${escapeHtml(group)}" aria-haspopup="menu" aria-expanded="false">
      <span class="filter-pill-label">${escapeHtml(label)}</span>
      <span class="filter-pill-value">${escapeHtml(selectedLabel)}</span>
      <span class="filter-pill-chevron" aria-hidden="true">⌄</span>
    </button>
    <div class="filter-menu" role="menu" hidden>
      ${(options || []).map((opt) => `<button type="button" class="filter-option${opt.value === value ? " is-selected" : ""}" role="menuitemradio" aria-checked="${opt.value === value ? "true" : "false"}" data-filter-option="${escapeHtml(group)}" data-filter-value="${escapeHtml(opt.value)}">
        <span class="filter-option-check" aria-hidden="true">${opt.value === value ? "✓" : ""}</span>
        <span>${escapeHtml(opt.label)}</span>
      </button>`).join("")}
    </div>
  </div>`;
}

function _renderSearchBox(id, value, placeholder = "Search") {
  return `<label class="filter-search" for="${escapeHtml(id)}">
    <span class="filter-search-icon" aria-hidden="true">⌕</span>
    <input type="search" id="${escapeHtml(id)}" value="${escapeHtml(value || "")}" placeholder="${escapeHtml(placeholder)}" autocomplete="off">
  </label>`;
}

function _renderChipGroup(group, value, options) {
  return `<div class="filter-chip-group" role="group" aria-label="${escapeHtml(group)} filters">
    ${(options || []).map((opt) => `<button type="button" class="filter-chip${opt.value === value ? " fc-active" : ""}" data-filter-option="${escapeHtml(group)}" data-filter-value="${escapeHtml(opt.value)}">${escapeHtml(opt.label)}</button>`).join("")}
  </div>`;
}

function _closeFilterMenus() {
  document.querySelectorAll(".filter-dropdown .filter-menu").forEach((menu) => {
    menu.hidden = true;
  });
  document.querySelectorAll("[data-filter-menu]").forEach((btn) => {
    btn.setAttribute("aria-expanded", "false");
  });
}

function _dateWithinFilter(value, filter) {
  if (!filter || filter === "all") return true;
  if (!value) return false;
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return false;
  const now = Date.now();
  const ageMs = now - dt.getTime();
  const day = 24 * 60 * 60 * 1000;
  const limits = {
    "24h": day,
    "7d": 7 * day,
    "1m": 31 * day,
    "1q": 92 * day,
    "1y": 366 * day,
  };
  return ageMs <= (limits[filter] || Infinity);
}

function _handleFilterSelection(group, value) {
  if (group.startsWith("leaderboard-")) {
    const key = group.replace("leaderboard-", "");
    if (key in _leaderboardFilter) _leaderboardFilter[key] = value;
    _renderLeaderboardEntries();
    return;
  }
  if (group.startsWith("index-")) {
    const key = group.replace("index-", "");
    if (key in _indexFilter) _indexFilter[key] = value;
    if (batchState.uploadData) renderBatchTable(batchState.uploadData.submissions || []);
    return;
  }
  if (group === "validation-status") {
    _reviewFilter.filter = value;
    _reviewFilter.showAll = false;
    _refreshValidationFilterBar();
    _applyReviewFilters();
    return;
  }
  if (group === "validation-sort") {
    _reviewFilter.sort = value;
    _refreshValidationFilterBar();
    _applyReviewFilters();
    return;
  }
  if (group === "run-status") {
    _runFilter.view = value;
    _runFilter.showAll = false;
    _refreshRunFilterBar();
    _applyRunFilters();
  }
}

document.addEventListener("click", (e) => {
  const menuBtn = e.target.closest("[data-filter-menu]");
  if (menuBtn) {
    e.preventDefault();
    const menu = menuBtn.closest(".filter-dropdown")?.querySelector(".filter-menu");
    const wasOpen = menu && !menu.hidden;
    _closeFilterMenus();
    if (menu && !wasOpen) {
      menu.hidden = false;
      menuBtn.setAttribute("aria-expanded", "true");
    }
    return;
  }

  const option = e.target.closest("[data-filter-option]");
  if (option) {
    e.preventDefault();
    _handleFilterSelection(option.dataset.filterOption || "", option.dataset.filterValue || "all");
    _closeFilterMenus();
    return;
  }

  if (!e.target.closest(".filter-dropdown")) _closeFilterMenus();
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") _closeFilterMenus();
});

function issueCount(result, field) {
  if (!result) return 0;
  if (Array.isArray(result[field])) return result[field].length;
  const countField = field === "warnings" ? "warning_count" : "error_count";
  return Number(result[countField] || 0);
}

function dedupeMessages(items) {
  const seen = new Set();
  return (items || []).filter((item) => {
    const key = String(item || "").trim().toLowerCase();
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

function simplifyMessage(item) {
  const text  = msgText(item);
  const lower = text.toLowerCase();

  if (lower.includes("nifti file appears") || lower.includes("appears to be empty") ||
      lower.includes("zero-byte") || lower.includes("empty nifti"))
    return "Zero-byte NIfTI file detected";

  if (lower.includes("no .nii") || lower.includes("no nifti") || lower.includes("missing nifti"))
    return "No output files found";

  if (lower.includes("expected") && lower.includes("parameter map") && lower.includes("not found")) {
    const m = text.match(/\b(ktrans|kep|vp|cbf|cbv|mtt|att|ve|vb|adc|t1|t2)\b/i) ||
              text.match(/expected[^a-z]+([A-Za-z0-9]+)\s+parameter/i);
    const mapName = m ? m[1].toUpperCase() : null;
    return mapName ? `Missing expected map: ${mapName}` : "Expected parameter map not found";
  }

  if (lower.includes("readme") || lower.includes("sop"))
    return "README or SOP file missing";

  if (lower.includes("metadata") && (lower.includes("missing") || lower.includes("invalid") ||
      lower.includes("not found")))
    return "Metadata file missing or invalid";

  if (lower.includes("map type could not") || lower.includes("could not auto-detect") ||
      lower.includes("multiple parameter map types") ||
      (lower.includes("map type") && (lower.includes("auto") || lower.includes("undetect"))))
    return "Parameter map type could not be determined";

  if (lower.includes("count mismatch") || lower.includes("nifti_count_mismatch") ||
      (lower.includes("were expected") && lower.includes("found")))
    return "NIfTI count does not match expected";

  if (lower.includes("duplicate") && lower.includes("file"))
    return "Duplicate filename in submission";

  if (lower.includes("no output maps found") || lower.includes("no existing output maps") ||
      lower.includes("maps will be generated"))
    return "No pre-existing output maps (will be generated on run)";

  if (lower.includes("no run instructions") || lower.includes("run instructions not found"))
    return "No run instructions found";

  if (lower.includes("multiple dockerfiles") || lower.includes("multiple run instructions"))
    return "Multiple run instruction files found";

  if (lower.includes("dockerfile") && (lower.includes("missing") || lower.includes("not found")))
    return "Run instructions not found";

  if (lower.includes("dockerfile") || lower.includes("code file"))
    return "No code files detected";

  if (text.length > 90) return text.slice(0, 87) + "…";
  return text;
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleString("en-US", {
      year: "numeric", month: "short", day: "numeric",
      hour: "2-digit", minute: "2-digit",
    });
  } catch { return iso.slice(0, 16).replace("T", " "); }
}

function buildSuccessChecks(data, errCount, warnCount, detectedMapType) {
  const checks    = [];
  const allIssues = [...(data.errors || []), ...(data.warnings || [])]
    .map(msgText).map((t) => t.toLowerCase());
  const hasIssue  = (...needles) =>
    allIssues.some((m) => needles.some((n) => m.includes(n)));

  if (Number(data.nifti_count) > 0 &&
      !hasIssue("no .nii", "no nifti", "nifti file appears", "missing nifti"))
    checks.push(`${data.nifti_count} NIfTI file${data.nifti_count !== 1 ? "s" : ""} detected`);
  if (!hasIssue("readme", "sop", "metadata"))
    checks.push("README / SOP found");
  if (!hasIssue("code file", "dockerfile", "requirements", "no_code"))
    checks.push("Code files present");
  if (!hasIssue("map type", "auto-detect", "map_type", "parameter map was not found")) {
    const mt = data.map_type || detectedMapType || state.detection.detected_parameter_map_type;
    if (mt && mt !== "Unknown")
      checks.push(`Parameter map type identified: ${mt}`);
  }
  if (!hasIssue("were expected", "count mismatch", "nifti_count_mismatch", "expected parameter map"))
    checks.push("Map count matches expectations");
  if (errCount === 0 && warnCount === 0)
    checks.push("All checks passed — submission is ready for scoring");

  return checks;
}

// ── Workflow navigation ───────────────────────────────────────────────────────

const WF_STEPS = ["upload", "index", "validate", "run", "score", "summary", "export"];

const STEP_TITLES = {
  upload:   { title: "Upload",             sub: "Submit parameter maps for automated validation" },
  index:    { title: "Review",             sub: "Detected submissions ready to validate" },
  validate: { title: "Validate",           sub: "Validation results for all submissions" },
  run:      { title: "Run",                sub: "Execute validated submissions" },
  score:    { title: "Score",              sub: "Score validated submissions using the OSIPI TF6.2 provider" },
  summary:  { title: "Results Summary",    sub: "Summary of validation, execution, and scoring results" },
  export:   { title: "Export",             sub: "Download results as CSV" },
};

const COMPACT_PROGRESS_STEPS = [
  { id: "upload",   label: "Upload" },
  { id: "index",    label: "Review" },
  { id: "validate", label: "Validate" },
  { id: "run",      label: "Run" },
  { id: "score",    label: "Score" },
  { id: "summary",  label: "Summary" },
  { id: "export",   label: "Export" },
];

function goToStep(step) {
  wf.step = step;
  document.body.dataset.step = step;   // CSS hook for the current wizard step
  WF_STEPS.forEach((s) => {
    const panel = el(`step-${s}`);
    if (panel) panel.hidden = (s !== step);
  });
  _syncWfNav();
  // Scroll the content area to top on step change
  document.querySelector(".content")?.scrollTo({ top: 0, behavior: "instant" });
  window.scrollTo({ top: 0, behavior: "instant" });
  // Update header title (page-title / page-subtitle may not exist in topbar layout — no-op if missing)
  const titles = STEP_TITLES[step] || {};
  const hTitle = el("page-title");
  const hSub   = el("page-subtitle");
  if (hTitle) hTitle.textContent  = titles.title || step;
  if (hSub)   hSub.textContent    = titles.sub   || "";
  // Update local wizard actions
  _updateWizardFooter(step);
  // Persist step to session
  saveSessionState();
}

// ── Wizard Actions ────────────────────────────────────────────────────────────

const _WF_FOOTER_CONFIG = {
  upload:   { back: null,       next: null,       nextLabel: "Upload and Continue",     hint: "Fill in team details and choose a submission file below" },
  index:    { back: "upload",   next: "validate", nextLabel: "Validate Submission",     hint: "" },
  validate: { back: "index",    next: "run",       nextLabel: "Continue to Run",        hint: "" },
  run:      { back: "validate", next: "score",     nextLabel: "Continue to Score",      hint: "" },
  score:    { back: "run",      next: "summary",  nextLabel: "Continue",                hint: "" },
  summary:  { back: "score",    next: "export",   nextLabel: "Continue to Export",      hint: "" },
  export:   { back: "summary",  next: null,        nextLabel: "Finish",                  hint: "" },
};

function _selectedSubmissionCount() {
  return batchState.selectedIds.size || batchState.uploadData?.submissions?.length || 0;
}

function _allValidationResultsAreResultOnly() {
  const results = batchState.validationData ? (batchState.validationData.results || []) : [];
  return results.length > 0 && results.every((r) => inferredRunReadiness(r) === "result_only");
}

function _hideLegacyWizardFooter() {
  const footer  = el("wizard-footer");
  const backBtn = el("wf-back-btn");
  const nextBtn = el("wf-next-btn");
  if (!footer) return;
  footer.hidden = true;
  footer.style.display = "none";
  delete footer.dataset.disabledReason;
  footer.removeAttribute("title");
  if (backBtn) {
    backBtn.onclick = null;
    backBtn.style.display = "";
    backBtn.style.visibility = "hidden";
  }
  if (nextBtn) {
    nextBtn.onclick = null;
    nextBtn.disabled = true;
    nextBtn.removeAttribute("aria-label");
    nextBtn.removeAttribute("aria-describedby");
    delete nextBtn.dataset.disabledReason;
  }
}

function _stepActionHost(step) {
  if (step === "index") {
    return el("step-index")?.querySelector(".pg-card") || el("step-index");
  }
  if (step === "run" && _allValidationResultsAreResultOnly()) {
    return el("run-skipped-notice") || el("step-run");
  }
  return el(`step-${step}`);
}

function _stepActionPrimary(step) {
  return document.querySelector(`[data-step-action-row="${step}"] .step-action-primary`);
}

function _syncInactiveStepActions(activeStep) {
  document.querySelectorAll("[data-step-action-row]").forEach((row) => {
    const isActive = row.dataset.stepActionRow === activeStep && activeStep !== "upload";
    row.hidden = !isActive;
    row.style.display = isActive ? "" : "none";
  });
}

function _ensureStepActionRow(step) {
  const cfg = _WF_FOOTER_CONFIG[step];
  if (!cfg || step === "upload") return null;
  const host = _stepActionHost(step);
  if (!host) return null;

  let row = document.querySelector(`[data-step-action-row="${step}"]`);
  if (!row) {
    row = document.createElement("div");
    row.className = "step-action-row";
    row.dataset.stepActionRow = step;
    row.innerHTML = `
      <div class="step-action-left">
        <button type="button" class="btn btn-secondary step-action-back">Back</button>
      </div>
      <div class="step-action-right">
        <button type="button" class="btn btn-primary step-action-primary">Continue</button>
      </div>`;
  }

  if (row.parentElement !== host) host.appendChild(row);
  return row;
}

function _stepPrimaryLabel(step) {
  if (step === "run" && _allValidationResultsAreResultOnly()) return "Continue to Score";
  return _WF_FOOTER_CONFIG[step]?.nextLabel || "Continue";
}

function _advanceWizardStep(step) {
  const cfg = _WF_FOOTER_CONFIG[step];
  if (!cfg) return;

  if (step === "index") {
    const selected = batchState.selectedIds.size
      ? [...batchState.selectedIds]
      : (batchState.uploadData?.submissions || []).map((s) => s.submission_id);
    if (selected.length > 0) {
      runBatchValidation(selected);
    } else {
      unlockStep("validate");
      goToStep("validate");
    }
    return;
  }

  if (step === "export") return;
  if (!cfg.next) return;

  unlockStep(cfg.next);
  if (cfg.next === "run")     { renderRunStep().catch(() => {}); }
  if (cfg.next === "score")   { renderScoreStep().catch(() => {}); }
  if (cfg.next === "summary") { renderSummaryStep(); }
  if (cfg.next === "export")  { _syncExportStep(); }
  goToStep(cfg.next);
}

function _syncStepActionRow(step) {
  _syncInactiveStepActions(step);
  if (step === "upload") return;

  const cfg = _WF_FOOTER_CONFIG[step];
  const row = _ensureStepActionRow(step);
  if (!cfg || !row) return;

  row.hidden = false;
  row.style.display = "";
  delete row.dataset.disabledReason;
  row.removeAttribute("title");

  const backBtn = row.querySelector(".step-action-back");
  const primaryBtn = row.querySelector(".step-action-primary");

  if (backBtn) {
    if (cfg.back) {
      backBtn.style.display = "";
      backBtn.onclick = () => goToStep(cfg.back);
    } else {
      backBtn.style.display = "none";
      backBtn.onclick = null;
    }
  }

  if (!primaryBtn) return;
  const label = _stepPrimaryLabel(step);
  if (!primaryBtn.classList.contains("btn-loading")) primaryBtn.textContent = label;
  primaryBtn.style.opacity = "";
  primaryBtn.title = "";
  primaryBtn.removeAttribute("aria-describedby");
  primaryBtn.removeAttribute("aria-label");
  delete primaryBtn.dataset.disabledReason;

  const canProceed = step === "export" ? true : _isStepReady(step);
  primaryBtn.disabled = !canProceed;
  primaryBtn.onclick = () => {
    if (primaryBtn.disabled) return;
    _advanceWizardStep(step);
  };

  const blockedReason = canProceed ? "" : _stepBlockedReason(step);
  if (blockedReason) {
    primaryBtn.title = blockedReason;
    primaryBtn.dataset.disabledReason = blockedReason;
    primaryBtn.setAttribute("aria-label", `${label}. ${blockedReason}`);
    row.dataset.disabledReason = blockedReason;
    row.title = blockedReason;
  }

  if (step === "export") {
    primaryBtn.style.opacity = "0.72";
    primaryBtn.title = "All done. Start a new session anytime.";
    delete row.dataset.disabledReason;
    row.removeAttribute("title");
  }
}

function _updateWizardFooter(step) {
  const cfg = _WF_FOOTER_CONFIG[step];
  if (!cfg) return;
  _hideLegacyWizardFooter();

  if (step === "upload") {
    const gnBar = el("global-start-new");
    if (gnBar) { gnBar.hidden = true; gnBar.style.display = "none"; }  // no Start-new on first step
    _syncUploadSubmitButton();
    _syncInactiveStepActions("upload");
    return;
  }

  // Show global Start New bar on non-upload steps
  const gnBar = el("global-start-new");
  if (gnBar) { gnBar.hidden = false; gnBar.style.display = ""; }
  _syncStepActionRow(step);
}

// True when a submission source/file is selected on the Upload step (so the
// in-card "Upload and Detect" button can enable). Edit mode (re-validate
// an existing submission) is always ready.
function _canUpload() {
  if (state.mode === "edit" && state.submissionId) return true;
  const source = getSourceType();
  if (source === "zenodo") return !!(el("zenodo-input") && el("zenodo-input").value.trim());
  if (source === "github") return !!(el("github-url") && el("github-url").value.trim());
  return !!state.pendingLocalFiles; // local
}

function _syncUploadSubmitButton() {
  const submitBtn = el("submit-btn");
  if (!submitBtn) return;
  const label = submitLabel();
  if (!submitBtn.classList.contains("btn-loading")) {
    submitBtn.textContent = label;
  }
  const canUpload = _canUpload();
  submitBtn.disabled = !canUpload;
  if (!canUpload) {
    const reason = "Choose a submission file or source to continue.";
    submitBtn.title = reason;
    submitBtn.dataset.disabledReason = reason;
    submitBtn.setAttribute("aria-label", `${label}. ${reason}`);
  } else {
    submitBtn.title = "";
    submitBtn.removeAttribute("aria-label");
    delete submitBtn.dataset.disabledReason;
  }
}

// Whether the current step allows proceeding to next
function _isStepReady(step) {
  switch (step) {
    case "upload":  return _canUpload(); // enabled once a file/source is selected
    // Index: ready as soon as upload data exists (user can run validation)
    case "index":
      return !!(batchState.uploadData || batchState.validationData || state.submissionId);
    case "validate": {
      const results = batchState.validationData ? (batchState.validationData.results || []) : [];
      // Warnings never block. Continue → Run is enabled when at least one
      // submission has ZERO blocking errors (r.passed === error_count === 0).
      // Run-readiness (runnable vs result-only) is decided on the Run step, so
      // it must NOT gate the action row here — a result-only submission that passed
      // with warnings still has 0 errors and must be allowed to continue.
      return results.some((r) => r.passed || issueCount(r, "errors") === 0);
    }
    case "run":    return true;  // always allow continue — non-blocking
    case "score":  return true;  // always allow continue to export
    // Summary is a read-only review: never trap the user. Once they reach it,
    // Continue → Export must always be available (validation/execution exports
    // are useful even when scoring is not configured).
    case "summary": return true;
    case "export": return false; // no "next" after export
    default: return false;
  }
}

function _stepBlockedReason(step) {
  switch (step) {
    case "index":
      if (!(batchState.uploadData || batchState.validationData || state.submissionId)) {
        return "Upload a submission before validating.";
      }
      if (batchState.uploadData && batchState.isBatch && batchState.selectedIds.size === 0) {
        return "Select at least one submission to validate.";
      }
      return "Review the detected submission before validating.";
    case "validate": {
      const results = batchState.validationData ? (batchState.validationData.results || []) : [];
      if (!results.length) return "Run validation before continuing.";
      return "Every submission has blocking errors. Fix the errors to continue (warnings do not block).";
    }
    default:
      return "Complete the current step before continuing.";
  }
}

// Refresh local wizard actions without changing step (e.g. after validation finishes)
function _refreshWizardFooter() {
  _updateWizardFooter(wf.step);
  _syncCompactProgress();
}

// Show a step completion banner (id: either "validate-completion-banner" or "run-completion-banner")
function _showCompletionBanner(bannerId, html, type) {
  const banner = el(bannerId);
  if (!banner) return;
  banner.innerHTML    = html;
  banner.className    = `step-completion-banner ${type || "info"}`;
  banner.style.display = "";
}

function unlockStep(step) {
  const btn = el(`wf-btn-${step}`);
  if (btn) btn.disabled = false;
  // Also unlock the visible top progress item
  const tsn = el(`tsn-${step}`);
  if (tsn && !tsn.classList.contains("tsn-active")) {
    tsn.classList.remove("tsn-locked");
    tsn.classList.add("tsn-unlocked");
  }
  _syncCompactProgress();
}

function _syncWfNav() {
  WF_STEPS.forEach((s) => {
    // Hidden state-holder buttons
    const btn = el(`wf-btn-${s}`);
    if (btn) btn.classList.toggle("wf-active", s === wf.step);
    // Visible top step progress items
    const tsn = el(`tsn-${s}`);
    if (!tsn) return;
    tsn.classList.remove("tsn-active", "tsn-unlocked", "tsn-locked");
    const stateBtn = el(`wf-btn-${s}`);
    if (s === wf.step) {
      tsn.classList.add("tsn-active");
    } else if (stateBtn && !stateBtn.disabled) {
      tsn.classList.add("tsn-unlocked");
    } else {
      tsn.classList.add("tsn-locked");
    }
  });
  _syncCompactProgress();
}

function _stepUnlocked(step) {
  if (step === "upload") return true;
  const btn = el(`wf-btn-${step}`);
  return !!btn && !btn.disabled;
}

function _validationProgressStatus() {
  const results = batchState.validationData ? (batchState.validationData.results || []) : [];
  if (!results.length) return null;
  const hasError = results.some((r) => (issueCount(r, "errors") > 0) || !r.passed);
  const hasWarning = results.some((r) => issueCount(r, "warnings") > 0);
  const actionable = results.some((r) =>
    r.passed && ["runnable", "result_only"].includes(inferredRunReadiness(r))
  );
  if (hasError && !actionable) return { state: "error", label: "Blocked" };
  if (hasError || hasWarning) return { state: "warning", label: "Needs review" };
  return { state: "complete", label: "Complete" };
}

function _runProgressStatus() {
  const results = batchState.validationData ? (batchState.validationData.results || []) : [];
  if (!results.length) return null;
  if (_allValidationResultsAreResultOnly()) return { state: "warning", label: "Skipped" };
  const execs = Object.values(_execSummaries);
  if (execs.some((r) => r.status === "failed" || r.status === "timed-out" || r.timedOut)) {
    return { state: "error", label: "Needs review" };
  }
  if (execs.length > 0) return { state: "complete", label: "Complete" };
  return { state: "ready", label: "Ready" };
}

function _scoreProgressStatus() {
  const scores = Object.values(_scoreCache);
  if (scores.some((s) => s.status === "failed")) return { state: "error", label: "Needs review" };
  if (scores.some((s) => s.status === "scored")) return { state: "complete", label: "Complete" };
  const ncCard = el("score-not-configured-card");
  if (ncCard && ncCard.style.display !== "none" && _stepUnlocked("score")) {
    return { state: "warning", label: "Optional" };
  }
  return null;
}

function _compactStatusForStep(step) {
  const unlocked = _stepUnlocked(step);
  if (!unlocked) return { state: "locked", label: "Locked" };

  if (step === "upload") {
    return (batchState.uploadData || batchState.validationData || state.submissionId)
      ? { state: "complete", label: "Complete" }
      : { state: "ready", label: "Ready" };
  }
  if (step === "index") {
    return batchState.validationData
      ? { state: "complete", label: "Complete" }
      : { state: "ready", label: "Ready" };
  }
  if (step === "validate") return _validationProgressStatus() || { state: "ready", label: "Ready" };
  if (step === "run")      return _runProgressStatus() || { state: "ready", label: "Ready" };
  if (step === "score")    return _scoreProgressStatus() || { state: "ready", label: "Ready" };
  if (step === "summary")  return _stepUnlocked("export") ? { state: "complete", label: "Complete" } : { state: "ready", label: "Ready" };
  if (step === "export")   return { state: "ready", label: "Ready" };
  return { state: "pending", label: "Pending" };
}

function _ensureCompactProgress() {
  const nav = el("compact-progress");
  if (!nav || nav.dataset.ready === "1") return nav;
  nav.innerHTML = COMPACT_PROGRESS_STEPS.map((step, idx) => `
    <button type="button" id="cp-${step.id}" class="compact-progress-item" data-step="${step.id}">
      <span class="compact-progress-dot" aria-hidden="true">${idx + 1}</span>
      <span class="compact-progress-label">${escapeHtml(step.label)}</span>
    </button>
  `).join("");
  nav.addEventListener("click", (e) => {
    const btn = e.target.closest(".compact-progress-item");
    if (!btn || btn.disabled) return;
    const targetStep = btn.dataset.step;
    if (!targetStep || targetStep === wf.step) return;
    if (targetStep === "run") renderRunStep().catch(() => {});
    if (targetStep === "score") renderScoreStep().catch(() => {});
    if (targetStep === "summary") renderSummaryStep();
    if (targetStep === "export") _syncExportStep();
    goToStep(targetStep);
  });
  nav.dataset.ready = "1";
  return nav;
}

function _syncCompactProgress() {
  const nav = _ensureCompactProgress();
  if (!nav) return;
  const currentIdx = WF_STEPS.indexOf(wf.step);
  COMPACT_PROGRESS_STEPS.forEach((step) => {
    const btn = el(`cp-${step.id}`);
    if (!btn) return;
    const idx = WF_STEPS.indexOf(step.id);
    const status = _compactStatusForStep(step.id);
    const isCurrent = step.id === wf.step;
    const clickableBack = idx < currentIdx && _stepUnlocked(step.id);
    const disabled = !clickableBack;

    btn.className = [
      "compact-progress-item",
      `is-${status.state}`,
      isCurrent ? "is-current" : "",
      clickableBack ? "is-clickable" : "is-disabled",
    ].filter(Boolean).join(" ");
    btn.disabled = disabled;
    btn.setAttribute("aria-disabled", String(disabled));
    if (isCurrent) btn.setAttribute("aria-current", "step");
    else btn.removeAttribute("aria-current");

    const future = idx > currentIdx;
    btn.title = clickableBack
      ? `Go back to ${step.label}`
      : isCurrent ? `${step.label}: current step`
      : future ? `${step.label}: complete previous steps first`
      : `${step.label}: ${status.label}`;
  });
}

// Wire wf-nav button clicks
document.querySelectorAll(".wf-step[data-step]").forEach((btn) => {
  btn.addEventListener("click", () => {
    if (btn.disabled) return;
    goToStep(btn.dataset.step);
    // Refresh step-specific content on nav click
    if (btn.dataset.step === "run")   _renderRunPanel();
    if (btn.dataset.step === "score") renderScoreStep().catch(() => {});
  });
});

// ── Form getters ──────────────────────────────────────────────────────────────

function getTeamName()    { const f = el("team-name");     return f ? f.value.trim() : ""; }
function getEmail()       { const f = el("contact-email"); return f ? f.value.trim() : ""; }
function getSourceType()  { return getRadio("submission_type") || "local"; }

function getChallengeType() {
  const raw = getRadio("challenge_type") || "dce";
  if (raw === "other") {
    const other = el("challenge-type-other");
    return (other ? other.value.trim() : "") || "other";
  }
  return raw;
}

function getMapType() {
  if (!state.selectedMapType) return null;
  if (state.selectedMapType === "Other") {
    const other = el("map-type-other");
    return (other && other.value.trim()) ? other.value.trim() : "Other";
  }
  return state.selectedMapType;
}

function getMapTypeMode() { return state.selectedMapType ? "manual" : "auto"; }

// ── Submit button label ───────────────────────────────────────────────────────

function submitLabel() {
  if (state.mode === "edit" && state.submissionId) return "Save Changes and Revalidate";
  return "Upload and Detect";
}

function syncSubmitLabel() {
  setLoading(el("submit-btn"), false, submitLabel());
  _syncUploadSubmitButton();
}

// ── State reset ───────────────────────────────────────────────────────────────

function clearSubmissionData() {
  state.submissionId      = null;
  state.validationResult  = null;
  state.pendingLocalFiles = null;
  state.detection = { nifti_count: null, detected_parameter_map_type: "Unknown" };

  const lbl = el("local-file-label");
  if (lbl) { lbl.textContent = ""; lbl.className = "file-label"; }

  ["zenodo-input", "github-url", "github-branch"].forEach((id) => {
    const f = el(id); if (f) f.value = "";
  });

  const srcErr = el("source-error");
  if (srcErr) srcErr.textContent = "";
  clearSubmitStatus();
}

function resetAll() {
  clearSubmissionData();
  state.mode            = "new";
  state.selectedMapType = null;

  batchState.uploadData      = null;
  batchState.selectedIds.clear();
  batchState.batchId         = null;
  batchState.validationData  = null;
  batchState.isBatch         = false;

  // Reset nav steps (all steps except upload)
  ["index", "validate", "run", "score", "summary", "export"].forEach((s) => {
    const btn = el(`wf-btn-${s}`);
    if (btn) btn.disabled = true;
    if (btn) btn.classList.remove("wf-done", "wf-warn", "wf-fail");
  });

  // Clear persisted session
  clearSessionState();

  // Session chip
  const chip = el("session-chip");
  if (chip) chip.style.display = "none";

  ["team-name", "contact-email", "challenge-type-other", "map-type-other"].forEach((id) => {
    const f = el(id); if (f) f.value = "";
  });

  const dceRadio = document.querySelector("input[name='challenge_type'][value='dce']");
  if (dceRadio) dceRadio.checked = true;
  const wrap = el("challenge-other-wrap");
  if (wrap) wrap.style.display = "none";

  updateMapTypePills("dce");

  const localRadio = document.querySelector("input[name='submission_type'][value='local']");
  if (localRadio) localRadio.checked = true;
  switchSource("local");

  ["team-name", "contact-email"].forEach((id) => {
    const inp = el(id); if (inp) inp.classList.remove("field-invalid");
    const err = el(`${id}-error`); if (err) err.textContent = "";
  });
  const ctErr = el("challenge-type-error");
  if (ctErr) ctErr.textContent = "";
}

// ══════════════════════════════════════════════════════════════════════════════
// Session persistence  (localStorage key: osipi_pipeline_session_v1)
// Rules:
//   • No auto-restore — banner shown, user must click "Restore"
//   • 24 h expiry — expired sessions are silently discarded
//   • No files, logs, CSVs, or large arrays stored — IDs and summaries only
// ══════════════════════════════════════════════════════════════════════════════

function _generateSessionId() {
  return `sess_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 8)}`;
}

function saveSessionState() {
  try {
    const existing = loadSessionState();
    const now      = new Date().toISOString();

    const submissions = (batchState.uploadData?.submissions || []).map((s) => ({
      submission_id:               s.submission_id,
      nifti_count:                 s.nifti_count,
      detected_parameter_map_type: s.detected_parameter_map_type,
      has_run_instructions:        s.has_run_instructions,
      source_folder:               s.source_folder,
      detection_warning:           s.detection_warning,
      status:                      s.status,
    }));

    const validationSummary = batchState.validationData ? {
      batchId:     batchState.batchId,
      total:       (batchState.validationData.results || []).length,
      passedCount: batchState.validationData.passed_count || 0,
      failedCount: batchState.validationData.failed_count || 0,
      results:     (batchState.validationData.results || []).map((r) => ({
        submission_id:      r.submission_id,
        passed:             r.passed,
        errorCount:         (r.errors   || []).length,
        warningCount:       (r.warnings || []).length,
        mapType:            r.map_type,
        niftiCount:         r.nifti_count,
        hasRunInstructions: r.has_run_instructions,
        hasResultMaps:      r.has_result_maps || false,
        runReadiness:       r.run_readiness || null,
        challengeType:      r.challenge_type,
        teamName:           r.team_name,
        contactEmail:       r.contact_email,
        validatedAt:        r.validated_at,
        // Store first 3 errors/warnings as short text only — no raw logs
        topErrors:   (r.errors   || []).slice(0, 3).map((e) => msgText(e).slice(0, 80)),
        topWarnings: (r.warnings || []).slice(0, 3).map((w) => msgText(w).slice(0, 80)),
      })),
    } : null;

    const payload = {
      version:            SESSION_VERSION,
      sessionId:          existing?.sessionId || _generateSessionId(),
      createdAt:          existing?.createdAt || now,
      updatedAt:          now,
      step:               wf.step,
      submissionId:       state.submissionId,
      batchId:            batchState.batchId,
      isBatch:            batchState.isBatch,
      sourceType:         getSourceType(),
      challengeType:      getChallengeType(),
      mapType:            state.selectedMapType,
      teamName:           getTeamName(),
      contactEmail:       getEmail(),
      submissions,
      selectedIds:        [...batchState.selectedIds],
      validationSummary,
      executionSummaries: { ..._execSummaries },
      // Scoring: store provider/status snapshot only — no metric values
      scoringSnapshot:    _collectScoringSnapshot(),
    };

    localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
  } catch (_) { /* localStorage unavailable or full — fail silently */ }
}

function loadSessionState() {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || parsed.version !== SESSION_VERSION) { clearSessionState(); return null; }
    // Expiry check
    const updatedAt = parsed.updatedAt ? new Date(parsed.updatedAt).getTime() : 0;
    if (Date.now() - updatedAt > SESSION_EXPIRY_MS) {
      clearSessionState();
      return null;
    }
    return parsed;
  } catch (_) { return null; }
}

function clearSessionState() {
  try { localStorage.removeItem(SESSION_KEY); } catch (_) {}
  // Reset exec + score summaries
  Object.keys(_execSummaries).forEach((k) => delete _execSummaries[k]);
  Object.keys(_scoreCache).forEach((k) => delete _scoreCache[k]);
}

// Collect provider status snapshot from the DOM (after score step renders)
function _collectScoringSnapshot() {
  const grid = el("score-provider-grid");
  if (!grid) return [];
  return [...grid.querySelectorAll(".score-provider-card")].map((card) => ({
    displayName: card.querySelector(".spc-title")?.textContent?.trim()       || "",
    status:      card.querySelector(".spc-status-label")?.textContent?.trim() || "",
  }));
}

// ── Restore chip (replaces the old full-width banner) ────────────────────────
// The old #restore-banner is kept in the DOM for smoke-test compatibility but
// is NEVER shown. All restore UI goes through #restore-chip-btn in the topbar.

function showRestoreBanner(saved) {
  // Build a human-readable summary for the notice text
  const parts = [];
  const subs      = saved.submissions?.length || 0;
  const typeLabel = saved.isBatch
    ? `batch · ${subs} submission${subs !== 1 ? "s" : ""}`
    : "single submission";
  parts.push(typeLabel);
  const stepTitle = STEP_TITLES[saved.step]?.title || saved.step || "";
  if (stepTitle) parts.push(`last step: ${stepTitle}`);

  // Show inline notice inside the Upload card
  const notice = el("upload-restore-notice");
  const msgEl  = el("upload-restore-msg");
  if (notice) {
    if (msgEl) msgEl.textContent = `Previous session found (${parts.join(", ")}).`;
    notice.classList.remove("upload-restore-notice--warn");
    notice.style.display = "";
  }

  // Keep hidden chip in sync so its click handler still works
  const chip = el("restore-chip-btn");
  if (chip) {
    chip.classList.remove("topbar-restore-chip--warn");
    chip.style.display = "";
  }
}

function _hideRestoreBanner() {
  // Hide upload-card restore notice
  const notice = el("upload-restore-notice");
  if (notice) notice.style.display = "none";
  // Hide hidden chip
  const chip = el("restore-chip-btn");
  if (chip) chip.style.display = "none";
  // Ensure legacy banner stays hidden
  const banner = el("restore-banner");
  if (banner) banner.style.display = "none";
}

// ── Restore logic ─────────────────────────────────────────────────────────────

async function restoreSessionFromStorage() {
  const saved = loadSessionState();
  if (!saved) return false;

  // 1. Restore form values
  const teamField  = el("team-name");
  const emailField = el("contact-email");
  if (teamField  && saved.teamName)     teamField.value  = saved.teamName;
  if (emailField && saved.contactEmail) emailField.value = saved.contactEmail;

  // Restore challenge type radio
  if (saved.challengeType) {
    const radio = document.querySelector(
      `input[name='challenge_type'][value='${saved.challengeType}']`
    );
    if (radio) {
      radio.checked = true;
      const wrap = el("challenge-other-wrap");
      if (wrap) wrap.style.display = (saved.challengeType === "other") ? "block" : "none";
    }
    updateMapTypePills(saved.challengeType);
  }

  // Restore map type pill selection
  if (saved.mapType) {
    state.selectedMapType = saved.mapType;
    updateMapTypePills(saved.challengeType || "dce");
  }

  // Restore source type radio
  if (saved.sourceType) {
    const radio = document.querySelector(
      `input[name='submission_type'][value='${saved.sourceType}']`
    );
    if (radio) { radio.checked = true; switchSource(saved.sourceType); }
  }

  // 2. Restore batch/submission state (no files — backend already has them)
  state.submissionId  = saved.submissionId || null;
  batchState.isBatch  = !!saved.isBatch;
  batchState.batchId  = saved.batchId || null;
  batchState.selectedIds = new Set(saved.selectedIds || []);

  if (saved.submissions && saved.submissions.length > 0) {
    batchState.uploadData = {
      batch:            saved.isBatch,
      submission_count: saved.submissions.length,
      submissions:      saved.submissions,
    };
  }

  // Restore execution summaries
  if (saved.executionSummaries) {
    Object.assign(_execSummaries, saved.executionSummaries);
  }

  // 3. Unlock steps up to the saved step
  const stepOrder    = ["upload", "index", "validate", "run", "score", "summary", "export"];
  const savedStepIdx = stepOrder.indexOf(saved.step);
  if (savedStepIdx >= 1) stepOrder.slice(1, savedStepIdx + 1).forEach((s) => unlockStep(s));

  // 4. Re-render index table
  if (savedStepIdx >= 1 && batchState.uploadData) {
    renderBatchTable(batchState.uploadData.submissions);
  }

  // 5. Re-render validation table from summary
  if (savedStepIdx >= 2 && saved.validationSummary) {
    const synthData = _synthValidationData(saved.validationSummary);
    batchState.validationData = synthData;
    // renderValidateStep may auto-advance to "run" — we'll override with goToStep after
    renderValidateStep(synthData);
  }

  // 6. Apply saved exec summaries to run rows (after run step renders)
  if (savedStepIdx >= 3 && Object.keys(_execSummaries).length > 0) {
    renderRunStep().then(() => _applyExecSummariesToRows()).catch(() => {});
  }

  // 7. Navigate to the saved step (overrides any auto-advance)
  goToStep(saved.step);
  _updateSessionChip();

  // 8. Async: verify backend files still exist
  const firstSubId = saved.submissionId || saved.submissions?.[0]?.submission_id;
  if (firstSubId && savedStepIdx >= 1) {
    _verifyBackendFiles(firstSubId);
  }

  return true;
}

// Build a synthetic validation data object from a persisted summary (no full error arrays)
function _synthValidationData(summary) {
  const results = (summary.results || []).map((r) => ({
    submission_id:        r.submission_id,
    passed:               r.passed,
    errors:               r.topErrors   || [],
    warnings:             r.topWarnings || [],
    nifti_count:          r.niftiCount,
    map_type:             r.mapType,
    has_run_instructions: r.hasRunInstructions,
    has_result_maps:      r.hasResultMaps || false,
    run_readiness:        r.runReadiness || null,
    challenge_type:       r.challengeType,
    team_name:            r.teamName,
    contact_email:        r.contactEmail,
    validated_at:         r.validatedAt,
    _restored:            true,
  }));
  return {
    batch_id:     summary.batchId,
    results,
    passed_count: summary.passedCount,
    failed_count: summary.failedCount,
    _restored:    true,
  };
}

// Apply saved exec summaries to run cards/rows (status labels only, no logs)
function _applyExecSummariesToRows() {
  Object.entries(_execSummaries).forEach(([subId, summary]) => {
    const wrap = _findRunCard(subId);
    if (!wrap) return;
    const prevStatus = wrap.dataset.execStatus;
    if (prevStatus && prevStatus !== "not-run") return; // already updated
    const newStatus = summary.status || "failed";
    wrap.dataset.execStatus = newStatus;
    const runnable = wrap.dataset.runnable === "true";

    // Update status display (card or table)
    const statusCell = wrap.querySelector(".er-run-status-cell, .run-card-status-row");
    if (statusCell) {
      const reasonEl   = statusCell.querySelector(".run-card-reason, .run-card-reason-warn");
      const reasonHtml = reasonEl ? reasonEl.outerHTML : "";
      statusCell.innerHTML = _erRunStatusHtml(newStatus, runnable) + reasonHtml;
    }
    const outputsCell = wrap.querySelector(".er-outputs-cell, .run-card-outputs");
    if (outputsCell && summary.outputFileCount > 0) {
      const fc = summary.outputFileCount;
      outputsCell.innerHTML = `<span class="vr-run-ok">${fc} file${fc !== 1 ? "s" : ""}</span>`;
    }
    const outCheckCell = wrap.querySelector(".er-outcheck-cell");
    if (outCheckCell) {
      outCheckCell.innerHTML = `<span style="font-size:0.72rem;color:var(--muted)">from session</span>`;
    }
    const drawer = wrap.querySelector(".er-row-detail, .run-card-detail");
    if (drawer) {
      drawer.innerHTML = `<p style="font-size:0.73rem;color:var(--muted);margin:0">Session restored — re-run to see full logs.</p>`;
    }
  });
  if (typeof _applyRunFilters === "function") _applyRunFilters();
}

// Check whether backend files exist for the restored session
async function _verifyBackendFiles(submissionId) {
  try {
    const res  = await fetch(`${API}/api/nifti-files/${encodeURIComponent(submissionId)}`);
    const data = await res.json();
    // If directory exists but is empty, files were deleted
    if (res.ok && Array.isArray(data.files) && data.files.length === 0) {
      _showRestoreWarning(
        "Saved session found, but backend files are no longer available. Please upload again."
      );
      clearSessionState();
    }
  } catch (_) { /* network error during check — silent */ }
}

function _showRestoreWarning(msg) {
  // Show the upload-card notice in warn state
  const notice = el("upload-restore-notice");
  const msgEl  = el("upload-restore-msg");
  if (notice) {
    if (msgEl) msgEl.textContent = "⚠ " + msg;
    notice.classList.add("upload-restore-notice--warn");
    notice.style.display = "";
  }
  // Keep hidden chip in sync
  const chip = el("restore-chip-btn");
  if (chip) {
    chip.classList.add("topbar-restore-chip--warn");
    chip.style.display = "";
  }
  // Clicking the restore button in the notice should clear state and dismiss
  const restoreBtn = el("upload-restore-btn");
  if (restoreBtn) {
    restoreBtn.textContent = "Dismiss";
    restoreBtn.onclick = function () {
      clearSessionState();
      _hideRestoreBanner();
    };
  }
}

// ── Map type pills ────────────────────────────────────────────────────────────

function updateMapTypePills(challengeType) {
  const container = el("map-type-pills");
  if (!container) return;

  const options = MAP_OPTIONS[challengeType] || MAP_OPTIONS.other;

  if (state.selectedMapType && !options.includes(state.selectedMapType)) {
    state.selectedMapType = null;
    const otherWrap = el("map-type-other-wrap");
    if (otherWrap) otherWrap.style.display = "none";
  }

  container.innerHTML = "";
  options.forEach((opt) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "map-pill" + (state.selectedMapType === opt ? " active" : "");
    btn.dataset.value = opt;
    btn.textContent = opt;
    btn.setAttribute("aria-pressed", String(state.selectedMapType === opt));
    btn.addEventListener("click", () => selectMapPill(opt));
    container.appendChild(btn);
  });
}

function selectMapPill(value) {
  const container = el("map-type-pills");
  if (!container) return;

  if (state.selectedMapType === value) {
    state.selectedMapType = null;
    const otherWrap = el("map-type-other-wrap");
    if (otherWrap) otherWrap.style.display = "none";
  } else {
    state.selectedMapType = value;
    const otherWrap = el("map-type-other-wrap");
    if (otherWrap) otherWrap.style.display = (value === "Other") ? "block" : "none";
  }

  container.querySelectorAll(".map-pill").forEach((btn) => {
    const active = (btn.dataset.value === state.selectedMapType);
    btn.classList.toggle("active", active);
    btn.setAttribute("aria-pressed", String(active));
  });
}

// ── Conditional UI wiring ─────────────────────────────────────────────────────

document.querySelectorAll("input[name='challenge_type']").forEach((r) => {
  r.addEventListener("change", () => {
    const wrap = el("challenge-other-wrap");
    if (wrap) wrap.style.display = (r.value === "other") ? "block" : "none";
    updateMapTypePills(r.value);
  });
});

// ── Source tab switching ──────────────────────────────────────────────────────

document.querySelectorAll("input[name='submission_type']").forEach((r) => {
  r.addEventListener("change", () => switchSource(r.value));
});

function switchSource(type) {
  document.querySelectorAll(".source-tab").forEach((tab) => {
    const inp = tab.querySelector("input[name='submission_type']");
    tab.classList.toggle("active", !!(inp && inp.value === type));
  });
  document.querySelectorAll(".source-panel").forEach((p) => p.classList.remove("active"));
  const panel = el(`source-${type}`);
  if (panel) panel.classList.add("active");
  _refreshWizardFooter();   // selection requirement differs per source
  _syncUploadSubmitButton();
}

// ── Local file inputs ─────────────────────────────────────────────────────────

const chooseBtn  = el("choose-btn");
const chooseMenu = el("choose-menu");
const dropZone   = el("drop-zone");

function setChooseMenuOpen(open) {
  if (!chooseMenu || !chooseBtn) return;
  chooseMenu.classList.toggle("open", open);
  chooseBtn.setAttribute("aria-expanded", String(open));
}

if (chooseBtn) {
  chooseBtn.addEventListener("click", (e) => {
    e.stopPropagation();
    setChooseMenuOpen(!chooseMenu.classList.contains("open"));
  });
}

if (chooseMenu) {
  chooseMenu.querySelectorAll("[data-choice]").forEach((btn) => {
    btn.addEventListener("click", (e) => {
      e.stopPropagation();
      setChooseMenuOpen(false);
      const choice = btn.dataset.choice;
      if (choice === "zip")    { const f = el("file-input");   if (f) f.click(); }
      if (choice === "folder") { const f = el("folder-input"); if (f) f.click(); }
      if (choice === "files")  { const f = el("files-input");  if (f) f.click(); }
    });
  });
}

document.addEventListener("click",   () => setChooseMenuOpen(false));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") setChooseMenuOpen(false); });

if (dropZone) {
  dropZone.addEventListener("dragover", (e) => {
    e.preventDefault();
    dropZone.classList.add("dragging");
  });
  dropZone.addEventListener("dragleave", () => dropZone.classList.remove("dragging"));
  dropZone.addEventListener("drop", (e) => {
    e.preventDefault();
    dropZone.classList.remove("dragging");
    const files = Array.from(e.dataTransfer.files || []);
    if (!files.length) return;
    const zip = files.find((f) => f.name.toLowerCase().endsWith(".zip"));
    setLocalFiles(zip ? [zip] : files,
                  zip ? zip.name : `${files.length} file${files.length !== 1 ? "s" : ""} dropped`);
  });
}

const fileInput = el("file-input");
if (fileInput) {
  fileInput.addEventListener("change", () => {
    const files = Array.from(fileInput.files || []).filter((f) =>
      f.name.toLowerCase().endsWith(".zip"));
    if (files.length) setLocalFiles(files, files[0].name);
    fileInput.value = "";
  });
}

const folderInput = el("folder-input");
if (folderInput) {
  folderInput.addEventListener("change", () => {
    const files = Array.from(folderInput.files || []);
    if (!files.length) return;
    const name = files[0].webkitRelativePath
      ? files[0].webkitRelativePath.split("/")[0]
      : "Folder";
    setLocalFiles(files, `${name} (${files.length} file${files.length !== 1 ? "s" : ""})`);
  });
}

const filesInput = el("files-input");
if (filesInput) {
  filesInput.addEventListener("change", () => {
    const files = Array.from(filesInput.files || []);
    if (files.length)
      setLocalFiles(files, `${files.length} file${files.length !== 1 ? "s" : ""} selected`);
    filesInput.value = "";
  });
}

function setLocalFiles(files, label) {
  state.pendingLocalFiles = files;
  const lbl = el("local-file-label");
  if (lbl) { lbl.textContent = label; lbl.className = "file-label ready"; }
  const srcErr = el("source-error");
  if (srcErr) srcErr.textContent = "";
  _refreshWizardFooter();   // enable the in-card "Upload and Detect" button
  _syncUploadSubmitButton();
}

// Enable/disable the in-card "Upload and Detect" button as Zenodo/GitHub inputs change.
["zenodo-input", "github-url"].forEach((id) => {
  const inp = el(id);
  if (inp) inp.addEventListener("input", () => {
    if (wf.step === "upload") {
      _refreshWizardFooter();
      _syncUploadSubmitButton();
    }
  });
});

// ── Form validation ───────────────────────────────────────────────────────────

function clearFieldError(inputId, errorId) {
  const inp = el(inputId); const err = el(errorId);
  if (inp) inp.classList.remove("field-invalid");
  if (err) err.textContent = "";
}

function setFieldError(inputId, errorId, message) {
  const inp = el(inputId); const err = el(errorId);
  if (inp) inp.classList.add("field-invalid");
  if (err) err.textContent = message;
}

function validateForm() {
  let ok = true;

  clearFieldError("team-name", "team-name-error");
  if (!getTeamName()) {
    setFieldError("team-name", "team-name-error", "Team name is required.");
    ok = false;
  }

  clearFieldError("contact-email", "contact-email-error");
  const email = getEmail();
  if (!email) {
    setFieldError("contact-email", "contact-email-error", "Contact email is required.");
    ok = false;
  } else if (!email.includes("@") || !email.split("@")[1]?.includes(".")) {
    setFieldError("contact-email", "contact-email-error", "Enter a valid email address.");
    ok = false;
  }

  const ctErr = el("challenge-type-error");
  if (ctErr) ctErr.textContent = "";
  if (getRadio("challenge_type") === "other") {
    const otherInp = el("challenge-type-other");
    if (otherInp && !otherInp.value.trim()) {
      if (ctErr) ctErr.textContent = "Enter a challenge type.";
      ok = false;
    }
  }

  const srcErr = el("source-error");
  if (srcErr) srcErr.textContent = "";
  if (!(state.mode === "edit" && state.submissionId)) {
    const source = getSourceType();
    if (source === "zenodo") {
      const zi = el("zenodo-input");
      if (!zi || !zi.value.trim()) {
        if (srcErr) srcErr.textContent = "Enter a Zenodo URL, DOI, or record ID.";
        ok = false;
      }
    } else if (source === "github") {
      const gi = el("github-url");
      if (!gi || !gi.value.trim()) {
        if (srcErr) srcErr.textContent = "Enter a GitHub repository URL.";
        ok = false;
      }
    } else if (source === "local" && !state.pendingLocalFiles) {
      if (srcErr) srcErr.textContent = "Choose a submission file or folder.";
      ok = false;
    }
  }

  if (!ok) {
    const firstErr = document.querySelector(".field-invalid, .field-error:not(:empty)");
    if (firstErr) firstErr.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  return ok;
}

// ── Upload functions ──────────────────────────────────────────────────────────

async function uploadLocalFiles() {
  const files = state.pendingLocalFiles;
  if (!files || !files.length) throw new Error("No files selected.");

  const isZip = files.length === 1 && files[0].name.toLowerCase().endsWith(".zip");

  if (isZip) {
    const fd = new FormData();
    fd.append("file", files[0]);
    const res  = await fetch(`${API}/api/upload-batch`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed.");
    return data;
  }

  const fd = new FormData();
  files.forEach((f) => fd.append("files", f, f.webkitRelativePath || f.name));
  const res  = await fetch(`${API}/api/upload-folder-batch`, { method: "POST", body: fd });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Folder upload failed.");
  return data;
}

async function importZenodo() {
  const zi = el("zenodo-input");
  const res = await fetch(`${API}/api/import-submission-zenodo`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({ zenodo_input: zi ? zi.value.trim() : "" }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Zenodo import failed.");
  return data;
}

async function importGithub() {
  const gi = el("github-url");
  const gb = el("github-branch");
  const res = await fetch(`${API}/api/import-submission-github`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify({
      repo_url: gi ? gi.value.trim() : "",
      branch:   gb && gb.value.trim() ? gb.value.trim() : null,
    }),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "GitHub import failed.");
  return data;
}

// ── Single-submission validate call ──────────────────────────────────────────

async function runValidation() {
  const payload = {
    submission_id:  state.submissionId,
    challenge_type: getChallengeType(),
    team_name:      getTeamName()  || null,
    contact_email:  getEmail()     || null,
    map_type:       getMapType(),
    map_type_mode:  getMapTypeMode(),
    notes:          null,
    mode:           "auto",
  };

  const res  = await fetch(`${API}/api/validate`, {
    method:  "POST",
    headers: { "Content-Type": "application/json" },
    body:    JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.detail || "Validation failed.");
  return data;
}

// ── Submit button status ──────────────────────────────────────────────────────

function showSubmitStatus(type, msg) {
  const s = el("submit-status");
  if (!s) return;
  s.style.display = "block";
  s.className = `submit-status status-${type}`;
  s.textContent = msg;
}

function clearSubmitStatus() {
  const s = el("submit-status");
  if (!s) return;
  s.style.display = "none";
  s.textContent = "";
}

// ── Session chip ──────────────────────────────────────────────────────────────

function _updateSessionChip() {
  const chip = el("session-chip");
  const teamEl = el("session-team");
  const challEl = el("session-challenge");
  if (!chip) return;
  const team  = getTeamName();
  const chall = getChallengeType().toUpperCase();
  if (team || chall) {
    if (teamEl)  teamEl.textContent  = team  || "—";
    if (challEl) challEl.textContent = chall || "—";
    chip.style.display = "";
  }
}

// ── Submit handler ────────────────────────────────────────────────────────────

const submitBtn = el("submit-btn");
if (submitBtn) submitBtn.addEventListener("click", handleSubmit);

async function handleSubmit() {
  if (requestInProgress) return;
  if (!validateForm()) return;

  requestInProgress = true;
  const btn = el("submit-btn");
  clearSubmitStatus();

  // ── Edit mode: reuse existing submission, re-validate ────────────────────
  if (state.mode === "edit" && state.submissionId) {
    setLoading(btn, true, "Saving & Revalidating");
    try {
      const result = await runValidation();
      state.validationResult = result;
      // Normalize to batch-of-1 and re-render validate step
      _renderSingleAsValidate(result);
    } catch (err) {
      showSubmitStatus("error", err.message || "Validation failed.");
    } finally {
      requestInProgress = false;
      setLoading(btn, false, submitLabel());
      _syncUploadSubmitButton();
    }
    return;
  }

  // ── Upload + detect ───────────────────────────────────────────────────────
  setLoading(btn, true, "Uploading");
  try {
    const source = getSourceType();
    showSubmitStatus("info", "Uploading submission…");
    let importData;
    if      (source === "local")   importData = await uploadLocalFiles();
    else if (source === "zenodo") { showSubmitStatus("info", "Importing from Zenodo…");  importData = await importZenodo();  }
    else if (source === "github") { showSubmitStatus("info", "Importing from GitHub…");  importData = await importGithub();  }

    state.pendingLocalFiles = null;
    clearSubmitStatus();

    _updateSessionChip();

    if (importData.batch === true) {
      // ── Multi-submission batch ─────────────────────────────────────────
      batchState.isBatch    = true;
      batchState.uploadData = importData;
      batchState.selectedIds = new Set(importData.submissions.map((s) => s.submission_id));
      batchState.batchId    = null;
      batchState.validationData = null;

      const desc = el("batch-header-desc");
      if (desc) {
        const count = importData.submission_count;
        desc.textContent = `${count} submission${count !== 1 ? "s" : ""} detected.`;
      }

      renderBatchTable(importData.submissions);
      unlockStep("index");
      goToStep("index");
    } else {
      // ── Single submission — normalize to index step ────────────────────
      batchState.isBatch = false;
      state.submissionId = importData.submission_id;
      state.detection = {
        nifti_count: Number.isFinite(Number(importData.nifti_count)) ? Number(importData.nifti_count) : null,
        detected_parameter_map_type: importData.detected_parameter_map_type || "Unknown",
      };

      const fakeUpload = {
        batch: false,
        original_filename: importData.original_filename || importData.submission_id,
        submission_count: 1,
        source_type: source,
        submissions: [{
          submission_id: importData.submission_id,
          display_name: cleanSubmissionName(importData.original_filename || importData.submission_id),
          nifti_count:   importData.nifti_count ?? null,
          detected_parameter_map_type: importData.detected_parameter_map_type || "Unknown",
          has_run_instructions: importData.has_run_instructions ?? importData.has_dockerfile ?? null,
          has_result_maps: importData.has_result_maps ?? (Number(importData.nifti_count || 0) > 0),
          source_folder: importData.source_folder || null,
          detection_warning: importData.detection_warning || null,
          status: "ready",
        }],
      };

      batchState.uploadData = fakeUpload;
      batchState.selectedIds = new Set([importData.submission_id]);

      const desc = el("batch-header-desc");
      if (desc) {
        desc.textContent = "1 submission detected.";
      }

      renderBatchTable(fakeUpload.submissions);
      unlockStep("index");
      goToStep("index");
    }
  } catch (err) {
    showSubmitStatus("error", err.message || "Upload failed. Is the server running?");
  } finally {
    requestInProgress = false;
    setLoading(btn, false, submitLabel());
    _syncUploadSubmitButton();
  }
}

// ── Step 2: Review detected submissions ───────────────────────────────────────

function _indexSubmissionTypeValue(sub) {
  const info = submissionTypeInfo(sub);
  if (info.state === "skipped" || sub.run_readiness === "result_only") return "result-only";
  if (hasRunInstructions(sub)) return "runnable";
  return "unknown";
}

function _indexSubmissionStatusValue(sub) {
  const info = submissionTypeInfo(sub);
  if (sub.status === "failed" || sub.detection_warning || info.state === "warning") return "needs-attention";
  if (!sub.challenge_type || (!sub.detected_parameter_map_type && !sub.map_type)) return "missing-details";
  return "ready";
}

function _renderIndexFilterBar(submissions) {
  const mapOptions = ["all", ...new Set((submissions || [])
    .map((s) => s.detected_parameter_map_type || s.map_type)
    .filter(Boolean)
    .map(String))].map((value) => ({ value, label: value === "all" ? "All" : value }));
  return `<div class="filter-bar review-filter-bar" id="index-filter-bar">
    ${_renderSearchBox("index-search", _indexFilter.search, "Search submissions")}
    ${_renderFilterDropdown("index-challenge", "Challenge", _indexFilter.challenge, [
      { value: "all", label: "All" },
      { value: "asl", label: "ASL" },
      { value: "dce", label: "DCE" },
      { value: "dsc", label: "DSC" },
      { value: "other", label: "Other" },
    ])}
    ${_renderFilterDropdown("index-type", "Type", _indexFilter.type, [
      { value: "all", label: "All" },
      { value: "runnable", label: "Runnable" },
      { value: "result-only", label: "Result-only" },
      { value: "unknown", label: "Needs review" },
    ])}
    ${_renderFilterDropdown("index-status", "Status", _indexFilter.status, [
      { value: "all", label: "All" },
      { value: "ready", label: "Ready" },
      { value: "needs-attention", label: "Needs attention" },
      { value: "missing-details", label: "Missing details" },
    ])}
    ${_renderFilterDropdown("index-map", "Map", _indexFilter.map, mapOptions)}
    <button type="button" class="filter-clear-btn" id="index-clear-filters">Clear filters</button>
  </div>`;
}

function _filterIndexSubmissions(submissions) {
  const q = (_indexFilter.search || "").trim().toLowerCase();
  return (submissions || []).filter((sub) => {
    const challenge = String(sub.challenge_type || getChallengeType() || "other").toLowerCase();
    const mapType = String(sub.detected_parameter_map_type || sub.map_type || "").toLowerCase();
    const type = _indexSubmissionTypeValue(sub);
    const status = _indexSubmissionStatusValue(sub);
    const text = [
      sub.submission_id,
      sub.display_name,
      sub.name,
      sub.source_folder,
      sub.original_filename,
      mapType,
      challenge,
    ].filter(Boolean).join(" ").toLowerCase();
    if (q && !text.includes(q)) return false;
    if (_indexFilter.challenge !== "all" && challenge !== _indexFilter.challenge) return false;
    if (_indexFilter.type !== "all" && type !== _indexFilter.type) return false;
    if (_indexFilter.status !== "all" && status !== _indexFilter.status) return false;
    if (_indexFilter.map !== "all" && mapType !== String(_indexFilter.map).toLowerCase()) return false;
    return true;
  });
}

function _wireIndexFilterBar() {
  const search = el("index-search");
  if (search) {
    search.oninput = () => {
      _indexFilter.search = search.value;
      if (batchState.uploadData) renderBatchTable(batchState.uploadData.submissions || []);
    };
  }
  const clear = el("index-clear-filters");
  if (clear) {
    clear.onclick = () => {
      _indexFilter.search = "";
      _indexFilter.challenge = "all";
      _indexFilter.type = "all";
      _indexFilter.status = "all";
      _indexFilter.map = "all";
      if (batchState.uploadData) renderBatchTable(batchState.uploadData.submissions || []);
    };
  }
}

// Render detected submissions as clean cards. Selection state and action-row
// validation both work through batchState.selectedIds.
function renderBatchTable(submissions) {
  const wrap = el("batch-table-wrap");
  if (!wrap) return;
  wrap.innerHTML = "";

  const safeSubmissions = submissions || [];
  const isSingle = safeSubmissions.length <= 1;
  if (isSingle && safeSubmissions[0]?.submission_id) {
    batchState.selectedIds = new Set([safeSubmissions[0].submission_id]);
  }

  const title = el("batch-index-title");
  if (title) title.textContent = isSingle ? "Review Detected Submission" : "Review Detected Submissions";
  const desc = el("batch-header-desc");
  if (desc && safeSubmissions.length > 0) {
    desc.textContent = `${safeSubmissions.length} submission${safeSubmissions.length !== 1 ? "s" : ""} detected.`;
  }
  const hintCopy = el("index-hint-copy");
  if (hintCopy) {
    hintCopy.textContent = safeSubmissions.length <= 1
      ? "Review detected submission."
      : "Select submissions to validate.";
  }

  const controls = document.querySelector("#step-index .batch-controls");
  const controlsLeft = controls?.querySelector(".batch-controls-left");
  const controlsRight = controls?.querySelector(".batch-controls-right");
  if (controls) controls.style.display = isSingle ? "none" : "";
  if (controlsLeft) controlsLeft.style.display = isSingle ? "none" : "";
  if (controlsRight) controlsRight.style.display = "none";

  const visibleSubmissions = isSingle ? safeSubmissions : _filterIndexSubmissions(safeSubmissions);

  if (!isSingle) {
    wrap.insertAdjacentHTML("beforeend", _renderIndexFilterBar(safeSubmissions));
    _wireIndexFilterBar();
  }

  const list = document.createElement("div");
  list.className = "sub-card-list" + (isSingle ? " sub-card-list--single" : "");

  visibleSubmissions.forEach((sub, idx) => {
    const isSelected = batchState.selectedIds.has(sub.submission_id);
    const typeInfo = submissionTypeInfo(sub);
    const statusState = sub.status === "failed" ? "error"
      : typeInfo.state === "warning" || sub.detection_warning ? "warning"
      : sub.status === "passed" ? "complete"
      : "ready";
    const statusLabel = sub.status === "failed" ? "Needs attention"
      : sub.status === "passed" ? "Validated"
      : statusState === "warning" ? "Needs attention"
      : "Ready";

    const card = document.createElement("div");
    card.className = "sub-card guided-sub-card"
      + (isSingle ? " sub-card--single" : "")
      + (isSelected ? " is-selected" : "");
    card.dataset.subCard = sub.submission_id;

    const safeMapLabel = escapeHtml(sub.detected_parameter_map_type || sub.map_type || "Not detected");
    const safeName = escapeHtml(submissionDisplayName(sub, `Submission ${idx + 1}`));
    const safeChallenge = escapeHtml(challengeLabel(sub.challenge_type));
    const safeType = escapeHtml(typeInfo.label);
    const subTypeHelp = typeInfo.state === "skipped"
      ? "This means the submission already includes output maps, so Docker execution may be skipped."
      : "Submission type indicates whether output maps are provided or code must be run.";
    const niftiCount   = sub.nifti_count ?? "—";
    const mapChip = sub.detected_parameter_map_type || sub.map_type || "Map not detected";
    const readinessChip = _indexSubmissionTypeValue(sub) === "result-only" ? "Result maps provided"
      : _indexSubmissionTypeValue(sub) === "runnable" ? "Runnable" : "Needs review";

    card.innerHTML = `
      ${isSingle ? "" : `<input type="checkbox" class="sub-card-check" data-id="${escapeHtml(sub.submission_id)}" ${isSelected ? "checked" : ""} aria-label="Select ${safeName}" />`}
      <div class="sub-card-body">
        <div class="sub-card-top">
          <span class="sub-card-name" title="${safeName}">${safeName}</span>
          ${statusPill(statusLabel, statusState)}
        </div>
        <div class="sub-card-tags">
          <span class="sub-tag">${safeChallenge}</span>
          <span class="sub-tag">${escapeHtml(mapChip)}</span>
          <span class="sub-tag ${_indexSubmissionTypeValue(sub) === "runnable" ? "sub-tag--repro" : "sub-tag--result"}">${escapeHtml(readinessChip)}</span>
        </div>
        <div class="sub-card-fields">
          <div class="sub-field">
            <span class="sub-field-label">Challenge type ${helpTooltip("Select the OSIPI challenge type for this submission.", "Challenge type help")}</span>
            <span class="sub-field-value">${safeChallenge}</span>
          </div>
          <div class="sub-field">
            <span class="sub-field-label">Map type ${helpTooltip("Optional. The app can auto-detect CBF, ATT, Ktrans, ve, vp, or Kep from filenames and metadata.", "Parameter map type help")}</span>
            <span class="sub-field-value">${safeMapLabel}</span>
          </div>
          <div class="sub-field">
            <span class="sub-field-label">Submission type ${helpTooltip(subTypeHelp, typeInfo.state === "skipped" ? "Result maps provided help" : "Submission type help")}</span>
            <span class="sub-field-value">${safeType}</span>
          </div>
          <div class="sub-field">
            <span class="sub-field-label">NIfTI count</span>
            <span class="sub-field-value">${escapeHtml(niftiCount)}</span>
          </div>
        </div>
      </div>
    `;

    const cb = card.querySelector("input.sub-card-check");
    const setSelected = (on) => {
      if (cb) cb.checked = on;
      if (on) batchState.selectedIds.add(sub.submission_id);
      else    batchState.selectedIds.delete(sub.submission_id);
      card.classList.toggle("is-selected", on);
      _syncBatchHeaderCheckbox();
      _syncBatchValidateBtn();
      saveSessionState();
      _refreshWizardFooter();
    };
    if (cb) cb.addEventListener("change", () => setSelected(cb.checked));
    // Clicking anywhere on the card toggles selection, except on a tooltip.
    card.addEventListener("click", (e) => {
      if (isSingle) return;
      if (e.target.closest(".help-tooltip")) return;
      if (e.target === cb) return;  // checkbox handles its own change
      setSelected(!cb?.checked);
    });

    list.appendChild(card);
  });

  wrap.appendChild(list);
  if (!visibleSubmissions.length) {
    wrap.insertAdjacentHTML("beforeend", `<div class="list-empty-state">
      <p>No submissions match these filters.</p>
      <button type="button" class="btn btn-secondary btn-sm" id="index-empty-clear">Clear filters</button>
    </div>`);
    const emptyClear = el("index-empty-clear");
    if (emptyClear) emptyClear.onclick = () => {
      _indexFilter.search = "";
      _indexFilter.challenge = "all";
      _indexFilter.type = "all";
      _indexFilter.status = "all";
      _indexFilter.map = "all";
      renderBatchTable(safeSubmissions);
    };
  }
  _syncBatchValidateBtn();
  _refreshWizardFooter();
}

function _syncBatchHeaderCheckbox() {
  const checkAll = document.querySelector("#batch-check-all");
  if (!checkAll || !batchState.uploadData) return;
  checkAll.checked = batchState.selectedIds.size === batchState.uploadData.submissions.length;
}

function _syncBatchValidateBtn() {
  const btn = el("batch-validate-selected-btn");
  if (!btn) return;
  btn.disabled = batchState.selectedIds.size === 0;
}

// Batch index controls
const batchSelectAllBtn   = el("batch-select-all-btn");
const batchDeselectAllBtn = el("batch-deselect-all-btn");
const batchValSelBtn      = el("batch-validate-selected-btn");
const batchValAllBtn      = el("batch-validate-all-btn");

if (batchSelectAllBtn) {
  batchSelectAllBtn.addEventListener("click", () => {
    if (!batchState.uploadData) return;
    batchState.uploadData.submissions.forEach((s) => batchState.selectedIds.add(s.submission_id));
    renderBatchTable(batchState.uploadData.submissions);
  });
}

if (batchDeselectAllBtn) {
  batchDeselectAllBtn.addEventListener("click", () => {
    batchState.selectedIds.clear();
    if (batchState.uploadData) renderBatchTable(batchState.uploadData.submissions);
  });
}

if (batchValSelBtn) {
  batchValSelBtn.addEventListener("click", () => runBatchValidation([...batchState.selectedIds]));
}

if (batchValAllBtn) {
  batchValAllBtn.addEventListener("click", () => {
    if (!batchState.uploadData) return;
    runBatchValidation(batchState.uploadData.submissions.map((s) => s.submission_id));
  });
}

const batchNewBtn = el("batch-new-btn");
if (batchNewBtn) {
  batchNewBtn.addEventListener("click", () => { resetAll(); syncSubmitLabel(); goToStep("upload"); });
}

// ── Step 2→3: Validate ────────────────────────────────────────────────────────

async function runBatchValidation(submissionIds) {
  if (!submissionIds.length) return;
  const statusEl = el("batch-validate-status");
  const actionNext = wf.step === "index" ? _stepActionPrimary("index") : null;
  const actionLabel = actionNext ? actionNext.textContent.trim() : "Validate Submission";

  const disableBtns = (v) => {
    [batchValSelBtn, batchValAllBtn, batchSelectAllBtn, batchDeselectAllBtn].forEach((b) => {
      if (b) b.disabled = v;
    });
  };
  disableBtns(true);
  if (actionNext) setLoading(actionNext, true, "Validating");
  if (statusEl) {
    statusEl.style.display = "block";
    statusEl.className = "submit-status status-info";
    statusEl.textContent = `Validating ${submissionIds.length} submission${submissionIds.length !== 1 ? "s" : ""}…`;
  }

  try {
    // ── Single submission path ────────────────────────────────────────────
    if (!batchState.isBatch && submissionIds.length === 1) {
      // Run single validate then normalize
      const singleResult = await runValidation();
      state.validationResult = singleResult;
      if (statusEl) statusEl.style.display = "none";
      _renderSingleAsValidate(singleResult);
      return;
    }

    // ── Batch path ────────────────────────────────────────────────────────
    const sharedTeam  = getTeamName();
    const sharedEmail = getEmail();
    const teamNamesMap  = sharedTeam  ? Object.fromEntries(submissionIds.map((id) => [id, sharedTeam]))  : null;
    const emailsMap     = sharedEmail ? Object.fromEntries(submissionIds.map((id) => [id, sharedEmail])) : null;

    const res  = await fetch(`${API}/api/validate-batch`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        submission_ids:  submissionIds,
        challenge_type:  getChallengeType(),
        map_type:        getMapType(),
        map_type_mode:   getMapTypeMode(),
        team_names:      teamNamesMap,
        contact_emails:  emailsMap,
        mode:            "auto",
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Batch validation failed.");

    batchState.batchId        = data.batch_id;
    batchState.validationData = data;

    // Update status badges in index table
    if (batchState.uploadData) {
      const resultMap = {};
      (data.results || []).forEach((r) => { resultMap[r.submission_id] = r; });
      batchState.uploadData.submissions.forEach((s) => {
        const r = resultMap[s.submission_id];
        if (r) s.status = r.passed ? "passed" : "failed";
      });
      renderBatchTable(batchState.uploadData.submissions);
    }

    if (statusEl) statusEl.style.display = "none";
    renderValidateStep(data);
    saveSessionState();
  } catch (err) {
    if (statusEl) {
      statusEl.style.display = "block";
      statusEl.className = "submit-status status-error";
      statusEl.textContent = err.message || "Validation failed.";
    }
  } finally {
    if (actionNext) setLoading(actionNext, false, actionLabel);
    disableBtns(false);
    _syncBatchValidateBtn();
    _refreshWizardFooter();
  }
}

// ── Single result: normalize to batch-of-1 and render validate step ───────────

function _renderSingleAsValidate(data) {
  // Build a pseudo-batch response from a single validation result
  const errCount  = (data.errors   || []).length;
  const warnCount = (data.warnings || []).length;
  const normalized = {
    results: [{
      submission_id:        data.submission_id || state.submissionId,
      passed:               data.passed,
      errors:               data.errors  || [],
      warnings:             data.warnings || [],
      nifti_count:          data.nifti_count ?? state.detection.nifti_count,
      map_type:             data.map_type || state.detection.detected_parameter_map_type,
      has_run_instructions: data.has_run_instructions ?? data.has_dockerfile ?? null,
      source_folder:        data.source_folder || null,
      challenge_type:       data.challenge_type || getChallengeType(),
      team_name:            data.team_name || getTeamName() || null,
      contact_email:        data.contact_email || getEmail() || null,
      validated_at:         data.validated_at || data.checked_at || null,
    }],
    batch_id:    null,
    passed_count:  data.passed ? 1 : 0,
    failed_count:  data.passed ? 0 : 1,
    validated_at:  data.validated_at || data.checked_at || null,
  };
  batchState.batchId        = null;
  batchState.validationData = normalized;
  renderValidateStep(normalized, /*single=*/true);
  saveSessionState();
}

// ── Step 3: Validate — render validation cards ────────────────────────────────

const _reviewFilter = { filter: "all", search: "", sort: "status", showAll: false };

// ── Issue summary helper — one brief text (not a list) ───────────────────────
function _issueSummary(errors, warnings) {
  function trunc(s, n) { return s.length > n ? s.slice(0, n - 1) + "…" : s; }
  if (errors.length === 0 && warnings.length === 0)
    return { html: `<span class="vr-issue-ok">No issues</span>` };
  if (errors.length > 0) {
    const main = errors.length === 1 ? trunc(errors[0], 32) : `${errors.length} errors`;
    const extra = warnings.length > 0 ? ` · ${warnings.length}w` : "";
    return { html: `<span class="vr-issue-err" title="${escapeHtml(errors.join("; "))}">${escapeHtml(main + extra)}</span>` };
  }
  const main = warnings.length === 1 ? trunc(warnings[0], 32) : `${warnings.length} warnings`;
  return { html: `<span class="vr-issue-warn" title="${escapeHtml(warnings.join("; "))}">${escapeHtml(main)}</span>` };
}

function _validationIssueDetails(r) {
  const rNiftiCount = Number(r.nifti_count ?? 0);
  const withoutSpuriousNoOutput = (msgs) =>
    rNiftiCount > 0 ? msgs.filter((m) => m !== "No output files found") : msgs;

  const errors = withoutSpuriousNoOutput(dedupeMessages((r.errors || []).map(simplifyMessage)));
  const warnings = withoutSpuriousNoOutput(dedupeMessages((r.warnings || []).map(simplifyMessage)))
    .filter((w) => !errors.some((e) => e.toLowerCase() === w.toLowerCase()));
  const checks = buildSuccessChecks(r, errors.length, warnings.length, r.map_type);
  return { errors, warnings, checks, niftiCount: rNiftiCount };
}

function _validationReason(errors, warnings) {
  if (errors.length > 0) return errors[0];
  if (warnings.length > 0) return warnings[0];
  return "No issues found.";
}

function _renderValidationFilterBar() {
  return `<div class="filter-bar validation-filter-bar">
    ${_renderSearchBox("batch-search", _reviewFilter.search, "Search validation results")}
    ${_renderChipGroup("validation-status", _reviewFilter.filter, [
      { value: "all", label: "All" },
      { value: "passed", label: "Passed" },
      { value: "warnings", label: "Warnings" },
      { value: "errors", label: "Errors" },
      { value: "runnable", label: "Runnable" },
      { value: "result-only", label: "Result-only" },
    ])}
    ${_renderFilterDropdown("validation-sort", "Sort", _reviewFilter.sort, [
      { value: "status", label: "Status" },
      { value: "name", label: "Name A-Z" },
      { value: "errors", label: "Most errors" },
      { value: "warnings", label: "Warnings" },
      { value: "runnable", label: "Runnable first" },
    ])}
    <div class="filter-bar-spacer"></div>
    <button type="button" id="batch-expand-all" class="filter-text-btn">Expand all</button>
    <button type="button" id="batch-collapse-all" class="filter-text-btn">Collapse all</button>
  </div>`;
}

function _wireValidationFilterBar() {
  const searchEl = el("batch-search");
  if (searchEl) {
    searchEl.oninput = () => {
      _reviewFilter.search = searchEl.value;
      _reviewFilter.showAll = false;
      _applyReviewFilters();
    };
  }
}

function _refreshValidationFilterBar() {
  const toolbar = el("val-toolbar");
  if (!toolbar || toolbar.style.display === "none") return;
  toolbar.innerHTML = _renderValidationFilterBar();
  _wireValidationFilterBar();
}

function renderValidateStep(data, isSingleMode) {
  const results = data.results || [];
  const single  = isSingleMode === true || !batchState.isBatch;
  const issueDetails = new Map(results.map((r) => [r.submission_id, _validationIssueDetails(r)]));
  const checkedCount = results.length;
  const passedCount = results.filter((r) => r.passed).length;
  const totalWarnings = results.reduce((sum, r) => sum + (issueDetails.get(r.submission_id)?.warnings.length || 0), 0);
  const totalErrors = results.reduce((sum, r) => sum + (issueDetails.get(r.submission_id)?.errors.length || 0), 0);
  const validationTitle = totalErrors > 0
    ? "Validation failed"
    : totalWarnings > 0 ? "Validation needs attention" : "Validation complete";

  // Pre-compute counts for summary and action readiness.
  const runnableCount    = results.filter((r) => r.run_readiness === "runnable"
                                               || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile))).length;
  const resultOnlyCount  = results.filter((r) => r.run_readiness === "result_only"
                                               || (r.passed && !r.has_run_instructions && (r.nifti_count > 0 || r.has_result_maps))).length;
  const needsReviewCount = results.filter((r) => !r.passed || (r.warnings || []).length > 0).length;

  // Reset filter state
  _reviewFilter.filter  = "all";
  _reviewFilter.search  = "";
  _reviewFilter.sort    = "status";
  _reviewFilter.showAll = false;
  const valToolbar = el("val-toolbar");
  if (valToolbar) valToolbar.innerHTML = _renderValidationFilterBar();
  const searchEl  = el("batch-search");
  if (searchEl) searchEl.value = "";

  // ── 1. One-line summary in step header desc ───────────────────────────────
  const titleEl = el("validate-card-title");
  if (titleEl) titleEl.textContent = validationTitle;

  const statsEl = el("validate-summary-stats");
  if (statsEl) {
    statsEl.innerHTML = `
      <div class="validation-stat"><span>${checkedCount}</span><small>Checked</small></div>
      <div class="validation-stat"><span>${passedCount}</span><small>Passed</small></div>
      <div class="validation-stat ${totalWarnings > 0 ? "is-warning" : ""}"><span>${totalWarnings}</span><small>Warnings</small></div>
      <div class="validation-stat ${totalErrors > 0 ? "is-error" : ""}"><span>${totalErrors}</span><small>Errors</small></div>
    `;
  }

  const desc = el("batch-results-desc");
  if (desc) {
    const parts = [`${checkedCount} checked`];
    if (totalErrors > 0) parts.push(`${totalErrors} error${totalErrors !== 1 ? "s" : ""}`);
    if (totalWarnings > 0) parts.push(`${totalWarnings} warning${totalWarnings !== 1 ? "s" : ""}`);
    if (runnableCount > 0) parts.push(`${runnableCount} ready to run`);
    if (resultOnlyCount > 0) parts.push(`${resultOnlyCount} result-only`);
    if (needsReviewCount > 0 && totalErrors === 0 && totalWarnings === 0) parts.push(`${needsReviewCount} need review`);
    desc.textContent = parts.join(" · ");
  }

  // ── 2. Filter/search handlers ─────────────────────────────────────────────
  _wireValidationFilterBar();

  // ── Toolbar visibility: hide for single submission (no search/filter needed)
  if (valToolbar) valToolbar.style.display = results.length <= 1 ? "none" : "";

  // ── 3. Show-all button ────────────────────────────────────────────────────
  const showAllBtn = el("review-show-all-btn");
  if (showAllBtn) {
    showAllBtn.onclick = () => {
      _reviewFilter.showAll = !_reviewFilter.showAll;
      _applyReviewFilters();
    };
  }

  // ── 4. Review table rows ──────────────────────────────────────────────────
  const list = el("batch-submissions-list");
  if (list) {
    list.innerHTML = "";
    results.forEach((r, idx) => {
      const { errors, warnings, checks, niftiCount: rNiftiCount } = issueDetails.get(r.submission_id) || _validationIssueDetails(r);
      const passed   = r.passed;
      const hasWarn  = warnings.length > 0;
      const hasRunInstructions = !!(r.has_run_instructions ?? r.has_dockerfile);
      const runReadiness = r.run_readiness
        || (passed && hasRunInstructions ? "runnable"
            : passed && !hasRunInstructions && ((r.nifti_count || 0) > 0 || r.has_result_maps) ? "result_only"
            : passed && !hasRunInstructions ? "result_only"   // passed w/ no code = result-only
            : "not_runnable");
      const runnable = runReadiness === "runnable";
      const isResultOnly = runReadiness === "result_only";

      let valStatus, pillState, pillText;
      if (!passed)      { valStatus = "failed";  pillState = "error";    pillText = "Error"; }
      else if (hasWarn) { valStatus = "warning"; pillState = "warning";  pillText = "Warning"; }
      else              { valStatus = "passed";  pillState = "complete"; pillText = "Complete"; }

      let runTxt, runState;
      if (!passed)        { runTxt = "Cannot run"; runState = "error"; }
      else if (isResultOnly) { runTxt = "Result maps provided"; runState = "skipped"; }
      else if (runnable)  { runTxt = "Ready to run"; runState = "ready"; }
      else                { runTxt = "Cannot run"; runState = "warning"; }

      const execInitStatus = runnable ? "not-run" : "cannot-run";

      const safeSubId     = escapeHtml(r.submission_id);
      const safeName      = escapeHtml(submissionDisplayName(r, `Submission ${idx + 1}`));
      const safeChallenge = escapeHtml(r.challenge_type || getChallengeType() || "dce");
      const safeMap       = escapeHtml(r.map_type || "Not detected");
      const reason        = escapeHtml(_validationReason(errors, warnings));
      const subType       = submissionTypeInfo(r);
      const subTypeHelp   = isResultOnly
        ? "This means the submission already includes output maps, so Docker execution may be skipped."
        : "Submission type indicates whether output maps are provided or code must be run.";

      // Detail content
      const niftiLine = rNiftiCount > 0
        ? `<div class="vr-detail-nifti">NIfTI files: <strong>${rNiftiCount}</strong></div>` : "";
      const errHtml  = errors.length > 0
        ? `<div class="vp-section error-section" style="margin-top:8px">
             <div class="vp-section-heading">Errors</div>
             <ul class="issue-list">${errors.map((m) => `<li class="is-error">✕ ${escapeHtml(m)}</li>`).join("")}</ul>
           </div>` : "";
      const warnHtml = warnings.length > 0
        ? `<div class="vp-section warn-section" style="margin-top:8px">
             <div class="vp-section-heading">Warnings</div>
             <ul class="issue-list">${warnings.map((m) => `<li class="is-warning">! ${escapeHtml(m)}</li>`).join("")}</ul>
           </div>` : "";
      const techHtml = checks.length > 0
        ? `<details class="tech-checks-toggle" style="margin-top:8px">
             <summary>Technical checks (${checks.length} passed)</summary>
             <ul class="issue-list" style="margin-top:6px">${checks.map((m) => `<li class="is-pass">✓ ${escapeHtml(m)}</li>`).join("")}</ul>
           </details>` : "";
      const resultOnlyNote = isResultOnly
        ? `<p class="vr-result-only-note">This submission already includes output maps. Code execution is not needed.</p>`
        : "";
      const noIssueHtml = (!errHtml && !warnHtml)
        ? `<p style="font-size:0.73rem;color:var(--subtle);margin:0">No errors or warnings.</p>` : "";

      // No inline exec section on Validate — execution happens in Run step only
      const execHtml = "";

	      const wrap = document.createElement("div");
	      wrap.className = "br-row-wrap validation-card";
	      wrap.dataset.valStatus  = valStatus;
	      wrap.dataset.runnable   = String(runnable);
	      wrap.dataset.execStatus = execInitStatus;
	      wrap.dataset.resultOnly = String(isResultOnly);
	      wrap.dataset.subId      = r.submission_id;
      wrap.dataset.name       = (r.submission_id + " " + (r.source_folder || "") + " " + safeName).toLowerCase();
      wrap.dataset.errCount   = String(errors.length);
      wrap.dataset.warnCount  = String(warnings.length);

      wrap.innerHTML = `
        <div class="validation-card-main">
          <div class="validation-card-heading">
            <div class="validation-card-title">${safeName}</div>
            ${statusPill(pillText, pillState)}
          </div>
          <p class="validation-card-reason">${reason}</p>
          <div class="validation-card-meta">
            <span>Challenge: ${safeChallenge}</span>
            <span>Map: ${safeMap}</span>
            <span>NIfTI: ${escapeHtml(rNiftiCount)}</span>
            <span class="validation-meta-with-help">${escapeHtml(subType.label)} ${helpTooltip(subTypeHelp, isResultOnly ? "Result maps provided help" : "Submission type help")}</span>
            <span class="validation-meta-with-help">${statusPill(runTxt, runState)} ${helpTooltip("Runnable submissions include executable code. Result-only submissions skip execution and go directly to scoring.", "Run readiness help")}</span>
            <span class="br-badge badge-exec-none val-card-exec-badge" style="display:none;font-size:0.65rem"></span>
          </div>
          <div class="validation-card-actions">
            <button type="button" class="btn btn-ghost vr-action-btn vr-details-btn" aria-expanded="false">Details</button>
          </div>
        </div>
        <div class="vr-row-detail" style="display:none">
          <div class="validation-detail-inner">
            ${resultOnlyNote}${niftiLine}${noIssueHtml}${errHtml}${warnHtml}${techHtml}${execHtml}
            <details class="validation-technical-detail">
              <summary>Technical reference</summary>
              <p>Submission ID: ${safeSubId}</p>
            </details>
          </div>
        </div>`;

      list.appendChild(wrap);
    });

    _applyReviewFilters();
  }

  // ── 5. Unlock nav steps + navigate ───────────────────────────────────────
  unlockStep("validate");
  unlockStep("run");
  unlockStep("export");

  const banner = el("validate-completion-banner");
  if (banner) banner.style.display = "none";
  _refreshWizardFooter();

  const singleActions = el("single-result-actions");
  if (singleActions) singleActions.style.display = single ? "" : "none";

  const backBtn = el("batch-back-to-batch-btn");
  if (backBtn) backBtn.style.display = "none";

  // Show/hide "no runnable" message — only show when there are truly no valid submissions
  const noRunnableMsg = el("validate-no-runnable-msg");
  const hasAnyPassed  = runnableCount > 0 || resultOnlyCount > 0;
  if (noRunnableMsg)
    noRunnableMsg.style.display = !hasAnyPassed && results.length > 0 ? "" : "none";

  // Continue button — enabled for both runnable and result-only; label reflects destination
  const continueBtn = el("validate-continue-btn");
  if (continueBtn) {
    continueBtn.disabled  = !hasAnyPassed;
    continueBtn.textContent = runnableCount > 0
      ? "Continue to Run →"
      : resultOnlyCount > 0 ? "Continue →" : "Continue to Run →";
  }

  _syncExportStep();

  goToStep("validate");
}

// ── Review filter / sort / search ─────────────────────────────────────────────

function _applyReviewFilters() {
  const { filter, search, sort, showAll } = _reviewFilter;
  const list = el("batch-submissions-list");
  if (!list) return;

  const rows = [...list.querySelectorAll(".br-row-wrap")];

  const ORDER = { failed: 0, warning: 1, passed: 2 };
  rows.sort((a, b) => {
    if (sort === "name")     return (a.dataset.name || "").localeCompare(b.dataset.name || "");
    if (sort === "errors")   return (Number(b.dataset.errCount) || 0) - (Number(a.dataset.errCount) || 0);
    if (sort === "warnings") return (Number(b.dataset.warnCount) || 0) - (Number(a.dataset.warnCount) || 0);
    if (sort === "runnable") {
      const ra = a.dataset.runnable === "true" ? 0 : 1;
      const rb = b.dataset.runnable === "true" ? 0 : 1;
      return ra !== rb ? ra - rb : (ORDER[a.dataset.valStatus] ?? 3) - (ORDER[b.dataset.valStatus] ?? 3);
    }
    return (ORDER[a.dataset.valStatus] ?? 3) - (ORDER[b.dataset.valStatus] ?? 3);
  });
  rows.forEach((r) => list.appendChild(r));

  // Apply filter + search — collect matching rows
  const q = search.toLowerCase();
  const matchingRows = [];
  rows.forEach((row) => {
    const vs       = row.dataset.valStatus;
    const runnable = row.dataset.runnable === "true";
    const es       = row.dataset.execStatus;
    const name     = (row.dataset.name || "").toLowerCase();

    let show = true;
    switch (filter) {
      case "passed":       show = vs === "passed"; break;
      case "warnings":     show = vs === "warning"; break;
      case "errors":       show = vs === "failed"; break;
      case "runnable":     show = runnable && es === "not-run"; break;
      case "result-only":  show = row.dataset.resultOnly === "true"; break;
      case "needs-review": show = vs === "warning" || vs === "failed"; break;
      case "failed":       show = vs === "failed"; break;
      case "ready":        show = runnable && es === "not-run"; break;
      // legacy values kept for safety
      case "cannot-run":   show = !runnable; break;
      case "executed":     show = es === "passed" || es === "failed"; break;
      case "exec-failed":  show = es === "failed"; break;
      default:             show = true;
    }
    if (show && q) show = name.includes(q);

    if (show) matchingRows.push(row);
    else      row.style.display = "none";
  });

  // Show-first-5 / show-all logic
  const LIMIT = 5;
  const total = matchingRows.length;
  if (!showAll && total > LIMIT) {
    matchingRows.forEach((row, i) => { row.style.display = i < LIMIT ? "" : "none"; });
  } else {
    matchingRows.forEach((row) => { row.style.display = ""; });
  }

  // Update show-all button
  const showAllWrap = el("review-show-all-wrap");
  const showAllBtn  = el("review-show-all-btn");
  if (showAllWrap && showAllBtn) {
    if (total > LIMIT) {
      showAllWrap.style.display = "";
      showAllBtn.textContent = showAll
        ? "Show less"
        : `Show all ${total} submissions`;
    } else {
      showAllWrap.style.display = "none";
    }
  }

  const empty = el("batch-empty-state");
  if (empty) empty.style.display = total === 0 ? "" : "none";
}

function _syncFilterChips() {
  document.querySelectorAll('[data-filter-option="validation-status"]').forEach((c) => {
    c.classList.toggle("fc-active", c.dataset.filterValue === _reviewFilter.filter);
  });
}

// ── Row expand/collapse helpers ───────────────────────────────────────────────

function _toggleRowDetail(wrap, forceOpen) {
  // Support both v22 (.br-row-detail) and v23 (.vr-row-detail) drawer class names
  const detail = wrap.querySelector(".vr-row-detail") || wrap.querySelector(".br-row-detail");
  const toggleBtn = wrap.querySelector(".br-toggle-btn");
  if (!detail) return;
  const open = forceOpen !== undefined ? forceOpen : detail.style.display === "none";
  detail.style.display = open ? "" : "none";
  if (toggleBtn) {
    toggleBtn.textContent = open ? "▾" : "▸";
    toggleBtn.setAttribute("aria-expanded", String(open));
  }
  // Update .vr-details-btn label if present
  const detailsBtn = wrap.querySelector(".vr-details-btn");
  if (detailsBtn) {
    detailsBtn.textContent = open ? "Close" : "Details";
    detailsBtn.setAttribute("aria-expanded", String(open));
  }
}

// Row expand — legacy toggle button (.br-toggle-btn) click
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".br-toggle-btn");
  if (!btn) return;
  const wrap = btn.closest(".br-row-wrap");
  if (!wrap) return;
  e.stopPropagation();
  _toggleRowDetail(wrap);
});

// Row expand — Details button (.vr-details-btn) click
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".vr-details-btn");
  if (!btn) return;
  const wrap = btn.closest(".br-row-wrap");
  if (!wrap) return;
  e.stopPropagation();
  _toggleRowDetail(wrap);
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".br-run-btn");
  if (!btn) return;
  const wrap = btn.closest(".br-row-wrap");
  if (!wrap) return;
  e.stopPropagation();
  _toggleRowDetail(wrap, true);
  // Show exec section (hidden by default — only revealed when Run is clicked)
  const execSection = wrap.querySelector(".batch-exec-section");
  if (execSection) execSection.classList.add("exec-visible");
  const execBtn = wrap.querySelector(".batch-exec-btn");
  if (execBtn && !execBtn.disabled) execBtn.click();
});

(function initExpandCollapseAll() {
  const rows = () => document.querySelectorAll("#batch-submissions-list .br-row-wrap");
  document.addEventListener("click", (e) => {
    if (e.target.closest("#batch-expand-all"))      rows().forEach((w) => _toggleRowDetail(w, true));
    if (e.target.closest("#batch-collapse-all"))    rows().forEach((w) => _toggleRowDetail(w, false));
    if (e.target.closest("#batch-expand-failed"))   rows().forEach((w) => _toggleRowDetail(w, w.dataset.valStatus === "failed"));
    if (e.target.closest("#batch-expand-runnable")) rows().forEach((w) => _toggleRowDetail(w, w.dataset.runnable === "true"));
  });
})();

// ── Validate step action buttons ──────────────────────────────────────────────

const batchBackToBatchBtn = el("batch-back-to-batch-btn");
if (batchBackToBatchBtn) {
  batchBackToBatchBtn.addEventListener("click", () => goToStep("index"));
}

const validateContinueBtn = el("validate-continue-btn");
if (validateContinueBtn) {
  validateContinueBtn.addEventListener("click", () => {
    renderRunStep().catch(() => {});
    goToStep("run");
  });
}

const batchResultsNewBtn = el("batch-results-new-btn");
if (batchResultsNewBtn) {
  batchResultsNewBtn.addEventListener("click", () => { resetAll(); syncSubmitLabel(); goToStep("upload"); });
}

const editBtn = el("edit-btn");
if (editBtn) {
  editBtn.addEventListener("click", () => {
    state.mode = "edit";
    clearSubmitStatus();
    syncSubmitLabel();
    goToStep("upload");
  });
}

const replaceBtn = el("replace-btn");
if (replaceBtn) {
  replaceBtn.addEventListener("click", () => {
    state.mode = "replace";
    clearSubmissionData();
    const localRadio = document.querySelector("input[name='submission_type'][value='local']");
    if (localRadio) localRadio.checked = true;
    switchSource("local");
    syncSubmitLabel();
    goToStep("upload");
  });
}

const newBtn = el("new-btn");
if (newBtn) {
  newBtn.addEventListener("click", () => { resetAll(); syncSubmitLabel(); goToStep("upload"); });
}

// ── Step 4: Run ───────────────────────────────────────────────────────────────

const _runFilter = { view: "ready", showAll: false };

function _renderRunFilterBar() {
  return `<div class="filter-bar run-filter-bar">
    ${_renderChipGroup("run-status", _runFilter.view, [
      { value: "all", label: "All" },
      { value: "ready", label: "Ready to run" },
      { value: "cannot-run", label: "Cannot run" },
      { value: "complete", label: "Complete" },
      { value: "failed", label: "Failed" },
      { value: "skipped", label: "Skipped" },
    ])}
  </div>`;
}

function _refreshRunFilterBar() {
  const toolbar = el("run-toolbar");
  if (!toolbar || toolbar.style.display === "none") return;
  toolbar.innerHTML = _renderRunFilterBar();
}

// ── Run progress tracking ─────────────────────────────────────────────────────
const _runProgress = { total: 0, completed: 0, failed: 0, outputs: 0 };

function _initRunProgress(total) {
  _runProgress.total     = total;
  _runProgress.completed = 0;
  _runProgress.failed    = 0;
  _runProgress.outputs   = 0;
  const panel = el("run-progress-panel");
  if (panel) {
    panel.style.display = "";
    panel.className = "run-progress-panel";
  }
  _refreshRunProgress();
}

function _tickRunProgress(passed, outputCount) {
  _runProgress.completed++;
  if (!passed) _runProgress.failed++;
  _runProgress.outputs += (outputCount || 0);
  _refreshRunProgress();
}

function _refreshRunProgress() {
  const { total, completed, failed, outputs } = _runProgress;
  const pct   = total > 0 ? Math.round((completed / total) * 100) : 0;
  const fill  = el("run-prog-fill");
  const panel = el("run-progress-panel");
  const tEl   = el("rp-total");
  const cEl   = el("rp-completed");
  const fEl   = el("rp-failed");
  const oEl   = el("rp-outputs");
  const txt   = el("run-progress-text");
  const eta   = el("run-progress-eta");
  if (fill)  fill.style.width = `${pct}%`;
  if (tEl)   tEl.textContent  = String(total);
  if (cEl)   cEl.textContent  = String(completed);
  if (fEl)   fEl.textContent  = String(failed);
  if (oEl)   oEl.textContent  = String(outputs);

  if (completed >= total && total > 0) {
    if (panel) panel.classList.add("state-done");
    if (txt)   txt.textContent = "Execution complete";
    if (eta)   eta.textContent = "";

    // Count result-only and cannot-run from card list
    const allCards = [...document.querySelectorAll("#run-submissions-list .run-sub-card, #run-submissions-list .er-row-wrap")];
    const skipped  = allCards.filter((c) => c.dataset.execStatus === "result-only" || c.dataset.execStatus === "cannot-run").length;
    const passed   = completed - failed;
    const parts    = [];
    if (passed > 0)  parts.push(`${passed} passed`);
    if (failed > 0)  parts.push(`${failed} failed`);
    if (skipped > 0) parts.push(`${skipped} skipped`);

    // Show completion banner
    const bannerType = failed > 0 ? "warn" : "success";
    const bannerHtml = `<span class="scb-icon">${failed > 0 ? "⚠" : "✓"}</span>`
      + `<span class="scb-text">Execution complete — ${parts.join(" · ")}</span>`;
    _showCompletionBanner("run-completion-banner", bannerHtml, bannerType);

    // Unlock Score step and refresh local actions
    unlockStep("score");
    _refreshWizardFooter();
    renderScoreStep().catch(() => {});
  } else {
    if (txt) txt.textContent = `Running submissions… ${completed} of ${total}`;
    if (eta) eta.textContent = `${pct}%`;
  }
}

// Build/refresh the entire run step table from batchState.validationData
async function renderRunStep() {
  const results = batchState.validationData ? (batchState.validationData.results || []) : [];

  // Reset filter
  _runFilter.view    = "all";
  _runFilter.showAll = false;
  const viewSel = el("run-view-select");
  if (viewSel) viewSel.value = "all";

  // Count runnable and result-only
  const runnableResults  = results.filter((r) =>
    (r.run_readiness === "runnable") || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile)));
  // result_only: any submission with result maps (includes ASL results/maps/ structure)
  const resultOnlyResult = results.filter((r) =>
    (r.run_readiness === "result_only") || (r.passed && !r.has_run_instructions && ((r.nifti_count || 0) > 0 || r.has_result_maps)));
  const cannotRunResults = results.filter((r) => {
    const rr = r.run_readiness
      || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile) ? "runnable"
          : r.passed && !r.has_run_instructions && ((r.nifti_count || 0) > 0 || r.has_result_maps) ? "result_only"
          : "not_runnable");
    return rr === "not_runnable" || (!r.passed && rr !== "result_only");
  });
  const runnableCount   = runnableResults.length;
  const resultOnlyCount = resultOnlyResult.length;
  const cannotRunCount  = cannotRunResults.length;
  const allResultOnly   = runnableCount === 0 && resultOnlyCount > 0;

  // ── Skipped notice (all result-only) ──────────────────────────────────────
  const skippedNotice = el("run-skipped-notice");
  if (skippedNotice) skippedNotice.style.display = allResultOnly ? "" : "none";
  const list = el("run-submissions-list");
  if (list) list.style.display = allResultOnly ? "none" : "";
  const skippedContinueBtn = el("run-skipped-continue-btn");
  if (skippedContinueBtn) {
    skippedContinueBtn.style.display = "none";
    skippedContinueBtn.onclick = null;
  }

  // ── Settings card (hide when all result-only) ──────────────────────────────
  const settingsCard = el("run-settings-card");
  if (settingsCard) settingsCard.style.display = allResultOnly ? "none" : "";

  // ── Summary text ───────────────────────────────────────────────────────────
  const desc = el("run-results-desc");
  const cannotReasonEl = el("run-cannot-reason");
  if (desc) {
    if (results.length === 0) {
      desc.textContent = "No validated submissions. Complete the Validate step first.";
    } else if (allResultOnly) {
      desc.textContent = "Execution skipped.";
    } else if (runnableCount === 0 && cannotRunCount > 0) {
      desc.textContent = `${cannotRunCount} submission${cannotRunCount !== 1 ? "s" : ""} cannot be run.`;
      if (cannotReasonEl) {
        cannotReasonEl.textContent = "No runnable code or result maps were found in these submissions.";
        cannotReasonEl.style.display = "";
      }
    } else {
      const parts = [];
      if (runnableCount > 0) parts.push(`${runnableCount} ready to run`);
      if (resultOnlyCount > 0) parts.push(`${resultOnlyCount} result-only (skipped)`);
      if (cannotRunCount > 0)  parts.push(`${cannotRunCount} cannot run`);
      desc.textContent = parts.join(" · ");
      if (cannotReasonEl) cannotReasonEl.style.display = "none";
    }
  }

  if (allResultOnly) {
    if (list) list.innerHTML = "";
    ["batch-docker-banner", "run-toolbar", "run-empty-state", "run-show-all-wrap", "batch-exec-status", "run-progress-panel", "run-completion-banner"].forEach((id) => {
      const node = el(id);
      if (node) node.style.display = "none";
    });
    unlockStep("score");
    unlockStep("export");
    _refreshWizardFooter();
    saveSessionState();
    return;
  }

  // ── Docker availability ────────────────────────────────────────────────────
  const docker       = await checkDockerAvailability();
  const dockerBanner = el("batch-docker-banner");
  if (dockerBanner) {
    const cls  = docker.available ? "ok"  : "err";
    const dot  = `<span class="rsc-docker-dot"></span>`;
    const label = docker.available
      ? `${dot} Docker ready${docker.version ? ` · ${escapeHtml(docker.version)}` : ""}`
      : `${dot} Docker not available — run step disabled`;
    dockerBanner.innerHTML = `<span class="rsc-docker-badge ${cls}">${label}</span>`;
    dockerBanner.style.display = "";
  }

  // ── Run All button ─────────────────────────────────────────────────────────
  const runAllBtn = el("batch-exec-all-btn");
  if (runAllBtn)
    runAllBtn.style.display = batchState.isBatch && runnableCount > 0 && docker.available ? "" : "none";

  // ── Toolbar visibility ─────────────────────────────────────────────────────
  const toolbar = el("run-toolbar");
  if (toolbar) {
    toolbar.innerHTML = _renderRunFilterBar();
    toolbar.style.display = results.length > 1 && !allResultOnly ? "" : "none";
    if (list && toolbar.parentElement) toolbar.parentElement.insertBefore(toolbar, list);
  }

  const showAllBtn = el("run-show-all-btn");
  if (showAllBtn) {
    showAllBtn.onclick = () => {
      _runFilter.showAll = !_runFilter.showAll;
      _applyRunFilters();
    };
  }

  // ── Build submission cards ─────────────────────────────────────────────────
  if (!list) return;
  list.style.display = "";
  list.innerHTML = "";

  results.forEach((r) => {
    const runReadiness = r.run_readiness
      || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile) ? "runnable"
          : r.passed && !r.has_run_instructions && ((r.nifti_count || 0) > 0 || r.has_result_maps) ? "result_only"
          : "not_runnable");
    const runnable     = runReadiness === "runnable";
    const isResultOnly = runReadiness === "result_only";
    const safeSubId    = escapeHtml(r.submission_id);
    const chall        = r.challenge_type || getChallengeType() || "dce";

    let initExecStatus;
    if (runnable)          initExecStatus = "not-run";
    else if (isResultOnly) initExecStatus = "result-only";
    else                   initExecStatus = "cannot-run";

    const wrap = document.createElement("div");
    wrap.className         = "run-sub-card";
    wrap.dataset.subId     = r.submission_id;
    wrap.dataset.challenge = chall;
    wrap.dataset.runnable  = String(runnable);
    wrap.dataset.execStatus = initExecStatus;
    wrap.dataset.name      = r.submission_id.toLowerCase();

    // Status badge
    const statusHtml = _erRunStatusHtml(initExecStatus, runnable);

    // Plain-English reason line
    let reasonHtml = "";
    if (isResultOnly) {
      reasonHtml = `<span class="run-card-reason">Output maps already included — no Docker execution needed.</span>`;
    } else if (!runnable) {
      reasonHtml = `<span class="run-card-reason run-card-reason-warn">No runnable code or result maps were found in this submission.</span>`;
    }

    // Action buttons
      const actionsHtml = runnable
      ? `<div class="run-card-actions">
           <button type="button" class="btn btn-primary btn-sm er-run-btn"
                   data-sub-id="${safeSubId}"
                   data-challenge="${escapeHtml(chall)}">Run code in Docker</button>
           <button type="button" class="btn btn-ghost btn-sm er-detail-btn">Details</button>
         </div>`
      : `<div class="run-card-actions">
           <button type="button" class="btn btn-ghost btn-sm er-detail-btn">Details</button>
         </div>`;

	    const safeName = r.source_folder ? escapeHtml(r.source_folder) : safeSubId;
	    const fileCount = r.nifti_count ?? r.output_file_count ?? "—";

	    wrap.innerHTML = `
	      <div class="run-card-main">
	        <div class="run-card-info">
	          <div class="run-card-name" title="${safeSubId}">${safeName}</div>
          <div class="run-card-status-row">
            ${statusHtml}
            ${reasonHtml}
          </div>
	        </div>
	        <div class="run-card-right">
	          <div class="run-card-outputs er-outputs-cell"><span class="rs-na">${escapeHtml(fileCount)} file${Number(fileCount) === 1 ? "" : "s"}</span></div>
	          ${actionsHtml}
	        </div>
      </div>
      <div class="er-row-detail run-card-detail" style="display:none">
        ${isResultOnly
          ? `<p class="vr-result-only-note" style="margin:0">This submission already includes output maps. Code execution is not needed — the submitted maps will be used directly for scoring and export.</p>`
          : `<p class="vr-issue-ok" style="margin:0 0 8px">Run this submission to see execution details.</p>`
        }
      </div>`;

    list.appendChild(wrap);
  });

  _applyRunFilters();
  _refreshWizardFooter();
}

// Render the run-status cell content for a given execStatus + runnable
function _erRunStatusHtml(execStatus, runnable) {
  switch (execStatus) {
    case "not-run":     return runnable
                          ? `<span class="rs-badge rs-ready">Ready to run</span>`
                          : `<span class="rs-na">—</span>`;
    case "cannot-run":  return `<span class="rs-badge rs-cannot">Cannot run</span>`;
    case "result-only": return `<span class="rs-badge rs-skipped">Skipped</span>`;
    case "running":     return `<span class="rs-badge rs-running">Running…</span>`;
    case "passed":      return `<span class="rs-badge rs-pass">Passed</span>`;
    case "failed":      return `<span class="rs-badge rs-fail">Failed</span>`;
    case "timed-out":   return `<span class="rs-badge rs-timeout">Timed out</span>`;
    default:            return `<span class="rs-na">—</span>`;
  }
}

function _applyRunFilters() {
  const { view, showAll } = _runFilter;
  const list = el("run-submissions-list");
  if (!list) return;
  // Works for both old .er-row-wrap (legacy) and new .run-sub-card elements
  const rows = [...list.querySelectorAll(".er-row-wrap, .run-sub-card")];

  const matchingRows = [];
  rows.forEach((row) => {
    const es       = row.dataset.execStatus;
    const runnable = row.dataset.runnable === "true";
	    let show = true;
	    switch (view) {
	      case "ready":     show = (runnable && es === "not-run") || es === "result-only"; break;
	      case "cannot-run": show = es === "cannot-run"; break;
	      case "complete":  show = es === "passed"; break;
	      case "skipped":   show = es === "result-only"; break;
	      case "not-run":   show = es === "not-run"; break;
	      case "passed":    show = es === "passed"; break;
	      case "failed":    show = es === "failed" || es === "timed-out"; break;
	      case "timed-out": show = es === "timed-out"; break;
      default:          show = true; // "all"
    }
    if (show) matchingRows.push(row);
    else row.style.display = "none";
  });

  const LIMIT = 6;
  const total = matchingRows.length;
  if (!showAll && total > LIMIT) {
    matchingRows.forEach((row, i) => { row.style.display = i < LIMIT ? "" : "none"; });
  } else {
    matchingRows.forEach((row) => { row.style.display = ""; });
  }

  const showAllWrap = el("run-show-all-wrap");
  const showAllBtn  = el("run-show-all-btn");
  if (showAllWrap && showAllBtn) {
    if (total > LIMIT) {
      showAllWrap.style.display = "";
      showAllBtn.textContent = showAll ? "Show less" : `Show all ${total} submissions`;
    } else {
      showAllWrap.style.display = "none";
    }
  }

  const empty = el("run-empty-state");
  if (empty) empty.style.display = total === 0 ? "" : "none";
}

// Update a run card/row after execution completes
function _updateRunRow(subId, execData, isError) {
  const wrap = [...document.querySelectorAll("#run-submissions-list .er-row-wrap, #run-submissions-list .run-sub-card")]
    .find((w) => w.dataset.subId === subId);
  if (!wrap) return;

  let newExecStatus;
  if (isError) {
    newExecStatus = "failed";
  } else if (execData.timed_out) {
    newExecStatus = "timed-out";
  } else if (execData.passed) {
    newExecStatus = "passed";
  } else {
    newExecStatus = "failed";
  }
  wrap.dataset.execStatus = newExecStatus;

  // Persist execution summary (no logs — IDs and counts only)
  if (isError) {
    _execSummaries[subId] = { status: "failed", passed: false, outputFileCount: 0 };
  } else if (execData) {
    _execSummaries[subId] = {
      status:          newExecStatus,
      passed:          !!execData.passed,
      exitCode:        execData.exit_code ?? null,
      outputFileCount: execData.output_file_count ?? (Array.isArray(execData.output_files) ? execData.output_files.length : 0),
      executedAt:      execData.executed_at || execData.finished_at || null,
      timedOut:        !!execData.timed_out,
      buildFailed:     !!execData.build_failed,
    };
  }
  saveSessionState();

  const runnable = wrap.dataset.runnable === "true";

  // Update status badge — works for both card (.run-card-status-row) and legacy table (.er-run-status-cell)
  const statusCell = wrap.querySelector(".er-run-status-cell, .run-card-status-row");
  if (statusCell) {
    // Preserve reason text in card layout; only replace the badge portion
    const reasonEl = statusCell.querySelector(".run-card-reason, .run-card-reason-warn");
    const reasonHtml = reasonEl ? reasonEl.outerHTML : "";
    if (isError || newExecStatus === "failed") {
      const errMsg = typeof isError === "string" ? isError : (execData?.error_message || "Execution failed");
      statusCell.innerHTML = _erRunStatusHtml(newExecStatus, runnable)
        + `<span class="run-card-reason run-card-reason-warn">${escapeHtml(errMsg.slice(0, 120))}</span>`;
    } else {
      statusCell.innerHTML = _erRunStatusHtml(newExecStatus, runnable) + reasonHtml;
    }
  }

  // Update outputs (card: .run-card-outputs, table: .er-outputs-cell)
  const outputsCell = wrap.querySelector(".er-outputs-cell, .run-card-outputs");
  if (outputsCell && !isError) {
    const fc = execData.output_file_count ?? (Array.isArray(execData.output_files) ? execData.output_files.length : 0);
    outputsCell.innerHTML = fc > 0
      ? `<span class="vr-run-ok">${fc} file${fc !== 1 ? "s" : ""}</span>`
      : `<span class="vr-issue-warn">0 files</span>`;
  }

  // Update output-check cell (table only — card skips this column)
  const outCheckCell = wrap.querySelector(".er-outcheck-cell");
  if (outCheckCell) {
    if (isError || !execData.output_validation) {
      outCheckCell.innerHTML = `<span class="oc-skipped">Skipped</span>`;
    } else {
      const ov = execData.output_validation;
      outCheckCell.innerHTML = ov.passed
        ? `<span class="oc-valid">Valid</span>`
        : `<span class="oc-issues">${(ov.errors || []).length} issue${(ov.errors || []).length !== 1 ? "s" : ""}</span>`;
    }
  }

  // Populate the detail drawer and auto-open it
  const drawer = wrap.querySelector(".er-row-detail, .run-card-detail");
  if (drawer) {
    if (isError) {
      drawer.innerHTML = `<p class="vr-issue-err" style="margin:0">${escapeHtml(typeof isError === "string" ? isError : "Execution failed.")}</p>`;
    } else {
      drawer.innerHTML = `<details open><summary style="font-size:0.76rem;font-weight:600;cursor:pointer;color:var(--muted);margin-bottom:8px">Execution details</summary>${renderExecResult(execData)}</details>`;
    }
    drawer.style.display = "";
    // Update the Details button label since the drawer is now open
    const detailBtn = wrap.querySelector(".er-detail-btn");
    if (detailBtn) detailBtn.textContent = "Close";
  }

  // Refresh run filter visibility
  _applyRunFilters();
  _syncCompactProgress();
}

async function _renderRunPanel() {
  // Called when user clicks Run nav item — just refresh the step
  await renderRunStep();
}

// ── Execution render helpers ──────────────────────────────────────────────────

function _execLogBlock(label, text) {
  if (!text || !text.trim()) return "";
  return `<details style="margin-top:8px">
    <summary style="cursor:pointer;font-weight:500;font-size:13px">${escapeHtml(label)}</summary>
    <pre style="max-height:240px;overflow:auto;background:var(--bg-muted,#f5f5f5);padding:10px;border-radius:6px;font-size:12px;white-space:pre-wrap;margin-top:6px">${escapeHtml(text.slice(0, 4096))}</pre>
  </details>`;
}

function renderExecResult(data) {
  const passed          = data.passed;
  const timedOut        = data.timed_out;
  const buildFailed     = data.build_failed;
  const containerFailed = !!data.container_start_failed;
  const exitCode        = data.exit_code;
  const outputFiles     = Array.isArray(data.output_files) ? data.output_files : [];
  const fileCount       = data.output_file_count ?? outputFiles.length;
  const ov              = data.output_validation;
  const earlyFail       = buildFailed || containerFailed;

  function _step(iconCls, iconChar, label, statusCls, statusTxt, body) {
    return `
      <div class="exec-step${iconCls === "si-skip" ? " exec-step-skipped" : ""}">
        <div class="exec-step-header">
          <span class="exec-step-icon ${iconCls}">${iconChar}</span>
          <span class="exec-step-title">${escapeHtml(label)}</span>
          <span class="exec-step-badge exec-step-status-label ${statusCls}">${statusTxt}</span>
        </div>
        ${body}
      </div>`;
  }

  // Step 1: Build run instructions
  let s1Icon, s1Char, s1StatusCls, s1StatusTxt, s1Body = "";
  if (buildFailed) {
    s1Icon = "si-fail"; s1Char = "✗"; s1StatusCls = "sl-fail"; s1StatusTxt = "Build failed";
    s1Body = `<p class="exec-step-note">Run instructions could not be built. Check technical logs below.</p>`;
  } else if (containerFailed) {
    s1Icon = "si-fail"; s1Char = "✗"; s1StatusCls = "sl-fail"; s1StatusTxt = "Could not start";
    s1Body = `<p class="exec-step-note">Container failed to start (exit 125) — this is typically a host configuration issue, not a problem with the submission itself.</p>`;
  } else {
    s1Icon = "si-pass"; s1Char = "✓"; s1StatusCls = "sl-pass"; s1StatusTxt = "Ready";
  }
  const s1Logs = `
    <details class="exec-logs">
      <summary>Technical logs</summary>
      <div class="exec-log-pre" style="font-size:0.68rem;color:var(--muted);margin-top:6px">Exit code: ${exitCode ?? "—"}${data.command ? ` · Command: ${escapeHtml(data.command)}` : ""}</div>
      ${_execLogBlock("stderr / run errors", data.stderr_preview)}
      ${_execLogBlock("stdout / run output", data.stdout_preview)}
    </details>`;
  const step1 = _step(s1Icon, s1Char, "Build run instructions", s1StatusCls, s1StatusTxt, s1Body + s1Logs);

  // Step 2: Run package
  let s2Icon, s2Char, s2StatusCls, s2StatusTxt, s2Body = "";
  if (earlyFail) {
    s2Icon = "si-skip"; s2Char = "–"; s2StatusCls = "sl-skip"; s2StatusTxt = "Skipped";
  } else if (timedOut) {
    s2Icon = "si-warn"; s2Char = "⏱"; s2StatusCls = "sl-warn"; s2StatusTxt = "Timed out";
    s2Body = `<p class="exec-step-note">Submission exceeded the time limit and was stopped.</p>`;
  } else if (!passed) {
    s2Icon = "si-fail"; s2Char = "✗"; s2StatusCls = "sl-fail"; s2StatusTxt = `Exit ${exitCode ?? "?"}`;
  } else {
    s2Icon = "si-pass"; s2Char = "✓"; s2StatusCls = "sl-pass"; s2StatusTxt = "Exit 0";
  }
  const step2 = _step(s2Icon, s2Char, "Run package", s2StatusCls, s2StatusTxt, s2Body);

  // Step 3: Collect generated outputs
  let s3Icon, s3Char, s3StatusCls, s3StatusTxt, s3Body = "";
  if (earlyFail) {
    s3Icon = "si-skip"; s3Char = "–"; s3StatusCls = "sl-skip"; s3StatusTxt = "Skipped";
  } else if (fileCount === 0) {
    s3Icon = "si-warn"; s3Char = "!"; s3StatusCls = "sl-warn"; s3StatusTxt = "No files";
    s3Body = `<p class="exec-step-note">No files were written to <code>/output</code>.</p>`;
  } else {
    s3Icon = "si-pass"; s3Char = "✓"; s3StatusCls = "sl-pass"; s3StatusTxt = `${fileCount} file${fileCount !== 1 ? "s" : ""}`;
    if (outputFiles.length > 0) {
      const chips = outputFiles.slice(0, 12).map((f) => `<span class="exec-file-chip">${escapeHtml(f)}</span>`).join("");
      const more  = outputFiles.length > 12 ? `<span style="font-size:0.68rem;color:var(--subtle)">+${outputFiles.length - 12} more</span>` : "";
      s3Body = `<div class="exec-file-list">${chips}${more}</div>`;
    }
  }
  const step3 = _step(s3Icon, s3Char, "Collect generated outputs", s3StatusCls, s3StatusTxt, s3Body);

  // Step 4: Validate generated outputs
  let s4Icon, s4Char, s4StatusCls, s4StatusTxt, s4Body = "";
  if (earlyFail || fileCount === 0 || !ov) {
    s4Icon = "si-skip"; s4Char = "–"; s4StatusCls = "sl-skip"; s4StatusTxt = "Skipped";
  } else {
    const ovErrs  = (ov.errors   || []).map((e) => e.message || String(e));
    const ovWarns = (ov.warnings || []).map((w) => w.message || String(w));
    if (ov.passed) {
      s4Icon = "si-pass"; s4Char = "✓"; s4StatusCls = "sl-pass"; s4StatusTxt = "Valid";
      if (ov.nifti_count != null) s4Body = `<p class="exec-step-note">${ov.nifti_count} NIfTI file${ov.nifti_count !== 1 ? "s" : ""} detected in output.</p>`;
    } else {
      s4Icon = "si-fail"; s4Char = "✗"; s4StatusCls = "sl-fail"; s4StatusTxt = `${ovErrs.length} error${ovErrs.length !== 1 ? "s" : ""}`;
      const errHtml  = ovErrs.map((m)  => `<li class="is-error">✕ ${escapeHtml(m)}</li>`).join("");
      const warnHtml = ovWarns.map((m) => `<li class="is-warning">! ${escapeHtml(m)}</li>`).join("");
      s4Body = `<ul class="batch-issue-list" style="margin-top:6px">${errHtml}${warnHtml}</ul>`;
    }
  }
  const step4 = _step(s4Icon, s4Char, "Validate generated outputs", s4StatusCls, s4StatusTxt, s4Body);

  return `<div class="exec-steps">${step1}${step2}${step3}${step4}</div>`;
}

function renderExecError(msg) {
  return `<div style="color:var(--error,#c00);font-size:13px;padding:6px 0">${escapeHtml(msg)}</div>`;
}

// ── Core execution function (used by both validate-row buttons and run-step buttons) ──

// Helper: find a run card by submission ID (works for both .run-sub-card and legacy .er-row-wrap)
function _findRunCard(subId) {
  return [...document.querySelectorAll("#run-submissions-list .run-sub-card, #run-submissions-list .er-row-wrap")]
    .find((w) => w.dataset.subId === subId) || null;
}

async function runBatchExec(btn, subId, challenge) {
  const timeout = parseInt(el("batch-exec-timeout")?.value || "300", 10) || 300;
  const idleLabel = btn ? (btn.textContent.trim() || "Run code in Docker") : "";
  if (btn) setLoading(btn, true, "Running");

  // Mark card as running
  const runWrap = _findRunCard(subId);
  if (runWrap) {
    runWrap.dataset.execStatus = "running";
    const sc = runWrap.querySelector(".er-run-status-cell, .run-card-status-row");
    if (sc) {
      const reasonEl = sc.querySelector(".run-card-reason, .run-card-reason-warn");
      const reasonHtml = reasonEl ? reasonEl.outerHTML : "";
      sc.innerHTML = _erRunStatusHtml("running", true) + reasonHtml;
    }
  }

  try {
    const resp = await fetch("/api/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ submission_id: subId, challenge_type: challenge, timeout_seconds: timeout }),
    });
    const data = await resp.json();

    if (!resp.ok || !data.success) {
      const msg = (data && (data.detail || data.message)) || "Execution failed.";
      _updateRunRow(subId, null, msg);
      _tickRunProgress(false, 0);
    } else {
      _updateRunRow(subId, data, false);
      const fc = data.output_file_count ?? (Array.isArray(data.output_files) ? data.output_files.length : 0);
      _tickRunProgress(data.passed && !data.timed_out, fc);
    }
  } catch (err) {
    _updateRunRow(subId, null, "Network error: " + err.message);
    _tickRunProgress(false, 0);
  } finally {
    if (btn) setLoading(btn, false, idleLabel || "Run code in Docker");
    _syncCompactProgress();
  }
}

function _updateValCardExecBadge(subId, newStatus) {
  const wraps = [...document.querySelectorAll("#batch-submissions-list .br-row-wrap")]
    .filter((w) => w.dataset.subId === subId);
  wraps.forEach((wrap) => {
    wrap.dataset.execStatus = newStatus;
    // Update all .val-card-exec-badge inside this row (both row cell and detail)
    wrap.querySelectorAll(".val-card-exec-badge").forEach((badge) => {
      badge.style.display = "";
      if (newStatus === "passed") {
        badge.className = "br-badge badge-exec-pass val-card-exec-badge";
        badge.textContent = "Ran ✓";
      } else {
        badge.className = "br-badge badge-exec-fail val-card-exec-badge";
        badge.textContent = "Ran ✗";
      }
    });
  });
}

// Delegation: Run buttons in validate-step inline exec panels (.batch-exec-btn)
(function initBatchExecDelegation() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".batch-exec-btn");
    if (!btn) return;
    const section = btn.closest(".batch-exec-section");
    if (!section) return;
    e.stopPropagation();
    await runBatchExec(btn, section.dataset.subId, section.dataset.challenge || "dce");
  });
})();

// Delegation: Run buttons in the Run step table (.er-run-btn)
(function initRunStepBtnDelegation() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".er-run-btn");
    if (!btn) return;
    e.stopPropagation();
    const subId     = btn.dataset.subId;
    const challenge = btn.dataset.challenge || "dce";
    if (!subId) return;
    await runBatchExec(btn, subId, challenge);
  });
})();

// Delegation: Details button in run step (.er-detail-btn) — works for table rows + new cards
(function initRunDetailBtnDelegation() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".er-detail-btn");
    if (!btn) return;
    // Support both old .er-row-wrap and new .run-sub-card
    const wrap = btn.closest(".er-row-wrap, .run-sub-card");
    if (!wrap) return;
    e.stopPropagation();
    const drawer = wrap.querySelector(".er-row-detail, .run-card-detail");
    if (!drawer) return;
    const isHidden = drawer.style.display === "none";
    drawer.style.display = isHidden ? "" : "none";
    btn.textContent = isHidden ? "Close" : "Details";
  });
})();

// "Run All" button in run step
(function initBatchExecAll() {
  const btn      = el("batch-exec-all-btn");
  const statusEl = el("batch-exec-status");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    // Collect all runnable cards/rows that haven't been executed yet
    const runnableRows = [...document.querySelectorAll("#run-submissions-list .run-sub-card, #run-submissions-list .er-row-wrap")]
      .filter((w) => w.dataset.runnable === "true" && w.dataset.execStatus === "not-run");
    if (!runnableRows.length) return;
    const idleLabel = btn.textContent.trim() || "Run code in Docker";
    setLoading(btn, true, "Running");
    _initRunProgress(runnableRows.length);
    if (statusEl) { statusEl.style.display = ""; statusEl.textContent = `Starting ${runnableRows.length} execution(s)…`; }
    let done = 0;
    try {
      for (const row of runnableRows) {
        if (statusEl) statusEl.textContent = `Running ${done + 1} of ${runnableRows.length}…`;
        await runBatchExec(null, row.dataset.subId, row.dataset.challenge || "dce");
        done++;
      }
      if (statusEl) statusEl.textContent = `Done — ran ${done} submission(s).`;
    } finally {
      setLoading(btn, false, idleLabel);
      _syncCompactProgress();
    }
  });
})();

// Single submission execute section (kept for compatibility; unused in main flow)
(function initExecuteSection() {
  const executeBtn     = el("execute-btn");
  const executeStatus  = el("execute-status");
  const executeResult  = el("execute-result");
  const executeSection = el("execute-section");

  window._showExecuteSection = function (errCount, hasRunInstructions) {
    if (!executeSection) return;
    if (errCount > 0) { executeSection.style.display = "none"; return; }
    executeSection.style.display = "";
    const cannotNote  = el("run-cannot-note");
    const runControls = el("run-controls");
    const rps         = el("run-panel-status");
    if (hasRunInstructions === false) {
      if (cannotNote) { cannotNote.style.display = ""; cannotNote.textContent = "No run instructions found — this submission can be validated as result-only but cannot be run automatically."; }
      if (runControls) runControls.style.display = "none";
      if (rps) { rps.className = "run-panel-status rps-na"; rps.textContent = "Cannot run"; }
    } else {
      if (cannotNote) cannotNote.style.display = "none";
      if (runControls) runControls.style.display = "flex";
      if (rps) { rps.className = "run-panel-status rps-pending"; rps.textContent = "Not run"; }
    }
  };

  if (!executeBtn) return;

  executeBtn.addEventListener("click", async () => {
    const submissionId  = window._currentSubmissionId || state.submissionId;
    const challengeType = window._currentChallengeType || getChallengeType() || "dce";
    if (!submissionId) return;

    const timeoutSeconds = parseInt(el("execute-timeout")?.value || "300", 10) || 300;
    executeBtn.disabled = true;
    if (executeStatus) { executeStatus.style.display = ""; executeStatus.className = "submit-status"; executeStatus.textContent = "Building and running package…"; }
    if (executeResult) { executeResult.style.display = "none"; executeResult.innerHTML = ""; }

    try {
      const resp = await fetch("/api/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ submission_id: submissionId, challenge_type: challengeType, timeout_seconds: timeoutSeconds }),
      });
      const data = await resp.json();

      if (!resp.ok || !data.success) {
        if (executeStatus) { executeStatus.className = "submit-status status-error"; executeStatus.textContent = (data && (data.detail || data.message)) || "Execution failed."; }
        const rps = el("run-panel-status");
        if (rps) { rps.className = "run-panel-status rps-fail"; rps.textContent = "✗ Failed"; }
        return;
      }

      const rps = el("run-panel-status");
      if (rps) {
        if (data.timed_out)   { rps.className = "run-panel-status rps-warn"; rps.textContent = "⏱ Timed out"; }
        else if (data.passed) { rps.className = "run-panel-status rps-pass"; rps.textContent = "✓ Passed"; }
        else                  { rps.className = "run-panel-status rps-fail"; rps.textContent = "✗ Failed"; }
      }

      if (executeStatus) executeStatus.style.display = "none";
      if (executeResult) { executeResult.style.display = ""; executeResult.innerHTML = renderExecResult(data); }
      _showExecExportRow(submissionId);
    } catch (err) {
      if (executeStatus) { executeStatus.className = "submit-status status-error"; executeStatus.textContent = "Network error: " + err.message; }
    } finally {
      executeBtn.disabled = false;
    }
  });

  function _showExecExportRow(submissionId) {
    const row = el("exec-export-row");
    if (!row || !submissionId) return;
    row.style.display = "";
    const blindedBtn   = el("exec-export-blinded-btn");
    const unblindedBtn = el("exec-export-unblinded-btn");
    const statusEl     = el("exec-export-status");

    async function doExport(blinded) {
      const btn = blinded ? blindedBtn : unblindedBtn;
      const label = btn.textContent.trim() || (blinded ? "Download Execution CSV" : "Download Unblinded Export");
      if (!btn) return;
      setLoading(btn, true, label);
      if (statusEl) statusEl.style.display = "none";
      try {
        const url = `${API}/api/export-execution?submission_id=${encodeURIComponent(submissionId)}&blinded=${blinded}`;
        const r = await fetch(url);
        if (!r.ok) { const err = await r.json().catch(() => ({})); if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = err.detail || "Export failed."; } return; }
        const blob = await r.blob();
        const cd   = r.headers.get("Content-Disposition") || "";
        const fname = cd.match(/filename="([^"]+)"/)?.[1] || `execution_${blinded ? "blinded" : "unblinded"}.csv`;
        triggerDownload(blob, fname);
      } catch (err) {
        if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = "Network error: " + err.message; }
      } finally {
        setLoading(btn, false, label);
      }
    }

    if (blindedBtn)   blindedBtn.onclick   = () => doExport(true);
    if (unblindedBtn) unblindedBtn.onclick = () => doExport(false);
  }
})();

// ── Leaderboard ───────────────────────────────────────────────────────────────

function _leaderboardStatusLabel(status) {
  switch (status) {
    case "scored":         return "Scored";
    case "failed":         return "Failed";
    case "not_configured": return "Needs setup";
    case "not_ready":      return "Incomplete";
    case "ready":          return "Ready";
    case "reference_not_available": return "Reference unavailable";
    case "partial_reference_scoring": return "Partial reference scoring";
    case "scoring_error": return "Scoring error";
    case "reference_invalid": return "Reference invalid";
    case "no_finite_overlap": return "No finite overlap";
    default:               return status ? status.replace(/_/g, " ") : "Unknown";
  }
}

function _leaderboardStatusClass(status) {
  const clean = String(status || "unknown").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
  return `leaderboard-status-badge leaderboard-status-${clean}`;
}

function _leaderboardReferenceStatus(entry) {
  return entry.reference_scoring_status
    || entry.reference_status
    || entry.referenceBasedScoringStatus
    || entry.reference_based_scoring_status
    || (entry.reference_based_scoring_available ? "scored" : "");
}

function _leaderboardChallenge(entry) {
  return String(entry.challenge_type || entry.challenge || entry.metrics?.challenge_type || "Not provided").toUpperCase();
}

function _leaderboardMapTypes(entry) {
  const raw = entry.map_types || entry.detected_map_types || entry.parameter_maps_detected
    || entry.metrics?.parameter_maps_detected || entry.metrics?.map_type || entry.map_type;
  if (Array.isArray(raw)) return raw.filter(Boolean).map(String);
  if (typeof raw === "string" && raw.trim()) return raw.split(/[,|;]/).map((v) => v.trim()).filter(Boolean);
  return [];
}

function _leaderboardMetric(entry, keys) {
  const metrics = entry.metrics || {};
  for (const key of keys) {
    if (typeof metrics[key] === "number" && isFinite(metrics[key])) return metrics[key];
  }
  return null;
}

function _leaderboardMetrics(entry) {
  return {
    rmse: _leaderboardMetric(entry, ["rmse", "RMSE", "reference_mean_rmse", "mean_rmse"]),
    mae: _leaderboardMetric(entry, ["mae", "MAE", "reference_mean_mae", "mean_mae"]),
    bias: _leaderboardMetric(entry, ["bias", "mean_error", "reference_mean_bias", "mean_bias"]),
    cov: _leaderboardMetric(entry, ["coefficient_of_variation", "cov", "CoV", "reference_mean_coefficient_of_variation", "mean_coefficient_of_variation"]),
  };
}

function _leaderboardEntryText(entry) {
  return [
    entry.submission_id,
    entry.display_name,
    entry.team_name,
    entry.provider_id,
    _leaderboardChallenge(entry),
    _leaderboardMapTypes(entry).join(" "),
  ].filter(Boolean).join(" ").toLowerCase();
}

function _renderLeaderboardFilterBar() {
  const f = _leaderboardFilter;
  return `
    ${_renderSearchBox("leaderboard-search", f.search, "Search scored submissions")}
    ${_renderFilterDropdown("leaderboard-date", "Date", f.date, [
      { value: "all", label: "All time" },
      { value: "24h", label: "Last 24 hours" },
      { value: "7d", label: "Last 7 days" },
      { value: "1m", label: "Last month" },
      { value: "1q", label: "Last quarter" },
      { value: "1y", label: "Last year" },
    ])}
    ${_renderFilterDropdown("leaderboard-status", "Status", f.status, [
      { value: "all", label: "All" },
      { value: "scored", label: "Scored" },
      { value: "not_configured", label: "Not configured" },
      { value: "failed", label: "Failed" },
      { value: "reference_not_available", label: "Reference unavailable" },
      { value: "partial_reference_scoring", label: "Partial reference scoring" },
    ])}
    ${_renderFilterDropdown("leaderboard-challenge", "Challenge", f.challenge, [
      { value: "all", label: "All" },
      { value: "ASL", label: "ASL" },
      { value: "DCE", label: "DCE" },
      { value: "DSC", label: "DSC" },
      { value: "OTHER", label: "Other" },
    ])}
    ${_renderFilterDropdown("leaderboard-map", "Map type", f.map, [
      { value: "all", label: "All" },
      { value: "CBF", label: "CBF" },
      { value: "ATT", label: "ATT" },
      { value: "Ktrans", label: "Ktrans" },
      { value: "ve", label: "ve" },
      { value: "vp", label: "vp" },
      { value: "Kep", label: "Kep" },
    ])}
    ${_renderFilterDropdown("leaderboard-sort", "Sort", f.sort, [
      { value: "newest", label: "Newest first" },
      { value: "oldest", label: "Oldest first" },
      { value: "name", label: "Name A-Z" },
      { value: "status", label: "Status" },
    ])}
    <button type="button" class="filter-clear-btn" id="leaderboard-clear-filters">Clear filters</button>`;
}

function _filteredLeaderboardEntries() {
  const f = _leaderboardFilter;
  const q = (f.search || "").trim().toLowerCase();
  const rows = (f.entries || []).filter((entry) => {
    const status = entry.status || "unknown";
    const refStatus = _leaderboardReferenceStatus(entry);
    const challenge = _leaderboardChallenge(entry);
    const mapTypes = _leaderboardMapTypes(entry).map((m) => m.toLowerCase());
    if (q && !_leaderboardEntryText(entry).includes(q)) return false;
    if (!_dateWithinFilter(entry.scored_at || entry.created_at || entry.uploaded_at, f.date)) return false;
    if (f.status !== "all" && status !== f.status && refStatus !== f.status) return false;
    if (f.challenge !== "all" && challenge !== f.challenge) return false;
    if (f.map !== "all" && !mapTypes.includes(String(f.map).toLowerCase())) return false;
    return true;
  });
  rows.sort((a, b) => {
    if (f.sort === "oldest") return new Date(a.scored_at || 0) - new Date(b.scored_at || 0);
    if (f.sort === "name") return String(a.submission_id || "").localeCompare(String(b.submission_id || ""));
    if (f.sort === "status") return String(a.status || "").localeCompare(String(b.status || ""));
    return new Date(b.scored_at || 0) - new Date(a.scored_at || 0);
  });
  return rows;
}

function _leaderboardStatusBadge(status) {
  return `<span class="${_leaderboardStatusClass(status)}">${escapeHtml(_leaderboardStatusLabel(status))}</span>`;
}

function _leaderboardMetricChip(label, value) {
  const tips = {
    RMSE: "Root mean squared error between the submitted map and reference map.",
    MAE: "Mean absolute error between the submitted map and reference map.",
    Bias: "Mean signed difference between submitted and reference values.",
    CoV: "Standard deviation divided by mean. Useful for checking map variability.",
  };
  return `<span class="metric-chip">${escapeHtml(label)}${tips[label] ? ` ${helpTooltip(tips[label], `${label} help`)}` : ""} <strong>${escapeHtml(_dashMetric(value))}</strong></span>`;
}

function _renderLeaderboardEntry(entry) {
  const sid = entry.submission_id || "unknown";
  const safeSid = escapeHtml(sid);
  const ts = _formatLeaderboardTimestamp(entry.scored_at);
  const status = entry.status || "unknown";
  const refStatus = _leaderboardReferenceStatus(entry);
  const challenge = _leaderboardChallenge(entry);
  const mapTypes = _leaderboardMapTypes(entry);
  const metrics = _leaderboardMetrics(entry);
  const artifactCount = Number(entry.artifact_count || 0);
  const exportReady = status === "scored" || artifactCount > 0;
  const mapBadges = mapTypes.length
    ? mapTypes.slice(0, 4).map((m) => `<span class="list-chip">${escapeHtml(m)}</span>`).join("")
    : `<span class="list-chip is-muted">Map type not provided</span>`;
  const refBadge = refStatus
    ? `<span class="${_leaderboardStatusClass(refStatus)}">${escapeHtml(_leaderboardStatusLabel(refStatus))}</span>`
    : `<span class="leaderboard-status-badge leaderboard-status-unknown">Reference not provided</span>`;
  return `<article class="leaderboard-row" data-leaderboard-row data-sub-id="${safeSid}" data-status="${escapeHtml(status)}" data-reference-status="${escapeHtml(refStatus || "")}">
    <div class="leaderboard-row-main">
      <div class="leaderboard-row-title">
        <div class="leaderboard-submission-name" title="${safeSid}">${safeSid}</div>
        <div class="leaderboard-row-badges">
          <span class="list-chip list-chip-strong">${escapeHtml(challenge)}</span>
          ${mapBadges}
          ${_leaderboardStatusBadge(status)}
          ${refBadge}
        </div>
      </div>
      <div class="leaderboard-meta-line">
        <span>${escapeHtml(ts.date)}${ts.time ? ` · ${escapeHtml(ts.time)}` : ""}</span>
        <span>Export readiness ${helpTooltip("Export files are available after validation/scoring artifacts have been generated.", "Export readiness help")}: ${exportReady ? "Ready" : "Pending"}</span>
      </div>
      <div class="leaderboard-metric-row">
        ${_leaderboardMetricChip("RMSE", metrics.rmse)}
        ${_leaderboardMetricChip("MAE", metrics.mae)}
        ${_leaderboardMetricChip("Bias", metrics.bias)}
        ${_leaderboardMetricChip("CoV", metrics.cov)}
      </div>
    </div>
    <div class="leaderboard-actions">
      <button type="button" class="btn btn-secondary btn-sm" data-leaderboard-view="${safeSid}">View results</button>
      <a class="btn btn-secondary btn-sm" href="/api/export-scoring?submission_id=${encodeURIComponent(sid)}&blinded=false">Export</a>
      <button type="button" class="btn btn-ghost btn-sm" data-leaderboard-detail="${safeSid}">Details</button>
    </div>
    <div class="leaderboard-detail" hidden>
      <div><strong>Provider:</strong> ${escapeHtml(entry.provider_id || "not provided")}</div>
      <div><strong>Reference scoring:</strong> ${escapeHtml(refStatus || "not provided")}</div>
      <div><strong>Artifacts:</strong> ${escapeHtml(artifactCount)} file${artifactCount === 1 ? "" : "s"}</div>
      ${entry.message ? `<div><strong>Message:</strong> ${escapeHtml(entry.message)}</div>` : ""}
    </div>
  </article>`;
}

function _wireLeaderboardFilterControls() {
  const search = el("leaderboard-search");
  if (search) {
    search.oninput = () => {
      _leaderboardFilter.search = search.value;
      _renderLeaderboardEntries();
    };
  }
  const clear = el("leaderboard-clear-filters");
  if (clear) {
    clear.onclick = () => {
      _leaderboardFilter.search = "";
      _leaderboardFilter.date = "all";
      _leaderboardFilter.status = "all";
      _leaderboardFilter.challenge = "all";
      _leaderboardFilter.map = "all";
      _leaderboardFilter.sort = "newest";
      _renderLeaderboardEntries();
    };
  }
}

function _renderLeaderboardEntries() {
  const card = el("leaderboard-card");
  const list = el("leaderboard-list");
  const filterBar = el("leaderboard-filter-bar");
  const countEl = el("leaderboard-count");
  const sub = el("leaderboard-sub");
  if (!card || !list || !filterBar) return;
  card.style.display = "";
  filterBar.innerHTML = _renderLeaderboardFilterBar();
  _wireLeaderboardFilterControls();

  if (_leaderboardFilter.loading) {
    list.innerHTML = `<div class="leaderboard-loading">
      <div class="leaderboard-skeleton"></div>
      <div class="leaderboard-skeleton short"></div>
    </div>`;
    if (countEl) countEl.textContent = "Loading scored submissions…";
    if (sub) sub.textContent = "Refreshing scored submissions";
    return;
  }

  if (_leaderboardFilter.error) {
    list.innerHTML = `<div class="list-empty-state is-error">
      <p>${escapeHtml(_leaderboardFilter.error)}</p>
      <button type="button" class="btn btn-secondary btn-sm" id="leaderboard-retry-btn">Retry</button>
    </div>`;
    const retry = el("leaderboard-retry-btn");
    if (retry) retry.onclick = loadLeaderboard;
    if (countEl) countEl.textContent = "Refresh failed";
    if (sub) sub.textContent = "Review scored submissions, reference status, and export readiness.";
    return;
  }

  const entries = _filteredLeaderboardEntries();
  if (countEl) countEl.textContent = `${entries.length} submission${entries.length !== 1 ? "s" : ""}`;
  if (sub) sub.textContent = "Review scored submissions, reference status, and export readiness.";
  if (!entries.length) {
    const hasAny = (_leaderboardFilter.entries || []).length > 0;
    list.innerHTML = `<div class="list-empty-state">
      <p>${hasAny ? "No submissions match these filters." : "No scored submissions yet."}</p>
      ${hasAny ? `<button type="button" class="btn btn-secondary btn-sm" id="leaderboard-empty-clear">Clear filters</button>` : ""}
    </div>`;
    const emptyClear = el("leaderboard-empty-clear");
    if (emptyClear) emptyClear.onclick = () => {
      _leaderboardFilter.search = "";
      _leaderboardFilter.date = "all";
      _leaderboardFilter.status = "all";
      _leaderboardFilter.challenge = "all";
      _leaderboardFilter.map = "all";
      _leaderboardFilter.sort = "newest";
      _renderLeaderboardEntries();
    };
    return;
  }

  list.innerHTML = entries.map(_renderLeaderboardEntry).join("");
}

function _formatLeaderboardTimestamp(value) {
  if (!value) return { date: "—", time: "" };
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return { date: String(value), time: "" };
  return {
    date: dt.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" }),
    time: dt.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" }),
  };
}

async function loadLeaderboard() {
  const card = el("leaderboard-card");
  if (!card) return;
  _leaderboardFilter.loading = true;
  _leaderboardFilter.error = "";
  _renderLeaderboardEntries();

  try {
    const res  = await fetch(`${API}/api/leaderboard`);
    if (!res.ok) {
      _leaderboardFilter.loading = false;
      _leaderboardFilter.error = "Could not load scored submissions.";
      _renderLeaderboardEntries();
      return;
    }
    const data = await res.json();
    _leaderboardFilter.entries = data.entries || [];
    _leaderboardFilter.loading = false;
    _leaderboardFilter.error = "";
    _renderLeaderboardEntries();
  } catch (_) {
    _leaderboardFilter.loading = false;
    _leaderboardFilter.error = "Could not load scored submissions.";
    _renderLeaderboardEntries();
  }
}

(function _wireLeaderboard() {
  const btn = el("leaderboard-refresh-btn");
  if (btn) btn.addEventListener("click", loadLeaderboard);
})();

document.addEventListener("click", (e) => {
  const detailBtn = e.target.closest("[data-leaderboard-detail]");
  if (detailBtn) {
    const row = detailBtn.closest(".leaderboard-row");
    const detail = row?.querySelector(".leaderboard-detail");
    if (detail) {
      detail.hidden = !detail.hidden;
      detailBtn.textContent = detail.hidden ? "Details" : "Close";
    }
    return;
  }
  const viewBtn = e.target.closest("[data-leaderboard-view]");
  if (viewBtn) {
    const sid = viewBtn.getAttribute("data-leaderboard-view");
    if (sid && _scoreCache[sid]) {
      _goToSummary();
    } else {
      viewBtn.closest(".leaderboard-row")?.querySelector("[data-leaderboard-detail]")?.click();
    }
  }
});

// ── Step 6: Export ────────────────────────────────────────────────────────────

function _syncExportStep() {
  // Show single or batch validation export buttons based on mode
  const batchValWrap  = el("batch-export-val-wrap");
  const singleValWrap = el("single-export-val");
  const batchExecWrap = el("batch-export-exec-wrap");
  const singleExecRow = el("exec-export-row");

  const hasExecResults  = Object.keys(_execSummaries).length > 0;
  // Scoring export group: hidden until _enableScoringExport() is called after scoring
  const scoringGroup = el("export-scoring-group");
  if (scoringGroup) scoringGroup.style.display = "none";

  if (batchState.isBatch) {
    if (batchValWrap)  batchValWrap.style.display  = "";
    if (singleValWrap) singleValWrap.style.display = "none";
    // Only show execution export group if at least one submission was actually run
    if (batchExecWrap) batchExecWrap.style.display = hasExecResults ? "" : "none";
    if (singleExecRow) singleExecRow.style.display = "none";
  } else {
    if (batchValWrap)  batchValWrap.style.display  = "none";
    if (singleValWrap) singleValWrap.style.display = "";
    if (batchExecWrap) batchExecWrap.style.display = "none";
    // singleExecRow shown by _showExecExportRow after exec runs
  }
}

// ── Batch validation export ───────────────────────────────────────────────────

const batchExportBlindedBtn   = el("batch-export-blinded-btn");
const batchExportUnblindedBtn = el("batch-export-unblinded-btn");

async function exportBatch(blinded) {
  const statusEl = el("batch-export-status");
  if (!batchState.batchId) {
    if (statusEl) { statusEl.style.display = "block"; statusEl.className = "submit-status status-error"; statusEl.textContent = "No batch to export. Run validation first."; }
    return;
  }
  const btn   = blinded ? batchExportBlindedBtn : batchExportUnblindedBtn;
  const label = btn ? (btn.textContent.trim() || (blinded ? "Download Validation CSV" : "Download Unblinded Export")) : "";
  if (btn) setLoading(btn, true, label);
  if (statusEl) statusEl.style.display = "none";
  try {
    const url = `${API}/api/export-batch?batch_id=${encodeURIComponent(batchState.batchId)}&blinded=${blinded}`;
    const res = await fetch(url);
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
    const blob = await res.blob();
    triggerDownload(blob, `osipi-batch-${batchState.batchId}-${blinded ? "blinded" : "unblinded"}.csv`);
  } catch (err) {
    if (statusEl) { statusEl.style.display = "block"; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "Export failed."; }
  } finally {
    if (btn) setLoading(btn, false, label);
  }
}

if (batchExportBlindedBtn)   batchExportBlindedBtn.addEventListener("click",   () => exportBatch(true));
if (batchExportUnblindedBtn) batchExportUnblindedBtn.addEventListener("click", () => exportBatch(false));

// ── Single validation export ──────────────────────────────────────────────────

function _makeValExportHandler(btn, blinded) {
  if (!btn) return;
  const label = btn.textContent.trim() || (blinded ? "Download Validation CSV" : "Download Unblinded Export");
  btn.addEventListener("click", async () => {
    const statusEl = el("download-status");
    if (!state.submissionId) {
      if (statusEl) { statusEl.style.display = "block"; statusEl.className = "submit-status status-error"; statusEl.textContent = "No submission to export."; }
      return;
    }
    if (requestInProgress) return;
    requestInProgress = true;
    setLoading(btn, true, label);
    if (statusEl) statusEl.style.display = "none";
    try {
      const url = `${API}/api/export-validation?submission_id=${encodeURIComponent(state.submissionId)}&format=csv&blinded=${blinded}`;
      const res = await fetch(url);
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
      const blob = await res.blob();
      triggerDownload(blob, `osipi-validation-${blinded ? "blinded" : "unblinded"}-${state.submissionId}.csv`);
    } catch (err) {
      if (statusEl) { statusEl.style.display = "block"; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "Could not export CSV."; }
    } finally {
      requestInProgress = false;
      setLoading(btn, false, label);
    }
  });
}
_makeValExportHandler(el("export-val-blinded-btn"),   true);
_makeValExportHandler(el("export-val-unblinded-btn"), false);

// ── Batch execution export ─────────────────────────────────────────────────────

const batchExportExecBlindedBtn   = el("batch-export-exec-blinded-btn");
const batchExportExecUnblindedBtn = el("batch-export-exec-unblinded-btn");

async function exportBatchExecution(blinded) {
  const statusEl = el("batch-export-status");
  if (!batchState.batchId) {
    if (statusEl) { statusEl.style.display = "block"; statusEl.className = "submit-status status-error"; statusEl.textContent = "No batch to export. Run validation and execution first."; }
    return;
  }
  const btn   = blinded ? batchExportExecBlindedBtn : batchExportExecUnblindedBtn;
  const label = btn ? (btn.textContent.trim() || (blinded ? "Download Execution CSV" : "Download Unblinded Export")) : "";
  if (btn) setLoading(btn, true, label);
  if (statusEl) statusEl.style.display = "none";
  try {
    const url = `${API}/api/export-batch-execution?batch_id=${encodeURIComponent(batchState.batchId)}&blinded=${blinded}`;
    const res = await fetch(url);
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
    const blob = await res.blob();
    triggerDownload(blob, `osipi-batch-execution-${batchState.batchId}-${blinded ? "blinded" : "unblinded"}.csv`);
  } catch (err) {
    if (statusEl) { statusEl.style.display = "block"; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "Export failed."; }
  } finally {
    if (btn) setLoading(btn, false, label);
  }
}

if (batchExportExecBlindedBtn)   batchExportExecBlindedBtn.addEventListener("click",   () => exportBatchExecution(true));
if (batchExportExecUnblindedBtn) batchExportExecUnblindedBtn.addEventListener("click", () => exportBatchExecution(false));

// ── Step 5: Score ─────────────────────────────────────────────────────────────

// ── Score progress tracking ───────────────────────────────────────────────────
const _scoreProgress = { total: 0, scored: 0, failed: 0, notConf: 0 };

function _initScoreProgress(total) {
  _scoreProgress.total   = total;
  _scoreProgress.scored  = 0;
  _scoreProgress.failed  = 0;
  _scoreProgress.notConf = 0;
  const panel = el("score-progress-panel");
  if (panel) { panel.style.display = ""; panel.className = "run-progress-panel"; }
  _refreshScoreProgress();
}

function _tickScoreProgress(scored, notConf) {
  if (scored)   _scoreProgress.scored++;
  else if (notConf) _scoreProgress.notConf++;
  else          _scoreProgress.failed++;
  _refreshScoreProgress();
}

function _refreshScoreProgress() {
  const { total, scored, failed, notConf } = _scoreProgress;
  const completed = scored + failed + notConf;
  const pct   = total > 0 ? Math.round((completed / total) * 100) : 0;
  const fill  = el("score-prog-fill");
  const panel = el("score-progress-panel");
  const tEl   = el("sp-total");
  const sEl   = el("sp-scored");
  const fEl   = el("sp-failed");
  const nEl   = el("sp-not-conf");
  const txt   = el("score-progress-text");
  const eta   = el("score-progress-eta");
  if (fill) fill.style.width = `${pct}%`;
  if (tEl)  tEl.textContent  = String(total);
  if (sEl)  sEl.textContent  = String(scored);
  if (fEl)  fEl.textContent  = String(failed);
  if (nEl)  nEl.textContent  = String(notConf);
  if (completed >= total && total > 0) {
    if (panel) panel.classList.add("state-done");
    if (txt)   txt.textContent = "Scoring complete";
    if (eta)   eta.textContent = `${scored} scored · ${failed + notConf} not scored`;
    const titleEl = el("score-status-title");
    const subEl = el("score-status-sub");
    const previewEl = el("score-metric-preview");
    if (titleEl) titleEl.textContent = failed + notConf > 0 ? "Scoring needs attention" : "Scoring complete";
    if (subEl) subEl.textContent = scored > 0 ? `${scored} submission${scored !== 1 ? "s" : ""} scored.` : "No submissions were scored.";
    if (previewEl) {
      const preview = _scoreMetricPreviewHtml();
      previewEl.innerHTML = preview;
      previewEl.style.display = preview ? "" : "none";
    }
    _enableScoringExport();
    loadLeaderboard();
    _syncCompactProgress();
  } else {
    if (txt) txt.textContent = `Scoring submissions… ${completed} of ${total}`;
    if (eta) eta.textContent = "";
    _syncCompactProgress();
  }
}

// ── renderScoreStep() ─────────────────────────────────────────────────────────

const _SC_METRIC_KEYS   = ["accuracy", "repeatability", "reproducibility", "osipi_silver_score", "osipi_gold_score"];
const _SC_METRIC_LABELS = {
  accuracy:           "Accuracy",
  repeatability:      "Repeatability",
  reproducibility:    "Reproducibility",
  osipi_silver_score: "Silver Score",
  osipi_gold_score:   "Gold Score",
  // QC / demo metrics (ASL QC demo package and similar)
  file_count:                    "Files",
  ok_file_count:                 "Readable files",
  failed_file_count:             "Failed files",
  mean_finite_percent:           "Mean finite %",
  mean_coefficient_of_variation: "Mean CoV",
  reference_comparisons:         "Ref. comparisons",
  mean_rmse:                     "Mean RMSE",
  mean_bias:                     "Mean bias",
};

// Human label for an arbitrary metric key (falls back to a tidy Title Case).
function _metricLabel(key) {
  if (_SC_METRIC_LABELS[key]) return _SC_METRIC_LABELS[key];
  return String(key).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

// Format a numeric metric value compactly (no spurious trailing decimals).
function _fmtMetricVal(v) {
  if (typeof v !== "number" || !isFinite(v)) return String(v);
  if (Number.isInteger(v)) return String(v);
  return (Math.abs(v) >= 100 ? v.toFixed(1) : v.toFixed(3)).replace(/\.?0+$/, "");
}

// Return only the numeric metric entries from a (possibly mixed) metrics object,
// skipping strings, booleans, and nested objects/arrays. This keeps string
// metadata (e.g. "package") out of the metrics table per OSIPI requirements.
function _numericMetricEntries(metrics) {
  if (!metrics || typeof metrics !== "object") return [];
  return Object.entries(metrics).filter(
    ([, v]) => typeof v === "number" && isFinite(v)
  );
}

function _scoreMetricPreviewHtml(limit = 4) {
  const entries = Object.values(_scoreCache).filter((s) => s.status === "scored");
  const numeric = [];
  entries.forEach((entry) => {
    _numericMetricEntries(entry.metrics || {}).forEach(([key, value]) => {
      numeric.push([key, value]);
    });
  });
  if (!numeric.length) return "";
  return numeric.slice(0, limit).map(([key, value]) =>
    `<span class="score-preview-pill">${escapeHtml(_metricLabel(key))}: ${escapeHtml(_fmtMetricVal(value))}</span>`
  ).join("");
}

function _fmtPercentValue(v) {
  if (typeof v !== "number" || !isFinite(v)) return "not available";
  return `${_fmtMetricVal(v)}%`;
}

function _scorePayload(data) {
  return (data && data.score_result) ? data.score_result : (data || {});
}

function _cacheScoreStatus(sid, data, row) {
  const result = _scorePayload(data);
  const status = data?.status || result.status || "not_configured";
  const analysis = result.nifti_analysis || data?.nifti_analysis || null;
  if (!(status === "scored" || status === "failed" || analysis)) return;
  _scoreCache[sid] = {
    status,
    displayName: row ? row.querySelector(".sc-col-sub")?.textContent?.trim() : sid,
    metrics: result.metrics || data?.metrics || {},
    metricsDetail: result.metrics_detail || data?.metrics_detail || {},
    niftiAnalysis: analysis,
    message: data?.message || result.message || "",
    official: result.official === true,
    referenceBasedScoringAvailable: result.reference_based_scoring_available === true
      || data?.reference_based_scoring_available === true,
  };
}

function _niftiAnalysisEntries() {
  return Object.values(_scoreCache)
    .map((entry) => entry.niftiAnalysis)
    .filter((analysis) => analysis && typeof analysis === "object");
}

function _aggregateNiftiAnalyses(analyses) {
  const maps = analyses.flatMap((analysis) => Array.isArray(analysis.maps) ? analysis.maps : []);
  const summaries = analyses.map((analysis) => analysis.summary || {}).filter((s) => s && typeof s === "object");
  const refObjects = analyses
    .map((analysis) => analysis.reference_scoring || {})
    .filter((ref) => ref && typeof ref === "object");
  const sum = (key) => summaries.reduce((acc, s) => acc + (Number(s[key]) || 0), 0);
  const avg = (key) => {
    const vals = summaries.map((s) => Number(s[key])).filter((v) => isFinite(v));
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  };
  const refAvg = (key) => {
    const vals = refObjects
      .map((ref) => ref.summary ? ref.summary[key] : null)
      .filter((raw) => typeof raw === "number" && isFinite(raw));
    return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : null;
  };
  const referenceRows = [];
  const referenceMapRows = [];
  refObjects.forEach((ref) => {
    (ref.maps || []).forEach((item) => {
      const whole = item.whole_map || {};
      const wholeRow = {
        ...whole,
        ...item,
        scope: "whole map",
        mask_name: "",
        status: item.status || whole.status || "",
      };
      referenceRows.push(wholeRow);
      referenceMapRows.push(wholeRow);
      (item.masks || []).forEach((mask) => {
        referenceRows.push({
          ...(mask.metrics || {}),
          submitted_file: item.submitted_file,
          reference_file: item.reference_file,
          detected_map_type: item.detected_map_type,
          scope: mask.mask_label || "mask",
          mask_name: mask.mask_name || "",
          status: mask.status,
          difference_map: item.difference_map,
        });
      });
    });
  });
  const referenceMapCount = referenceMapRows.length;
  const referenceComparedMapCount = referenceMapRows.filter((row) => row.status === "compared").length;
  const referenceMissingMapCount = referenceMapRows.filter((row) => row.status === "reference_not_available").length;
  const referenceErroredMapCount = referenceMapRows.filter((row) =>
    row.status && row.status !== "compared" && row.status !== "reference_not_available"
  ).length;
  let referenceStatus = refObjects.find((ref) => ref.status)?.status || "reference_not_available";
  if (referenceMapCount > 0) {
    if (referenceComparedMapCount > 0 && referenceComparedMapCount < referenceMapCount) {
      referenceStatus = "partial_reference_scoring";
    } else if (referenceComparedMapCount === referenceMapCount) {
      referenceStatus = "available";
    } else if (referenceErroredMapCount > 0) {
      referenceStatus = "scoring_error";
    } else if (referenceMissingMapCount === referenceMapCount) {
      referenceStatus = "reference_not_available";
    }
  }
  const referenceMapStatuses = referenceMapRows.map((row) => ({
    map: row.detected_map_type || row.submitted_file || "map",
    status: row.status || "unknown",
  }));
  const referenceMapStatusText = referenceMapStatuses.length
    ? referenceMapStatuses.slice(0, 4).map((row) => `${row.map}: ${row.status}`).join("; ")
      + (referenceMapStatuses.length > 4 ? `; +${referenceMapStatuses.length - 4} more` : "")
    : "not available";
  const totalVoxelCount = sum("total_voxel_count");
  const finiteVoxelCount = sum("finite_voxel_count");
  const negativeVoxelCount = sum("negative_voxel_count");
  const detected = [];
  summaries.forEach((s) => (s.parameter_maps_detected || []).forEach((m) => {
    if (m && !detected.includes(m)) detected.push(m);
  }));
  const meansByType = {};
  ["CBF", "ATT", "Ktrans"].forEach((type) => {
    const vals = summaries
      .map((s) => s.means_by_map_type && Number(s.means_by_map_type[type]))
      .filter((v) => isFinite(v));
    if (vals.length) meansByType[type] = vals.reduce((a, b) => a + b, 0) / vals.length;
  });
  return {
    mapCount: maps.length || sum("map_count"),
    maps,
    detected,
    totalVoxelCount,
    finiteVoxelCount,
    nanCount: sum("nan_count"),
    infCount: sum("inf_count"),
    negativeVoxelCount,
    finitePercent: totalVoxelCount ? (finiteVoxelCount / totalVoxelCount) * 100 : null,
    negativePercent: finiteVoxelCount ? (negativeVoxelCount / finiteVoxelCount) * 100 : null,
    coefficientOfVariation: avg("mean_coefficient_of_variation"),
    standardDeviation: avg("mean_standard_deviation"),
    meansByType,
    referenceBasedScoringAvailable: referenceComparedMapCount > 0,
    referenceStatus,
    referenceMapCount,
    referenceComparedMapCount,
    referenceMissingMapCount,
    referenceErroredMapCount,
    referenceMapStatuses,
    referenceMapStatusText,
    referenceRows,
    referenceMetrics: {
      rmse: refAvg("mean_rmse"),
      mae: refAvg("mean_mae"),
      bias: refAvg("mean_bias"),
      coefficientOfVariation: refAvg("mean_coefficient_of_variation"),
    },
  };
}

function _summaryMetricTooltip(label) {
  const tooltips = {
    "Reference scoring status": "Reference metrics are calculated only when a matching private ground-truth map is available.",
    "Finite voxels": "Percent of voxels that are valid numbers, excluding NaN and Inf.",
    "Finite voxel percentage": "Percent of voxels that are valid numbers, excluding NaN and Inf.",
    "Negative voxels": "Percent of voxels below zero. Some map types should rarely contain negative values.",
    "Negative voxel percentage": "Percent of voxels below zero. Some map types should rarely contain negative values.",
    "Coefficient of variation": "Standard deviation divided by mean. Useful for checking map variability.",
    "CoV": "Standard deviation divided by mean. Useful for checking map variability.",
    "RMSE": "Root mean squared error between the submitted map and reference map.",
    "MAE": "Mean absolute error between the submitted map and reference map.",
    "Bias": "Mean signed difference between submitted and reference values.",
  };
  return tooltips[label] || "";
}

function _summaryMetric(label, value, tone = "") {
  const tip = _summaryMetricTooltip(label);
  return `<div class="summary-kv${tone ? ` is-${tone}` : ""}">
    <span class="summary-kv-label">${escapeHtml(label)}${tip ? ` ${helpTooltip(tip, `${label} help`)}` : ""}</span>
    <span class="summary-kv-value">${escapeHtml(value)}</span>
  </div>`;
}

function _summaryPanel(title, bodyHtml) {
  return `<section class="summary-nifti-card">
    <div class="summary-nifti-title">${escapeHtml(title)}</div>
    <div class="summary-nifti-body">${bodyHtml}</div>
  </section>`;
}

function _metricOrUnavailable(value, formatter = _fmtMetricVal) {
  return typeof value === "number" && isFinite(value) ? formatter(value) : "not available";
}

function _dashMetric(value, formatter = _fmtMetricVal) {
  return typeof value === "number" && isFinite(value) ? formatter(value) : "—";
}

function _previewCacheKey(submissionId, challengeType) {
  return `${submissionId || "submission"}::${challengeType || "auto"}`;
}

function _previewSubmissionId(valResults) {
  if (valResults && valResults.length) return valResults[0].submission_id || state.submissionId || "";
  if (state.submissionId) return state.submissionId;
  const cachedIds = Object.keys(_scoreCache);
  return cachedIds.length ? cachedIds[0] : "";
}

function _previewShapeText(shape) {
  return Array.isArray(shape) && shape.length ? shape.join(" x ") : "not available";
}

function _previewVoxelText(voxelSize) {
  return Array.isArray(voxelSize) && voxelSize.length ? voxelSize.join(", ") : "not available";
}

function _previewStatusBadge(item) {
  return item?.preview_available
    ? statusPill("Preview available", "complete")
    : statusPill("Preview unavailable", "pending");
}

function _previewNote() {
  return `<p class="nifti-preview-note">For full medical image inspection, download the NIfTI file and open it in ITK-SNAP, FSLeyes, 3D Slicer, or another NIfTI viewer.</p>`;
}

function _storePreviewItems(manifest) {
  Object.keys(_previewItemsById).forEach((key) => delete _previewItemsById[key]);
  (manifest?.maps || []).forEach((item) => {
    if (item?.map_id) _previewItemsById[item.map_id] = item;
  });
}

function _renderPreviewCard(item) {
  const mapId = item.map_id || "";
  const thumb = item.preview_available && item.thumbnail_url
    ? `<img src="${escapeHtml(item.thumbnail_url)}" alt="Middle-slice preview for ${escapeHtml(item.file_name || "NIfTI map")}">`
    : `<div class="nifti-preview-placeholder">Preview unavailable</div>`;
  const previewDisabled = item.preview_available ? "" : " disabled";
  const fullPreview = item.preview_available && item.full_preview_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.full_preview_url)}" target="_blank" rel="noopener">Open full preview</a>`
    : `<span class="btn btn-secondary btn-sm is-disabled" aria-disabled="true">Open full preview</span>`;
  const download = item.download_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.download_url)}" title="Downloads the original submitted/result NIfTI map.">Download NIfTI for ITK-SNAP</a>`
    : `<span class="btn btn-secondary btn-sm is-disabled" aria-disabled="true" title="Download is not available for this file.">Download NIfTI for ITK-SNAP</span>`;
  return `<article class="nifti-preview-card" data-preview-map-id="${escapeHtml(mapId)}">
    <button type="button" class="nifti-preview-thumb" data-open-preview-map="${escapeHtml(mapId)}"${previewDisabled} aria-label="Preview ${escapeHtml(item.file_name || "NIfTI map")}">
      ${thumb}
    </button>
    <div class="nifti-preview-card-body">
      <div class="nifti-preview-card-head">
        <div>
          <div class="nifti-preview-file" title="${escapeHtml(item.file_name || "")}">${escapeHtml(item.file_name || "NIfTI map")}</div>
          <div class="nifti-preview-map">${escapeHtml(item.detected_map_type || "Unknown map")}</div>
        </div>
        ${_previewStatusBadge(item)}
      </div>
      <div class="nifti-preview-meta">
        ${_summaryMetric("Shape", _previewShapeText(item.shape))}
        ${_summaryMetric("Voxel size", _previewVoxelText(item.voxel_size))}
        ${_summaryMetric("Finite voxels", _dashMetric(item.finite_percent, (v) => `${_fmtMetricVal(v)}%`))}
      </div>
      ${item.preview_error ? `<p class="nifti-preview-error">${escapeHtml(item.preview_error)}</p>` : ""}
      <div class="nifti-preview-actions">
        <button type="button" class="btn btn-secondary btn-sm preview-open-btn" data-open-preview-map="${escapeHtml(mapId)}"${previewDisabled}>Preview</button>
        ${fullPreview}
        ${download}
      </div>
    </div>
  </article>`;
}

function _renderImagePreviewSection(manifest, options = {}) {
  const loading = options.loading === true;
  const submissionId = options.submissionId || manifest?.submission_id || "";
  const maps = manifest?.maps || [];
  const body = loading
    ? `<div class="nifti-preview-loading">Generating cached NIfTI previews…</div>`
    : maps.length
      ? `<div class="nifti-preview-list">${maps.map(_renderPreviewCard).join("")}</div>${_previewNote()}`
      : `<div class="nifti-preview-empty">No submitted/result NIfTI maps are available for preview yet.</div>${_previewNote()}`;
  return `<section id="summary-image-preview-section" class="summary-section summary-image-preview" data-submission-id="${escapeHtml(submissionId)}">
    <div class="summary-section-header">
      <span class="summary-section-kicker">Image Preview</span>
      <h2>Image Preview</h2>
      <p>Submitted/result NIfTI map previews.</p>
    </div>
    ${body}
  </section>`;
}

async function _loadAndRenderImagePreviews(submissionId, challengeType) {
  if (!submissionId) return;
  const key = _previewCacheKey(submissionId, challengeType);
  const section = el("summary-image-preview-section");
  if (_previewManifestCache[key]) {
    _storePreviewItems(_previewManifestCache[key]);
  }
  try {
    const url = new URL(`${API}/api/submissions/${encodeURIComponent(submissionId)}/previews`);
    if (challengeType) url.searchParams.set("challenge_type", challengeType);
    const resp = await fetch(url.toString());
    if (!resp.ok) throw new Error(`Preview request failed (${resp.status})`);
    const manifest = await resp.json();
    _previewManifestCache[key] = manifest;
    _storePreviewItems(manifest);
    const current = el("summary-image-preview-section");
    if (current && current.dataset.submissionId === String(submissionId)) {
      current.outerHTML = _renderImagePreviewSection(manifest, { submissionId });
    }
  } catch (err) {
    const current = el("summary-image-preview-section") || section;
    if (current && current.dataset.submissionId === String(submissionId)) {
      current.outerHTML = `<section id="summary-image-preview-section" class="summary-section summary-image-preview" data-submission-id="${escapeHtml(submissionId)}">
        <div class="summary-section-header">
          <span class="summary-section-kicker">Image Preview</span>
          <h2>Image Preview</h2>
          <p>Submitted/result NIfTI map previews.</p>
        </div>
        <div class="nifti-preview-empty">Preview unavailable: ${escapeHtml(err.message || String(err))}</div>
        ${_previewNote()}
      </section>`;
    }
  }
}

function _ensurePreviewModal() {
  let modal = el("nifti-preview-modal");
  if (modal) return modal;
  modal = document.createElement("div");
  modal.id = "nifti-preview-modal";
  modal.className = "nifti-preview-modal-backdrop";
  modal.hidden = true;
  modal.innerHTML = `<div class="nifti-preview-modal-panel" role="dialog" aria-modal="true" aria-labelledby="nifti-preview-title">
    <button type="button" class="nifti-preview-close" data-preview-close aria-label="Close preview">Close</button>
    <div id="nifti-preview-modal-content"></div>
  </div>`;
  document.body.appendChild(modal);
  return modal;
}

function _previewPlaneUrl(item, plane) {
  return item?.[`${plane}_url`] || "";
}

function _renderPreviewModalContent(item, plane = "axial") {
  const availablePlanes = ["axial", "coronal", "sagittal"].filter((p) => _previewPlaneUrl(item, p));
  const activePlane = availablePlanes.includes(plane) ? plane : (availablePlanes[0] || "axial");
  _activePreviewPlane = activePlane;
  const imageUrl = _previewPlaneUrl(item, activePlane);
  const tabs = availablePlanes.map((p) =>
    `<button type="button" class="${p === activePlane ? "is-active" : ""}" data-preview-plane="${escapeHtml(p)}">${escapeHtml(p[0].toUpperCase() + p.slice(1))}</button>`
  ).join("");
  const image = imageUrl
    ? `<img class="nifti-preview-modal-image" src="${escapeHtml(imageUrl)}" alt="${escapeHtml(activePlane)} preview for ${escapeHtml(item.file_name || "NIfTI map")}">`
    : `<div class="nifti-preview-modal-empty">Preview unavailable</div>`;
  const fullPreview = item.full_preview_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.full_preview_url)}" target="_blank" rel="noopener">Open full preview</a>`
    : "";
  const download = item.download_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.download_url)}">Download NIfTI for ITK-SNAP</a>`
    : "";
  return `<div class="nifti-preview-modal-header">
      <div>
        <div class="summary-section-kicker">Image Preview</div>
        <h2 id="nifti-preview-title">${escapeHtml(item.file_name || "NIfTI map")}</h2>
        <p>${escapeHtml(item.detected_map_type || "Unknown map")}</p>
      </div>
      ${_previewStatusBadge(item)}
    </div>
    ${tabs ? `<div class="nifti-preview-tabs">${tabs}</div>` : ""}
    <div class="nifti-preview-modal-grid">
      <div class="nifti-preview-modal-image-wrap">${image}</div>
      <div class="nifti-preview-modal-meta">
        ${_summaryMetric("Map type", item.detected_map_type || "Unknown")}
        ${_summaryMetric("Shape", _previewShapeText(item.shape))}
        ${_summaryMetric("Voxel size", _previewVoxelText(item.voxel_size))}
        ${_summaryMetric("Mean", _dashMetric(item.mean))}
        ${_summaryMetric("Std. deviation", _dashMetric(item.std))}
        ${_summaryMetric("Finite voxels", _dashMetric(item.finite_percent, (v) => `${_fmtMetricVal(v)}%`))}
        ${_summaryMetric("Negative voxels", _dashMetric(item.negative_percent, (v) => `${_fmtMetricVal(v)}%`))}
        ${item.preview_error ? `<p class="nifti-preview-error">${escapeHtml(item.preview_error)}</p>` : ""}
        <div class="nifti-preview-actions">${fullPreview}${download}<button type="button" class="btn btn-secondary btn-sm" data-preview-close>Close</button></div>
        ${_previewNote()}
      </div>
    </div>`;
}

function _openNiftiPreview(mapId, plane = "axial") {
  const item = _previewItemsById[mapId];
  if (!item || !item.preview_available) return;
  _activePreviewMapId = mapId;
  const modal = _ensurePreviewModal();
  const content = modal.querySelector("#nifti-preview-modal-content");
  if (content) content.innerHTML = _renderPreviewModalContent(item, plane);
  modal.hidden = false;
  document.body.classList.add("preview-modal-open");
  modal.querySelector("[data-preview-close]")?.focus();
}

function _closeNiftiPreview() {
  const modal = el("nifti-preview-modal");
  if (!modal) return;
  modal.hidden = true;
  document.body.classList.remove("preview-modal-open");
  _activePreviewMapId = null;
}

function _referenceStatusBadge(status) {
  const raw = status || "reference_not_available";
  const state = raw === "available" || raw === "compared" ? "complete"
    : raw === "partial_reference_scoring" ? "warning"
    : raw === "scoring_error" ? "error"
    : raw === "reference_not_available" ? "pending"
    : "ready";
  const label = raw === "available" ? "Complete"
    : raw === "partial_reference_scoring" ? "Partial reference scoring"
    : raw === "reference_not_available" ? "Reference unavailable"
    : raw === "scoring_error" ? "Scoring error"
    : raw;
  return statusPill(label, state);
}

function _overallSummaryStatus(mapSummary, valTotal, valFailed, scoredCount) {
  if (valTotal === 0) return { state: "pending", label: "Needs attention", refStatus: "not_started" };
  if (valFailed > 0) return { state: "error", label: "Needs attention", refStatus: mapSummary.referenceStatus };
  if (mapSummary.referenceStatus === "partial_reference_scoring") {
    return { state: "warning", label: "Partial reference scoring", refStatus: mapSummary.referenceStatus };
  }
  if (mapSummary.referenceStatus === "scoring_error") {
    return { state: "error", label: "Needs attention", refStatus: mapSummary.referenceStatus };
  }
  if (mapSummary.referenceComparedMapCount > 0) {
    return { state: "complete", label: "Complete", refStatus: mapSummary.referenceStatus };
  }
  if (scoredCount > 0 || mapSummary.mapCount > 0) {
    return { state: "pending", label: "QC only", refStatus: "reference_not_available" };
  }
  return { state: "complete", label: "Complete", refStatus: mapSummary.referenceStatus };
}

function _renderReferenceReportSection(mapSummary) {
  const wholeRows = mapSummary.referenceRows.filter((row) => row.scope === "whole map" || !row.scope);
  if (!wholeRows.length) return "";
  const maskRowsFor = (row) => mapSummary.referenceRows.filter((mask) =>
    mask.scope && mask.scope !== "whole map"
    && mask.submitted_file === row.submitted_file
    && (mask.detected_map_type || "") === (row.detected_map_type || "")
  );
  return `<section class="summary-section summary-reference-report">
    <div class="summary-section-header">
      <span class="summary-section-kicker">Reference-Based Scoring</span>
      <h2>Reference-Based Scoring ${helpTooltip("Reference metrics are calculated only when a matching private ground-truth map is available.", "Reference scoring status help")}</h2>
      <p>Available reference comparisons.</p>
    </div>
    <div class="summary-reference-list">
      ${wholeRows.map((row) => {
        const masks = maskRowsFor(row);
        return `<article class="summary-reference-card">
          <div class="summary-reference-card-head">
            <div>
              <div class="summary-reference-map">${escapeHtml(row.detected_map_type || "Unknown map")}</div>
              <div class="summary-reference-file" title="${escapeHtml(row.submitted_file || "")}">${escapeHtml(row.submitted_file || "submitted map")}</div>
            </div>
            ${_referenceStatusBadge(row.status || mapSummary.referenceStatus)}
          </div>
          <div class="summary-reference-metrics">
            ${_summaryMetric("Status", row.status || mapSummary.referenceStatus)}
            ${_summaryMetric("RMSE", _metricOrUnavailable(row.rmse))}
            ${_summaryMetric("MAE", _metricOrUnavailable(row.mae))}
            ${_summaryMetric("Bias", _metricOrUnavailable(row.bias))}
            ${_summaryMetric("CoV", _metricOrUnavailable(row.coefficient_of_variation))}
            ${_summaryMetric("Correlation", _metricOrUnavailable(row.correlation))}
          </div>
          ${masks.length ? `<details class="summary-mask-details">
            <summary>Mask / ROI metrics</summary>
            <div class="summary-mask-table-wrap">
              <table class="summary-mask-table">
                <thead><tr><th>Mask / ROI</th><th>Status</th><th>RMSE</th><th>MAE</th><th>Bias</th><th>CoV</th><th>Correlation</th></tr></thead>
                <tbody>${masks.map((mask) => `<tr>
                  <td>${escapeHtml(mask.scope || mask.mask_name || "mask")}</td>
                  <td>${escapeHtml(mask.status || "")}</td>
                  <td>${escapeHtml(_metricOrUnavailable(mask.rmse))}</td>
                  <td>${escapeHtml(_metricOrUnavailable(mask.mae))}</td>
                  <td>${escapeHtml(_metricOrUnavailable(mask.bias))}</td>
                  <td>${escapeHtml(_metricOrUnavailable(mask.coefficient_of_variation))}</td>
                  <td>${escapeHtml(_metricOrUnavailable(mask.correlation))}</td>
                </tr>`).join("")}</tbody>
              </table>
            </div>
          </details>` : ""}
        </article>`;
      }).join("")}
    </div>
  </section>`;
}

function _shapeText(shape) {
  return Array.isArray(shape) && shape.length ? shape.join(" x ") : "not available";
}

function _listText(values) {
  return Array.isArray(values) && values.length ? values.join(", ") : "None detected";
}

function _renderNiftiTechnicalTable(analyses) {
  const rows = analyses.flatMap((analysis) => Array.isArray(analysis.maps) ? analysis.maps : []);
  if (!rows.length) return `<p class="summary-muted">No NIfTI map metadata is available yet.</p>`;
  return `<div class="summary-tech-scroll"><table class="summary-metric-table summary-nifti-table">
    <thead><tr>
      <th>File</th><th>Map</th><th>Label</th><th>Units</th><th>Shape</th><th>Voxel size</th>
      <th>Data type</th><th>Affine/orientation</th><th>Total voxels</th><th>Finite voxels</th>
      <th>NaN</th><th>Inf</th><th>Mean</th><th>Median</th><th>Std. deviation</th>
      <th>Min</th><th>Max</th><th>Finite %</th><th>Negative %</th><th>CoV</th>
    </tr></thead>
    <tbody>${rows.map((item) => {
      const meta = item.metadata || {};
      const stats = item.stats || {};
      return `<tr>
        <td>${escapeHtml(item.file_name || "")}</td>
        <td>${escapeHtml(item.detected_map_type || "Unknown")}</td>
        <td>${escapeHtml(item.parameter_label || "")}</td>
        <td>${escapeHtml(item.units || "units not provided")}</td>
        <td>${escapeHtml(_shapeText(meta.shape))}</td>
        <td>${escapeHtml(Array.isArray(meta.voxel_size) ? meta.voxel_size.join(", ") : "not available")}</td>
        <td>${escapeHtml(meta.data_type || "not available")}</td>
        <td>${escapeHtml(meta.affine_orientation_summary || "not available")}</td>
        <td>${escapeHtml(meta.total_voxel_count ?? "not available")}</td>
        <td>${escapeHtml(meta.finite_voxel_count ?? "not available")}</td>
        <td>${escapeHtml(meta.nan_count ?? "not available")}</td>
        <td>${escapeHtml(meta.inf_count ?? "not available")}</td>
        <td>${escapeHtml(stats.mean == null ? "not available" : _fmtMetricVal(stats.mean))}</td>
        <td>${escapeHtml(stats.median == null ? "not available" : _fmtMetricVal(stats.median))}</td>
        <td>${escapeHtml(stats.standard_deviation == null ? "not available" : _fmtMetricVal(stats.standard_deviation))}</td>
        <td>${escapeHtml(stats.min == null ? "not available" : _fmtMetricVal(stats.min))}</td>
        <td>${escapeHtml(stats.max == null ? "not available" : _fmtMetricVal(stats.max))}</td>
        <td>${escapeHtml(stats.finite_percent == null ? "not available" : _fmtPercentValue(stats.finite_percent))}</td>
        <td>${escapeHtml(stats.negative_voxel_percent == null ? "not available" : _fmtPercentValue(stats.negative_voxel_percent))}</td>
        <td>${escapeHtml(stats.coefficient_of_variation == null ? "not available" : _fmtMetricVal(stats.coefficient_of_variation))}</td>
      </tr>`;
    }).join("")}</tbody>
  </table></div>`;
}

function _renderReferenceTechnicalTable(analyses) {
  const rows = _aggregateNiftiAnalyses(analyses).referenceRows;
  if (!rows.length) return `<p class="summary-muted">Reference map not available; QC metrics only.</p>`;
  return `<div class="summary-tech-scroll"><table class="summary-metric-table summary-reference-table">
    <thead><tr>
      <th>Submitted</th><th>Reference</th><th>Map</th><th>Scope</th><th>Mask</th><th>Status</th>
      <th>RMSE</th><th>MAE</th><th>Bias</th><th>CoV</th><th>Correlation</th>
      <th>Finite %</th><th>Voxels</th><th>Difference map</th>
    </tr></thead>
    <tbody>${rows.map((row) => `<tr>
      <td>${escapeHtml(row.submitted_file || "")}</td>
      <td>${escapeHtml(row.reference_file || "")}</td>
      <td>${escapeHtml(row.detected_map_type || "")}</td>
      <td>${escapeHtml(row.scope || "")}</td>
      <td>${escapeHtml(row.mask_name || "")}</td>
      <td>${escapeHtml(row.status || "")}</td>
      <td>${escapeHtml(row.rmse == null ? "not available" : _fmtMetricVal(row.rmse))}</td>
      <td>${escapeHtml(row.mae == null ? "not available" : _fmtMetricVal(row.mae))}</td>
      <td>${escapeHtml(row.bias == null ? "not available" : _fmtMetricVal(row.bias))}</td>
      <td>${escapeHtml(row.coefficient_of_variation == null ? "not available" : _fmtMetricVal(row.coefficient_of_variation))}</td>
      <td>${escapeHtml(row.correlation == null ? "not available" : _fmtMetricVal(row.correlation))}</td>
      <td>${escapeHtml(row.finite_voxel_percent == null ? "not available" : _fmtPercentValue(row.finite_voxel_percent))}</td>
      <td>${escapeHtml(row.voxel_count ?? "not available")}</td>
      <td>${escapeHtml(row.difference_map || "")}</td>
    </tr>`).join("")}</tbody>
  </table></div>`;
}

// Build HTML for a provider card inside the collapsed details section.
function _renderProviderCard(p) {
  const isOfficial = p.category === "official";
  const isDev      = p.category === "development";
  const isReady    = p.status === "ready" || p.status === "dev_data_available";

  const cardCls    = `score-provider-card ${isOfficial ? "spc-official" : "spc-dev"}`;
  const badgeCls   = isOfficial ? "spc-badge-official" : "spc-badge-dev";
  const badgeLabel = isDev ? "Development only" : "Official";
  const dotCls     = isReady && isOfficial ? "spc-dot-ready"
                   : isReady && isDev      ? "spc-dot-dev"
                   : "spc-dot-not-conf";

  const statusLabel = isReady ? (isDev ? "Test data available" : "Ready to score") : "Not configured";
  const labelCls    = isReady ? "spc-status-label" : "spc-status-label nc";

  const devNote = isDev
    ? `<p class="spc-warning" style="margin:4px 0 0">Development only · Not official challenge scoring</p>`
    : "";

  // Checklist: fixed required items, matched against missing list from backend
  const checkItems = [
    { label: "challengeScoring.py", matchKey: "script" },
    { label: "Reference data",      matchKey: "reference" },
    { label: "Mask files",          matchKey: "mask" },
    { label: "Generated Ktrans outputs", matchKey: "output" },
  ];
  const missingStr = (p.missing || []).join(" ").toLowerCase();
  const checkHtml = `<ul class="score-checklist" style="margin-top:8px">` +
    checkItems.map(({ label, matchKey }) => {
      const isMissing = missingStr.includes(matchKey);
      const iconCls   = isMissing ? "chk-icon chk-missing" : "chk-icon chk-ok";
      const rowCls    = isMissing ? "chk-row-missing" : "";
      return `<li class="${rowCls}"><span class="${iconCls}">${isMissing ? "✕" : "✓"}</span>`
           + `<span class="chk-label">${escapeHtml(label)}</span></li>`;
    }).join("") + `</ul>`;

  return `<div class="${cardCls}">
    <div class="spc-header">
      <div class="spc-title">${escapeHtml(p.provider_name)}</div>
      <span class="spc-category-badge ${badgeCls}">${badgeLabel}</span>
    </div>
    <div class="spc-status-row">
      <span class="spc-status-dot ${dotCls}"></span>
      <span class="${labelCls}">${statusLabel}</span>
    </div>
    ${devNote}
    ${checkHtml}
  </div>`;
}

// Update the main user-facing status card based on provider status.
// activeMode: "none" | "builtin" | "custom"
// packageName: display name of active custom package, or null
function _updateScoreStatusCard(provs, activeMode, packageName) {
  const titleEl = el("score-status-title");
  const subEl   = el("score-status-sub");
  const badgeEl = el("score-status-badge");
  const hintEl  = el("score-status-hint");
  const btnAll  = el("btn-score-all");
  const previewEl = el("score-metric-preview");

  const isConfigured = !!(activeMode && activeMode !== "none");

  if (isConfigured) {
    const existingPreview = _scoreMetricPreviewHtml();
    if (titleEl) titleEl.textContent = existingPreview ? "Scoring complete" : "Scoring is ready";
    if (previewEl) {
      previewEl.innerHTML = existingPreview;
      previewEl.style.display = existingPreview ? "" : "none";
    }

    const scorerName = packageName
      || (activeMode === "builtin" ? "Default OSIPI scoring" : "Custom scoring package");
    const pkgLabel = existingPreview
      ? "Metrics generated successfully."
      : `${scorerName} is active.`;
    if (subEl)   subEl.textContent  = pkgLabel;

    const badgeTxt = "Ready";
    if (badgeEl) { badgeEl.textContent = badgeTxt; badgeEl.className = "smc-badge smc-badge--ready"; }
    if (hintEl)  hintEl.textContent   = "";
    if (btnAll)  btnAll.disabled      = false;
  } else {
    if (btnAll)  btnAll.disabled      = true;
  }

  // Re-wire "Run Scoring" button (clone clears old listeners)
  if (btnAll && isConfigured) {
    const fresh = btnAll.cloneNode(true);
    fresh.disabled    = false;
    fresh.textContent = "Run Scoring";
    btnAll.replaceWith(fresh);
    fresh.addEventListener("click", async () => {
      const subs = _getKnownSubmissions();
      // Ensure table is visible
      const tc = el("score-table-card");
      if (tc) tc.style.display = "";
      if (!subs.length) return;
      setLoading(fresh, true, "Scoring");
      _initScoreProgress(subs.length);
      try {
        for (const sub of subs) {
          const sid       = sub.submission_id || sub;
          const challenge = sub.challenge_type || _getSessionChallengeType() || "dce";
          const mapType   = "Ktrans";
          await _runSingleScore(null, sid, challenge, mapType);
        }
      } finally {
        setLoading(fresh, false, "Run Scoring");
        _syncCompactProgress();
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Admin Scoring Setup Panel
// ═══════════════════════════════════════════════════════════════════════════

// Detect challenge type from the current session state.
function _getSessionChallengeType() {
  if (batchState.validationData && batchState.validationData.results) {
    const first = batchState.validationData.results[0];
    if (first && first.challenge_type) return first.challenge_type.toLowerCase();
  }
  return "dce"; // default
}

// Load active config from backend and sync radio buttons + badge.
async function _loadScoringSetup() {
  const badgeEl = el("scoring-admin-badge");
  try {
    const r = await fetch(`${API}/api/scoring/active-config`);
    if (!r.ok) return;
    const data = await r.json();
    const ct   = _getSessionChallengeType();
    const entry = (data.active_config || {})[ct] || { mode: "none" };
    const mode  = entry.mode || "none";

    // Set radio
    const radio = document.querySelector(`input[name="scoring-mode"][value="${mode}"]`);
    if (radio) radio.checked = true;

    // Show/hide custom section
    _onScoringModeChange(mode);

    // If custom, populate package list and selector
    if (mode === "custom") {
      await _loadInstalledPackages(ct, entry.package_id || null);
    }

    // Update badge
    if (badgeEl) {
      if (mode === "none") {
        badgeEl.textContent = "Not configured";
        badgeEl.className   = "scoring-admin-badge";
      } else if (mode === "builtin") {
        badgeEl.textContent = "Default OSIPI";
        badgeEl.className   = "scoring-admin-badge badge-ready";
      } else {
        const pkgs   = data.packages || [];
        const active = pkgs.find((p) => p.package_id === entry.package_id);
        badgeEl.textContent = active ? `${active.name} v${active.version}` : "Custom";
        badgeEl.className   = "scoring-admin-badge badge-custom";
      }
    }

    // Update builtin status line
    await _updateBuiltinStatus();
  } catch (_) { /* silently ignore */ }
}

// Show/hide the custom package section based on selected mode.
function _onScoringModeChange(mode) {
  const customSec = el("scoring-custom-section");
  if (!customSec) return;
  customSec.style.display = mode === "custom" ? "" : "none";
}

// Check built-in TF6.2 provider readiness and update mode description.
async function _updateBuiltinStatus() {
  const statusEl = el("scoring-builtin-status");
  if (!statusEl) return;
  try {
    const r = await fetch(`${API}/api/scoring-status`);
    const d = await r.json();
    const prov = (d.providers || []).find(
      (p) => p.provider_id === "osipi_tf62_dce_ktrans"
    );
    if (prov) {
      if (prov.status === "ready") {
        statusEl.textContent  = "✓ Reference data found — ready to score";
        statusEl.className    = "scoring-mode-status ok";
      } else {
        const missing = (prov.missing || []).join(", ");
        statusEl.textContent  = `Missing: ${missing || "reference data not configured"}`;
        statusEl.className    = "scoring-mode-status err";
      }
    }
  } catch (_) { /* silently ignore */ }
}

// Load and render the installed packages list.
async function _loadInstalledPackages(challengeType, activePackageId) {
  const listEl   = el("scoring-pkg-list");
  const selectEl = el("scoring-pkg-select");
  const wrapEl   = el("scoring-pkg-select-wrap");
  if (!listEl) return;

  let packages = [];
  try {
    const r = await fetch(`${API}/api/scoring/packages`);
    const d = await r.json();
    // Endpoint returns a bare array; tolerate a legacy {packages:[...]} too.
    packages = Array.isArray(d) ? d : (d.packages || []);
  } catch (_) {
    listEl.innerHTML = `<p style="font-size:0.75rem;color:var(--muted)">Could not load packages.</p>`;
    return;
  }

  if (packages.length === 0) {
    listEl.innerHTML = `<p style="font-size:0.75rem;color:var(--muted);margin:4px 0">No packages installed yet. Upload a scoring package ZIP above.</p>`;
    if (wrapEl) wrapEl.style.display = "none";
    return;
  }

  // Render package cards
  listEl.innerHTML = packages.map((pkg) => {
    const ready    = pkg.status && pkg.status.ready;
    const badgeCls = ready ? "scoring-pkg-badge" : "scoring-pkg-badge not-ready";
    const badgeTxt = ready ? "Ready" : "Incomplete";
    const isSel    = pkg.package_id === activePackageId;
    return `
    <div class="scoring-pkg-item${isSel ? " pkg-selected" : ""}" data-pkg-id="${escapeHtml(pkg.package_id)}">
      <div class="scoring-pkg-info">
        <div class="scoring-pkg-name">${escapeHtml(pkg.name)}</div>
        <div class="scoring-pkg-meta">
          v${escapeHtml(pkg.version)} · ${escapeHtml(pkg.challenge_type.toUpperCase())}
          ${pkg.map_type ? " · " + escapeHtml(pkg.map_type) : ""}
          ${pkg.description ? " — " + escapeHtml(pkg.description.slice(0, 80)) : ""}
        </div>
      </div>
      <div class="scoring-pkg-actions">
        <span class="${badgeCls}">${badgeTxt}</span>
        <button type="button" class="btn btn-danger scoring-pkg-remove-btn" data-pkg-id="${escapeHtml(pkg.package_id)}">Remove</button>
      </div>
    </div>`;
  }).join("");

  // Populate select
  if (selectEl) {
    selectEl.innerHTML = packages.map((pkg) =>
      `<option value="${escapeHtml(pkg.package_id)}"${pkg.package_id === activePackageId ? " selected" : ""}>
        ${escapeHtml(pkg.name)} v${escapeHtml(pkg.version)} (${escapeHtml(pkg.challenge_type.toUpperCase())})
      </option>`
    ).join("");
    if (wrapEl) wrapEl.style.display = "";
  }
}

// Save the current scoring setup selection to the backend.
async function _saveScoringSetup() {
  const btn    = el("scoring-setup-save-btn");
  const msgEl  = el("scoring-setup-msg");
  const radio  = document.querySelector('input[name="scoring-mode"]:checked');
  const mode   = radio ? radio.value : "none";
  const ct     = _getSessionChallengeType();

  let packageId = null;
  if (mode === "custom") {
    const sel = el("scoring-pkg-select");
    packageId = sel ? sel.value : null;
    if (!packageId) {
      if (msgEl) { msgEl.textContent = "Select a package first."; msgEl.className = "scoring-setup-msg err"; msgEl.style.display = ""; }
      return;
    }
  }

  if (btn) setLoading(btn, true, "Applying");

  try {
    const r = await fetch(`${API}/api/scoring/set-active`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({ challenge_type: ct, mode, package_id: packageId }),
    });
    const d = await r.json();
    if (!r.ok) throw new Error(d.detail || "Request failed");

    if (msgEl) {
      const label = mode === "none" ? "Scoring disabled" : mode === "builtin" ? "Default OSIPI scoring configured" : "Custom package configured";
      msgEl.textContent = `✓ ${label}`;
      msgEl.className   = "scoring-setup-msg ok";
      msgEl.style.display = "";
    }

    // Refresh the status card and badge
    await _loadScoringSetup();
    await renderScoreStep();
  } catch (err) {
    if (msgEl) { msgEl.textContent = `Error: ${err.message}`; msgEl.className = "scoring-setup-msg err"; msgEl.style.display = ""; }
  } finally {
    if (btn) setLoading(btn, false, "Apply Configuration");
    _syncCompactProgress();
  }
}

// Wire up radio change, file upload, package remove, and save button.
(function _wireScoringSetup() {
  // Radio buttons — show/hide custom section
  document.querySelectorAll('input[name="scoring-mode"]').forEach((radio) => {
    radio.addEventListener("change", () => {
      _onScoringModeChange(radio.value);
      if (radio.value === "custom") {
        _loadInstalledPackages(_getSessionChallengeType(), null);
      }
    });
  });

  // Package file upload
  const fileInput = el("scoring-pkg-input");
  if (fileInput) {
    fileInput.addEventListener("change", async () => {
      const file = fileInput.files[0];
      if (!file) return;
      const statusEl = el("scoring-pkg-upload-status");
      if (statusEl) { statusEl.textContent = "Uploading…"; statusEl.className = "scoring-upload-status"; }

      const fd = new FormData();
      fd.append("file", file);
      try {
        const r = await fetch(`${API}/api/scoring/packages/upload`, { method: "POST", body: fd });
        const d = await r.json();
        if (!r.ok) throw new Error(d.detail || "Upload failed");
        if (statusEl) { statusEl.textContent = `✓ Installed: ${d.manifest?.name || d.package_id}`; statusEl.className = "scoring-upload-status ok"; }
        await _loadInstalledPackages(_getSessionChallengeType(), d.package_id);
        fileInput.value = "";
      } catch (err) {
        if (statusEl) { statusEl.textContent = `Error: ${err.message}`; statusEl.className = "scoring-upload-status err"; }
      }
    });
  }

  // Package remove (delegated)
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".scoring-pkg-remove-btn");
    if (!btn) return;
    const pkgId = btn.dataset.pkgId;
    if (!pkgId) return;
    if (!confirm(`Remove scoring package "${pkgId}"?`)) return;
    try {
      const r = await fetch(`${API}/api/scoring/packages/${encodeURIComponent(pkgId)}`, { method: "DELETE" });
      if (!r.ok) { const d = await r.json(); throw new Error(d.detail || "Remove failed"); }
      await _loadInstalledPackages(_getSessionChallengeType(), null);
    } catch (err) {
      alert(`Failed to remove package: ${err.message}`);
    }
  });

  // Save button
  const saveBtn = el("scoring-setup-save-btn");
  if (saveBtn) saveBtn.addEventListener("click", _saveScoringSetup);
})();

// ── renderScoreStep() ─────────────────────────────────────────────────────────

async function renderScoreStep() {
  unlockStep("score");
  loadLeaderboard();

  // Load admin scoring setup first (determines active mode)
  await _loadScoringSetup();

  // ── 1. Determine active scoring mode + package name ──────────────────────────
  let activeMode      = "none";
  let activePackageName = null;
  try {
    const r = await fetch(`${API}/api/scoring/active-config`);
    if (r.ok) {
      const d   = await r.json();
      const ct  = _getSessionChallengeType();
      const entry = (d.active_config || {})[ct] || {};
      activeMode  = entry.mode || "none";
      // Resolve human-readable package name for custom mode
      if (activeMode === "custom" && entry.package_id) {
        const pkgs = d.packages || [];
        const pkg  = pkgs.find((p) => p.package_id === entry.package_id);
        if (pkg) activePackageName = pkg.name + (pkg.version ? ` v${pkg.version}` : "");
      }
    }
  } catch (_) { /* default to none */ }

  const notConfiguredCard = el("score-not-configured-card");
  const statusCard        = el("score-status-card");
  const tableCard         = el("score-table-card");

  if (activeMode === "none") {
    // ── Not configured view ───────────────────────────────────────────────────
    if (notConfiguredCard) notConfiguredCard.style.display = "";
    if (statusCard)        statusCard.style.display        = "none";
    if (tableCard)         tableCard.style.display         = "none";
    const subs = _getKnownSubmissions();
    await Promise.all(subs.map(async (sub) => {
      const sid = sub.submission_id || sub;
      const ct  = sub.challenge_type || getChallengeType() || "dce";
      try {
        const r = await fetch(`${API}/api/scoring-status?submission_id=${encodeURIComponent(sid)}&challenge_type=${encodeURIComponent(ct)}&map_type=Ktrans`);
        const d = await r.json();
        _applyScoreStatus(sid, d);
      } catch (_) { /* Summary can still render validation/export state. */ }
    }));
    saveSessionState();
    _syncCompactProgress();
    _refreshWizardFooter();
    return;
  }

  // ── 2. Scoring is configured — show ready card ───────────────────────────────
  if (notConfiguredCard) notConfiguredCard.style.display = "none";
  if (statusCard)        statusCard.style.display        = "";
  // Note: score-provider-details is now inside the admin <details> panel —
  // it stays hidden unless the user opens the admin section.

  // Fetch providers for the advanced details panel (populated lazily)
  const grid = el("score-provider-grid");
  if (grid) grid.innerHTML = `<p style="font-size:0.78rem;color:var(--muted);margin:0">Loading…</p>`;

  let provs = [];
  try {
    const r = await fetch(`${API}/api/scoring-status`);
    const d = await r.json();
    provs   = d.providers || [];
  } catch (_) { /* ignore */ }

  _updateScoreStatusCard(provs, activeMode, activePackageName);

  if (grid) {
    grid.innerHTML = provs.length
      ? provs.map(_renderProviderCard).join("")
      : `<p style="font-size:0.78rem;color:var(--muted);margin:0">No providers found.</p>`;
  }

  saveSessionState();
  _syncCompactProgress();

  // ── 3. Submission scoring rows (hidden until scoring runs) ──────────────────
  const tbody = el("score-table-body");
  if (!tableCard || !tbody) {
    _refreshWizardFooter();
    return;
  }

  const subs = _getKnownSubmissions();
  if (!subs.length) {
    _refreshWizardFooter();
    return;
  }

  tableCard.style.display = "none";
  tbody.innerHTML = "";

  for (const sub of subs) {
    const sid  = sub.submission_id || sub;
    const name = sub.display_name  || sid;
    const ct   = sub.challenge_type || getChallengeType() || "dce";
    tbody.insertAdjacentHTML("beforeend", _buildScoreRow(sid, name, ct));
  }

  for (const sub of subs) {
    const sid = sub.submission_id || sub;
    const ct  = sub.challenge_type || getChallengeType() || "dce";
    _fetchAndUpdateScoreStatus(sid, ct);
  }
  _refreshWizardFooter();
}

// Build an HTML score table row for a given submission.
// Metric pills are hidden until actual scored values are available.
function _buildScoreRow(sid, displayName, challengeType) {
  const safeSid  = escapeHtml(sid);
  const safeChCt = escapeHtml(challengeType || "dce");
  return `
  <tr class="sc-row-wrap" data-sub-id="${safeSid}"
      data-score-status="not_checked"
      data-challenge="${safeChCt}" data-map-type="Ktrans">
    <td class="sc-col-sub">${escapeHtml(displayName || sid)}</td>
    <td class="sc-col-status">
      <span class="ss-badge ss-not-conf">Checking…</span>
    </td>
    <td class="sc-col-metrics">
      <div class="sc-metrics-row" id="sc-metrics-${safeSid}">
        <span class="sc-metrics-placeholder">—</span>
      </div>
    </td>
    <td class="sc-col-artifacts" id="sc-artifacts-${safeSid}">—</td>
    <td class="sc-col-action">
      <button type="button" class="btn btn-secondary sc-score-btn btn-sm"
              data-sub-id="${safeSid}"
              data-challenge="${safeChCt}" data-map-type="Ktrans"
              disabled>Score</button>
    </td>
  </tr>
  <tr class="sc-row-detail-row" style="display:none">
    <td colspan="5">
      <div class="sc-row-detail" id="sc-detail-${safeSid}"></div>
    </td>
  </tr>`;
}

// Fetch /api/scoring-status for one submission and update its row.
async function _fetchAndUpdateScoreStatus(sid, challengeType) {
  const ct = challengeType || getChallengeType() || "dce";
  try {
    const r    = await fetch(`${API}/api/scoring-status?submission_id=${encodeURIComponent(sid)}&challenge_type=${encodeURIComponent(ct)}&map_type=Ktrans`);
    const data = await r.json();
    _applyScoreStatus(sid, data);
  } catch (err) {
    _applyScoreStatus(sid, { status: "not_configured", message: "Could not fetch status: " + err.message });
  }
}

// Apply a status response to a row — sets badge text, enables/disables Score button.
function _applyScoreStatus(sid, data) {
  const row = [...document.querySelectorAll(".sc-row-wrap")]
    .find((r) => r.dataset.subId === sid);
  _cacheScoreStatus(sid, data, row);
  if (!row) { _syncCompactProgress(); return; }

  const status = data.status || "not_configured";
  row.dataset.scoreStatus = status;

  let badgeCls, badgeTxt;
  switch (status) {
    case "ready":          badgeCls = "ss-ready";    badgeTxt = "Ready to score"; break;
    case "scored":         badgeCls = "ss-scored";   badgeTxt = "Scored"; break;
    case "failed":         badgeCls = "ss-failed";   badgeTxt = "Scoring failed"; break;
    case "not_configured": badgeCls = "ss-not-conf"; badgeTxt = "Needs setup"; break;
    case "not_ready":      badgeCls = "ss-not-conf"; badgeTxt = "Incomplete"; break;
    default:               badgeCls = "ss-not-conf"; badgeTxt = "—"; break;
  }

  const badge = row.querySelector(".ss-badge");
  if (badge) { badge.className = `ss-badge ${badgeCls}`; badge.textContent = badgeTxt; }

  // Enable Score button only when ready or retry
  const scoreBtn = row.querySelector(".sc-score-btn");
  if (scoreBtn) {
    scoreBtn.disabled = !(status === "ready" || status === "scored" || status === "failed");
  }

  // If already scored, populate metrics and cache for summary step
  if (status === "scored") {
    const result = _scorePayload(data);
    _applyMetrics(sid, result.metrics || {});
    _applyArtifacts(sid, result.artifacts || []);
    const tableCard = el("score-table-card");
    if (tableCard) tableCard.style.display = "";
    _enableScoringExport();
  }
  // Cache score result for summary step (works for both "scored" and "failed")
  if (status === "scored" || status === "failed") {
    const tableCard = el("score-table-card");
    if (tableCard) tableCard.style.display = "";
    _cacheScoreStatus(sid, data, row);
  }

  // Populate detail drawer — only show it for scored/failed/missing, NOT for bare "not_configured"
  const detail    = el(`sc-detail-${sid}`);
  const detailRow = detail?.closest("tr.sc-row-detail-row");
  if (detail) {
    const missing = data.missing || [];
    const msg     = data.message || "";

    if (status === "scored" || status === "failed") {
      // Show full detail for scored/failed
      const misHtml = missing.length
        ? `<details style="margin-top:6px"><summary style="font-size:0.73rem;cursor:pointer;color:var(--muted)">Prerequisites checklist</summary>`
          + `<ul class="sc-missing-list">${missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul></details>`
        : "";
      detail.innerHTML = msg ? `<p style="font-size:0.73rem;margin:0 0 4px">${escapeHtml(msg)}</p>${misHtml}` : misHtml;
      if (detailRow) detailRow.style.display = "";
    } else if (status === "not_ready" && missing.length > 0) {
      // Show missing prereqs collapsed
      detail.innerHTML = `<details><summary style="font-size:0.73rem;cursor:pointer;color:var(--muted)">Scoring needs additional setup — expand to see details</summary>`
        + `<ul class="sc-missing-list">${missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul></details>`;
      if (detailRow) detailRow.style.display = "";
    } else {
      // not_configured or generic — don't show a noisy detail row
      detail.innerHTML = "";
      if (detailRow) detailRow.style.display = "none";
    }
  }
  _syncCompactProgress();
}

function _applyMetrics(sid, metrics) {
  // Prefer the ID-based lookup (works after _buildScoreRow with new template)
  const metRow = el(`sc-metrics-${sid}`) || (() => {
    const row = [...document.querySelectorAll(".sc-row-wrap")].find((r) => r.dataset.subId === sid);
    return row ? row.querySelector(".sc-metrics-row") : null;
  })();
  if (!metRow) return;

  // Show ONLY numeric metric values (RMSE, CoV, finite %, …). String metadata
  // such as "package" is never shown as a metric. Works for both the official
  // DCE metrics and flattened QC/demo metrics from custom packages.
  const numeric = _numericMetricEntries(metrics);

  if (numeric.length === 0) {
    metRow.innerHTML = `<span class="sc-metric-pill">No numeric metrics</span>`;
    return;
  }

  // Prefer the well-known official keys first, then any remaining numeric keys.
  const ordered = [
    ...numeric.filter(([k]) => _SC_METRIC_KEYS.includes(k)),
    ...numeric.filter(([k]) => !_SC_METRIC_KEYS.includes(k)),
  ].slice(0, 8);

  metRow.innerHTML = ordered.map(([k, v]) =>
    `<span class="sc-metric-pill has-value">${escapeHtml(_metricLabel(k))}: ${escapeHtml(_fmtMetricVal(v))}</span>`
  ).join("");
}

function _applyArtifacts(sid, artifacts) {
  const cell = el(`sc-artifacts-${sid}`);
  if (!cell) return;
  if (!artifacts || artifacts.length === 0) { cell.textContent = "—"; return; }
  cell.textContent = `${artifacts.length} file${artifacts.length > 1 ? "s" : ""}`;
}

// Return the list of known submissions for the current session.
// batchState.validationData is an object {results:[...], batch_id:...}, not an array.
function _getKnownSubmissions() {
  const results = batchState.validationData && batchState.validationData.results;
  if (results && results.length > 0) {
    return results.map((r) => ({
      submission_id:  r.submission_id,
      display_name:   r.source_folder || r.submission_id,
      challenge_type: r.challenge_type || getChallengeType() || "dce",
    }));
  }
  if (state.submissionId) {
    return [{
      submission_id:  state.submissionId,
      display_name:   state.submissionId,
      challenge_type: getChallengeType() || "dce",
    }];
  }
  return [];
}

// ── Score row update after scoring ───────────────────────────────────────────

function _updateScoreRow(subId, data) {
  // Use the unified status applier, then additionally handle artifacts
  _applyScoreStatus(subId, data);
  if (data.status === "scored") {
    _applyMetrics(subId, data.metrics || {});
    _applyArtifacts(subId, data.artifacts || []);
    _enableScoringExport();
  }
}

// ── Single score execution ────────────────────────────────────────────────────

async function _runSingleScore(btn, subId, challenge, mapType) {
  const idleLabel = btn ? (btn.textContent.trim() || "Score") : "";
  if (btn) setLoading(btn, true, "Scoring");

  const wrap = [...document.querySelectorAll(".sc-row-wrap")]
    .find((w) => w.dataset.subId === subId);
  if (wrap) {
    wrap.dataset.scoreStatus = "scoring";
    const badge = wrap.querySelector(".ss-badge");
    if (badge) { badge.className = "ss-badge ss-scoring"; badge.textContent = "Scoring…"; }
  }

  try {
    const resp = await fetch(`${API}/api/score`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ submission_id: subId, challenge_type: challenge, map_type: mapType }),
    });
    const data = await resp.json();
    _updateScoreRow(subId, data);
    _tickScoreProgress(data.status === "scored", data.status === "not_configured");
  } catch (err) {
    _updateScoreRow(subId, { status: "failed", message: "Network error: " + err.message, metrics: {} });
    _tickScoreProgress(false, false);
  } finally {
    if (btn) setLoading(btn, false, idleLabel || "Score");
    _syncCompactProgress();
  }
}

// ── Delegation: Score button (.sc-score-btn) ──────────────────────────────────

document.addEventListener("click", async (e) => {
  const btn = e.target.closest(".sc-score-btn");
  if (!btn) return;
  e.stopPropagation();
  const subId   = btn.dataset.subId;
  const chall   = btn.dataset.challenge || "dce";
  const mapType = btn.dataset.mapType   || "Ktrans";
  if (!subId) return;
  await _runSingleScore(btn, subId, chall, mapType);
});

// ── Delegation: Details toggle in score rows (.sc-detail-btn) ─────────────────

document.addEventListener("click", (e) => {
  const btn = e.target.closest(".sc-detail-btn");
  if (!btn) return;
  const wrap = btn.closest(".sc-row-wrap");
  if (!wrap) return;
  e.stopPropagation();
  const drawer   = wrap.querySelector(".sc-row-detail");
  if (!drawer) return;
  const isHidden = drawer.style.display === "none";
  drawer.style.display = isHidden ? "" : "none";
  btn.textContent = isHidden ? "Close" : "Details";
});

// ── Delegation: NIfTI preview cards and modal controls ───────────────────────

document.addEventListener("click", (e) => {
  const trigger = e.target.closest("[data-open-preview-map]");
  if (trigger) {
    e.preventDefault();
    const mapId = trigger.getAttribute("data-open-preview-map");
    if (mapId) _openNiftiPreview(mapId);
    return;
  }

  const planeBtn = e.target.closest("[data-preview-plane]");
  if (planeBtn && _activePreviewMapId) {
    e.preventDefault();
    const item = _previewItemsById[_activePreviewMapId];
    const plane = planeBtn.getAttribute("data-preview-plane") || _activePreviewPlane;
    const content = el("nifti-preview-modal")?.querySelector("#nifti-preview-modal-content");
    if (item && content) content.innerHTML = _renderPreviewModalContent(item, plane);
    return;
  }

  if (e.target.closest("[data-preview-close]")) {
    e.preventDefault();
    _closeNiftiPreview();
    return;
  }

  const modal = el("nifti-preview-modal");
  if (modal && !modal.hidden && e.target === modal) {
    _closeNiftiPreview();
  }
});

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") _closeNiftiPreview();
});

// "Score All" button is wired dynamically inside renderScoreStep().

// ── renderSummaryStep() ───────────────────────────────────────────────────────

function renderSummaryStep() {
  unlockStep("summary");
  unlockStep("export");
  // Keep the action row in sync so Continue to Export is enabled the moment we arrive.
  if (typeof _refreshWizardFooter === "function") _refreshWizardFooter();

  const container = el("summary-cards");
  if (!container) return;

  // ── Validation ────────────────────────────────────────────────────────────
  const valResults = (batchState.validationData && batchState.validationData.results) || [];
  const valTotal   = valResults.length;
  const valPassed  = valResults.filter((r) => r.passed).length;
  const valFailed  = valResults.filter((r) => !r.passed).length;
  const valWarned  = valResults.filter((r) => r.passed && issueCount(r, "warnings") > 0).length;
  const totalErrors   = valResults.reduce((a, r) => a + issueCount(r, "errors"), 0);
  const totalWarnings = valResults.reduce((a, r) => a + issueCount(r, "warnings"), 0);

  // ── Execution ─────────────────────────────────────────────────────────────
  let execRan = 0, execSkipped = 0, execFailed = 0, execCannot = 0;
  for (const r of valResults) {
    const rr = (r.run_readiness || "").replace(/_/g, "-");
    if (rr === "result-only") { execSkipped++; continue; }
    if (rr === "cannot-run" || rr === "not-runnable") { execCannot++; continue; }
    const ex = _execSummaries[r.submission_id];
    if (!ex) continue;
    if (ex.passed || ex.status === "pass") execRan++;
    else if (ex.status === "failed") execFailed++;
  }

  // ── Scoring (numeric metrics only) ──────────────────────────────────────────
  const scoredEntries = Object.values(_scoreCache).filter((s) => s.status === "scored");
  const scoredCount   = scoredEntries.length;
  const scoringFailed = Object.values(_scoreCache).filter((s) => s.status === "failed").length;
  const scoreCardEl   = el("score-status-card");
  const scoringConfigured = scoredCount > 0 || scoringFailed > 0 ||
    !!(scoreCardEl && scoreCardEl.style.display !== "none");
  const anyOfficial   = scoredEntries.some((s) => s.official === true);
  const nonOfficialScored = scoredCount > 0 && !anyOfficial;

  const _agg = {};
  scoredEntries.forEach((s) => {
    _numericMetricEntries(s.metrics || {}).forEach(([k, v]) => { (_agg[k] = _agg[k] || []).push(v); });
  });
  const metrics = {};
  Object.entries(_agg).forEach(([k, arr]) => { metrics[k] = arr.reduce((a, b) => a + b, 0) / arr.length; });
  const analysisEntries = _niftiAnalysisEntries();
  const mapSummary = _aggregateNiftiAnalyses(analysisEntries);

  // ── Final report sections ──────────────────────────────────────────────────
  const overall = _overallSummaryStatus(mapSummary, valTotal, valFailed, scoredCount);
  const previewSubmissionId = _previewSubmissionId(valResults);
  const submissionName = valResults.length === 1
    ? submissionDisplayName(valResults[0], valResults[0].submission_id || "Submission")
    : valResults.length > 1 ? `${valResults.length} submissions`
      : scoredEntries[0]?.displayName || state.submissionId || "not available";
  const challengeType = valResults[0]?.challenge_type || getChallengeType() || "not available";
  const previewKey = _previewCacheKey(previewSubmissionId, challengeType);
  const previewManifest = _previewManifestCache[previewKey] || null;
  if (previewManifest) _storePreviewItems(previewManifest);
  const comparedMapText = mapSummary.referenceComparedMapCount > 0
    ? `${mapSummary.referenceComparedMapCount}/${mapSummary.referenceMapCount || mapSummary.referenceComparedMapCount}`
    : String(mapSummary.mapCount || 0);
  const referenceStatusText = mapSummary.referenceMapStatuses.length
    ? mapSummary.referenceMapStatusText
    : (mapSummary.referenceStatus === "reference_not_available" ? "Reference unavailable" : mapSummary.referenceStatus || "reference_not_available");
  const referenceUnavailableNote = mapSummary.referenceComparedMapCount > 0
    ? ""
    : `<p class="summary-qc-only-note">Reference scoring unavailable — showing QC metrics only.</p>`;
  const referenceBadgeHtml = mapSummary.referenceComparedMapCount > 0
    || mapSummary.referenceStatus === "partial_reference_scoring"
    || mapSummary.referenceStatus === "scoring_error"
    ? _referenceStatusBadge(mapSummary.referenceStatus)
    : statusPill("QC only", "pending");
  const finalMetricRows = mapSummary.referenceComparedMapCount > 0
    ? `
      ${_summaryMetric("RMSE", _metricOrUnavailable(mapSummary.referenceMetrics.rmse))}
      ${_summaryMetric("MAE", _metricOrUnavailable(mapSummary.referenceMetrics.mae))}
      ${_summaryMetric("Bias", _metricOrUnavailable(mapSummary.referenceMetrics.bias))}
      ${_summaryMetric("CoV", _metricOrUnavailable(mapSummary.referenceMetrics.coefficientOfVariation))}
    `
    : "";
  const finalOutputHtml = `
    <section class="summary-section summary-final-output">
      <div class="summary-section-header">
        <span class="summary-section-kicker">Final Output</span>
        <div class="summary-final-title-row">
          <h2>Final Output</h2>
          <div class="summary-final-badges">
            ${statusPill(overall.label, overall.state)}
            ${referenceBadgeHtml}
          </div>
        </div>
        ${referenceUnavailableNote}
      </div>
      <div class="summary-final-grid">
        ${_summaryMetric("Overall status", overall.label)}
        ${_summaryMetric("Submission name", submissionName)}
        ${_summaryMetric("Challenge type", challengeType)}
        ${_summaryMetric("Detected map types", _listText(mapSummary.detected))}
        ${_summaryMetric("Number of maps scored", comparedMapText)}
        ${_summaryMetric("Reference scoring status", referenceStatusText)}
      </div>
      ${finalMetricRows ? `<div class="summary-final-metrics">${finalMetricRows}</div>` : ""}
    </section>`;

  const qcRows = [
    _summaryMetric("Finite voxels", `${_fmtPercentValue(mapSummary.finitePercent)} (${mapSummary.finiteVoxelCount}/${mapSummary.totalVoxelCount})`, "good"),
    _summaryMetric("Negative voxels", _fmtPercentValue(mapSummary.negativePercent)),
    _summaryMetric("NaN count", String(mapSummary.nanCount)),
    _summaryMetric("Inf count", String(mapSummary.infCount)),
    _summaryMetric("Coefficient of variation", _metricOrUnavailable(mapSummary.coefficientOfVariation)),
    _summaryMetric("Standard deviation", _metricOrUnavailable(mapSummary.standardDeviation)),
    _summaryMetric("Map count", String(mapSummary.mapCount || 0)),
  ];
  if (typeof mapSummary.meansByType.CBF === "number") qcRows.push(_summaryMetric("Mean CBF", `${_fmtMetricVal(mapSummary.meansByType.CBF)} mL/100g/min`));
  if (typeof mapSummary.meansByType.ATT === "number") qcRows.push(_summaryMetric("Mean ATT", `${_fmtMetricVal(mapSummary.meansByType.ATT)} seconds`));
  if (typeof mapSummary.meansByType.Ktrans === "number") qcRows.push(_summaryMetric("Mean Ktrans", `${_fmtMetricVal(mapSummary.meansByType.Ktrans)} min^-1`));
  const qcSummaryHtml = `
    <section class="summary-section summary-qc-summary">
      <div class="summary-section-header">
        <span class="summary-section-kicker">Key QC Summary</span>
        <h2>Key QC Summary ${helpTooltip("QC metrics describe map validity and statistics. They are not official OSIPI scores.", "QC metrics help")}</h2>
        <p>Map validity and statistics.</p>
      </div>
      <div class="summary-vertical-metrics">${qcRows.join("")}</div>
    </section>`;

  const imagePreviewHtml = _renderImagePreviewSection(previewManifest, {
    loading: !!previewSubmissionId && !previewManifest,
    submissionId: previewSubmissionId,
  });

  const referenceReportHtml = _renderReferenceReportSection(mapSummary);

  // ── Export readiness (stacked checklist) ────────────────────────────────────
  const hasDifferenceMaps = mapSummary.referenceRows.some((row) => row.difference_map);
  const checklist = [
    { label: "Validation CSV", ready: valTotal > 0 },
    { label: "Scoring CSV",    ready: scoredCount > 0 },
    { label: "Combined CSV",   ready: valTotal > 0 },
    { label: "HTML Report",    ready: valTotal > 0 },
  ];
  if (mapSummary.referenceComparedMapCount > 0) {
    checklist.push({ label: "Reference scoring JSON/CSV", ready: true });
  }
  if (hasDifferenceMaps) {
    checklist.push({ label: "Difference maps", ready: true });
  }
  const checklistHtml = `
    <section class="summary-section summary-export-checklist">
      <div class="summary-section-header">
        <span class="summary-section-kicker">Export Readiness</span>
        <h2>Export Readiness</h2>
        <p>Files available from the Export step.</p>
      </div>
      <ul class="summary-check-grid">
        ${checklist.map((c) => `
          <li class="summary-check-item ${c.ready ? "is-ready" : "is-muted"}">
            <span class="summary-check-mark" aria-hidden="true">${c.ready ? "✓" : "○"}</span>
            <span class="summary-check-name">${escapeHtml(c.label)}</span>
            <span class="summary-check-state">${c.ready ? "Ready" : "—"}</span>
          </li>`).join("")}
      </ul>
        </section>`;

  // ── Collapsed technical details (issues, raw metrics, scorer note) ──────────
  const qcSummaryJson = {
    total_voxel_count: mapSummary.totalVoxelCount,
    finite_voxel_count: mapSummary.finiteVoxelCount,
    finite_voxel_percent: mapSummary.finitePercent,
    negative_voxel_count: mapSummary.negativeVoxelCount,
    negative_voxel_percent: mapSummary.negativePercent,
    nan_count: mapSummary.nanCount,
    inf_count: mapSummary.infCount,
    coefficient_of_variation: mapSummary.coefficientOfVariation,
    standard_deviation: mapSummary.standardDeviation,
    detected_map_types: mapSummary.detected,
    means_by_map_type: mapSummary.meansByType,
    reference_based_scoring_available: mapSummary.referenceBasedScoringAvailable,
    reference_status: mapSummary.referenceStatus,
  };
  const issuesHtml = valResults.map((r) => {
    const name = escapeHtml(submissionDisplayName(r, r.submission_id || "submission"));
    const errs = dedupeMessages((r.errors || []).map(simplifyMessage));
    const warns = dedupeMessages((r.warnings || []).map(simplifyMessage));
    if (!errs.length && !warns.length) return `<div class="summary-detail-sub"><b>${name}</b> — no issues.</div>`;
    const ehtml = errs.map((e) => `<li class="sdi-err">${escapeHtml(e)}</li>`).join("");
    const whtml = warns.map((w) => `<li class="sdi-warn">${escapeHtml(w)}</li>`).join("");
    return `<div class="summary-detail-sub"><b>${name}</b><ul class="summary-detail-list">${ehtml}${whtml}</ul></div>`;
  }).join("");

  let rawMetricTable = "";
  const numericKeys = [...new Set(scoredEntries.flatMap((s) => _numericMetricEntries(s.metrics || {}).map(([k]) => k)))];
  if (scoredEntries.length && numericKeys.length) {
    const head = numericKeys.map((k) => `<th><code>${escapeHtml(k)}</code><span class="raw-metric-label">${escapeHtml(_metricLabel(k))}</span></th>`).join("");
    const rows = scoredEntries.map((s) => {
      const cells = numericKeys.map((k) => {
        const v = (s.metrics || {})[k];
        return `<td>${typeof v === "number" ? escapeHtml(_fmtMetricVal(v)) : "—"}</td>`;
      }).join("");
      return `<tr><td>${escapeHtml(s.displayName || "Submission")}</td>${cells}</tr>`;
    }).join("");
    rawMetricTable = `<div class="summary-detail-block"><div class="summary-detail-h">Raw metric values</div>
      <div style="overflow-x:auto"><table class="summary-metric-table"><thead><tr><th>Submission</th>${head}</tr></thead><tbody>${rows}</tbody></table></div></div>`;
  }
  const scorerNote = nonOfficialScored
    ? `<p class="summary-muted">Reference scoring package not installed. QC metrics are shown from the active scorer.</p>`
    : "";
  const detailsHtml = `
    <details class="summary-details">
      <summary>Technical Details</summary>
      <div class="summary-details-body">
        ${scorerNote}
        <div class="summary-detail-block"><div class="summary-detail-h">Validation issues</div>${issuesHtml || '<p class="summary-muted">No submissions.</p>'}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">Reference scoring metrics</div>${_renderReferenceTechnicalTable(analysisEntries)}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">Per-map NIfTI metadata and statistics</div>${_renderNiftiTechnicalTable(analysisEntries)}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">QC summary JSON</div><pre class="summary-json-block">${escapeHtml(JSON.stringify(qcSummaryJson, null, 2))}</pre></div>
        ${rawMetricTable}
      </div>
    </details>`;

  // ── Assemble ────────────────────────────────────────────────────────────────
  container.className = "summary-report";
  container.innerHTML = finalOutputHtml + qcSummaryHtml + imagePreviewHtml + referenceReportHtml + checklistHtml + detailsHtml;
  if (previewSubmissionId) _loadAndRenderImagePreviews(previewSubmissionId, challengeType);
}

// Legacy "Continue to Summary" buttons on Score step (hidden; action row is primary)
function _goToSummary() {
  unlockStep("summary");
  unlockStep("export");
  renderSummaryStep();
  goToStep("summary");
}

// Direct "Continue to Export" (used from summary action row and internal nav)
function _goToExport() {
  unlockStep("export");
  _syncExportStep();
  goToStep("export");
}

const scoreContinueBtn = el("btn-score-continue");
if (scoreContinueBtn) scoreContinueBtn.addEventListener("click", _goToSummary);
const scoreContinueBtnNc = el("btn-score-continue-nc");
if (scoreContinueBtnNc) scoreContinueBtnNc.addEventListener("click", _goToSummary);

// ── Scoring export helpers ────────────────────────────────────────────────────

function _enableScoringExport() {
  const blindedBtn   = el("export-scoring-blinded-btn");
  const unblindedBtn = el("export-scoring-unblinded-btn");
  const sub          = el("export-scoring-sub");
  const group        = el("export-scoring-group");
  if (group)        group.style.display  = "";
  if (blindedBtn)   blindedBtn.disabled   = false;
  if (unblindedBtn) unblindedBtn.disabled = false;
  if (sub) sub.textContent = "Ready.";
}

function _makeScoringExportHandler(btn, blinded) {
  if (!btn) return;
  const label = btn.textContent.trim() || (blinded ? "Download Scoring CSV" : "Download Unblinded Export");
  btn.addEventListener("click", async () => {
    const statusEl = el("export-scoring-status");
    setLoading(btn, true, label);
    if (statusEl) statusEl.style.display = "none";
    try {
      let url;
      if (batchState.batchId) {
        url = `${API}/api/export-scoring?batch_id=${encodeURIComponent(batchState.batchId)}&blinded=${blinded}`;
      } else if (state.submissionId) {
        url = `${API}/api/export-scoring?submission_id=${encodeURIComponent(state.submissionId)}&blinded=${blinded}`;
      } else {
        throw new Error("No submission or batch to export.");
      }
      const res = await fetch(url);
      if (!res.ok) {
        const d = await res.json().catch(() => ({}));
        throw new Error(d.detail || "Export failed.");
      }
      const blob  = await res.blob();
      const cd    = res.headers.get("Content-Disposition") || "";
      const fname = cd.match(/filename="([^"]+)"/)?.[1]
                 || `scoring_${blinded ? "blinded" : "unblinded"}.csv`;
      triggerDownload(blob, fname);
    } catch (err) {
      if (statusEl) {
        statusEl.style.display = "";
        statusEl.className     = "submit-status status-error";
        statusEl.textContent   = err.message || "Export failed.";
      }
    } finally {
      setLoading(btn, false, label);
    }
  });
}

_makeScoringExportHandler(el("export-scoring-blinded-btn"),   true);
_makeScoringExportHandler(el("export-scoring-unblinded-btn"), false);

// ── Combined summary CSV + HTML report ────────────────────────────────────────

// Build a ?submission_id=…|batch_id=… query for the current session.
function _sessionExportQuery() {
  if (batchState.batchId) return `batch_id=${encodeURIComponent(batchState.batchId)}`;
  if (state.submissionId) return `submission_id=${encodeURIComponent(state.submissionId)}`;
  return null;
}

function _makeCombinedExportHandler(btn, blinded) {
  if (!btn) return;
  const label = btn.textContent.trim();
  btn.addEventListener("click", async () => {
    const statusEl = el("export-combined-status");
    const q = _sessionExportQuery();
    if (!q) {
      if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = "No submission or batch to export. Validate first."; }
      return;
    }
    setLoading(btn, true, label);
    if (statusEl) statusEl.style.display = "none";
    try {
      const res = await fetch(`${API}/api/export-combined?${q}&blinded=${blinded}`);
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
      const blob  = await res.blob();
      const cd    = res.headers.get("Content-Disposition") || "";
      const fname = cd.match(/filename="([^"]+)"/)?.[1] || `osipi_combined_${blinded ? "blinded" : "unblinded"}.csv`;
      triggerDownload(blob, fname);
    } catch (err) {
      if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "Export failed."; }
    } finally {
      setLoading(btn, false, label);
    }
  });
}

_makeCombinedExportHandler(el("export-combined-blinded-btn"),   true);
_makeCombinedExportHandler(el("export-combined-unblinded-btn"), false);

const reportBtn = el("export-report-btn");
if (reportBtn) reportBtn.addEventListener("click", () => {
  const statusEl = el("export-combined-status");
  const q = _sessionExportQuery();
  if (!q) {
    if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = "No submission or batch to report on. Validate first."; }
    return;
  }
  // Blinded HTML report is safe to share; open in a new tab.
  window.open(`${API}/api/report?${q}&blinded=true`, "_blank", "noopener");
});

// ── Init ───────────────────────────────────────────────────────────────────────

updateMapTypePills(getRadio("challenge_type") || "dce");
syncSubmitLabel();

// Stamp the initial step on <body> so CSS body[data-step] rules fire immediately,
// then sync the local action area for the current step.
document.body.dataset.step = wf.step;
_syncWfNav();
_updateWizardFooter(wf.step);

// ── Sidebar collapse toggle ────────────────────────────────────────────────────
(function initSidebarCollapse() {
  const sidebar     = document.getElementById("sidebar");
  const collapseBtn = document.getElementById("collapse-btn");
  if (!sidebar || !collapseBtn) return;

  if (localStorage.getItem("sidebar-collapsed") === "1") {
    sidebar.classList.add("collapsed");
  }

  collapseBtn.addEventListener("click", () => {
    const isNowCollapsed = sidebar.classList.toggle("collapsed");
    try { localStorage.setItem("sidebar-collapsed", isNowCollapsed ? "1" : "0"); } catch(_) {}
  });
})();

// ══════════════════════════════════════════════════════════════════════════════
// Session restore — startup check (no auto-restore; subtle chip only)
// ══════════════════════════════════════════════════════════════════════════════

(function initSessionBanner() {
  const saved = loadSessionState();
  if (!saved) return;   // nothing saved or expired — normal fresh start

  // Show the subtle topbar chip — do NOT auto-restore or show a banner
  showRestoreBanner(saved);

  // Wire up the topbar restore chip
  const chipBtn = el("restore-chip-btn");
  if (chipBtn) {
    chipBtn.addEventListener("click", async () => {
      _hideRestoreBanner();
      const ok = await restoreSessionFromStorage();
      if (!ok) {
        // Session data was cleared (expired or corrupt) between chip display and click
        clearSessionState();
      }
    });
  }

  // Legacy hidden buttons still wired for completeness (they are never visible)
  const restoreBtn = el("restore-session-btn");
  if (restoreBtn) {
    restoreBtn.addEventListener("click", async () => {
      _hideRestoreBanner();
      const ok = await restoreSessionFromStorage();
      if (!ok) clearSessionState();
    });
  }
  const startNewBtn = el("start-new-session-btn");
  if (startNewBtn) {
    startNewBtn.addEventListener("click", () => {
      clearSessionState();
      _hideRestoreBanner();
      resetAll();
      syncSubmitLabel();
      goToStep("upload");
    });
  }
})();

// ── "Start New" button in sidebar ─────────────────────────────────────────────
(function initSidebarNewSession() {
  const btn = el("sidebar-new-session-btn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    clearSessionState();
    _hideRestoreBanner();
    resetAll();
    syncSubmitLabel();
    goToStep("upload");
  });
})();
