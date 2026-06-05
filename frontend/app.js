// OSIPI Pipeline — frontend logic
const API = window.location.origin;

// submission_id stored after a successful submission import.
// Never exposed to the user as a file path.
let currentSubmissionId = null;
let currentSubmissionStatus = "No submission imported yet.";
let currentSubmissionSource = "—";
let importedSubmissions = [];
let currentDetection = {
  nifti_count: null,
  detected_parameter_map_type: "Unknown",
  detected_map_type_confidence: "none",
  detection_warning: null,
};
let stepOneValidationAttempted = false;

// ── Step/panel mapping ───────────────────────────────────────────────────────

const PANELS = {
  1: "tab-intake",
  2: "tab-reference",
  3: "tab-validation",
  4: "tab-results",
  5: "tab-export",
};

// Navigate to a numbered step: update stepper UI and show the right panel.
function goToStep(num) {
  // Stepper appearance
  document.querySelectorAll(".step-item[data-step]").forEach((item) => {
    const n = parseInt(item.dataset.step, 10);
    item.classList.toggle("active", n === num);
    item.classList.toggle("done",   n < num);
  });

  // Panel visibility
  document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
  const panelId = PANELS[num];
  if (panelId) el(panelId).classList.add("active");

  // Step-specific side effects
  if (num === 3) refreshValidationPanel();
}

// Wire stepper clicks.
document.querySelectorAll(".step-item[data-step]").forEach((item) => {
  item.addEventListener("click", () => goToStep(parseInt(item.dataset.step, 10)));
});

// ── Utilities ────────────────────────────────────────────────────────────────

const el = (id) => document.getElementById(id);

function getRadio(name) {
  const checked = document.querySelector(`input[name="${name}"]:checked`);
  return checked ? checked.value : null;
}

function setLoading(btn, loading, label) {
  btn.disabled = loading;
  btn.innerHTML = loading
    ? `<span class="spinner"></span>${label}…`
    : label;
}

function formatDate(iso) {
  if (!iso) return "";
  try {
    return new Date(iso).toLocaleDateString("en-US", {
      year: "numeric", month: "short", day: "numeric",
    });
  } catch {
    return iso.slice(0, 10);
  }
}

function setFieldError(id, message) {
  const error = el(`${id}-error`);
  const field = el(id);
  if (error) error.textContent = message || "";
  if (field) field.classList.toggle("field-invalid", Boolean(message));
}

function setGroupError(id, message) {
  const error = el(`${id}-error`);
  if (error) error.textContent = message || "";
}

function clearStepOneErrors() {
  [
    "team-name",
    "contact-email",
    "challenge-type",
    "challenge-type-other",
    "map-type-other",
    "expected-maps",
    "include-code",
    "include-readme",
  ].forEach((id) => {
    setFieldError(id, "");
    setGroupError(id, "");
  });
}

