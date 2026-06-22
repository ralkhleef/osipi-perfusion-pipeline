const API = window.location.origin;

// ── State ─────────────────────────────────────────────────────────────────────
//
// All mutable state lives here. Never read/write the old scattered `let` vars.

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

// ── Map type options per challenge ────────────────────────────────────────────

const MAP_OPTIONS = {
  asl:   ["CBF", "ATT", "Other"],
  dce:   ["Ktrans", "Kep", "Vp", "Other"],
  dsc:   ["CBF", "CBV", "MTT", "Other"],
  other: ["CBF", "ATT", "Ktrans", "Kep", "Vp", "CBV", "MTT", "Other"],
};

// ── Cached screen references (avoids querySelectorAll on every transition) ────

const submissionScreen    = document.getElementById("submission-screen");
const resultsScreen       = document.getElementById("results-screen");
const batchScreen         = document.getElementById("batch-screen");
const batchResultsScreen  = document.getElementById("batch-results-screen");

// ── Tiny helpers ──────────────────────────────────────────────────────────────

function el(id) { return document.getElementById(id); }

function getRadio(name) {
  const checked = document.querySelector(`input[name="${name}"]:checked`);
  return checked ? checked.value : null;
}

function setLoading(btn, loading, label) {
  if (!btn) return;
  btn.disabled = loading;
  btn.innerHTML = loading ? `<span class="spinner"></span>${label}…` : label;
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

/** Escape a string for safe insertion into HTML via innerHTML. */
function escapeHtml(str) {
  if (str == null) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#x27;");
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

  // Zero-byte NIfTI must be checked BEFORE the generic "no NIfTI" branch —
  // "NIfTI file appears to be empty" would otherwise match "nifti file appears".
  if (lower.includes("nifti file appears") || lower.includes("appears to be empty") ||
      lower.includes("zero-byte") || lower.includes("empty nifti"))
    return "Zero-byte NIfTI file detected";

  if (lower.includes("no .nii") || lower.includes("no nifti") || lower.includes("missing nifti"))
    return "No NIfTI files found";

  // EXPECTED_MAP_MISSING — keep the original message intact so the specific map
  // name (e.g. "KTRANS") is visible.  Match: "Expected ... parameter map was not found."
  if (lower.includes("expected") && lower.includes("parameter map") &&
      lower.includes("not found"))
    return text.length > 90 ? text.slice(0, 87) + "…" : text;

  if (lower.includes("readme") || lower.includes("sop"))
    return "README / SOP file missing";

  // MAP_TYPE_UNDETECTED / MAP_TYPE_MIXED — only match when the message is
  // explicitly about detection failure, NOT about a missing expected map.
  if (lower.includes("map type could not") || lower.includes("could not auto-detect") ||
      lower.includes("multiple parameter map types") ||
      (lower.includes("map type") && (lower.includes("auto") || lower.includes("undetect"))))
    return "Parameter map type could not be determined";

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

// ── Batch state ───────────────────────────────────────────────────────────────

const batchState = {
  uploadData:      null,   // full /api/upload-batch response
  selectedIds:     new Set(),
  batchId:         null,   // returned by /api/validate-batch
  validationData:  null,   // full /api/validate-batch response
};

// ── Docker availability ───────────────────────────────────────────────────────
// Cached after the first check; null = not yet checked.

const dockerStatus = { available: null, version: "", message: "" };

async function checkDockerAvailability() {
  if (dockerStatus.available !== null) return dockerStatus;  // use cache
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

// ── Screen switching ──────────────────────────────────────────────────────────

function showScreen(screenId) {
  if (submissionScreen)   submissionScreen.hidden   = (screenId !== "submission-screen");
  if (resultsScreen)      resultsScreen.hidden      = (screenId !== "results-screen");
  if (batchScreen)        batchScreen.hidden        = (screenId !== "batch-screen");
  if (batchResultsScreen) batchResultsScreen.hidden = (screenId !== "batch-results-screen");
  window.scrollTo({ top: 0, behavior: "instant" });
}

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
  return "Upload and Validate";
}

// Always re-enables the button and sets the correct label.
// Safe to call from action handlers regardless of previous button state.
function syncSubmitLabel() {
  setLoading(el("submit-btn"), false, submitLabel());
}

// ── State reset helpers ───────────────────────────────────────────────────────

// Clear submission-specific data; leave metadata fields (team, email, challenge, map type) intact.
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

// Full reset — clears everything including metadata fields.
function resetAll() {
  clearSubmissionData();
  state.mode          = "new";
  state.selectedMapType = null;

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

// ── Parameter Map Type pills ──────────────────────────────────────────────────

function updateMapTypePills(challengeType) {
  const container = el("map-type-pills");
  if (!container) return;

  const options = MAP_OPTIONS[challengeType] || MAP_OPTIONS.other;

  // Deselect if current selection isn't valid for the new challenge type
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

  // Toggle off if already selected
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
}

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
  // Skip file/URL check in edit mode — existing submission is reused
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
    // Always use the batch endpoint — it auto-detects single vs. multi-submission
    const fd = new FormData();
    fd.append("file", files[0]);
    const res  = await fetch(`${API}/api/upload-batch`, { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Upload failed.");
    return data;
  }

  // Folder / multi-file upload — use the batch-aware folder endpoint
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

// ── Validate ──────────────────────────────────────────────────────────────────

async function runValidation() {
  const mapType = getMapType();
  const payload = {
    submission_id:  state.submissionId,
    challenge_type: getChallengeType(),
    team_name:      getTeamName()  || null,
    contact_email:  getEmail()     || null,
    map_type:       mapType,
    map_type_mode:  getMapTypeMode(),
    notes:          null,
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

// ── Submit handler ────────────────────────────────────────────────────────────

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

const submitBtn = el("submit-btn");
if (submitBtn) submitBtn.addEventListener("click", handleSubmit);

async function handleSubmit() {
  if (requestInProgress) return;
  if (!validateForm()) return;

  requestInProgress = true;
  const btn = el("submit-btn");
  clearSubmitStatus();

  // ── Edit mode: reuse existing submission, only re-validate ────────────────
  if (state.mode === "edit" && state.submissionId) {
    setLoading(btn, true, "Saving & Revalidating");
    try {
      state.validationResult = await runValidation();
      showResults(state.validationResult);
    } catch (err) {
      showSubmitStatus("error", err.message || "Validation failed.");
    } finally {
      requestInProgress = false;
      // Always re-enable the button with the correct label, even after showResults.
      // The button is on the submission screen (now hidden), so this is invisible
      // but ensures it's ready when the user returns via Edit Details.
      setLoading(btn, false, submitLabel());
    }
    return;
  }

  // ── Upload + validate (new or replace) ───────────────────────────────────
  setLoading(btn, true, "Uploading");
  try {
    const source = getSourceType();
    showSubmitStatus("info", "Uploading submission…");
    let importData;
    if      (source === "local")   importData = await uploadLocalFiles();
    else if (source === "zenodo") { showSubmitStatus("info", "Importing from Zenodo…");  importData = await importZenodo();  }
    else if (source === "github") { showSubmitStatus("info", "Importing from GitHub…");  importData = await importGithub();  }

    state.pendingLocalFiles = null;  // Free memory — upload complete

    // ── Batch detected — hand off to batch dashboard ──────────────────────
    if (importData.batch === true) {
      clearSubmitStatus();
      requestInProgress = false;
      setLoading(btn, false, submitLabel());
      showBatchDashboard(importData);
      return;
    }

    // ── Single submission — continue with normal validation flow ──────────
    state.submissionId = importData.submission_id;
    state.detection = {
      nifti_count: Number.isFinite(Number(importData.nifti_count))
        ? Number(importData.nifti_count) : null,
      detected_parameter_map_type: importData.detected_parameter_map_type || "Unknown",
    };

    if (btn) btn.innerHTML = `<span class="spinner"></span>Validating…`;
    showSubmitStatus("info", "Running validation…");

    state.validationResult = await runValidation();
    clearSubmitStatus();
    showResults(state.validationResult);
  } catch (err) {
    showSubmitStatus("error", err.message || "Upload or validation failed. Is the server running?");
  } finally {
    requestInProgress = false;
    // Always re-enable the button. On success the user is on the results screen
    // and won't see this, but the button must be ready when they return to the form.
    setLoading(btn, false, submitLabel());
  }
}

// ── Render results ────────────────────────────────────────────────────────────

function showResults(data) {
  // Do NOT touch state.mode here — it is managed exclusively by the action buttons.

  // If nifti_count > 0, suppress any spurious "No NIfTI files found" that
  // can appear when a zero-byte NIfTI warning is mis-simplified.
  const _niftiCount = Number(data.nifti_count ?? 0);
  const _noNiftiLabel = "No NIfTI files found";
  const _filterSpurious = (msgs) =>
    _niftiCount > 0 ? msgs.filter((m) => m !== _noNiftiLabel) : msgs;

  const errors   = _filterSpurious(dedupeMessages((data.errors   || []).map(simplifyMessage)));
  const warnings = _filterSpurious(dedupeMessages((data.warnings || []).map(simplifyMessage)))
    .filter((w) => !errors.some((e) => e.toLowerCase() === w.toLowerCase()));

  const errCount  = errors.length;
  const warnCount = warnings.length;

  const banner = el("result-banner");
  if (banner) {
    banner.className = "result-banner";
    const icon  = el("banner-icon");
    const title = el("banner-title");
    const sub   = el("banner-sub");

    if (errCount > 0) {
      banner.classList.add("banner-fail");
      if (icon)  icon.textContent  = "✕";
      if (title) title.textContent = "Changes required";
      if (sub)   sub.textContent   = `${errCount} blocking error${errCount !== 1 ? "s" : ""} must be resolved before scoring.`;
    } else if (warnCount > 0) {
      banner.classList.add("banner-warn");
      if (icon)  icon.textContent  = "!";
      if (title) title.textContent = "Passed with warnings";
      if (sub)   sub.textContent   = `${warnCount} warning${warnCount !== 1 ? "s" : ""} — submission accepted but review recommended.`;
    } else {
      banner.classList.add("banner-pass");
      if (icon)  icon.textContent  = "✓";
      if (title) title.textContent = "Ready for scoring";
      if (sub)   sub.textContent   = "All validation checks passed.";
    }
  }

  const mapTypeDisplay = data.map_type || state.detection.detected_parameter_map_type || "Unknown";
  const niftiCount     = data.nifti_count ?? state.detection.nifti_count ?? "—";
  const summaryItems   = [
    ["Team",           data.team_name     || getTeamName() || "—"],
    ["Email",          data.contact_email || getEmail()    || "—"],
    ["Challenge Type", (data.challenge_type || getChallengeType() || "—").toUpperCase()],
    ["Parameter Map",  mapTypeDisplay],
    ["NIfTI Files",    `${niftiCount} detected`],
    ["Validated At",   formatDate(data.validated_at || data.checked_at) || "—"],
  ];

  const grid = el("summary-grid");
  if (grid) {
    grid.innerHTML = "";
    summaryItems.forEach(([key, val]) => {
      const dt = document.createElement("dt"); dt.textContent = key;
      const dd = document.createElement("dd"); dd.textContent = val;
      grid.append(dt, dd);
    });
  }

  renderIssueSection("errors-section",   "errors-list",   errors);
  renderIssueSection("warnings-section", "warnings-list", warnings);
  renderIssueSection("checks-section",   "checks-list",   buildSuccessChecks(data, errCount, warnCount));

  const files = data.files || data.file_list || [];
  const invDetails = el("inventory-details");
  if (invDetails) {
    if (files.length) {
      invDetails.style.display = "block";
      const countEl = el("inventory-count");
      if (countEl) countEl.textContent = `(${files.length} file${files.length !== 1 ? "s" : ""})`;
      const ul = el("inventory-list");
      if (ul) {
        ul.innerHTML = "";
        files.forEach((f) => {
          const li = document.createElement("li");
          li.textContent = typeof f === "string" ? f : (f.path || f.name || JSON.stringify(f));
          ul.appendChild(li);
        });
      }
    } else {
      invDetails.style.display = "none";
    }
  }

  // Store submission context for the execute section.
  window._currentSubmissionId  = data.submission_id || null;
  window._currentChallengeType = data.challenge_type || getChallengeType() || "dce";
  // Only show execute section when there are no blocking errors.
  if (typeof window._showExecuteSection === "function") window._showExecuteSection(errCount);

  showScreen("results-screen");
}

function renderIssueSection(sectionId, listId, items) {
  const section = el(sectionId);
  const list    = el(listId);
  if (!section) return;
  if (!items || !items.length) { section.style.display = "none"; return; }
  section.style.display = "block";
  if (list) {
    list.innerHTML = "";
    items.forEach((msg) => {
      const li = document.createElement("li"); li.textContent = msg; list.appendChild(li);
    });
  }
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

// ── Action buttons (attached once at startup) ─────────────────────────────────

// Edit Details: return to form preserving all metadata + current submission ID
const editBtn = el("edit-btn");
if (editBtn) {
  editBtn.addEventListener("click", () => {
    state.mode = "edit";
    clearSubmitStatus();
    syncSubmitLabel();   // re-enables button + shows "Save Changes and Revalidate"
    showScreen("submission-screen");
  });
}

// Replace Submission: keep metadata, discard files + submission ID
const replaceBtn = el("replace-btn");
if (replaceBtn) {
  replaceBtn.addEventListener("click", () => {
    state.mode = "replace";
    clearSubmissionData();   // clears submissionId, files, URL inputs, file label
    const localRadio = document.querySelector("input[name='submission_type'][value='local']");
    if (localRadio) localRadio.checked = true;
    switchSource("local");
    syncSubmitLabel();   // re-enables button + shows "Upload and Validate"
    showScreen("submission-screen");
  });
}

// Export Validation CSV
const downloadBtn = el("download-btn");
if (downloadBtn) {
  downloadBtn.addEventListener("click", async () => {
    const statusEl = el("download-status");
    if (!state.submissionId) {
      if (statusEl) {
        statusEl.style.display = "block";
        statusEl.className = "submit-status status-error";
        statusEl.textContent = "No submission to export.";
      }
      return;
    }
    if (requestInProgress) return;
    requestInProgress = true;
    setLoading(downloadBtn, true, "Export Validation CSV");
    if (statusEl) statusEl.style.display = "none";
    try {
      const url = `${API}/api/export-validation?submission_id=${encodeURIComponent(state.submissionId)}&format=csv`;
      const res = await fetch(url);
      if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
      const blob = await res.blob();
      triggerDownload(blob, `osipi-validation-${state.submissionId}.csv`);
    } catch (err) {
      if (statusEl) {
        statusEl.style.display = "block";
        statusEl.className = "submit-status status-error";
        statusEl.textContent = err.message || "Could not export CSV.";
      }
    } finally {
      requestInProgress = false;
      setLoading(downloadBtn, false, "Export Validation CSV");
    }
  });
}

// Start New Submission: clear everything
const newBtn = el("new-btn");
if (newBtn) {
  newBtn.addEventListener("click", () => {
    resetAll();
    syncSubmitLabel();   // re-enables button + shows "Upload and Validate"
    showScreen("submission-screen");
  });
}

// ── Docker Execution — shared render helpers ──────────────────────────────────

function _execLogBlock(label, text) {
  if (!text || !text.trim()) return "";
  return `<details style="margin-top:8px">
    <summary style="cursor:pointer;font-weight:500;font-size:13px">${escapeHtml(label)}</summary>
    <pre style="max-height:240px;overflow:auto;background:var(--bg-muted,#f5f5f5);padding:10px;border-radius:6px;font-size:12px;white-space:pre-wrap;margin-top:6px">${escapeHtml(text.slice(0, 4096))}</pre>
  </details>`;
}

function renderExecResult(data) {
  const passed      = data.passed;
  const timedOut    = data.timed_out;
  const buildFailed = data.build_failed;
  const badgeText   = timedOut    ? "⏱ Timed Out"
                    : buildFailed ? "✗ Build Failed"
                    : passed      ? "✓ Passed"
                                  : "✗ Failed";
  const badgeCls    = (timedOut || buildFailed || !passed) ? "badge-fail" : "badge-pass";
  const outputFiles = Array.isArray(data.output_files) ? data.output_files : [];
  const fileCount   = data.output_file_count ?? outputFiles.length;
  const filesHtml   = outputFiles.length
    ? `<ul style="margin:4px 0 0 16px;padding:0">${
        outputFiles.map((f) => `<li><code>${escapeHtml(f)}</code></li>`).join("")
      }</ul>`
    : `<em style="color:var(--text-muted)">None</em>`;
  return `
    <div style="border:1px solid var(--divider);border-radius:6px;padding:12px 16px;background:var(--bg-card,#fff);font-size:13px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
        <span class="${escapeHtml(badgeCls)}" style="font-weight:600;padding:2px 10px;border-radius:20px;font-size:12px">${badgeText}</span>
        <span style="color:var(--text-muted)">Exit: <code>${data.exit_code ?? "—"}</code></span>
      </div>
      <div style="line-height:1.8">
        <strong>Image:</strong> <code>${escapeHtml(data.image_name || "—")}</code><br>
        <strong>Command:</strong> <code>${escapeHtml(data.command || "—")}</code><br>
        <strong>Output files (${fileCount}):</strong> ${filesHtml}
      </div>
      ${_execLogBlock("stdout", data.stdout_preview)}${_execLogBlock("stderr", data.stderr_preview)}
    </div>`;
}

function renderExecError(msg) {
  return `<div style="color:var(--error,#c00);font-size:13px;padding:6px 0">${escapeHtml(msg)}</div>`;
}

// ── Single-submission execute section ─────────────────────────────────────────

(function initExecuteSection() {
  const executeBtn     = el("execute-btn");
  const executeStatus  = el("execute-status");
  const executeResult  = el("execute-result");
  const executeSection = el("execute-section");

  // Show the execute section only when validation passed (errCount === 0).
  // Called from showResults() after rendering the banner.
  window._showExecuteSection = function (errCount) {
    if (!executeSection) return;
    if (errCount > 0) {
      executeSection.style.display = "none";
      if (executeResult) { executeResult.style.display = "none"; executeResult.innerHTML = ""; }
      if (executeStatus) executeStatus.style.display = "none";
    } else {
      executeSection.style.display = "";
      if (executeResult) { executeResult.style.display = "none"; executeResult.innerHTML = ""; }
      if (executeStatus) executeStatus.style.display = "none";
    }
  };

  if (!executeBtn) return;

  executeBtn.addEventListener("click", async () => {
    const submissionId  = window._currentSubmissionId;
    const challengeType = window._currentChallengeType || "dce";
    if (!submissionId) return;

    const timeoutSeconds = parseInt(el("execute-timeout")?.value || "300", 10) || 300;

    executeBtn.disabled = true;
    if (executeStatus) {
      executeStatus.style.display = "";
      executeStatus.className = "submit-status";
      executeStatus.textContent = "Building Docker image and running submission… this may take a few minutes.";
    }
    if (executeResult) { executeResult.style.display = "none"; executeResult.innerHTML = ""; }

    try {
      const resp = await fetch("/api/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ submission_id: submissionId, challenge_type: challengeType, timeout_seconds: timeoutSeconds }),
      });
      const data = await resp.json();

      // Pre-flight errors (no Dockerfile, bad path, Docker missing) → 4xx
      if (!resp.ok || !data.success) {
        if (executeStatus) {
          executeStatus.className = "submit-status status-error";
          executeStatus.textContent = (data && (data.detail || data.message)) || "Docker execution failed.";
        }
        return;
      }

      if (executeStatus) executeStatus.style.display = "none";
      if (executeResult) {
        executeResult.style.display = "";
        executeResult.innerHTML = renderExecResult(data);
      }
    } catch (err) {
      if (executeStatus) {
        executeStatus.className = "submit-status status-error";
        executeStatus.textContent = "Network error: " + err.message;
      }
    } finally {
      executeBtn.disabled = false;
    }
  });
})();

// ── Batch execution — per-card and Execute All Passed ─────────────────────────

async function runBatchExec(btn, section) {
  const subId     = section.dataset.subId;
  const challenge = section.dataset.challenge || "dce";
  const timeout   = parseInt(el("batch-exec-timeout")?.value || "300", 10) || 300;

  // The exec-status badge lives in the parent exec-card <summary>
  const card      = section.closest("details.exec-card");
  const execBadge = card ? card.querySelector(".exec-status-badge") : null;
  const resultDiv = section.querySelector(".batch-exec-result");

  btn.disabled = true;
  if (card) card.open = true;   // auto-open so progress is visible
  if (execBadge) {
    execBadge.className = "batch-badge badge-exec-run exec-status-badge";
    execBadge.textContent = "Running…";
  }
  if (resultDiv) resultDiv.style.display = "none";

  try {
    const resp = await fetch("/api/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ submission_id: subId, challenge_type: challenge, timeout_seconds: timeout }),
    });
    const data = await resp.json();

    if (!resp.ok || !data.success) {
      if (execBadge) {
        execBadge.className = "batch-badge badge-exec-fail exec-status-badge";
        execBadge.textContent = "✗ Failed";
      }
      if (resultDiv) {
        resultDiv.style.display = "";
        resultDiv.innerHTML = renderExecError((data && (data.detail || data.message)) || "Execution failed.");
      }
    } else {
      const passed   = data.passed;
      const timedOut = data.timed_out;
      if (execBadge) {
        if (timedOut) {
          execBadge.className = "batch-badge badge-exec-warn exec-status-badge";
          execBadge.textContent = "⏱ Timed out";
        } else if (passed) {
          execBadge.className = "batch-badge badge-exec-pass exec-status-badge";
          execBadge.textContent = "✓ Passed";
        } else {
          execBadge.className = "batch-badge badge-exec-fail exec-status-badge";
          execBadge.textContent = "✗ Failed";
        }
      }
      if (resultDiv) {
        resultDiv.style.display = "";
        resultDiv.innerHTML = renderExecResult(data);
      }
    }
  } catch (err) {
    if (execBadge) {
      execBadge.className = "batch-badge badge-exec-fail exec-status-badge";
      execBadge.textContent = "✗ Error";
    }
    if (resultDiv) {
      resultDiv.style.display = "";
      resultDiv.innerHTML = renderExecError("Network error: " + err.message);
    }
  } finally {
    btn.disabled = false;
  }
}

// Event delegation: clicks on any .batch-exec-btn inside the exec cards panel
(function initBatchExecDelegation() {
  // Listen on the whole document so it works regardless of when exec cards are injected
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".batch-exec-btn");
    if (!btn) return;
    const section = btn.closest(".batch-exec-section");
    if (!section) return;
    e.stopPropagation();
    await runBatchExec(btn, section);
  });
})();

// "Execute All Passed" button
(function initBatchExecAll() {
  const btn      = el("batch-exec-all-btn");
  const statusEl = el("batch-exec-status");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    // Only run sections that have an execute button (has Dockerfile) and Docker available
    const sections = [...document.querySelectorAll("#batch-exec-cards .batch-exec-section")]
      .filter((s) => s.querySelector(".batch-exec-btn"));
    if (!sections.length) return;
    btn.disabled = true;
    if (statusEl) { statusEl.style.display = ""; statusEl.textContent = `Starting ${sections.length} execution(s)…`; }
    let done = 0;
    for (const section of sections) {
      const execBtn = section.querySelector(".batch-exec-btn");
      if (!execBtn) continue;
      if (statusEl) statusEl.textContent = `Executing ${done + 1} of ${sections.length}…`;
      await runBatchExec(execBtn, section);
      done++;
    }
    if (statusEl) statusEl.textContent = `Done — ran ${done} submission(s).`;
    btn.disabled = false;
  });
})();

