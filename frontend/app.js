"use strict";
// ─────────────────────────────────────────────────────────────────────────────
// OSIPI Perfusion Challenge review interface
// ─────────────────────────────────────────────────────────────────────────────

const API = window.location.origin;

// ── Map type options per challenge ────────────────────────────────────────────

let MAP_OPTIONS = {};

let _appConfig = {
  defaults: {},
  challengeTypes: null,
  mapTypes: null,
  mapTypePatterns: {},
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
  pendingLocalLabel: null,
  selectedMapType:   null,
  detection: {
    nifti_count:                 null,
    detected_parameter_map_type: "Unknown",
  },
};

// Request guard: prevents concurrent submits from double-clicks
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

// Execution result summaries populated by _updateRunRow(), keyed by submission_id
const _execSummaries = {};

// Scoring result cache populated by _applyScoreStatus(), keyed by submission_id
const _scoreCache = {};
const _displayAliases = {};
let _suppressSessionSave = false;

// NIfTI preview manifest cache populated by Score & Preview.
const _previewManifestCache = {};
const _previewItemsById = {};
let _previewMapOrder = [];        // map_ids in manifest order (gallery order)
let _previewSelectedMapId = null; // map shown in the one-at-a-time panel
let _activePreviewMapId = null;
let _activePreviewPlane = "axial";

// Frontend-only list filters for review/validation/run/score history.
let MAP_FILTER_OPTIONS = [{ value: "all", label: "All" }];

const SORT_FILTER_OPTIONS = [
  { value: "newest", label: "Newest" },
  { value: "oldest", label: "Oldest" },
  { value: "name", label: "Name A-Z" },
  { value: "status", label: "Status" },
];

const _collapseState = {
  index: null,
  validation: null,
  run: null,
  leaderboard: null,
};

const _indexFilter = { search: "", status: "all", map: "all", sort: "newest", showAll: false };
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
  showAll: false,
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

function defaultChallengeType() {
  return String(_appConfig.defaults?.challenge_type || getRadio("challenge_type") || "").toLowerCase();
}

function defaultScoringMapType() {
  return String(_appConfig.defaults?.scoring_map_type || _appConfig.mapTypes?.[0]?.display || "");
}