function validateStepOne({ showErrors = true } = {}) {
  const errors = [];
  const addError = (id, message) => {
    errors.push(id);
    if (showErrors) {
      if (el(id)) setFieldError(id, message);
      else setGroupError(id, message);
    }
  };

  if (showErrors) clearStepOneErrors();

  if (!el("team-name").value.trim()) addError("team-name", "Team name is required.");
  if (!el("contact-email").value.trim()) addError("contact-email", "Contact email is required.");
  if (!getRadio("challenge_type")) addError("challenge-type", "Select a challenge type.");
  if (getRadio("challenge_type") === "other" && !el("challenge-type-other").value.trim()) {
    addError("challenge-type-other", "Enter challenge type.");
  }
  if (getMapTypeRaw() === "other" && !el("map-type-other").value.trim()) {
    addError("map-type-other", "Enter parameter map type.");
  }
  if (getExpectedMapsMode() === "manual" && !el("expected-maps").value.trim()) {
    addError("expected-maps", "Enter the expected number of maps.");
  }
  if (!getRadio("include_code")) addError("include-code", "Select yes or no.");
  if (!getRadio("include_readme")) addError("include-readme", "Select yes or no.");

  if (errors.length && showErrors) {
    const first = el(errors[0]) || el(`${errors[0]}-error`);
    if (first) first.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  return errors.length === 0;
}

// ── "Other" conditional fields ───────────────────────────────────────────────

// Show/hide the custom challenge-type text input when "Other" is selected.
document.querySelectorAll("input[name='challenge_type']").forEach((radio) => {
  radio.addEventListener("change", () => {
    el("challenge-type-other-wrap").style.display =
      getRadio("challenge_type") === "other" ? "flex" : "none";
    if (stepOneValidationAttempted) validateStepOne({ showErrors: true });
  });
});

// Show/hide the custom map-type text input when "Other" is selected.
document.querySelectorAll("input[name='map_type']").forEach((radio) => {
  radio.addEventListener("change", updateMapTypeMode);
});

document.querySelectorAll("input[name='expected_maps_mode']").forEach((radio) => {
  radio.addEventListener("change", updateExpectedMapsMode);
});

document.querySelectorAll("input[name='include_code'], input[name='include_readme']").forEach((radio) => {
  radio.addEventListener("change", () => {
    if (stepOneValidationAttempted) validateStepOne({ showErrors: true });
  });
});

["team-name", "contact-email", "challenge-type-other", "map-type-other", "expected-maps"].forEach((id) => {
  el(id).addEventListener("input", () => {
    if (stepOneValidationAttempted) validateStepOne({ showErrors: true });
  });
});

// Return the effective challenge type (custom text when "Other" is chosen).
function getChallengeType() {
  const raw = getRadio("challenge_type");
  if (raw === "other") {
    const custom = el("challenge-type-other").value.trim();
    return custom || "other";
  }
  return raw || "dce";
}

// Return the effective parameter map type.
function getMapType() {
  const raw = getMapTypeRaw();
  if (raw === "auto") {
    const detected = currentDetection.detected_parameter_map_type;
    return isDetectedMapTypeUsable(detected) ? detected : "";
  }
  if (raw === "other") {
    const custom = el("map-type-other").value.trim();
    return custom || "other";
  }
  return raw;
}

function getMapTypeRaw() {
  return getRadio("map_type") || "auto";
}

function getMapTypeDisplay() {
  const raw = getMapTypeRaw();
  if (raw === "auto") {
    const detected = currentDetection.detected_parameter_map_type;
    return isDetectedMapTypeUsable(detected) ? `Auto-detect (${detected})` : "Auto-detect";
  }
  if (raw === "other") {
    return el("map-type-other").value.trim() || "Other";
  }
  return raw || "Not selected";
}

function getExpectedMapsMode() {
  return getRadio("expected_maps_mode") || "auto";
}

function getExpectedNiftiCount() {
  if (getExpectedMapsMode() === "auto") {
    return Number.isInteger(currentDetection.nifti_count)
      ? currentDetection.nifti_count
      : null;
  }
  return parseInt(el("expected-maps").value, 10) || null;
}

function getExpectedMapsDisplay() {
  if (getExpectedMapsMode() === "auto") {
    return Number.isInteger(currentDetection.nifti_count)
      ? `Auto-detected: ${currentDetection.nifti_count}`
      : "Auto-detect";
  }
  return el("expected-maps").value || "—";
}

function isDetectedMapTypeUsable(value) {
  return ["CBF", "Ktrans", "ATT", "Mixed/Other"].includes(value);
}

function updateExpectedMapsMode() {
  const isManual = getExpectedMapsMode() === "manual";
  el("expected-maps").disabled = !isManual;
  el("expected-maps-wrap").style.display = isManual ? "flex" : "none";
  updateAutoDetectionHints();
  if (stepOneValidationAttempted) validateStepOne({ showErrors: true });
}

function updateMapTypeMode() {
  el("map-type-other-wrap").style.display =
    getMapTypeRaw() === "other" ? "flex" : "none";
  updateAutoDetectionHints();
  if (stepOneValidationAttempted) validateStepOne({ showErrors: true });
}

function setDetectionFromImport(data) {
  const detectedCount = Number(data.nifti_count);
  currentDetection = {
    nifti_count: Number.isFinite(detectedCount) ? detectedCount : null,
    detected_parameter_map_type: data.detected_parameter_map_type || "Unknown",
    detected_map_type_confidence: data.detected_map_type_confidence || "none",
    detection_warning: data.detection_warning || null,
  };
  updateAutoDetectionHints();
}

function resetDetection() {
  currentDetection = {
    nifti_count: null,
    detected_parameter_map_type: "Unknown",
    detected_map_type_confidence: "none",
    detection_warning: null,
  };
  updateAutoDetectionHints();
}

function updateAutoDetectionHints() {
  const expectedStatus = el("expected-maps-auto-status");
  if (expectedStatus) {
    if (getExpectedMapsMode() === "manual") {
      expectedStatus.textContent = "Manual expected count.";
      expectedStatus.className = "auto-detect-status muted";
    } else if (Number.isInteger(currentDetection.nifti_count)) {
      const n = currentDetection.nifti_count;
      expectedStatus.textContent = `Auto-detected: ${n} NIfTI file${n !== 1 ? "s" : ""}`;
      expectedStatus.className = "auto-detect-status success";
    } else {
      expectedStatus.textContent = "";
      expectedStatus.className = "auto-detect-status";
    }
  }

  const mapStatus = el("map-type-auto-status");
  if (mapStatus) {
    if (getMapTypeRaw() !== "auto") {
      mapStatus.textContent = "Manual map type selected.";
      mapStatus.className = "auto-detect-status muted";
    } else if (isDetectedMapTypeUsable(currentDetection.detected_parameter_map_type)) {
      mapStatus.textContent = `Auto-detected: ${currentDetection.detected_parameter_map_type}`;
      mapStatus.className = "auto-detect-status success";
    } else if (currentSubmissionId) {
      mapStatus.textContent = "Could not auto-detect. Please select manually.";
      mapStatus.className = "auto-detect-status warning";
    } else {
      mapStatus.textContent = "";
      mapStatus.className = "auto-detect-status";
    }
  }
}

// ── Step 2: Submission import ────────────────────────────────────────────────

const SOURCE_PANELS = {
  local: "source-local",
  zenodo: "source-zenodo",
  github: "source-github",
};

document.querySelectorAll("input[name='submission_type']").forEach((radio) => {
  radio.addEventListener("change", () => switchSubmissionSource(radio.value));
});

function getSubmissionType() {
  return getRadio("submission_type") || "local";
}

function switchSubmissionSource(type) {
  document.querySelectorAll(".submission-type-card").forEach((card) => {
    const input = card.querySelector("input[name='submission_type']");
    card.classList.toggle("active", input && input.value === type);
  });

  Object.entries(SOURCE_PANELS).forEach(([key, panelId]) => {
    el(panelId).classList.toggle("active", key === type);
  });
}

function updateImportContinue() {
  const btn = el("intake-continue-btn");
  btn.disabled = !currentSubmissionId;
}

function clearSubmissionState() {
  currentSubmissionId = null;
  currentSubmissionStatus = "No submission imported yet.";
  currentSubmissionSource = "—";
  resetDetection();
  const validationCard = el("validation-result-card");
  if (validationCard) validationCard.style.display = "none";
  updateImportContinue();
  renderSubmissionList();
}

function makeTempSubmissionId() {
  return `pending-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function getImportCount(data) {
  return data.file_count || data.downloaded_file_count || 0;
}

function getDisplayName(data, fallback = "Submission") {
  return data.original_filename || data.submission_id || fallback;
}

function addPendingSubmission(name, source) {
  const item = {
    tempId: makeTempSubmissionId(),
    submission_id: null,
    name,
    source,
    status: "uploading",
    file_count: 0,
    detection: null,
  };
  importedSubmissions.push(item);
  renderSubmissionList();
  return item.tempId;
}

function markSubmissionImported(data, verb = "Upload complete", fileLabel = "extracted", source = "Local Upload", tempId = null, fallbackName = "Submission") {
  const n = data.file_count || data.downloaded_file_count || 0;
  currentSubmissionStatus = `${verb} • ${n} file${n !== 1 ? "s" : ""} ${fileLabel}`;
  const detection = {
    nifti_count: Number.isFinite(Number(data.nifti_count)) ? Number(data.nifti_count) : null,
    detected_parameter_map_type: data.detected_parameter_map_type || "Unknown",
    detected_map_type_confidence: data.detected_map_type_confidence || "none",
    detection_warning: data.detection_warning || null,
  };
  const tempIndex = tempId
    ? importedSubmissions.findIndex((item) => item.tempId === tempId)
    : -1;
  const duplicateIndex = data.submission_id
    ? importedSubmissions.findIndex((item) => item.submission_id === data.submission_id && item.tempId !== tempId)
    : -1;
  const existingIndex = duplicateIndex >= 0 ? duplicateIndex : tempIndex;
  const item = {
    tempId: existingIndex >= 0 ? importedSubmissions[existingIndex].tempId : tempId || makeTempSubmissionId(),
    submission_id: data.submission_id,
    name: getDisplayName(data, fallbackName),
    source,
    status: "complete",
    file_count: n,
    detection,
  };

  if (existingIndex >= 0) importedSubmissions[existingIndex] = item;
  else importedSubmissions.push(item);
  if (tempIndex >= 0 && tempIndex !== existingIndex) {
    importedSubmissions.splice(tempIndex, 1);
  }
  selectSubmission(item.submission_id);
  showPill("success", currentSubmissionStatus);
  renderSubmissionList();
}

function markSubmissionFailed(tempId, message) {
  const item = importedSubmissions.find((entry) => entry.tempId === tempId);
  if (item) {
    item.status = "error";
    item.error = message;
    renderSubmissionList();
  }
}

function selectSubmission(submissionId) {
  const item = importedSubmissions.find((entry) => entry.submission_id === submissionId);
  if (!item) return;
  currentSubmissionId = item.submission_id;
  currentSubmissionSource = item.source;
  currentSubmissionStatus = `Selected • ${item.file_count} file${item.file_count !== 1 ? "s" : ""} imported`;
  if (item.detection) {
    currentDetection = { ...item.detection };
    updateAutoDetectionHints();
  }
  updateImportContinue();
  renderSubmissionList();
}

function removeSubmission(tempId) {
  const removed = importedSubmissions.find((entry) => entry.tempId === tempId);
  importedSubmissions = importedSubmissions.filter((entry) => entry.tempId !== tempId);
  if (removed && removed.submission_id === currentSubmissionId) {
    const next = [...importedSubmissions].reverse().find((entry) => entry.submission_id);
    if (next) selectSubmission(next.submission_id);
    else clearSubmissionState();
  }
  renderSubmissionList();
}

function renderSubmissionList() {
  const list = el("submission-list");
  if (!list) return;
  list.innerHTML = "";
  if (!importedSubmissions.length) return;

  importedSubmissions.forEach((item) => {
    const row = document.createElement("div");
    const selected = item.submission_id && item.submission_id === currentSubmissionId;
    row.className = `submission-file-row ${selected ? "selected" : ""} ${item.status}`;
    row.type = "button";
    row.dataset.tempId = item.tempId;
    if (item.submission_id) {
      row.addEventListener("click", () => selectSubmission(item.submission_id));
    }

    const icon = document.createElement("div");
    icon.className = "submission-file-icon";
    icon.textContent = item.source === "GitHub" ? "GH" : item.source === "Zenodo" ? "ZEN" : "ZIP";

    const body = document.createElement("div");
    body.className = "submission-file-body";
    const meta = document.createElement("div");
    meta.className = "submission-file-meta";
    const name = document.createElement("strong");
    name.textContent = item.name || item.submission_id || "Submission";
    const detail = document.createElement("span");
    if (item.status === "error") detail.textContent = item.error || "Import failed";
    else if (item.status === "uploading") detail.textContent = "Importing...";
    else detail.textContent = `${item.file_count} file${item.file_count !== 1 ? "s" : ""} imported`;
    meta.append(name, detail);

    const progress = document.createElement("div");
    progress.className = "submission-progress";
    const bar = document.createElement("span");
    bar.style.width = item.status === "complete" ? "100%" : item.status === "error" ? "100%" : "72%";
    progress.appendChild(bar);
    body.append(meta, progress);

    const status = document.createElement("span");
    status.className = "submission-row-status";
    status.textContent = item.status === "complete" ? (selected ? "Selected" : "Ready") : item.status === "error" ? "Error" : "Uploading";

    const remove = document.createElement("button");
    remove.className = "submission-remove";
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${item.name || "submission"}`);
    remove.textContent = "×";
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      removeSubmission(item.tempId);
    });

    row.append(icon, body, status, remove);
    list.appendChild(row);
  });
}