// ── Batch Dashboard ───────────────────────────────────────────────────────────

function showBatchDashboard(uploadData) {
  batchState.uploadData     = uploadData;
  batchState.selectedIds    = new Set(uploadData.submissions.map((s) => s.submission_id));
  batchState.batchId        = null;
  batchState.validationData = null;

  const desc = el("batch-header-desc");
  if (desc) {
    const sourceLabel = uploadData.source_type && uploadData.source_type !== "local"
      ? `${uploadData.source_type.charAt(0).toUpperCase() + uploadData.source_type.slice(1)}: `
      : "";
    const name  = uploadData.original_filename || "upload";
    const count = uploadData.submission_count;
    desc.textContent = `${sourceLabel}"${name}" contains ${count} submission${count !== 1 ? "s" : ""}.`;
  }

  renderBatchTable(uploadData.submissions);
  showScreen("batch-screen");
}

function renderBatchTable(submissions) {
  const wrap = el("batch-table-wrap");
  if (!wrap) return;

  const table = document.createElement("table");
  table.className = "batch-table";

  table.innerHTML = `
    <thead>
      <tr>
        <th class="td-check"><input type="checkbox" id="batch-check-all" title="Toggle all" /></th>
        <th>Source Folder</th>
        <th>Submission ID</th>
        <th>NIfTI</th>
        <th>Detected Map</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody id="batch-tbody"></tbody>
  `;
  wrap.innerHTML = "";
  wrap.appendChild(table);

  const tbody = table.querySelector("#batch-tbody");

  submissions.forEach((sub) => {
    const isSelected = batchState.selectedIds.has(sub.submission_id);
    const tr = document.createElement("tr");
    if (isSelected) tr.classList.add("selected-row");

    // Escape all server-supplied strings before innerHTML insertion (XSS prevention)
    const safeMapLabel  = escapeHtml(sub.detected_parameter_map_type || "Unknown");
    const safeWarning   = escapeHtml(sub.detection_warning || "");
    const safeFolder    = escapeHtml(sub.source_folder || "—");
    const safeSubId     = escapeHtml(sub.submission_id);
    const mapCell = safeWarning
      ? `${safeMapLabel}<div class="batch-warn-text">${safeWarning}</div>`
      : safeMapLabel;

    const statusBadge = sub.status === "passed"
      ? `<span class="batch-badge badge-pass">Passed</span>`
      : sub.status === "failed"
        ? `<span class="batch-badge badge-fail">Failed</span>`
        : `<span class="batch-badge badge-ready">Ready</span>`;

    // submission_id is guaranteed alphanumeric+hyphens/underscores by _safe_id
    // so it's safe to use directly in data- attributes and title without escaping.
    tr.innerHTML = `
      <td class="td-check">
        <input type="checkbox" data-id="${sub.submission_id}" ${isSelected ? "checked" : ""} />
      </td>
      <td class="td-folder">${safeFolder}</td>
      <td class="td-id" title="${sub.submission_id}">${safeSubId}</td>
      <td>${sub.nifti_count ?? "—"}</td>
      <td>${mapCell}</td>
      <td>${statusBadge}</td>
    `;

    const cb = tr.querySelector(`input[data-id="${sub.submission_id}"]`);
    cb.addEventListener("change", () => {
      if (cb.checked) {
        batchState.selectedIds.add(sub.submission_id);
        tr.classList.add("selected-row");
      } else {
        batchState.selectedIds.delete(sub.submission_id);
        tr.classList.remove("selected-row");
      }
      syncBatchHeaderCheckbox();
      syncBatchValidateBtn();
    });

    tbody.appendChild(tr);
  });

  // Header checkbox
  const checkAll = table.querySelector("#batch-check-all");
  checkAll.checked = batchState.selectedIds.size === submissions.length;
  checkAll.addEventListener("change", () => {
    if (checkAll.checked) {
      submissions.forEach((s) => batchState.selectedIds.add(s.submission_id));
    } else {
      batchState.selectedIds.clear();
    }
    tbody.querySelectorAll("input[data-id]").forEach((cb) => {
      cb.checked = batchState.selectedIds.has(cb.dataset.id);
      cb.closest("tr").classList.toggle("selected-row", cb.checked);
    });
    syncBatchValidateBtn();
  });

  syncBatchValidateBtn();
}

