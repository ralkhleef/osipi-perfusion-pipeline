"""Handle submission uploads and imports.

Returns a submission_id (the ZIP stem) so the rest of the backend
can locate the extracted folder without exposing file paths to the frontend.
"""

import shutil
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

from services.path_config import EXTRACTED_DIR, INCOMING_DIR, safe_relative_path
from osipi_pipeline.config.rules import app_settings, map_type_patterns, settings_tuple, tuple_setting
from osipi_pipeline.ingestion.detector import detect_challenge_type
from osipi_pipeline.ingestion.manifest import load_manifest, manifest_files, refresh_manifest

# ── Map type detection ─────────────────────────────────────────────────────────

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")


def _map_type_patterns() -> dict[str, tuple[str, ...]]:
    return map_type_patterns(display_keys=True)

# ── Safety limits (override via environment variables) ─────────────────────────

import os as _os

_LIMITS = app_settings().get("limits", {})
ZIP_MAX_BYTES = int(_os.environ.get("OSIPI_ZIP_MAX_BYTES", str(_LIMITS.get("zip_max_bytes", 500 * 1024 * 1024))))
EXTRACT_MAX_BYTES = int(_os.environ.get("OSIPI_EXTRACT_MAX_BYTES", str(_LIMITS.get("extract_max_bytes", 2 * 1024 * 1024 * 1024))))
EXTRACT_MAX_FILES = int(_os.environ.get("OSIPI_EXTRACT_MAX_FILES", str(_LIMITS.get("extract_max_files", 10000))))

# Paths/filenames to silently skip when extracting ZIPs
_SKIP_PREFIXES = set(settings_tuple("ingestion", "skip_prefixes"))
_SKIP_NAMES    = set(settings_tuple("ingestion", "skip_names"))


# ── Public API — single submission (legacy) ───────────────────────────────────


def save_and_extract(file_bytes: bytes, filename: str) -> Dict:
    """Save an uploaded ZIP and extract it (single-submission path).

    Superseded by ``save_and_extract_batch`` / ``save_and_extract_batch_from_path``
    which also handle multi-submission ZIPs.  Kept for backward-compatibility.
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
        return {"success": False, "error": f"{safe_filename} is not a valid ZIP file."}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    detection = detect_submission_metadata(submission_id)
    return {
        "success": True,
        "batch": False,
        "source_type": "local",
        "submission_id": submission_id,
        "original_filename": safe_filename,
        "file_count": file_count,
        **detection,
        "message": f"Extracted {file_count} file(s).",
    }


def save_and_extract_batch(file_bytes: bytes, filename: str) -> Dict:
    """Extract a ZIP (bytes) and auto-detect single vs. batch submissions.

    Size is checked against ZIP_MAX_BYTES before writing.
    Delegates to ``save_and_extract_batch_from_path`` after saving to disk.
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

    return save_and_extract_batch_from_path(zip_path, filename)


def save_and_extract_batch_from_path(zip_path: Path, filename: str) -> Dict:
    """Batch extraction from a ZIP already on disk.

    No in-memory size check — the caller is responsible for enforcing the limit
    while streaming (e.g. the ``/api/upload-batch`` endpoint).
    Handles wrapper-folder ZIPs and returns the same response shape as
    ``save_and_extract_batch``.
    """
    safe_filename = Path(filename).name
    batch_stem = _safe_id(Path(safe_filename).stem)
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

    return _finalize_staged_dir(temp_dir, batch_stem, safe_filename, file_count)