const localDropZone = el("local-drop-zone");
const fileInput = el("file-input");
const folderInput = el("folder-input");
const filesInput = el("files-input");
const chooseSubmissionBtn = el("choose-submission-btn");
const chooseSubmissionMenu = el("choose-submission-menu");

localDropZone.addEventListener("click", (e) => {
  if (e.target.closest(".upload-menu-wrap")) return;
});

function setChooseSubmissionMenu(open) {
  chooseSubmissionMenu.classList.toggle("open", open);
  chooseSubmissionBtn.setAttribute("aria-expanded", String(open));
}

chooseSubmissionBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  setChooseSubmissionMenu(!chooseSubmissionMenu.classList.contains("open"));
});

chooseSubmissionMenu.querySelectorAll("[data-upload-choice]").forEach((button) => {
  button.addEventListener("click", (e) => {
    e.stopPropagation();
    setChooseSubmissionMenu(false);
    const choice = button.dataset.uploadChoice;
    if (choice === "zip") fileInput.click();
    if (choice === "folder") folderInput.click();
    if (choice === "files") filesInput.click();
  });
});

document.addEventListener("click", () => setChooseSubmissionMenu(false));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") setChooseSubmissionMenu(false);
});

localDropZone.addEventListener("dragover",  (e) => { e.preventDefault(); localDropZone.classList.add("dragging"); });
localDropZone.addEventListener("dragleave", ()  => localDropZone.classList.remove("dragging"));
localDropZone.addEventListener("drop", (e) => {
  e.preventDefault();
  localDropZone.classList.remove("dragging");
  const files = Array.from(e.dataTransfer.files || []);
  const zipFile = files.find((file) => file.name.toLowerCase().endsWith(".zip"));
  if (zipFile) uploadZip(zipFile);
  else if (files.length) uploadFolderFiles(files);
});

