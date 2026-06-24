"""File-level validation for an ingested submission.

Accepts a submission_id, resolves the folder path internally, and returns
structured results. Errors and warnings are dicts with the same shape as the
CLI validation package: {severity, code, message, path}.
Results are saved to data/outputs/validation/ for the Outputs page.
"""

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR
from services.ingest_service import detect_submission_metadata, make_safe_id

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

NIFTI_SUFFIXES = (".nii", ".nii.gz")

EXPECTED_MAPS: Dict[str, tuple] = {
    "dce": ("ktrans", "kep", "vp"),
    "asl": ("cbf", "att"),
    "dsc": ("cbv", "cbf", "mtt"),
}

MAP_TYPE_PATTERNS: Dict[str, tuple] = {
    "cbf":    ("cbf", "cerebral_blood_flow", "perfmap", "perfusion", "perf"),
    "ktrans": ("ktrans", "k_trans", "transfer_constant"),
    "att":    ("att", "arterial_transit_time", "attmap"),
    "kep":    ("kep", "k_ep", "rate_constant"),
    "vp":     ("vp", "v_p", "plasma_volume"),
    "cbv":    ("cbv", "cerebral_blood_volume"),
    "mtt":    ("mtt", "mean_transit_time"),
}

# All map type labels that detect_submission_metadata() can return (title-cased)
KNOWN_AUTO_DETECTED = frozenset({
    "CBF", "Ktrans", "ATT", "Kep", "Vp", "CBV", "MTT", "Mixed/Other",
})

CODE_FILE_NAMES = {
    "dockerfile", "requirements.txt", "environment.yml",
    "main.py", "run.py", "setup.py",
}
CODE_EXTENSIONS = {".py", ".sh", ".m", ".r", ".ipynb", ".jl"}
CODE_FOLDER_NAMES = {"scripts", "code", "src"}

README_NAMES = {"readme.md", "readme.txt", "readme", "sop.pdf", "metadata.json"}

KNOWN_CHALLENGE_TYPES = {"asl", "dce", "dsc"}

# Output subdirectory — matches the CLI package.
VALIDATION_SUBDIR = OUTPUTS_DIR / "validation"


# ---------------------------------------------------------------------------
# Issue helpers
# ---------------------------------------------------------------------------

def _err(code: str, message: str, path: str = "") -> Dict:
    return {"severity": "error", "code": code, "message": message, "path": path or None}