function syncBatchHeaderCheckbox() {
  const checkAll = document.querySelector("#batch-check-all");
  if (!checkAll || !batchState.uploadData) return;
  checkAll.checked = batchState.selectedIds.size === batchState.uploadData.submissions.length;
}

function syncBatchValidateBtn() {
  const btn = el("batch-validate-selected-btn");
  if (!btn) return;
  btn.disabled = batchState.selectedIds.size === 0;
}

// Batch controls
const batchSelectAllBtn    = el("batch-select-all-btn");
const batchDeselectAllBtn  = el("batch-deselect-all-btn");
const batchValSelBtn       = el("batch-validate-selected-btn");
const batchValAllBtn       = el("batch-validate-all-btn");

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
  batchNewBtn.addEventListener("click", () => { resetAll(); syncSubmitLabel(); showScreen("submission-screen"); });
}

async function runBatchValidation(submissionIds) {
  if (!submissionIds.length) return;
  const statusEl = el("batch-validate-status");

  const disableBtns = (v) => {
    [batchValSelBtn, batchValAllBtn, batchSelectAllBtn, batchDeselectAllBtn].forEach((b) => { if (b) b.disabled = v; });
  };
  disableBtns(true);
  if (statusEl) {
    statusEl.style.display = "block";
    statusEl.className = "submit-status status-info";
    statusEl.textContent = `Validating ${submissionIds.length} submission${submissionIds.length !== 1 ? "s" : ""}…`;
  }

  try {
    // Send the shared team name/email from the form as per-submission metadata
    // so the unblinded CSV export contains meaningful values.
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
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || "Batch validation failed.");

    batchState.batchId        = data.batch_id;
    batchState.validationData = data;

    // Update submission statuses in table
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
    showBatchResults(data);
  } catch (err) {
    if (statusEl) {
      statusEl.style.display = "block";
      statusEl.className = "submit-status status-error";
      statusEl.textContent = err.message || "Batch validation failed.";
    }
  } finally {
    disableBtns(false);
    syncBatchValidateBtn();
  }
}