function escapeRegExp(text) {
  return String(text || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function _mapDisplayById() {
  const display = {};
  (_appConfig.mapTypes || []).forEach((item) => {
    if (item?.id) display[String(item.id).toLowerCase()] = item.display || item.id;
  });
  return display;
}

function _mapMetaByDisplay() {
  const meta = {};
  (_appConfig.mapTypes || []).forEach((item) => {
    const display = item?.display || item?.id;
    if (display) meta[String(display)] = item;
  });
  return meta;
}

function _configuredMapTokenRegex() {
  const tokens = [];
  (_appConfig.mapTypes || []).forEach((item) => {
    [item?.id, item?.display].forEach((value) => {
      const clean = String(value || "").trim();
      if (clean && !tokens.some((existing) => existing.toLowerCase() === clean.toLowerCase())) tokens.push(clean);
    });
  });
  if (!tokens.length) return null;
  return new RegExp(`\\b(${tokens.map(escapeRegExp).join("|")})\\b`, "i");
}

function _refreshMapFilterOptions() {
  const seen = new Set();
  const options = [{ value: "all", label: "All" }];
  (_appConfig.mapTypes || []).forEach((item) => {
    const label = item?.display || item?.id;
    if (!label || seen.has(label)) return;
    seen.add(label);
    options.push({ value: label, label });
  });
  MAP_FILTER_OPTIONS = options;
}

function _patternToRegex(pattern) {
  const tokens = String(pattern || "").trim().split(/[-_\s]+/).filter(Boolean);
  if (!tokens.length) return null;
  const body = tokens.map(escapeRegExp).join("[_\\-.\\s]*");
  const raw = tokens.join("");
  const bounded = raw.length <= 3 || tokens.length === 1;
  return new RegExp(bounded ? `(^|[_\\-.\\s])${body}([_\\-.\\s]|$)` : body, "i");
}

let _ROW_MAP_PATTERNS = [];

function _refreshRowMapPatterns() {
  const configured = _appConfig.mapTypePatterns || {};
  const rows = [];
  Object.entries(configured).forEach(([label, patterns]) => {
    (patterns || []).forEach((pattern) => {
      const re = _patternToRegex(pattern);
      if (label && re) rows.push([label, re]);
    });
  });
  _ROW_MAP_PATTERNS = rows;
}

function _applyConfigMapOptions(config) {
  const mapDisplay = _mapDisplayById();
  const allDisplays = [];
  const next = {};
  (config.challengeTypes || []).forEach((challenge) => {
    const id = String(challenge.id || "").toLowerCase();
    if (!id) return;
    const displays = (challenge.expected_maps || [])
      .map((mapId) => mapDisplay[String(mapId).toLowerCase()] || String(mapId))
      .filter(Boolean);
    displays.forEach((label) => { if (!allDisplays.includes(label)) allDisplays.push(label); });
    next[id] = [...displays, "Other"];
  });
  if (allDisplays.length) next.other = [...allDisplays, "Other"];
  MAP_OPTIONS = next;
  _refreshMapFilterOptions();
  _refreshRowMapPatterns();
}

function _wireChallengeTypeInputs() {
  document.querySelectorAll("input[name='challenge_type']").forEach((r) => {
    r.addEventListener("change", () => {
      updateMapTypePills(r.value);
    });
  });
}

function _renderChallengeTypeOptions() {
  const row = document.querySelector("[role='radiogroup'][aria-label='Challenge type']");
  const options = _appConfig.challengeTypes || [];
  if (!row) return;
  if (!options.length) {
    row.innerHTML = '<span class="config-placeholder">Configured challenges will appear here.</span>';
    return;
  }
  const selected = getRadio("challenge_type") || defaultChallengeType();
  row.innerHTML = options.map((challenge) => {
    const id = String(challenge.id || "").toLowerCase();
    const label = challenge.label || id.toUpperCase();
    const checked = id === selected ? " checked" : "";
    return `<label class="pill"><input type="radio" name="challenge_type" value="${escapeHtml(id)}"${checked} /><span>${escapeHtml(label)}</span></label>`;
  }).join("");
  if (!row.querySelector("input[name='challenge_type']:checked")) {
    const fallback = row.querySelector(`input[name='challenge_type'][value='${defaultChallengeType()}']`)
      || row.querySelector("input[name='challenge_type']");
    if (fallback) fallback.checked = true;
  }
  _wireChallengeTypeInputs();
}

function _showConfigLoadError(message) {
  const text = message || "Configuration could not be loaded. Challenge options are unavailable.";
  const challengeError = el("challenge-type-error");
  if (challengeError) challengeError.textContent = text;
  const row = document.querySelector("[role='radiogroup'][aria-label='Challenge type']");
  if (row) row.innerHTML = '<span class="config-placeholder">Configured challenges will appear here.</span>';
  const mapContainer = el("map-type-pills");
  if (mapContainer) mapContainer.innerHTML = '<span class="config-placeholder">Select map</span>';
}

async function hydrateAppConfig() {
  try {
    const res = await fetch(`${API}/api/config`);
    if (!res.ok) throw new Error("config endpoint failed");
    const data = await res.json();
    if (!Array.isArray(data.challenge_types) || !Array.isArray(data.map_types)) {
      throw new Error("config endpoint returned an invalid shape");
    }
    _appConfig = {
      defaults: data.defaults || _appConfig.defaults,
      challengeTypes: data.challenge_types,
      mapTypes: data.map_types,
      mapTypePatterns: data.map_type_patterns || {},
    };
    const challengeError = el("challenge-type-error");
    if (challengeError) challengeError.textContent = "";
    _applyConfigMapOptions(_appConfig);
    _renderChallengeTypeOptions();
    updateMapTypePills(getChallengeType());
    if (wf.step === "index" && batchState.submissions.length) renderBatchTable();
  } catch (err) {
    _showConfigLoadError(err && err.message ? err.message : "");
  }
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

function originalSubmissionName(sub, fallback) {
  return cleanSubmissionName(
    sub?.submission_id || sub?.source_folder || sub?.original_filename || sub?.name || sub?.display_name,
    fallback || "Submission"
  );
}

function _middleTruncate(value, maxLen = 28) {
  const text = String(value || "").trim();
  if (text.length <= maxLen) return text;
  const keep = Math.max(8, Math.floor((maxLen - 3) / 2));
  return `${text.slice(0, keep)}...${text.slice(text.length - keep)}`;
}

function _subjectNameFromText(value) {
  const match = String(value || "").match(/\bsub[-_]?([0-9][A-Za-z0-9]*)\b/i);
  return match ? `sub-${match[1]}` : "";
}

function _defaultSubmissionDisplayName(sub, fallback) {
  const candidates = [
    sub?.display_name,
    sub?.name,
    sub?.source_folder,
    sub?.original_filename,
    sub?.submission_id,
    fallback,
  ].filter(Boolean).map((v) => cleanSubmissionName(v));

  for (const candidate of candidates) {
    const subject = _subjectNameFromText(candidate);
    if (subject) return subject;
  }

  let name = candidates[0] || "Submission";
  name = name.replace(/\s+/g, " ").trim();
  const afterBatch = name.split(/(?:^|[_\-\s])submissions?[_\-\s]+/i).pop();
  if (afterBatch && afterBatch !== name) name = afterBatch;
  name = name
    .replace(/[_\-\s]+submission$/i, "")
    .replace(/[_\-\s]+submissions$/i, "")
    .replace(/^(multi|batch|dataset)[_\-\s]+/i, "")
    .trim();
  if (name.includes("_")) name = name.replace(/_/g, " ").replace(/\s+/g, " ").trim();
  return _middleTruncate(name || candidates[0] || "Submission");
}

function getSubmissionDisplayName(sub, fallback) {
  const sid = sub?.submission_id || fallback || "";
  const alias = sid ? _displayAliases[sid] : "";
  if (alias && alias.trim()) return alias.trim();
  if (sub?.display_alias && String(sub.display_alias).trim()) return String(sub.display_alias).trim();
  return _defaultSubmissionDisplayName(sub, fallback || sid || "Submission");
}

function submissionDisplayName(sub, fallback) {
  return getSubmissionDisplayName(sub, fallback);
}

function _hydrateDisplayAliases(saved) {
  const aliases = saved?.displayAliases || {};
  Object.entries(aliases).forEach(([sid, alias]) => {
    if (sid && alias) _displayAliases[sid] = String(alias).trim();
  });
  (saved?.submissions || []).forEach((s) => {
    if (s?.submission_id && s.display_alias) _displayAliases[s.submission_id] = String(s.display_alias).trim();
  });
  (saved?.validationSummary?.results || []).forEach((r) => {
    if (r?.submission_id && r.displayAlias) _displayAliases[r.submission_id] = String(r.displayAlias).trim();
  });
}

function _clearDisplayAliases() {
  Object.keys(_displayAliases).forEach((key) => delete _displayAliases[key]);
}

function _setSubmissionDisplayAlias(submissionId, value) {
  const sid = String(submissionId || "").trim();
  if (!sid) return "";
  const alias = String(value || "").trim();
  if (alias) _displayAliases[sid] = alias;
  else delete _displayAliases[sid];
  return getSubmissionDisplayName({ submission_id: sid }, sid);
}

function _refreshDisplayNameDom(submissionId) {
  const sid = String(submissionId || "");
  if (!sid) return;
  const displayName = getSubmissionDisplayName({ submission_id: sid }, sid);
  document.querySelectorAll("[data-display-name-for]").forEach((node) => {
    if (node.dataset.displayNameFor !== sid) return;
    node.textContent = displayName;
    node.setAttribute("title", displayName);
  });
  document.querySelectorAll("[data-display-name-input]").forEach((node) => {
    if (node.dataset.displayNameInput === sid) node.value = displayName;
  });
  if (_scoreCache[sid]) _scoreCache[sid].displayName = displayName;
  if (wf.step === "score") renderScorePreviewPanel();
}

function challengeLabel(value) {
  return String(value || getChallengeType() || defaultChallengeType()).toUpperCase();
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

// Canonical role-based counts from the backend validation result. Absent on
// results stored before counts existed, so every reader defaults safely.
function submissionCounts(item) {
  const counts = item && typeof item.counts === "object" && item.counts ? item.counts : {};
  return {
    parameterMaps: Number(counts.parameter_maps || 0),
    fittedSignals: Number(counts.fitted_signals || 0),
    methodsDocuments: Number(counts.methods_documents || 0),
    scans: Number(counts.scans || 0),
    scansByDataset: counts.scans_by_dataset && typeof counts.scans_by_dataset === "object"
      ? counts.scans_by_dataset : {},
  };
}

// What the submission covers, e.g. "Clinical + Synthetic". Derived from
// resolved scan identity, not from how many map types were detected:
// a DCE submission holding Ktrans, vp and ve is complete, not "Mixed/Other".
function datasetDisplay(item) {
  const names = Object.keys(submissionCounts(item).scansByDataset);
  if (!names.length) return "";
  return names
    .slice()
    .sort()
    .map((name) => name.charAt(0).toUpperCase() + name.slice(1))
    .join(" + ");
}

// One short line of counts, each labelled, omitting anything absent.
function submissionCountSummary(item) {
  const c = submissionCounts(item);
  const parts = [];
  if (c.scans) parts.push(`${c.scans} scan${c.scans === 1 ? "" : "s"}`);
  if (c.parameterMaps) {
    parts.push(`${c.parameterMaps} parameter map${c.parameterMaps === 1 ? "" : "s"}`);
  }
  if (c.fittedSignals) {
    parts.push(`${c.fittedSignals} modelled S-t volume${c.fittedSignals === 1 ? "" : "s"}`);
  }
  if (c.methodsDocuments) {
    parts.push(`${c.methodsDocuments} methods document${c.methodsDocuments === 1 ? "" : "s"}`);
  }
  return parts.join(" · ");
}

function statusChipTone(state) {
  const clean = String(state || "pending").toLowerCase();
  if (["complete", "completed", "passed", "pass", "ready", "success", "scored"].includes(clean)) return "success";
  if (["warning", "warn", "partial", "timeout", "timed-out"].includes(clean)) return "warning";
  if (["error", "failed", "fail", "danger", "cannot-run"].includes(clean)) return "danger";
  if (["running", "run", "info", "loading"].includes(clean)) return "info";
  return "neutral";
}

function statusPill(label, state) {
  const safeState = escapeHtml(state || "pending");
  return `<span class="status-chip status-pill status-${safeState} status-chip-${statusChipTone(state)}">${escapeHtml(label)}</span>`;
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
      <span class="filter-pill-chevron" aria-hidden="true"></span>
    </button>
    <div class="filter-menu" role="menu" hidden>
      ${(options || []).map((opt) => `<button type="button" class="filter-option${opt.value === value ? " is-selected" : ""}" role="menuitemradio" aria-checked="${opt.value === value ? "true" : "false"}" data-filter-option="${escapeHtml(group)}" data-filter-value="${escapeHtml(opt.value)}">
        <span class="filter-option-label">${escapeHtml(opt.label)}</span>
        <span class="filter-option-check" aria-hidden="true">${opt.value === value ? "Selected" : ""}</span>
      </button>`).join("")}
    </div>
  </div>`;
}

function _renderSearchBox(id, value, placeholder = "Search") {
  return `<label class="filter-search" for="${escapeHtml(id)}">
    <span class="filter-search-icon" aria-hidden="true"></span>
    <input type="search" id="${escapeHtml(id)}" value="${escapeHtml(value || "")}" placeholder="${escapeHtml(placeholder)}" autocomplete="off">
  </label>`;
}

function _renderClearFilterButton(id, active) {
  return active ? `<button type="button" class="filter-clear-btn" id="${escapeHtml(id)}">Clear filters</button>` : "";
}

function submissionFileIconHtml() {
  // Shared submission icon: the IMG document asset, used identically in
  // Review / Validate / Run / Score rows.
  return `<div class="worklist-icon submission-file-icon" aria-hidden="true">
    <img src="/static/assets/submission-img-icon.png" alt="" width="28" height="28">
  </div>`;
}

function _renderCountBadge(count) {
  return `<span class="collapsible-count section-count">${escapeHtml(count || 0)}</span>`;
}

function _summaryChip(label, value, state = "") {
  const cls = state ? ` list-summary-chip-${escapeHtml(state)}` : "";
  return `<span class="list-summary-chip${cls}"><strong>${escapeHtml(value)}</strong>${escapeHtml(label)}</span>`;
}

function _latestLabel(value) {
  if (!value) return "";
  return `<span class="list-summary-latest">Latest: ${escapeHtml(value)}</span>`;
}

function _collapsibleDefaultOpen(key, count) {
  _collapseState[key] = true;
  return true;
}

function _setCollapsibleSectionOpen(key, open) {
  // Step list sections stay open; only each row's Details area collapses.
  open = true;
  _collapseState[key] = true;
  const trigger = document.querySelector(`[data-collapse-toggle="${key}"]`);
  const sectionId = key === "leaderboard" ? "leaderboard-card" : `${key}-list-section`;
  const bodyId = key === "leaderboard" ? "leaderboard-section-body" : `${key}-list-body`;
  const summaryId = key === "leaderboard" ? "leaderboard-section-summary" : `${key}-section-summary`;
  const section = trigger?.closest(".collapsible-section") || el(sectionId);
  const body = trigger ? el(trigger.dataset.collapseBody || "") : el(bodyId);
  const summary = trigger ? el(trigger.dataset.collapseSummary || "") : el(summaryId);
  if (trigger) trigger.setAttribute("aria-expanded", "true");
  if (section) section.classList.toggle("is-collapsed", !open);
  if (body) body.hidden = !open;
  if (summary) summary.hidden = !!open;
}

function _syncCollapsibleSection(key, count, summaryHtml) {
  const open = _collapsibleDefaultOpen(key, count);
  const countEl = el(`${key}-section-count`);
  const summaryEl = el(`${key}-section-summary`);
  if (countEl) countEl.textContent = String(count || 0);
  if (summaryEl) summaryEl.innerHTML = summaryHtml || "";
  _setCollapsibleSectionOpen(key, open);
}

function _renderCollapsibleSection(key, title, count, summaryHtml, bodyHtml) {
  // Delegates to the shared renderSection so every section header comes from
  // one place (section-row / section-title / section-count / section-actions).
  return renderSection({
    key, title, count, summaryHtml, bodyHtml,
    open: true,
  });
}

function _restoreSearchFocus(id, cursor = null) {
  const input = el(id);
  if (!input) return;
  input.focus({ preventScroll: true });
  const pos = cursor == null ? input.value.length : Math.min(cursor, input.value.length);
  try {
    input.setSelectionRange(pos, pos);
  } catch (_) {}
}

function _closeFilterMenus() {
  document.querySelectorAll(".filter-dropdown .filter-menu").forEach((menu) => {
    menu.hidden = true;
    // Reset the floating (fixed-position) state applied on open.
    menu.classList.remove("filter-menu--floating");
    menu.style.removeProperty("--fm-top");
    menu.style.removeProperty("--fm-left");
    menu.style.removeProperty("--fm-max-h");
    menu.style.removeProperty("--fm-min-w");
  });
  document.querySelectorAll("[data-filter-menu]").forEach((btn) => {
    btn.setAttribute("aria-expanded", "false");
  });
}

// Open a filter menu as a fixed-position overlay so it is never clipped by the
// card / step scroll container, and clamp it inside the viewport (right edge +
// height). Applies to every filter dropdown (Status / Map / Sort / …).
function _positionFilterMenu(btn, menu) {
  menu.classList.add("filter-menu--floating");
  const r = btn.getBoundingClientRect();
  // Measure natural width now that the menu is visible + floating.
  const menuW = Math.min(Math.max(menu.offsetWidth || 200, r.width), window.innerWidth - 16);
  let left = r.left;
  if (left + menuW > window.innerWidth - 8) left = window.innerWidth - 8 - menuW;
  if (left < 8) left = 8;
  const maxH = Math.max(160, window.innerHeight - r.bottom - 12);
  menu.style.setProperty("--fm-top", `${Math.round(r.bottom + 6)}px`);
  menu.style.setProperty("--fm-left", `${Math.round(left)}px`);
  menu.style.setProperty("--fm-min-w", `${Math.round(r.width)}px`);
  menu.style.setProperty("--fm-max-h", `${Math.round(maxH)}px`);
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
    if (key !== "sort") _leaderboardFilter.showAll = false;
    _renderLeaderboardEntries();
    return;
  }
  if (group.startsWith("index-")) {
    const key = group.replace("index-", "");
    if (key in _indexFilter) _indexFilter[key] = value;
    if (key !== "sort") _indexFilter.showAll = false;
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
  if (group === "validation-map") {
    _reviewFilter.map = value;
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
    return;
  }
  if (group === "run-map") {
    _runFilter.map = value;
    _runFilter.showAll = false;
    _refreshRunFilterBar();
    _applyRunFilters();
    return;
  }
  if (group === "run-sort") {
    _runFilter.sort = value;
    _refreshRunFilterBar();
    _applyRunFilters();
  }
}

document.addEventListener("click", (e) => {
  const collapseBtn = e.target.closest("[data-collapse-toggle]");
  if (collapseBtn) {
    e.preventDefault();
    const key = collapseBtn.dataset.collapseToggle || "";
    _setCollapsibleSectionOpen(key, true);
    return;
  }

  const menuBtn = e.target.closest("[data-filter-menu]");
  if (menuBtn) {
    e.preventDefault();
    const menu = menuBtn.closest(".filter-dropdown")?.querySelector(".filter-menu");
    const wasOpen = menu && !menu.hidden;
    _closeFilterMenus();
    if (menu && !wasOpen) {
      menu.hidden = false;
      menuBtn.setAttribute("aria-expanded", "true");
      _positionFilterMenu(menuBtn, menu);
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

// A fixed-position menu must not drift when the page/panel scrolls or resizes.
window.addEventListener("scroll", _closeFilterMenus, true);
window.addEventListener("resize", _closeFilterMenus);

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
    const configuredMapRe = _configuredMapTokenRegex();
    const m = (configuredMapRe ? text.match(configuredMapRe) : null) ||
              text.match(/expected[^a-z]+([A-Za-z0-9]+)\s+parameter/i);
    const mapName = m ? m[1] : null;
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

  // Messages read better whole. The issue list wraps them cleanly, so only
  // genuinely runaway text, a validator that pastes an entire path list,
  // needs a cap, and that cap breaks at a word boundary instead of mid-word
  // ("… because datase…" told the reader nothing).
  if (text.length > 240) {
    const cut = text.slice(0, 240);
    const boundary = cut.lastIndexOf(" ");
    return (boundary > 160 ? cut.slice(0, boundary) : cut).trimEnd() + "…";
  }
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
    checks.push("All checks passed, submission is ready for QC and configured analysis");

  return checks;
}

// ── Workflow navigation ───────────────────────────────────────────────────────

const WF_STEPS = ["upload", "index", "validate", "run", "score", "export"];

const STEP_TITLES = {
  upload:   { title: "Upload",             sub: "Submit parameter maps for automated validation" },
  index:    { title: "Review",             sub: "Submissions detected" },
  validate: { title: "Validate",           sub: "Validation results for all submissions" },
  run:      { title: "Run",                sub: "Execute validated submissions" },
  score:    { title: "QC & Preview",       sub: "Quality checks and generic reference comparisons" },
  export:   { title: "Export",             sub: "Download reports, CSV, and JSON summaries" },
};

// The top session-summary card and the numbered workflow stepper were removed;
// the Upload card is now the first content. Internal step state lives in the
// hidden wf-btn-* holders + goToStep()/hash navigation below.

function _normalizeWorkflowStep(step, fallback = "upload") {
  if (step === "summary") return fallback === "export" ? "export" : "score";
  return WF_STEPS.includes(step) ? step : fallback;
}

function _openStepListSection(step) {
  const key = {
    index: "index",
    validate: "validation",
    run: "run",
    score: "leaderboard",
  }[step];
  if (key) _setCollapsibleSectionOpen(key, true);
}

function goToStep(step) {
  step = _normalizeWorkflowStep(step, "upload");
  wf.step = step;
  document.body.dataset.step = step;   // CSS hook for the current wizard step
  WF_STEPS.forEach((s) => {
    const panel = el(`step-${s}`);
    if (panel) panel.hidden = (s !== step);
  });
  _syncWfNav();
  // Scroll the content area and the step's internal scroll region to top
  document.querySelector(".content")?.scrollTo({ top: 0, behavior: "instant" });
  document.querySelector(`#step-${step} .step-body`)?.scrollTo({ top: 0, behavior: "instant" });
  window.scrollTo({ top: 0, behavior: "instant" });
  // Update header title (page-title / page-subtitle may not exist in topbar layout, no-op if missing)
  const titles = STEP_TITLES[step] || {};
  const hTitle = el("page-title");
  const hSub   = el("page-subtitle");
  if (hTitle) hTitle.textContent  = titles.title || step;
  if (hSub)   hSub.textContent    = titles.sub   || "";
  // Update local wizard actions
  _updateWizardFooter(step);
  _openStepListSection(step);
  // Keep the URL hash in sync with the active step (#review for the index step)
  const hash = STEP_TO_HASH[step];
  if (hash && location.hash !== `#${hash}`) {
    try { history.replaceState(null, "", `#${hash}`); } catch (_) { location.hash = hash; }
  }
  // Persist step to session (also refreshes the sessionStorage wizard state)
  if (!_suppressSessionSave) saveSessionState();
}

// ── Wizard Actions ────────────────────────────────────────────────────────────

const _WF_FOOTER_CONFIG = {
  upload:   { back: null,       next: null,       nextLabel: "Upload and Continue",     hint: "Fill in team details and choose a submission file below" },
  index:    { back: "upload",   next: "validate", nextLabel: "Validate Submission",     hint: "" },
  validate: { back: "index",    next: "run",       nextLabel: "Continue to Run",        hint: "" },
  run:      { back: "validate", next: "score",     nextLabel: "Continue to QC & Preview", hint: "" },
  score:    { back: "run",      next: "export",   nextLabel: "Continue to Export",      hint: "" },
  export:   { back: "score",    next: null,        nextLabel: "Start New Submission",    hint: "" },
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
  // Action rows pin to the bottom of the step card itself (after the
  // .step-body internal scroll region) so they stay visible on the
  // contained one-screen layouts of Steps 2-6.
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
        <p class="step-action-guidance" aria-live="polite"></p>
        <button type="button" class="btn btn-primary step-action-primary">Continue</button>
      </div>`;
  }

  if (row.parentElement !== host) host.appendChild(row);
  return row;
}

function _stepPrimaryLabel(step) {
  if (step === "run" && _allValidationResultsAreResultOnly()) return "Continue to QC & Preview";
  return _WF_FOOTER_CONFIG[step]?.nextLabel || "Continue";
}

function _startNewSubmissionFromExport() {
  _resetToUploadAndClearPersistence();
}

function _resetToUploadAndClearPersistence() {
  _suppressSessionSave = true;
  try {
    resetAll();
    syncSubmitLabel();
    goToStep("upload");
  } finally {
    _suppressSessionSave = false;
  }
  clearSessionState();
  clearWizardState();
}

// The session-card "Start New" button was removed. Start New remains available
// on the Validate step header (#new-btn) and the Export step.

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

  if (step === "export") {
    _startNewSubmissionFromExport();
    return;
  }
  if (!cfg.next) return;

  unlockStep(cfg.next);
  if (cfg.next === "run")     { renderRunStep().catch(() => {}); }
  if (cfg.next === "score")   { renderScoreStep().catch(() => {}); }
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
  const guidance = row.querySelector(".step-action-guidance");

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
  const readyGuidance = {
    index: "Next: validate the selected submission files.",
    validate: "Next: confirm whether processing is required.",
    run: "Next: review QC and available analyses.",
    score: "Next: choose reviewer or organiser outputs.",
    export: "This clears the current local review and returns to Upload.",
  };
  if (guidance) {
    guidance.textContent = blockedReason ? "" : (readyGuidance[step] || "");
    guidance.hidden = !!blockedReason;
  }
  if (blockedReason) {
    primaryBtn.title = blockedReason;
    primaryBtn.dataset.disabledReason = blockedReason;
    primaryBtn.setAttribute("aria-label", `${label}. ${blockedReason}`);
    row.dataset.disabledReason = blockedReason;
    row.title = blockedReason;
  }

  if (step === "export") {
    primaryBtn.style.opacity = "";
    primaryBtn.title = "Clear this workflow and return to Upload.";
    delete row.dataset.disabledReason;
    row.removeAttribute("title");
  }
}

function _updateWizardFooter(step) {
  const cfg = _WF_FOOTER_CONFIG[step];
  if (!cfg) return;
  _hideLegacyWizardFooter();

  if (step === "upload") {
    _syncUploadSubmitButton();
    _syncInactiveStepActions("upload");
    return;
  }

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
  const guidance = el("submit-guidance");
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
    if (guidance) guidance.textContent = reason;
  } else {
    submitBtn.title = "";
    submitBtn.removeAttribute("aria-label");
    delete submitBtn.dataset.disabledReason;
    if (guidance) guidance.textContent = "Ready. The pipeline will upload, detect, and organise the submission automatically.";
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
      // it must NOT gate the action row here, a result-only submission that passed
      // with warnings still has 0 errors and must be allowed to continue.
      return results.some((r) => r.passed || issueCount(r, "errors") === 0);
    }
    case "run": {
      const results = batchState.validationData ? (batchState.validationData.results || []) : [];
      const valid = results.filter((r) => r.passed || issueCount(r, "errors") === 0);
      if (!valid.length) return false;
      return valid.every((r) => {
        const readiness = r.run_readiness || inferredRunReadiness(r);
        if (readiness === "result_only") return true;
        const sid = r.submission_id || r.id;
        return readiness === "runnable" && !!sid && _execSummaries[sid]?.status === "passed";
      });
    }
    case "score":  return true;  // always allow continue to export
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
    case "run":
      return "Process runnable submissions and resolve generated-output errors before continuing. Result-map-only submissions do not need a code run.";
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

// Per-step progress-status helpers were removed together with the numbered
// stepper that consumed them.

// The visible numbered stepper was removed. This is kept as a safe no-op so the
// many progress-refresh call sites stay valid; step gating/navigation is handled
// by the hidden wf-btn-* state holders, _syncWfNav(), and goToStep().
function _syncCompactProgress() { /* stepper removed, no visible progress nav to sync */ }

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
  return getRadio("challenge_type") || defaultChallengeType();
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
  state.pendingLocalLabel = null;
  state.detection = { nifti_count: null, detected_parameter_map_type: "Unknown" };

  const lbl = el("local-file-label");
  if (lbl) { lbl.innerHTML = ""; lbl.className = "file-label"; }

  ["zenodo-input", "github-url", "github-branch"].forEach((id) => {
    const f = el(id); if (f) f.value = "";
  });

  const srcErr = el("source-error");
  if (srcErr) srcErr.textContent = "";
  clearSubmitStatus();
}

function resetAll() {
  clearSubmissionData();
  _clearDisplayAliases();
  state.mode            = "new";
  state.selectedMapType = null;

  batchState.uploadData      = null;
  batchState.selectedIds.clear();
  batchState.batchId         = null;
  batchState.validationData  = null;
  batchState.isBatch         = false;

  // Reset nav steps (all steps except upload)
  ["index", "validate", "run", "score", "export"].forEach((s) => {
    const btn = el(`wf-btn-${s}`);
    if (btn) btn.disabled = true;
    if (btn) btn.classList.remove("wf-done", "wf-warn", "wf-fail");
  });

  // Clear persisted session
  clearSessionState();

  // Session chip
  const chip = el("session-chip");
  if (chip) chip.style.display = "none";

  ["team-name", "contact-email", "map-type-other"].forEach((id) => {
    const f = el(id); if (f) f.value = "";
  });

  const defaultRadio = document.querySelector(`input[name='challenge_type'][value='${defaultChallengeType()}']`);
  if (defaultRadio) defaultRadio.checked = true;

  updateMapTypePills(defaultChallengeType());

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
//   • No auto-restore, banner shown, user must click "Restore"
//   • 24 h expiry, expired sessions are silently discarded
//   • No files, logs, CSVs, or large arrays stored, IDs and summaries only
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
      display_alias:               _displayAliases[s.submission_id] || s.display_alias || null,
      nifti_count:                 s.nifti_count,
      detected_parameter_map_type: s.detected_parameter_map_type,
      has_run_instructions:        s.has_run_instructions,
      source_folder:               s.source_folder,
      detection_warning:           s.detection_warning,
      detected_challenge_type:     s.detected_challenge_type || null,
      challenge_type:              s.challenge_type || null,
      confirmed_challenge_type:    s.confirmed_challenge_type || null,
      status:                      s.status,
    }));

    const validationSummary = batchState.validationData ? {
      batchId:     batchState.batchId,
      total:       (batchState.validationData.results || []).length,
      passedCount: batchState.validationData.passed_count || 0,
      failedCount: batchState.validationData.failed_count || 0,
      results:     (batchState.validationData.results || []).map((r) => ({
        submission_id:      r.submission_id,
        displayAlias:       _displayAliases[r.submission_id] || r.display_alias || null,
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
        // Store first 3 errors/warnings as short text only, no raw logs
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
      displayAliases:     { ..._displayAliases },
      validationSummary,
      executionSummaries: { ..._execSummaries },
      // Scoring: store provider/status snapshot only, no metric values
      scoringSnapshot:    _collectScoringSnapshot(),
    };

    localStorage.setItem(SESSION_KEY, JSON.stringify(payload));
  } catch (_) { /* localStorage unavailable or full, fail silently */ }
  // Lightweight reload-restore state (sessionStorage) tracks every save point:
  // upload/detection, validation, run, scoring, and step changes.
  saveWizardState();
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
  clearWizardState();
  _clearDisplayAliases();
  // Reset exec + score summaries
  Object.keys(_execSummaries).forEach((k) => delete _execSummaries[k]);
  Object.keys(_scoreCache).forEach((k) => delete _scoreCache[k]);
}

// ══════════════════════════════════════════════════════════════════════════════
// Wizard reload persistence  (sessionStorage key: osipi_wizard_state_v1)
// Auto-restores the active step on browser reload. Lightweight state only,
// step id, submission/batch ids, and form basics. No files, no results.
// The full session snapshot stays in localStorage (saveSessionState above);
// this layer only decides WHERE to resume and whether that is still valid.
// URL hash (#upload/#review/#validate/#run/#score/#export) is kept
// in sync and, when valid, wins over the saved step on load.
// ══════════════════════════════════════════════════════════════════════════════

const WIZARD_KEY = "osipi_wizard_state_v1";
const STEP_TO_HASH = { upload: "upload", index: "review", validate: "validate", run: "run", score: "score", export: "export" };
const HASH_TO_STEP = { upload: "upload", review: "index", index: "index", validate: "validate", run: "run", score: "score", summary: "score", export: "export" };

function saveWizardState() {
  try {
    sessionStorage.setItem(WIZARD_KEY, JSON.stringify({
      step:          wf.step,
      submissionId:  state.submissionId || null,
      batchId:       batchState.batchId || null,
      challengeType: getChallengeType() || null,
      mapType:       state.selectedMapType || null,
      teamName:      getTeamName() || "",
      contactEmail:  getEmail() || "",
      displayAliases: { ..._displayAliases },
      updatedAt:     new Date().toISOString(),
    }));
  } catch (_) { /* sessionStorage unavailable, fail silently */ }
}

function loadWizardState() {
  try {
    const raw = sessionStorage.getItem(WIZARD_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch (_) { return null; }
}

function clearWizardState() {
  try { sessionStorage.removeItem(WIZARD_KEY); } catch (_) {}
}

// A step can only be restored when the state it depends on still exists.
function canRestoreStep(stepId, saved) {
  const s = saved === undefined ? loadSessionState() : saved;
  const hasSubmissions = !!(s && ((s.submissions || []).length > 0 || s.submissionId));
  const hasValidation  = !!(s && s.validationSummary && (s.validationSummary.results || []).length > 0);
  switch (stepId) {
    case "upload":   return true;
    case "index":    return hasSubmissions;
    case "validate": return hasSubmissions && hasValidation;
    case "run":      return hasSubmissions && hasValidation;
    case "score":    return hasSubmissions && hasValidation;
    case "export":   return hasSubmissions && hasValidation;
    default:         return false;
  }
}

// Walk backwards from the requested step to the latest step that is still valid.
function _fallbackRestoreStep(requested, saved) {
  requested = _normalizeWorkflowStep(requested, "score");
  const order = ["upload", "index", "validate", "run", "score", "export"];
  let idx = order.indexOf(requested);
  if (idx < 0) idx = 0;
  for (let i = idx; i >= 0; i--) {
    if (canRestoreStep(order[i], saved)) return order[i];
  }
  return "upload";
}

function _hashStep() {
  return HASH_TO_STEP[(location.hash || "").replace(/^#/, "").toLowerCase()] || null;
}

// Auto-restore on reload. Prefers a valid URL hash step, then the saved
// sessionStorage step. Falls back gracefully and never lands on a blank step.
async function restoreWizardState() {
  const wizard = loadWizardState();
  const hashStep = _hashStep();
  const requested = _normalizeWorkflowStep(hashStep || wizard?.step || null, "upload");
  if (!requested || requested === "upload") return false;

  const saved = loadSessionState();
  if (!saved) {
    // Nothing to rebuild from: restore form basics only and stay on Upload.
    if (wizard) {
      const teamField  = el("team-name");
      const emailField = el("contact-email");
      if (teamField  && wizard.teamName)     teamField.value  = wizard.teamName;
      if (emailField && wizard.contactEmail) emailField.value = wizard.contactEmail;
      if (wizard.challengeType) {
        const radio = document.querySelector(`input[name='challenge_type'][value='${wizard.challengeType}']`);
        if (radio) radio.checked = true;
        updateMapTypePills(wizard.challengeType);
      }
    }
    return false;
  }

  const target = _fallbackRestoreStep(requested, saved);
  try {
    const ok = await restoreSessionFromStorage();
    if (!ok) { goToStep("upload"); return false; }
    if (target !== wf.step) {
      const order = ["upload", "index", "validate", "run", "score", "export"];
      order.slice(1, order.indexOf(target) + 1).forEach((s) => unlockStep(s));
      goToStep(target);
    }
    // Re-fetch live data for late steps instead of relying on stale DOM.
    if (target === "score")   renderScoreStep().catch(() => {});
    if (target === "export")  _syncExportStep();
    return true;
  } catch (_) {
    _showRestoreWarning("Could not restore the previous session. Starting from Upload.");
    goToStep("upload");
    return false;
  }
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
  const savedStep = _normalizeWorkflowStep(saved.step, "score");
  const stepTitle = STEP_TITLES[savedStep]?.title || savedStep || "";
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
  _hydrateDisplayAliases(saved);

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
    }
    updateMapTypePills(saved.challengeType);
  }

  // Restore map type pill selection
  if (saved.mapType) {
    state.selectedMapType = saved.mapType;
    updateMapTypePills(saved.challengeType || defaultChallengeType());
  }

  // Restore source type radio
  if (saved.sourceType) {
    const radio = document.querySelector(
      `input[name='submission_type'][value='${saved.sourceType}']`
    );
    if (radio) { radio.checked = true; switchSource(saved.sourceType); }
  }

  // 2. Restore batch/submission state (no files, backend already has them)
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

  // 3. Unlock steps up to the saved step. Older sessions may contain the
  // removed "summary" step; land those on Score & Preview.
  const restoredStep = _normalizeWorkflowStep(saved.step, "score");
  const stepOrder    = ["upload", "index", "validate", "run", "score", "export"];
  const savedStepIdx = stepOrder.indexOf(restoredStep);
  if (savedStepIdx >= 1) stepOrder.slice(1, savedStepIdx + 1).forEach((s) => unlockStep(s));

  // 4. Re-render index table
  if (savedStepIdx >= 1 && batchState.uploadData) {
    renderBatchTable(batchState.uploadData.submissions);
  }

  // 5. Re-render validation table from summary
  if (savedStepIdx >= 2 && saved.validationSummary) {
    const synthData = _synthValidationData(saved.validationSummary);
    batchState.validationData = synthData;
    // renderValidateStep may auto-advance to "run", we'll override with goToStep after
    renderValidateStep(synthData);
  }

  // 6. Apply saved exec summaries to run rows (after run step renders)
  if (savedStepIdx >= 3 && Object.keys(_execSummaries).length > 0) {
    renderRunStep().then(() => _applyExecSummariesToRows()).catch(() => {});
  }

  // 7. Navigate to the saved step (overrides any auto-advance)
  goToStep(restoredStep);
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
    display_alias:        r.displayAlias || null,
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

    // Update compact run meta text (card or table)
    const statusCell = wrap.querySelector(".er-run-status-cell, .run-card-status-row");
    if (statusCell) {
      statusCell.textContent = _erRunMetaText(newStatus, runnable);
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
      drawer.innerHTML = `<p style="font-size:0.73rem;color:var(--muted);margin:0">Session restored, re-run to see full logs.</p>`;
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
  } catch (_) { /* network error during check, silent */ }
}

function _showRestoreWarning(msg) {
  // Show the upload-card notice in warn state
  const notice = el("upload-restore-notice");
  const msgEl  = el("upload-restore-msg");
  if (notice) {
    if (msgEl) msgEl.textContent = msg;
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

  const options = MAP_OPTIONS[challengeType] || MAP_OPTIONS.other || [];
  if (!options.length) {
    container.innerHTML = '<span class="config-placeholder">Select map</span>';
    return;
  }

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

_wireChallengeTypeInputs();

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
  const sourceGuidance = el("source-guidance");
  const sourceCopy = {
    local: "Choose a ZIP, folder, or set of files from this computer.",
    zenodo: "Enter a public Zenodo record URL, DOI, or record ID.",
    github: "Enter a public GitHub repository URL and an optional branch.",
  };
  if (sourceGuidance) sourceGuidance.textContent = sourceCopy[type] || sourceCopy.local;
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
  state.pendingLocalLabel = label;
  renderSelectedUploadFile();
  const srcErr = el("source-error");
  if (srcErr) srcErr.textContent = "";
  _refreshWizardFooter();   // enable the in-card "Upload and Detect" button
  _syncUploadSubmitButton();
}

// ── Selected upload file card ────────────────────────────────────────────────
// Rendered below the dropzone once a file/folder/ZIP is picked. Shows name,
// size, kind tag, live status, and a real (never faked) progress bar.

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = bytes, u = -1;
  do { v /= 1024; u++; } while (v >= 1024 && u < units.length - 1);
  return `${v >= 100 ? Math.round(v) : v.toFixed(1)} ${units[u]}`;
}

// Icon tag for the selected payload: ZIP, NIfTI, Folder, Files, or extension.
function _uploadKindTag(files) {
  if (!files || !files.length) return "File";
  if (files.length > 1) {
    return files[0].webkitRelativePath ? "Folder" : "Files";
  }
  const name = (files[0].name || "").toLowerCase();
  if (name.endsWith(".zip")) return "ZIP";
  if (name.endsWith(".nii") || name.endsWith(".nii.gz")) return "NIfTI";
  const ext = name.includes(".") ? name.split(".").pop().toUpperCase() : "";
  return ext && ext.length <= 5 ? ext : "File";
}

function renderSelectedUploadFile() {
  const holder = el("local-file-label");
  if (!holder) return;
  const files = state.pendingLocalFiles;
  if (!files || !files.length) { holder.innerHTML = ""; holder.className = "file-label"; return; }
  const totalBytes = files.reduce((a, f) => a + (Number(f.size) || 0), 0);
  const name = files.length === 1
    ? (files[0].name || "Selected file")
    : (state.pendingLocalLabel || `${files.length} files selected`);
  holder.className = "file-label has-upload-card";
  holder.innerHTML = `
    <div class="upload-file-card" data-status="ready">
      <div class="ufc-icon">${escapeHtml(_uploadKindTag(files))}</div>
      <div class="ufc-info">
        <div class="ufc-name" title="${escapeHtml(name)}">${escapeHtml(name)}</div>
        <div class="ufc-meta">
          <span class="ufc-size">${escapeHtml(formatFileSize(totalBytes))}</span>
          <span class="ufc-status">Ready to upload</span>
        </div>
        <div class="ufc-progress" hidden><div class="ufc-progress-fill" style="width:0%"></div></div>
        <div class="ufc-error" hidden></div>
      </div>
      <button type="button" class="btn-icon ufc-remove" aria-label="Remove selected file" title="Remove">Remove</button>
    </div>`;
  const removeBtn = holder.querySelector(".ufc-remove");
  if (removeBtn) removeBtn.addEventListener("click", clearSelectedUploadFile);
}

function clearSelectedUploadFile() {
  state.pendingLocalFiles = null;
  state.pendingLocalLabel = null;
  const holder = el("local-file-label");
  if (holder) { holder.innerHTML = ""; holder.className = "file-label"; }
  _refreshWizardFooter();
  _syncUploadSubmitButton();
}

// status: "ready" | "uploading" | "detecting" | "completed" | "failed"
// progress: real percentage 0–100, or null for an indeterminate bar.
function setUploadStatus(status, progress, message) {
  const card = document.querySelector("#local-file-label .upload-file-card");
  if (!card) return;
  card.dataset.status = status;
  const statusEl = card.querySelector(".ufc-status");
  const bar      = card.querySelector(".ufc-progress");
  const fill     = card.querySelector(".ufc-progress-fill");
  const errEl    = card.querySelector(".ufc-error");
  const remove   = card.querySelector(".ufc-remove");
  const labels = {
    ready: "Ready to upload",
    uploading: Number.isFinite(progress) ? `Uploading… ${Math.round(progress)}%` : "Uploading…",
    detecting: "Detecting…",
    completed: "Completed",
    failed: "Failed",
  };
  if (statusEl) statusEl.textContent = labels[status] || status;
  const busy = status === "uploading" || status === "detecting";
  if (bar && fill) {
    bar.hidden = !(busy || status === "completed");
    bar.classList.toggle("ufc-progress--indeterminate", busy && !Number.isFinite(progress));
    if (status === "completed") fill.style.width = "100%";
    else if (Number.isFinite(progress)) fill.style.width = `${Math.max(0, Math.min(100, progress))}%`;
    else if (status === "detecting") fill.style.width = "100%";
  }
  if (errEl) {
    errEl.hidden = !(status === "failed" && message);
    errEl.textContent = status === "failed" && message ? message : "";
  }
  if (remove) remove.disabled = busy;   // no removal mid-upload
}

// Upload with real progress via XMLHttpRequest (fetch cannot expose it).
function _xhrUpload(url, formData, onProgress) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    xhr.open("POST", url);
    xhr.responseType = "json";
    if (xhr.upload && typeof onProgress === "function") {
      xhr.upload.onprogress = (e) => {
        if (e.lengthComputable && e.total > 0) onProgress((e.loaded / e.total) * 100);
        else onProgress(null);   // size unknown, indeterminate, never faked
      };
    }
    xhr.onload = () => {
      const data = xhr.response || {};
      if (xhr.status >= 200 && xhr.status < 300) resolve(data);
      else reject(new Error(data.detail || "Upload failed."));
    };
    xhr.onerror   = () => reject(new Error("Upload failed. Is the server running?"));
    xhr.ontimeout = () => reject(new Error("Upload timed out."));
    xhr.send(formData);
  });
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

  const zipCount = files.filter((f) => f.name.toLowerCase().endsWith(".zip")).length;
  const isSingleZip = files.length === 1 && zipCount === 1;
  const isMultiZip = files.length > 1 && zipCount === files.length;
  const onProgress = (pct) => {
    // Real transfer progress only; once fully sent, the server is detecting.
    if (Number.isFinite(pct) && pct >= 100) setUploadStatus("detecting");
    else setUploadStatus("uploading", pct);
  };

  const fd = new FormData();
  if (isSingleZip) {
    fd.append("file", files[0]);
    return _xhrUpload(`${API}/api/upload-batch`, fd, onProgress);
  }
  // Several ZIPs at once → merge into one batch; each submission keeps its own
  // detected challenge so a mixed upload (e.g. ASL + DCE) stays scoped.
  if (isMultiZip) {
    files.forEach((f) => fd.append("files", f, f.name));
    return _xhrUpload(`${API}/api/upload-submissions`, fd, onProgress);
  }
  files.forEach((f) => fd.append("files", f, f.webkitRelativePath || f.name));
  return _xhrUpload(`${API}/api/upload-folder-batch`, fd, onProgress);
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
    if (source === "local") {
      setUploadStatus("uploading", null);
      importData = await uploadLocalFiles();
      setUploadStatus("completed", 100);
    }
    else if (source === "zenodo") { showSubmitStatus("info", "Importing from Zenodo…");  importData = await importZenodo();  }
    else if (source === "github") { showSubmitStatus("info", "Importing from GitHub…");  importData = await importGithub();  }

    state.pendingLocalFiles = null;
    state.pendingLocalLabel = null;
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
      // ── Single submission, normalize to index step ────────────────────
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
          detected_challenge_type: importData.detected_challenge_type || null,
          challenge_type: getChallengeType(),
          confirmed_challenge_type: getChallengeType(),
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
    if (getSourceType() === "local") {
      setUploadStatus("failed", null, err.message || "Upload failed.");
    }
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
  if (sub.status === "failed") return "errors";
  if (_indexSubmissionTypeValue(sub) === "result-only") return "skipped";
  if (sub.detection_warning || info.state === "warning") return "warnings";
  if (!sub.challenge_type || (!sub.detected_parameter_map_type && !sub.map_type)) return "warnings";
  return "ready";
}

function _renderIndexFilterBar(submissions) {
  const selectionControls = (submissions || []).length > 1
    ? `<span class="index-selection-group">
       <button type="button" class="index-selection-link" id="index-select-all-btn">Select all</button>
       <button type="button" class="index-selection-link" id="index-deselect-all-btn">Clear</button>
       </span>`
    : "";
  return `<div class="filter-bar compact-filter-bar review-filter-bar" id="index-filter-bar">
    ${_renderSearchBox("index-search", _indexFilter.search, "Search submissions...")}
    ${_renderFilterDropdown("index-status", "Status", _indexFilter.status, [
      { value: "all", label: "All" },
      { value: "ready", label: "Ready" },
      { value: "warnings", label: "Warnings" },
      { value: "errors", label: "Errors" },
      { value: "skipped", label: "Skipped" },
    ])}
    ${_renderFilterDropdown("index-map", "Map", _indexFilter.map, MAP_FILTER_OPTIONS)}
    ${selectionControls}
  </div>`;
}

function _filterIndexSubmissions(submissions) {
  const q = (_indexFilter.search || "").trim().toLowerCase();
  const filtered = (submissions || []).filter((sub) => {
    const challenge = String(sub.challenge_type || getChallengeType() || "other").toLowerCase();
    const mapType = String(sub.detected_parameter_map_type || sub.map_type || "").toLowerCase();
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
    if (_indexFilter.status !== "all" && status !== _indexFilter.status) return false;
    if (_indexFilter.map !== "all" && mapType !== String(_indexFilter.map).toLowerCase()) return false;
    return true;
  });
  const withIndex = filtered.map((sub, idx) => ({ sub, idx }));
  withIndex.sort((a, b) => {
    if (_indexFilter.sort === "oldest") return a.idx - b.idx;
    if (_indexFilter.sort === "name") {
      return submissionDisplayName(a.sub, `Submission ${a.idx + 1}`)
        .localeCompare(submissionDisplayName(b.sub, `Submission ${b.idx + 1}`));
    }
    if (_indexFilter.sort === "status") {
      return _indexSubmissionStatusValue(a.sub).localeCompare(_indexSubmissionStatusValue(b.sub));
    }
    return b.idx - a.idx;
  });
  return withIndex.map((item) => item.sub);
}

function _indexListSummary(submissions) {
  const items = submissions || [];
  const selectedCount = items.filter((s) => batchState.selectedIds.has(s.submission_id)).length;
  return [
    _summaryChip("detected", items.length),
    _summaryChip("selected", selectedCount),
  ].filter(Boolean).join("");
}

function _wireIndexFilterBar() {
  const search = el("index-search");
  if (search) {
    search.oninput = () => {
      const cursor = search.selectionStart ?? search.value.length;
      _indexFilter.search = search.value;
      if (batchState.uploadData) {
        renderBatchTable(batchState.uploadData.submissions || []);
        _restoreSearchFocus("index-search", cursor);
      }
    };
  }
  const clear = el("index-clear-filters");
  if (clear) {
    clear.onclick = () => {
      _indexFilter.search = "";
      _indexFilter.status = "all";
      _indexFilter.map = "all";
      _indexFilter.sort = "newest";
      _indexFilter.showAll = false;
      if (batchState.uploadData) renderBatchTable(batchState.uploadData.submissions || []);
    };
  }
  const selectAll = el("index-select-all-btn");
  if (selectAll) {
    selectAll.onclick = () => {
      if (!batchState.uploadData) return;
      batchState.uploadData.submissions.forEach((s) => batchState.selectedIds.add(s.submission_id));
      renderBatchTable(batchState.uploadData.submissions);
      saveSessionState();
    };
  }
  const deselectAll = el("index-deselect-all-btn");
  if (deselectAll) {
    deselectAll.onclick = () => {
      batchState.selectedIds.clear();
      if (batchState.uploadData) renderBatchTable(batchState.uploadData.submissions || []);
      saveSessionState();
    };
  }
}

// ── Compact submission rows: map-type labels + metadata line ─────────────────
// Display-only resolution of configured map labels from existing metadata and
// NIfTI filenames. Backend detection is unchanged.

function _mapTypesFromFilenames(names) {
  const found = [];
  (names || []).forEach((raw) => {
    const name = String(raw || "").toLowerCase().split("/").pop();
    _ROW_MAP_PATTERNS.forEach(([label, re]) => {
      if (re.test(name) && !found.includes(label)) found.push(label);
    });
  });
  return found;
}

// Visible map label for a row: resolved configured labels beat "Mixed/Other".
function _subMapTypesLabel(sub) {
  if (Array.isArray(sub._resolvedMapTypes) && sub._resolvedMapTypes.length) {
    return sub._resolvedMapTypes.join(", ");
  }
  const t = sub.detected_parameter_map_type || sub.map_type;
  if (!t || t === "Unknown") return "Map not detected";
  if (t === "Mixed/Other") return "Multiple maps";
  return t;
}

// Uppercase challenge key for a submission ("" when unknown), used to group
// the Review list by challenge so ASL/DCE/DSC stay visually separate.
function _subChallengeKey(sub) {
  const c = sub && (sub.confirmed_challenge_type || sub.challenge_type || sub.detected_challenge_type);
  return c && String(c).toLowerCase() !== "unknown" ? String(c).toUpperCase() : "";
}

// One-line row summary: challenge, map labels, and map count.
function _subRowMetaLine(sub) {
  // Collapsed row shows only the essentials; NIfTI count and readiness live in Details.
  const niftiCount = sub.nifti_count;
  // Before validation the row only has the ingestion-detected challenge; after
  // validation it has the confirmed challenge_type. Prefer the confirmed one.
  const detected = sub.detected_challenge_type && String(sub.detected_challenge_type).toLowerCase() !== "unknown"
    ? sub.detected_challenge_type : null;
  const parts = [
    challengeLabel(sub.confirmed_challenge_type || sub.challenge_type || detected),
    _subMapTypesLabel(sub),
    Number.isFinite(Number(niftiCount)) ? `${niftiCount} map${Number(niftiCount) === 1 ? "" : "s"}` : null,
  ].filter(Boolean);
  return escapeHtml(parts.join(" · "));
}

function _reviewChallengeSuggestion(sub) {
  const mapTypes = Array.isArray(sub?._resolvedMapTypes) ? sub._resolvedMapTypes : [];
  if (!mapTypes.length) return null;

  const mapIdByLabel = {};
  (_appConfig.mapTypes || []).forEach((item) => {
    const id = String(item?.id || "").toLowerCase();
    if (!id) return;
    mapIdByLabel[id] = id;
    mapIdByLabel[String(item?.display || id).toLowerCase()] = id;
  });
  const foundIds = new Set(mapTypes.map((label) => mapIdByLabel[String(label).toLowerCase()]).filter(Boolean));
  if (!foundIds.size) return null;

  const scores = (_appConfig.challengeTypes || []).map((challenge) => {
    const expected = new Set((challenge.expected_maps || []).map((id) => String(id).toLowerCase()));
    return {
      id: String(challenge.id || "").toLowerCase(),
      label: challenge.label || String(challenge.id || "").toUpperCase(),
      score: [...foundIds].filter((id) => expected.has(id)).length,
    };
  }).filter((item) => item.id);
  scores.sort((a, b) => b.score - a.score);
  const best = scores[0];
  if (!best || best.score === 0 || scores[1]?.score === best.score) return null;

  const currentId = String(
    sub?.confirmed_challenge_type || sub?.challenge_type || sub?.detected_challenge_type || getChallengeType()
  ).toLowerCase();
  const currentScore = scores.find((item) => item.id === currentId)?.score || 0;
  if (best.id === currentId || best.score <= currentScore) return null;
  return { ...best, currentId, currentLabel: challengeLabel(currentId) };
}

function _syncReviewChallengeSuggestion(sub, rowEl) {
  const host = rowEl?.querySelector("[data-review-challenge-suggestion]");
  if (!host) return;
  const suggestion = _reviewChallengeSuggestion(sub);
  if (!suggestion) {
    host.hidden = true;
    host.innerHTML = "";
    return;
  }
  host.hidden = false;
  host.innerHTML = `<div><strong>Check challenge selection.</strong> These map filenames match ${escapeHtml(suggestion.label)} more closely than ${escapeHtml(suggestion.currentLabel)}.</div>
    <button type="button" class="review-challenge-use" data-use-review-challenge="${escapeHtml(suggestion.id)}"
            data-review-submission-id="${escapeHtml(sub.submission_id || "")}">Use ${escapeHtml(suggestion.label)}</button>`;
}

function _displayNameEditorHtml(sub, fallback) {
  const sid = sub?.submission_id || fallback || "";
  const displayName = getSubmissionDisplayName(sub, fallback);
  const inputId = `display-name-${String(sid || "submission").replace(/[^A-Za-z0-9_-]/g, "-")}`;
  return `<div class="display-name-editor">
    <label class="display-name-label" for="${escapeHtml(inputId)}">Display name</label>
    <div class="display-name-row">
      <input type="text" class="display-name-input" id="${escapeHtml(inputId)}"
             data-display-name-input="${escapeHtml(sid)}"
             value="${escapeHtml(displayName)}"
             aria-label="Display name for ${escapeHtml(displayName)}">
      <button type="button" class="btn btn-secondary btn-sm display-name-save"
              data-save-display-name="${escapeHtml(sid)}">Save</button>
    </div>
    <p class="display-name-note">Changes only the name shown in this app.</p>
  </div>`;
}

// For "Mixed/Other"/"Unknown" rows, look up the real NIfTI filenames already
// stored on the backend and upgrade the visible label to configured map names.
function _resolveRowMapTypes(sub, rowEl) {
  const t = sub.detected_parameter_map_type || sub.map_type;
  const syncChallengeValue = () => {
    const value = rowEl?.querySelector(".sub-field-challenge-type .sub-field-value");
    if (value) {
      value.textContent = challengeLabel(
        sub.confirmed_challenge_type || sub.challenge_type || sub.detected_challenge_type,
      );
    }
  };
  if (sub._resolvedMapTypes) {
    syncChallengeValue();
    _syncReviewChallengeSuggestion(sub, rowEl);
    return;
  }
  if (!(t === "Mixed/Other" || t === "Unknown" || !t)) return;
  if (!sub.submission_id || !(Number(sub.nifti_count) > 0)) return;
  fetch(`${API}/api/nifti-files/${encodeURIComponent(sub.submission_id)}`)
    .then((res) => (res.ok ? res.json() : null))
    .then((data) => {
      const types = _mapTypesFromFilenames(data?.files || []);
      if (!types.length) return;
      sub._resolvedMapTypes = types;
      const meta = rowEl?.querySelector(".sub-row-meta");
      if (meta) meta.innerHTML = _subRowMetaLine(sub);
      const detailVal = rowEl?.querySelector(".sub-row-detail .sub-field-map-types .sub-field-value");
      if (detailVal) detailVal.textContent = types.join(", ")
      + (t === "Mixed/Other" ? " (detected as Mixed/Other)" : "");
      syncChallengeValue();
      _syncReviewChallengeSuggestion(sub, rowEl);
    })
    .catch(() => { /* display-only enhancement, ignore failures */ });
}

// ── Submission Structure popover ─────────────────────────────────────────────
// A compact, click-to-open folder tree of a submission's contents. Built as a
// best-effort tree from the submission's known file paths via the existing
// /api/nifti-files endpoint (no backend changes). Reused by Review + Validate.

const _structureCache = {};   // submissionId -> array of relative file paths

function _buildFileTree(paths) {
  const root = { children: {} };
  (paths || []).forEach((raw) => {
    const parts = String(raw || "").replace(/\\/g, "/").split("/").filter(Boolean);
    let node = root;
    parts.forEach((part, i) => {
      const isFile = i === parts.length - 1;
      node.children[part] = node.children[part] || { name: part, isFile, children: {} };
      node = node.children[part];
    });
  });
  return root;
}

function _renderStructureNodes(node, depth) {
  const entries = Object.values(node.children || {});
  // Folders first, then files; each group sorted alphabetically.
  entries.sort((a, b) => (a.isFile === b.isFile) ? a.name.localeCompare(b.name) : (a.isFile ? 1 : -1));
  return entries.map((e) => {
    const icon = e.isFile ? "File" : "Folder";
    const row = `<div class="structure-tree-item">
      <span class="structure-tree-indent" style="width:${depth * 15}px" aria-hidden="true"></span>
      <span class="structure-tree-icon" aria-hidden="true">${icon}</span>
      <span class="structure-file-name">${escapeHtml(e.name)}</span>
    </div>`;
    return row + (e.isFile ? "" : _renderStructureNodes(e, depth + 1));
  }).join("");
}

function renderSubmissionStructure(files) {
  if (!files || !files.length) {
    return `<div class="structure-empty">No map files available for this submission.</div>`;
  }
  return `<div class="structure-tree">${_renderStructureNodes(_buildFileTree(files), 0)}</div>`;
}

function closeStructurePopovers(except) {
  document.querySelectorAll(".structure-popover").forEach((pop) => {
    if (pop === except) return;
    pop.hidden = true;
    const trig = pop.closest(".structure-control")?.querySelector(".structure-trigger");
    if (trig) trig.setAttribute("aria-expanded", "false");
  });
}

function _positionStructurePopover(pop, trigger) {
  if (!pop || !trigger) return;
  pop.classList.remove("opens-upward");
  const triggerRect = trigger.getBoundingClientRect();
  const actionRow = trigger.closest(".step-shell")?.querySelector(".step-action-row");
  const lowerBoundary = actionRow?.getBoundingClientRect().top || window.innerHeight;
  const availableBelow = lowerBoundary - triggerRect.bottom - 12;
  const availableAbove = triggerRect.top - 12;
  const desiredHeight = Math.min(pop.scrollHeight || 260, 260);
  if (availableBelow < Math.min(desiredHeight, 180) && availableAbove > availableBelow) {
    pop.classList.add("opens-upward");
  }
}

function toggleStructurePopover(sid, trigger) {
  // Look up the popover next to THIS trigger (ids can repeat across steps).
  const pop = trigger?.closest(".structure-control")?.querySelector(".structure-popover")
    || el(`structure-pop-${sid}`);
  if (!pop) return;
  const opening = pop.hidden;
  closeStructurePopovers(opening ? pop : null);
  if (!opening) {
    pop.hidden = true;
    trigger?.setAttribute("aria-expanded", "false");
    return;
  }
  pop.hidden = false;
  trigger?.setAttribute("aria-expanded", "true");
  if (pop.dataset.loaded === "1") {
    _positionStructurePopover(pop, trigger);
    return;
  }
  if (_structureCache[sid]) {
    pop.innerHTML = renderSubmissionStructure(_structureCache[sid]);
    pop.dataset.loaded = "1";
    _positionStructurePopover(pop, trigger);
    return;
  }
  pop.innerHTML = `<div class="structure-loading">Loading file structure…</div>`;
  _positionStructurePopover(pop, trigger);
  fetch(`${API}/api/nifti-files/${encodeURIComponent(sid)}`)
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      const files = (data && data.files) || [];
      _structureCache[sid] = files;
      pop.innerHTML = renderSubmissionStructure(files);
      pop.dataset.loaded = "1";
      _positionStructurePopover(pop, trigger);
    })
    .catch(() => { pop.innerHTML = renderSubmissionStructure(null); });
}

// Reusable markup for the trigger + (empty, lazy-loaded) popover.
// The popover id is unique per instance (same submission can appear in both
// the Review and Validate lists), while the tree lookup stays relative.
let _structureUid = 0;
function _structureControlHtml(sid) {
  const safeSid = escapeHtml(sid || "");
  const domId = `structure-pop-${++_structureUid}`;
  return `<div class="structure-control">
    <button type="button" class="structure-trigger" data-structure-id="${safeSid}" aria-controls="${domId}" aria-expanded="false" aria-haspopup="true">
      <span class="structure-trigger-icon" aria-hidden="true">Files</span> Map Files
    </button>
    <div class="structure-popover" id="${domId}" role="dialog" aria-label="Included NIfTI map files" hidden></div>
  </div>`;
}

// Delegated events: click to open/close, click-outside and Escape to close.
document.addEventListener("click", (e) => {
  const trig = e.target.closest(".structure-trigger");
  if (trig) {
    e.preventDefault();
    e.stopPropagation();
    toggleStructurePopover(trig.getAttribute("data-structure-id"), trig);
    return;
  }
  if (!e.target.closest(".structure-popover")) closeStructurePopovers();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") closeStructurePopovers();
});

// ══════════════════════════════════════════════════════════════════════════════
// Shared worklist component system
// ------------------------------------------------------------------------------
// ONE renderer used by every step (Review, Validate, Run, Score, Map Preview,
// Export). Each row produces the SAME structure, only the content differs:
//   <row>
//     [checkbox]
//     <worklist-icon>
//     <worklist-main>
//       <worklist-row-head> <worklist-title> [<worklist-status>] </>
//       [lead]  [worklist-meta]  [extraMain]  [worklist-details]
//     </worklist-main>
//     [worklist-actions]
//     [chevron]
//   </row>
// Legacy per-step class names are passed through as slot aliases so existing
// CSS/JS hooks keep working, but the STRUCTURE comes from here only.
// ══════════════════════════════════════════════════════════════════════════════

function _wlCls(base, extra) { return extra ? `${base} ${extra}` : base; }

function _wlDataAttrs(dataset) {
  return Object.entries(dataset || {})
    .map(([k, v]) => ` data-${k.replace(/[A-Z]/g, (m) => "-" + m.toLowerCase())}="${escapeHtml(String(v))}"`)
    .join("");
}

function renderWorklistRow(o = {}) {
  const tag = o.tag || "div";
  const rowClass = ["worklist-row", o.extraClass, o.selected ? "is-selected" : ""].filter(Boolean).join(" ");
  const attrs = (o.attrs ? " " + o.attrs : "") + _wlDataAttrs(o.dataset);
  const checkbox = o.checkbox || "";
  const icon = o.iconHtml != null
    ? o.iconHtml
    : (o.icon != null ? `<div class="${_wlCls("worklist-icon", o.iconClass)}" aria-hidden="true">${o.icon}</div>` : "");
  const head = (o.title != null || o.statusHtml)
    ? `<div class="${_wlCls("worklist-row-head", o.headClass)}">`
      + (o.title != null ? `<span class="${_wlCls("worklist-title", o.titleClass)}"${o.titleAttrs ? " " + o.titleAttrs : ""}>${o.title}</span>` : "")
      + (o.statusHtml ? `<span class="${_wlCls("worklist-status", o.statusClass)}">${o.statusHtml}</span>` : "")
      + `</div>`
    : "";
  const meta = o.metaHtml ? `<div class="${_wlCls("worklist-meta", o.metaClass)}">${o.metaHtml}</div>` : "";
  const hideAttr = o.detailsHidden === false ? "" : (o.detailsHiddenAttr || " hidden");
  const details = o.detailsHtml
    ? `<div class="${_wlCls("worklist-details", o.detailsClass)}"${hideAttr}>${o.detailsHtml}</div>`
    : "";
  const main = `<div class="${_wlCls("worklist-main", o.mainClass)}">${head}${o.lead || ""}${meta}${o.extraMain || ""}${details}</div>`;
  // ONE shared Details control on every row that has secondary info.
  const detailsBtn = (o.detailsHtml && o.detailsToggle !== false)
    ? `<button type="button" class="details-toggle" aria-expanded="false">Details</button>`
    : "";
  const actionsInner = (o.actionsHtml || "") + detailsBtn;
  const actions = actionsInner ? `<div class="${_wlCls("worklist-actions", o.actionsClass)}">${actionsInner}</div>` : "";
  const chevron = "";
  return `<${tag} class="${rowClass}"${attrs}>${checkbox}${icon}${main}${actions}${chevron}</${tag}>`;
}

// ONE shared Details / Hide details toggle for every worklist row.
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".details-toggle");
  if (!btn) return;
  const row = btn.closest(".worklist-row");
  const detail = row && row.querySelector(".worklist-details");
  if (!detail) return;
  e.preventDefault();
  e.stopPropagation();
  const isOpen = detail.hasAttribute("hidden") || detail.style.display === "none";
  if (isOpen) { detail.hidden = false; detail.style.display = ""; }
  else { detail.hidden = true; }
  btn.setAttribute("aria-expanded", String(isOpen));
  btn.textContent = isOpen ? "Hide details" : "Details";
  row.classList.toggle("is-expanded", isOpen);
});

document.addEventListener("click", (e) => {
  const btn = e.target.closest("[data-save-display-name]");
  if (!btn) return;
  e.preventDefault();
  e.stopPropagation();
  const sid = btn.getAttribute("data-save-display-name") || "";
  const input = btn.closest(".display-name-editor")?.querySelector(".display-name-input");
  const displayName = _setSubmissionDisplayAlias(sid, input?.value || "");
  if (input) input.value = displayName;
  _refreshDisplayNameDom(sid);
  saveSessionState();
});

document.addEventListener("keydown", (e) => {
  const input = e.target.closest?.(".display-name-input");
  if (!input || e.key !== "Enter") return;
  e.preventDefault();
  input.closest(".display-name-editor")?.querySelector("[data-save-display-name]")?.click();
});

// File-style row (no checkbox/details/chevron): selected upload, map preview,
// and export rows all share this so file-like objects look identical.
function renderFileRow(o = {}) {
  return renderWorklistRow({
    tag: o.tag || "div",
    extraClass: ["worklist-file-row", o.extraClass].filter(Boolean).join(" "),
    dataset: o.dataset,
    attrs: o.attrs,
    iconHtml: o.iconHtml,
    icon: o.icon,
    iconClass: o.iconClass,
    title: o.title,
    titleClass: o.titleClass,
    titleAttrs: o.titleAttrs,
    metaHtml: o.metaHtml,
    metaClass: o.metaClass,
    statusHtml: o.statusHtml,
    statusClass: o.statusClass,
    actionsHtml: o.actionsHtml,
    actionsClass: o.actionsClass,
    lead: o.lead,
  });
}

// Shared section header/body. _renderCollapsibleSection delegates here so the
// section-row / section-title / section-count / section-actions structure is
// produced in exactly one place.
function renderSection(o = {}) {
  const key = o.key || "section";
  const open = true;
  const actions = o.actions
    ? `<span class="section-actions">${o.actions}</span>`
    : `<span class="section-actions"></span>`;
  return `<section class="collapsible-section" id="${escapeHtml(key)}-list-section">
    <div class="collapsible-section-header section-row">
      <span class="collapsible-section-title section-title">${escapeHtml(o.title || "")} ${_renderCountBadge(o.count)}</span>
      ${actions}
    </div>
    <div id="${escapeHtml(key)}-section-summary" class="list-summary-strip" hidden>${o.summaryHtml || ""}</div>
    <div id="${escapeHtml(key)}-list-body" class="collapsible-section-body">${o.bodyHtml || ""}</div>
  </section>`;
}

// Build a worklist-row DOM element (not a string) for builders that need to
// wire events on the element afterwards.
function _worklistRowEl(opts) {
  const tmp = document.createElement("div");
  tmp.innerHTML = renderWorklistRow(opts).trim();
  return tmp.firstElementChild;
}

// Render detected submissions as compact list rows. Selection state and
// action-row validation both work through batchState.selectedIds.
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
  const controls = document.querySelector("#step-index .batch-controls");
  const controlsLeft = controls?.querySelector(".batch-controls-left");
  const controlsRight = controls?.querySelector(".batch-controls-right");
  if (controls) controls.style.display = "none";
  if (controlsLeft) controlsLeft.style.display = "none";
  if (controlsRight) controlsRight.style.display = "none";

  const visibleSubmissions = isSingle ? safeSubmissions : _filterIndexSubmissions(safeSubmissions);
  const visibleTotal = visibleSubmissions.length;
  // Group the list by challenge when a mixed batch (e.g. ASL + DCE) is present.
  const _challengesPresent = [...new Set(safeSubmissions.map(_subChallengeKey).filter(Boolean))];
  const groupByChallenge = !isSingle && _challengesPresent.length > 1;
  const orderedVisible = groupByChallenge
    ? [...visibleSubmissions].sort((a, b) => _subChallengeKey(a).localeCompare(_subChallengeKey(b)))
    : visibleSubmissions;
  const LIMIT = 5;
  const renderSubmissions = (!isSingle && !_indexFilter.showAll && visibleTotal > LIMIT)
    ? orderedVisible.slice(0, LIMIT)
    : orderedVisible;

  const selectedCount = safeSubmissions.filter((s) => batchState.selectedIds.has(s.submission_id)).length;
  const summaryText = safeSubmissions.length > 1
    ? `${safeSubmissions.length} submissions · ${selectedCount} selected`
    : `${safeSubmissions.length} submission detected`;
  if (desc && safeSubmissions.length > 0) desc.textContent = summaryText;
  const countEl = el("index-section-count");
  if (countEl) countEl.textContent = String(safeSubmissions.length);
  const summaryEl = el("index-section-summary");
  if (summaryEl) summaryEl.innerHTML = _indexListSummary(safeSubmissions);
  _setCollapsibleSectionOpen("index", true);
  const body = wrap;

  const toolbar = el("index-toolbar");
  if (toolbar) {
    toolbar.innerHTML = !isSingle ? _renderIndexFilterBar(safeSubmissions) : "";
    toolbar.style.display = !isSingle ? "" : "none";
  }
  if (!isSingle) {
    _wireIndexFilterBar();
  }

  const list = document.createElement("div");
  list.className = "worklist sub-row-list" + (isSingle ? " sub-row-list--single" : "");

  let _lastGroupChallenge = null;
  renderSubmissions.forEach((sub, idx) => {
    if (groupByChallenge) {
      const chKey = _subChallengeKey(sub) || "UNKNOWN";
      if (chKey !== _lastGroupChallenge) {
        _lastGroupChallenge = chKey;
        const groupHeader = document.createElement("div");
        groupHeader.className = "sub-challenge-group-header";
        groupHeader.textContent =
          (challengeLabel(sub.challenge_type || sub.detected_challenge_type) || "Other") + " submissions";
        list.appendChild(groupHeader);
      }
    }
    const isSelected = batchState.selectedIds.has(sub.submission_id);
    const typeInfo = submissionTypeInfo(sub);
    const safeSubId = escapeHtml(sub.submission_id || "");
    const safeMapLabel = escapeHtml(_subMapTypesLabel(sub));
    const safeName = escapeHtml(submissionDisplayName(sub, `Submission ${idx + 1}`));
    const safeOriginalName = escapeHtml(originalSubmissionName(sub, `Submission ${idx + 1}`));
    const reviewChallenge = sub.confirmed_challenge_type
      || sub.challenge_type
      || sub.detected_challenge_type
      || getChallengeType()
      || defaultChallengeType();
    const safeChallenge = escapeHtml(String(reviewChallenge || "Unknown").toUpperCase());
    const safeType = escapeHtml(typeInfo.label);
    const subTypeHelp = typeInfo.state === "skipped"
      ? "This means the submission already includes output maps, so Docker execution may be skipped."
      : "Submission type indicates whether output maps are provided or code must be run.";
    const niftiCount   = sub.nifti_count ?? "—";
    const readinessChip = _indexSubmissionTypeValue(sub) === "result-only" ? "Result maps provided"
      : _indexSubmissionTypeValue(sub) === "runnable" ? "Runnable" : "Needs review";

    const detailsHtml = `
          ${_displayNameEditorHtml(sub, `Submission ${idx + 1}`)}
          <div class="sub-card-fields">
            <div class="sub-field sub-field-wide">
              <span class="sub-field-label">Original submission name</span>
              <span class="sub-field-value original-submission-name">${safeOriginalName}</span>
            </div>
            <div class="sub-field sub-field-challenge-type">
              <span class="sub-field-label">Challenge type ${helpTooltip("Select the OSIPI challenge type for this submission.", "Challenge type help")}</span>
              <span class="sub-field-value">${safeChallenge}</span>
            </div>
            <div class="sub-field sub-field-map-types">
              <span class="sub-field-label">Map types ${helpTooltip("Optional. The app can auto-detect configured parameter maps from filenames and metadata.", "Parameter map type help")}</span>
              <span class="sub-field-value">${safeMapLabel}${sub.detected_parameter_map_type === "Mixed/Other" ? ` <span class="sub-field-note">(detected as Mixed/Other)</span>` : ""}</span>
            </div>
            <div class="sub-field">
              <span class="sub-field-label">Map count</span>
              <span class="sub-field-value">${escapeHtml(niftiCount)}</span>
            </div>
            <div class="sub-field">
              <span class="sub-field-label">Original NIfTI count</span>
              <span class="sub-field-value">${escapeHtml(niftiCount)}</span>
            </div>
            <div class="sub-field">
              <span class="sub-field-label">Result maps / readiness ${helpTooltip(subTypeHelp, typeInfo.state === "skipped" ? "Result maps provided help" : "Submission type help")}</span>
              <span class="sub-field-value">${escapeHtml(readinessChip)}</span>
            </div>
            <div class="sub-field">
              <span class="sub-field-label">Submission type</span>
              <span class="sub-field-value">${safeType}</span>
            </div>
          </div>
          ${sub.detection_warning ? `<p class="sub-row-warning">${escapeHtml(sub.detection_warning)}</p>` : ""}
          <div class="sub-detail-files">${_structureControlHtml(sub.submission_id)}</div>`;

	    const card = _worklistRowEl({
	      extraClass: "sub-row guided-sub-card" + (isSingle ? " sub-row--single sub-card--single" : ""),
	      selected: isSelected,
      dataset: { subCard: sub.submission_id },
      checkbox: isSingle ? "" : `<input type="checkbox" class="worklist-checkbox sub-card-check sub-row-check" data-id="${escapeHtml(sub.submission_id)}" ${isSelected ? "checked" : ""} aria-label="Select ${safeName}" />`,
      iconHtml: submissionFileIconHtml(),
      title: safeName, titleClass: "sub-row-name", titleAttrs: `title="${safeName}" data-display-name-for="${safeSubId}"`,
	      headClass: "sub-row-top",
	      metaHtml: _subRowMetaLine(sub), metaClass: "sub-row-meta",
	      extraMain: `<div class="review-challenge-suggestion" data-review-challenge-suggestion hidden></div>`,
	      detailsHtml, detailsClass: "sub-row-detail",
	    });

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
    // Clicking the row toggles selection, except tooltips, actions, details.
    card.addEventListener("click", (e) => {
      if (isSingle) return;
      if (e.target.closest(".help-tooltip, .worklist-actions, .details-toggle, .worklist-details, .structure-control")) return;
      if (e.target === cb) return;  // checkbox handles its own change
      setSelected(!cb?.checked);
    });

    list.appendChild(card);
    _resolveRowMapTypes(sub, card);
  });

  body.appendChild(list);
  if (!visibleTotal) {
    body.insertAdjacentHTML("beforeend", `<div class="list-empty-state">
      <p>No submissions match these filters.</p>
      <button type="button" class="btn btn-secondary btn-sm" id="index-empty-clear">Clear filters</button>
    </div>`);
    const emptyClear = el("index-empty-clear");
    if (emptyClear) emptyClear.onclick = () => {
      _indexFilter.search = "";
      _indexFilter.status = "all";
      _indexFilter.map = "all";
      _indexFilter.sort = "newest";
      _indexFilter.showAll = false;
      renderBatchTable(safeSubmissions);
    };
  } else if (!isSingle && visibleTotal > LIMIT) {
    body.insertAdjacentHTML("beforeend", `<div class="show-more-row" id="index-show-all-wrap">
      <button type="button" id="index-show-all-btn" class="vr-show-all-btn">${_indexFilter.showAll ? "Show less" : `Show all ${visibleTotal} submissions`}</button>
    </div>`);
    const showAllBtn = el("index-show-all-btn");
    if (showAllBtn) {
      showAllBtn.onclick = () => {
        _indexFilter.showAll = !_indexFilter.showAll;
        renderBatchTable(safeSubmissions);
      };
    }
  }
  _syncBatchValidateBtn();
  _refreshWizardFooter();
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-use-review-challenge]");
  if (!button) return;
  event.preventDefault();
  event.stopPropagation();
  const submissionId = button.getAttribute("data-review-submission-id") || "";
  const challenge = button.getAttribute("data-use-review-challenge") || "";
  const submission = (batchState.uploadData?.submissions || []).find((item) => item.submission_id === submissionId);
  if (!submission || !challenge) return;

  submission.confirmed_challenge_type = challenge;
  submission.challenge_type = challenge;
  if (!batchState.isBatch) {
    const radio = document.querySelector(`input[name="challenge_type"][value="${challenge}"]`);
    if (radio) {
      radio.checked = true;
      radio.dispatchEvent(new Event("change", { bubbles: true }));
    }
  }
  renderBatchTable(batchState.uploadData.submissions || []);
  saveSessionState();
});

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
  batchNewBtn.addEventListener("click", _resetToUploadAndClearPersistence);
}

// ── Step 2→3: Validate ────────────────────────────────────────────────────────

// Build {submission_id: challenge} from ingestion's per-submission detection.
// Only confidently detected challenges are included; unknown/blank are omitted
// so the backend falls back to the globally selected challenge for those.
function _perSubmissionChallengeMap(submissionIds) {
  const subs = batchState.uploadData?.submissions || [];
  const byId = Object.fromEntries(subs.map((s) => [s.submission_id, s]));
  const map = {};
  submissionIds.forEach((id) => {
    const c = byId[id]?.confirmed_challenge_type || byId[id]?.detected_challenge_type;
    if (c && String(c).toLowerCase() !== "unknown") map[id] = String(c).toLowerCase();
  });
  return Object.keys(map).length ? map : null;
}

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

    // Per-submission challenge overrides from ingestion detection, so a mixed
    // batch validates each submission under its own challenge. Only include
    // confidently detected challenges; the rest fall back to the selected one.
    const challengeTypesMap = _perSubmissionChallengeMap(submissionIds);

    const res  = await fetch(`${API}/api/validate-batch`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify({
        submission_ids:  submissionIds,
        challenge_type:  getChallengeType(),
        challenge_types: challengeTypesMap,
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

// ── Step 3: Validate, render validation cards ────────────────────────────────

const _reviewFilter = { filter: "all", search: "", map: "all", sort: "status", showAll: false };

// ── Issue summary helper, one brief text (not a list) ───────────────────────
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
  const active = !!((_reviewFilter.search || "").trim()
    || _reviewFilter.filter !== "all"
    || _reviewFilter.map !== "all");
  return `<div class="filter-bar compact-filter-bar validation-filter-bar">
    ${_renderSearchBox("batch-search", _reviewFilter.search, "Search submissions...")}
    ${_renderFilterDropdown("validation-status", "Status", _reviewFilter.filter, [
      { value: "all", label: "All" },
      { value: "ready", label: "Ready" },
      { value: "passed", label: "Passed" },
      { value: "warnings", label: "Warnings" },
      { value: "errors", label: "Errors" },
      { value: "failed", label: "Failed" },
      { value: "skipped", label: "Skipped" },
    ])}
    ${_renderFilterDropdown("validation-map", "Map", _reviewFilter.map, MAP_FILTER_OPTIONS)}
    ${_renderClearFilterButton("validation-clear-filters", active)}
  </div>`;
}

function _wireValidationFilterBar() {
  const searchEl = el("batch-search");
  if (searchEl) {
    searchEl.oninput = () => {
      const cursor = searchEl.selectionStart ?? searchEl.value.length;
      _reviewFilter.search = searchEl.value;
      _reviewFilter.showAll = false;
      _refreshValidationFilterBar();
      _applyReviewFilters();
      _restoreSearchFocus("batch-search", cursor);
    };
  }
  const clear = el("validation-clear-filters");
  if (clear) {
    clear.onclick = () => {
      _reviewFilter.filter = "all";
      _reviewFilter.search = "";
      _reviewFilter.map = "all";
      _reviewFilter.sort = "status";
      _reviewFilter.showAll = false;
      _refreshValidationFilterBar();
      _applyReviewFilters();
    };
  }
}

function _validationListSummary(results, issueDetails) {
  const rows = results || [];
  const passed = rows.filter((r) => r.passed).length;
  const warnings = rows.filter((r) => (issueDetails.get(r.submission_id)?.warnings.length || 0) > 0).length;
  const errors = rows.filter((r) => (issueDetails.get(r.submission_id)?.errors.length || 0) > 0).length;
  const runnable = rows.filter((r) => r.run_readiness === "runnable"
    || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile))).length;
  const resultOnly = rows.filter((r) => r.run_readiness === "result_only"
    || (r.passed && !r.has_run_instructions && (r.nifti_count > 0 || r.has_result_maps))).length;
  return [
    _summaryChip("checked", rows.length),
    _summaryChip("passed", passed, "success"),
    _summaryChip("warnings", warnings, warnings ? "warning" : ""),
    _summaryChip("errors", errors, errors ? "error" : ""),
    _summaryChip("runnable", runnable, "success"),
    _summaryChip("result-only", resultOnly, resultOnly ? "muted" : ""),
    _latestLabel(rows.length ? submissionDisplayName(rows[rows.length - 1], `Submission ${rows.length}`) : ""),
  ].filter(Boolean).join("");
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
  _reviewFilter.map     = "all";
  _reviewFilter.sort    = "status";
  _reviewFilter.showAll = false;
  const valToolbar = el("val-toolbar");
  if (valToolbar) valToolbar.innerHTML = _renderValidationFilterBar();
  const searchEl  = el("batch-search");
  if (searchEl) searchEl.value = "";

  // ── 1. Header summary: one short line, no dashboard/stat tiles ────────────
  const titleEl = el("validate-card-title");
  if (titleEl) {
    titleEl.textContent = validationTitle;
  }

  const statsEl = el("validate-summary-stats");
  if (statsEl) {
    statsEl.hidden = true;
    statsEl.innerHTML = "";
  }

  const desc = el("batch-results-desc");
  if (desc) {
    const parts = [
      `${checkedCount} submission${checkedCount === 1 ? "" : "s"}`,
      `${totalWarnings} warning${totalWarnings === 1 ? "" : "s"}`,
      `${totalErrors} error${totalErrors === 1 ? "" : "s"}`,
    ];
    desc.textContent = parts.join(" · ");
  }

  _syncCollapsibleSection("validation", results.length, _validationListSummary(results, issueDetails));

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
    list.classList.add("worklist");
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
      if (!passed)      { valStatus = "failed";  pillState = "error";    pillText = "Errors"; }
      else if (hasWarn) { valStatus = "warning"; pillState = "warning";  pillText = "Warnings"; }
      else              { valStatus = "passed";  pillState = "complete"; pillText = "Passed"; }

      let runTxt, runState;
      if (!passed)        { runTxt = "Cannot run"; runState = "error"; }
      else if (isResultOnly) { runTxt = "Result maps provided"; runState = "skipped"; }
      else if (runnable)  { runTxt = "Ready to run"; runState = "ready"; }
      else                { runTxt = "Cannot run"; runState = "warning"; }

      const execInitStatus = runnable ? "not-run" : "cannot-run";

      const safeSubId     = escapeHtml(r.submission_id);
      const safeName      = escapeHtml(submissionDisplayName(r, `Submission ${idx + 1}`));
      const safeChallenge = escapeHtml(r.challenge_type || getChallengeType() || defaultChallengeType());
      const safeMap       = escapeHtml(r.map_type || "Not detected");
      const subType       = submissionTypeInfo(r);
      const subTypeHelp   = isResultOnly
        ? "This submission already includes result maps, so no processing run is needed."
        : "Indicates whether result maps are included or processing must be run.";

      // Detail content
      // Role-based counts, computed once by the backend. The old line showed
      // the raw NIfTI file count, which counted fitted signals and organiser
      // reference data as parameter maps.
      const countSummary = submissionCountSummary(r);
      const niftiLine = countSummary
        ? `<div class="vr-detail-nifti">${escapeHtml(countSummary)}</div>`
        : (rNiftiCount > 0
            ? `<div class="vr-detail-nifti">Map count: <strong>${rNiftiCount}</strong></div>` : "");
      // Blocking errors keep red styling; everything else is a calm "Items to review" list.
      const errHtml  = errors.length > 0
        ? `<div class="vp-section error-section" style="margin-top:8px">
             <div class="vp-section-heading">Blocking errors</div>
             <ul class="issue-list">${errors.map((m) => `<li class="is-error">${escapeHtml(m)}</li>`).join("")}</ul>
           </div>` : "";
      const warnHtml = warnings.length > 0
        ? `<div class="vp-section review-section" style="margin-top:8px">
             <div class="vp-section-heading">Items to review</div>
             <ul class="issue-list">${warnings.map((m) => `<li class="review-item">${escapeHtml(m)}</li>`).join("")}</ul>
           </div>` : "";
      const techHtml = checks.length > 0
        ? `<details class="tech-checks-toggle" style="margin-top:8px">
             <summary>Technical details</summary>
             <ul class="issue-list" style="margin-top:6px">${checks.map((m) => `<li class="is-pass">${escapeHtml(m)}</li>`).join("")}</ul>
           </details>` : "";
      // The run-readiness chip already says "Result maps provided"; repeating
      // it here made three mentions of one fact. The note carries only the
      // consequence.
      const resultOnlyNote = isResultOnly
        ? `<p class="vr-result-only-note">No processing run is needed.</p>`
        : "";
      const noIssueHtml = (!errHtml && !warnHtml)
        ? `<p style="font-size:0.73rem;color:var(--subtle);margin:0">No items to review.</p>` : "";

      // No inline exec section on Validate, execution happens in Run step only
      const execHtml = "";

      // Collapsed row: one short meta line. Everything else moves into Details.
      // Dataset coverage beats a detected map-type label here: "Clinical +
      // Synthetic" says what the submission contains, where "Mixed/Other"
      // only said that more than one map type was found, which is the
      // expected state for a challenge that defines several.
      const datasets = datasetDisplay(r);
      const counts = submissionCounts(r);
      const mapCount = counts.parameterMaps || Number(rNiftiCount) || 0;
      const metaHtml = [
        safeChallenge,
        datasets ? escapeHtml(datasets) : safeMap,
        `${escapeHtml(mapCount)} parameter map${mapCount === 1 ? "" : "s"}`,
        warnings.length ? `${warnings.length} item${warnings.length === 1 ? "" : "s"} to review` : null,
      ].filter(Boolean).join(" · ");
      // The submission-type chip and the run-readiness chip both read
      // "Result maps provided" for a result-only submission, so the card
      // said it twice. Show the type chip only when it adds something.
      const typeChipHtml = subType.label === runTxt ? "" :
        `<span class="validation-meta-with-help">${escapeHtml(subType.label)} ${helpTooltip(subTypeHelp, "Submission type help")}</span>`;
      const detailChips = `
            <div class="validation-detail-chips worklist-meta">
              <span class="validation-meta-with-help">${statusPill(pillText, pillState)}</span>
              ${typeChipHtml}
              <span class="validation-meta-with-help">${statusPill(runTxt, runState)} ${helpTooltip(isResultOnly ? subTypeHelp : "Runnable submissions include executable code. Result-only submissions skip execution and go directly to QC and configured analysis.", "Run readiness help")}</span>
              <span class="br-badge badge-exec-none val-card-exec-badge" style="display:none;font-size:0.65rem"></span>
            </div>`;
      const detailsHtml = `
          <div class="validation-detail-inner">
            ${detailChips}
            ${resultOnlyNote}${niftiLine}${noIssueHtml}${errHtml}${warnHtml}${techHtml}${execHtml}
            ${_structureControlHtml(r.submission_id)}
            <details class="validation-technical-detail">
              <summary>Technical reference</summary>
              <p>Submission ID: ${safeSubId}</p>
            </details>
          </div>`;

      const wrap = _worklistRowEl({
        extraClass: "br-row-wrap validation-card",
        dataset: {
          valStatus, runnable: String(runnable), execStatus: execInitStatus,
          resultOnly: String(isResultOnly), subId: r.submission_id,
          name: (r.submission_id + " " + (r.source_folder || "") + " " + safeName).toLowerCase(),
          map: String(r.map_type || "").toLowerCase(), rowIndex: String(idx),
          errCount: String(errors.length), warnCount: String(warnings.length),
        },
        iconHtml: submissionFileIconHtml(),
        mainClass: "validation-card-content",
        headClass: "validation-card-heading",
        title: safeName, titleClass: "validation-card-title", titleAttrs: `title="${safeName}" data-display-name-for="${safeSubId}"`,
        lead: "",
        metaHtml, metaClass: "validation-card-meta",
        detailsHtml, detailsClass: "vr-row-detail",
        actionsClass: "validation-card-actions",
      });

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

  // Show/hide "no runnable" message, only show when there are truly no valid submissions
  const noRunnableMsg = el("validate-no-runnable-msg");
  const hasAnyPassed  = runnableCount > 0 || resultOnlyCount > 0;
  if (noRunnableMsg)
    noRunnableMsg.style.display = !hasAnyPassed && results.length > 0 ? "" : "none";

  // Continue button: enabled for both runnable and result-only; label reflects destination
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
  const { filter, search, map, sort, showAll } = _reviewFilter;
  const list = el("batch-submissions-list");
  if (!list) return;

  const rows = [...list.querySelectorAll(".br-row-wrap")];

  const ORDER = { failed: 0, warning: 1, passed: 2 };
  rows.sort((a, b) => {
    if (sort === "newest")   return (Number(b.dataset.rowIndex) || 0) - (Number(a.dataset.rowIndex) || 0);
    if (sort === "oldest")   return (Number(a.dataset.rowIndex) || 0) - (Number(b.dataset.rowIndex) || 0);
    if (sort === "name")     return (a.dataset.name || "").localeCompare(b.dataset.name || "");
    return (ORDER[a.dataset.valStatus] ?? 3) - (ORDER[b.dataset.valStatus] ?? 3);
  });
  rows.forEach((r) => list.appendChild(r));

  // Apply filter + search, collect matching rows
  const q = search.toLowerCase();
  const matchingRows = [];
  rows.forEach((row) => {
    const vs       = row.dataset.valStatus;
    const runnable = row.dataset.runnable === "true";
    const es       = row.dataset.execStatus;
    const name     = (row.dataset.name || "").toLowerCase();
    const rowMap   = (row.dataset.map || "").toLowerCase();

    let show = true;
    switch (filter) {
      case "ready":        show = runnable && es === "not-run"; break;
      case "passed":       show = vs === "passed"; break;
      case "warnings":     show = vs === "warning"; break;
      case "errors":       show = vs === "failed"; break;
      case "result-only":  show = row.dataset.resultOnly === "true"; break;
      case "skipped":      show = row.dataset.resultOnly === "true"; break;
      case "needs-review": show = vs === "warning" || vs === "failed"; break;
      case "failed":       show = vs === "failed"; break;
      // legacy values kept for safety
      case "runnable":     show = runnable && es === "not-run"; break;
      case "cannot-run":   show = !runnable; break;
      case "executed":     show = es === "passed" || es === "failed"; break;
      case "exec-failed":  show = es === "failed"; break;
      default:             show = true;
    }
    if (show && q) show = name.includes(q);
    if (show && map !== "all") show = rowMap === String(map).toLowerCase();

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

// ── Row expand/collapse helpers ───────────────────────────────────────────────

function _toggleRowDetail(wrap, forceOpen) {
  const detail = wrap.querySelector(".vr-row-detail");
  const toggleBtn = wrap.querySelector(".br-toggle-btn");
  if (!detail) return;
  const open = forceOpen !== undefined ? forceOpen : detail.style.display === "none";
  detail.style.display = open ? "" : "none";
  if (toggleBtn) {
    toggleBtn.textContent = open ? "Hide details" : "Details";
    toggleBtn.setAttribute("aria-expanded", String(open));
  }
  // Keep the secondary details button label in sync when present.
  const detailsBtn = wrap.querySelector(".vr-details-btn");
  if (detailsBtn) {
    detailsBtn.textContent = open ? "Close" : "Details";
    detailsBtn.setAttribute("aria-expanded", String(open));
  }
}

// Row expand: legacy toggle button (.br-toggle-btn) click
document.addEventListener("click", (e) => {
  const btn = e.target.closest(".br-toggle-btn");
  if (!btn) return;
  const wrap = btn.closest(".br-row-wrap");
  if (!wrap) return;
  e.stopPropagation();
  _toggleRowDetail(wrap);
});

// Row expand: Details button (.vr-details-btn) click
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
  // Show exec section (hidden by default, only revealed when Run is clicked)
  const execSection = wrap.querySelector(".batch-exec-section");
  if (execSection) execSection.classList.add("exec-visible");
  const execBtn = wrap.querySelector(".batch-exec-btn");
  if (execBtn && !execBtn.disabled) execBtn.click();
});

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
  batchResultsNewBtn.addEventListener("click", _resetToUploadAndClearPersistence);
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
  newBtn.addEventListener("click", _resetToUploadAndClearPersistence);
}

// ── Step 4: Run ───────────────────────────────────────────────────────────────

const _runFilter = { view: "all", search: "", map: "all", sort: "newest", showAll: false };

function _renderRunFilterBar() {
  const active = !!((_runFilter.search || "").trim()
    || _runFilter.view !== "all"
    || _runFilter.map !== "all");
  return `<div class="filter-bar compact-filter-bar run-filter-bar">
    ${_renderSearchBox("run-search", _runFilter.search, "Search submissions...")}
    ${_renderFilterDropdown("run-status", "Status", _runFilter.view, [
      { value: "all", label: "All" },
      { value: "ready", label: "Ready" },
      { value: "passed", label: "Passed" },
      { value: "failed", label: "Failed" },
      { value: "skipped", label: "Skipped" },
    ])}
    ${_renderFilterDropdown("run-map", "Map", _runFilter.map, MAP_FILTER_OPTIONS)}
    ${_renderClearFilterButton("run-clear-filters", active)}
  </div>`;
}

function _wireRunFilterBar() {
  const search = el("run-search");
  if (search) {
    search.oninput = () => {
      const cursor = search.selectionStart ?? search.value.length;
      _runFilter.search = search.value;
      _runFilter.showAll = false;
      _refreshRunFilterBar();
      _applyRunFilters();
      _restoreSearchFocus("run-search", cursor);
    };
  }
  const clear = el("run-clear-filters");
  if (clear) {
    clear.onclick = () => {
      _runFilter.view = "all";
      _runFilter.search = "";
      _runFilter.map = "all";
      _runFilter.sort = "newest";
      _runFilter.showAll = false;
      _refreshRunFilterBar();
      _applyRunFilters();
    };
  }
}

function _refreshRunFilterBar() {
  const toolbar = el("run-toolbar");
  if (!toolbar || toolbar.style.display === "none") return;
  toolbar.innerHTML = _renderRunFilterBar();
  _wireRunFilterBar();
}

function _runListSummary(results) {
  const rows = results || [];
  const ready = rows.filter((r) =>
    (r.run_readiness === "runnable") || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile))).length;
  const skipped = rows.filter((r) =>
    (r.run_readiness === "result_only") || (r.passed && !r.has_run_instructions && ((r.nifti_count || 0) > 0 || r.has_result_maps))).length;
  const cannotRun = rows.length - ready - skipped;
  const completed = Object.values(_execSummaries).filter((s) => s.status === "passed").length;
  const failed = Object.values(_execSummaries).filter((s) => s.status === "failed").length;
  return [
    _summaryChip("total", rows.length),
    _summaryChip("to process", ready, ""),
    _summaryChip("maps ready", skipped, skipped ? "muted" : ""),
    _summaryChip("need review", Math.max(0, cannotRun), ""),
    _summaryChip("processed", completed, ""),
    _summaryChip("needs review", failed, failed ? "error" : ""),
    _latestLabel(rows.length ? submissionDisplayName(rows[rows.length - 1], `Submission ${rows.length}`) : ""),
  ].filter(Boolean).join("");
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
    if (txt)   txt.textContent = "Processing complete";
    if (eta)   eta.textContent = "";

    // Count result-only and cannot-run from card list
    const allCards = [...document.querySelectorAll("#run-submissions-list .run-sub-card, #run-submissions-list .er-row-wrap")];
    const skipped  = allCards.filter((c) => c.dataset.execStatus === "result-only" || c.dataset.execStatus === "cannot-run").length;
    const passed   = completed - failed;
    const parts    = [];
    if (passed > 0)  parts.push(`${passed} processed`);
    if (failed > 0)  parts.push(`${failed} need review`);
    if (skipped > 0) parts.push(`${skipped} maps ready`);

    // Show a calm, neutral completion banner (no loud success/warn styling).
    const bannerType = failed > 0 ? "warn" : "success";
    const bannerHtml = `<span class="scb-text">Processing complete, ${parts.join(", ")}</span>`;
    _showCompletionBanner("run-completion-banner", bannerHtml, bannerType);

    // Unlock Score step and refresh local actions
    unlockStep("score");
    _refreshWizardFooter();
    renderScoreStep().catch(() => {});
  } else {
    if (txt) txt.textContent = `Processing… ${completed} of ${total}`;
    if (eta) eta.textContent = `${pct}%`;
  }
}

// Build/refresh the entire run step table from batchState.validationData
async function renderRunStep() {
  const results = batchState.validationData ? (batchState.validationData.results || []) : [];

  // Reset filter
  _runFilter.view    = "all";
  _runFilter.search  = "";
  _runFilter.map     = "all";
  _runFilter.sort    = "newest";
  _runFilter.showAll = false;

  // Count runnable and result-only
  const runnableResults  = results.filter((r) =>
    (r.run_readiness === "runnable") || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile)));
  // result_only: any submission with result maps, including configured submitted-map folders.
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
  _syncCollapsibleSection("run", results.length, _runListSummary(results));

  // ── Skipped notice (all result-only) ──────────────────────────────────────
  const skippedNotice = el("run-skipped-notice");
  if (skippedNotice) skippedNotice.style.display = allResultOnly ? "" : "none";
  const runListSection = el("run-list-section");
  if (runListSection) runListSection.style.display = "";
  const list = el("run-submissions-list");
  if (list) list.style.display = "";
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
      desc.textContent = "No submissions yet. Complete the review step first.";
    } else if (allResultOnly) {
      desc.textContent = "Maps ready for review.";
    } else if (runnableCount === 0 && cannotRunCount > 0) {
      desc.textContent = `${cannotRunCount} submission${cannotRunCount !== 1 ? "s" : ""} need review.`;
      if (cannotReasonEl) {
        cannotReasonEl.textContent = "No maps or code were found in these submissions.";
        cannotReasonEl.style.display = "";
      }
    } else {
      const parts = [];
      if (runnableCount > 0) parts.push(`${runnableCount} to process`);
      if (resultOnlyCount > 0) parts.push(`${resultOnlyCount} maps ready`);
      if (cannotRunCount > 0)  parts.push(`${cannotRunCount} need review`);
      desc.textContent = parts.join(" · ");
      if (cannotReasonEl) cannotReasonEl.style.display = "none";
    }
  }

  if (allResultOnly) {
    ["batch-docker-banner", "run-toolbar", "run-empty-state", "run-show-all-wrap", "batch-exec-status", "run-progress-panel", "run-completion-banner"].forEach((id) => {
      const node = el(id);
      if (node) node.style.display = "none";
    });
    unlockStep("score");
    unlockStep("export");
    _refreshWizardFooter();
    saveSessionState();
  }

  // ── Docker availability ────────────────────────────────────────────────────
  const docker       = await checkDockerAvailability();
  const dockerBanner = el("batch-docker-banner");
  if (dockerBanner) {
    const cls  = docker.available ? "ok"  : "err";
    const dot  = `<span class="rsc-docker-dot"></span>`;
    const label = docker.available
      ? `${dot} Processing available`
      : `${dot} Processing not available on this computer`;
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
    _wireRunFilterBar();
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
  list.classList.add("worklist");
  list.style.display = "";
  list.innerHTML = "";

  results.forEach((r, idx) => {
    const runReadiness = r.run_readiness
      || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile) ? "runnable"
          : r.passed && !r.has_run_instructions && ((r.nifti_count || 0) > 0 || r.has_result_maps) ? "result_only"
          : "not_runnable");
    const runnable     = runReadiness === "runnable";
    const isResultOnly = runReadiness === "result_only";
    const safeSubId    = escapeHtml(r.submission_id);
    const chall        = r.challenge_type || getChallengeType() || defaultChallengeType();

    let initExecStatus;
    if (runnable)          initExecStatus = "not-run";
    else if (isResultOnly) initExecStatus = "result-only";
    else                   initExecStatus = "cannot-run";

    // Primary action only (Run when runnable); the shared Details button is
    // appended by the renderer.
    const actionsHtml = runnable
      ? `<button type="button" class="btn btn-secondary btn-sm er-run-btn"
                 data-sub-id="${safeSubId}"
                 data-challenge="${escapeHtml(chall)}">Run processing</button>`
      : "";

	    const displayName = submissionDisplayName(r, r.submission_id || "Submission");
	    const safeName = escapeHtml(displayName);
	    const safeOriginalName = escapeHtml(originalSubmissionName(r, r.submission_id || "Submission"));
	    const fileCount = r.nifti_count ?? r.output_file_count ?? "—";
    const mapLabel = escapeHtml(r.detected_parameter_map_type || r.map_type || "Not detected");
    const runNote = isResultOnly
      ? "Result maps were included in this submission. No code run was needed."
      : !runnable
        ? "No maps or code were found. This submission needs review."
        : "Run processing to prepare this submission's maps.";

    // Researcher-facing details: what maps are included and whether a run is needed.
    const detailsHtml = `
      <div class="run-card-detail-body">
        <div><strong>Original submission name:</strong> <span class="original-submission-name">${safeOriginalName}</span></div>
        <div>Included maps: ${mapLabel}</div>
        <div>Map count: <span class="run-card-outputs er-outputs-cell"><span class="rs-na">${escapeHtml(fileCount)}</span></span></div>
        <div>Code run needed: ${runnable ? "Yes" : "No"}</div>
        <p style="margin:6px 0 0">${runNote}</p>
      </div>`;

    const wrap = _worklistRowEl({
      extraClass: "run-sub-card",
      dataset: {
        subId: r.submission_id, challenge: chall, runnable: String(runnable),
        execStatus: initExecStatus,
        name: [r.submission_id, r.source_folder, r.display_name, displayName].filter(Boolean).join(" ").toLowerCase(),
        map: String(r.map_type || "").toLowerCase(), rowIndex: String(idx),
      },
      iconHtml: submissionFileIconHtml(),
      mainClass: "run-card-info",
      title: safeName, titleClass: "run-card-name", titleAttrs: `title="${safeName}" data-display-name-for="${safeSubId}"`,
      metaHtml: _erRunMetaText(initExecStatus, runnable), metaClass: "run-card-status-row",
      actionsHtml, actionsClass: "run-card-right",
      detailsHtml, detailsClass: "er-row-detail run-card-detail",
    });

    list.appendChild(wrap);
  });

  _applyRunFilters();
  _refreshWizardFooter();
}

// Render the run-status cell content for a given execStatus + runnable
function _erRunStatusHtml(execStatus, runnable) {
  switch (execStatus) {
    case "not-run":     return runnable
                          ? `<span class="status-chip status-chip-neutral rs-badge rs-ready">Ready to review</span>`
                          : `<span class="status-chip status-chip-neutral rs-na">—</span>`;
    case "cannot-run":  return `<span class="status-chip status-chip-neutral rs-badge rs-cannot">Needs review</span>`;
    case "result-only": return `<span class="status-chip status-chip-neutral rs-badge rs-skipped">Maps ready</span>`;
    case "running":     return `<span class="status-chip status-chip-neutral rs-badge rs-running">Processing…</span>`;
    case "passed":      return `<span class="status-chip status-chip-neutral rs-badge rs-pass">Processing complete</span>`;
    case "failed":      return `<span class="status-chip status-chip-danger rs-badge rs-fail">Needs review</span>`;
    case "timed-out":   return `<span class="status-chip status-chip-neutral rs-badge rs-timeout">Needs review</span>`;
    default:            return `<span class="status-chip status-chip-neutral rs-na">—</span>`;
  }
}

function _erRunMetaText(execStatus, runnable) {
  switch (execStatus) {
    case "not-run":     return runnable ? "Ready to review" : "Needs review";
    case "cannot-run":  return "Needs review";
    case "result-only": return "Maps ready for review";
    case "running":     return "Processing…";
    case "passed":      return "Processing complete";
    case "failed":      return "Needs review";
    case "timed-out":   return "Needs review";
    default:            return "Maps ready for review";
  }
}

function _applyRunFilters() {
  const { view, search, map, sort, showAll } = _runFilter;
  const list = el("run-submissions-list");
  if (!list) return;
  // Works for both old .er-row-wrap (legacy) and new .run-sub-card elements
  const rows = [...list.querySelectorAll(".er-row-wrap, .run-sub-card")];
  rows.sort((a, b) => {
    if (sort === "oldest") return (Number(a.dataset.rowIndex) || 0) - (Number(b.dataset.rowIndex) || 0);
    if (sort === "name") return (a.dataset.name || "").localeCompare(b.dataset.name || "");
    if (sort === "status") return String(a.dataset.execStatus || "").localeCompare(String(b.dataset.execStatus || ""));
    return (Number(b.dataset.rowIndex) || 0) - (Number(a.dataset.rowIndex) || 0);
  });
  rows.forEach((row) => list.appendChild(row));

  const matchingRows = [];
  const q = (search || "").trim().toLowerCase();
  rows.forEach((row) => {
    const es       = row.dataset.execStatus;
    const runnable = row.dataset.runnable === "true";
    const rowMap   = (row.dataset.map || "").toLowerCase();
    const name     = (row.dataset.name || "").toLowerCase();
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
    if (show && q) show = name.includes(q);
    if (show && map !== "all") show = rowMap === String(map).toLowerCase();
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
  } else if (execData.ready_for_analysis === true || (execData.passed && execData.output_validation?.passed)) {
    newExecStatus = "passed";
  } else {
    newExecStatus = "failed";
  }
  wrap.dataset.execStatus = newExecStatus;

  // Persist execution summary (no logs, IDs and counts only)
  if (isError) {
    _execSummaries[subId] = { status: "failed", passed: false, outputFileCount: 0 };
  } else if (execData) {
    _execSummaries[subId] = {
      status:          newExecStatus,
      passed:          !!execData.passed,
      processPassed:   !!execData.passed,
      outputComplete:  !!execData.output_validation?.passed,
      readyForAnalysis: execData.ready_for_analysis === true,
      exitCode:        execData.exit_code ?? null,
      outputFileCount: execData.output_file_count ?? (Array.isArray(execData.output_files) ? execData.output_files.length : 0),
      executedAt:      execData.executed_at || execData.finished_at || null,
      timedOut:        !!execData.timed_out,
      buildFailed:     !!execData.build_failed,
    };
  }
  saveSessionState();

  const runnable = wrap.dataset.runnable === "true";

  // Update compact run meta text: works for both card and legacy table cells.
  const statusCell = wrap.querySelector(".er-run-status-cell, .run-card-status-row");
  if (statusCell) {
    statusCell.textContent = _erRunMetaText(newExecStatus, runnable);
  }

  // Update outputs (card: .run-card-outputs, table: .er-outputs-cell)
  const outputsCell = wrap.querySelector(".er-outputs-cell, .run-card-outputs");
  if (outputsCell && !isError) {
    const fc = execData.output_file_count ?? (Array.isArray(execData.output_files) ? execData.output_files.length : 0);
    outputsCell.innerHTML = fc > 0
      ? `<span class="vr-run-ok">${fc} file${fc !== 1 ? "s" : ""}</span>`
      : `<span class="vr-issue-warn">0 files</span>`;
  }

  // Update output-check cell (table only, card skips this column)
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
    drawer.hidden = false;
    // Update the Details button label since the drawer is now open
    const detailBtn = wrap.querySelector(".details-toggle, .er-detail-btn");
    if (detailBtn) {
      detailBtn.textContent = "Hide details";
      detailBtn.setAttribute("aria-expanded", "true");
    }
    wrap.classList.add("is-expanded");
  }

  // Refresh run filter visibility
  _syncCollapsibleSection("run", (batchState.validationData?.results || []).length, _runListSummary(batchState.validationData?.results || []));
  _applyRunFilters();
  _refreshWizardFooter();
  _syncCompactProgress();
}

async function _renderRunPanel() {
  // Called when user clicks Run nav item, just refresh the step
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
    s1Icon = "si-fail"; s1Char = "FAIL"; s1StatusCls = "sl-fail"; s1StatusTxt = "Build failed";
    s1Body = `<p class="exec-step-note">Run instructions could not be built. Check technical logs below.</p>`;
  } else if (containerFailed) {
    s1Icon = "si-fail"; s1Char = "FAIL"; s1StatusCls = "sl-fail"; s1StatusTxt = "Could not start";
    s1Body = `<p class="exec-step-note">Container failed to start (exit 125), this is typically a host configuration issue, not a problem with the submission itself.</p>`;
  } else {
    s1Icon = "si-pass"; s1Char = "OK"; s1StatusCls = "sl-pass"; s1StatusTxt = "Ready";
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
    s2Icon = "si-skip"; s2Char = "SKIP"; s2StatusCls = "sl-skip"; s2StatusTxt = "Skipped";
  } else if (timedOut) {
    s2Icon = "si-warn"; s2Char = "TIME"; s2StatusCls = "sl-warn"; s2StatusTxt = "Timed out";
    s2Body = `<p class="exec-step-note">Submission exceeded the time limit and was stopped.</p>`;
  } else if (!passed) {
    s2Icon = "si-fail"; s2Char = "FAIL"; s2StatusCls = "sl-fail"; s2StatusTxt = `Exit ${exitCode ?? "?"}`;
  } else {
    s2Icon = "si-pass"; s2Char = "OK"; s2StatusCls = "sl-pass"; s2StatusTxt = "Exit 0";
  }
  const step2 = _step(s2Icon, s2Char, "Run package", s2StatusCls, s2StatusTxt, s2Body);

  // Step 3: Collect generated outputs
  let s3Icon, s3Char, s3StatusCls, s3StatusTxt, s3Body = "";
  if (earlyFail) {
    s3Icon = "si-skip"; s3Char = "SKIP"; s3StatusCls = "sl-skip"; s3StatusTxt = "Skipped";
  } else if (fileCount === 0) {
    s3Icon = "si-warn"; s3Char = "!"; s3StatusCls = "sl-warn"; s3StatusTxt = "No files";
    s3Body = `<p class="exec-step-note">No files were written to <code>/output</code>.</p>`;
  } else {
    s3Icon = "si-pass"; s3Char = "OK"; s3StatusCls = "sl-pass"; s3StatusTxt = `${fileCount} file${fileCount !== 1 ? "s" : ""}`;
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
    s4Icon = "si-skip"; s4Char = "SKIP"; s4StatusCls = "sl-skip"; s4StatusTxt = "Skipped";
  } else {
    const ovErrs  = (ov.errors   || []).map((e) => e.message || String(e));
    const ovWarns = (ov.warnings || []).map((w) => w.message || String(w));
    if (ov.passed) {
      s4Icon = "si-pass"; s4Char = "OK"; s4StatusCls = "sl-pass"; s4StatusTxt = "Valid";
      if (ov.nifti_count != null) s4Body = `<p class="exec-step-note">${ov.nifti_count} NIfTI file${ov.nifti_count !== 1 ? "s" : ""} detected in output.</p>`;
    } else {
      s4Icon = "si-fail"; s4Char = "FAIL"; s4StatusCls = "sl-fail"; s4StatusTxt = `${ovErrs.length} error${ovErrs.length !== 1 ? "s" : ""}`;
      const errHtml  = ovErrs.map((m)  => `<li class="is-error">Error: ${escapeHtml(m)}</li>`).join("");
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
      sc.textContent = _erRunMetaText("running", true);
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
      _tickRunProgress(data.ready_for_analysis === true && !data.timed_out, fc);
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
        badge.textContent = "Ran";
      } else {
        badge.className = "br-badge badge-exec-fail val-card-exec-badge";
        badge.textContent = "Run failed";
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
    await runBatchExec(btn, section.dataset.subId, section.dataset.challenge || defaultChallengeType());
  });
})();

// Delegation: Run buttons in the Run step table (.er-run-btn)
(function initRunStepBtnDelegation() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".er-run-btn");
    if (!btn) return;
    e.stopPropagation();
    const subId     = btn.dataset.subId;
    const challenge = btn.dataset.challenge || defaultChallengeType();
    if (!subId) return;
    await runBatchExec(btn, subId, challenge);
  });
})();

// Delegation: Details button in run step (.er-detail-btn), works for table rows + new cards
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
        await runBatchExec(null, row.dataset.subId, row.dataset.challenge || defaultChallengeType());
        done++;
      }
      if (statusEl) statusEl.textContent = `Done, ran ${done} submission(s).`;
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
      if (cannotNote) { cannotNote.style.display = ""; cannotNote.textContent = "No run instructions found, this submission can be validated as result-only but cannot be run automatically."; }
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
    const challengeType = window._currentChallengeType || getChallengeType() || defaultChallengeType();
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
        if (rps) { rps.className = "run-panel-status rps-fail"; rps.textContent = "Failed"; }
        return;
      }

      const rps = el("run-panel-status");
      if (rps) {
        if (data.timed_out)   { rps.className = "run-panel-status rps-warn"; rps.textContent = "Timed out"; }
        else if (data.passed) { rps.className = "run-panel-status rps-pass"; rps.textContent = "Passed"; }
        else                  { rps.className = "run-panel-status rps-fail"; rps.textContent = "Failed"; }
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
    case "scored":         return "Analysis complete";
    case "failed":         return "Failed";
    case "not_configured": return "Needs setup";
    case "not_ready":      return "Incomplete";
    case "ready":          return "Ready";
    case "reference_not_available": return "Reference unavailable";
    case "partial_reference_scoring": return "Partial reference";
    case "scoring_error": return "Scoring error";
    case "reference_invalid": return "Reference invalid";
    case "no_finite_overlap": return "No finite overlap";
    default:               return status ? status.replace(/_/g, " ") : "Unknown";
  }
}

function _leaderboardReferenceStatusLabel(status) {
  switch (status) {
    case "scored": return "Reference comparison available";
    case "reference_not_available": return "Reference unavailable";
    case "partial_reference_scoring": return "Partial reference";
    case "not_configured":
    case "not_ready":
    case "":
    case null:
    case undefined:
      return "Unavailable";
    default: return _leaderboardStatusLabel(status);
  }
}

function _leaderboardStatusTone(status) {
  const clean = String(status || "unknown").toLowerCase();
  if (["scored", "ready", "passed", "complete"].includes(clean)) return "success";
  if (["partial_reference_scoring", "not_configured"].includes(clean)) return "warning";
  if (["failed", "scoring_error", "reference_invalid", "no_finite_overlap"].includes(clean)) return "danger";
  if (["running", "processing"].includes(clean)) return "info";
  return "neutral";
}

function _leaderboardStatusClass(status) {
  const clean = String(status || "unknown").replace(/[^a-z0-9_-]/gi, "-").toLowerCase();
  return `status-chip status-chip-${_leaderboardStatusTone(status)} leaderboard-status-badge leaderboard-status-${clean}`;
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
    getSubmissionDisplayName(entry, entry.submission_id || "Submission"),
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
  const active = !!((f.search || "").trim()
    || f.status !== "all"
    || f.map !== "all");
  return `
    ${_renderSearchBox("leaderboard-search", f.search, "Search submissions...")}
    ${_renderFilterDropdown("leaderboard-status", "Status", f.status, [
      { value: "all", label: "All" },
      { value: "ready", label: "Ready" },
      { value: "passed", label: "Passed" },
      { value: "warnings", label: "Warnings" },
      { value: "errors", label: "Errors" },
      { value: "scored", label: "Analysis complete" },
      { value: "failed", label: "Failed" },
      { value: "skipped", label: "Skipped" },
    ])}
    ${_renderFilterDropdown("leaderboard-map", "Map", f.map, MAP_FILTER_OPTIONS)}
    ${_renderClearFilterButton("leaderboard-clear-filters", active)}`;
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
    if (f.status !== "all") {
      const matchesStatus =
        (f.status === "scored" && status === "scored") ||
        (f.status === "passed" && status === "scored") ||
        (f.status === "ready" && (status === "ready" || status === "not_ready")) ||
        (f.status === "failed" && status === "failed") ||
        (f.status === "errors" && (status === "failed" || ["scoring_error", "reference_invalid", "no_finite_overlap"].includes(refStatus))) ||
        (f.status === "warnings" && ["partial_reference_scoring", "reference_not_available", "not_configured"].includes(refStatus || status)) ||
        (f.status === "skipped" && (status === "skipped" || refStatus === "reference_not_available"));
      if (!matchesStatus) return false;
    }
    if (f.map !== "all" && !mapTypes.includes(String(f.map).toLowerCase())) return false;
    return true;
  });
  rows.sort((a, b) => {
    if (f.sort === "oldest") return new Date(a.scored_at || 0) - new Date(b.scored_at || 0);
    if (f.sort === "name") return getSubmissionDisplayName(a, a.submission_id || "Submission")
      .localeCompare(getSubmissionDisplayName(b, b.submission_id || "Submission"));
    if (f.sort === "status") return String(a.status || "").localeCompare(String(b.status || ""));
    return new Date(b.scored_at || 0) - new Date(a.scored_at || 0);
  });
  return rows;
}

function _leaderboardStatusBadge(status) {
  return `<span class="${_leaderboardStatusClass(status)}">${escapeHtml(_leaderboardStatusLabel(status))}</span>`;
}

function _leaderboardSummary(entries) {
  const rows = entries || [];
  const scored = rows.filter((e) => e.status === "scored").length;
  const failed = rows.filter((e) => e.status === "failed").length;
  const partial = rows.filter((e) => _leaderboardReferenceStatus(e) === "partial_reference_scoring").length;
  const unavailable = rows.filter((e) => _leaderboardReferenceStatus(e) === "reference_not_available").length;
  const sorted = [...rows].sort((a, b) => new Date(b.scored_at || 0) - new Date(a.scored_at || 0));
  return [
    _summaryChip("total", rows.length),
    _summaryChip("analysis complete", scored, "success"),
    _summaryChip("failed", failed, failed ? "error" : ""),
    _summaryChip("partial", partial, partial ? "warning" : ""),
    _summaryChip("reference unavailable", unavailable, unavailable ? "muted" : ""),
    _latestLabel(sorted[0] ? getSubmissionDisplayName(sorted[0], sorted[0].submission_id || "Submission") : ""),
  ].filter(Boolean).join("");
}

function _leaderboardSummaryLine(entries) {
  const rows = entries || [];
  const scored = rows.filter((e) => e.status === "scored").length;
  const unavailable = rows.filter((e) => _leaderboardReferenceStatus(e) === "reference_not_available").length;
  const parts = [
    `${rows.length} submission${rows.length === 1 ? "" : "s"}`,
    scored ? `${scored} analysis complete` : "Analysis pending",
    unavailable ? "Reference unavailable" : "Reference status ready",
  ];
  return parts.join(" · ");
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
  const displayName = getSubmissionDisplayName(entry, sid);
  const safeDisplayName = escapeHtml(displayName);
  const ts = _formatLeaderboardTimestamp(entry.scored_at);
  const status = entry.status || "unknown";
  const refStatus = _leaderboardReferenceStatus(entry);
  const challenge = _leaderboardChallenge(entry);
  const mapTypes = _leaderboardMapTypes(entry);
  const metrics = _leaderboardMetrics(entry);
  const artifactCount = Number(entry.artifact_count || 0);
  const exportReady = status === "scored" || artifactCount > 0;
  const mapLabel = mapTypes.length ? mapTypes.join(", ") : "Map type not provided";
  const mapCount = mapTypes.length || artifactCount || 0;
  const refScored = refStatus === "available" || refStatus === "compared" || refStatus === "partial_reference_scoring";
  // Compact single meta line, same shape as Review: challenge · maps · state · N maps.
  const metaHtml = `${escapeHtml(challenge)} · ${escapeHtml(mapLabel)} · ${refScored ? "Reference comparison available" : "QC only"} · ${mapCount} map${mapCount === 1 ? "" : "s"}`;
  // Metrics + reference info move into the collapsed details (not the always-visible row).
  const refNote = refScored
    ? "Reference maps were available; reference metrics are included."
    : "Reference maps were not available, so this is QC only.";
  const hasRefMetrics = [metrics.rmse, metrics.mae, metrics.bias].some((v) => typeof v === "number" && isFinite(v));
  const detailsHtml = `
      <div class="leaderboard-detail-actions">
        <button type="button" class="btn btn-secondary btn-sm" data-leaderboard-view="${safeSid}">Preview Maps</button>
      </div>
      <p style="margin:0 0 6px">${refNote}</p>
      <div>Map types: ${escapeHtml(mapLabel)}</div>
      <div>Map count: ${mapCount}</div>
      ${hasRefMetrics ? `<div class="leaderboard-metric-row">
        ${_leaderboardMetricChip("RMSE", metrics.rmse)}
        ${_leaderboardMetricChip("MAE", metrics.mae)}
        ${_leaderboardMetricChip("Bias", metrics.bias)}
        ${_leaderboardMetricChip("CoV", metrics.cov)}
      </div>` : ""}`;
  return renderWorklistRow({
    tag: "article",
    extraClass: "leaderboard-row",
    attrs: "data-leaderboard-row",
    dataset: { subId: sid, status, referenceStatus: refStatus || "" },
    iconHtml: submissionFileIconHtml(),
    mainClass: "leaderboard-row-main",
    headClass: "leaderboard-row-title",
    title: safeDisplayName, titleClass: "leaderboard-submission-name", titleAttrs: `title="${safeDisplayName}" data-display-name-for="${safeSid}"`,
    metaHtml, metaClass: "leaderboard-meta-line",
    detailsHtml, detailsClass: "leaderboard-detail",
    actionsClass: "leaderboard-actions",
  });
}

function _wireLeaderboardFilterControls() {
  const search = el("leaderboard-search");
  if (search) {
    search.oninput = () => {
      const cursor = search.selectionStart ?? search.value.length;
      _leaderboardFilter.search = search.value;
      _leaderboardFilter.showAll = false;
      _renderLeaderboardEntries();
      _restoreSearchFocus("leaderboard-search", cursor);
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
      _leaderboardFilter.showAll = false;
      _renderLeaderboardEntries();
    };
  }
}

function _renderLeaderboardEntries(options = {}) {
  const card = el("leaderboard-card");
  const list = el("leaderboard-list");
  const filterBar = el("leaderboard-filter-bar");
  const countEl = el("leaderboard-count");
  const sub = el("leaderboard-sub");
  if (!card || !list || !filterBar) return;
  card.style.display = "";
  _syncCollapsibleSection("leaderboard", (_leaderboardFilter.entries || []).length, _leaderboardSummary(_leaderboardFilter.entries || []));
  if (!options.skipFilterBar) {
    filterBar.innerHTML = _renderLeaderboardFilterBar();
    _wireLeaderboardFilterControls();
  }

  if (_leaderboardFilter.loading) {
    list.innerHTML = `<div class="leaderboard-loading">
      <div class="leaderboard-skeleton"></div>
      <div class="leaderboard-skeleton short"></div>
    </div>`;
    if (countEl) countEl.textContent = "Loading processed submissions…";
    if (sub) sub.textContent = "Refreshing analysis results";
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
    if (sub) sub.textContent = _leaderboardSummaryLine(_leaderboardFilter.entries || []);
    return;
  }

  const entries = _filteredLeaderboardEntries();
  if (countEl) {
    const totalEntries = (_leaderboardFilter.entries || []).length;
    countEl.textContent = entries.length === totalEntries
      ? `${entries.length} submission${entries.length !== 1 ? "s" : ""}`
      : `${entries.length} of ${totalEntries} submissions`;
  }
  if (sub) sub.textContent = _leaderboardSummaryLine(entries);
  if (!entries.length) {
    const hasAny = (_leaderboardFilter.entries || []).length > 0;
    list.innerHTML = `<div class="list-empty-state">
      <p>${hasAny ? "No submissions match these filters." : "No processed submissions yet."}</p>
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
      _leaderboardFilter.showAll = false;
      _renderLeaderboardEntries();
    };
    return;
  }

  const LIMIT = 10;
  const visibleEntries = !_leaderboardFilter.showAll && entries.length > LIMIT ? entries.slice(0, LIMIT) : entries;
  const showMore = entries.length > LIMIT
    ? `<div class="show-more-row leaderboard-show-more-row">
        <button type="button" id="leaderboard-show-all-btn" class="vr-show-all-btn">${_leaderboardFilter.showAll ? "Show less" : `Show all ${entries.length} submissions`}</button>
      </div>`
    : "";
  list.innerHTML = visibleEntries.map(_renderLeaderboardEntry).join("") + showMore;
  const showAll = el("leaderboard-show-all-btn");
  if (showAll) {
    showAll.onclick = () => {
      _leaderboardFilter.showAll = !_leaderboardFilter.showAll;
      _renderLeaderboardEntries({ skipFilterBar: true });
    };
  }
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
      _leaderboardFilter.error = "Could not load analysis results.";
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
    _leaderboardFilter.error = "Could not load analysis results.";
    _renderLeaderboardEntries();
  }
}

(function _wireLeaderboard() {
  const btn = el("leaderboard-refresh-btn");
  if (btn) btn.addEventListener("click", loadLeaderboard);
})();

document.addEventListener("click", (e) => {
  // Score row Details use the shared .details-toggle handler.
  const viewBtn = e.target.closest("[data-leaderboard-view]");
  if (viewBtn) {
    const sid = viewBtn.getAttribute("data-leaderboard-view");
    if (sid) _openSubmissionPreviewFromDetails(sid);
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
let _scoreOfficialMode = false;

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
    const noun = _scoreOfficialMode ? "Official scoring" : "Analysis";
    if (txt)   txt.textContent = `${noun} complete`;
    if (eta)   eta.textContent = `${scored} complete · ${failed + notConf} need attention`;
    const titleEl = el("score-status-title");
    const subEl = el("score-status-sub");
    const previewEl = el("score-metric-preview");
    if (titleEl) titleEl.textContent = failed + notConf > 0 ? `${noun} needs attention` : `${noun} complete`;
    if (subEl) subEl.textContent = scored > 0 ? `${scored} submission${scored !== 1 ? "s" : ""} processed.` : "No submissions were processed.";
    if (previewEl) {
      const preview = _scoreMetricPreviewHtml();
      previewEl.innerHTML = preview;
      previewEl.style.display = preview ? "" : "none";
    }
    _enableScoringExport();
    loadLeaderboard();
    _syncCompactProgress();
  } else {
    if (txt) txt.textContent = `${_scoreOfficialMode ? "Scoring" : "Analyzing"} submissions… ${completed} of ${total}`;
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
  // QC / demo metrics from custom packages.
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

function _metricUnitText(unit) {
  return unit ? String(unit) : "";
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

function _setScoreStepCopy({ official = false, providerName = "" } = {}) {
  const labelEl = el("score-step-label");
  const titleEl = el("score-step-title");
  const descEl = el("score-step-desc");
  if (labelEl) labelEl.textContent = "Step 5 of 6: QC & Preview";
  if (titleEl) titleEl.textContent = "QC & Preview";
  if (descEl) descEl.textContent = official
    ? `QC and previews are available for readable maps. Results from ${providerName || "the configured provider"} appear when its required data are available.`
    : "QC and previews are available for readable maps. Other analyses appear when compatible data are available.";
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
    submissionId: sid,
    displayName: row ? row.querySelector(".sc-col-sub")?.textContent?.trim() : getSubmissionDisplayName({ submission_id: sid }, sid),
    metrics: result.metrics || data?.metrics || {},
    metricsDetail: result.metrics_detail || data?.metrics_detail || {},
    niftiAnalysis: analysis,
    message: data?.message || result.message || "",
    official: result.official === true,
    referenceBasedScoringAvailable: result.reference_based_scoring_available === true
      || data?.reference_based_scoring_available === true,
  };
}

/* Canonical ROI payload for the Results Summary.

   Path, verified against the real API response rather than assumed:
     /api/scoring-status
       -> _scorePayload(data).nifti_analysis
       -> _scoreCache[sid].niftiAnalysis
       -> .reference_scoring.roi_descriptive_statistics

   Returns [rows, status] for the existing renderer. Both are empty when no
   submission carries ROI data, which is what clears stale rows when a new
   submission or a challenge without configured ROI analysis is loaded. */
function _roiDescriptivePayload() {
  const rows = [];
  let status = null;
  for (const analysis of _niftiAnalysisEntries()) {
    const ref = analysis.reference_scoring;
    if (!ref || typeof ref !== "object") continue;
    const records = ref.roi_descriptive_statistics;
    if (Array.isArray(records)) rows.push(...records);
    // First explicit status wins; it explains an empty table.
    if (!status && ref.roi_descriptive_status) status = ref.roi_descriptive_status;
  }
  return [rows, status];
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
  const meanTypes = [...new Set(summaries.flatMap((s) => Object.keys(s.means_by_map_type || {})))];
  meanTypes.forEach((type) => {
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

// One small note at the top of the Image Preview panel (not repeated per card).
function _previewNote() {
  return `<p class="nifti-preview-note">Open full NIfTI files in ITK-SNAP, FSLeyes, or 3D Slicer.</p>`;
}

// A preview item is a scored parameter map only when it is exactly 3-D and has
// a recognized configured map type (CBF/ATT/…). 4-D ASL/model data and
// unrecognized files are never shown in the gallery. Prefers the backend flag;
// falls back to shape + map type for older cached manifests.
function _isParameterMapPreview(item) {
  if (typeof item?.is_parameter_map === "boolean") return item.is_parameter_map;
  const shape = Array.isArray(item?.shape) ? item.shape.filter(Boolean) : [];
  const mt = String(item?.detected_map_type || "").trim().toLowerCase();
  return shape.length === 3 && mt !== "" && mt !== "unknown" && mt !== "mixed/other";
}

// Display label for a non-parameter-map submitted file (e.g. the 4-D ASL input).
function _nonParameterFileLabel(item) {
  if (item?.role_label) return item.role_label;
  const shape = Array.isArray(item?.shape) ? item.shape.filter(Boolean) : [];
  return shape.length >= 4 ? "4D ASL data" : "Other submitted file";
}

function _storePreviewItems(manifest) {
  Object.keys(_previewItemsById).forEach((key) => delete _previewItemsById[key]);
  _previewMapOrder = [];
  (manifest?.maps || []).forEach((item) => {
    if (item?.map_id) {
      _previewItemsById[item.map_id] = item;
      // Only parameter maps join the gallery/modal navigation order.
      if (_isParameterMapPreview(item)) _previewMapOrder.push(item.map_id);
    }
  });
  // Keep the current tab selection when still valid, else fall back to first.
  if (!_previewSelectedMapId || !_previewItemsById[_previewSelectedMapId]
      || !_isParameterMapPreview(_previewItemsById[_previewSelectedMapId])) {
    _previewSelectedMapId = _previewMapOrder[0] || null;
  }
}

// Map ids that can actually be shown in the modal gallery.
function _previewGalleryIds() {
  return _previewMapOrder.filter((id) => _previewItemsById[id]?.preview_available);
}

// Compact imaging strip item: small dark-framed thumbnail (~88px), map type
// label, filename, and two small actions (Preview / Download). The existing
// modal keeps the large axial/coronal/sagittal views and full stats.
function _renderPreviewCard(item) {
  const mapId = item.map_id || "";
  const mapLabel = item.detected_map_type || "Unknown map";
  const thumb = item.preview_available && item.thumbnail_url
    ? `<img src="${escapeHtml(item.thumbnail_url)}" alt="Middle-slice preview for ${escapeHtml(item.file_name || "NIfTI map")}">`
    : `<div class="nifti-preview-placeholder">No preview</div>`;
  const previewDisabled = item.preview_available ? "" : " disabled";
  const download = item.download_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.download_url)}" title="Downloads the original submitted/result NIfTI map.">Download NIfTI</a>`
    : `<span class="btn btn-secondary btn-sm is-disabled" aria-disabled="true" title="Download is not available for this file.">Download NIfTI</span>`;
  return renderFileRow({
    tag: "article",
    extraClass: "nifti-preview-card imaging-preview-item",
    dataset: { previewMapId: mapId },
    iconHtml: `<button type="button" class="nifti-preview-thumb imaging-thumb" data-open-preview-map="${escapeHtml(mapId)}"${previewDisabled} aria-label="Preview ${escapeHtml(item.file_name || "NIfTI map")}">${thumb}</button>`,
    mainClass: "imaging-item-info",
    title: escapeHtml(mapLabel), titleClass: "nifti-preview-map imaging-item-label",
    metaHtml: `<span title="${escapeHtml(item.file_name || "")}">${escapeHtml(item.file_name || "NIfTI map")}</span>`,
    metaClass: "nifti-preview-file",
    lead: item.preview_error ? `<p class="nifti-preview-error">${escapeHtml(item.preview_error)}</p>` : "",
    actionsHtml: `<button type="button" class="btn btn-secondary btn-sm preview-open-btn" data-open-preview-map="${escapeHtml(mapId)}"${previewDisabled}>Preview</button>${download}`,
    actionsClass: "nifti-preview-actions",
  });
}

// Short chip label for a map tab: configured map label or a trimmed filename.
function _previewTabLabel(item, idx) {
  if (item?.detected_map_type && item.detected_map_type !== "Unknown") return item.detected_map_type;
  const name = String(item?.file_name || "").replace(/\.nii(\.gz)?$/i, "");
  return name || `Map ${idx + 1}`;
}

// One-at-a-time Map Preview: small tabs/chips to switch maps, a single
// compact preview item visible at any time. No stacked giant cards.
function _renderImagePreviewSection(manifest, options = {}) {
  const loading = options.loading === true;
  const submissionId = options.submissionId || manifest?.submission_id || "";
  const allItems = manifest?.maps || [];
  // Gallery shows only 3-D recognized parameter maps (CBF/ATT/…). 4-D ASL/model
  // data and unrecognized files are kept out of the gallery and listed below.
  const maps = allItems.filter(_isParameterMapPreview);
  const otherFiles = allItems.filter((m) => !_isParameterMapPreview(m));
  const selected = maps.find((m) => m.map_id === _previewSelectedMapId) || maps[0] || null;
  if (selected) _previewSelectedMapId = selected.map_id;
  const tabs = maps.length > 1
    ? `<div class="map-preview-tabs" role="tablist" aria-label="Available maps">
        ${maps.map((m, idx) => `<button type="button" role="tab"
          class="map-preview-tab${m.map_id === _previewSelectedMapId ? " is-active" : ""}"
          aria-selected="${m.map_id === _previewSelectedMapId}"
          data-preview-tab="${escapeHtml(m.map_id || "")}">${escapeHtml(_previewTabLabel(m, idx))}</button>`).join("")}
      </div>`
    : "";
  const body = loading
    ? `<div class="nifti-preview-loading">Generating cached NIfTI previews…</div>`
    : selected
      ? `${tabs}<div class="worklist nifti-preview-list imaging-preview-strip map-preview-single">${_renderPreviewCard(selected)}</div>`
      : `<div class="nifti-preview-empty">No parameter-map previews are available.</div>`;
  // Non-parameter-map files (e.g. the 4-D ASL input) remain available for
  // download in a collapsed section, never treated as scored parameter maps.
  const otherFilesHtml = otherFiles.length ? `
    <details class="submitted-files-details">
      <summary>Submitted files (not scored as parameter maps)</summary>
      <ul class="submitted-files-list">
        ${otherFiles.map((m) => {
          const dl = m.download_url
            ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(m.download_url)}" title="Download the original submitted NIfTI file.">Download NIfTI</a>`
            : "";
          return `<li><span class="submitted-file-role">${escapeHtml(_nonParameterFileLabel(m))}</span>
            <span class="submitted-file-name" title="${escapeHtml(m.file_name || "")}">${escapeHtml(m.file_name || "NIfTI file")}</span>${dl}</li>`;
        }).join("")}
      </ul>
    </details>` : "";
  return `<section id="score-image-preview-section" class="imaging-preview-panel sdc score-image-preview" data-submission-id="${escapeHtml(submissionId)}">
    <div class="sdc-head">
      <h3>Parameter Map Previews</h3>
    </div>
    ${_previewNote()}
    ${body}
    ${otherFilesHtml}
  </section>`;
}

async function _loadAndRenderImagePreviews(submissionId, challengeType) {
  if (!submissionId) return;
  const key = _previewCacheKey(submissionId, challengeType);
  const section = el("score-image-preview-section");
  if (_previewManifestCache[key]) {
    _storePreviewItems(_previewManifestCache[key]);
    const current = el("score-image-preview-section");
    if (current && current.dataset.submissionId === String(submissionId)) {
      current.outerHTML = _renderImagePreviewSection(_previewManifestCache[key], { submissionId });
      return;
    }
  }
  try {
    const url = new URL(`${API}/api/submissions/${encodeURIComponent(submissionId)}/previews`);
    if (challengeType) url.searchParams.set("challenge_type", challengeType);
    const resp = await fetch(url.toString());
    if (!resp.ok) throw new Error(`Preview request failed (${resp.status})`);
    const manifest = await resp.json();
    _previewManifestCache[key] = manifest;
    _storePreviewItems(manifest);
    const current = el("score-image-preview-section");
    if (current && current.dataset.submissionId === String(submissionId)) {
      current.outerHTML = _renderImagePreviewSection(manifest, { submissionId });
    }
  } catch (err) {
    const current = el("score-image-preview-section") || section;
    if (current && current.dataset.submissionId === String(submissionId)) {
      current.outerHTML = `<section id="score-image-preview-section" class="imaging-preview-panel sdc score-image-preview" data-submission-id="${escapeHtml(submissionId)}">
        <div class="sdc-head">
          <h3>Parameter Map Previews</h3>
        </div>
        ${_previewNote()}
        <div class="nifti-preview-empty">Preview unavailable: ${escapeHtml(err.message || String(err))}</div>
      </section>`;
    }
  }
}

function _previewChallengeForSubmission(submissionId) {
  const sid = String(submissionId || "");
  const validation = ((batchState.validationData && batchState.validationData.results) || [])
    .find((r) => r.submission_id === sid);
  const leaderboard = (_leaderboardFilter.entries || [])
    .find((entry) => entry.submission_id === sid);
  const configured = new Set((_appConfig.challengeTypes || []).map((row) => String(row.id || "").toLowerCase()));
  const placeholders = new Set(["unknown", "not provided", "not_provided", "none", "n/a"]);
  const candidates = [
    validation?.challenge_type,
    leaderboard?.challenge_type,
    leaderboard?.challenge,
    leaderboard?.metrics?.challenge_type,
    getChallengeType(),
    _getSessionChallengeType(),
    defaultChallengeType(),
  ].map((value) => String(value || "").trim().toLowerCase());
  return candidates.find((value) => value && !placeholders.has(value) && (!configured.size || configured.has(value))) || "";
}

async function _openSubmissionPreviewFromDetails(submissionId) {
  const sid = String(submissionId || "");
  if (!sid) return;
  const challengeType = _previewChallengeForSubmission(sid);
  const key = _previewCacheKey(sid, challengeType);
  let manifest = _previewManifestCache[key];
  if (!manifest) {
    try {
      const url = new URL(`${API}/api/submissions/${encodeURIComponent(sid)}/previews`);
      if (challengeType) url.searchParams.set("challenge_type", challengeType);
      const resp = await fetch(url.toString());
      if (!resp.ok) throw new Error(`Preview request failed (${resp.status})`);
      manifest = await resp.json();
      _previewManifestCache[key] = manifest;
    } catch (_) {
      return;
    }
  }
  _storePreviewItems(manifest);
  const firstPreview = _previewGalleryIds()[0];
  if (firstPreview) _openNiftiPreview(firstPreview);
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
  if (plane === "mask-overlay") return item?.mask_overlay_url || "";
  if (plane?.startsWith("mask-overlay-")) {
    return (item?.mask_overlays || []).find((overlay) => overlay.plane === plane)?.url || "";
  }
  return item?.[`${plane}_url`] || "";
}

function _previewPlaneLabel(item, plane) {
  if (plane?.startsWith("mask-overlay-")) {
    const label = (item?.mask_overlays || []).find((overlay) => overlay.plane === plane)?.label;
    return label ? `${label} overlay` : "Mask overlay";
  }
  if (plane === "mask-overlay") return "Mask overlay";
  return plane[0].toUpperCase() + plane.slice(1);
}

function _renderPreviewModalContent(item, plane = "axial") {
  const overlayPlanes = (item?.mask_overlays || []).map((overlay) => overlay.plane);
  const legacyOverlayPlanes = overlayPlanes.length ? [] : ["mask-overlay"];
  const availablePlanes = ["axial", "coronal", "sagittal", ...overlayPlanes, ...legacyOverlayPlanes].filter((p) => _previewPlaneUrl(item, p));
  const activePlane = availablePlanes.includes(plane) ? plane : (availablePlanes[0] || "axial");
  _activePreviewPlane = activePlane;
  const imageUrl = _previewPlaneUrl(item, activePlane);
  const tabs = availablePlanes.map((p) =>
    `<button type="button" class="${p === activePlane ? "is-active" : ""}" data-preview-plane="${escapeHtml(p)}">${escapeHtml(_previewPlaneLabel(item, p))}</button>`
  ).join("");
  const image = imageUrl
    ? `<img class="nifti-preview-modal-image" src="${escapeHtml(imageUrl)}" alt="${escapeHtml(activePlane)} preview for ${escapeHtml(item.file_name || "NIfTI map")}">`
    : `<div class="nifti-preview-modal-empty">Preview unavailable</div>`;
  const fullPreview = item.full_preview_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.full_preview_url)}" target="_blank" rel="noopener">Open image in new tab</a>`
    : "";
  const download = item.download_url
    ? `<a class="btn btn-secondary btn-sm" href="${escapeHtml(item.download_url)}" title="Download the original map for ITK-SNAP, FSLeyes, or 3D Slicer.">Download original NIfTI</a>`
    : "";
  // Gallery navigation: browse between maps when more than one is available.
  const galleryIds = _previewGalleryIds();
  const galleryPos = galleryIds.indexOf(item.map_id);
  const galleryNav = galleryIds.length > 1 && galleryPos >= 0
    ? `<div class="nifti-preview-modal-nav">
        <button type="button" class="btn btn-secondary btn-sm" data-preview-nav="prev" aria-label="Previous map">‹ Previous map</button>
        <span class="nifti-preview-counter"><strong>${galleryPos + 1}</strong> of ${galleryIds.length} maps</span>
        <button type="button" class="btn btn-secondary btn-sm" data-preview-nav="next" aria-label="Next map">Next map ›</button>
      </div>`
    : "";
  return `<div class="nifti-preview-modal-header">
      <div>
        <div class="summary-section-kicker">Map Preview</div>
        <h2 id="nifti-preview-title">${escapeHtml(item.file_name || "NIfTI map")}</h2>
        <p>${escapeHtml(item.detected_map_type || "Unknown map")}</p>
      </div>
      ${_previewStatusBadge(item)}
    </div>
    ${galleryNav}
    ${tabs ? `<div class="nifti-preview-view-row"><span class="nifti-preview-section-label">View</span><div class="nifti-preview-tabs">${tabs}</div></div>` : ""}
    <div class="nifti-preview-modal-grid">
      <div class="nifti-preview-modal-image-wrap">${image}</div>
      <div class="nifti-preview-modal-meta">
        <h3>Image information</h3>
        ${_summaryMetric("Map type", item.detected_map_type || "Unknown")}
        ${_summaryMetric("Shape", _previewShapeText(item.shape))}
        ${_summaryMetric("Voxel size", _previewVoxelText(item.voxel_size))}
        ${_summaryMetric("Orientation", item.orientation || "not available")}
        ${(item.mask_overlays || []).length
          ? _summaryMetric("Overlay masks", item.mask_overlays.map((overlay) => overlay.label).join(", "))
          : (item.mask_overlay_label ? _summaryMetric("Overlay mask", item.mask_overlay_label) : "")}
        ${_summaryMetric("Mean", _dashMetric(item.mean))}
        ${_summaryMetric("Std. deviation", _dashMetric(item.std))}
        ${_summaryMetric("Finite voxels", _dashMetric(item.finite_percent, (v) => `${_fmtMetricVal(v)}%`))}
        ${_summaryMetric("Negative voxels", _dashMetric(item.negative_percent, (v) => `${_fmtMetricVal(v)}%`))}
        ${item.preview_error ? `<p class="nifti-preview-error">${escapeHtml(item.preview_error)}</p>` : ""}
      </div>
    </div>
    ${(fullPreview || download) ? `<div class="nifti-preview-modal-footer">${fullPreview}${download}</div>` : ""}`;
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

// Step to the previous/next available map in the modal gallery (wraps around).
function _stepNiftiPreview(delta) {
  const ids = _previewGalleryIds();
  if (ids.length < 2 || !_activePreviewMapId) return;
  const idx = ids.indexOf(_activePreviewMapId);
  if (idx < 0) return;
  const nextId = ids[(idx + delta + ids.length) % ids.length];
  _openNiftiPreview(nextId, _activePreviewPlane);
}

function _referenceStatusBadge(status) {
  const raw = status || "reference_not_available";
  const state = raw === "available" || raw === "compared" ? "complete"
    : raw === "partial_reference_scoring" ? "warning"
    : raw === "scoring_error" ? "error"
    : raw === "reference_not_available" ? "pending"
    : "ready";
  const label = raw === "available" ? "Complete"
    : raw === "partial_reference_scoring" ? "Partial reference comparison"
    : raw === "reference_not_available" ? "Reference unavailable"
    : raw === "scoring_error" ? "Scoring error"
    : raw;
  return statusPill(label, state);
}

function _overallSummaryStatus(mapSummary, valTotal, valFailed, scoredCount) {
  if (valTotal === 0) return { state: "pending", label: "Needs attention", refStatus: "not_started" };
  if (valFailed > 0) return { state: "error", label: "Needs attention", refStatus: mapSummary.referenceStatus };
  if (mapSummary.referenceStatus === "partial_reference_scoring") {
    return { state: "warning", label: "Partial reference comparison", refStatus: mapSummary.referenceStatus };
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

/* How a header check verdict reads in the interface, keyed by the status the
   scorer returns. An unrecognised status falls through to itself rather than
   being shown as a pass. */
const HEADER_CHECK_VERDICTS = {
  matches: "Matches reference",
  dtype_differs: "Data type differs",
  geometry_mismatch: "Geometry differs",
  not_verified: "Not verified",
};

/* One header field for display. A field that differs shows both values,
   because "differs" alone does not say whether a map is flipped or simply at
   a different voxel size. Axis codes join without a separator, as LAS. */
function _headerFieldText(field, joiner = " x ") {
  if (!field || typeof field !== "object") return "Not verified";
  if (field.matches === null || field.matches === undefined) return "Not verified";
  const text = (value) => {
    if (value === null || value === undefined) return "not declared";
    return Array.isArray(value) ? value.join(joiner) : String(value);
  };
  return field.matches
    ? text(field.submitted)
    : `${text(field.submitted)} vs ${text(field.reference)}`;
}

/* The header and orientation check for one map, as a small table.

   Both challenge leads asked for this. A submission can be the right shape,
   score plausibly, and still be flipped, in which case every number computed
   from it is wrong in a way no comparison metric reveals. */
function _renderHeaderCheck(check) {
  if (!check || typeof check !== "object") return "";
  const fields = check.fields && typeof check.fields === "object" ? check.fields : {};
  const status = String(check.status || "not_verified");
  const verdict = HEADER_CHECK_VERDICTS[status] || status;
  const rows = [
    ["Shape", _headerFieldText(fields.shape)],
    ["Voxel size", _headerFieldText(fields.voxel_size)],
    ["Orientation", _headerFieldText(fields.orientation, "")],
    ["Data type", _headerFieldText(fields.dtype)],
  ];
  const tone = status === "geometry_mismatch"
    ? "warning"
    : (status === "matches" ? "complete" : "pending");
  return `<details class="summary-mask-details summary-header-check"${
    status === "geometry_mismatch" ? " open" : ""}>
    <summary>Header and orientation ${statusPill(verdict, tone)}</summary>
    <div class="summary-mask-table-wrap">
      <table class="summary-mask-table">
        <thead><tr><th>Field</th><th>Submitted vs reference</th></tr></thead>
        <tbody>${rows.map(([label, value]) => `<tr>
          <td>${escapeHtml(label)}</td>
          <td>${escapeHtml(value)}</td>
        </tr>`).join("")}</tbody>
      </table>
      ${status === "geometry_mismatch" ? `<p class="sdc-reason">This map differs
        from the reference in shape, voxel size or orientation. Its comparison
        metrics are not reliable until the difference is explained.</p>` : ""}
    </div>
  </details>`;
}

function _renderReferenceReportSection(mapSummary) {
  const wholeRows = mapSummary.referenceRows.filter((row) => row.scope === "whole map" || !row.scope);
  if (!wholeRows.length && !mapSummary.referenceMapStatuses.length) return "";
  const maskRowsFor = (row) => mapSummary.referenceRows.filter((mask) =>
    mask.scope && mask.scope !== "whole map"
    && mask.submitted_file === row.submitted_file
    && (mask.detected_map_type || "") === (row.detected_map_type || "")
  );
  const refAvailable = mapSummary.referenceComparedMapCount > 0;
  const refPartial = mapSummary.referenceStatus === "partial_reference_scoring";
  const refChip = refAvailable
    ? statusPill(refPartial ? "Partial" : "Available", refPartial ? "warning" : "complete")
    : statusPill("Unavailable", "pending");
  const refReason = refAvailable
    ? `${mapSummary.referenceComparedMapCount} of ${mapSummary.referenceMapCount || mapSummary.referenceComparedMapCount} maps compared with reference data.`
    : "No matching reference maps were found for this submission.";
  const statusListHtml = mapSummary.referenceMapStatuses.length
    ? `<div class="summary-reference-status-list">${mapSummary.referenceMapStatuses.map((row) =>
        _summaryMetric(row.map, row.status)).join("")}</div>`
    : "";
  // Compact status panel; raw per-map strings stay inside the collapsed details.
  return `<section class="compact-review-panel compact-status-panel reference-status-card sdc">
    <div class="sdc-head">
      <h3>Reference Comparison ${helpTooltip("Reference metrics are calculated only when a matching private ground-truth map is available.", "Reference comparison status help")}</h3>
      ${refChip}
    </div>
    <p class="sdc-reason">${escapeHtml(refReason)}</p>
    <details class="summary-details summary-reference-report">
    <summary>View details</summary>
    <div class="summary-details-body">
    ${statusListHtml}
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
          ${_renderHeaderCheck(row.header_check)}
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
    </div>
    </details>
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
  const isOfficial = p.official === true || p.category === "official";
  const isDev      = p.category === "development" || p.not_for_scoring === true;
  const isCustom   = !isOfficial && !isDev;
  const isReady    = p.status === "ready" || p.status === "dev_data_available";

  const cardCls    = `score-provider-card ${isOfficial ? "spc-official" : isDev ? "spc-dev" : "spc-custom"}`;
  const badgeCls   = isOfficial ? "spc-badge-official" : isDev ? "spc-badge-dev" : "spc-badge-custom";
  const badgeLabel = isOfficial ? "Official provider" : isDev ? "Development only" : "Custom package";
  const dotCls     = isReady && isOfficial ? "spc-dot-ready"
                   : isReady && isDev      ? "spc-dot-dev"
                   : isReady               ? "spc-dot-custom"
                   : "spc-dot-not-conf";

  const statusLabel = isReady
    ? (isOfficial ? "Ready for official scoring" : isDev ? "Test data available" : "Ready for analysis")
    : "Not configured";
  const labelCls    = isReady ? "spc-status-label" : "spc-status-label nc";

  const devNote = isDev
    ? `<p class="spc-warning">For development and provider testing only; not official challenge scoring.</p>`
    : "";

  const metadata = [
    p.challenge_type ? String(p.challenge_type).toUpperCase() : "",
    p.map_type ? String(p.map_type).toUpperCase() : "",
    p.source === "package" ? "Installed package" : p.source === "builtin" ? "Built in" : "",
  ].filter(Boolean);
  const missing = Array.isArray(p.missing) ? p.missing.filter(Boolean) : [];
  const requirementsHtml = missing.length
    ? `<div class="spc-requirements">
        <div class="spc-requirements-title">Missing requirements</div>
        <ul>${missing.map((item) => `<li><span class="spc-missing-marker">Missing</span><span>${escapeHtml(item)}</span></li>`).join("")}</ul>
      </div>`
    : `<div class="spc-ready-note"><span class="spc-ready-marker">Ready</span><span>No missing provider requirements reported.</span></div>`;

  return `<article class="${cardCls}">
    <div class="spc-header">
      <div class="spc-heading-copy">
        <div class="spc-title">${escapeHtml(p.provider_name)}</div>
        ${metadata.length ? `<div class="spc-meta">${metadata.map((item) => `<span>${escapeHtml(item)}</span>`).join("")}</div>` : ""}
      </div>
      <span class="spc-category-badge ${badgeCls}">${badgeLabel}</span>
    </div>
    <div class="spc-status-row">
      <span class="spc-status-dot ${dotCls}"></span>
      <span class="${labelCls}">${statusLabel}</span>
    </div>
    ${p.description ? `<p class="spc-desc">${escapeHtml(p.description)}</p>` : ""}
    ${devNote}
    ${requirementsHtml}
  </article>`;
}

// Update the main user-facing status card based on provider status.
// activeMode: "none" | "builtin" | "custom"
// packageName: display name of active custom package, or null
function _updateScoreStatusCard(provs, activeMode, packageName, activeOfficial = false) {
  const titleEl = el("score-status-title");
  const subEl   = el("score-status-sub");
  const badgeEl = el("score-status-badge");
  const hintEl  = el("score-status-hint");
  const btnAll  = el("btn-score-all");
  const previewEl = el("score-metric-preview");

  const isConfigured = !!(activeMode && activeMode !== "none");
  const isOfficial = activeOfficial === true;
  const actionText = isOfficial ? "Run Official Scoring" : "Run Analysis";

  if (isConfigured) {
    const existingPreview = _scoreMetricPreviewHtml();
    if (titleEl) titleEl.textContent = existingPreview
      ? (isOfficial ? "Official scoring complete" : "Analysis complete")
      : (isOfficial ? "Official scoring is ready" : "Analysis is ready");
    if (previewEl) {
      previewEl.innerHTML = existingPreview;
      previewEl.style.display = existingPreview ? "" : "none";
    }

    const scorerName = packageName || "Configured analysis provider";
    const pkgLabel = existingPreview
      ? "Metrics generated successfully."
      : isOfficial
        ? `${scorerName} is active.`
        : `${scorerName} is active for configured analysis. These are not official OSIPI scores.`;
    if (subEl)   subEl.textContent  = pkgLabel;

    const badgeTxt = "Ready";
    if (badgeEl) { badgeEl.textContent = badgeTxt; badgeEl.className = "smc-badge smc-badge--ready"; }
    if (hintEl)  hintEl.textContent   = "";
    if (btnAll)  btnAll.disabled      = false;
  } else {
    if (btnAll)  btnAll.disabled      = true;
  }

  // Re-wire the configured analysis action (clone clears old listeners)
  if (btnAll && isConfigured) {
    const fresh = btnAll.cloneNode(true);
    fresh.disabled    = false;
    fresh.textContent = actionText;
    btnAll.replaceWith(fresh);
    fresh.addEventListener("click", async () => {
      const subs = _getKnownSubmissions();
      // Ensure table is visible
      const tc = el("score-table-card");
      if (tc) tc.style.display = "";
      if (!subs.length) return;
      setLoading(fresh, true, isOfficial ? "Scoring" : "Checking");
      _initScoreProgress(subs.length);
      try {
        for (const sub of subs) {
          const sid       = sub.submission_id || sub;
          const challenge = sub.challenge_type || _getSessionChallengeType() || defaultChallengeType();
          const mapType   = defaultScoringMapType();
          await _runSingleScore(null, sid, challenge, mapType);
        }
      } finally {
        setLoading(fresh, false, actionText);
        _syncCompactProgress();
      }
    });
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// Admin Scoring Setup Panel
// ═══════════════════════════════════════════════════════════════════════════

let _configurationManagerState = null;
let _configurationManagerModalOpener = null;
let _installedScoringPackages = [];
let _compatibleBuiltinProviders = [];

function _configurationManagerChallenge() {
  return String(el("config-manager-challenge")?.value || _getSessionChallengeType() || defaultChallengeType()).toLowerCase();
}

function _configurationManagerShow(message, ok = null, html = "") {
  const target = el("config-manager-results");
  if (!target) return;
  target.className = `config-manager-results${ok === true ? " ok" : ok === false ? " err" : ""}`;
  target.innerHTML = html || escapeHtml(message || "");
  target.style.display = "";
}

function _configurationMapUnit(mapId) {
  const item = (_appConfig.mapTypes || []).find((entry) => String(entry?.id || "").toLowerCase() === String(mapId || "").toLowerCase());
  return String(item?.units || "Not specified");
}

function _builtinProviderForChallenge(providers, challengeType) {
  const challenge = String(challengeType || "").toLowerCase();
  const compatible = (providers || []).filter((provider) =>
    provider?.source === "builtin"
    && provider?.not_for_scoring !== true
    && String(provider?.challenge_type || "").toLowerCase() === challenge
  );
  return compatible.length === 1 ? compatible[0] : null;
}

function _providerDisplayName(provider, fallback = "Built-in provider") {
  return String(provider?.display_name || provider?.provider_name || provider?.provider_id || fallback);
}

function _pluralSummary(value, singular, plural = `${singular}s`) {
  return `${value} ${value === 1 ? singular : plural}`;
}

function _renderConfigurationSummaries(state = _configurationManagerState) {
  if (!state?.editable) return;
  const editable = state.editable;
  const challenge = String(editable.challenge_type || _configurationManagerChallenge()).toUpperCase();
  const challengeSummary = el("config-summary-challenge");
  if (challengeSummary) challengeSummary.textContent = `${editable.label || challenge} · ${editable.description || "No description configured"}`;

  const mapRows = Array.from(document.querySelectorAll("#config-manager-maps .config-map-row"));
  const mapStates = mapRows.map((row) => row.querySelector(".config-map-state")?.value || "unused");
  const requiredMaps = mapStates.filter((value) => value === "required").length;
  const optionalMaps = mapStates.filter((value) => value === "optional").length;
  const mapsSummary = el("config-summary-maps");
  if (mapsSummary) mapsSummary.textContent = `${_pluralSummary(requiredMaps, "required map")} · ${_pluralSummary(optionalMaps, "optional map")} · ${_pluralSummary(mapRows.length, "map definition")}`;

  const datasetRows = Array.from(document.querySelectorAll("#config-manager-datasets [data-dataset-name]"));
  const datasetText = datasetRows.map((row) => {
    const name = row.dataset.datasetName || "dataset";
    const value = (field, singular) => {
      const raw = row.querySelector(`.config-dataset-${field}`)?.value;
      return raw ? _pluralSummary(Number(raw), singular) : `${singular} count pending`;
    };
    return `${name}: ${value("participants", "participant")}, ${value("repeats", "repeat")}, ${value("sites", "site")}`;
  });
  const datasetsSummary = el("config-summary-datasets");
  if (datasetsSummary) datasetsSummary.textContent = datasetText.join(" · ") || "No dataset grid configured";

  const artifactRows = Array.from(document.querySelectorAll("#config-manager-artifacts [data-artifact-id]"));
  const requiredArtifacts = artifactRows.filter((row) => row.querySelector(".config-artifact-required")?.checked).length;
  const artifactsSummary = el("config-summary-artifacts");
  if (artifactsSummary) artifactsSummary.textContent = `${_pluralSummary(requiredArtifacts, "required artifact")} · Code execution ${el("config-manager-code-required")?.checked ? "required" : "optional"}`;

  const mode = el("config-manager-scoring-mode")?.value || editable.scoring?.mode || "none";
  const builtin = _builtinProviderForChallenge(state.builtin_providers, editable.challenge_type);
  const modeLabels = {
    none: "No provider",
    builtin: builtin ? _providerDisplayName(builtin) : "No compatible built-in provider",
    custom: "Trusted custom package",
  };
  let scoringText = modeLabels[mode] || mode;
  if (mode === "custom") {
    const packageId = el("config-manager-package")?.value || "";
    const installed = (state.packages || []).find((item) => item.package_id === packageId);
    scoringText += installed ? ` · ${installed.name} v${installed.version} · ${installed.ready ? "ready" : "not ready"}` : " · No compatible package selected";
  }
  const scoringSummary = el("config-summary-scoring");
  if (scoringSummary) scoringSummary.textContent = `${scoringText}. Generic QC and compatible reference comparisons remain separate.`;

  const counts = state.assets?.counts || {};
  const assetsSummary = el("config-summary-assets");
  if (assetsSummary) assetsSummary.textContent = `${_pluralSummary(Number(counts.reference || 0), "reference map")} · ${_pluralSummary(Number(counts.mask || 0), "mask")} · ${_pluralSummary(Number(counts.measured_signal || 0), "measured signal")}`;

  const capability = (state.capabilities || []).find((row) => row.challenge_type === editable.challenge_type);
  const capabilitiesSummary = el("config-summary-capabilities");
  if (capabilitiesSummary) capabilitiesSummary.textContent = capability
    ? `QC and previews available for readable maps · Provider analysis ${String(capability.provider_analysis || "not configured").toLowerCase()} · Official ranking not configured`
    : "Capability details are not available.";

  const versions = state.versions || [];
  const active = versions.find((item) => item.active);
  const versionsSummary = el("config-summary-versions");
  if (versionsSummary) versionsSummary.textContent = `${active ? `Active: ${active.version_id}` : "Active: repository rules"} · ${_pluralSummary(versions.length, "saved version")}`;
}

function _renderConfigurationChallengeDetails(state) {
  const target = el("config-manager-challenge-details");
  if (!target) return;
  const editable = state.editable || {};
  const active = (state.versions || []).find((item) => item.active);
  target.innerHTML = `
    <dt>Challenge type</dt><dd>${escapeHtml(String(editable.challenge_type || "").toUpperCase())}</dd>
    <dt>Label</dt><dd>${escapeHtml(editable.label || "Not configured")}</dd>
    <dt>Description</dt><dd>${escapeHtml(editable.description || "Not configured")}</dd>
    <dt>Active configuration</dt><dd>${escapeHtml(active?.version_id || "Repository validation rules")}</dd>`;
}

/* ── Configuration Manager: maps and artifacts ──────────────────────────────

   These panels are edited by challenge organisers, who are researchers rather
   than developers. The earlier version asked them to keep filename aliases in
   a comma separated textarea and gave every map an equally tall block, so the
   one decision that actually matters, whether a map is required, was the same
   size as reference information they rarely change.

   The rewrite makes the requirement a visible three way choice, turns aliases
   into chips so nobody has to punctuate a list correctly, and hides the rest
   behind a disclosure. The hidden inputs are deliberate: ``config-map-state``
   and ``config-map-aliases`` stay in the DOM with the same class names and the
   same value format, so the code that reads a draft is untouched by any of
   this.
*/

const MAP_STATES = [
  ["required", "Required", "A submission without this map is rejected."],
  ["optional", "Optional", "Accepted and analysed when present."],
  ["unused", "Not used", "Ignored entirely for this challenge."],
];

/* Plain descriptions of what requiring each artifact actually does. A bare
   checkbox labelled "Modelled signal-time curve" tells an organiser nothing
   about the consequence of ticking it. */
const ARTIFACT_NOTES = {
  modelled_st: "The fitted curve the model produced. Needed to compare the model against the measurement.",
  measured_st: "The measured curve from the scan. Needed for the residual sum of squares comparison.",
  methods: "A short write up of the method used, read alongside the results.",
};

function _aliasChip(alias) {
  const safe = escapeHtml(alias);
  return `<span class="cfg-chip">${safe}<button type="button" class="cfg-chip-x"
    data-alias="${safe}" aria-label="Remove ${safe}">&times;</button></span>`;
}

function _configurationMapRow(item) {
  const id = escapeHtml(item.id);
  const state = MAP_STATES.some(([value]) => value === item.state) ? item.state : "unused";
  const aliases = (item.aliases || []).filter(Boolean);
  const units = _configurationMapUnit(item.id);
  const segments = MAP_STATES.map(([value, label, note]) => `
    <button type="button" class="cfg-seg-btn" role="radio" data-state="${value}"
            aria-checked="${state === value}" title="${escapeHtml(note)}">${label}</button>`).join("");

  return `
  <div class="config-map-row cfg-map" data-map-id="${id}" data-state="${state}"
       data-search="${escapeHtml(`${item.display} ${item.label} ${aliases.join(" ")}`.toLowerCase())}">
    <div class="cfg-map-head">
      <button type="button" class="cfg-map-toggle" aria-expanded="false">
        <span class="cfg-caret" aria-hidden="true"></span>
        <span class="cfg-map-name">
          <strong>${escapeHtml(item.display)}</strong>
          <small>${escapeHtml(item.label)}</small>
        </span>
      </button>
      <div class="cfg-seg" role="radiogroup"
           aria-label="${escapeHtml(item.display)} requirement">${segments}</div>
      <input type="hidden" class="config-map-state" value="${escapeHtml(state)}">
    </div>

    <div class="cfg-map-detail" hidden>
      <div class="cfg-detail-block">
        <p class="cfg-label">Recognised filenames</p>
        <p class="cfg-hint">A file is treated as ${escapeHtml(item.display)} when its name
          contains one of these. Add the spellings your participants actually use.</p>
        <div class="cfg-chips" data-chips>
          ${aliases.map(_aliasChip).join("")}
          <input type="text" class="cfg-chip-input" placeholder="Add a name"
                 aria-label="Add a recognised filename for ${escapeHtml(item.display)}">
        </div>
        <input type="hidden" class="config-map-aliases" value="${escapeHtml(aliases.join(", "))}">
      </div>
      <div class="cfg-detail-grid">
        <label class="cfg-field">Dimensions
          <input class="config-map-dimensions" type="number" min="2" max="7"
                 value="${item.dimensions ?? ""}" placeholder="Any">
          <span class="cfg-hint">3 for a single map, 4 if it varies over time.</span>
        </label>
        <div class="cfg-field">Units
          <strong class="cfg-units">${escapeHtml(units)}</strong>
          <span class="cfg-hint">Read from the challenge definition.</span>
        </div>
      </div>
    </div>
  </div>`;
}

function _configurationMapsMarkup(items) {
  if (!items.length) return `<p class="cfg-empty">No maps are defined for this challenge.</p>`;
  const count = (state) => items.filter((item) => item.state === state).length;
  return `
    <div class="cfg-toolbar">
      <div class="cfg-counts">
        <span class="cfg-count cfg-count-required"><b data-count="required">${count("required")}</b> required</span>
        <span class="cfg-count cfg-count-optional"><b data-count="optional">${count("optional")}</b> optional</span>
        <span class="cfg-count cfg-count-unused"><b data-count="unused">${count("unused")}</b> not used</span>
      </div>
      <input type="search" class="cfg-search" id="cfg-map-search"
             placeholder="Search maps" aria-label="Search maps">
    </div>
    <div class="cfg-map-list">${items.map(_configurationMapRow).join("")}</div>
    <p class="cfg-empty" data-no-results hidden>No maps match that search.</p>`;
}

function _configurationArtifactsMarkup(items) {
  if (!items.length) return `<p class="cfg-empty">No artifact types are configured.</p>`;
  return `<div class="cfg-check-list">${items.map((item) => {
    const note = ARTIFACT_NOTES[item.id] || "";
    return `
    <label class="config-manager-check cfg-check" data-artifact-id="${escapeHtml(item.id)}">
      <input type="checkbox" class="config-artifact-required"${item.required ? " checked" : ""}>
      <span class="cfg-check-body">
        <span class="cfg-check-label">${escapeHtml(item.label)}</span>
        ${note ? `<span class="cfg-hint">${escapeHtml(note)}</span>` : ""}
      </span>
    </label>`;
  }).join("")}</div>
  <p class="cfg-hint cfg-check-footnote">Ticked items must be present or the submission
    is rejected at validation.</p>`;
}

/* Recount the summary chips after any requirement change. */
function _refreshConfigurationMapCounts() {
  const host = el("config-manager-maps");
  if (!host) return;
  const rows = Array.from(host.querySelectorAll(".config-map-row"));
  MAP_STATES.forEach(([value]) => {
    const badge = host.querySelector(`[data-count="${value}"]`);
    if (badge) badge.textContent = String(rows.filter((r) => r.dataset.state === value).length);
  });
}

function _writeAliases(row) {
  const chips = Array.from(row.querySelectorAll(".cfg-chip"))
    .map((chip) => chip.firstChild?.textContent?.trim())
    .filter(Boolean);
  const hidden = row.querySelector(".config-map-aliases");
  if (hidden) hidden.value = chips.join(", ");
}

/* Which names a typed entry actually adds, given what is already there.

   Kept free of the DOM so it can be tested directly: this is where the fiddly
   rules live. Someone pasting "cbf, perfusion" means two names rather than
   one, a repeat of something already listed adds nothing, and case is not a
   distinction worth keeping since the matching that uses these is itself
   case insensitive. */
function _newAliases(existing, raw) {
  const seen = new Set((existing || []).map((alias) => String(alias).trim().toLowerCase()));
  const additions = [];
  String(raw).split(",").forEach((part) => {
    const alias = part.trim();
    if (!alias || seen.has(alias.toLowerCase())) return;
    seen.add(alias.toLowerCase());
    additions.push(alias);
  });
  return additions;
}

function _addAlias(row, raw) {
  const input = row.querySelector(".cfg-chip-input");
  const existing = Array.from(row.querySelectorAll(".cfg-chip"))
    .map((chip) => (chip.firstChild?.textContent || "").trim());
  _newAliases(existing, raw).forEach((alias) => {
    input.insertAdjacentHTML("beforebegin", _aliasChip(alias));
  });
  _writeAliases(row);
}

let _configurationMapsBound = false;

function _bindConfigurationMapsInteractions() {
  const host = el("config-manager-maps");
  if (!host || _configurationMapsBound) return;
  _configurationMapsBound = true;

  host.addEventListener("click", (event) => {
    const segment = event.target.closest(".cfg-seg-btn");
    if (segment) {
      const row = segment.closest(".config-map-row");
      const value = segment.dataset.state;
      row.dataset.state = value;
      row.querySelector(".config-map-state").value = value;
      row.querySelectorAll(".cfg-seg-btn").forEach((button) => {
        button.setAttribute("aria-checked", String(button === segment));
      });
      _refreshConfigurationMapCounts();
      return;
    }
    const toggle = event.target.closest(".cfg-map-toggle");
    if (toggle) {
      const row = toggle.closest(".config-map-row");
      const detail = row.querySelector(".cfg-map-detail");
      const open = toggle.getAttribute("aria-expanded") === "true";
      toggle.setAttribute("aria-expanded", String(!open));
      detail.hidden = open;
      return;
    }
    const remove = event.target.closest(".cfg-chip-x");
    if (remove) {
      const row = remove.closest(".config-map-row");
      remove.closest(".cfg-chip").remove();
      _writeAliases(row);
    }
  });

  // Enter and comma commit a chip; backspace on an empty box removes the last.
  host.addEventListener("keydown", (event) => {
    const input = event.target.closest(".cfg-chip-input");
    if (!input) return;
    const row = input.closest(".config-map-row");
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      _addAlias(row, input.value);
      input.value = "";
    } else if (event.key === "Backspace" && !input.value) {
      const chips = row.querySelectorAll(".cfg-chip");
      if (chips.length) {
        chips[chips.length - 1].remove();
        _writeAliases(row);
      }
    }
  });

  // Clicking away must not silently discard what was typed.
  host.addEventListener("focusout", (event) => {
    const input = event.target.closest(".cfg-chip-input");
    if (!input || !input.value.trim()) return;
    const row = input.closest(".config-map-row");
    _addAlias(row, input.value);
    input.value = "";
  });

  host.addEventListener("input", (event) => {
    if (!event.target.closest(".cfg-search")) return;
    const term = event.target.value.trim().toLowerCase();
    const rows = Array.from(host.querySelectorAll(".config-map-row"));
    let shown = 0;
    rows.forEach((row) => {
      const match = !term || (row.dataset.search || "").includes(term);
      row.hidden = !match;
      if (match) shown += 1;
    });
    const empty = host.querySelector("[data-no-results]");
    if (empty) empty.hidden = shown !== 0;
  });
}

function _renderConfigurationManager(state) {
  _configurationManagerState = state;
  const editable = state.editable || {};
  const editor = el("config-manager-editor");
  const loading = el("config-manager-loading");
  if (loading) loading.style.display = "none";
  if (editor) editor.style.display = "";

  const maps = el("config-manager-maps");
  if (maps) {
    maps.innerHTML = _configurationMapsMarkup(editable.maps || []);
    _bindConfigurationMapsInteractions();
  }

  const artifacts = el("config-manager-artifacts");
  if (artifacts) {
    artifacts.innerHTML = _configurationArtifactsMarkup(editable.required_artifacts || []);
  }

  const datasets = el("config-manager-datasets");
  if (datasets) datasets.innerHTML = Object.entries(editable.datasets || {}).map(([name, counts]) => `
    <div class="config-dataset-row" data-dataset-name="${escapeHtml(name)}">
      <strong>${escapeHtml(name)}</strong>
      ${["participants", "repeats", "sites"].map((field) => `<label>${field}<input class="config-dataset-${field}" type="number" min="1" value="${counts?.[field] ?? ""}" placeholder="pending"></label>`).join("")}
    </div>`).join("") || `<p>No dataset grid is configured for this challenge.</p>`;

  const codeRequired = el("config-manager-code-required");
  if (codeRequired) codeRequired.checked = !!editable.code_execution_required;
  const referenceVersion = el("config-manager-reference-version");
  if (referenceVersion) referenceVersion.value = editable.reference_dataset_version || "";

  const scoringMode = el("config-manager-scoring-mode");
  if (scoringMode) {
    const builtin = _builtinProviderForChallenge(state.builtin_providers, editable.challenge_type);
    const builtinOption = el("config-manager-scoring-builtin-option");
    if (builtinOption) {
      builtinOption.textContent = builtin
        ? _providerDisplayName(builtin)
        : `No compatible built-in provider for ${String(editable.challenge_type || "").toUpperCase()}`;
      builtinOption.disabled = !builtin;
    }
    scoringMode.value = editable.scoring?.mode || "none";
  }
  const packageSelect = el("config-manager-package");
  const packages = (state.packages || []).filter((item) => item.challenge_type === editable.challenge_type);
  if (packageSelect) packageSelect.innerHTML = packages.map((item) =>
    `<option value="${escapeHtml(item.package_id)}"${item.package_id === editable.scoring?.package_id ? " selected" : ""}>${escapeHtml(item.name)} v${escapeHtml(item.version)}${item.ready ? "" : " (not ready)"}</option>`
  ).join("") || `<option value="">No compatible package installed</option>`;
  _syncConfigurationPackageVisibility();
  _renderConfigurationPackageDetail();
  _renderConfigurationVersions(state.versions || []);
  _renderConfigurationAssets(state.assets || {});
  _renderConfigurationCapabilities(state.capabilities || []);
  _renderConfigurationChallengeDetails(state);
  _renderConfigurationSummaries(state);
}

function _syncConfigurationPackageVisibility() {
  const wrap = el("config-manager-package-wrap");
  if (wrap) wrap.style.display = el("config-manager-scoring-mode")?.value === "custom" ? "" : "none";
}

function _renderConfigurationPackageDetail() {
  const target = el("config-manager-package-detail");
  if (!target) return;
  const packageId = el("config-manager-package")?.value || "";
  const item = (_configurationManagerState?.packages || []).find((entry) => entry.package_id === packageId);
  target.textContent = item
    ? `${item.name} · version ${item.version} · package id ${item.package_id} · ${item.ready ? "ready" : "not ready"}`
    : "No compatible package is selected.";
}

function _renderConfigurationVersions(versions) {
  const target = el("config-manager-versions");
  if (!target) return;
  target.innerHTML = versions.length ? versions.map((item) => `
    <div class="config-version-row">
      <span><span class="${item.active ? "config-version-active" : ""}">${escapeHtml(item.version_id)}${item.active ? " · active" : ""}</span><br><small>${escapeHtml(item.created_at || "")} · ${escapeHtml(item.source || "saved")}</small></span>
      <button type="button" class="btn btn-secondary btn-sm config-version-activate" data-version-id="${escapeHtml(item.version_id)}">${item.active ? "Re-test active" : "Activate / Restore"}</button>
    </div>`).join("") : `<p>No saved versions yet. Test, preview, then save the first version.</p>`;
}

function _renderConfigurationAssets(assets) {
  const target = el("config-manager-assets-status");
  if (!target) return;
  const counts = assets.counts || {};
  const summary = `<p>${Number(counts.reference || 0)} reference map(s) · ${Number(counts.mask || 0)} mask(s) · ${Number(counts.measured_signal || 0)} measured signal(s)</p>`;
  const rows = (assets.items || []).map((item) => `
    <div class="config-asset-row"><span>${escapeHtml(item.name)}<br><small>${escapeHtml(item.kind.replaceAll("_", " "))}</small></span><span class="${item.readable ? "config-version-active" : ""}">${item.readable ? "Readable" : "Unreadable"}${item.shape ? ` · ${escapeHtml(item.shape.join(" × "))}` : ""}</span></div>`).join("");
  target.innerHTML = summary + (rows || `<p>No private assets have been added for this challenge.</p>`);
}

function _renderConfigurationCapabilities(rows) {
  const target = el("config-manager-capabilities");
  if (!target) return;
  target.innerHTML = `<div class="config-capability-grid">${rows.map((row) => `
    <article class="config-capability-card">
      <h5>${escapeHtml(row.label)}</h5>
      <dl class="config-capability-list">
        <dt>QC &amp; previews</dt><dd>${escapeHtml(row.map_qc)}</dd>
        <dt>ROI statistics</dt><dd>${escapeHtml(row.roi_statistics)}</dd>
        <dt>Reference comparison</dt><dd>${escapeHtml(row.reference_comparison)}</dd>
        <dt>Difference maps</dt><dd>${escapeHtml(row.difference_maps)}</dd>
        <dt>RSS</dt><dd>${escapeHtml(row.rss)}</dd>
        <dt>Provider analysis</dt><dd>${escapeHtml(row.provider_analysis)}</dd>
        <dt>ICC</dt><dd>${escapeHtml(row.icc)}</dd>
        <dt>Official ranking</dt><dd>${escapeHtml(row.official_ranking)}</dd>
      </dl>
    </article>`).join("")}</div>`;
}

function _openConfigurationModal(section, opener) {
  const modal = el(`config-modal-${section}`);
  if (!modal) return;
  document.querySelectorAll(".config-manager-modal:not([hidden])").forEach((item) => {
    item.hidden = true;
    item.setAttribute("aria-hidden", "true");
  });
  _configurationManagerModalOpener = opener || null;
  modal.hidden = false;
  modal.setAttribute("aria-hidden", "false");
  document.body.classList.add("config-manager-modal-open");
  modal.querySelector(".config-manager-modal-close")?.focus();
}

function _closeConfigurationModal(modal = null) {
  const target = modal || document.querySelector(".config-manager-modal:not([hidden])");
  if (!target) return;
  target.hidden = true;
  target.setAttribute("aria-hidden", "true");
  document.body.classList.remove("config-manager-modal-open");
  _renderConfigurationSummaries();
  const opener = _configurationManagerModalOpener;
  _configurationManagerModalOpener = null;
  if (opener?.isConnected) opener.focus();
}

function _collectConfigurationDraft() {
  if (!_configurationManagerState?.editable) throw new Error("Configuration is not loaded.");
  const editable = JSON.parse(JSON.stringify(_configurationManagerState.editable));
  editable.maps = Array.from(document.querySelectorAll("#config-manager-maps .config-map-row")).map((row) => ({
    id: row.dataset.mapId,
    state: row.querySelector(".config-map-state")?.value || "unused",
    dimensions: row.querySelector(".config-map-dimensions")?.value || null,
    aliases: (row.querySelector(".config-map-aliases")?.value || "").split(",").map((item) => item.trim()).filter(Boolean),
  }));
  editable.required_artifacts = Array.from(document.querySelectorAll("#config-manager-artifacts [data-artifact-id]")).map((row) => ({
    id: row.dataset.artifactId,
    required: !!row.querySelector(".config-artifact-required")?.checked,
  }));
  editable.datasets = {};
  document.querySelectorAll("#config-manager-datasets [data-dataset-name]").forEach((row) => {
    const read = (field) => row.querySelector(`.config-dataset-${field}`)?.value || null;
    editable.datasets[row.dataset.datasetName] = { participants: read("participants"), repeats: read("repeats"), sites: read("sites") };
  });
  editable.code_execution_required = !!el("config-manager-code-required")?.checked;
  editable.reference_dataset_version = el("config-manager-reference-version")?.value.trim() || "";
  editable.scoring = {
    mode: el("config-manager-scoring-mode")?.value || "none",
    package_id: el("config-manager-scoring-mode")?.value === "custom" ? (el("config-manager-package")?.value || null) : null,
  };
  return { challenge_type: _configurationManagerChallenge(), configuration: editable };
}

async function _loadConfigurationManager(challengeType = null) {
  const select = el("config-manager-challenge");
  if (!select) return;
  if (!select.options.length) {
    select.innerHTML = (_appConfig.challengeTypes || []).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.label)}</option>`).join("");
  }
  const challenge = String(challengeType || _getSessionChallengeType() || select.value || defaultChallengeType()).toLowerCase();
  if ([...select.options].some((item) => item.value === challenge)) select.value = challenge;
  const loading = el("config-manager-loading");
  if (loading) { loading.textContent = "Loading active configuration…"; loading.style.display = ""; }
  try {
    const response = await fetch(`${API}/api/configuration-manager?challenge_type=${encodeURIComponent(challenge)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail || "Could not load configuration.");
    _renderConfigurationManager(data);
  } catch (error) {
    if (loading) loading.textContent = error.message;
  }
}

async function _configurationManagerPost(path, payload) {
  const response = await fetch(`${API}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || "Request failed.");
  return data;
}

async function _testConfigurationManager() {
  try {
    const data = await _configurationManagerPost("/api/configuration-manager/test", _collectConfigurationDraft());
    const items = (data.checks || []).map((item) => `<li><strong>${escapeHtml(item.status.toUpperCase())}</strong> · ${escapeHtml(item.name)}: ${escapeHtml(item.detail)}</li>`).join("");
    _configurationManagerShow(data.message, data.ready, `<strong>${escapeHtml(data.message)}</strong><ul>${items}</ul>`);
  } catch (error) { _configurationManagerShow(error.message, false); }
}

async function _previewConfigurationManager() {
  try {
    const data = await _configurationManagerPost("/api/configuration-manager/preview", _collectConfigurationDraft());
    const rows = (data.changes || []).map((item) => `<li><strong>${escapeHtml(item.field)}</strong>: ${escapeHtml(JSON.stringify(item.before))} → ${escapeHtml(JSON.stringify(item.after))}</li>`).join("");
    _configurationManagerShow("Preview ready", true, data.change_count ? `<strong>${data.change_count} change(s)</strong><ul>${rows}</ul>` : `<strong>No changes from the active configuration.</strong>`);
  } catch (error) { _configurationManagerShow(error.message, false); }
}

async function _saveConfigurationManager() {
  try {
    const data = await _configurationManagerPost("/api/configuration-manager/versions", _collectConfigurationDraft());
    _configurationManagerShow(`${data.version.version_id} saved. It is not active yet.`, true);
    await _loadConfigurationManager(_configurationManagerChallenge());
  } catch (error) { _configurationManagerShow(error.message, false); }
}

async function _activateConfigurationVersion(versionId) {
  try {
    const data = await _configurationManagerPost("/api/configuration-manager/activate", { challenge_type: _configurationManagerChallenge(), version_id: versionId });
    _configurationManagerShow(`${data.version_id} is now active.`, true);
    await hydrateAppConfig();
    await _loadConfigurationManager(data.challenge_type);
    await _loadScoringSetup();
  } catch (error) { _configurationManagerShow(error.message, false); }
}

function _wireConfigurationManager() {
  const panel = el("scoring-admin-panel");
  if (!panel || panel.dataset.configurationManagerWired) return;
  panel.dataset.configurationManagerWired = "true";
  panel.addEventListener("toggle", () => { if (panel.open && !_configurationManagerState) _loadConfigurationManager(); });
  el("config-manager-challenge")?.addEventListener("change", (event) => _loadConfigurationManager(event.target.value));
  el("config-manager-scoring-mode")?.addEventListener("change", () => {
    _syncConfigurationPackageVisibility();
    _renderConfigurationSummaries();
  });
  el("config-manager-package")?.addEventListener("change", () => {
    _renderConfigurationPackageDetail();
    _renderConfigurationSummaries();
  });
  el("config-manager-test")?.addEventListener("click", _testConfigurationManager);
  el("config-manager-preview")?.addEventListener("click", _previewConfigurationManager);
  el("config-manager-save")?.addEventListener("click", _saveConfigurationManager);
  document.addEventListener("click", (event) => {
    const openButton = event.target.closest("[data-config-modal-open]");
    if (openButton) {
      _openConfigurationModal(openButton.dataset.configModalOpen, openButton);
      return;
    }
    const closeButton = event.target.closest("[data-config-modal-close]");
    if (closeButton) {
      _closeConfigurationModal(closeButton.closest(".config-manager-modal"));
      return;
    }
    if (event.target.classList?.contains("config-manager-modal")) {
      _closeConfigurationModal(event.target);
      return;
    }
    const button = event.target.closest(".config-version-activate");
    if (button) _activateConfigurationVersion(button.dataset.versionId);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") _closeConfigurationModal();
  });
  el("config-manager-editor")?.addEventListener("input", () => _renderConfigurationSummaries());
  el("config-manager-editor")?.addEventListener("change", () => _renderConfigurationSummaries());
  el("config-manager-asset-file")?.addEventListener("change", (event) => {
    const target = el("config-manager-asset-file-name");
    if (target) target.textContent = event.target.files?.[0]?.name || "No file selected";
  });
  el("config-manager-export")?.addEventListener("click", () => {
    window.location.href = `${API}/api/configuration-manager/export?challenge_type=${encodeURIComponent(_configurationManagerChallenge())}`;
  });
  el("config-manager-import")?.addEventListener("change", async (event) => {
    const file = event.target.files?.[0];
    if (!file) return;
    const body = new FormData(); body.append("file", file);
    try {
      const response = await fetch(`${API}/api/configuration-manager/import`, { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Import failed.");
      _configurationManagerShow(`${data.version.version_id} imported as an inactive version.`, true);
      await _loadConfigurationManager(data.version.challenge_type);
    } catch (error) { _configurationManagerShow(error.message, false); }
    event.target.value = "";
  });
  el("config-manager-asset-upload")?.addEventListener("click", async () => {
    const file = el("config-manager-asset-file")?.files?.[0];
    if (!file) return _configurationManagerShow("Choose a NIfTI asset first.", false);
    const body = new FormData();
    body.append("challenge_type", _configurationManagerChallenge());
    body.append("asset_kind", el("config-manager-asset-kind")?.value || "reference");
    body.append("file", file);
    try {
      const response = await fetch(`${API}/api/configuration-manager/assets/upload`, { method: "POST", body });
      const data = await response.json();
      if (!response.ok) throw new Error(data.detail || "Asset upload failed.");
      _configurationManagerShow(`${data.asset.name} added to local private assets.`, true);
      el("config-manager-asset-file").value = "";
      if (el("config-manager-asset-file-name")) el("config-manager-asset-file-name").textContent = "No file selected";
      await _loadConfigurationManager(_configurationManagerChallenge());
    } catch (error) { _configurationManagerShow(error.message, false); }
  });
}

// Detect challenge type from the current session state.
function _getSessionChallengeType() {
  if (batchState.validationData && batchState.validationData.results) {
    const first = batchState.validationData.results[0];
    if (first && first.challenge_type) return first.challenge_type.toLowerCase();
  }
  // Reload restoration does not persist full validation payloads.
  // In that state the restored challenge radio is the authoritative fallback.
  return String(getChallengeType() || defaultChallengeType()).toLowerCase();
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
    _compatibleBuiltinProviders = (data.providers || []).filter((provider) =>
      provider?.source === "builtin"
      && provider?.not_for_scoring !== true
      && String(provider?.challenge_type || "").toLowerCase() === ct
    );
    const builtin = _builtinProviderForChallenge(data.providers, ct);
    const builtinRadio = el("scoring-mode-builtin");
    const builtinRow = el("scoring-mode-builtin-row");
    const builtinTitle = el("scoring-builtin-title");
    const builtinDescription = el("scoring-builtin-description");
    if (builtinRadio) builtinRadio.disabled = !builtin;
    if (builtinRow) builtinRow.classList.toggle("scoring-mode-row-unavailable", !builtin);
    if (builtinTitle) builtinTitle.textContent = builtin
      ? _providerDisplayName(builtin)
      : `No compatible built-in provider for ${ct.toUpperCase()}`;
    if (builtinDescription) builtinDescription.textContent = builtin
      ? `${builtin.description || "Built-in provider analysis."} This does not configure an overall OSIPI challenge ranking.`
      : "Use no provider analysis or select a compatible trusted custom package.";

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
        badgeEl.textContent = builtin ? _providerDisplayName(builtin) : "Unavailable";
        badgeEl.className   = builtin ? "scoring-admin-badge badge-ready" : "scoring-admin-badge";
      } else {
        const pkgs   = data.packages || [];
        const active = pkgs.find((p) => p.package_id === entry.package_id);
        badgeEl.textContent = active ? `${active.name} v${active.version}` : "Custom";
        badgeEl.className   = "scoring-admin-badge badge-custom";
      }
    }

    _renderScoringProviderSummary(mode, entry, data.packages || []);

    // Update builtin status line
    await _updateBuiltinStatus(data.providers || []);
  } catch (_) { /* silently ignore */ }
}

function _renderScoringProviderSummary(mode, entry, packages) {
  const title = el("scoring-provider-summary-title");
  const detail = el("scoring-provider-summary-detail");
  if (!title || !detail) return;
  const challenge = _getSessionChallengeType().toUpperCase();
  if (mode === "builtin") {
    title.textContent = entry.provider_name || "Built-in provider unavailable";
    detail.textContent = entry.provider_name
      ? `${challenge} provider analysis is active. Readiness details are available under Configure provider. Official OSIPI challenge ranking is not currently configured.`
      : `${challenge} has no compatible built-in provider. Select no provider analysis or a trusted custom package.`;
    return;
  }
  if (mode === "custom") {
    const active = (packages || []).find((item) => item.package_id === entry.package_id);
    title.textContent = active ? `${active.name} v${active.version}` : "Custom provider package";
    detail.textContent = active
      ? `${challenge} · ${active.status?.ready ? "Ready" : "Needs attention"} · ${active.description || active.package_id}`
      : `${challenge} uses a custom provider, but its installed package could not be resolved.`;
    return;
  }
  title.textContent = "No provider analysis configured";
  detail.textContent = `${challenge} still has generic QC and compatible generic reference comparisons. Official OSIPI ranking is not configured.`;
}

// Show/hide the custom package section based on selected mode.
function _onScoringModeChange(mode) {
  const customSec = el("scoring-custom-section");
  if (!customSec) return;
  customSec.style.display = mode === "custom" ? "" : "none";
}

// Check built-in TF6.2 provider readiness and update mode description.
async function _updateBuiltinStatus(providers = null) {
  const statusEl = el("scoring-builtin-status");
  if (!statusEl) return;
  try {
    let rows = providers;
    if (!Array.isArray(rows)) {
      const r = await fetch(`${API}/api/scoring-status`);
      const d = await r.json();
      rows = d.providers || [];
    }
    const prov = _builtinProviderForChallenge(rows, _getSessionChallengeType());
    if (prov) {
      if (prov.status === "ready") {
        statusEl.textContent  = `${_providerDisplayName(prov)} is ready`;
        statusEl.className    = "scoring-mode-status ok";
      } else {
        const missing = (prov.missing || []).join(", ");
        statusEl.textContent  = `Missing: ${missing || "reference data not configured"}`;
        statusEl.className    = "scoring-mode-status err";
      }
    } else {
      statusEl.textContent = "Not available for this challenge";
      statusEl.className = "scoring-mode-status";
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
    _installedScoringPackages = packages;
  } catch (_) {
    _installedScoringPackages = [];
    listEl.innerHTML = `<p style="font-size:0.75rem;color:var(--muted)">Could not load packages.</p>`;
    if (wrapEl) wrapEl.style.display = "none";
    _renderScoringPackageSelectionDetail();
    return;
  }

  if (packages.length === 0) {
    listEl.innerHTML = `<p style="font-size:0.75rem;color:var(--muted);margin:4px 0">No packages installed yet. Upload a scoring package ZIP above.</p>`;
    if (wrapEl) wrapEl.style.display = "none";
    _renderScoringPackageSelectionDetail();
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
          ${pkg.description ? " · " + escapeHtml(pkg.description) : ""}
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
    _renderScoringPackageSelectionDetail();
  }
}

function _renderScoringPackageSelectionDetail() {
  const target = el("scoring-pkg-selection-detail");
  const selected = el("scoring-pkg-select")?.value || "";
  if (!target) return;
  const item = _installedScoringPackages.find((pkg) => pkg.package_id === selected);
  target.textContent = item
    ? `${item.name} · version ${item.version} · ${String(item.challenge_type || "").toUpperCase()} · package id ${item.package_id} · ${item.status?.ready ? "ready" : "not ready"}`
    : "No package is selected.";
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
      const builtin = _builtinProviderForChallenge(
        _compatibleBuiltinProviders,
        ct,
      );
      const label = mode === "none"
        ? "Provider analysis disabled"
        : mode === "builtin"
          ? `${_providerDisplayName(builtin)} configured`
          : "Custom package configured";
      msgEl.textContent = label;
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
  _wireConfigurationManager();
  // Radio buttons: show/hide custom section
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
        if (statusEl) { statusEl.textContent = `Installed: ${d.manifest?.name || d.package_id}`; statusEl.className = "scoring-upload-status ok"; }
        await _loadInstalledPackages(_getSessionChallengeType(), d.package_id);
        fileInput.value = "";
      } catch (err) {
        if (statusEl) { statusEl.textContent = `Error: ${err.message}`; statusEl.className = "scoring-upload-status err"; }
      }
    });
  }

  el("scoring-pkg-select")?.addEventListener("change", _renderScoringPackageSelectionDetail);

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

  // Reload challenge rules after someone edits validation_rules.yaml.
  const reloadBtn = el("config-reload-btn");
  if (reloadBtn) reloadBtn.addEventListener("click", _reloadChallengeRules);
})();

