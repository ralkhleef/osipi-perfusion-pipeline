"""Handle submission uploads and imports.

Returns a submission_id (the ZIP stem) so the rest of the backend
can locate the extracted folder without exposing file paths to the frontend.
"""

import os
import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from services.path_config import EXTRACTED_DIR, INCOMING_DIR

# ── Map type detection ─────────────────────────────────────────────────────────

NIFTI_SUFFIXES = (".nii", ".nii.gz")

MAP_TYPE_PATTERNS = {
    "CBF":    ("cbf", "cerebral_blood_flow"),
    "Ktrans": ("ktrans", "k_trans", "transfer_constant"),
    "ATT":    ("att", "arterial_transit_time"),
    "Kep":    ("kep", "k_ep", "rate_constant"),
    "Vp":     ("vp", "v_p", "plasma_volume"),
    "CBV":    ("cbv", "cerebral_blood_volume"),
    "MTT":    ("mtt", "mean_transit_time"),
}

# ── Safety limits (override via environment variables) ─────────────────────────

ZIP_MAX_BYTES     = int(os.environ.get("OSIPI_ZIP_MAX_BYTES",     str(500  * 1024 * 1024)))       # 500 MB
EXTRACT_MAX_BYTES = int(os.environ.get("OSIPI_EXTRACT_MAX_BYTES", str(2    * 1024 * 1024 * 1024))) # 2 GB
EXTRACT_MAX_FILES = int(os.environ.get("OSIPI_EXTRACT_MAX_FILES", "10000"))

# Paths/filenames to silently skip when extracting ZIPs
_SKIP_PREFIXES = {"__MACOSX", "__pycache__"}
_SKIP_NAMES    = {".DS_Store", "Thumbs.db", "desktop.ini", ".gitkeep"}


# ── Public API — single submission ────────────────────────────────────────────


def save_and_extract(file_bytes: bytes, filename: str) -> Dict:
    """Save an uploaded ZIP and extract it (single-submission path).

    Returns submission_id, original_filename, file_count, and a short message.
    The actual folder path stays on the server — the frontend never sees it.
    """
    if len(file_bytes) > ZIP_MAX_BYTES:
        return {
            "success": False,
            "error": f"ZIP file is too large (limit: {ZIP_MAX_BYTES // (1024 * 1024)} MB).",
        }

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    safe_filename = Path(filename).name
    zip_path = INCOMING_DIR / safe_filename
    zip_path.write_bytes(file_bytes)

    submission_id = _safe_id(Path(safe_filename).stem)
    extracted_dir = _reset_submission_dir(submission_id)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            file_count, _ = _safe_extract_zip(zf, extracted_dir)
    except zipfile.BadZipFile:
        return {
            "success": False,
            "error": f"{safe_filename} is not a valid ZIP file.",
        }
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    detection = detect_submission_metadata(submission_id)
    return {
        "success": True,
        "submission_id": submission_id,
        "original_filename": safe_filename,
        "file_count": file_count,
        **detection,
        "message": f"Extracted {file_count} file(s).",
    }


def save_and_extract_batch(file_bytes: bytes, filename: str) -> Dict:
    """Extract a ZIP and auto-detect whether it is a single or batch submission.

    If the ZIP contains multiple top-level directories that each contain NIfTI
    files, each directory is treated as one independent submission and this
    function returns a batch result.  Otherwise it falls back to single-
    submission behaviour (identical result shape to save_and_extract).

    Batch detection rule:
        At least two top-level subdirectories must each contain at least one
        NIfTI file (.nii / .nii.gz) anywhere inside them.
    """
    if len(file_bytes) > ZIP_MAX_BYTES:
        return {
            "success": False,
            "error": f"ZIP file is too large (limit: {ZIP_MAX_BYTES // (1024 * 1024)} MB).",
        }

    safe_filename = Path(filename).name
    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = INCOMING_DIR / safe_filename
    zip_path.write_bytes(file_bytes)

    batch_stem = _safe_id(Path(safe_filename).stem)
    # Extract into a temporary staging directory first
    temp_id = f"_batch_temp_{batch_stem}"
    temp_dir = _reset_submission_dir(temp_id)

    try:
        with zipfile.ZipFile(zip_path) as zf:
            file_count, _ = _safe_extract_zip(zf, temp_dir)
    except zipfile.BadZipFile:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return {"success": False, "error": f"{safe_filename} is not a valid ZIP file."}
    except ValueError as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        return {"success": False, "error": str(exc)}

    batch_dirs = detect_batch_boundaries(temp_dir)

    # ── Single submission ─────────────────────────────────────────────────────
    if not batch_dirs:
        final_dir = EXTRACTED_DIR / batch_stem
        if final_dir.exists():
            shutil.rmtree(final_dir)
        temp_dir.rename(final_dir)
        detection = detect_submission_metadata(batch_stem)
        return {
            "success": True,
            "batch": False,
            "submission_id": batch_stem,
            "original_filename": safe_filename,
            "file_count": file_count,
            **detection,
            "message": f"Extracted {file_count} file(s).",
        }

    # ── Batch: carve out per-team submission folders ──────────────────────────
    submissions = []
    for batch_dir in batch_dirs:
        sub_id = _safe_id(f"{batch_stem}_{batch_dir.name}")
        sub_dir = _reset_submission_dir(sub_id)
        # Move all items from this team's dir into its dedicated submission folder
        for item in sorted(batch_dir.iterdir()):
            shutil.move(str(item), str(sub_dir / item.name))
        detection = detect_submission_metadata(sub_id)
        submissions.append({
            "submission_id": sub_id,
            "source_folder": batch_dir.name,
            "file_count": sum(1 for f in sub_dir.rglob("*") if f.is_file()),
            **detection,
        })

    shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        "success": True,
        "batch": True,
        "original_filename": safe_filename,
        "batch_stem": batch_stem,
        "submission_count": len(submissions),
        "submissions": submissions,
        "message": f"Detected {len(submissions)} submission(s) in batch ZIP.",
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
    confidence = "high"
    warning = None

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