fileInput.addEventListener("change", async () => {
  const files = Array.from(fileInput.files || []).filter((file) => file.name.toLowerCase().endsWith(".zip"));
  for (const file of files) {
    await uploadZip(file);
  }
  fileInput.value = "";
});

folderInput.addEventListener("change", () => {
  const count = folderInput.files ? folderInput.files.length : 0;
  el("folder-selection-text").textContent = count
    ? `${count} file${count !== 1 ? "s" : ""} selected`
    : "No local files selected";
  if (count) uploadFolder();
});

filesInput.addEventListener("change", () => {
  const files = Array.from(filesInput.files || []);
  const count = files.length;
  el("folder-selection-text").textContent = count
    ? `${count} file${count !== 1 ? "s" : ""} selected`
    : "No local files selected";
  if (count) uploadFolderFiles(files);
  filesInput.value = "";
});

async function uploadZip(file) {
  if (!file.name.toLowerCase().endsWith(".zip")) {
    showPill("error", "Please select a .zip file.");
    return;
  }

  const tempId = addPendingSubmission(file.name, "Local Upload");

  const formData = new FormData();
  formData.append("file", file);

  try {
    const res  = await fetch(`${API}/api/upload-submission`, { method: "POST", body: formData });
    const data = await res.json();

    if (!res.ok) {
      markSubmissionFailed(tempId, data.detail || "Upload failed.");
      showPill("error", data.detail || "Upload failed.");
    } else {
      markSubmissionImported(data, "Upload complete", "imported", "Local Upload", tempId, file.name);
    }
  } catch {
    markSubmissionFailed(tempId, "Could not reach the server.");
    showPill("error", "Could not reach the server. Is the backend running?");
  }
}