def save_uploaded_folder(files: Iterable[Tuple[str, Union[bytes, Path]]]) -> Dict:
    """Save browser folder-upload files into a single submission folder.

    ``files`` is an iterable of ``(relative_path_string, data)`` where
    ``data`` is either raw ``bytes`` (legacy) or a ``Path`` to a temp file
    on disk (streaming path — avoids holding all files in RAM at once).

    Enforces cumulative file count and size limits before staging.
    """
    materialized = list(files)
    if not materialized:
        return {"success": False, "error": "No files were uploaded."}

    if len(materialized) > EXTRACT_MAX_FILES:
        return {
            "success": False,
            "error": f"Too many files in folder upload (limit: {EXTRACT_MAX_FILES:,}).",
        }

    safe_files: List[Tuple[Path, Union[bytes, Path]]] = []
    cumulative_bytes = 0
    for raw_name, contents in materialized:
        try:
            safe_files.append((_safe_relative_path(raw_name), contents))
        except ValueError:
            continue
        size = contents.stat().st_size if isinstance(contents, Path) else len(contents)
        cumulative_bytes += size
        if cumulative_bytes > EXTRACT_MAX_BYTES:
            return {
                "success": False,
                "error": f"Folder upload exceeds size limit ({EXTRACT_MAX_BYTES // (1024 ** 3)} GB).",
            }

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
        if isinstance(contents, Path):
            shutil.copy2(contents, dest)
        else:
            dest.write_bytes(contents)
        saved += 1

    return {
        "success": True,
        "batch": False,
        "source_type": "local",
        "submission_id": submission_id,
        "file_count": saved,
        **detect_submission_metadata(submission_id),
        "message": f"Uploaded {saved} file(s).",
    }


def save_folder_as_batch(files: Iterable[Tuple[str, Union[bytes, Path]]]) -> Dict:
    """Save browser folder-upload files and auto-detect batch boundaries.

    ``files`` is an iterable of ``(relative_path_string, data)`` where
    ``data`` is either raw ``bytes`` (legacy) or a ``Path`` to a temp file
    on disk (streaming path — avoids holding all files in RAM at once).

    Enforces cumulative size / file-count limits before staging.
    Uses the same ``_finalize_staged_dir`` path as ZIP uploads, so
    wrapper-folder detection and the shared response shape are consistent.
    """
    materialized = list(files)
    if not materialized:
        return {"success": False, "error": "No files were uploaded."}

    if len(materialized) > EXTRACT_MAX_FILES:
        return {
            "success": False,
            "error": f"Too many files in folder upload (limit: {EXTRACT_MAX_FILES:,}).",
        }

    safe_files: List[Tuple[Path, Union[bytes, Path]]] = []
    cumulative_bytes = 0
    for raw_name, contents in materialized:
        try:
            safe_files.append((_safe_relative_path(raw_name), contents))
        except ValueError:
            continue
        size = contents.stat().st_size if isinstance(contents, Path) else len(contents)
        cumulative_bytes += size
        if cumulative_bytes > EXTRACT_MAX_BYTES:
            return {
                "success": False,
                "error": f"Folder upload exceeds size limit ({EXTRACT_MAX_BYTES // (1024 ** 3)} GB).",
            }

    if not safe_files:
        return {"success": False, "error": "No valid files were uploaded."}

    first_path = safe_files[0][0]
    common_root = first_path.parts[0] if len(first_path.parts) > 1 else None
    if common_root:
        for rel_path, _ in safe_files:
            if len(rel_path.parts) < 2 or rel_path.parts[0] != common_root:
                common_root = None
                break

    batch_stem = _safe_id(common_root or first_path.stem or "folder_batch")
    temp_id = f"_folder_temp_{batch_stem}"
    temp_dir = _reset_submission_dir(temp_id)

    saved = 0
    for rel_path, contents in safe_files:
        rel_stored = Path(*rel_path.parts[1:]) if common_root and len(rel_path.parts) > 1 else rel_path
        dest = temp_dir / rel_stored
        dest.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(contents, Path):
            shutil.copy2(contents, dest)
        else:
            dest.write_bytes(contents)
        saved += 1

    display_filename = f"{common_root or batch_stem} (folder)"
    return _finalize_staged_dir(temp_dir, batch_stem, display_filename, saved)