def detect_batch_boundaries(extracted_dir: Path) -> Optional[List[Path]]:
    """Return the top-level subdirectories that each contain NIfTI files.

    If fewer than two such directories exist, returns None (not a batch).

    Detection rule:
        A top-level subdirectory qualifies as a submission boundary when it
        (recursively) contains at least one .nii or .nii.gz file.  Top-level
        files (not inside any subdirectory) are ignored for boundary detection.
    """
    top_dirs = sorted(d for d in extracted_dir.iterdir() if d.is_dir())
    if len(top_dirs) < 2:
        return None

    submission_dirs = [
        d for d in top_dirs
        if any(
            f.name.lower().endswith(NIFTI_SUFFIXES)
            for f in d.rglob("*")
            if f.is_file()
        )
    ]

    return submission_dirs if len(submission_dirs) >= 2 else None


# ── Internal helpers ──────────────────────────────────────────────────────────


def _detect_parameter_map_type(files: Iterable[Path]) -> str:
    found: set = set()
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


def _should_skip_path(filename: str) -> bool:
    """Return True if a ZIP entry should be silently skipped (junk/system files)."""
    parts = Path(filename.replace("\\", "/")).parts
    if not parts:
        return True
    # Skip macOS resource fork container dir and Python cache dirs
    for part in parts:
        if part in _SKIP_PREFIXES:
            return True
    # Skip specific system files by exact name
    name = parts[-1]
    if name in _SKIP_NAMES:
        return True
    # Skip macOS resource fork files (always start with "._")
    if name.startswith("._"):
        return True
    return False


def _safe_extract_zip(zf: zipfile.ZipFile, target_dir: Path) -> Tuple[int, int]:
    """Extract ZIP safely, skipping junk files and enforcing size/count limits.

    Returns ``(file_count, extracted_bytes)``.
    Raises ``ValueError`` if any configured limit is exceeded.
    """
    file_count = 0
    extracted_bytes = 0
    CHUNK = 65536  # 64 KB streaming chunks

    for member in zf.infolist():
        # Skip macOS metadata, .DS_Store, etc.
        if _should_skip_path(member.filename):
            continue

        try:
            rel_path = _safe_relative_path(member.filename)
        except ValueError:
            # Skip unsafe paths rather than aborting the whole extraction
            continue

        if member.is_dir():
            (target_dir / rel_path).mkdir(parents=True, exist_ok=True)
            continue

        file_count += 1
        if file_count > EXTRACT_MAX_FILES:
            raise ValueError(
                f"ZIP contains too many files (limit: {EXTRACT_MAX_FILES:,}). "
                "Split the batch into smaller ZIPs."
            )

        dest = target_dir / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)

        # Stream in 64 KB chunks to avoid loading large files into RAM
        with zf.open(member) as src, open(dest, "wb") as dst:
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                extracted_bytes += len(chunk)
                if extracted_bytes > EXTRACT_MAX_BYTES:
                    raise ValueError(
                        f"Extracted content exceeds the size limit "
                        f"({EXTRACT_MAX_BYTES // (1024 ** 3)} GB)."
                    )
                dst.write(chunk)

    return file_count, extracted_bytes


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


def _safe_relative_path(raw_path: str) -> Path:
    path = Path((raw_path or "").replace("\\", "/"))
    parts = [
        part for part in path.parts
        if part not in ("", ".", "/") and part != path.anchor
    ]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Archive contains an unsafe file path.")
    return Path(*parts)