async function uploadFolder() {
  const files = Array.from(folderInput.files || []);
  if (!files.length) {
    showPill("error", "Please choose a folder first.");
    return;
  }
  return uploadFolderFiles(files);
}

async function uploadFolderFiles(files) {
  const folderName = files[0] && files[0].webkitRelativePath
    ? files[0].webkitRelativePath.split("/")[0]
    : "Folder submission";
  const tempId = addPendingSubmission(folderName, "Local Upload");

  const formData = new FormData();
  files.forEach((file) => {
    formData.append("files", file, file.webkitRelativePath || file.name);
  });

  try {
    const res = await fetch(`${API}/api/upload-folder-submission`, {
      method: "POST",
      body: formData,
    });
    const data = await res.json();
    if (!res.ok) {
      markSubmissionFailed(tempId, data.detail || "Folder upload failed.");
      showPill("error", data.detail || "Folder upload failed.");
    } else markSubmissionImported(data, "Upload complete", "imported", "Local Upload", tempId, folderName);
  } catch {
    markSubmissionFailed(tempId, "Could not reach the server.");
    showPill("error", "Could not reach the server. Is the backend running?");
  }
}

el("submission-zenodo-btn").addEventListener("click", importSubmissionZenodo);

async function importSubmissionZenodo() {
  const input = el("submission-zenodo-input").value.trim();
  if (!input) {
    showPill("error", "Enter a Zenodo URL, DOI, or record ID.");
    return;
  }

  const btn = el("submission-zenodo-btn");
  setLoading(btn, true, "Import");
  const tempId = addPendingSubmission(input, "Zenodo");

  try {
    const res = await fetch(`${API}/api/import-submission-zenodo`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ zenodo_input: input }),
    });
    const data = await res.json();
    if (!res.ok) {
      markSubmissionFailed(tempId, data.detail || "Zenodo import failed.");
      showPill("error", data.detail || "Zenodo import failed.");
    } else markSubmissionImported(data, "Import complete", "imported", "Zenodo", tempId, input);
  } catch {
    markSubmissionFailed(tempId, "Could not reach the server.");
    showPill("error", "Could not reach the server. Is the backend running?");
  } finally {
    setLoading(btn, false, "Import");
  }
}

el("github-import-btn").addEventListener("click", importGithubSubmission);

async function importGithubSubmission() {
  const repoUrl = el("github-url-input").value.trim();
  const branch = el("github-branch-input").value.trim();
  if (!repoUrl) {
    showPill("error", "Enter a GitHub repository URL.");
    return;
  }

  const btn = el("github-import-btn");
  setLoading(btn, true, "Import");
  const tempId = addPendingSubmission(repoUrl, "GitHub");

  try {
    const res = await fetch(`${API}/api/import-submission-github`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ repo_url: repoUrl, branch: branch || null }),
    });
    const data = await res.json();
    if (!res.ok) {
      markSubmissionFailed(tempId, data.detail || "GitHub import failed.");
      showPill("error", data.detail || "GitHub import failed.");
    } else markSubmissionImported(data, "Import complete", "imported", "GitHub", tempId, repoUrl);
  } catch {
    markSubmissionFailed(tempId, "Could not reach the server.");
    showPill("error", "Could not reach the server. Is the backend running?");
  } finally {
    setLoading(btn, false, "Import");
  }
}

