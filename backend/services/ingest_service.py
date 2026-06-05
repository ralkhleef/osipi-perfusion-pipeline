"""Handle submission uploads and imports.

Returns a submission_id (the ZIP stem) so the rest of the backend
can locate the extracted folder without exposing file paths to the frontend.
"""

import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, Tuple

from services.path_config import EXTRACTED_DIR, INCOMING_DIR

NIFTI_SUFFIXES = (".nii", ".nii.gz")
MAP_TYPE_PATTERNS = {
    "CBF": ("cbf", "cerebral_blood_flow"),
    "Ktrans": ("ktrans", "k_trans", "transfer_constant"),
    "ATT": ("att", "arterial_transit_time"),
}


def save_and_extract(file_bytes: bytes, filename: str) -> Dict:
    """Save an uploaded ZIP and extract it.

    Returns submission_id, original_filename, file_count, and a short message.
    The actual folder path stays on the server — the frontend never sees it.
    """
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)

    safe_filename = Path(filename).name
    zip_path = INCOMING_DIR / safe_filename
    zip_path.write_bytes(file_bytes)

    submission_id = _safe_id(Path(safe_filename).stem)
    extracted_dir = _reset_submission_dir(submission_id)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extract_zip(zf, extracted_dir)
    except zipfile.BadZipFile:
        return {
            "success": False,
            "error": f"{safe_filename} is not a valid ZIP file.",
        }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    file_count = sum(1 for p in extracted_dir.rglob("*") if p.is_file())
    detection = detect_submission_metadata(submission_id)

    return {
        "success": True,
        "submission_id": submission_id,
        "original_filename": safe_filename,
        "file_count": file_count,
        **detection,
        "message": f"Extracted {file_count} file(s).",
    }


def save_uploaded_folder(files: Iterable[Tuple[str, bytes]]) -> Dict:
    """Save browser folder-upload files into the standard submission folder."""
    materialized = list(files)
    if not materialized:
        return {"success": False, "error": "No files were uploaded."}

    safe_files = []
    for raw_name, contents in materialized:
        try:
            safe_files.append((_safe_relative_path(raw_name), contents))
        except ValueError:
            continue

    if not safe_files:
        return {"success": False, "error": "No valid files were uploaded."}

    first_path = safe_files[0][0]
    common_root = first_path.parts[0] if len(first_path.parts) > 1 else None
    if common_root:
        for rel_path, _ in safe_files:
            if len(rel_path.parts) < 2 or rel_path.parts[0] != common_root:
                common_root = None
                break

    first_part = common_root or first_path.stem
    submission_id = _safe_id(first_part or "folder_submission")
    extracted_dir = _reset_submission_dir(submission_id)

    saved = 0
    for rel_path, contents in safe_files:
        if common_root and len(rel_path.parts) > 1:
            rel_path = Path(*rel_path.parts[1:])
        dest = extracted_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(contents)
        saved += 1

    return {
        "success": True,
        "submission_id": submission_id,
        "file_count": saved,
        **detect_submission_metadata(submission_id),
        "message": f"Uploaded {saved} file(s).",
    }


def reset_submission_dir(submission_id: str) -> Path:
    """Create a clean internal submission folder and return it."""
    return _reset_submission_dir(_safe_id(submission_id))


def count_submission_files(submission_id: str) -> int:
    """Count files in an internal submission folder."""
    folder = EXTRACTED_DIR / _safe_id(submission_id)
    return sum(1 for p in folder.rglob("*") if p.is_file())


def detect_submission_metadata(submission_id: str) -> Dict:
    """Detect NIfTI count and likely map type for an ingested submission."""
    folder = EXTRACTED_DIR / _safe_id(submission_id)
    if not folder.exists() or not folder.is_dir():
        return {
            "nifti_count": 0,
            "detected_parameter_map_type": "Unknown",
            "detected_map_type_confidence": "none",
            "detection_warning": "Submission files were not found for auto-detection.",
        }

    files = [p for p in folder.rglob("*") if p.is_file()]
    nifti_count = sum(1 for p in files if p.name.lower().endswith(NIFTI_SUFFIXES))
    detected = _detect_parameter_map_type(files)
    warning = None
    confidence = "high"

    if detected == "Unknown":
        confidence = "none"
        warning = "Could not auto-detect the parameter map type from filenames."
    elif detected == "Mixed/Other":
        confidence = "low"
        warning = "Multiple parameter map types were detected from filenames."

    return {
        "nifti_count": nifti_count,
        "detected_parameter_map_type": detected,
        "detected_map_type_confidence": confidence,
        "detection_warning": warning,
    }


def _detect_parameter_map_type(files: Iterable[Path]) -> str:
    found = set()
    for file_path in files:
        name = file_path.name.lower()
        for map_type, patterns in MAP_TYPE_PATTERNS.items():
            if any(pattern in name for pattern in patterns):
                found.add(map_type)

    if len(found) == 1:
        return next(iter(found))
    if len(found) > 1:
        return "Mixed/Other"
    return "Unknown"


def _safe_id(stem: str) -> str:
    """Turn a ZIP filename stem into a safe submission ID."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in stem)
    return safe.strip("_") or "submission"


def _reset_submission_dir(submission_id: str) -> Path:
    EXTRACTED_DIR.mkdir(parents=True, exist_ok=True)
    extracted_dir = EXTRACTED_DIR / _safe_id(submission_id)
    if extracted_dir.exists():
        shutil.rmtree(extracted_dir)
    extracted_dir.mkdir(parents=True, exist_ok=True)
    return extracted_dir


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> None:
    for member in zf.infolist():
        rel_path = _safe_relative_path(member.filename)
        if member.is_dir():
            (target_dir / rel_path).mkdir(parents=True, exist_ok=True)
            continue
        dest = target_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(member) as src, open(dest, "wb") as dst:
            shutil.copyfileobj(src, dst)


def _safe_relative_path(raw_path: str) -> Path:
    path = Path((raw_path or "").replace("\\", "/"))
    parts = [
        part for part in path.parts
        if part not in ("", ".", "/") and part != path.anchor
    ]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Archive contains an unsafe file path.")
    return Path(*parts)