def finalize_imported_dir(
    imported_dir: Path,
    submission_id: str,
    display_name: str,
    source_type: str,
) -> Dict:
    """Run batch detection on an already-downloaded import directory.

    Used by Zenodo and GitHub import paths after their files land on disk.
    If the directory contains exactly one ZIP file and nothing else, that ZIP
    is auto-extracted before detection runs (common for Zenodo records).

    Returns the same response shape as ``save_and_extract_batch``.
    """
    if not imported_dir.exists() or not imported_dir.is_dir():
        return {
            "success": False,
            "error": (
                f"Import directory not found. "
                f"The {source_type} download may have failed."
            ),
        }

    # Auto-extract if the record consists of a single ZIP file
    _auto_extract_single_zip(imported_dir)

    file_count = sum(1 for f in imported_dir.rglob("*") if f.is_file())

    if file_count == 0:
        shutil.rmtree(imported_dir, ignore_errors=True)
        return {
            "success": False,
            "error": (
                f"No files were found after importing from {source_type}. "
                "Verify the record contains valid submission data."
            ),
        }

    batch_stem = _safe_id(submission_id)

    # Rename to a temp staging dir so _finalize_staged_dir can work safely
    temp_id = f"_import_temp_{batch_stem}"
    temp_dir = EXTRACTED_DIR / temp_id
    if temp_dir.exists():
        shutil.rmtree(temp_dir)

    try:
        imported_dir.rename(temp_dir)
    except OSError:
        shutil.copytree(str(imported_dir), str(temp_dir))
        shutil.rmtree(imported_dir, ignore_errors=True)

    return _finalize_staged_dir(temp_dir, batch_stem, display_name, file_count, source_type)


# ── Public utilities ──────────────────────────────────────────────────────────


def make_safe_id(stem: str) -> str:
    """Turn an arbitrary string into a safe submission ID (alphanumeric, hyphens, underscores)."""
    return _safe_id(stem)


def reset_submission_dir(submission_id: str) -> Path:
    """Create a clean internal submission folder and return it."""
    return _reset_submission_dir(_safe_id(submission_id))


def count_submission_files(submission_id: str) -> int:
    """Count files in an internal submission folder."""
    safe_id = _safe_id(submission_id)
    folder = EXTRACTED_DIR / safe_id
    manifest = load_manifest(folder, refresh_if_stale=True, submission_id=safe_id)
    return int((manifest or {}).get("file_count") or 0)


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

    files = manifest_files(folder, refresh_if_stale=True, submission_id=_safe_id(submission_id))
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

    # Per-submission challenge detection so a mixed upload (e.g. ASL + DCE) can
    # tag each submission with its own challenge. "unknown" means the user must
    # pick/confirm; downstream steps must scope by challenge, never mix them.
    try:
        detected_challenge = detect_challenge_type(folder)
    except Exception:
        detected_challenge = "unknown"

    return {
        "nifti_count": nifti_count,
        "detected_parameter_map_type": detected,
        "detected_map_type_confidence": confidence,
        "detection_warning": warning,
        "detected_challenge_type": detected_challenge,
    }


def _is_submission_candidate(d: Path) -> bool:
    """Return True if *d* looks like a submission folder.

    A directory qualifies as a submission candidate if it contains at least one
    file anywhere in its subtree.  NIfTI files are *not* required — a folder
    that only contains a README, Dockerfile, metadata.json, or any other file
    is still a valid (if incomplete) submission.  Validation decides pass/fail.

    Empty directories are not considered submission candidates.
    """
    try:
        for p in d.rglob("*"):
            if p.is_file():
                return True
    except PermissionError:
        pass
    return False


def detect_batch_boundaries(extracted_dir: Path) -> Optional[List[Path]]:
    """Return the top-level subdirectories that are submission candidates.

    A directory is a candidate if it contains any files (NIfTI or otherwise).
    NIfTI files are *not* required — invalid/incomplete submissions are included
    so that validation can mark them as failed rather than silently skipping them.

    Handles the wrapper-folder pattern: if exactly one top-level directory
    exists, looks one level deeper for multiple submission directories.

    Examples:
        batch.zip/Team_A/ Team_B/              → [Team_A, Team_B]
        batch.zip/wrapper/Team_A/ Team_B/      → [Team_A, Team_B]  (wrapper unwrapped)
        batch.zip/Team_A/ Team_B/ Team_C_bad/  → [Team_A, Team_B, Team_C_bad]
        batch.zip/Team_A/                      → None  (single submission)

    Returns None if fewer than 2 qualifying directories are found.
    """
    try:
        top_dirs = sorted(d for d in extracted_dir.iterdir() if d.is_dir())
    except PermissionError:
        return None

    if not top_dirs:
        return None

    # Wrapper-folder case: one top-level dir that wraps multiple submission dirs
    if len(top_dirs) == 1:
        return _check_inner_batch(top_dirs[0])

    submission_dirs = [d for d in top_dirs if _is_submission_candidate(d)]

    if len(submission_dirs) < 2:
        return None

    # If every top-level dir is a well-known structural subdir (input/, results/,
    # maps/, …) this is a single submission whose data is laid out across those
    # folders — NOT a multi-team batch.  Without this check a ZIP that contains
    # ``input/`` and ``results/maps/`` at its top level would be wrongly split
    # into two submissions (e.g. ``<name>_input`` and ``<name>_results``).
    if _is_structural_layout(submission_dirs):
        return None

    return submission_dirs