function showPill(type, text, spinner = false) {
  const pill = el("upload-pill");
  pill.className = `upload-pill visible ${type}`;
  const icon = type === "success" ? `<span class="status-check" aria-hidden="true">✓</span>` : "";
  el("upload-pill-text").innerHTML = spinner
    ? `<span class="spinner dark"></span>${text}`
    : `${icon}${text}`;
}

el("intake-continue-btn").addEventListener("click", () => {
  if (!currentSubmissionId) {
    showPill("error", "Please import a submission first.");
    return;
  }
  goToStep(3);
});

el("details-continue-btn").addEventListener("click", () => {
  stepOneValidationAttempted = true;
  if (validateStepOne({ showErrors: true })) goToStep(2);
});
updateExpectedMapsMode();
updateMapTypeMode();
updateImportContinue();

// ── Step 3: Validation panel ─────────────────────────────────────────────────
// Validate UI is temporarily disabled while validation checks are being finalized.

const validateContinueResultsBtn = el("validate-continue-results-btn");
if (validateContinueResultsBtn) {
  validateContinueResultsBtn.addEventListener("click", () => goToStep(4));
}

const resultsContinueExportBtn = el("results-continue-export-btn");
if (resultsContinueExportBtn) {
  resultsContinueExportBtn.addEventListener("click", () => goToStep(5));
}

// Called every time Step 3 becomes active.
function refreshValidationPanel() {
  const noSub = el("val-no-submission");
  const ready = el("val-ready");
  if (!noSub || !ready) return;

  if (!currentSubmissionId) {
    noSub.style.display = "grid";
    ready.style.display = "none";
    return;
  }

  noSub.style.display = "none";
  ready.style.display = "block";

  // Populate the submission summary from Step 1 form values.
  const challenge = getChallengeType().toUpperCase();
  const maps      = getExpectedMapsDisplay();

  el("val-submission").textContent = currentSubmissionId;
  el("val-challenge").textContent = challenge;
  el("val-map-type").textContent = getMapTypeDisplay();
  el("val-maps").textContent    = maps;
  el("val-source").textContent  = currentSubmissionSource;
  resetValidationChecklist();
}

function yesNo(val) {
  if (val === "yes") return "Yes";
  if (val === "no")  return "No";
  return "—";
}

// Validate button lives in Step 3.
const validateBtn = el("validate-btn");
if (validateBtn) validateBtn.addEventListener("click", async () => {
  if (!currentSubmissionId) return;

  const btn = validateBtn;
  setLoading(btn, true, "Run Validation");

  const payload = {
    submission_id:        currentSubmissionId,
    challenge_type:       getChallengeType(),
    expected_nifti_count: getExpectedNiftiCount(),
    expected_nifti_count_mode: getExpectedMapsMode(),
    include_code:         getRadio("include_code"),
    include_readme:       getRadio("include_readme"),
    team_name:            el("team-name").value.trim()     || null,
    contact_email:        el("contact-email").value.trim() || null,
    map_type:             getMapType()                     || null,
    map_type_mode:        getMapTypeRaw() === "auto" ? "auto" : "manual",
    notes:                null,
  };

  try {
    const res  = await fetch(`${API}/api/validate`, {
      method:  "POST",
      headers: { "Content-Type": "application/json" },
      body:    JSON.stringify(payload),
    });
    const data = await res.json();

    if (!res.ok) renderValidateError(data.detail || "Validation request failed.");
    else         renderValidateResult(data, payload);
  } catch {
    renderValidateError("Could not reach the server. Is the backend running?");
  } finally {
    setLoading(btn, false, "Run Validation");
  }
});

function renderValidateResult(data, form) {
  const card = el("validation-result-card");
  if (!card) return;
  card.style.display = "block";

  const rawErrors = [...(data.errors || [])];
  if (Number(data.nifti_count) === 0) {
    rawErrors.push("No NIfTI parameter maps found.");
  }
  const errorMessages = uniqueDisplayMessages(rawErrors);
  const warningMessages = uniqueDisplayMessages(data.warnings || [])
    .filter((msg) => !errorMessages.some((err) => err.toLowerCase() === msg.toLowerCase()));

  renderResultPanel("result-errors-wrap", "result-errors", errorMessages, "error", true);
  renderResultPanel("result-warnings-wrap", "result-warnings", warningMessages, "warning", true);
  renderValidationStatusLine(errorMessages.length, warningMessages.length);

  const success = el("result-success-wrap");
  success.style.display = "none";

  // Mark step 3 as active (it already is, but done-state will fill in when step 4 is clicked)
  goToStep(3);

  setTimeout(() => card.scrollIntoView({ behavior: "smooth", block: "nearest" }), 80);
}