// ── Batch Results ─────────────────────────────────────────────────────────────

async function showBatchResults(data) {
  const results = data.results || [];

  // ── 1. Header desc + overall banner ──────────────────────────────────────
  const desc = el("batch-results-desc");
  if (desc) {
    const validated = results.length;
    desc.textContent = `${validated} submission${validated !== 1 ? "s" : ""} validated — ${data.passed_count} passed, ${data.failed_count} failed.`;
  }

  const banner = el("batch-result-banner");
  if (banner) {
    banner.className = "result-banner";
    const allPassed = data.failed_count === 0;
    const anyPassed = data.passed_count > 0;
    banner.classList.add(allPassed ? "banner-pass" : anyPassed ? "banner-warn" : "banner-fail");
    const icon  = el("batch-banner-icon");
    const title = el("batch-banner-title");
    const sub   = el("batch-banner-sub");
    if (icon)  icon.textContent  = allPassed ? "✓" : anyPassed ? "!" : "✕";
    if (title) title.textContent = allPassed ? "All submissions passed"
                                 : anyPassed ? "Partial pass"
                                             : "All submissions failed";
    if (sub) {
      const totalWarn = results.reduce((acc, r) => acc + (r.warnings || []).length, 0);
      sub.textContent = `${results.length} validated · ${data.passed_count} passed · ${data.failed_count} failed · ${totalWarn} warning${totalWarn !== 1 ? "s" : ""}`;
    }
  }

  // ── 2. Collapse the exec details section (reset between runs) ────────────
  const execDetails = el("batch-exec-details");
  if (execDetails) execDetails.open = false;

  // ── 3. Stats chips ────────────────────────────────────────────────────────
  const statsRow = el("batch-stats-row");
  if (statsRow) {
    statsRow.innerHTML = "";
    const totalWarn = results.reduce((acc, r) => acc + (r.warnings || []).length, 0);
    [
      { num: results.length,    lbl: "Total",    cls: "" },
      { num: data.passed_count, lbl: "Passed",   cls: "chip-pass" },
      { num: data.failed_count, lbl: "Failed",   cls: data.failed_count > 0 ? "chip-fail" : "" },
      { num: totalWarn,         lbl: "Warnings", cls: totalWarn > 0 ? "chip-warn" : "" },
    ].forEach(({ num, lbl, cls }) => {
      const chip = document.createElement("div");
      chip.className = `batch-stat-chip${cls ? " " + cls : ""}`;
      chip.innerHTML = `<span class="stat-num">${num}</span><span class="stat-lbl">${lbl}</span>`;
      statsRow.appendChild(chip);
    });
    if (data.batch_id) {
      const id = document.createElement("div");
      id.className = "batch-stat-meta";
      id.innerHTML = `<span style="font-weight:700">Batch</span><code style="font-size:0.71rem;font-family:monospace">${escapeHtml(data.batch_id)}</code>`;
      statsRow.appendChild(id);
    }
    if (data.validated_at) {
      const dt = document.createElement("div");
      dt.className = "batch-stat-meta";
      dt.innerHTML = `<span style="font-weight:700">At</span><span>${formatDate(data.validated_at)}</span>`;
      statsRow.appendChild(dt);
    }
  }

  // ── 4. Validation cards — all collapsed by default ────────────────────────
  const list = el("batch-submissions-list");
  if (list) {
    list.innerHTML = "";
    results.forEach((r) => {
      const rNiftiCount = Number(r.nifti_count ?? 0);
      const _filterSpur = (msgs) =>
        rNiftiCount > 0 ? msgs.filter((m) => m !== "No NIfTI files found") : msgs;

      const errors   = _filterSpur(dedupeMessages((r.errors   || []).map(simplifyMessage)));
      const warnings = _filterSpur(dedupeMessages((r.warnings || []).map(simplifyMessage)))
        .filter((w) => !errors.some((e) => e.toLowerCase() === w.toLowerCase()));
      const checks   = buildSuccessChecks(r, errors.length, warnings.length, r.map_type);
      const passed   = r.passed;
      const hasWarn  = warnings.length > 0;

      // Status badge
      let statusText, statusCls;
      if (!passed)      { statusText = "Failed";               statusCls = "badge-fail"; }
      else if (hasWarn) { statusText = "Passed with warnings"; statusCls = "badge-warn"; }
      else              { statusText = "Passed";               statusCls = "badge-pass"; }

      // Mini count chips shown in the collapsed header
      const errChip  = errors.length > 0
        ? `<span class="batch-mini-chip batch-mini-error">${errors.length} error${errors.length !== 1 ? "s" : ""}</span>` : "";
      const warnChip = warnings.length > 0
        ? `<span class="batch-mini-chip batch-mini-warn">${warnings.length} warning${warnings.length !== 1 ? "s" : ""}</span>` : "";
      const niftiChip = `<span class="batch-mini-chip">${r.nifti_count ?? "—"} NIfTI</span>`;

      const issuesHtml = [
        ...errors.map((m)   => `<li class="is-error">✕ ${escapeHtml(m)}</li>`),
        ...warnings.map((m) => `<li class="is-warning">! ${escapeHtml(m)}</li>`),
        ...checks.map((m)   => `<li class="is-pass">✓ ${escapeHtml(m)}</li>`),
      ].join("");

      const safeSubId   = escapeHtml(r.submission_id);
      const safeMapType = escapeHtml(r.map_type    || "—");
      const safeTeam    = r.team_name     ? escapeHtml(r.team_name)     : "";
      const safeFolder  = r.source_folder ? escapeHtml(r.source_folder) : "";

      const details = document.createElement("details");
      details.className = "batch-sub-card";
      // All cards start collapsed — user controls via Expand All / individual click
      details.innerHTML = `
        <summary class="batch-sub-header">
          <span class="batch-sub-toggle">▸</span>
          <span class="batch-sub-name" title="${safeSubId}">${safeSubId}</span>
          <div class="batch-sub-header-right">
            <span class="batch-badge ${escapeHtml(statusCls)}">${escapeHtml(statusText)}</span>
            ${niftiChip}${errChip}${warnChip}
          </div>
        </summary>
        <div class="batch-sub-body">
          <div class="batch-sub-meta">
            Total files: ${r.total_files ?? "—"}&nbsp;·&nbsp;Map type: ${safeMapType}
            ${safeFolder ? `&nbsp;·&nbsp;Source: ${safeFolder}` : ""}
            ${safeTeam   ? `&nbsp;·&nbsp;Team: ${safeTeam}`     : ""}
          </div>
          <ul class="batch-issue-list">${issuesHtml}</ul>
        </div>
      `;
      list.appendChild(details);
    });
  }

  // ── 5. Execution tab — Docker check + exec cards ──────────────────────────
  const docker       = await checkDockerAvailability();
  const dockerBanner = el("batch-docker-banner");
  const execControls = el("batch-exec-controls");
  const execStatus   = el("batch-exec-status");
  const execCards    = el("batch-exec-cards");

  if (execStatus) execStatus.style.display = "none";

  // Global Docker banner (always shown in execution tab)
  if (dockerBanner) {
    if (docker.available) {
      dockerBanner.className = "docker-avail-row docker-ok";
      dockerBanner.innerHTML = `<strong>&#x2713; Docker ${escapeHtml(docker.version)} available.</strong>&nbsp;Submissions with a Dockerfile can be executed.`;
    } else {
      dockerBanner.className = "docker-avail-row docker-err";
      dockerBanner.innerHTML = `<strong>&#x26A0; Docker unavailable.</strong>&nbsp;${escapeHtml(docker.message)} All execution buttons are disabled.`;
    }
    dockerBanner.style.display = "";
  }

  // Build exec cards — show ALL submissions so the user can see why each is or isn't executable
  if (execCards) {
    execCards.innerHTML = "";
    let executableCount = 0;

    results.forEach((r) => {
      const passed        = r.passed;
      const hasDockerfile = !!r.has_dockerfile;
      const canExec       = passed && hasDockerfile && docker.available;

      // Reason note for non-executable submissions
      let execNote = null;
      if (!passed)             execNote = "Validation failed — submission must pass before it can be executed.";
      else if (!hasDockerfile) execNote = "Not executable — no Dockerfile found in this submission.";
      else if (!docker.available) execNote = "Execution disabled — Docker is unavailable on this host.";

      if (canExec) executableCount++;

      const valBadgeCls = passed ? "badge-pass" : "badge-fail";
      const valBadgeTxt = passed ? "Validated ✓" : "Failed ✗";

      let execBadgeCls, execBadgeTxt;
      if (!passed || !hasDockerfile)  { execBadgeCls = "badge-exec-na";   execBadgeTxt = "Not executable"; }
      else if (!docker.available)     { execBadgeCls = "badge-exec-na";   execBadgeTxt = "Docker unavailable"; }
      else                            { execBadgeCls = "badge-exec-none"; execBadgeTxt = "Not run"; }

      const safeSubId = escapeHtml(r.submission_id);

      const card    = document.createElement("details");
      card.className = "exec-card";

      const summary = document.createElement("summary");
      summary.className = "exec-card-header";
      summary.innerHTML = `
        <span class="exec-toggle">&#x25B8;</span>
        <span class="exec-card-name" title="${safeSubId}">${safeSubId}</span>
        <div class="exec-card-badges">
          <span class="batch-badge ${escapeHtml(valBadgeCls)}">${escapeHtml(valBadgeTxt)}</span>
          <span class="batch-badge ${escapeHtml(execBadgeCls)} exec-status-badge">${escapeHtml(execBadgeTxt)}</span>
        </div>
      `;

      const body = document.createElement("div");
      body.className = "exec-card-body";

      if (execNote) {
        const note = document.createElement("p");
        note.style.cssText = "font-size:0.78rem;color:var(--muted);font-style:italic;margin:0";
        note.textContent = execNote;
        body.appendChild(note);
      } else {
        // Executable — build exec section (DOM, not innerHTML — subId safe via dataset)
        const execSection = document.createElement("div");
        execSection.className         = "batch-exec-section";
        execSection.dataset.subId     = r.submission_id;
        execSection.dataset.challenge = r.challenge_type || getChallengeType() || "dce";

        const btnRow = document.createElement("div");
        btnRow.style.cssText = "display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:6px";

        const execBtn = document.createElement("button");
        execBtn.type      = "button";
        execBtn.className = "btn-secondary btn-sm batch-exec-btn";
        execBtn.textContent = "Execute in Docker";

        btnRow.appendChild(execBtn);
        execSection.appendChild(btnRow);

        const resultDiv = document.createElement("div");
        resultDiv.className     = "batch-exec-result";
        resultDiv.style.display = "none";
        execSection.appendChild(resultDiv);

        body.appendChild(execSection);
      }

      card.appendChild(summary);
      card.appendChild(body);
      execCards.appendChild(card);
    });

    if (execControls)
      execControls.style.display = executableCount > 0 ? "flex" : "none";
  }

  showScreen("batch-results-screen");
}