async function _reloadChallengeRules() {
  const btn = el("config-reload-btn");
  const msg = el("config-reload-msg");

  const show = (text, ok) => {
    if (!msg) return;
    msg.textContent = text;
    msg.className = `config-reload-msg ${ok ? "ok" : "err"}`;
    msg.style.display = "";
  };

  if (btn) setLoading(btn, true, "Reloading");
  try {
    const r = await fetch(`${API}/api/config/reload`, { method: "POST" });
    const d = await r.json();

    // A rejected config is a normal answer here, not a failure to reach the
    // server: the person is expected to fix the file and press the button
    // again. Say which configuration is actually running so they are not left
    // wondering whether they have broken the pipeline.
    if (!r.ok) throw new Error(d.detail || "Request failed");
    if (!d.reloaded) {
      show(`${d.error} ${d.detail || ""}`.trim(), false);
      return;
    }

    const challenges = (d.challenges || []).join(", ") || "none";
    show(`Rules reloaded. Challenges: ${challenges}.`, true);

    // The challenge dropdown and map pills were built from the old config, so
    // re-pull them. Without this the server has the new rules and the screen
    // still shows the old ones.
    await hydrateAppConfig();
  } catch (err) {
    show(`Could not reload: ${err.message}`, false);
  } finally {
    if (btn) setLoading(btn, false);
  }
}