function renderValidateError(message) {
  const card = el("validation-result-card");
  if (!card) return;
  card.style.display = "block";
  renderValidationStatusLine(1, 0);
  renderResultPanel("result-errors-wrap", "result-errors", [message], "error");
  el("result-warnings-wrap").style.display = "none";
  el("result-success-wrap").style.display  = "none";
}

function resetValidationChecklist() {
  ["nifti", "readme", "code", "map-type", "map-count"].forEach((name) => {
    setValidationCheckState(name, "pending", "Pending");
  });
  const statusLine = el("validation-status-line");
  if (statusLine) statusLine.style.display = "none";
}

function renderValidationStatusLine(errorCount, warningCount) {
  const line = el("validation-status-line");
  if (!line) return;
  line.style.display = "flex";
  line.className = "validation-status-line";
  if (errorCount > 0) {
    line.classList.add("failed");
    line.innerHTML = `<span class="status-dot">×</span><strong>Validation failed</strong>`;
  } else if (warningCount > 0) {
    line.classList.add("warning");
    line.innerHTML = `<span class="status-dot">!</span><strong>Passed with warnings</strong>`;
  } else {
    line.classList.add("passed");
    line.innerHTML = `<span class="status-dot">✓</span><strong>Validation complete</strong>`;
  }
}

function renderResultPanel(wrapId, listId, items, type, alreadySimplified = false) {
  const wrap = el(wrapId);
  const list = el(listId);
  if (!wrap || !list) return;
  const messages = alreadySimplified ? dedupeMessages(items) : uniqueDisplayMessages(items);
  const count = messages.length;
  wrap.style.display = count ? "block" : "none";
  if (!count) {
    list.innerHTML = "";
    return;
  }

  const label = type === "error" ? "Errors" : "Warnings";
  const icon = type === "error" ? "!" : "!";
  wrap.querySelector(".issue-box-title").innerHTML =
    `<span class="result-icon">${icon}</span>${label}`;
  renderList(listId, messages);
}

function updateValidationChecklist(data) {
  const errors = data.errors || [];
  const warnings = data.warnings || [];
  const noNifti = Number(data.nifti_count) === 0;

  setValidationCheckState(
    "nifti",
    data.nifti_count > 0 && !hasIssue(errors, warnings, ["no .nii", "no nifti", "missing nifti", "nifti file appears"])
      ? "pass"
      : noNifti
        ? "error"
        : stateForIssue(errors, warnings, ["no .nii", "no nifti", "missing nifti", "nifti file appears"]),
    data.nifti_count > 0 ? `${data.nifti_count} found` : "0 found"
  );
  setValidationCheckState(
    "readme",
    stateForIssue(errors, warnings, ["readme", "sop", "metadata"]),
    hasIssue(errors, warnings, ["readme", "sop", "metadata"]) ? "Needs review" : "Passed"
  );
  setValidationCheckState(
    "code",
    stateForIssue(errors, warnings, ["code", "dockerfile", "requirements", "scripts"]),
    hasIssue(errors, warnings, ["code", "dockerfile", "requirements", "scripts"]) ? "Needs review" : "Passed"
  );
  setValidationCheckState(
    "map-type",
    stateForIssue(errors, warnings, ["map type", "auto-detect", "auto-detected"]),
    hasIssue(errors, warnings, ["map type", "auto-detect", "auto-detected"]) ? "Needs review" : "Passed"
  );
  setValidationCheckState(
    "map-count",
    stateForIssue(errors, warnings, ["were expected", "but", "expected parameter map"]),
    hasIssue(errors, warnings, ["were expected", "but", "expected parameter map"]) ? "Needs review" : "Passed"
  );
}

function hasIssue(errors, warnings, needles) {
  return [...errors, ...warnings].some((msg) => {
    const lower = String(msg).toLowerCase();
    return needles.some((needle) => lower.includes(needle));
  });
}

function stateForIssue(errors, warnings, needles) {
  if (hasIssue(errors, [], needles)) return "error";
  if (hasIssue([], warnings, needles)) return "warning";
  return "pass";
}

