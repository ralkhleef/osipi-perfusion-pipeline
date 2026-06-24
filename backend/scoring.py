"""backend/scoring.py — Scoring framework for OSIPI pipeline.

Provider system
--------------
Two providers are registered:

    osipi_tf62_dce_ktrans          [OFFICIAL]
        OSIPI Task Force 6.2 DCE-MRI Ktrans challenge scoring.
        Required directory layout under data/scoring/providers/osipi_tf62_dce_ktrans/:
            challengeScoring.py       ← official scoring script
            reference/                ← DRO / reference Ktrans NIfTI maps
            masks/                    ← binary mask NIfTI files

        If masks/ does not exist, the code falls back to searching reference/
        for any file containing "mask" in its name.

        Expected participant output filenames (from Docker execution):
            Synthetic_P<n>_Visit<n>.nii[.gz]
            Clinical_P<n>_Visit<n>.nii[.gz]

        NOTE: The argument names passed to challengeScoring.py (--submission,
        --reference, --masks, --output) are an educated guess based on common
        Python CLI patterns. Adjust SCRIPT_ARGS_TEMPLATE in score_submission()
        if the real script uses different flag names.

    osipi_codecollection_dce_testdata   [DEVELOPMENT ONLY — never runs scoring]
        CSV test data from OSIPI/DCE-DSC-MRI_CodeCollection.
        Used only to test provider-discovery UI. NOT for scoring NIfTI maps.

This module NEVER returns or fabricates metric values.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.path_config import (
    CODECOLLECTION_DIR,
    OSIPI_TF62_DIR,
    OUTPUTS_DIR,
    SCORING_OUTPUTS_DIR,
)

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    # ── Official: OSIPI TF6.2 DCE Ktrans ─────────────────────────────────────
    "osipi_tf62_dce_ktrans": {
        "provider_id":   "osipi_tf62_dce_ktrans",
        "display_name":  "OSIPI TF6.2 DCE Ktrans",
        # legacy key kept for API compat
        "provider_name": "OSIPI TF6.2 DCE Ktrans",
        "category":      "official",
        "official":      True,
        "challenge_type": "dce",
        "map_type":      "ktrans",
        "description":   "OSIPI Task Force 6.2 DCE-MRI Ktrans challenge scoring",
        "not_for_scoring": False,
        "metrics":       ["accuracy", "repeatability", "reproducibility",
                          "osipi_silver_score", "osipi_gold_score"],
        # Paths — derived from OSIPI_TF62_DIR
        "provider_dir":  OSIPI_TF62_DIR,
        "script_file":   OSIPI_TF62_DIR / "challengeScoring.py",
        "ref_data_dir":  OSIPI_TF62_DIR / "reference",
        "masks_dir":     OSIPI_TF62_DIR / "masks",
        "setup_note": (
            "Place the following inside "
            "data/scoring/providers/osipi_tf62_dce_ktrans/ to enable scoring:\n"
            "  challengeScoring.py  — from OSIPI/TF6.2_DCE-DSC-MRI_Challenges\n"
            "  reference/           — DRO / reference Ktrans NIfTI maps\n"
            "  masks/               — mask NIfTI files"
        ),
    },

    # ── Development / test-data only ─────────────────────────────────────────
    "osipi_codecollection_dce_testdata": {
        "provider_id":   "osipi_codecollection_dce_testdata",
        "display_name":  "OSIPI CodeCollection Test Data",
        "provider_name": "OSIPI DCE/DSC CodeCollection — Test Data",
        "category":      "development",
        "official":      False,
        "challenge_type": "dce",
        "map_type":      None,
        "description": (
            "CSV pharmacokinetic-model test data from OSIPI/DCE-DSC-MRI_CodeCollection. "
            "For provider-discovery UI testing only. NOT official challenge scoring."
        ),
        "not_for_scoring": True,
        "metrics":       [],
        "provider_dir":  CODECOLLECTION_DIR,
        "test_data_dir": CODECOLLECTION_DIR / "test" / "DCEmodels" / "data",
        "expected_csv_files": [
            "dce_DRO_data_tofts.csv",
            "dce_DRO_data_extended_tofts.csv",
        ],
        "setup_note": (
            "Clone or copy test/DCEmodels/data/ from "
            "github.com/OSIPI/DCE-DSC-MRI_CodeCollection into "
            "data/scoring/providers/osipi_codecollection_dce/test/DCEmodels/data/"
        ),
    },
}


def get_provider_by_id(provider_id: str) -> Optional[dict]:
    """Return a provider dict by its exact provider_id, or None."""
    return PROVIDERS.get(provider_id)


def get_provider(challenge_type: str, map_type: str) -> Optional[dict]:
    """Return the official provider matching challenge + map type, or None.

    Skips development-only (not_for_scoring=True) providers.
    """
    ct = (challenge_type or "").lower().strip()
    mt = (map_type or "").lower().strip()
    for p in PROVIDERS.values():
        if p.get("not_for_scoring"):
            continue
        if p.get("challenge_type") == ct and p.get("map_type") == mt:
            return p
    return None


def _resolve_provider(
    provider_id: Optional[str],
    challenge_type: str,
    map_type: str,
) -> tuple[Optional[dict], str]:
    """Return (provider_dict, error_message).

    Prefers provider_id lookup; falls back to challenge/map lookup.
    Returns (None, error) if nothing matches or provider is dev-only.
    """
    if provider_id:
        p = PROVIDERS.get(provider_id)
        if p is None:
            return None, f"Unknown provider_id: {provider_id!r}"
        if p.get("not_for_scoring"):
            return None, f"Provider {provider_id!r} is a development-only provider and cannot score submissions."
        return p, ""
    p = get_provider(challenge_type, map_type)
    if p is None:
        return None, (
            f"No official scoring provider configured for "
            f"challenge_type={challenge_type!r}, map_type={map_type!r}."
        )
    return p, ""


# ---------------------------------------------------------------------------
# Path helpers — mirror docker_runner._safe_name
# ---------------------------------------------------------------------------

def _safe_name(value: str) -> str:
    """Convert to filesystem-safe lowercase-hyphenated form."""
    safe = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in safe.split("-") if part) or "submission"


def _exec_output_dir(submission_id: str, challenge_type: str) -> Path:
    """Return the outputs/ dir written by execution_service for this submission."""
    key = f"{_safe_name(challenge_type)}_{_safe_name(submission_id)}"
    return OUTPUTS_DIR / "execution" / key / "outputs"


def _score_artifact_dir(submission_id: str) -> Path:
    """Return the directory where scoring artifacts are stored for this submission."""
    d = SCORING_OUTPUTS_DIR / _safe_name(submission_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scoring_result_path(submission_id: str) -> Path:
    SCORING_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    return SCORING_OUTPUTS_DIR / f"{_safe_name(submission_id)}_score.json"


def load_scoring_result(submission_id: str) -> Optional[dict]:
    path = _scoring_result_path(submission_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_scoring_result(submission_id: str, result: dict) -> None:
    try:
        _scoring_result_path(submission_id).write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Infrastructure checks
# ---------------------------------------------------------------------------

def _check_tf62_infrastructure() -> dict:
    """Check TF6.2 provider infrastructure: script + reference NIfTI + masks.

    Does NOT check submission-specific outputs.
    Returns:
        all_present     : bool
        missing         : list[str]
        ref_nifti_count : int
        mask_count      : int
    """
    prov     = PROVIDERS["osipi_tf62_dce_ktrans"]
    script   = prov["script_file"]
    ref_dir  = prov["ref_data_dir"]
    masks_dir = prov["masks_dir"]
    missing: list[str] = []

    if not script.exists():
        missing.append("challengeScoring.py")

    ref_nifti: list[Path] = []
    if ref_dir.exists():
        ref_nifti = [
            f for f in ref_dir.rglob("*")
            if f.suffix in (".nii", ".gz") and f.is_file()
        ]
    if not ref_nifti:
        missing.append("reference_data (NIfTI maps in reference/)")

    # Masks: prefer dedicated masks/ dir, fall back to reference/ search
    mask_files: list[Path] = []
    if masks_dir.exists():
        mask_files = [f for f in masks_dir.rglob("*") if f.is_file()]
    if not mask_files and ref_dir.exists():
        mask_files = [
            f for f in ref_dir.rglob("*")
            if "mask" in f.name.lower() and f.is_file()
        ]
    if not mask_files:
        missing.append("mask_files (NIfTI masks in masks/ or reference/)")

    return {
        "all_present":      len(missing) == 0,
        "missing":          missing,
        "ref_nifti_count":  len(ref_nifti),
        "mask_count":       len(mask_files),
    }


def _check_codecollection_infrastructure() -> dict:
    """Check whether the CodeCollection CSV test data files are present."""
    prov      = PROVIDERS["osipi_codecollection_dce_testdata"]
    data_dir  = prov["test_data_dir"]
    expected  = prov["expected_csv_files"]

    if not data_dir.exists():
        return {
            "all_present":     False,
            "missing":         ["Test data directory not found"],
            "available_files": [],
        }

    available = sorted(f.name for f in data_dir.glob("*.csv"))
    not_found = [f for f in expected if f not in available]

    return {
        "all_present":     (len(not_found) == 0 and len(available) > 0),
        "missing":         not_found or (["No CSV files found"] if not available else []),
        "available_files": available,
    }


# ---------------------------------------------------------------------------
# all_providers_status() — infrastructure snapshot (no submission needed)
# ---------------------------------------------------------------------------

def all_providers_status() -> list[dict]:
    """Return infrastructure-level status for every registered provider."""
    result: list[dict] = []

    # Official provider
    prov  = PROVIDERS["osipi_tf62_dce_ktrans"]
    infra = _check_tf62_infrastructure()
    result.append({
        "provider_id":     prov["provider_id"],
        "provider_name":   prov["provider_name"],
        "display_name":    prov["display_name"],
        "category":        "official",
        "official":        True,
        "not_for_scoring": False,
        "status":          "ready" if infra["all_present"] else "not_configured",
        "message": (
            "All infrastructure requirements met. Ready to score."
            if infra["all_present"] else
            "Scoring script, reference data, or mask files are missing. "
            "See setup_note for instructions."
        ),
        "missing":          infra["missing"],
        "description":      prov["description"],
        "metrics":          prov["metrics"],
        "setup_note":       prov["setup_note"],
        "ref_nifti_count":  infra["ref_nifti_count"],
        "mask_count":       infra["mask_count"],
        "challenge_type":   prov["challenge_type"],
        "map_type":         prov["map_type"],
    })

    # Development provider
    prov = PROVIDERS["osipi_codecollection_dce_testdata"]
    cc   = _check_codecollection_infrastructure()
    result.append({
        "provider_id":     prov["provider_id"],
        "provider_name":   prov["provider_name"],
        "display_name":    prov["display_name"],
        "category":        "development",
        "official":        False,
        "not_for_scoring": True,
        "status":          "dev_data_available" if cc["all_present"] else "not_configured",
        "message": (
            "Development test data available. NOT official challenge scoring."
            if cc["all_present"] else
            "CSV test data not found. See setup_note for instructions. Development provider only."
        ),
        "missing":          cc["missing"],
        "available_files":  cc["available_files"],
        "description":      prov["description"],
        "metrics":          [],
        "setup_note":       prov["setup_note"],
    })

    return result


# ---------------------------------------------------------------------------
# Submission-level prerequisite check (TF6.2 official provider)
# ---------------------------------------------------------------------------

_OSIPI_FNAME_RE = re.compile(
    r"^(Synthetic|Clinical)_P\d+_Visit\d+\.nii(\.gz)?$",
    re.IGNORECASE,
)


def _check_submission_prerequisites(
    submission_id: str,
    challenge_type: str,
    provider: dict,
) -> dict:
    """Check every prerequisite for scoring a single submission.

    Returns:
        all_present  : bool
        missing      : list[str]
        outputs_ready: bool    — execution produced ≥1 NIfTI file
        ktrans_compat: bool    — at least one file matches OSIPI naming pattern
        nifti_files  : list[Path]
    """
    # First: infrastructure
    infra   = _check_tf62_infrastructure()
    missing = list(infra["missing"])

    # Second: submission-specific outputs
    exec_out = _exec_output_dir(submission_id, challenge_type)
    if not exec_out.exists():
        missing.append("Execution outputs (run the submission first)")
        return {
            "all_present":   False,
            "missing":       missing,
            "outputs_ready": False,
            "ktrans_compat": False,
            "nifti_files":   [],
        }

    nifti_files   = [f for f in exec_out.rglob("*") if f.suffix in (".nii", ".gz") and f.is_file()]
    outputs_ready = len(nifti_files) > 0
    if not outputs_ready:
        missing.append("Execution outputs (no NIfTI files found — run the submission first)")

    ktrans_compat = any(_OSIPI_FNAME_RE.match(f.name) for f in nifti_files)
    if outputs_ready and not ktrans_compat:
        missing.append(
            "OSIPI-compatible output filenames "
            "(expected: Synthetic_P#_Visit#.nii or Clinical_P#_Visit#.nii)"
        )

    return {
        "all_present":   len(missing) == 0,
        "missing":       missing,
        "outputs_ready": outputs_ready,
        "ktrans_compat": ktrans_compat,
        "nifti_files":   nifti_files,
    }


# ---------------------------------------------------------------------------
# scoring_status() — per-submission
# ---------------------------------------------------------------------------

def scoring_status(
    submission_id: str,
    challenge_type: str,
    map_type: str,
    output_files: Optional[list[str]] = None,
    provider_id: Optional[str] = None,
) -> dict:
    """Return scoring status for a single submission.

    Never fabricates metric values. Returns status="not_configured" if any
    prerequisite is missing.
    """
    providers_snap = all_providers_status()

    provider, err = _resolve_provider(provider_id, challenge_type, map_type)

    if provider is None:
        exec_dir  = _exec_output_dir(submission_id, challenge_type)
        nifti_out = (
            [f for f in exec_dir.rglob("*") if f.suffix in (".nii", ".gz") and f.is_file()]
            if exec_dir.exists() else []
        )
        return {
            "provider_id":   None,
            "provider_name": "No official provider",
            "status":        "not_configured",
            "message":       err,
            "missing":       [],
            "outputs_ready": len(nifti_out) > 0,
            "outputs_count": len(nifti_out),
            "score_result":  None,
            "providers":     providers_snap,
        }

    pid = provider["provider_id"]

    # Already scored?
    saved = load_scoring_result(submission_id)
    if saved and saved.get("provider_id") == pid:
        exec_dir  = _exec_output_dir(submission_id, challenge_type)
        out_count = (
            len([f for f in exec_dir.rglob("*") if f.suffix in (".nii", ".gz")])
            if exec_dir.exists() else 0
        )
        return {
            "provider_id":   pid,
            "provider_name": provider["provider_name"],
            "status":        saved.get("status", "scored"),
            "message":       saved.get("message", "Scoring complete."),
            "missing":       [],
            "outputs_ready": True,
            "outputs_count": out_count,
            "score_result":  saved,
            "providers":     providers_snap,
        }

    # Prerequisite check
    pre = _check_submission_prerequisites(submission_id, challenge_type, provider)

    exec_dir  = _exec_output_dir(submission_id, challenge_type)
    out_count = (
        len([f for f in exec_dir.rglob("*") if f.suffix in (".nii", ".gz")])
        if exec_dir.exists() else 0
    )

    if not pre["all_present"]:
        return {
            "provider_id":   pid,
            "provider_name": provider["provider_name"],
            "status":        "not_configured",
            "message": (
                "DCE Ktrans scoring requires: the OSIPI TF6.2 scoring script, "
                "reference NIfTI maps, mask files, and correctly named Ktrans outputs."
            ),
            "missing":       pre["missing"],
            "outputs_ready": pre["outputs_ready"],
            "outputs_count": out_count,
            "score_result":  None,
            "providers":     providers_snap,
        }

    return {
        "provider_id":   pid,
        "provider_name": provider["provider_name"],
        "status":        "ready",
        "message":       "All prerequisites met. Ready to score.",
        "missing":       [],
        "outputs_ready": True,
        "outputs_count": out_count,
        "score_result":  None,
        "providers":     providers_snap,
    }


# ---------------------------------------------------------------------------
# score_submission() — run the real scoring script
# ---------------------------------------------------------------------------

def score_submission(
    submission_id: str,
    challenge_type: str,
    map_type: str,
    provider_id: Optional[str] = None,
) -> dict:
    """Run official scoring for a single submission via subprocess.

    Returns a result dict that is also written to
    data/outputs/scoring/{safe_id}_score.json.

    NEVER fabricates metric values. Returns status='not_configured'
    or status='not_ready' if any prerequisite is absent.
    """
    provider, err = _resolve_provider(provider_id, challenge_type, map_type)
    if provider is None:
        return {
            "success":    False,
            "status":     "not_configured",
            "provider_id": provider_id or "none",
            "message":    err,
            "metrics":    {},
            "artifacts":  [],
        }

    pid = provider["provider_id"]
    pre = _check_submission_prerequisites(submission_id, challenge_type, provider)

    if not pre["all_present"]:
        result = {
            "success":       False,
            "submission_id": submission_id,
            "provider_id":   pid,
            "status":        "not_configured",
            "message":       "Prerequisites not met — see missing list.",
            "missing":       pre["missing"],
            "metrics":       {},
            "artifacts":     [],
        }
        _save_scoring_result(submission_id, result)
        return result

    exec_out    = _exec_output_dir(submission_id, challenge_type)
    script      = provider["script_file"]
    ref_dir     = provider["ref_data_dir"]
    masks_dir   = provider["masks_dir"]
    artifact_dir = _score_artifact_dir(submission_id)

    # Build subprocess args.
    # NOTE: Adjust these flag names to match the real challengeScoring.py CLI.
    # The script is called with cwd=provider_dir so relative imports work.
    cmd = [
        sys.executable, str(script),
        "--submission", str(exec_out),
        "--reference",  str(ref_dir),
        "--output",     str(artifact_dir),
    ]
    # Pass masks dir only if it exists (some script versions find masks automatically)
    if masks_dir.exists():
        cmd += ["--masks", str(masks_dir)]

    scored_at = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(provider["provider_dir"]),
            # Explicit env inheritance — no shell=True
        )

        artifacts = _collect_artifacts(artifact_dir)
        metrics   = _parse_metrics_from_artifacts(artifact_dir)

        if proc.returncode != 0:
            result = {
                "success":       False,
                "submission_id": submission_id,
                "provider_id":   pid,
                "challenge_type": challenge_type,
                "map_type":       map_type,
                "status":        "failed",
                "scored_at":     scored_at,
                "message":       "Scoring script exited with a non-zero return code.",
                "stdout":        proc.stdout[:4096],
                "stderr":        proc.stderr[:4096],
                "metrics":       {},
                "artifacts":     artifacts,
                "artifact_count": len(artifacts),
            }
            _save_scoring_result(submission_id, result)
            return result

        result = {
            "success":        True,
            "submission_id":  submission_id,
            "provider_id":    pid,
            "challenge_type": challenge_type,
            "map_type":       map_type,
            "status":         "scored",
            "scored_at":      scored_at,
            "message":        (
                "Scoring complete — metrics parsed."
                if metrics else
                "Scoring complete — artifacts saved. Metrics could not be parsed from output."
            ),
            "stdout":         proc.stdout[:4096],
            "metrics":        metrics,
            "artifacts":      artifacts,
            "artifact_count": len(artifacts),
            "score_dir":      str(artifact_dir),
        }
        _save_scoring_result(submission_id, result)
        return result

    except subprocess.TimeoutExpired:
        result = {
            "success":       False,
            "submission_id": submission_id,
            "provider_id":   pid,
            "status":        "failed",
            "scored_at":     scored_at,
            "message":       "Scoring script timed out after 300 seconds.",
            "metrics":       {},
            "artifacts":     [],
            "artifact_count": 0,
        }
        _save_scoring_result(submission_id, result)
        return result

    except Exception as exc:
        result = {
            "success":       False,
            "submission_id": submission_id,
            "provider_id":   pid,
            "status":        "failed",
            "scored_at":     scored_at,
            "message":       f"Unexpected error while running scoring script: {exc}",
            "metrics":       {},
            "artifacts":     [],
            "artifact_count": 0,
        }
        _save_scoring_result(submission_id, result)
        return result


# ---------------------------------------------------------------------------
# Artifact + metric collection
# ---------------------------------------------------------------------------

def _collect_artifacts(score_dir: Path) -> list[str]:
    """Return a list of artifact filenames produced by the scoring script.

    Looks for JSON, CSV, PNG, PDF files in the output directory.
    Returns relative paths (relative to score_dir) as strings.
    """
    if not score_dir.exists():
        return []
    extensions = {".json", ".csv", ".png", ".pdf", ".txt", ".html"}
    artifacts: list[str] = []
    for f in sorted(score_dir.rglob("*")):
        if f.is_file() and f.suffix.lower() in extensions:
            artifacts.append(str(f.relative_to(score_dir)))
    return artifacts


def _parse_metrics_from_artifacts(score_dir: Path) -> dict:
    """Parse metric values from JSON files written by the scoring script.

    Searches score_dir and its scoringOutputs/ subdirectory.
    Returns an empty dict (never fabricates values) if nothing is found.
    """
    if not score_dir.exists():
        return {}
    metrics: dict = {}
    # Try scoringOutputs/ first (common OSIPI script output convention)
    search_dirs = [score_dir / "scoringOutputs", score_dir]
    for search in search_dirs:
        if not search.exists():
            continue
        for jf in sorted(search.glob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    metrics.update(data)
            except Exception:
                pass
        if metrics:
            break
    return metrics


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def score_batch(
    submission_ids: list[str],
    challenge_type: str,
    map_type: str,
    provider_id: Optional[str] = None,
) -> list[dict]:
    """Score multiple submissions sequentially. One failure does not stop the rest."""
    results = []
    for sid in submission_ids:
        try:
            r = score_submission(sid, challenge_type, map_type, provider_id)
        except Exception as exc:
            r = {
                "success":       False,
                "submission_id": sid,
                "status":        "failed",
                "message":       f"Unexpected error: {exc}",
                "metrics":       {},
                "artifacts":     [],
            }
        r["submission_id"] = sid  # ensure it's always set
        results.append(r)
    return results


def batch_scoring_status(
    submission_ids: list[str],
    challenge_type: str,
    map_type: str,
    provider_id: Optional[str] = None,
) -> dict:
    """Aggregated scoring status for a list of submissions, plus provider snapshot."""
    results = []
    for sid in submission_ids:
        st = scoring_status(sid, challenge_type, map_type, provider_id=provider_id)
        st["submission_id"] = sid
        results.append(st)

    providers_snap = all_providers_status()
    provider, _    = _resolve_provider(provider_id, challenge_type, map_type)

    outputs_ready   = sum(1 for r in results if r.get("outputs_ready"))
    ready_to_score  = sum(1 for r in results if r.get("status") == "ready")
    scored_count    = sum(1 for r in results if r.get("status") == "scored")
    failed_count    = sum(1 for r in results if r.get("status") == "failed")

    return {
        "provider_id":     provider["provider_id"]   if provider else None,
        "provider_name":   provider["provider_name"] if provider else "No official provider",
        "total":           len(submission_ids),
        "outputs_ready":   outputs_ready,
        "ready_to_score":  ready_to_score,
        "scored":          scored_count,
        "failed":          failed_count,
        "results":         results,
        "providers":       providers_snap,
    }
