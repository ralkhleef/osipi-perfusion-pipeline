"""File-level validation for an ingested submission.

Accepts a submission_id, resolves the folder path internally, and returns
structured results. Errors and warnings are dicts with the same shape as the
CLI validation package: {severity, code, message, path}.
Results are saved to data/outputs/validation/ for the Outputs page.
"""

import json
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import logging
from pathlib import Path
from typing import Dict, List, Optional

from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR
from services.ingest_service import detect_submission_metadata, make_safe_id
from osipi_pipeline.ingestion.manifest import (
    load_manifest,
    manifest_files,
    refresh_manifest,
)
from osipi_pipeline.ingestion.models import IdentityConflict, SubmissionArtifact
from osipi_pipeline.validation.validate import duplicate_filename_groups
from osipi_pipeline.validation.completeness import (
    suppressed_legacy_map_ids,
    validate_completeness,
)
from osipi_pipeline.performance import (
    configured_worker_limit,
    finish_job,
    start_job,
    timed,
    update_job,
)

# ---------------------------------------------------------------------------
# Optional nibabel NIfTI readability check
# ---------------------------------------------------------------------------

# Locate the pipeline package regardless of working directory.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC_DIR   = _REPO_ROOT / "src"
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from osipi_pipeline.config.rules import (
    challenge_types,
    default_challenge_type,
    expected_maps_by_challenge,
    known_auto_detected_labels,
    map_type_specs,
    map_type_patterns,
    tuple_setting,
)

try:
    from osipi_pipeline.validation.nifti_validator import validate_nifti_files as _validate_nifti_files
    _NIFTI_VALIDATOR_AVAILABLE = True
except ImportError:
    _NIFTI_VALIDATOR_AVAILABLE = False
    _validate_nifti_files = None  # type: ignore[assignment]


def _run_nifti_validation(
    nifti_files: List[Path],
    *,
    quick: bool = False,
    force_refresh: bool = False,
    workers: Optional[int] = None,
) -> tuple:
    """Run nibabel readability checks on non-empty NIfTI files.

    Returns (errors, warnings, nifti_summary) as lists. If nibabel is not
    installed the function returns empty lists and no summary so that
    validation can still complete without a hard dependency.
    """
    errors: List[Dict] = []
    warnings: List[Dict] = []
    summary: List[Dict] = []

    if not _NIFTI_VALIDATOR_AVAILABLE or _validate_nifti_files is None:
        warnings.append(_warn(
            "NIFTI_VALIDATION_SKIPPED",
            "nibabel is not installed; NIfTI readability checks were skipped.",
        ))
        return errors, warnings, summary

    non_empty = [f for f in nifti_files if f.stat().st_size > 0]
    if not non_empty:
        return errors, warnings, summary

    try:
        results = _validate_nifti_files(
            non_empty,
            quick=quick,
            force_refresh=force_refresh,
            workers=workers,
        )
    except Exception as exc:
        warnings.append(_warn("NIFTI_VALIDATION_ERROR", f"NIfTI check failed unexpectedly: {exc}"))
        return errors, warnings, summary

    for r in results:
        summary.append(r)
        if not r.get("valid"):
            for msg in r.get("errors", []):
                errors.append(_err("NIFTI_UNREADABLE", str(msg), r.get("file_path", "")))
        for msg in r.get("warnings", []):
            warnings.append(_warn("NIFTI_WARNING", str(msg), r.get("file_path", "")))

    return errors, warnings, summary

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")
CODE_FILE_NAMES = set(tuple_setting("code_file_names"))
CODE_EXTENSIONS = set(tuple_setting("code_extensions"))
CODE_FOLDER_NAMES = set(tuple_setting("code_folder_names"))
README_NAMES = set(tuple_setting("readme_names"))


def _expected_maps() -> Dict[str, tuple]:
    return expected_maps_by_challenge()


def _map_type_patterns() -> Dict[str, tuple]:
    return map_type_patterns()


def _known_auto_detected() -> frozenset[str]:
    return known_auto_detected_labels()


def _known_challenge_types() -> tuple[str, ...]:
    return tuple(challenge_types())


