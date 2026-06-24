"use strict";
// ─────────────────────────────────────────────────────────────────────────────
// OSIPI Perfusion Challenge — Review System
// app.js  v28
// ─────────────────────────────────────────────────────────────────────────────

const API = window.location.origin;

// ── Map type options per challenge ────────────────────────────────────────────

const MAP_OPTIONS = {
  asl:   ["CBF", "ATT", "Other"],
  dce:   ["Ktrans", "Kep", "Vp", "Other"],
  dsc:   ["CBF", "CBV", "MTT", "Other"],
  other: ["CBF", "ATT", "Ktrans", "Kep", "Vp", "CBV", "MTT", "Other"],
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

const WF_STEPS = ["upload", "index", "validate", "run", "score", "export"];

const STEP_TITLES = {
  upload:   { title: "Upload",             sub: "Submit parameter maps for automated validation" },
  index:    { title: "Index",              sub: "Detected submissions ready to validate" },
  validate: { title: "Validate",           sub: "Validation results for all submissions" },
  run:      { title: "Run",                sub: "Execute validated submissions" },
  score:    { title: "Score",              sub: "Score validated submissions using the OSIPI TF6.2 provider" },
  export:   { title: "Export",             sub: "Download results as CSV" },
};

function goToStep(step) {
  wf.step = step;
  WF_STEPS.forEach((s) => {
    const panel = el(`step-${s}`);
    if (panel) panel.hidden = (s !== step);
  });
  _syncWfNav();
  // Scroll the content area (sidebar layout), not the window
  document.querySelector(".content")?.scrollTo({ top: 0, behavior: "instant" });
  // Update header title
  const titles = STEP_TITLES[step] || {};
  const hTitle = el("page-title");
  const hSub   = el("page-subtitle");
  if (hTitle) hTitle.textContent  = titles.title || step;
  if (hSub)   hSub.textContent    = titles.sub   || "";
  // Persist step to session
  saveSessionState();
}

function unlockStep(step) {
  const btn = el(`wf-btn-${step}`);
  if (btn) btn.disabled = false;
}

function _syncWfNav() {
  WF_STEPS.forEach((s) => {
    const btn = el(`wf-btn-${s}`);
    if (!btn) return;
    btn.classList.toggle("wf-active", s === wf.step);
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

  // Reset nav steps
  ["index", "validate", "run", "export"].forEach((s) => {
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
  // Reset exec summaries too
  Object.keys(_execSummaries).forEach((k) => delete _execSummaries[k]);
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

// ── Restore banner ────────────────────────────────────────────────────────────

function showRestoreBanner(saved) {
  const banner = el("restore-banner");
  if (!banner) return;

  const infoEl = el("restore-banner-info");
  if (infoEl) {
    // Only non-sensitive summary — no team name, email, file paths, or IDs
    const parts = ["Previous local session found"];

    const subs      = saved.submissions?.length || 0;
    const typeLabel = saved.isBatch
      ? `Batch upload · ${subs} submission${subs !== 1 ? "s" : ""}`
      : "Single submission";
    parts.push(typeLabel);

    const stepTitle = STEP_TITLES[saved.step]?.title || saved.step || "";
    if (stepTitle) parts.push(`last step: ${stepTitle}`);

    // Time only — not date, not team, not email
    if (saved.updatedAt) {
      try {
        const timeStr = new Date(saved.updatedAt).toLocaleTimeString("en-US", {
          hour: "numeric", minute: "2-digit",
        });
        parts.push(`saved ${timeStr}`);
      } catch (_) {}
    }

    infoEl.textContent = parts.join(" · ") + ".";
  }
  banner.style.display = "";
}

function _hideRestoreBanner() {
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
  const stepOrder    = ["upload", "index", "validate", "run", "score", "export"];
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

// Apply saved exec summaries to the run table rows (status labels only, no logs)
function _applyExecSummariesToRows() {
  Object.entries(_execSummaries).forEach(([subId, summary]) => {
    const wrap = [...document.querySelectorAll("#run-submissions-list .er-row-wrap")]
      .find((w) => w.dataset.subId === subId);
    if (!wrap) return;
    const prevStatus = wrap.dataset.execStatus;
    if (prevStatus && prevStatus !== "not-run") return; // already updated
    const newStatus = summary.status || "failed";
    wrap.dataset.execStatus = newStatus;
    const runnable = wrap.dataset.runnable === "true";
    const statusCell = wrap.querySelector(".er-run-status-cell");
    if (statusCell) statusCell.innerHTML = _erRunStatusHtml(newStatus, runnable);
    const outputsCell = wrap.querySelector(".er-outputs-cell");
    if (outputsCell && summary.outputFileCount > 0) {
      const fc = summary.outputFileCount;
      outputsCell.innerHTML = `<span class="vr-run-ok">${fc} file${fc !== 1 ? "s" : ""}</span>`;
    }
    const outCheckCell = wrap.querySelector(".er-outcheck-cell");
    if (outCheckCell) {
      outCheckCell.innerHTML = `<span style="font-size:0.72rem;color:var(--muted)">from session</span>`;
    }
    // Note in detail area
    const drawer = wrap.querySelector(".er-row-detail");
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
  const banner = el("restore-banner");
  if (!banner) return;
  banner.style.display = "";
  banner.style.background = "#fff7ed";
  banner.style.borderColor = "#fed7aa";
  const infoEl = el("restore-banner-info");
  if (infoEl) {
    infoEl.textContent = msg;
    infoEl.style.color = "#c2410c";
  }
  const infoSvg = banner.querySelector("svg");
  if (infoSvg) infoSvg.style.color = "#f97316";
  // Hide restore button, keep only Start new
  const restoreBtn = el("restore-session-btn");
  if (restoreBtn) restoreBtn.style.display = "none";
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
        const sourceLabel = importData.source_type && importData.source_type !== "local"
          ? `${importData.source_type.charAt(0).toUpperCase() + importData.source_type.slice(1)}: `
          : "";
        const name  = importData.original_filename || "upload";
        const count = importData.submission_count;
        desc.textContent = `${sourceLabel}"${name}" contains ${count} submission${count !== 1 ? "s" : ""}.`;
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
          nifti_count:   importData.nifti_count ?? null,
          detected_parameter_map_type: importData.detected_parameter_map_type || "Unknown",
          has_run_instructions: importData.has_run_instructions ?? importData.has_dockerfile ?? null,
          source_folder: importData.source_folder || null,
          detection_warning: importData.detection_warning || null,
          status: "ready",
        }],
      };

      batchState.uploadData = fakeUpload;
      batchState.selectedIds = new Set([importData.submission_id]);

      const desc = el("batch-header-desc");
      if (desc) {
        const name = importData.original_filename || importData.submission_id;
        desc.textContent = `Detected 1 submission: "${name}".`;
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
  }
}

// ── Step 2: Index — render the detected submissions table ─────────────────────

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
        <th>Run Instructions</th>
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

    const safeMapLabel = escapeHtml(sub.detected_parameter_map_type || "Unknown");
    const safeWarning  = escapeHtml(sub.detection_warning || "");
    const safeFolder   = escapeHtml(sub.source_folder || "—");
    const safeSubId    = escapeHtml(sub.submission_id);
    const mapCell = safeWarning
      ? `${safeMapLabel}<div class="batch-warn-text">${safeWarning}</div>`
      : safeMapLabel;

    const hasRun = sub.has_run_instructions ?? sub.has_dockerfile;
    const runCell = hasRun === null || hasRun === undefined
      ? `<span style="color:var(--subtle)">—</span>`
      : hasRun
        ? `<span style="color:var(--success);font-weight:600">Yes</span>`
        : `<span style="color:var(--subtle)">No</span>`;

    const statusBadge = sub.status === "passed"
      ? `<span class="batch-badge badge-pass">Passed</span>`
      : sub.status === "failed"
        ? `<span class="batch-badge badge-fail">Failed</span>`
        : `<span class="batch-badge badge-ready">Ready</span>`;

    tr.innerHTML = `
      <td class="td-check">
        <input type="checkbox" data-id="${sub.submission_id}" ${isSelected ? "checked" : ""} />
      </td>
      <td class="td-folder">${safeFolder}</td>
      <td class="td-id" title="${sub.submission_id}">${safeSubId}</td>
      <td>${sub.nifti_count ?? "—"}</td>
      <td>${mapCell}</td>
      <td>${runCell}</td>
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
      _syncBatchHeaderCheckbox();
      _syncBatchValidateBtn();
      saveSessionState();
    });

    tbody.appendChild(tr);
  });

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
    _syncBatchValidateBtn();
  });

  _syncBatchValidateBtn();
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

  const disableBtns = (v) => {
    [batchValSelBtn, batchValAllBtn, batchSelectAllBtn, batchDeselectAllBtn].forEach((b) => {
      if (b) b.disabled = v;
    });
  };
  disableBtns(true);
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
    disableBtns(false);
    _syncBatchValidateBtn();
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

// ── Step 3: Validate — render the review table ────────────────────────────────

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

function renderValidateStep(data, isSingleMode) {
  const results = data.results || [];
  const single  = isSingleMode === true || !batchState.isBatch;

  // Pre-compute counts (used for summary + auto-advance)
  const runnableCount    = results.filter((r) => r.run_readiness === "runnable"
                                               || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile))).length;
  const resultOnlyCount  = results.filter((r) => r.run_readiness === "result_only"
                                               || (r.passed && !r.has_run_instructions && r.nifti_count > 0)).length;
  const needsReviewCount = results.filter((r) => !r.passed || (r.warnings || []).length > 0).length;

  // Reset filter state
  _reviewFilter.filter  = "all";
  _reviewFilter.search  = "";
  _reviewFilter.sort    = "status";
  _reviewFilter.showAll = false;
  const searchEl  = el("batch-search");
  const sortEl    = el("batch-sort");
  const viewSelEl = el("batch-view-select");
  if (searchEl)  searchEl.value  = "";
  if (sortEl)    sortEl.value    = "status";
  if (viewSelEl) viewSelEl.value = "all";

  // ── 1. One-line summary in step header desc ───────────────────────────────
  const desc = el("batch-results-desc");
  if (desc) {
    if (single) {
      const r0 = results[0] || {};
      const ec = (r0.errors   || []).length;
      const wc = (r0.warnings || []).length;
      desc.textContent = ec > 0
        ? `${ec} error${ec !== 1 ? "s" : ""}${wc > 0 ? `, ${wc} warning${wc !== 1 ? "s" : ""}` : ""}`
        : wc > 0 ? `${wc} warning${wc !== 1 ? "s" : ""} · review recommended`
                 : "All checks passed";
    } else {
      const parts = [`${results.length} validated`];
      if (runnableCount > 0) parts.push(`${runnableCount} ready to run`);
      if (resultOnlyCount > 0) parts.push(`${resultOnlyCount} result-only`);
      if (needsReviewCount > 0) parts.push(`${needsReviewCount} need review`);
      desc.textContent = parts.join(" · ");
    }
  }

  // ── 2. View dropdown + search + sort handlers ─────────────────────────────
  if (viewSelEl) {
    viewSelEl.onchange = () => {
      _reviewFilter.filter  = viewSelEl.value;
      _reviewFilter.showAll = false;
      _applyReviewFilters();
    };
  }
  if (searchEl) {
    searchEl.oninput = () => {
      _reviewFilter.search  = searchEl.value;
      _reviewFilter.showAll = false;
      _applyReviewFilters();
    };
  }
  if (sortEl) {
    sortEl.onchange = () => {
      _reviewFilter.sort = sortEl.value;
      _applyReviewFilters();
    };
  }

  // ── Toolbar visibility: hide for single submission (no search/filter needed)
  const valToolbar = el("val-toolbar");
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
    results.forEach((r) => {
      const rNiftiCount = Number(r.nifti_count ?? 0);
      const _spur = (msgs) =>
        rNiftiCount > 0 ? msgs.filter((m) => m !== "No output files found") : msgs;

      const errors   = _spur(dedupeMessages((r.errors   || []).map(simplifyMessage)));
      const warnings = _spur(dedupeMessages((r.warnings || []).map(simplifyMessage)))
        .filter((w) => !errors.some((e) => e.toLowerCase() === w.toLowerCase()));
      const checks   = buildSuccessChecks(r, errors.length, warnings.length, r.map_type);
      const passed   = r.passed;
      const hasWarn  = warnings.length > 0;
      const hasRunInstructions = !!(r.has_run_instructions ?? r.has_dockerfile);
      const runReadiness = r.run_readiness
        || (passed && hasRunInstructions ? "runnable"
            : passed && !hasRunInstructions ? "result_only"
            : "not_runnable");
      const runnable = runReadiness === "runnable";
      const isResultOnly = runReadiness === "result_only";

      // Status badge — 3 states only
      let valStatus, vsBadgeCss, vsBadgeTxt;
      if (!passed)      { valStatus = "failed";  vsBadgeCss = "vs-fail";   vsBadgeTxt = "Failed"; }
      else if (hasWarn) { valStatus = "warning"; vsBadgeCss = "vs-review"; vsBadgeTxt = "Needs review"; }
      else              { valStatus = "passed";  vsBadgeCss = "vs-pass";   vsBadgeTxt = "Passed"; }

      // Run state — plain text, no badge
      let runClass, runTxt;
      if (!passed)        { runClass = "vr-run-na";        runTxt = "Cannot run"; }
      else if (isResultOnly) { runClass = "vr-run-result-only"; runTxt = "Result-only"; }
      else if (runnable)  { runClass = "vr-run-ready";     runTxt = "Ready"; }
      else                { runClass = "vr-run-na";        runTxt = "—"; }

      const execInitStatus = runnable ? "not-run" : "cannot-run";

      const safeSubId     = escapeHtml(r.submission_id);
      const safeFolder    = r.source_folder ? escapeHtml(r.source_folder) : "";
      const safeChallenge = escapeHtml(r.challenge_type || getChallengeType() || "dce");

      const { html: issueHtml } = _issueSummary(errors, warnings);

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
        ? `<p class="vr-result-only-note">This submission contains result maps only and cannot be run automatically.</p>`
        : "";
      const noIssueHtml = (!errHtml && !warnHtml)
        ? `<p style="font-size:0.73rem;color:var(--subtle);margin:0">No errors or warnings.</p>` : "";

      // No inline exec section on Validate — execution happens in Run step only
      const execHtml = "";

      const wrap = document.createElement("div");
      wrap.className = "br-row-wrap";
      wrap.dataset.valStatus  = valStatus;
      wrap.dataset.runnable   = String(runnable);
      wrap.dataset.execStatus = execInitStatus;
      wrap.dataset.subId      = r.submission_id;
      wrap.dataset.name       = (r.submission_id + " " + (r.source_folder || "")).toLowerCase();
      wrap.dataset.errCount   = String(errors.length);
      wrap.dataset.warnCount  = String(warnings.length);

      // 5-column row: Submission | Status | Issue | Run | Action
      wrap.innerHTML = `
        <div class="br-row">
          <div style="min-width:0">
            <div class="br-sub-name" title="${safeSubId}">${safeSubId}</div>
            ${safeFolder ? `<div class="br-sub-folder">${safeFolder}</div>` : ""}
          </div>
          <div><span class="vs-badge ${vsBadgeCss}">${vsBadgeTxt}</span></div>
          <div>${issueHtml}</div>
          <div>
            <span class="${runClass}">${runTxt}</span>
            <span class="br-badge badge-exec-none val-card-exec-badge" style="display:none;font-size:0.65rem;margin-left:4px"></span>
          </div>
          <div>
            <button type="button" class="vr-action-btn vr-details-btn">Details</button>
          </div>
        </div>
        <div class="vr-row-detail" style="display:none">
          ${resultOnlyNote}${niftiLine}${noIssueHtml}${errHtml}${warnHtml}${techHtml}${execHtml}
        </div>`;

      list.appendChild(wrap);
    });

    _applyReviewFilters();
  }

  // ── 5. Unlock nav steps + navigate ───────────────────────────────────────
  unlockStep("validate");
  unlockStep("run");
  unlockStep("export");

  const singleActions = el("single-result-actions");
  if (singleActions) singleActions.style.display = single ? "" : "none";

  const backBtn = el("batch-back-to-batch-btn");
  if (backBtn) backBtn.style.display = batchState.isBatch ? "" : "none";

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

  // Auto-advance: go to Run step for both runnable and result-only cases.
  // Run step handles both: shows execution queue for runnable, "Skipped" notice for result-only.
  if (hasAnyPassed) {
    renderRunStep().catch(() => {});
    goToStep("run");
  } else {
    goToStep("validate");
  }
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
      case "needs-review": show = vs === "warning"; break;
      case "failed":       show = vs === "failed"; break;
      case "ready":        show = runnable && es === "not-run"; break;
      // legacy values kept for safety
      case "passed":       show = vs === "passed"; break;
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
  // Sync the view select dropdown (v23 toolbar)
  const viewSel = el("batch-view-select");
  if (viewSel) viewSel.value = _reviewFilter.filter;
  // Also sync any legacy chips still present
  document.querySelectorAll(".filter-chip[data-filter-val]").forEach((c) => {
    c.classList.toggle("fc-active", c.dataset.filterVal === _reviewFilter.filter);
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
  if (detailsBtn) detailsBtn.textContent = open ? "Close" : "Details";
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
  const pct = total > 0 ? Math.round((completed / total) * 100) : 0;
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
    if (txt)   txt.textContent = "All submissions complete";
    if (eta)   eta.textContent = `${completed - failed} passed · ${failed} failed`;
    // Unlock Score step now that execution outputs exist
    unlockStep("score");
    renderScoreStep().catch(() => {});
  } else {
    if (txt) txt.textContent = `Running submissions… ${completed} of ${total}`;
    if (eta) eta.textContent = "";
  }
}

// Build/refresh the entire run step table from batchState.validationData
async function renderRunStep() {
  const results = batchState.validationData ? (batchState.validationData.results || []) : [];
  const single  = !batchState.isBatch;

  // Reset filter
  _runFilter.view    = "ready";
  _runFilter.showAll = false;
  const viewSel = el("run-view-select");
  if (viewSel) viewSel.value = "ready";

  // Count runnable and result-only
  const runnableResults  = results.filter((r) =>
    (r.run_readiness === "runnable") || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile)));
  const resultOnlyResult = results.filter((r) =>
    (r.run_readiness === "result_only") || (r.passed && !r.has_run_instructions && (r.nifti_count || 0) > 0));
  const runnableCount   = runnableResults.length;
  const resultOnlyCount = resultOnlyResult.length;
  const allResultOnly   = runnableCount === 0 && resultOnlyCount > 0;

  // One-line summary
  const desc = el("run-results-desc");
  if (desc) {
    if (results.length === 0) {
      desc.textContent = "No validated submissions. Run validation first.";
    } else if (allResultOnly) {
      desc.textContent = `${resultOnlyCount} result-only submission${resultOnlyCount !== 1 ? "s" : ""} — execution not needed.`;
    } else if (runnableCount === 0) {
      desc.textContent = "No submissions ready to run — review validation issues.";
    } else {
      const parts = [`${runnableCount} ready to run`];
      if (resultOnlyCount > 0) parts.push(`${resultOnlyCount} result-only (skipped)`);
      const cannotRun = results.length - runnableCount - resultOnlyCount;
      if (cannotRun > 0) parts.push(`${cannotRun} cannot run`);
      desc.textContent = parts.join(" · ");
    }
  }

  // Skipped notice (shown when all result-only)
  const skippedNotice = el("run-skipped-notice");
  if (skippedNotice) skippedNotice.style.display = allResultOnly ? "" : "none";
  const skippedContinueBtn = el("run-skipped-continue-btn");
  if (skippedContinueBtn) {
    const newBtn = skippedContinueBtn.cloneNode(true);
    skippedContinueBtn.replaceWith(newBtn);
    newBtn.addEventListener("click", () => goToStep("export"));
  }

  // Docker availability
  const docker       = await checkDockerAvailability();
  const dockerBanner = el("batch-docker-banner");
  if (dockerBanner) {
    const cls  = docker.available ? "ok"  : "err";
    const dot  = `<span class="rsc-docker-dot"></span>`;
    const label = docker.available
      ? `${dot} Docker ready${docker.version ? ` · ${escapeHtml(docker.version)}` : ""}`
      : `${dot} Docker unavailable`;
    dockerBanner.innerHTML = `<span class="rsc-docker-badge ${cls}">${label}</span>`;
    dockerBanner.style.display = "";
  }

  // Run Settings Card: hide when all submissions are result-only (nothing to run)
  const settingsCard = el("run-settings-card");
  if (settingsCard) settingsCard.style.display = allResultOnly ? "none" : "";

  // Show toolbar + table only if there are results and not all-result-only
  const toolbar   = el("run-toolbar");
  const runTable  = el("run-table");
  const runAllBtn = el("batch-exec-all-btn");
  if (toolbar)  toolbar.style.display  = results.length > 0 && !allResultOnly ? "" : "none";
  if (runTable) runTable.style.display = results.length > 0 && !allResultOnly ? "" : "none";

  // Run All button: batch + runnable + docker OK
  if (runAllBtn)
    runAllBtn.style.display = batchState.isBatch && runnableCount > 0 && docker.available ? "" : "none";

  // Wire view-select
  if (viewSel) {
    viewSel.onchange = () => {
      _runFilter.view    = viewSel.value;
      _runFilter.showAll = false;
      _applyRunFilters();
    };
  }

  // Show-all button
  const showAllBtn = el("run-show-all-btn");
  if (showAllBtn) {
    showAllBtn.onclick = () => {
      _runFilter.showAll = !_runFilter.showAll;
      _applyRunFilters();
    };
  }

  // Build rows
  const list = el("run-submissions-list");
  if (!list) return;
  list.innerHTML = "";

  results.forEach((r) => {
    const runReadiness = r.run_readiness
      || (r.passed && !!(r.has_run_instructions ?? r.has_dockerfile) ? "runnable"
          : r.passed && !r.has_run_instructions && (r.nifti_count || 0) > 0 ? "result_only"
          : "not_runnable");
    const runnable     = runReadiness === "runnable";
    const isResultOnly = runReadiness === "result_only";
    const safeSubId    = escapeHtml(r.submission_id);
    const safeChall    = escapeHtml(r.challenge_type || getChallengeType() || "dce");

    let initExecStatus;
    if (runnable)     initExecStatus = "not-run";
    else if (isResultOnly) initExecStatus = "result-only";
    else              initExecStatus = "cannot-run";

    const wrap = document.createElement("div");
    wrap.className       = "br-row-wrap er-row-wrap";
    wrap.dataset.subId   = r.submission_id;
    wrap.dataset.challenge = r.challenge_type || getChallengeType() || "dce";
    wrap.dataset.runnable = String(runnable);
    wrap.dataset.execStatus = initExecStatus;
    wrap.dataset.name    = r.submission_id.toLowerCase();

    // Initial display values
    const runStatusHtml = _erRunStatusHtml(initExecStatus, runnable);
    const outputsHtml   = isResultOnly
      ? `<span class="rs-na" title="Submitted maps used">Submitted maps</span>`
      : `<span class="rs-na">—</span>`;
    const outCheckHtml  = isResultOnly
      ? `<span class="oc-skipped">—</span>`
      : `<span class="oc-pending">Pending</span>`;

    // Details-only action — no per-row Run button
    const actionHtml = `<button type="button" class="vr-action-btn er-detail-btn">Details</button>`;

    // Folder line (if present)
    const safeFolder = r.source_folder ? escapeHtml(r.source_folder) : "";
    const subNameHtml = `<div class="br-sub-name" title="${safeSubId}">${safeSubId}</div>
      ${safeFolder ? `<div class="br-sub-folder">${safeFolder}</div>` : ""}`;

    wrap.innerHTML = `
      <div class="br-row er-row">
        <div style="min-width:0">${subNameHtml}</div>
        <div class="er-run-status-cell">${runStatusHtml}</div>
        <div class="er-outputs-cell">${outputsHtml}</div>
        <div class="er-outcheck-cell">${outCheckHtml}</div>
        <div>${actionHtml}</div>
      </div>
      <div class="er-row-detail" style="display:none">
        ${isResultOnly
          ? `<p class="vr-result-only-note" style="margin:0">This submission contains result maps only — automatic execution is not needed. The submitted maps are used directly.</p>`
          : `<p class="vr-issue-ok" style="margin:0 0 8px">Run this submission to see details.</p>`
        }
      </div>`;

    list.appendChild(wrap);
  });

  _applyRunFilters();
}

// Render the run-status cell content for a given execStatus + runnable
function _erRunStatusHtml(execStatus, runnable) {
  switch (execStatus) {
    case "not-run":     return runnable
                          ? `<span class="rs-badge rs-ready">Ready</span>`
                          : `<span class="rs-na">Not run</span>`;
    case "cannot-run":  return `<span class="rs-na">Cannot run</span>`;
    case "result-only": return `<span class="rs-badge rs-skipped">Skipped — result-only</span>`;
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
  const rows = [...list.querySelectorAll(".er-row-wrap")];

  const matchingRows = [];
  rows.forEach((row) => {
    const es       = row.dataset.execStatus;
    const runnable = row.dataset.runnable === "true";
    let show = true;
    switch (view) {
      // "ready" view shows runnable-not-run and result-only rows together
      case "ready":     show = (runnable && es === "not-run") || es === "result-only"; break;
      case "not-run":   show = es === "not-run"; break;
      case "passed":    show = es === "passed"; break;
      case "failed":    show = es === "failed"; break;
      case "timed-out": show = es === "timed-out"; break;
      default:          show = true;
    }
    if (show) matchingRows.push(row);
    else row.style.display = "none";
  });

  const LIMIT = 5;
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

// Update a run row after execution completes
function _updateRunRow(subId, execData, isError) {
  const wrap = [...document.querySelectorAll("#run-submissions-list .er-row-wrap")]
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

  // Update run-status cell
  const statusCell = wrap.querySelector(".er-run-status-cell");
  if (statusCell) statusCell.innerHTML = _erRunStatusHtml(newExecStatus, runnable);

  // Update outputs cell
  const outputsCell = wrap.querySelector(".er-outputs-cell");
  if (outputsCell && !isError) {
    const fc = execData.output_file_count ?? (Array.isArray(execData.output_files) ? execData.output_files.length : 0);
    outputsCell.innerHTML = fc > 0
      ? `<span class="vr-run-ok">${fc} file${fc !== 1 ? "s" : ""}</span>`
      : `<span class="vr-issue-warn">0 files</span>`;
  }

  // Update output-check cell
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

  // Populate the detail drawer with full exec breakdown and auto-open it
  const drawer = wrap.querySelector(".er-row-detail");
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

async function runBatchExec(btn, subId, challenge) {
  const timeout = parseInt(el("batch-exec-timeout")?.value || "300", 10) || 300;
  if (btn) { btn.disabled = true; btn.textContent = "Running…"; }

  // Mark row as running
  const runWrap = [...document.querySelectorAll("#run-submissions-list .er-row-wrap")]
    .find((w) => w.dataset.subId === subId);
  if (runWrap) {
    runWrap.dataset.execStatus = "running";
    const sc = runWrap.querySelector(".er-run-status-cell");
    if (sc) sc.innerHTML = _erRunStatusHtml("running", true);
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
    if (btn) { btn.disabled = false; btn.textContent = "Run"; }
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

// Delegation: Details button in run step (.er-detail-btn)
(function initRunDetailBtnDelegation() {
  document.addEventListener("click", (e) => {
    const btn = e.target.closest(".er-detail-btn");
    if (!btn) return;
    const wrap = btn.closest(".er-row-wrap");
    if (!wrap) return;
    e.stopPropagation();
    const drawer = wrap.querySelector(".er-row-detail");
    if (!drawer) return;
    const open = drawer.style.display === "none" || drawer.style.display === "";
    // toggle based on current visibility
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
    // Collect all runnable rows that haven't been executed yet
    const runnableRows = [...document.querySelectorAll("#run-submissions-list .er-row-wrap")]
      .filter((w) => w.dataset.runnable === "true" && w.dataset.execStatus === "not-run");
    if (!runnableRows.length) return;
    btn.disabled = true;
    _initRunProgress(runnableRows.length);
    if (statusEl) { statusEl.style.display = ""; statusEl.textContent = `Starting ${runnableRows.length} execution(s)…`; }
    let done = 0;
    for (const row of runnableRows) {
      if (statusEl) statusEl.textContent = `Running ${done + 1} of ${runnableRows.length}…`;
      await runBatchExec(null, row.dataset.subId, row.dataset.challenge || "dce");
      done++;
    }
    if (statusEl) statusEl.textContent = `Done — ran ${done} submission(s).`;
    btn.disabled = false;
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
      const label = blinded ? "Export Blinded Execution CSV" : "Export Unblinded Execution CSV";
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

// ── Step 6: Export ────────────────────────────────────────────────────────────

function _syncExportStep() {
  // Show single or batch validation export buttons based on mode
  const batchValWrap  = el("batch-export-val-wrap");
  const singleValWrap = el("single-export-val");
  const batchExecWrap = el("batch-export-exec-wrap");
  const singleExecRow = el("exec-export-row");

  const hasExecResults = Object.keys(_execSummaries).length > 0;

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
  const label = blinded ? "Export Blinded CSV" : "Export Unblinded CSV";
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
  const label = blinded ? "Export Blinded CSV" : "Export Unblinded CSV";
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
  const label = blinded ? "Export Blinded Execution CSV" : "Export Unblinded Execution CSV";
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
    _enableScoringExport();
  } else {
    if (txt) txt.textContent = `Scoring submissions… ${completed} of ${total}`;
    if (eta) eta.textContent = "";
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
};

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
function _updateScoreStatusCard(provs) {
  const titleEl = el("score-status-title");
  const subEl   = el("score-status-sub");
  const badgeEl = el("score-status-badge");
  const hintEl  = el("score-status-hint");
  const btnAll  = el("btn-score-all");

  const officialReady = provs.some((p) => p.category === "official" && p.status === "ready");
  const hasSubs       = _getKnownSubmissions().length > 0;

  if (officialReady) {
    if (titleEl) titleEl.textContent = "Scoring ready";
    if (subEl)   subEl.textContent   = "The official scoring provider is configured. Select submissions below and run scoring.";
    if (badgeEl) { badgeEl.textContent = "Configured"; badgeEl.className = "score-status-badge badge-ready"; }
    if (hintEl)  hintEl.textContent  = "Score your Ktrans outputs. Exports include scoring results once complete.";
    if (btnAll)  btnAll.disabled     = !hasSubs;
  } else {
    if (titleEl) titleEl.textContent = "Scoring not configured yet";
    if (subEl)   subEl.textContent   = "Scoring will be available after the official scoring script, reference data, and masks are configured.";
    if (badgeEl) { badgeEl.textContent = "Not configured"; badgeEl.className = "score-status-badge"; }
    if (hintEl)  hintEl.textContent  = "Generated outputs can be validated and exported now. Scoring is the next provider-based step.";
    if (btnAll)  btnAll.disabled     = true;
  }

  // Re-wire button (clone clears old listeners)
  if (btnAll && officialReady && hasSubs) {
    const fresh = btnAll.cloneNode(true);
    fresh.disabled    = false;
    fresh.textContent = "Score outputs";
    btnAll.replaceWith(fresh);
    fresh.addEventListener("click", async () => {
      const readyRows = [...document.querySelectorAll(".sc-row-wrap")]
        .filter((w) => w.dataset.scoreStatus === "ready" || w.dataset.scoreStatus === "not_checked");
      if (!readyRows.length) return;
      fresh.disabled    = true;
      fresh.textContent = "Scoring…";
      _initScoreProgress(readyRows.length);
      for (const row of readyRows) {
        await _runSingleScore(null, row.dataset.subId, row.dataset.challenge || "dce", row.dataset.mapType || "Ktrans");
      }
      fresh.disabled    = false;
      fresh.textContent = "Score outputs";
    });
  }
}

async function renderScoreStep() {
  unlockStep("score");

  // ── 1. Fetch providers, update status card + collapsed details ───────────────
  const grid = el("score-provider-grid");
  if (grid) grid.innerHTML = `<p style="font-size:0.78rem;color:var(--muted);margin:0">Loading…</p>`;

  let provs = [];
  try {
    const r = await fetch(`${API}/api/scoring-status`);
    const d = await r.json();
    provs   = d.providers || [];
  } catch (_) {
    // Provider fetch failed — status card stays in "not configured" state
  }

  _updateScoreStatusCard(provs);

  if (grid) {
    grid.innerHTML = provs.length
      ? provs.map(_renderProviderCard).join("")
      : `<p style="font-size:0.78rem;color:var(--muted);margin:0">No providers found.</p>`;
  }

  saveSessionState();

  // ── 2. Submission scoring table ─────────────────────────────────────────────
  const tableCard = el("score-table-card");
  const tbody     = el("score-table-body");
  if (!tableCard || !tbody) return;

  const subs = _getKnownSubmissions();
  if (!subs.length) return;

  tableCard.style.display = "";
  tbody.innerHTML = "";

  for (const sub of subs) {
    const sid  = sub.submission_id || sub;
    const name = sub.display_name  || sid;
    tbody.insertAdjacentHTML("beforeend", _buildScoreRow(sid, name));
  }

  for (const sub of subs) {
    _fetchAndUpdateScoreStatus(sub.submission_id || sub);
  }
}

// Build an HTML score table row for a given submission.
function _buildScoreRow(sid, displayName) {
  return `
  <tr class="sc-row-wrap" data-sub-id="${escapeHtml(sid)}"
      data-score-status="not_checked"
      data-challenge="dce" data-map-type="Ktrans">
    <td class="sc-col-sub">${escapeHtml(displayName || sid)}</td>
    <td class="sc-col-status">
      <span class="ss-badge ss-not-conf">Checking…</span>
    </td>
    <td class="sc-col-metrics">
      <div class="sc-metrics-row">
        ${_SC_METRIC_KEYS.map((k) =>
          `<span class="sc-metric-pill">${escapeHtml(_SC_METRIC_LABELS[k])}</span>`
        ).join("")}
      </div>
    </td>
    <td class="sc-col-artifacts" id="sc-artifacts-${escapeHtml(sid)}">—</td>
    <td class="sc-col-action">
      <button type="button" class="sc-score-btn btn-sm"
              data-sub-id="${escapeHtml(sid)}"
              data-challenge="dce" data-map-type="Ktrans"
              disabled>Score</button>
    </td>
  </tr>
  <tr class="sc-row-detail-row" style="display:none">
    <td colspan="5">
      <div class="sc-row-detail" id="sc-detail-${escapeHtml(sid)}"></div>
    </td>
  </tr>`;
}

// Fetch /api/scoring-status for one submission and update its row.
async function _fetchAndUpdateScoreStatus(sid) {
  try {
    const r    = await fetch(`${API}/api/scoring-status?submission_id=${encodeURIComponent(sid)}&challenge_type=dce&map_type=Ktrans`);
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
  if (!row) return;

  const status = data.status || "not_configured";
  row.dataset.scoreStatus = status;

  let badgeCls, badgeTxt;
  switch (status) {
    case "ready":        badgeCls = "ss-ready";    badgeTxt = "Ready"; break;
    case "scored":       badgeCls = "ss-scored";   badgeTxt = "Scored"; break;
    case "failed":       badgeCls = "ss-failed";   badgeTxt = "Failed"; break;
    default:             badgeCls = "ss-not-conf"; badgeTxt = "Not configured"; break;
  }

  const badge = row.querySelector(".ss-badge");
  if (badge) { badge.className = `ss-badge ${badgeCls}`; badge.textContent = badgeTxt; }

  // Enable Score button only when ready or already scored (retry)
  const scoreBtn = row.querySelector(".sc-score-btn");
  if (scoreBtn) {
    scoreBtn.disabled = !(status === "ready" || status === "scored" || status === "failed");
  }

  // If already scored, populate metrics
  if (status === "scored" && data.score_result) {
    _applyMetrics(sid, data.score_result.metrics || {});
    _applyArtifacts(sid, data.score_result.artifacts || []);
    _enableScoringExport();
  }

  // Populate detail drawer if there's a message or missing list
  const detail = el(`sc-detail-${sid}`);
  const detailRow = detail?.closest("tr.sc-row-detail-row");
  if (detail) {
    const missing = data.missing || [];
    const msg     = data.message || "";
    const misHtml = missing.length
      ? `<ul class="sc-missing-list">${missing.map((m) => `<li>${escapeHtml(m)}</li>`).join("")}</ul>` : "";
    detail.innerHTML = `<p style="font-size:0.73rem;margin:0 0 4px">${escapeHtml(msg)}</p>${misHtml}`;
    if (detailRow && (missing.length > 0 || status === "scored" || status === "failed")) {
      detailRow.style.display = "";
    }
  }
}

function _applyMetrics(sid, metrics) {
  const row = [...document.querySelectorAll(".sc-row-wrap")]
    .find((r) => r.dataset.subId === sid);
  if (!row) return;
  const metRow = row.querySelector(".sc-metrics-row");
  if (!metRow) return;
  metRow.innerHTML = _SC_METRIC_KEYS.map((k) => {
    const val    = metrics[k];
    const hasVal = val !== undefined && val !== null && val !== "";
    return `<span class="sc-metric-pill${hasVal ? " has-value" : ""}">`
         + escapeHtml(_SC_METRIC_LABELS[k])
         + (hasVal ? `: ${escapeHtml(String(val))}` : "")
         + `</span>`;
  }).join("");
}

function _applyArtifacts(sid, artifacts) {
  const cell = el(`sc-artifacts-${sid}`);
  if (!cell) return;
  if (!artifacts || artifacts.length === 0) { cell.textContent = "—"; return; }
  cell.textContent = `${artifacts.length} file${artifacts.length > 1 ? "s" : ""}`;
}

// Return the list of known submissions for the current session.
// Falls back to batchState.validationData, then state.submissionId.
function _getKnownSubmissions() {
  if (batchState.validationData && batchState.validationData.length > 0) {
    return batchState.validationData.map((r) => ({
      submission_id: r.submission_id,
      display_name:  r.submission_id,
    }));
  }
  if (state.submissionId) {
    return [{ submission_id: state.submissionId, display_name: state.submissionId }];
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
  if (btn) { btn.disabled = true; btn.textContent = "Scoring…"; }

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
    if (btn) btn.disabled = false;
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

// "Score All" button is wired dynamically inside renderScoreStep().

// ── Scoring export helpers ────────────────────────────────────────────────────

function _enableScoringExport() {
  const blindedBtn   = el("export-scoring-blinded-btn");
  const unblindedBtn = el("export-scoring-unblinded-btn");
  const sub          = el("export-scoring-sub");
  if (blindedBtn)   blindedBtn.disabled   = false;
  if (unblindedBtn) unblindedBtn.disabled = false;
  if (sub) sub.textContent = "Scoring results available for export";
}

function _makeScoringExportHandler(btn, blinded) {
  if (!btn) return;
  const label = blinded ? "Export Blinded CSV" : "Export Unblinded CSV";
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

// ── Init ───────────────────────────────────────────────────────────────────────

updateMapTypePills(getRadio("challenge_type") || "dce");
syncSubmitLabel();

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
// Session restore — startup check (no auto-restore; banner only)
// ══════════════════════════════════════════════════════════════════════════════

(function initSessionBanner() {
  const saved = loadSessionState();
  if (!saved) return;   // nothing saved or expired — normal fresh start

  // Show banner — do NOT restore automatically
  showRestoreBanner(saved);

  // "Restore" button
  const restoreBtn = el("restore-session-btn");
  if (restoreBtn) {
    restoreBtn.addEventListener("click", async () => {
      _hideRestoreBanner();
      const ok = await restoreSessionFromStorage();
      if (!ok) {
        // Session data was cleared (expired or corrupt) between banner display and click
        clearSessionState();
      }
    });
  }

  // "Start new" button in banner
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