// Batch result action buttons
const batchBackToBatchBtn   = el("batch-back-to-batch-btn");
const batchExportBlindedBtn = el("batch-export-blinded-btn");
const batchExportUnblindedBtn = el("batch-export-unblinded-btn");
const batchResultsNewBtn    = el("batch-results-new-btn");

if (batchBackToBatchBtn) {
  batchBackToBatchBtn.addEventListener("click", () => showScreen("batch-screen"));
}

if (batchResultsNewBtn) {
  batchResultsNewBtn.addEventListener("click", () => { resetAll(); syncSubmitLabel(); showScreen("submission-screen"); });
}

async function exportBatch(blinded) {
  const statusEl = el("batch-export-status");
  if (!batchState.batchId) {
    if (statusEl) {
      statusEl.style.display = "block";
      statusEl.className = "submit-status status-error";
      statusEl.textContent = "No batch to export. Run validation first.";
    }
    return;
  }
  const btn    = blinded ? batchExportBlindedBtn : batchExportUnblindedBtn;
  const label  = blinded ? "Export Blinded CSV" : "Export Unblinded CSV";
  if (btn) setLoading(btn, true, label);
  if (statusEl) statusEl.style.display = "none";
  try {
    const url = `${API}/api/export-batch?batch_id=${encodeURIComponent(batchState.batchId)}&blinded=${blinded}`;
    const res = await fetch(url);
    if (!res.ok) { const d = await res.json().catch(() => ({})); throw new Error(d.detail || "Export failed."); }
    const blob = await res.blob();
    const suffix = blinded ? "blinded" : "unblinded";
    triggerDownload(blob, `osipi-batch-${batchState.batchId}-${suffix}.csv`);
  } catch (err) {
    if (statusEl) {
      statusEl.style.display = "block";
      statusEl.className = "submit-status status-error";
      statusEl.textContent = err.message || "Export failed.";
    }
  } finally {
    if (btn) setLoading(btn, false, label);
  }
}

if (batchExportBlindedBtn)   batchExportBlindedBtn.addEventListener("click", () => exportBatch(true));
if (batchExportUnblindedBtn) batchExportUnblindedBtn.addEventListener("click", () => exportBatch(false));

// ── Expand / Collapse all validation cards ────────────────────────────────────

(function initExpandCollapseAll() {
  document.addEventListener("click", (e) => {
    if (e.target.closest("#batch-expand-all")) {
      document.querySelectorAll("#batch-submissions-list details.batch-sub-card")
        .forEach((d) => { d.open = true; });
    }
    if (e.target.closest("#batch-collapse-all")) {
      document.querySelectorAll("#batch-submissions-list details.batch-sub-card")
        .forEach((d) => { d.open = false; });
    }
  });
})();

// ── Initialise ────────────────────────────────────────────────────────────────

updateMapTypePills(getRadio("challenge_type") || "dce");
syncSubmitLabel();