function setValidationCheckState(name, state, label) {
  const row = document.querySelector(`.validation-check-row[data-check="${name}"]`);
  if (!row) return;
  row.classList.remove("pending", "pass", "warning", "error");
  row.classList.add(state);
  const badge = row.querySelector(".check-badge");
  if (badge) badge.textContent = label;
}

function makeChip(text, cls) {
  const span = document.createElement("span");
  span.className = `chip ${cls}`;
  span.textContent = text;
  return span;
}

function renderList(listId, items, emptyText = "") {
  const ul = el(listId);
  ul.innerHTML = "";
  if ((!items || items.length === 0) && emptyText) {
    const li = document.createElement("li");
    li.textContent = emptyText;
    ul.appendChild(li);
    return;
  }
  items.forEach((msg) => {
    const li = document.createElement("li");
    li.textContent = msg;
    ul.appendChild(li);
  });
}

function uniqueDisplayMessages(items) {
  return dedupeMessages((items || []).map(simplifyIssue));
}

function dedupeMessages(items) {
  const seen = new Set();
  return (items || []).filter((item) => {
    const message = String(item || "").trim();
    if (!message) return false;
    const key = message.toLowerCase();
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

// ── Step 4: Results ──────────────────────────────────────────────────────────

async function loadOutputs() {
  const list  = el("outputs-list");
  const empty = el("outputs-empty");
  const latest = el("results-latest");
  const history = el("validation-history");
  list.innerHTML = "";
  empty.style.display  = "none";
  latest.style.display = "none";
  history.style.display = "none";

  try {
    const res  = await fetch(`${API}/api/outputs`);
    const data = await res.json();

    if (!data.results || data.results.length === 0) {
      empty.style.display = "block";
      return;
    }

    const first = data.results[0];
    el("results-latest-body").innerHTML = "";
    el("results-latest-body").appendChild(buildOutputCard(first, "latest"));
    latest.style.display = "block";

    data.results.slice(1).forEach((r) => list.appendChild(buildOutputCard(r, "history")));
    history.style.display = data.results.length > 1 ? "block" : "none";
  } catch {
    empty.style.display = "block";
    empty.querySelector("p").textContent = "Could not load validation results.";
  }
}

function buildOutputCard(r, variant = "history") {
  const passed = r.passed;
  const card   = document.createElement("div");
  card.className = `output-card ${variant} ${passed ? "pass" : "fail"}`;

  const header = document.createElement("div");
  header.className = "output-header";

  const badge = document.createElement("span");
  badge.className = `badge ${passed ? "badge-pass" : "badge-fail"}`;
  badge.textContent = passed ? "Passed" : "Failed";

  const idLabel = document.createElement("span");
  idLabel.className = "output-id";
  idLabel.textContent = r.team_name || r.submission_id;

  const challengeBadge = document.createElement("span");
  challengeBadge.className = "badge badge-info";
  challengeBadge.textContent = r.challenge_type || "—";

  const meta = document.createElement("span");
  meta.className = "output-meta";
  meta.textContent = formatDate(r.validated_at);

  header.append(badge, idLabel, challengeBadge, meta);

  const stats = document.createElement("div");
  stats.className = "output-stats";
  [`${r.nifti_count} NIfTI file(s)`, `${r.errors.length} error(s)`, `${r.warnings.length} warning(s)`]
    .forEach((t) => {
      const s = document.createElement("span");
      s.className = "output-stat";
      s.textContent = t;
      stats.appendChild(s);
    });

  card.append(header, stats);

  const messages = uniqueDisplayMessages([...(r.errors || []), ...(r.warnings || [])])
    .slice(0, variant === "latest" ? 4 : 2);
  if (messages.length) {
    const issues = document.createElement("div");
    issues.className = "output-issues";
    messages.forEach((m) => {
      const d = document.createElement("div");
      d.className = "output-issue";
      d.textContent = m;
      issues.appendChild(d);
    });
    card.appendChild(issues);
  }

  return card;
}

function simplifyIssue(message) {
  const text = String(message || "");
  const lower = text.toLowerCase();
  if (lower.includes("no .nii") || lower.includes("no nifti") || lower.includes("nifti file appears") || lower.includes("missing nifti")) {
    return "No NIfTI files found";
  }
  if (lower.includes("expected parameter map not found") || lower.includes("parameter map not found")) {
    return "Parameter map type missing";
  }
  if (lower.includes("readme") || lower.includes("sop")) {
    return "README/SOP missing";
  }
  if (lower.includes("map type") || lower.includes("auto-detect")) {
    return "Parameter map type missing";
  }
  if (lower.includes("code")) {
    return "Code files missing";
  }
  if (text.length > 86) {
    return `${text.slice(0, 83)}...`;
  }
  return text;
}