def _default_challenge_type() -> str:
    return default_challenge_type()


def _map_display_name(map_id: str) -> str:
    spec = map_type_specs().get(str(map_id).lower(), {})
    return str(spec.get("display") or map_id)

def _challenge_help() -> str:
    return ", ".join(_known_challenge_types())

def _suffix_help(suffixes: tuple[str, ...]) -> str:
    return ", ".join(suffixes) if suffixes else "configured NIfTI suffixes"

# Output subdirectory — matches the CLI package.
VALIDATION_SUBDIR = OUTPUTS_DIR / "validation"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Issue helpers
# ---------------------------------------------------------------------------

def _err(code: str, message: str, path: str = "") -> Dict:
    return {"severity": "error", "code": code, "message": message, "path": path or None}


def _warn(code: str, message: str, path: str = "") -> Dict:
    return {"severity": "warning", "code": code, "message": message, "path": path or None}


def _completeness_issues(folder: Path, challenge: str) -> List[Dict]:
    """Structural completeness issues from the normalized manifest artifacts.

    Reads the manifest that was just refreshed rather than walking the tree
    again. Any failure here is non-fatal: completeness is additive, and a
    manifest problem is already reported by the surrounding checks.
    """
    try:
        manifest = load_manifest(
            folder, refresh_if_stale=False, challenge_type=challenge
        ) or {}
        artifacts = [
            SubmissionArtifact(**item)
            for item in manifest.get("artifacts", [])
            if isinstance(item, dict)
        ]
        conflicts = [
            IdentityConflict(**item)
            for item in manifest.get("identity_conflicts", [])
            if isinstance(item, dict)
        ]
    except Exception:
        logger.exception("Could not read normalized artifacts for %s", folder)
        return []
    return validate_completeness(
        artifacts, challenge=challenge, identity_conflicts=conflicts
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def validate_submission(
    submission_id: str,
    challenge_type: str | None = None,
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
    qc_mode: str = "deep",
    force_validation_refresh: bool = False,
    nifti_validation_workers: Optional[int] = None,
    job_id: Optional[str] = None,
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
    job_id = job_id or start_job("validation", total=1, stage="starting", key=submission_id)
    quick_qc = (qc_mode or "deep").strip().lower() == "quick"
    # Normalise challenge type first so it is available for all early-exit paths.
    normalized_challenge = (challenge_type or _default_challenge_type()).lower()

    # Sanitize submission_id — block path traversal and unsafe characters
    safe_submission_id = make_safe_id(submission_id)
    folder = EXTRACTED_DIR / safe_submission_id

    # Extra guard: ensure the resolved path stays inside EXTRACTED_DIR
    try:
        folder.resolve().relative_to(EXTRACTED_DIR.resolve())
    except ValueError:
        result = _finish(
            safe_submission_id, normalized_challenge,
            [_err("INVALID_SUBMISSION_ID", "Submission ID contains an invalid path.")],
            [], 0, 0, team_name, contact_email, map_type, notes,
        )
        result["job_id"] = job_id
        finish_job(job_id, error="Invalid submission ID.")
        return result

    submission_id = safe_submission_id  # use sanitized ID for the rest of the function
    errors: List[Dict] = []
    warnings: List[Dict] = []

    if normalized_challenge not in _known_challenge_types():
        errors.append(_err(
            "UNKNOWN_CHALLENGE_TYPE",
            f"Challenge type '{challenge_type}' is not recognised. Use one of: {_challenge_help()}.",
        ))

    if not folder.exists() or not folder.is_dir():
        errors.append(_err(
            "SUBMISSION_FOLDER_MISSING",
            "Submission files were not found. Please re-upload your ZIP file.",
            str(folder),
        ))
        result = _finish(submission_id, normalized_challenge, errors, warnings, 0, 0,
                         team_name, contact_email, map_type, notes)
        result["job_id"] = job_id
        finish_job(job_id, error="Submission folder missing.")
        return result

    update_job(job_id, stage="scanning files")
    with timed("validation.manifest.load", submission_id=submission_id):
        all_files: List[Path] = manifest_files(
            folder,
            refresh_if_stale=True,
            submission_id=submission_id,
            challenge_type=normalized_challenge,
        )
    # Completeness runs off the normalized artifacts the manifest already
    # built, so map detection is not repeated here and filenames are never
    # re-parsed. Returns nothing for challenges without the new config.
    completeness_issues = _completeness_issues(folder, normalized_challenge)
    if not all_files:
        errors.append(_err("SUBMISSION_FOLDER_EMPTY", "The submission folder is empty.", str(folder)))
        result = _finish(submission_id, normalized_challenge, errors, warnings, 0, 0,
                         team_name, contact_email, map_type, notes)
        result["job_id"] = job_id
        finish_job(job_id, error="Submission folder empty.")
        return result

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
        if detected in _known_auto_detected():
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

    nifti_summary: List[Dict] = []

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
                f"No parameter map files were found ({_suffix_help(NIFTI_SUFFIXES)}). "
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
            patterns_by_map = _map_type_patterns()
            expected_maps = _expected_maps()
            selected_map = (effective_map_type or "").lower()
            selected_patterns = patterns_by_map.get(selected_map)
            if selected_patterns is not None:
                expected_groups = [(selected_map, selected_patterns)]
            elif selected_map not in {"", "auto", "other", "mixed/other"}:
                expected_groups = [(selected_map, (selected_map,))]
            else:
                expected_groups = [
                    (name, patterns_by_map.get(name, (name,)))
                    for name in expected_maps.get(normalized_challenge, ())
                ]

            # Challenges that declare required_maps/optional_maps get precise
            # per-scan errors from the completeness checker instead. Emitting
            # this warning too would either duplicate the error or, worse,
            # warn about a map the configuration marks optional.
            suppressed = suppressed_legacy_map_ids(normalized_challenge)
            for label, patterns in expected_groups:
                if label in suppressed:
                    continue
                if not any(pattern in joined for pattern in patterns):
                    warnings.append(_warn(
                        "EXPECTED_MAP_MISSING",
                        f"Expected {_map_display_name(label)} parameter map was not found.",
                        str(folder),
                    ))

        for f in nifti_files:
            if f.stat().st_size == 0:
                warnings.append(_warn(
                    "EMPTY_NIFTI_FILE",
                    f"NIfTI file appears to be empty: {f.name}",
                    str(f),
                ))

        # ---- nibabel readability check (non-empty files only) ---------------
        update_job(job_id, stage="validating NIfTI files", total=len(nifti_files))
        nib_errors, nib_warnings, nifti_summary = _run_nifti_validation(
            nifti_files,
            quick=quick_qc,
            force_refresh=force_validation_refresh,
            workers=nifti_validation_workers,
        )
        errors.extend(nib_errors)
        warnings.extend(nib_warnings)

    # ---- README / SOP -------------------------------------------------------

    readme_found = _has_readme(all_files)

    if include_readme == "yes" and not readme_found:
        errors.append(_err(
            "NO_README_OR_METADATA",
            "A README or SOP was marked as included, but none was found. "
            f"Add one of the configured README/metadata files: {', '.join(sorted(README_NAMES))}.",
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
                (
                    "Code files were marked as included, but none were found. "
                    "Expected one of the configured code indicators: "
                    f"{', '.join(sorted(CODE_FILE_NAMES | CODE_EXTENSIONS | CODE_FOLDER_NAMES))}."
                ),
                str(folder),
            ))

    # ---- Duplicate filenames ------------------------------------------------

    # Scoped by scan identity, not bare basename: the DCE-2026 layout reuses
    # standard filenames in every scan directory by design. Shared with the
    # library validator so the two cannot drift apart.
    for fname, matches in duplicate_filename_groups(all_files, folder, normalized_challenge):
        warnings.append(_warn(
            "DUPLICATE_FILENAME",
            f"Filename appears more than once within one scan: {fname}",
            ", ".join(str(m) for m in matches),
        ))

    blocking_errors = [e for e in errors if e["code"] != "UNKNOWN_CHALLENGE_TYPE"]
    runnable = has_run_instructions and len(blocking_errors) == 0

    # Detect whether the submission has NIfTI maps inside a results/ or maps/
    # subdirectory (for example: submission_root/results/maps/map.nii.gz).
    # A file only counts when it has a configured NIfTI suffix AND lives under
    # a "results" or "maps" folder — a stray .gz archive must not count.
    has_result_maps = any(
        f.name.lower().endswith(NIFTI_SUFFIXES)
        and any(part.lower() in ("results", "maps") for part in f.parts)
        for f in all_files
    )

    # Merge structural completeness into the existing issue lists so the
    # single existing gate (passed = no errors) covers them too. No second
    # status mechanism is introduced.
    for issue in completeness_issues:
        if issue.get("severity") == "error":
            errors.append(issue)
        else:
            warnings.append(issue)

    # run_readiness: "runnable" | "result_only" | "not_runnable"
    if runnable:
        run_readiness = "runnable"
    elif mode == "result_only" and len(blocking_errors) == 0:
        run_readiness = "result_only"
    elif not has_run_instructions and (len(nifti_files) > 0 if nifti_files else False) and len(blocking_errors) == 0:
        run_readiness = "result_only"
    else:
        run_readiness = "not_runnable"

    result = _finish(
        submission_id, normalized_challenge, errors, warnings,
        len(nifti_files) if nifti_files else 0,
        len(all_files),
        team_name, contact_email, effective_map_type, notes,
        has_dockerfile=has_dockerfile,
        has_run_instructions=has_run_instructions,
        runnable=runnable,
        run_readiness=run_readiness,
        mode=mode,
        nifti_summary=nifti_summary,
        has_result_maps=has_result_maps,
    )
    result["job_id"] = job_id
    result["qc_mode"] = "quick" if quick_qc else "deep"
    finish_job(job_id, error=None if result.get("passed") else "")
    return result


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
    nifti_summary: Optional[List[Dict]] = None,
    has_result_maps: bool = False,
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
        "has_result_maps": has_result_maps,
        "errors": errors,
        "error_count": len(errors),
        "warnings": warnings,
        "warning_count": len(warnings),   # pre-computed so JS doesn't have to call .length
        "nifti_count": nifti_count,
        "total_files": total_files,
        "has_dockerfile": has_dockerfile,
        "nifti_summary": nifti_summary or [],
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
    challenge_type: str | None = None,
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
    normalized_challenge = (challenge_type or _default_challenge_type()).lower()
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

    all_files: List[Path] = manifest_files(
        folder,
        refresh_if_stale=True,
        submission_id=safe_id,
        challenge_type=normalized_challenge,
    )

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
    challenge_type: str | None = None,
    map_type: Optional[str] = None,
) -> Dict:
    """Validate NIfTI files generated by Docker execution (from /output).

    Applies NIfTI-level checks (presence, map names, duplicate filenames,
    zero-byte files) to the output directory produced by running a submission.
    Does **not** require a submission_id — it operates directly on a directory.

    Returns:
        ``passed``, ``nifti_count``, ``output_files``, ``errors``, ``warnings``
    """
    normalized_challenge = (challenge_type or _default_challenge_type()).lower()
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

    with timed("validation.generated_outputs.manifest", path=str(output_dir)):
        refresh_manifest(output_dir, submission_id=output_dir.name, challenge_type=normalized_challenge)
        all_files = manifest_files(
            output_dir,
            refresh_if_stale=False,
            submission_id=output_dir.name,
            challenge_type=normalized_challenge,
        )
    output_files_rel = sorted(str(f.relative_to(output_dir)) for f in all_files)
    nifti_files = [f for f in all_files if f.name.lower().endswith(NIFTI_SUFFIXES)]

    if not nifti_files:
        errors.append(_err(
            "NO_GENERATED_NIFTI",
            f"The submission did not produce any NIfTI output files ({_suffix_help(NIFTI_SUFFIXES)}).",
        ))
    else:
        joined = " ".join(f.name.lower() for f in nifti_files)
        patterns_by_map = _map_type_patterns()
        expected_maps = _expected_maps()
        selected_map = (map_type or "").lower()
        selected_patterns = patterns_by_map.get(selected_map)
        if selected_patterns is not None:
            expected_groups = [(selected_map, selected_patterns)]
        elif selected_map not in {"", "auto", "other", "mixed/other"}:
            expected_groups = [(selected_map, (selected_map,))]
        else:
            expected_groups = [
                (name, patterns_by_map.get(name, (name,)))
                for name in expected_maps.get(normalized_challenge, ())
            ]

        for label, patterns in expected_groups:
            if not any(pattern in joined for pattern in patterns):
                warnings.append(_warn(
                    "EXPECTED_MAP_MISSING",
                    f"Expected {_map_display_name(label)} map not found in generated outputs.",
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
    challenge_type: str | None = None,
    challenge_types: Optional[Dict] = None,
    map_type: Optional[str] = None,
    map_type_mode: Optional[str] = None,
    notes: Optional[str] = None,
    team_names: Optional[Dict] = None,
    contact_emails: Optional[Dict] = None,
    mode: str = "result_validation",
    qc_mode: str = "deep",
) -> Dict:
    """Validate multiple submissions and return an aggregate batch result.

    Each submission is validated independently.  Failures in one submission do
    not prevent the remaining ones from being validated.

    ``challenge_types`` optionally maps ``submission_id -> challenge`` so a mixed
    batch validates each submission under its own challenge; any submission not
    listed falls back to ``challenge_type``. Challenges are never merged — each
    submission is validated strictly under its own challenge's rules.
    """
    team_names    = team_names    or {}
    contact_emails = contact_emails or {}
    challenge_types = challenge_types or {}

    def _challenge_for(sid: str) -> str | None:
        picked = challenge_types.get(sid)
        if picked and str(picked).strip() and str(picked).strip().lower() != "unknown":
            return str(picked).strip()
        return challenge_type

    job_id = start_job("batch_validation", total=len(submission_ids), stage="validating submissions")
    workers = configured_worker_limit("batch_validation_workers", 2, ceiling=4)
    results: List[Optional[Dict]] = [None] * len(submission_ids)

    def _validate_one(index: int, sid: str) -> tuple[int, Dict]:
        try:
            result = validate_submission(
                sid,
                challenge_type=_challenge_for(sid),
                map_type=map_type,
                map_type_mode=map_type_mode,
                notes=notes,
                team_name=team_names.get(sid),
                contact_email=contact_emails.get(sid),
                mode=mode,
                qc_mode=qc_mode,
                nifti_validation_workers=1,
            )
        except Exception as exc:
            now = datetime.now(timezone.utc).isoformat()
            result = {
                "submission_id": sid,
                "team_name":     team_names.get(sid, ""),
                "contact_email": contact_emails.get(sid, ""),
                "challenge_type": (_challenge_for(sid) or _default_challenge_type()).upper(),
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
        return index, result

    completed = 0
    with timed("validation.batch", submission_count=len(submission_ids), workers=workers):
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="batch-validate") as executor:
            futures = [
                executor.submit(_validate_one, index, sid)
                for index, sid in enumerate(submission_ids)
            ]
            for future in as_completed(futures):
                index, result = future.result()
                results[index] = result
                completed += 1
                update_job(job_id, completed=completed)

    now = datetime.now(timezone.utc).isoformat()
    batch_id = f"batch_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"
    final_results: List[Dict] = [r for r in results if r is not None]

    # Track which challenges are actually present so a mixed batch is honest
    # rather than being labelled with one global challenge.
    challenges_present = sorted({
        str(r.get("challenge_type") or "").upper()
        for r in final_results
        if r.get("challenge_type")
    })

    summary: Dict = {
        "batch_id":         batch_id,
        "job_id":           job_id,
        "workers":          workers,
        "challenge_type":   (challenge_type or _default_challenge_type()).upper(),
        "challenges_present": challenges_present,
        "mixed_challenges": len(challenges_present) > 1,
        "mode":             mode,
        "submission_count": len(final_results),
        "passed_count":     sum(1 for r in final_results if r.get("passed")),
        "failed_count":     sum(1 for r in final_results if not r.get("passed")),
        "runnable_count":   sum(1 for r in final_results if r.get("runnable")),
        "validated_at":     now,
        "results":          final_results,
    }
    _save_batch_result(batch_id, summary)
    finish_job(job_id)
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
