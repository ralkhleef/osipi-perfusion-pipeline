"""File-level validation for an ingested submission.

Accepts a submission_id, resolves the folder path internally, and returns
structured results. Errors and warnings are dicts with the same shape as the
CLI validation package: {severity, code, message, path}.
Results are saved to data/outputs/validation/ for the Outputs page.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR
from services.ingest_service import detect_submission_metadata

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
    "cbf": ("cbf", "cerebral_blood_flow"),
    "ktrans": ("ktrans", "k_trans", "transfer_constant"),
    "att": ("att", "arterial_transit_time"),
}

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
) -> Dict:
    """Validate the submission folder for the given submission_id."""
    folder = EXTRACTED_DIR / submission_id
    errors: List[Dict] = []
    warnings: List[Dict] = []
    normalized_challenge = (challenge_type or "dce").lower()

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

    detection = detect_submission_metadata(submission_id)
    if (expected_nifti_count_mode or "").lower() == "auto":
        expected_nifti_count = detection.get("nifti_count", 0)

    effective_map_type = map_type
    if (map_type_mode or "").lower() == "auto" or (map_type or "").lower() == "auto":
        detected = detection.get("detected_parameter_map_type", "Unknown")
        if detected in {"CBF", "Ktrans", "ATT", "Mixed/Other"}:
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

    if not nifti_files:
        errors.append(_err("NO_NIFTI_FILES", "No .nii or .nii.gz parameter map files were found.", str(folder)))
    else:
        actual = len(nifti_files)

        if expected_nifti_count is not None and actual != expected_nifti_count:
            warnings.append(_warn(
                "NIFTI_COUNT_MISMATCH",
                f"Found {actual} NIfTI file(s), but {expected_nifti_count} were expected.",
                str(folder),
            ))

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
                warnings.append(_warn("EMPTY_NIFTI_FILE", f"NIfTI file appears to be empty: {f.name}", str(f)))

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

    # ---- Code / Docker files ------------------------------------------------

    if not _has_docker(all_files):
        warnings.append(_warn("DOCKERFILE_MISSING", "Dockerfile is missing.", str(folder)))

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

    return _finish(
        submission_id, normalized_challenge, errors, warnings,
        len(nifti_files) if nifti_files else 0,
        len(all_files),
        team_name, contact_email, effective_map_type, notes,
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
) -> Dict:
    now = datetime.now(timezone.utc).isoformat()
    result: Dict = {
        "submission_id": submission_id,
        "team_name": team_name or "",
        "contact_email": contact_email or "",
        "challenge_type": challenge_type.upper(),
        "map_type": map_type or "",
        "notes": notes or "",
        "checked_at": now,
        "validated_at": now,  # kept for JS compatibility
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "nifti_count": nifti_count,
        "total_files": total_files,
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
    return any(
        f.name.lower() in {"dockerfile", ".dockerignore"}
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