// ── renderScoreStep() ─────────────────────────────────────────────────────────

async function renderScoreStep() {
  unlockStep("score");
  loadLeaderboard();

  // Load admin scoring setup first (determines active mode)
  await _loadScoringSetup();

  // ── 1. Determine active scoring mode + package name ──────────────────────────
  let activeMode      = "none";
  let activePackageName = null;
  let activeOfficial = false;
  try {
    const r = await fetch(`${API}/api/scoring/active-config`);
    if (r.ok) {
      const d   = await r.json();
      const ct  = _getSessionChallengeType();
      const entry = (d.active_config || {})[ct] || {};
      activeMode  = entry.mode || "none";
      activeOfficial = entry.official === true;
      if (entry.provider_name) activePackageName = entry.provider_name;
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
  const activeIsOfficial = activeOfficial;
  _scoreOfficialMode = activeIsOfficial;
  _setScoreStepCopy({
    official: activeIsOfficial,
    providerName: activeIsOfficial ? (activePackageName || "Official Scoring & Preview") : "",
  });

  if (activeMode === "none") {
    // No visible not-configured card; QC/export fallback still runs below.
    if (notConfiguredCard) {
      notConfiguredCard.hidden = true;
      notConfiguredCard.style.display = "none";
    }
    if (statusCard)        statusCard.style.display        = "none";
    if (tableCard)         tableCard.style.display         = "none";
    const subs = _getKnownSubmissions();
    await Promise.all(subs.map(async (sub) => {
      const sid = sub.submission_id || sub;
      const ct  = sub.challenge_type || getChallengeType() || defaultChallengeType();
      try {
        const r = await fetch(`${API}/api/scoring-status?submission_id=${encodeURIComponent(sid)}&challenge_type=${encodeURIComponent(ct)}&map_type=${encodeURIComponent(defaultScoringMapType())}`);
        const d = await r.json();
        _applyScoreStatus(sid, d);
      } catch (_) { /* Summary can still render validation/export state. */ }
    }));
    renderScorePreviewPanel();
    saveSessionState();
    _syncCompactProgress();
    _refreshWizardFooter();
    return;
  }

  // ── 2. Scoring is configured, show ready card ───────────────────────────────
  if (notConfiguredCard) {
    notConfiguredCard.hidden = true;
    notConfiguredCard.style.display = "none";
  }
  if (statusCard)        statusCard.style.display        = "";
  // Note: score-provider-details is now inside the admin <details> panel,
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

  _updateScoreStatusCard(provs, activeMode, activePackageName, activeIsOfficial);

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
    renderScorePreviewPanel();
    _refreshWizardFooter();
    return;
  }

  const subs = _getKnownSubmissions();
  if (!subs.length) {
    renderScorePreviewPanel();
    _refreshWizardFooter();
    return;
  }

  tableCard.style.display = "none";
  tbody.innerHTML = "";

  for (const sub of subs) {
    const sid  = sub.submission_id || sub;
    const name = getSubmissionDisplayName(sub, sid);
    const ct   = sub.challenge_type || getChallengeType() || defaultChallengeType();
    tbody.insertAdjacentHTML("beforeend", _buildScoreRow(sid, name, ct));
  }

  for (const sub of subs) {
    const sid = sub.submission_id || sub;
    const ct  = sub.challenge_type || getChallengeType() || defaultChallengeType();
    _fetchAndUpdateScoreStatus(sid, ct);
  }
  renderScorePreviewPanel();
  _refreshWizardFooter();
}

// Build an HTML score table row for a given submission.
// Metric pills are hidden until actual scored values are available.
function _buildScoreRow(sid, displayName, challengeType) {
  const safeSid  = escapeHtml(sid);
  const safeChCt = escapeHtml(challengeType || defaultChallengeType());
  return `
  <tr class="sc-row-wrap" data-sub-id="${safeSid}"
      data-score-status="not_checked"
      data-challenge="${safeChCt}" data-map-type="${escapeHtml(defaultScoringMapType())}">
    <td class="sc-col-sub" data-display-name-for="${safeSid}" title="${escapeHtml(displayName || sid)}">${escapeHtml(displayName || sid)}</td>
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
              data-challenge="${safeChCt}" data-map-type="${escapeHtml(defaultScoringMapType())}"
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
  const ct = challengeType || getChallengeType() || defaultChallengeType();
  try {
    const r    = await fetch(`${API}/api/scoring-status?submission_id=${encodeURIComponent(sid)}&challenge_type=${encodeURIComponent(ct)}&map_type=${encodeURIComponent(defaultScoringMapType())}`);
    const data = await r.json();
    _applyScoreStatus(sid, data);
  } catch (err) {
    _applyScoreStatus(sid, { status: "not_configured", message: "Could not fetch status: " + err.message });
  }
}

// Apply a status response to a row, sets badge text, enables/disables Score button.
function _applyScoreStatus(sid, data) {
  const row = [...document.querySelectorAll(".sc-row-wrap")]
    .find((r) => r.dataset.subId === sid);
  _cacheScoreStatus(sid, data, row);
  if (!row) { renderScorePreviewPanel(); _syncCompactProgress(); return; }

  const status = data.status || "not_configured";
  const isOfficial = _scorePayload(data).official === true;
  row.dataset.scoreStatus = status;

  let badgeCls, badgeTxt;
  switch (status) {
    case "ready":          badgeCls = "ss-ready";    badgeTxt = isOfficial ? "Ready for official scoring" : "Ready for analysis"; break;
    case "scored":         badgeCls = "ss-scored";   badgeTxt = isOfficial ? "Official scoring complete" : "Analysis complete"; break;
    case "failed":         badgeCls = "ss-failed";   badgeTxt = isOfficial ? "Official scoring failed" : "Analysis failed"; break;
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

  // Populate detail drawer: only show it for scored/failed/missing, NOT for bare "not_configured"
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
      detail.innerHTML = `<details><summary style="font-size:0.73rem;cursor:pointer;color:var(--muted)">Analysis needs additional setup, expand to see details</summary>`
        + `<ul class="sc-missing-list">${missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul></details>`;
      if (detailRow) detailRow.style.display = "";
    } else {
      // not_configured or generic, don't show a noisy detail row
      detail.innerHTML = "";
      if (detailRow) detailRow.style.display = "none";
    }
  }
  renderScorePreviewPanel();
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
  // Official-provider metrics and flattened QC/demo metrics from custom packages.
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
      display_name:   getSubmissionDisplayName(r, r.submission_id || "Submission"),
      challenge_type: r.challenge_type || getChallengeType() || defaultChallengeType(),
    }));
  }
  if (state.submissionId) {
    return [{
      submission_id:  state.submissionId,
      display_name:   getSubmissionDisplayName({ submission_id: state.submissionId }, state.submissionId),
      challenge_type: getChallengeType() || defaultChallengeType(),
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
  const chall   = btn.dataset.challenge || defaultChallengeType();
  const mapType = btn.dataset.mapType   || defaultScoringMapType();
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
  // Map tabs in the one-at-a-time Map Preview panel
  const mapTab = e.target.closest("[data-preview-tab]");
  if (mapTab) {
    e.preventDefault();
    const tabId = mapTab.getAttribute("data-preview-tab");
    if (tabId && _previewItemsById[tabId]) {
      _previewSelectedMapId = tabId;
      const section = el("score-image-preview-section");
      if (section) {
        const sid = section.dataset.submissionId || "";
        const manifest = {
          submission_id: sid,
          maps: _previewMapOrder.map((id) => _previewItemsById[id]).filter(Boolean),
        };
        section.outerHTML = _renderImagePreviewSection(manifest, { submissionId: sid });
      }
    }
    return;
  }

  // Prev/Next gallery controls in the preview modal
  const navBtn = e.target.closest("[data-preview-nav]");
  if (navBtn) {
    e.preventDefault();
    _stepNiftiPreview(navBtn.getAttribute("data-preview-nav") === "prev" ? -1 : 1);
    return;
  }

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
  if (e.key === "Escape") { _closeNiftiPreview(); return; }
  // Arrow keys browse the modal gallery while it is open.
  const modal = el("nifti-preview-modal");
  if (!modal || modal.hidden) return;
  if (e.key === "ArrowLeft")  { e.preventDefault(); _stepNiftiPreview(-1); }
  if (e.key === "ArrowRight") { e.preventDefault(); _stepNiftiPreview(1); }
});

// "Score All" button is wired dynamically inside renderScoreStep().

// ── Score & Preview panel ─────────────────────────────────────────────────────

function renderScorePreviewPanel() {
  unlockStep("export");
  // Keep the action row in sync so Continue to Export is enabled on Step 5.
  if (typeof _refreshWizardFooter === "function") _refreshWizardFooter();

  // ROI parameter-map statistics come from the canonical scoring result already in
  // the cache. Rendered here because this is the one function every entry
  // point funnels through, initial render, each async status resolution,
  // step navigation, and session restore, so the section updates without a
  // page reload and without a second request.
  renderRoiDescriptiveStatistics(..._roiDescriptivePayload());

  const container = el("score-preview-panel");
  if (!container) return;
  container.style.display = "";

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
  // One calm note when reference scoring is unavailable, raw per-map status
  // strings live in Technical Details, never in the hero card.
  const referenceUnavailableNote = mapSummary.referenceComparedMapCount > 0
    ? ""
    : `<p class="summary-qc-only-note">Reference maps were not available, so this run shows QC metrics only.</p>`;
  const finalMetricRows = mapSummary.referenceComparedMapCount > 0
    ? `
      ${_summaryMetric("RMSE", _metricOrUnavailable(mapSummary.referenceMetrics.rmse))}
      ${_summaryMetric("MAE", _metricOrUnavailable(mapSummary.referenceMetrics.mae))}
      ${_summaryMetric("Bias", _metricOrUnavailable(mapSummary.referenceMetrics.bias))}
      ${_summaryMetric("CoV", _metricOrUnavailable(mapSummary.referenceMetrics.coefficientOfVariation))}
    `
    : "";
  // Submission case bar: slim clinical case strip, name, one status chip,
  // one meta line, one calm note. Nothing raw, nothing duplicated.
  const heroMetaParts = [`${escapeHtml(challengeLabel(challengeType))} challenge`];
  if (valTotal > 1) heroMetaParts.push(`${valTotal} submissions`);
  heroMetaParts.push(`${escapeHtml(comparedMapText)} map${comparedMapText === "1" ? "" : "s"} checked`);
  heroMetaParts.push(escapeHtml(_listText(mapSummary.detected)));
  const heroMeta = heroMetaParts.join(" · ");
  const finalOutputHtml = `
    <header class="score-case-bar submission-case-header">
      <div class="sch-main">
        <h2 class="sch-name" title="${escapeHtml(submissionName)}">${escapeHtml(submissionName)}</h2>
        <p class="sch-meta">${heroMeta}</p>
        ${referenceUnavailableNote}
      </div>
      <div class="sch-chip">${statusPill(overall.label, overall.state)}</div>
      ${finalMetricRows ? `<div class="score-final-metrics">${finalMetricRows}</div>` : ""}
    </header>`;

  // QC Results table: compact lab-results style rows from real values only.
  // CoV, standard deviation, and full voxel counts stay in Technical Details.
  const finitePct = typeof mapSummary.finitePercent === "number" && isFinite(mapSummary.finitePercent)
    ? Math.max(0, Math.min(100, mapSummary.finitePercent)) : null;
  const invalidCount = (mapSummary.nanCount || 0) + (mapSummary.infCount || 0);
  const qcRows = [
    { metric: "Finite voxels", result: _fmtPercentValue(mapSummary.finitePercent),
      status: finitePct !== null && finitePct >= 100 ? "Passed" : "",
      tone: finitePct !== null && finitePct >= 99.5 ? "good" : "", bar: true },
    { metric: "NaN / Inf", result: `${mapSummary.nanCount} / ${mapSummary.infCount}`,
      status: invalidCount === 0 ? "None detected" : `${invalidCount} found`,
      tone: invalidCount === 0 ? "good" : "warn" },
    { metric: "Negative voxels", result: _fmtPercentValue(mapSummary.negativePercent),
      status: (mapSummary.negativePercent || 0) === 0 ? "None detected" : "Present",
      tone: (mapSummary.negativePercent || 0) === 0 ? "good" : "" },
  ];
  const mapMeta = _mapMetaByDisplay();
  Object.entries(mapSummary.meansByType || {}).forEach(([display, value]) => {
    if (typeof value !== "number" || !isFinite(value)) return;
    const unit = _metricUnitText(mapMeta[display]?.units);
    qcRows.push({ metric: `Mean ${display}`, result: _fmtMetricVal(value), status: unit });
  });
  qcRows.push({ metric: "Maps", result: String(mapSummary.mapCount || 0), status: "checked" });
  const finiteBarHtml = finitePct !== null
    ? `<div class="qc-quality-bar"><div class="qc-bar-track"><div class="qc-bar-fill${finitePct >= 99.5 ? " is-good" : ""}" style="width:${finitePct}%"></div></div></div>`
    : "";
  const qcSummaryHtml = `
    <section class="compact-review-panel qc-results-panel sdc">
      <div class="sdc-head">
        <h3>QC Results ${helpTooltip("QC metrics describe map validity and statistics. They are not official OSIPI scores.", "QC metrics help")}</h3>
      </div>
      <div class="qc-results-table">
        <div class="qc-result-row qc-result-head" aria-hidden="true">
          <span class="qc-cell qc-cell-metric">Metric</span>
          <span class="qc-cell qc-cell-result">Result</span>
          <span class="qc-cell qc-cell-status">Status</span>
        </div>
        ${qcRows.map((r) => `<div class="qc-result-row${r.tone ? ` is-${r.tone}` : ""}">
          <span class="qc-cell qc-cell-metric">${escapeHtml(r.metric)}</span>
          <span class="qc-cell qc-cell-result">${escapeHtml(r.result)}</span>
          <span class="qc-cell qc-cell-status">${escapeHtml(r.status || "")}</span>
        </div>${r.bar ? finiteBarHtml : ""}`).join("")}
      </div>
    </section>`;

  const imagePreviewHtml = _renderImagePreviewSection(previewManifest, {
    loading: !!previewSubmissionId && !previewManifest,
    submissionId: previewSubmissionId,
  });

  const referenceReportHtml = _renderReferenceReportSection(mapSummary);

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
    if (!errs.length && !warns.length) return `<div class="summary-detail-sub"><b>${name}</b>, no issues.</div>`;
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
  // Full QC metrics that were removed from the visible Key Metrics list.
  const hiddenQcMetricsHtml = `
    <div class="summary-vertical-metrics">
      ${_summaryMetric("Coefficient of variation", _metricOrUnavailable(mapSummary.coefficientOfVariation))}
      ${_summaryMetric("Standard deviation", _metricOrUnavailable(mapSummary.standardDeviation))}
      ${_summaryMetric("Finite voxel count", `${mapSummary.finiteVoxelCount}/${mapSummary.totalVoxelCount}`)}
      ${_summaryMetric("Negative voxel count", String(mapSummary.negativeVoxelCount))}
      ${_summaryMetric("Map count", String(mapSummary.mapCount || 0))}
      ${_summaryMetric("Reference status per map", mapSummary.referenceMapStatusText)}
    </div>`;
  const detailsHtml = `
    <details class="summary-details technical-details-drawer">
      <summary>Technical Details</summary>
      <div class="summary-details-body">
        ${scorerNote}
        <div class="summary-detail-block"><div class="summary-detail-h">Full QC metrics</div>${hiddenQcMetricsHtml}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">Validation issues</div>${issuesHtml || '<p class="summary-muted">No submissions.</p>'}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">Reference scoring metrics</div>${_renderReferenceTechnicalTable(analysisEntries)}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">Per-map NIfTI metadata and statistics</div>${_renderNiftiTechnicalTable(analysisEntries)}</div>
        <div class="summary-detail-block"><div class="summary-detail-h">QC summary JSON</div><pre class="summary-json-block">${escapeHtml(JSON.stringify(qcSummaryJson, null, 2))}</pre></div>
        ${rawMetricTable}
      </div>
    </details>`;

  // ── Assemble clinical review screen (no workflow chip row) ─────────────────
  container.className = "score-preview-panel summary-report";
  container.innerHTML = `<div class="score-preview-workbench">
    ${finalOutputHtml}
    ${qcSummaryHtml}
    ${imagePreviewHtml}
    <div class="scoring-export-strip reference-export-row">
      ${referenceReportHtml}
    </div>
    ${detailsHtml}
  </div>`;
  if (previewSubmissionId) _loadAndRenderImagePreviews(previewSubmissionId, challengeType);
}

// Direct "Continue to Export" (used from Score & Preview actions)
function _goToExport() {
  unlockStep("export");
  _syncExportStep();
  goToStep("export");
}

const scoreContinueBtn = el("btn-score-continue");
if (scoreContinueBtn) scoreContinueBtn.addEventListener("click", _goToExport);
const scoreContinueBtnNc = el("btn-score-continue-nc");
if (scoreContinueBtnNc) scoreContinueBtnNc.addEventListener("click", _goToExport);

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
      // Researcher CSV downloads use the tidy long format (one row per map/ROI/metric).
      const res = await fetch(`${API}/api/export-combined?${q}&blinded=${blinded}&shape=long`);
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
      const blob  = await res.blob();
      const cd    = res.headers.get("Content-Disposition") || "";
      const fname = cd.match(/filename="([^"]+)"/)?.[1] || `osipi_results_long_${blinded ? "blinded" : "unblinded"}.csv`;
      triggerDownload(blob, fname);
    } catch (err) {
      if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "Export failed."; }
    } finally {
      setLoading(btn, false, label);
    }
  });
}

function _makeCombinedJsonExportHandler(btn, blinded = true) {
  if (!btn) return;
  const label = btn.textContent.trim() || "Download JSON";
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
      const res = await fetch(`${API}/api/export-combined?${q}&blinded=${blinded}&format=json`);
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "JSON export failed."); }
      const blob = await res.blob();
      const cd = res.headers.get("Content-Disposition") || "";
      const fname = cd.match(/filename="([^"]+)"/)?.[1] || `osipi_combined_${blinded ? "blinded" : "unblinded"}.json`;
      triggerDownload(blob, fname);
    } catch (err) {
      if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "JSON export failed."; }
    } finally {
      setLoading(btn, false, label);
    }
  });
}

// Export rows come from the SAME shared file-row renderer as every other step.
// Rendered once at load so the button ids exist before the handlers wire below.
function _renderExportRows() {
  const host = el("export-main-list");
  if (!host) return;
  const rows = [
    { id: "export-pdf-report-group", icon: "PDF", iconClass: "export-icon-check", title: "PDF Report",
      meta: "Concise shareable report with metadata, validation and execution status, QC results, and limitations.",
      btn: `<button type="button" id="export-pdf-report-btn" class="btn btn-secondary export-dl-btn export-compact-btn export-primary-action" aria-label="Download PDF report" title="Downloads the blinded PDF report.">Download PDF</button>` },
    { id: "export-report-group", icon: "HTML", iconClass: "export-icon-check", title: "HTML Report",
      meta: "Self-contained report with validation, execution, QC and configured analysis tables, issues, methodology, and limitations.",
      btn: `<button type="button" id="export-report-btn" class="btn btn-secondary export-dl-btn export-compact-btn export-primary-action" aria-label="Open HTML report" title="Opens the blinded HTML report in a new tab.">Open Report</button>` },
    { id: "export-combined-csv-group", icon: "CSV", iconClass: "export-icon-score", title: "CSV Results",
      meta: "Standard blinded tabular QC/reference summary without team or contact details.",
      btn: `<button type="button" id="export-combined-csv-btn" class="btn btn-secondary export-dl-btn export-compact-btn export-primary-action" aria-label="Download CSV results" title="Downloads the blinded combined CSV summary.">Download CSV</button>` },
    { id: "export-combined-json-group", icon: "JSON", iconClass: "export-icon-run", title: "JSON Results",
      meta: "Machine-readable validation, execution, QC, reference, and limitation summary.",
      btn: `<button type="button" id="export-combined-json-btn" class="btn btn-secondary export-dl-btn export-compact-btn export-primary-action" aria-label="Download JSON results" title="Downloads the blinded combined JSON summary.">Download JSON</button>` },
    { id: "export-roi-descriptive-group", icon: "CSV", iconClass: "export-icon-score", title: "ROI Parameter-map Statistics CSV",
      meta: "Scan-level mean, median, SD, range, and CoV for configured parameter maps within each compatible ROI. Descriptive within-scan values, not accuracy or repeatability.",
      btn: `<button type="button" id="export-roi-descriptive-btn" class="btn btn-secondary export-dl-btn export-compact-btn export-primary-action" aria-label="Download ROI parameter-map statistics CSV" title="Downloads within-ROI descriptive statistics per map and scan.">Download CSV</button>` },
    { id: "export-combined-unblinded-group", icon: "CSV", iconClass: "export-icon-score", title: "Unblinded CSV",
      meta: "CSV with team, contact, and original submission identifiers for internal review.",
      btn: `<button type="button" id="export-combined-unblinded-btn" class="btn btn-secondary export-dl-btn export-compact-btn export-primary-action" aria-label="Download unblinded combined CSV" title="Unblinded export includes team name and contact email.">Download CSV</button>` },
  ];
  const renderRows = (items) => items.map((r) => renderFileRow({
    extraClass: "export-main-row export-file-row",
    attrs: `id="${r.id}"`,
    icon: r.icon, iconClass: `export-group-icon export-file-icon ${r.iconClass}`,
    title: r.title, titleClass: "export-group-title",
    metaHtml: r.meta, metaClass: "export-group-sub",
    actionsHtml: r.btn, actionsClass: "export-group-body export-file-actions",
  })).join("");
  host.innerHTML = `
    <section class="export-output-group" aria-labelledby="export-reviewer-heading">
      <div class="export-output-heading">
        <h2 id="export-reviewer-heading">Blinded reviewer outputs</h2>
        <p>Team, contact, and original submission identifiers are removed.</p>
      </div>
      <div class="worklist export-output-list">${renderRows(rows.slice(0, 5))}</div>
    </section>
    <section class="export-output-group export-output-group--organiser" aria-labelledby="export-organiser-heading">
      <div class="export-output-heading">
        <h2 id="export-organiser-heading">Organiser-only output</h2>
        <p>Contains identifying information and should remain internal.</p>
      </div>
      <div class="worklist export-output-list">${renderRows(rows.slice(5))}</div>
    </section>`;
}
_renderExportRows();

// ROI descriptive CSV. Reads records already computed during scoring, the
// download never triggers a recalculation.
const roiCsvBtn = el("export-roi-descriptive-btn");
if (roiCsvBtn) roiCsvBtn.addEventListener("click", async () => {
  const statusEl = el("export-combined-status");
  const q = _sessionExportQuery();
  if (!q) {
    if (statusEl) {
      statusEl.style.display = "";
      statusEl.className = "submit-status status-error";
      statusEl.textContent = "No submission or batch to export. Validate first.";
    }
    return;
  }
  const label = roiCsvBtn.textContent.trim() || "Download CSV";
  setLoading(roiCsvBtn, true, label);
  if (statusEl) statusEl.style.display = "none";
  try {
    const res = await fetch(`${API}/api/export-roi-descriptive?${q}`);
    if (!res.ok) {
      const d = await res.json().catch(() => ({}));
      throw new Error(d.detail || "Export failed.");
    }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    // A header-only CSV is a valid response when no ROI rows exist.
    const fname = cd.match(/filename="([^"]+)"/)?.[1]
      || "roi_descriptive_statistics.csv";
    triggerDownload(blob, fname);
  } catch (err) {
    if (statusEl) {
      statusEl.style.display = "";
      statusEl.className = "submit-status status-error";
      statusEl.textContent = err.message || "Export failed.";
    }
  } finally {
    setLoading(roiCsvBtn, false, label);
  }
});

_makeCombinedExportHandler(el("export-combined-unblinded-btn"), false);
_makeCombinedExportHandler(el("export-combined-csv-btn"), true);
_makeCombinedJsonExportHandler(el("export-combined-json-btn"), true);

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

const pdfReportBtn = el("export-pdf-report-btn");
if (pdfReportBtn) pdfReportBtn.addEventListener("click", async () => {
  const statusEl = el("export-combined-status");
  const q = _sessionExportQuery();
  const label = pdfReportBtn.textContent.trim() || "Download PDF";
  if (!q) {
    if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = "No submission or batch to report on. Validate first."; }
    return;
  }
  setLoading(pdfReportBtn, true, label);
  if (statusEl) statusEl.style.display = "none";
  try {
    const res = await fetch(`${API}/api/export/report/pdf?${q}&blinded=true`);
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "PDF report failed."); }
    const blob = await res.blob();
    const cd = res.headers.get("Content-Disposition") || "";
    const fname = cd.match(/filename="([^"]+)"/)?.[1] || "osipi_report.pdf";
    triggerDownload(blob, fname);
  } catch (err) {
    if (statusEl) { statusEl.style.display = ""; statusEl.className = "submit-status status-error"; statusEl.textContent = err.message || "PDF report failed."; }
  } finally {
    setLoading(pdfReportBtn, false, label);
  }
});

// ── Init ───────────────────────────────────────────────────────────────────────

updateMapTypePills(getRadio("challenge_type") || defaultChallengeType());
hydrateAppConfig();
syncSubmitLabel();

// Stamp the initial step on <body> so CSS body[data-step] rules fire immediately,
// then sync the local action area for the current step.
document.body.dataset.step = wf.step;
_syncWfNav();
_updateWizardFooter(wf.step);

// ══════════════════════════════════════════════════════════════════════════════
// Session restore: startup check
//   1. Reload in the same tab (sessionStorage wizard state or URL hash present)
//      → auto-restore straight to the last valid step.
//   2. Fresh tab with an older localStorage session → subtle notice, manual
//      restore via the existing chip (unchanged behavior).
// ══════════════════════════════════════════════════════════════════════════════

// Hash navigation: allow #step jumps between already-unlocked steps only.
window.addEventListener("hashchange", () => {
  const step = _hashStep();
  if (!step || step === wf.step) return;
  const btn = el(`wf-btn-${step}`);
  if (step === "upload" || (btn && !btn.disabled)) {
    goToStep(step);
    if (step === "run")     _renderRunPanel?.();
    if (step === "score")   renderScoreStep().catch(() => {});
    if (step === "export")  _syncExportStep();
  } else {
    // Locked step: revert the hash to the current step.
    const h = STEP_TO_HASH[wf.step];
    if (h) { try { history.replaceState(null, "", `#${h}`); } catch (_) {} }
  }
});

