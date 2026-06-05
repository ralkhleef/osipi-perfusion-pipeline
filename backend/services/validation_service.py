"""File-level validation for an ingested submission.

Accepts a submission_id and resolves the folder path internally —
the frontend never needs to know where files are stored.
Results are saved to data/outputs/ for the Outputs page.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR
from services.ingest_service import detect_submission_metadata

NIFTI_SUFFIXES = (".nii", ".nii.gz")

EXPECTED_MAPS: Dict[str, tuple] = {
    "dce": ("ktrans", "kep", "vp"),
    "asl": ("cbf", "att"),
    "dsc": (),  # DSC map names are not yet standardised.
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
    """Validate the submission folder for the given submission_id.

    The folder is resolved as EXTRACTED_DIR / submission_id.
    Results are written to OUTPUTS_DIR / {submission_id}_validation.json.
    """
    folder = EXTRACTED_DIR / submission_id
    errors: List[str] = []
    warnings: List[str] = []

    if not folder.exists() or not folder.is_dir():
        errors.append(
            "Submission files were not found. Please re-upload your ZIP file."
        )
        return _finish(
            submission_id, challenge_type, errors, warnings, 0, 0,
            team_name, contact_email, map_type, notes,
        )

    all_files: List[Path] = [f for f in folder.rglob("*") if f.is_file()]
    if not all_files:
        errors.append("The submission folder is empty.")
        return _finish(
            submission_id, challenge_type, errors, warnings, 0, 0,
            team_name, contact_email, map_type, notes,
        )

    detection = detect_submission_metadata(submission_id)
    if (expected_nifti_count_mode or "").lower() == "auto":
        expected_nifti_count = detection.get("nifti_count", 0)

    effective_map_type = map_type
    if (map_type_mode or "").lower() == "auto" or (map_type or "").lower() == "auto":
        detected_map_type = detection.get("detected_parameter_map_type", "Unknown")
        if detected_map_type in {"CBF", "Ktrans", "ATT", "Mixed/Other"}:
            effective_map_type = detected_map_type
        else:
            effective_map_type = ""
            warnings.append(
                "Parameter map type could not be auto-detected. Please select it manually if validation needs this metadata."
            )
        if detected_map_type == "Mixed/Other":
            warnings.append(
                "Multiple parameter map types were detected from filenames. Treating this submission as Mixed/Other."
            )

    # ---- NIfTI files --------------------------------------------------------

    nifti_files: List[Path] = [
        f for f in all_files if f.name.lower().endswith(NIFTI_SUFFIXES)
    ]

    if not nifti_files:
        errors.append("No .nii or .nii.gz parameter map files were found.")
    else:
        actual = len(nifti_files)

        if expected_nifti_count is not None and actual != expected_nifti_count:
            warnings.append(
                f"Found {actual} NIfTI file(s), but {expected_nifti_count} were expected."
            )

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
                for name in EXPECTED_MAPS.get(challenge_type.lower(), ())
            ]

        for label, patterns in expected_groups:
            if not any(pattern in joined for pattern in patterns):
                warnings.append(f"Expected parameter map not found: {label.upper()}.")

        for f in nifti_files:
            if f.stat().st_size == 0:
                warnings.append(f"NIfTI file appears to be empty: {f.name}")

    # ---- README / SOP -------------------------------------------------------

    readme_found = _has_readme(all_files)

    if include_readme == "yes" and not readme_found:
        errors.append(
            "A README or SOP was marked as included, but none was found. "
            "Please add README.md, README.txt, SOP.pdf, or metadata.json."
        )
    elif not readme_found:
        warnings.append(
            "No README or SOP file was found. "
            "Consider adding one to describe how results were generated."
        )

    # ---- Code files ---------------------------------------------------------

    if include_code == "yes" and not _has_code(all_files):
        warnings.append(
            "Code files were marked as included, but none were found. "
            "Expected: Dockerfile, requirements.txt, .py / .sh / .m files, "
            "or a scripts/ folder."
        )

    return _finish(
        submission_id, challenge_type, errors, warnings,
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
    errors: List[str],
    warnings: List[str],
    nifti_count: int,
    total_files: int,
    team_name: Optional[str],
    contact_email: Optional[str],
    map_type: Optional[str],
    notes: Optional[str],
) -> Dict:
    result: Dict = {
        "submission_id": submission_id,
        "team_name": team_name or "",
        "contact_email": contact_email or "",
        "challenge_type": challenge_type.upper(),
        "map_type": map_type or "",
        "notes": notes or "",
        "validated_at": datetime.now(timezone.utc).isoformat(),
        "passed": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "nifti_count": nifti_count,
        "total_files": total_files,
    }
    _save_result(submission_id, result)
    return result


def _save_result(submission_id: str, result: Dict) -> None:
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    out_file = OUTPUTS_DIR / f"{submission_id}_validation.json"
    out_file.write_text(json.dumps(result, indent=2), encoding="utf-8")


def _has_readme(files: List[Path]) -> bool:
    return any(f.name.lower() in README_NAMES for f in files)


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