# ── Internal helpers ──────────────────────────────────────────────────────────


def _finalize_staged_dir(
    temp_dir: Path,
    batch_stem: str,
    display_filename: str,
    file_count: int,
    source_type: str = "local",
) -> Dict:
    """Core post-staging logic shared by all import paths.

    Runs batch detection on ``temp_dir``.
    - Single submission → renames temp_dir to the final submission dir.
    - Batch → carves per-team submission dirs, then removes temp_dir.

    ``temp_dir`` is always cleaned up by the time this function returns.
    """
    batch_dirs = detect_batch_boundaries(temp_dir)

    # ── Single submission ─────────────────────────────────────────────────────
    if not batch_dirs:
        final_dir = EXTRACTED_DIR / batch_stem
        if final_dir.exists():
            shutil.rmtree(final_dir)
        # Unwrap a single redundant wrapper folder so the submission root
        # directly contains input/ , results/maps/ , README, etc.  Without this
        # a ZIP shaped like ``Lena_ASL/input/ Lena_ASL/results/maps/`` would
        # extract to ``<id>/Lena_ASL/input`` (an extra nesting level).
        inner = _redundant_wrapper(temp_dir)
        if inner is not None:
            inner.rename(final_dir)
            shutil.rmtree(temp_dir, ignore_errors=True)
        else:
            temp_dir.rename(final_dir)
        manifest = refresh_manifest(final_dir, submission_id=batch_stem, original_path=display_filename)
        detection = detect_submission_metadata(batch_stem)
        return {
            "success": True,
            "batch": False,
            "source_type": source_type,
            "submission_id": batch_stem,
            "original_filename": display_filename,
            "file_count": int(manifest.get("file_count") or file_count),
            **detection,
            "message": f"Extracted {file_count} file(s).",
        }

    # ── Batch: carve per-team submission dirs ─────────────────────────────────
    submissions = []
    try:
        for batch_dir in batch_dirs:
            sub_id = _safe_id(f"{batch_stem}_{batch_dir.name}")
            sub_dir = _reset_submission_dir(sub_id)
            for item in sorted(batch_dir.iterdir()):
                shutil.move(str(item), str(sub_dir / item.name))
            manifest = refresh_manifest(sub_dir, submission_id=sub_id, original_path=batch_dir.name)
            detection = detect_submission_metadata(sub_id)
            submissions.append({
                "submission_id": sub_id,
                "source_folder": batch_dir.name,
                "file_count": int(manifest.get("file_count") or 0),
                **detection,
            })
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    return {
        "success": True,
        "batch": True,
        "source_type": source_type,
        "original_filename": display_filename,
        "batch_stem": batch_stem,
        "submission_count": len(submissions),
        "submissions": submissions,
        "message": f"Detected {len(submissions)} submission(s) from {source_type}.",
    }


# ── Structural subdirectory names: indicate a SINGLE-submission layout ────────
# If ALL inner directories of a folder have names from this set, the parent is
# ONE submission (input/results/maps are its internal data layout), not a batch.
# A real multi-team batch would have names like "Team_A", "submission_001", etc.
_STRUCTURAL_SUBDIRS: frozenset = frozenset(
    item.lower() for item in settings_tuple("ingestion", "structural_subdirs")
)


def _is_structural_layout(dirs: List[Path]) -> bool:
    """Return True if every directory name is a well-known structural subdirectory.

    Example — treat as ONE submission (not a batch)::

        lena_realistic_asl_osipi_named/
            input/        ← structural
            results/      ← structural
                maps/

    A batch would have team-named subdirectories::

        batch_upload/
            Team_A/
            Team_B/
    """
    return bool(dirs) and all(d.name.lower() in _STRUCTURAL_SUBDIRS for d in dirs)