def _warn(code: str, message: str, path: str = "") -> Dict:
    return {"severity": "warning", "code": code, "message": message, "path": path or None}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def validate_submission(
    submission_id: str,
    challenge_type: str = "dce",
    expected_nifti_count: Optional[int] = None,
    expected_nifti_count_mode: Optional[str] = None,
    include_code: Optional[str] = None,
    include_readme: Optional[str] = None,
    team_name: Optional[str] = None,
    contact_email: Optional[str] = None,
    map_type: Optional[str] = None,
    map_type_mode: Optional[str] = None,
    notes: Optional[str] = None,
    mode: str = "auto",
) -> Dict:
    """Validate the submission folder for the given submission_id.

    Args:
        mode: ``"auto"`` (default) — detects whether the submission is
              result-only or reproducible based on its contents.
              ``"result_only"`` — expects NIfTI output maps; run instructions
              are non-blocking (result maps only, no Docker).
              ``"result_validation"`` — alias for ``"result_only"``.
              ``"reproducible"`` — does not require output maps; focuses on
              executability via Dockerfile.
    """
    # Normalise challenge type first so it is available for all early-exit paths.
    normalized_challenge = (challenge_type or "dce").lower()

    # Sanitize submission_id — block path traversal and unsafe characters
    safe_submission_id = make_safe_id(submission_id)
    folder = EXTRACTED_DIR / safe_submission_id

    # Extra guard: ensure the resolved path stays inside EXTRACTED_DIR
    try:
        folder.resolve().relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        return _finish(
            safe_submission_id, normalized_challenge,
            [_err("INVALID_SUBMISSION_ID", "Submission ID contains an invalid path.")],
            [], 0, 0, team_name, contact_email, map_type, notes,
        )

    submission_id = safe_submission_id  # use sanitized ID for the rest of the function
    errors: List[Dict] = []
    warnings: List[Dict] = []

    if normalized_challenge not in KNOWN_CHALLENGE_TYPES:
        errors.append(_err(
            "UNKNOWN_CHALLENGE_TYPE",
            f"Challenge type '{challenge_type}' is not recognised. Use asl, dce, or dsc.",
        ))

    if not folder.exists() or not folder.is_dir():
        errors.append(_err(
            "SUBMISSION_FOLDER_MISSING",
            "Submission files were not found. Please re-upload your ZIP file.",
            str(folder),
        ))
        return _finish(submission_id, normalized_challenge, errors, warnings, 0, 0,
                       team_name, contact_email, map_type, notes)

    all_files: List[Path] = [f for f in folder.rglob("*") if f.is_file()]
    if not all_files:
        errors.append(_err("SUBMISSION_FOLDER_EMPTY", "The submission folder is empty.", str(folder)))
        return _finish(submission_id, normalized_challenge, errors, warnings, 0, 0,
                       team_name, contact_email, map_type, notes)

    # ── Auto mode: detect whether this is result-only or reproducible ──────────
    if mode in ("auto", ""):
        _has_docker_early = _has_docker(all_files)
        _has_nifti_early  = any(f.name.lower().endswith(NIFTI_SUFFIXES) for f in all_files)
        if _has_docker_early:
            mode = "reproducible"
        elif _has_nifti_early:
            mode = "result_only"
        else:
            # Neither NIfTI maps nor run instructions — will produce natural errors
            mode = "result_only"

    # Normalise aliases
    if mode == "result_validation":
        mode = "result_only"

    detection = detect_submission_metadata(submission_id)
    if (expected_nifti_count_mode or "").lower() == "auto":
        expected_nifti_count = detection.get("nifti_count", 0)

    effective_map_type = map_type
    if (map_type_mode or "").lower() == "auto" or (map_type or "").lower() == "auto":
        detected = detection.get("detected_parameter_map_type", "Unknown")
        if detected in KNOWN_AUTO_DETECTED:
            effective_map_type = detected
        else:
            effective_map_type = ""
            warnings.append(_warn(
                "MAP_TYPE_UNDETECTED",
                "Parameter map type could not be auto-detected. Select it manually if needed.",
            ))
        if detected == "Mixed/Other":
            warnings.append(_warn(
                "MAP_TYPE_MIXED",
                "Multiple parameter map types detected. Treating as Mixed/Other.",
            ))

    # ---- NIfTI files --------------------------------------------------------

    nifti_files: List[Path] = [
        f for f in all_files if f.name.lower().endswith(NIFTI_SUFFIXES)
    ]
    reproducible = mode == "reproducible"
    result_only  = mode == "result_only"

    if not nifti_files:
        if reproducible:
            # In reproducible mode output maps will be generated by execution — not an error.
            warnings.append(_warn(
                "NO_EXISTING_OUTPUT_MAPS",
                "No output maps found in the package yet. "
                "Maps will be generated when the submission is run.",
                str(folder),
            ))
        else:
            # result_only mode: NIfTI maps are required
            errors.append(_err(
                "NO_NIFTI_FILES",
                "No .nii or .nii.gz parameter map files were found. "
                "Add your output maps or include a Dockerfile for reproducible execution.",
                str(folder),
            ))
    else:
        actual = len(nifti_files)

        if expected_nifti_count is not None and actual != expected_nifti_count:
            warnings.append(_warn(
                "NIFTI_COUNT_MISMATCH",
                f"Found {actual} NIfTI file(s), but {expected_nifti_count} were expected.",
                str(folder),
            ))

        if not reproducible:
            # Map-name checks only make sense when pre-existing maps are present.
            joined = " ".join(f.name.lower() for f in nifti_files)
            selected_map = (effective_map_type or "").lower()
            selected_patterns = MAP_TYPE_PATTERNS.get(selected_map)
            if selected_patterns is not None:
                expected_groups = [(selected_map, selected_patterns)]
            elif selected_map not in {"", "auto", "other", "mixed/other"}:
                expected_groups = [(selected_map, (selected_map,))]
            else:
                expected_groups = [
                    (name, MAP_TYPE_PATTERNS.get(name, (name,)))
                    for name in EXPECTED_MAPS.get(normalized_challenge, ())
                ]

            for label, patterns in expected_groups:
                if not any(pattern in joined for pattern in patterns):
                    warnings.append(_warn(
                        "EXPECTED_MAP_MISSING",
                        f"Expected {label.upper()} parameter map was not found.",
                        str(folder),
                    ))

        for f in nifti_files:
            if f.stat().st_size == 0:
                warnings.append(_warn(
                    "EMPTY_NIFTI_FILE",
                    f"NIfTI file appears to be empty: {f.name}",
                    str(f),
                ))

    # ---- README / SOP -------------------------------------------------------

    readme_found = _has_readme(all_files)

    if include_readme == "yes" and not readme_found:
        errors.append(_err(
            "NO_README_OR_METADATA",
            "A README or SOP was marked as included, but none was found. "
            "Add README.md, README.txt, SOP.pdf, or metadata.json.",
            str(folder),
        ))
    elif not readme_found:
        warnings.append(_warn(
            "README_MISSING",
            "No README or SOP file was found. Consider adding one.",
            str(folder),
        ))

    # ---- Run instructions / code files ---------------------------------------

    has_run_instructions = _has_docker(all_files)
    has_dockerfile = any(f.name == "Dockerfile" for f in all_files)

    if reproducible:
        if not has_run_instructions:
            errors.append(_err(
                "NO_RUN_INSTRUCTIONS",
                "No run instructions found (Dockerfile or docker-compose file). "
                "This package cannot be executed automatically.",
                str(folder),
            ))
        # In reproducible mode, missing code is a warning only
        if include_code == "yes" and not _has_code(all_files):
            warnings.append(_warn(
                "NO_CODE_FILES",
                "Code files were marked as included, but none were found.",
                str(folder),
            ))
    else:
        # result_only mode: no run instructions is expected and non-blocking
        if not has_run_instructions:
            warnings.append(_warn(
                "NO_RUN_INSTRUCTIONS",
                "This submission contains result maps only and cannot be run automatically. "
                "Add a Dockerfile to enable reproducible execution.",
                str(folder),
            ))
        if include_code == "yes" and not _has_code(all_files):
            warnings.append(_warn(
                "NO_CODE_FILES",
                "Code files were marked as included, but none were found. "
                "Expected: Dockerfile, .py / .sh / .m files, or a scripts/ folder.",
                str(folder),
            ))

    # ---- Duplicate filenames ------------------------------------------------

    seen: Dict[str, List[Path]] = {}
    for f in all_files:
        seen.setdefault(f.name.lower(), []).append(f)
    for fname, matches in seen.items():
        if len(matches) > 1:
            warnings.append(_warn(
                "DUPLICATE_FILENAME",
                f"Filename appears more than once: {fname}",
                ", ".join(str(m) for m in matches),
            ))

    blocking_errors = [e for e in errors if e["code"] != "UNKNOWN_CHALLENGE_TYPE"]
    runnable = has_run_instructions and len(blocking_errors) == 0

    # run_readiness: "runnable" | "result_only" | "not_runnable"
    if runnable:
        run_readiness = "runnable"
    elif mode == "result_only" and len(blocking_errors) == 0:
        run_readiness = "result_only"
    else:
        run_readiness = "not_runnable"

    return _finish(
        submission_id, normalized_challenge, errors, warnings,
        len(nifti_files) if nifti_files else 0,
        len(all_files),
        team_name, contact_email, effective_map_type, notes,
        has_dockerfile=has_dockerfile,
        has_run_instructions=has_run_instructions,
        runnable=runnable,
        run_readiness=run_readiness,
        mode=mode,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _finish(
    submission_id: str,
    challenge_type: str,
    errors: List[Dict],
    warnings: List[Dict],
    nifti_count: int,
    total_files: int,
    team_name: Optional[str],
    contact_email: Optional[str],
    map_type: Optional[str],
    notes: Optional[str],
    has_dockerfile: bool = False,
    has_run_instructions: bool = False,
    runnable: bool = False,
    run_readiness: str = "not_runnable",
    mode: str = "result_only",
) -> Dict:
    now = datetime.now(timezone.utc).isoformat()
    result: Dict = {
        "submission_id": submission_id,
        "team_name": team_name or "",
        "contact_email": contact_email or "",
        "challenge_type": challenge_type.upper(),
        "map_type": map_type or "",
        "notes": notes or "",
        "mode": mode,
        "checked_at": now,
        "validated_at": now,  # kept for JS compatibility
        "passed": len(errors) == 0,
        "runnable": runnable,
        "run_readiness": run_readiness,
        "has_run_instructions": has_run_instructions,
        "errors": errors,
        "warnings": warnings,
        "nifti_count": nifti_count,
        "total_files": total_files,
        "has_dockerfile": has_dockerfile,
    }
    _save_result(submission_id, result)
    return result


def _save_result(submission_id: str, result: Dict) -> None:
    VALIDATION_SUBDIR.mkdir(parents=True, exist_ok=True)
    out_file = VALIDATION_SUBDIR / f"{submission_id}_validation.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _has_readme(files: List[Path]) -> bool:
    return any(f.name.lower() in README_NAMES for f in files)


def _has_docker(files: List[Path]) -> bool:
    """Return True only when an actual Dockerfile or docker-compose file is present.

    ``.dockerignore`` alone does NOT satisfy this check — it is meaningless
    without a corresponding Dockerfile.
    """
    return any(
        f.name.lower() == "dockerfile"
        or f.name.lower().startswith("docker-compose")
        for f in files
    )


def _has_code(files: List[Path]) -> bool:
    file_names = {f.name.lower() for f in files}
    if file_names & CODE_FILE_NAMES:
        return True
    for f in files:
        if f.suffix.lower() in CODE_EXTENSIONS:
            return True
        if any(part.lower() in CODE_FOLDER_NAMES for part in f.parts):
            return True
    return False


# ---------------------------------------------------------------------------
# Preflight check (reproducible mode) — is this submission runnable?
# ---------------------------------------------------------------------------


def preflight_check(
    submission_id: str,
    challenge_type: str = "dce",
    team_name: Optional[str] = None,
    contact_email: Optional[str] = None,
) -> Dict:
    """Check whether a submission is ready to execute without requiring output maps.

    Returns a lightweight dict with:
      ``runnable``               — True if execution can be attempted.
      ``has_run_instructions``   — Dockerfile or docker-compose found.
      ``run_instructions_path``  — relative path to the Dockerfile, or "".
      ``has_run_config``         — run_config.json found (uses default cmd if absent).
      ``has_existing_maps``      — NIfTI files already present (informational).
      ``existing_map_count``     — how many NIfTI files exist before execution.
      ``errors``                 — blocking issues (not runnable).
      ``warnings``               — advisory issues (runnable but note these).
    """
    normalized_challenge = (challenge_type or "dce").lower()
    safe_id = make_safe_id(submission_id)
    folder  = EXTRACTED_DIR / safe_id

    now     = datetime.now(timezone.utc).isoformat()
    errors: List[Dict]   = []
    warnings: List[Dict] = []

    # Path safety guard
    try:
        folder.resolve().relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        return {
            "submission_id": safe_id, "runnable": False,
            "has_run_instructions": False, "run_instructions_path": "",
            "has_run_config": False, "has_existing_maps": False, "existing_map_count": 0,
            "challenge_type": normalized_challenge.upper(),
            "team_name": team_name or "", "contact_email": contact_email or "",
            "checked_at": now, "errors": [_err("INVALID_SUBMISSION_ID", "Invalid submission ID.")],
            "warnings": [],
        }

    if not folder.exists() or not folder.is_dir():
        return {
            "submission_id": safe_id, "runnable": False,
            "has_run_instructions": False, "run_instructions_path": "",
            "has_run_config": False, "has_existing_maps": False, "existing_map_count": 0,
            "challenge_type": normalized_challenge.upper(),
            "team_name": team_name or "", "contact_email": contact_email or "",
            "checked_at": now,
            "errors": [_err("SUBMISSION_FOLDER_MISSING", "Submission folder not found.")],
            "warnings": [],
        }

    all_files: List[Path] = [f for f in folder.rglob("*") if f.is_file()]

    # -- Run instructions (Dockerfile) --
    dockerfiles = [f for f in all_files if f.name == "Dockerfile"]
    has_run_instructions = bool(dockerfiles)
    run_instructions_path = ""

    if not dockerfiles:
        errors.append(_err(
            "NO_RUN_INSTRUCTIONS",
            "No run instructions found. A Dockerfile is required to run this submission automatically.",
        ))
    elif len(dockerfiles) > 1:
        paths = ", ".join(str(d.relative_to(folder)) for d in sorted(dockerfiles))
        errors.append(_err(
            "MULTIPLE_DOCKERFILES",
            f"Multiple Dockerfiles found: {paths}. Keep exactly one Dockerfile.",
        ))
        has_run_instructions = False
    else:
        run_instructions_path = str(dockerfiles[0].relative_to(folder))

    # -- run_config.json --
    has_run_config = any(f.name == "run_config.json" for f in all_files)
    if not has_run_config:
        warnings.append(_warn(
            "NO_RUN_CONFIG",
            "No run_config.json found. The default command (python3 run.py) will be used.",
        ))

    # -- Existing output maps (informational) --
    nifti_files = [f for f in all_files if f.name.lower().endswith(NIFTI_SUFFIXES)]
    has_existing_maps = bool(nifti_files)

    # -- README --
    if not _has_readme(all_files):
        warnings.append(_warn("README_MISSING", "No README or SOP file found. Consider adding one."))

    runnable = has_run_instructions and not errors

    return {
        "submission_id":        safe_id,
        "runnable":             runnable,
        "has_run_instructions": has_run_instructions,
        "run_instructions_path": run_instructions_path,
        "has_run_config":       has_run_config,
        "has_existing_maps":    has_existing_maps,
        "existing_map_count":   len(nifti_files),
        "total_files":          len(all_files),
        "challenge_type":       normalized_challenge.upper(),
        "team_name":            team_name or "",
        "contact_email":        contact_email or "",
        "checked_at":           now,
        "errors":               errors,
        "warnings":             warnings,
    }


# ---------------------------------------------------------------------------
# Post-execution output validation
# ---------------------------------------------------------------------------


def validate_generated_outputs(
    output_dir: Path,
    challenge_type: str = "dce",
    map_type: Optional[str] = None,
) -> Dict:
    """Validate NIfTI files generated by Docker execution (from /output).

    Applies NIfTI-level checks (presence, map names, duplicate filenames,
    zero-byte files) to the output directory produced by running a submission.
    Does **not** require a submission_id — it operates directly on a directory.

    Returns:
        ``passed``, ``nifti_count``, ``output_files``, ``errors``, ``warnings``
    """
    normalized_challenge = (challenge_type or "dce").lower()
    errors: List[Dict]   = []
    warnings: List[Dict] = []

    if not output_dir.exists() or not output_dir.is_dir():
        return {
            "passed": False,
            "nifti_count": 0,
            "output_files": [],
            "errors": [_err("OUTPUT_DIR_MISSING", "Output directory was not created during execution.")],
            "warnings": [],
        }

    all_files = [f for f in output_dir.rglob("*") if f.is_file()]
    output_files_rel = sorted(str(f.relative_to(output_dir)) for f in all_files)
    nifti_files = [f for f in all_files if f.name.lower().endswith(NIFTI_SUFFIXES)]

    if not nifti_files:
        errors.append(_err(
            "NO_GENERATED_NIFTI",
            "The submission did not produce any NIfTI output files (.nii or .nii.gz).",
        ))
    else:
        joined = " ".join(f.name.lower() for f in nifti_files)
        selected_map = (map_type or "").lower()
        selected_patterns = MAP_TYPE_PATTERNS.get(selected_map)
        if selected_patterns is not None:
            expected_groups = [(selected_map, selected_patterns)]
        elif selected_map not in {"", "auto", "other", "mixed/other"}:
            expected_groups = [(selected_map, (selected_map,))]
        else:
            expected_groups = [
                (name, MAP_TYPE_PATTERNS.get(name, (name,)))
                for name in EXPECTED_MAPS.get(normalized_challenge, ())
            ]

        for label, patterns in expected_groups:
            if not any(pattern in joined for pattern in patterns):
                warnings.append(_warn(
                    "EXPECTED_MAP_MISSING",
                    f"Expected {label.upper()} map not found in generated outputs.",
                ))

        for f in nifti_files:
            if f.stat().st_size == 0:
                warnings.append(_warn(
                    "EMPTY_NIFTI_FILE",
                    f"Generated NIfTI file appears to be empty or zero-byte: {f.name}",
                    str(f),
                ))

    # Duplicate filenames in output
    seen: Dict[str, int] = {}
    for f in all_files:
        seen[f.name.lower()] = seen.get(f.name.lower(), 0) + 1
    for fname, count in seen.items():
        if count > 1:
            warnings.append(_warn("DUPLICATE_OUTPUT_FILENAME", f"Duplicate output filename: {fname}"))

    return {
        "passed":       len(errors) == 0,
        "nifti_count":  len(nifti_files),
        "output_files": output_files_rel,
        "errors":       errors,
        "warnings":     warnings,
    }


# ---------------------------------------------------------------------------
# Batch validation
# ---------------------------------------------------------------------------


def validate_batch(
    submission_ids: List[str],
    challenge_type: str = "dce",
    map_type: Optional[str] = None,
    map_type_mode: Optional[str] = None,
    notes: Optional[str] = None,
    team_names: Optional[Dict] = None,
    contact_emails: Optional[Dict] = None,
    mode: str = "result_validation",
) -> Dict:
    """Validate multiple submissions and return an aggregate batch result.

    Each submission is validated independently.  Failures in one submission do
    not prevent the remaining ones from being validated.
    """
    team_names    = team_names    or {}
    contact_emails = contact_emails or {}

    results: List[Dict] = []
    for sid in submission_ids:
        try:
            result = validate_submission(
                sid,
                challenge_type=challenge_type,
                map_type=map_type,
                map_type_mode=map_type_mode,
                notes=notes,
                team_name=team_names.get(sid),
                contact_email=contact_emails.get(sid),
                mode=mode,
            )
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat()
            result = {
                "submission_id": sid,
                "team_name":     team_names.get(sid, ""),
                "contact_email": contact_emails.get(sid, ""),
                "challenge_type": challenge_type.upper(),
                "map_type":      "",
                "mode":          mode,
                "passed":        False,
                "runnable":      False,
                "run_readiness": "not_runnable",
                "has_run_instructions": False,
                "errors":        [_err("BATCH_ERROR", f"Validation failed unexpectedly: {exc}")],
                "warnings":      [],
                "nifti_count":   0,
                "total_files":   0,
                "validated_at":  now,
                "checked_at":    now,
            }
        results.append(result)

    now = datetime.now(timezone.utc).isoformat()
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"

    summary: Dict = {
        "batch_id":         batch_id,
        "challenge_type":   challenge_type.upper(),
        "mode":             mode,
        "submission_count": len(results),
        "passed_count":     sum(1 for r in results if r.get("passed")),
        "failed_count":     sum(1 for r in results if not r.get("passed")),
        "runnable_count":   sum(1 for r in results if r.get("runnable")),
        "validated_at":     now,
        "results":          results,
    }
    _save_batch_result(batch_id, summary)
    return summary


def find_batch_result(batch_id: str) -> Optional[Dict]:
    """Load a saved batch validation result by ID, or return None if not found."""
    safe_id = batch_id.replace("/", "_").replace("\\", "_")
    batch_file = VALIDATION_SUBDIR / f"{safe_id}_batch.json"
    if not batch_file.exists():
        return None
    try:
        return json.loads(batch_file.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_batch_result(batch_id: str, result: Dict) -> None:
    VALIDATION_SUBDIR.mkdir(parents=True, exist_ok=True)
    out_file = VALIDATION_SUBDIR / f"{batch_id}_batch.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")