(async function initSessionBanner() {
  // 1. Reload auto-restore (sessionStorage / hash preferred)
  try {
    const restored = await restoreWizardState();
    if (restored) { _hideRestoreBanner(); return; }
  } catch (_) { /* fall through to manual restore notice */ }

  const saved = loadSessionState();
  if (!saved) return;   // nothing saved or expired, normal fresh start

  // 2. Show the subtle notice, manual restore only
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
      _hideRestoreBanner();
      _resetToUploadAndClearPersistence();
    });
  }
})();

// Hidden reset hook used by contextual "New Submission" / restore actions.
(function initHiddenNewSessionHook() {
  const btn = el("sidebar-new-session-btn");
  if (!btn) return;
  btn.addEventListener("click", () => {
    _hideRestoreBanner();
    _resetToUploadAndClearPersistence();
  });
})();

/* ── ROI Ktrans statistics / configured parameter maps ───────────────────
   Renders the canonical records computed once during scoring. Nothing here
   recalculates a statistic: CoV arrives as a ratio and is only formatted
   for display, and unavailable values are never shown as zero.
   These are within-scan spatial summaries, not repeatability,
   reproducibility, or accuracy.                                          */

const ROI_UNAVAILABLE_MESSAGES = {
  no_roi_configured: "ROI parameter-map statistics are unavailable because no ROI masks were configured.",
  no_eligible_maps: "No valid configured parameter maps were available for ROI statistics.",
  calculation_error: "ROI parameter-map statistics could not be calculated. Existing validation and scoring results are still available.",
};