def _redundant_wrapper(staged_dir: Path) -> Optional[Path]:
    """Return the inner directory if *staged_dir* is just a single wrapper folder.

    Many submission ZIPs wrap everything in one named folder
    (e.g. ``Lena_ASL_osipi_named/`` containing ``input/`` and ``results/maps/``).
    After extraction that produces a redundant nesting level.  This detects the
    case so the caller can promote the inner folder up to the submission root.

    Returns the inner directory only when *staged_dir* contains exactly one
    entry, that entry is a directory, and its name is NOT a structural subdir
    (``input``/``results``/…). Returns None otherwise (nothing to unwrap).
    """
    try:
        entries = [e for e in staged_dir.iterdir()]
    except (PermissionError, FileNotFoundError):
        return None
    dirs = [e for e in entries if e.is_dir()]
    if (
        len(entries) == 1
        and len(dirs) == 1
        and dirs[0].name.lower() not in _STRUCTURAL_SUBDIRS
    ):
        return dirs[0]
    return None


def _check_inner_batch(wrapper_dir: Path) -> Optional[List[Path]]:
    """One-level wrapper-unwrap: check if wrapper_dir itself is a batch container.

    Only called when exactly one top-level directory exists.  Does NOT recurse
    further, so three-level nesting is treated as a single submission.

    Returns None (single submission) if:
    - fewer than 2 inner directories are found, OR
    - ALL inner directories look like structural subdirectories of one submission
      (e.g. ``input/``, ``results/``, ``maps/`` — see ``_is_structural_layout``).

    Includes all inner directories that contain any files — not just those with
    NIfTI files — so that incomplete submissions are detected and can fail
    validation rather than being silently skipped.
    """
    try:
        inner_dirs = sorted(d for d in wrapper_dir.iterdir() if d.is_dir())
    except PermissionError:
        return None

    if len(inner_dirs) < 2:
        return None

    submission_dirs = [d for d in inner_dirs if _is_submission_candidate(d)]

    if len(submission_dirs) < 2:
        return None

    # If all subdirs are structural names (input/, results/, maps/, …)
    # this is a single submission with an internal data layout — not a batch.
    if _is_structural_layout(submission_dirs):
        return None

    return submission_dirs


def _auto_extract_single_zip(directory: Path) -> None:
    """Extract ZIP file(s) in a directory if no NIfTI files are present yet.

    Used by ``finalize_imported_dir`` to unwrap Zenodo records that consist of
    one or more ZIP archives alongside non-NIfTI content (e.g. README.md).

    Extraction is skipped when:
    - The directory contains no ZIP files.
    - NIfTI files are already present (the record may already be unpacked).
    - A ZIP file is corrupt or would exceed extraction limits (silently skipped).
    """
    try:
        all_items = [f for f in directory.iterdir() if f.is_file()]
    except PermissionError:
        return

    zip_files = [f for f in all_items if f.name.lower().endswith(".zip")]
    if not zip_files:
        return  # Nothing to extract

    # If NIfTI files are already present the record is already unpacked — leave it.
    if any(f.name.lower().endswith(NIFTI_SUFFIXES) for f in all_items):
        return

    for zip_path in zip_files:
        try:
            with zipfile.ZipFile(zip_path) as zf:
                _safe_extract_zip(zf, directory)
            try:
                zip_path.unlink()
            except OSError:
                pass
        except (zipfile.BadZipFile, ValueError):
            pass  # Leave as-is if this ZIP is corrupt or oversized


def _detect_parameter_map_type(files: Iterable[Path]) -> str:
    found: set = set()
    for file_path in files:
        name = file_path.name.lower()
        for map_type, patterns in _map_type_patterns().items():
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
    for part in parts:
        if part in _SKIP_PREFIXES:
            return True
    name = parts[-1]
    if name in _SKIP_NAMES:
        return True
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
        if _should_skip_path(member.filename):
            continue

        try:
            rel_path = _safe_relative_path(member.filename)
        except ValueError:
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
    """Turn a filename stem into a safe submission ID (alphanumeric + hyphens/underscores)."""
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
    """Thin wrapper around the shared ``safe_relative_path`` utility."""
    return safe_relative_path(raw_path)