const ROI_REASON_LABELS = {
  empty_roi: "Empty ROI",
  no_finite_values: "No finite values",
  mean_near_zero: "Mean near zero",
  geometry_mismatch: "Geometry mismatch",
  map_unreadable: "Map unreadable",
  mask_unreadable: "Mask unreadable",
  available: "Available",
};

function _roiNumber(value, digits = 4) {
  // Unavailable is unavailable. Rendering it as 0 would read as a measurement.
  if (value === null || value === undefined || Number.isNaN(value)) return "Unavailable";
  const n = Number(value);
  if (!Number.isFinite(n)) return "Unavailable";
  return Math.abs(n) >= 0.0001 || n === 0 ? n.toFixed(digits) : n.toExponential(2);
}

function _roiPercent(value) {
  // Canonical value is a ratio; the percentage exists only for display.
  if (value === null || value === undefined) return "Unavailable";
  const n = Number(value);
  if (!Number.isFinite(n)) return "Unavailable";
  return `${(n * 100).toFixed(2)}%`;
}

function _roiIdentity(value) {
  return value === null || value === undefined || value === "" ? "—" : String(value);
}

function _roiDatasetLabel(value) {
  const text = _roiIdentity(value);
  return text === "—" ? text : text.charAt(0).toUpperCase() + text.slice(1);
}

function _roiRange(record) {
  if (record.roi_minimum == null || record.roi_maximum == null) return "Unavailable";
  return `${_roiNumber(record.roi_minimum)} to ${_roiNumber(record.roi_maximum)}`;
}

function renderRoiDescriptiveStatistics(records, status) {
  const card = el("roi-descriptive-card");
  if (!card) return;
  const table = el("roi-descriptive-table");
  const body = el("roi-descriptive-body");
  const empty = el("roi-descriptive-empty");
  const count = el("roi-descriptive-count");
  const method = el("roi-descriptive-method");
  const rows = Array.isArray(records) ? records : [];

  // Only shown when the canonical result actually carries ROI data or an
  // explicit status.
  if (!rows.length && !status) {
    card.style.display = "none";
    return;
  }
  card.style.display = "";
  if (count) count.textContent = String(rows.length);

  if (!rows.length) {
    // Clear, don't just hide. Hiding leaves the previous submission's rows in
    // the DOM, so opening a new submission or a result without ROI data would carry
    // stale values forward the moment the table was shown again.
    if (body) body.innerHTML = "";
    if (table) table.style.display = "none";
    if (empty) {
      empty.style.display = "";
      empty.textContent = ROI_UNAVAILABLE_MESSAGES[status]
        || "No ROI parameter-map statistics are available.";
    }
    if (method) method.textContent = "";
    return;
  }

  if (empty) empty.style.display = "none";
  if (table) table.style.display = "";

  // One table, built as a single string, no card per scan, no per-cell
  // listeners. Every dynamic field is escaped.
  if (body) {
    body.innerHTML = rows.map((r) => {
      const reason = ROI_REASON_LABELS[r.unavailable_reason || r.status]
        || String(r.status || "");
      const voxels = r.mask_voxel_count && r.mask_voxel_count !== r.voxel_count
        ? `${r.voxel_count} of ${r.mask_voxel_count}`
        : String(r.voxel_count ?? 0);
      return `<tr>
        <td>${escapeHtml(_roiDatasetLabel(r.dataset))}</td>
        <td>${escapeHtml(_roiIdentity(r.participant))}</td>
        <td>${escapeHtml(_roiIdentity(r.repeat))}</td>
        <td>${escapeHtml(_roiIdentity(r.site))}</td>
        <td>${escapeHtml(_roiIdentity(r.map_type).toUpperCase())}</td>
        <td>${escapeHtml(r.roi_label || r.roi_id || "—")}</td>
        <td>${escapeHtml(_roiNumber(r.roi_mean))}</td>
        <td>${escapeHtml(_roiNumber(r.roi_median))}</td>
        <td>${escapeHtml(_roiNumber(r.roi_within_scan_sd))}</td>
        <td>${escapeHtml(_roiRange(r))}</td>
        <td>${escapeHtml(_roiPercent(r.roi_within_scan_cov))}</td>
        <td>${escapeHtml(voxels)}</td>
        <td>${escapeHtml(reason)}</td>
      </tr>`;
    }).join("");
  }

  if (method) {
    method.textContent =
      "Statistics are calculated from finite parameter-map voxels within each configured ROI. "
      + "SD uses the population definition. CoV is SD divided by the absolute arithmetic mean. "
      + "CoV is shown as a percentage in this table but stored as a ratio in exports.";
  }
}
