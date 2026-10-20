"""Handle submission uploads and imports."""

import os
import shutil
import unicodedata
import uuid
import zipfile
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

from services.path_config import EXTRACTED_DIR, INCOMING_DIR, safe_relative_path
from osipi_pipeline.config.rules import app_settings, map_type_patterns, settings_tuple, tuple_setting
from osipi_pipeline.ingestion.detector import detect_challenge_type
from osipi_pipeline.ingestion.manifest import (
    MANIFEST_FILENAME,
    load_manifest,
    manifest_files,
    refresh_manifest,
)

# ── Map type detection ─────────────────────────────────────────────────────────

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")


def _map_type_patterns() -> dict[str, tuple[str, ...]]:
    return map_type_patterns(display_keys=True)

# ── Safety limits (override via environment variables) ─────────────────────────

_LIMITS = app_settings().get("limits", {})
ZIP_MAX_BYTES = int(os.environ.get("OSIPI_ZIP_MAX_BYTES", str(_LIMITS.get("zip_max_bytes", 500 * 1024 * 1024))))
EXTRACT_MAX_BYTES = int(os.environ.get("OSIPI_EXTRACT_MAX_BYTES", str(_LIMITS.get("extract_max_bytes", 2 * 1024 * 1024 * 1024))))
EXTRACT_MAX_FILES = int(os.environ.get("OSIPI_EXTRACT_MAX_FILES", str(_LIMITS.get("extract_max_files", 10000))))

# Paths/filenames to silently skip when extracting ZIPs
_SKIP_PREFIXES = set(settings_tuple("ingestion", "skip_prefixes"))
_SKIP_NAMES    = set(settings_tuple("ingestion", "skip_names"))


# ── Public API, single submission (legacy) ───────────────────────────────────


def save_and_extract_batch(file_bytes: bytes, filename: str) -> Dict:
    """Extract a ZIP (bytes) and auto-detect single vs."""
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
    """Batch extraction from a ZIP already on disk."""
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
    """Save browser folder-upload files into a single submission folder."""
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

    preserve_common_root = bool(
        common_root and _names_an_identity_level(Path(common_root))
    )
    first_part = common_root or first_path.stem
    submission_id = _safe_id(first_part or "folder_submission")
    extracted_dir = _reset_submission_dir(submission_id)

    saved = 0
    for rel_path, contents in safe_files:
        if common_root and not preserve_common_root and len(rel_path.parts) > 1:
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
    """Save browser folder-upload files and auto-detect batch boundaries."""
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

    preserve_common_root = bool(
        common_root and _names_an_identity_level(Path(common_root))
    )
    saved = 0
    for rel_path, contents in safe_files:
        rel_stored = (
            Path(*rel_path.parts[1:])
            if common_root and not preserve_common_root and len(rel_path.parts) > 1
            else rel_path
        )
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
    """Run batch detection on an already-downloaded import directory."""
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

    try:
        detected_challenge = detect_challenge_type(folder)
    except Exception:
        detected_challenge = "unknown"

    try:
        inner = sorted(d.name for d in folder.iterdir()
                       if d.is_dir() and not d.name.startswith("."))
    except OSError:
        inner = []

    return {
        "nifti_count": nifti_count,
        "detected_parameter_map_type": detected,
        "detected_map_type_confidence": confidence,
        "detection_warning": warning,
        "detected_challenge_type": detected_challenge,
        "inner_folders": inner,
    }


def _is_submission_candidate(d: Path) -> bool:
    """Return True if *d* looks like a submission folder."""
    try:
        for p in d.rglob("*"):
            if p.is_file():
                return True
    except PermissionError:
        pass
    return False


def detect_batch_boundaries(extracted_dir: Path) -> Optional[List[Path]]:
    """Return the top-level subdirectories that are submission candidates."""
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

    if _is_structural_layout(submission_dirs):
        return None

    # A participant-level layout (P01..P10) is one submission, not a batch.
    if _is_participant_layout(submission_dirs):
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
    """Core post-staging logic shared by all import paths."""
    batch_dirs = detect_batch_boundaries(temp_dir)

    # ── Single submission ─────────────────────────────────────────────────────
    if not batch_dirs:
        final_dir = EXTRACTED_DIR / batch_stem
        if final_dir.exists():
            _replace_previous_extraction(final_dir)
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

    shared_files = sorted(
        item for item in batch_dirs[0].parent.iterdir() if item.is_file()
    )

    submissions = []
    try:
        for batch_dir in batch_dirs:
            sub_id = _safe_id(f"{batch_stem}_{batch_dir.name}")
            sub_dir = _reset_submission_dir(sub_id)
            for item in sorted(batch_dir.iterdir()):
                shutil.move(str(item), str(sub_dir / item.name))
            # Copied, not moved: every submission needs its own copy, and the
            # submission's own file of the same name always wins.
            for shared in shared_files:
                target = sub_dir / shared.name
                if not target.exists():
                    shutil.copy2(str(shared), str(target))
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


_STRUCTURAL_SUBDIRS: frozenset = frozenset(
    item.lower() for item in settings_tuple("ingestion", "structural_subdirs")
)


def _dataset_dir_names() -> frozenset:
    """Dataset directory names declared by any challenge, lowercased.

    A challenge such as DCE-2026 lays a submission out as
    ``<team>/Synthetic/…`` and ``<team>/Clinical/…``, so its dataset
    directories sit at exactly the level batch detection inspects. They name a
    *partition of one submission*, not separate teams, and are read from the
    challenge configuration rather than hardcoded so a challenge that declares
    its own dataset names is handled automatically.
    """
    from osipi_pipeline.config.rules import datasets_by_challenge

    return frozenset(
        str(name).lower()
        for spec in datasets_by_challenge().values()
        for name in spec
    )


def _is_structural_layout(dirs: List[Path]) -> bool:
    """Return True if every directory name is structural rather than a team."""
    if not dirs:
        return False
    known = _STRUCTURAL_SUBDIRS | _dataset_dir_names()
    return all(d.name.lower() in known for d in dirs)


def regroup_submissions(submission_ids: List[str], mode: str) -> Dict:
    """Re-decide whether one upload is one submission or several."""
    mode = (mode or "").strip().lower()
    if mode not in ("split", "merge"):
        return {"success": False, "error": "mode must be 'split' or 'merge'."}
    ids = [str(s).strip() for s in (submission_ids or []) if str(s).strip()]
    if not ids:
        return {"success": False, "error": "No submissions were given."}

    if mode == "split":
        if len(ids) != 1:
            return {"success": False, "error": "Splitting takes exactly one submission."}
        return _split_submission(_safe_id(ids[0]))
    if len(ids) < 2:
        return {"success": False, "error": "Merging takes two or more submissions."}
    return _merge_submissions([_safe_id(s) for s in ids])


def _split_submission(submission_id: str) -> Dict:
    source = EXTRACTED_DIR / submission_id
    if not source.is_dir():
        return {"success": False, "error": f"{submission_id} was not found."}

    children = sorted(d for d in source.iterdir() if d.is_dir())
    if len(children) < 2:
        return {"success": False,
                "error": "This submission has no inner folders to split into."}
    shared_files = sorted(item for item in source.iterdir() if item.is_file())

    staged = EXTRACTED_DIR / f".regroup-{uuid.uuid4().hex}"
    staged.mkdir(parents=True)
    try:
        planned = []
        for child in children:
            sub_id = _safe_id(f"{submission_id}_{child.name}")
            target = staged / sub_id
            target.mkdir()
            shutil.move(str(child), str(target / child.name))
            for shared in shared_files:
                copy_to = target / shared.name
                if not copy_to.exists():
                    shutil.copy2(str(shared), str(copy_to))
            planned.append((sub_id, target, child.name))

        submissions = []
        for sub_id, target, source_folder in planned:
            final = _reset_submission_dir(sub_id)
            for item in sorted(target.iterdir()):
                shutil.move(str(item), str(final / item.name))
            manifest = refresh_manifest(final, submission_id=sub_id,
                                        original_path=source_folder)
            submissions.append({
                "submission_id": sub_id,
                "source_folder": source_folder,
                "file_count": int(manifest.get("file_count") or 0),
                **detect_submission_metadata(sub_id),
            })
        shutil.rmtree(source, ignore_errors=True)
        return {"success": True, "batch": True, "mode": "split",
                "submissions": submissions, "count": len(submissions)}
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _merged_stem(submission_ids: List[str]) -> str:
    """What the upload was called before it was carved into these."""
    ids = [s for s in submission_ids if s]
    if not ids:
        return ""
    if len(ids) == 1:
        return ids[0]
    shared = os.path.commonprefix(ids)
    if "_" in shared:
        return shared[: shared.rfind("_")]
    # No shared boundary at all: the ids came from different uploads, so the
    # first is a defensible name and inventing a merged one would be worse.
    return shared or ids[0]


def _merge_submissions(submission_ids: List[str]) -> Dict:
    sources = [EXTRACTED_DIR / s for s in submission_ids]
    missing = [s.name for s in sources if not s.is_dir()]
    if missing:
        return {"success": False, "error": f"Not found: {', '.join(missing)}."}

    merged_id = _safe_id(_merged_stem(submission_ids))

    staged = EXTRACTED_DIR / f".regroup-{uuid.uuid4().hex}"
    staged.mkdir(parents=True)
    try:
        for source, sub_id in zip(sources, submission_ids):
            recorded = (load_manifest(source) or {}).get("original_path")
            name = str(recorded or "").strip() or sub_id[len(merged_id):].lstrip("_") or sub_id
            name = Path(name).name
            inner = staged / name
            inner.mkdir(parents=True, exist_ok=True)
            preserved_identity_dir = source / name
            if preserved_identity_dir.is_dir():
                for item in sorted(preserved_identity_dir.iterdir()):
                    shutil.move(str(item), str(inner / item.name))
                for item in sorted(source.iterdir()):
                    if item == preserved_identity_dir or item.name == MANIFEST_FILENAME:
                        continue
                    destination = staged / item.name
                    if not destination.exists():
                        shutil.move(str(item), str(destination))
            else:
                # Backwards compatibility for submissions split by older
                # versions, which promoted site/scan folders to the root.
                for item in sorted(source.iterdir()):
                    if item.name != MANIFEST_FILENAME:
                        shutil.move(str(item), str(inner / item.name))

        final = _reset_submission_dir(merged_id)
        for item in sorted(staged.iterdir()):
            shutil.move(str(item), str(final / item.name))
        for source in sources:
            if source.resolve() != final.resolve():
                shutil.rmtree(source, ignore_errors=True)
        manifest = refresh_manifest(final, submission_id=merged_id,
                                    original_path=merged_id)
        return {"success": True, "batch": False, "mode": "merge",
                "submission_id": merged_id,
                "file_count": int(manifest.get("file_count") or 0),
                **detect_submission_metadata(merged_id)}
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def _is_participant_layout(dirs: List[Path]) -> bool:
    """True when directories describe scans and optional internal data.

    Participants, sites and repeats are all levels *inside* one submission::

        submission/          P01/             P01/site_1/
            P01/                 site_1/          scan_1/
            P02/                 site_2/          scan_2/

    Splitting on any of them would cause INCOMPLETE_ARTIFACT_IDENTITY errors.
    A real batch has team-named directories (``Team_A/``, ``Team_B/``).
    At least one directory must carry scan identity and every other directory
    must be structural.
    """
    if len(dirs) < 2:
        return False
    from osipi_pipeline.ingestion.identity_parser import parse_directory_identity

    structural_names = _STRUCTURAL_SUBDIRS | _dataset_dir_names()
    found_identity = False
    for directory in dirs:
        # The parser reads directory components and ignores the last element,
        # which is normally the filename, hence the sentinel.
        identity = parse_directory_identity((directory.name, "_"))
        if {"participant", "site", "repeat"} & set(identity):
            found_identity = True
            continue
        if directory.name.lower() not in structural_names:
            return False
    return found_identity


def _replace_previous_extraction(final_dir: Path) -> None:
    """Delete a previous extraction of the same submission, with a clear error on failure."""
    try:
        shutil.rmtree(final_dir)
    except OSError as exc:
        raise RuntimeError(
            f"Could not replace the previous version of this submission at "
            f"{final_dir.name}. Uploading again deletes what was extracted "
            f"last time, and that delete failed ({exc.strerror or exc}). "
            f"The folder may be open in another program, or the account "
            f"running the app may not have permission to write there. "
            f"Deleting {final_dir} by hand and uploading again will clear it."
        ) from exc


def _redundant_wrapper(staged_dir: Path) -> Optional[Path]:
    """Return the inner directory if *staged_dir* is just a single wrapper folder.

    Returns the inner directory only when *staged_dir* contains exactly one
    entry, that entry is a directory, and its name is not a structural subdir
    (``input``/``results``/...) or a participant, site or repeat folder.
    Returns None otherwise.
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
        and not _names_an_identity_level(dirs[0])
    ):
        return dirs[0]
    return None


def _names_an_identity_level(directory: Path) -> bool:
    """Whether this directory name carries scan identity rather than nothing."""
    from osipi_pipeline.ingestion.identity_parser import parse_directory_identity

    identity = parse_directory_identity((directory.name, "_"))
    return bool({"dataset", "participant", "site", "repeat"} & set(identity))


def _check_inner_batch(wrapper_dir: Path) -> Optional[List[Path]]:
    """One-level wrapper-unwrap: check if wrapper_dir itself is a batch container."""
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
    # this is a single submission with an internal data layout, not a batch.
    if _is_structural_layout(submission_dirs):
        return None

    # A participant-level layout (P01..P10) is one submission, not a batch.
    if _is_participant_layout(submission_dirs):
        return None

    return submission_dirs


def _auto_extract_single_zip(directory: Path) -> None:
    """Extract ZIP file(s) in a directory if no NIfTI files are present yet."""
    try:
        all_items = [f for f in directory.iterdir() if f.is_file()]
    except PermissionError:
        return

    zip_files = [f for f in all_items if f.name.lower().endswith(".zip")]
    if not zip_files:
        return  # Nothing to extract

    # If NIfTI files are already present the record is already unpacked, leave it.
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
    """Extract ZIP safely, skipping junk files and enforcing size/count limits."""
    planned: List[Tuple[zipfile.ZipInfo, Path]] = []
    path_kinds: dict[str, str] = {}
    for member in zf.infolist():
        if _should_skip_path(member.filename):
            continue
        try:
            rel_path = _safe_relative_path(member.filename)
        except ValueError as exc:
            raise ValueError(
                f"ZIP contains an unsafe path: {member.filename!r}."
            ) from exc
        if member.flag_bits & 0x1:
            raise ValueError(
                f"ZIP contains an encrypted file, which is not supported: {member.filename!r}."
            )

        key = unicodedata.normalize("NFC", rel_path.as_posix()).casefold()
        kind = "directory" if member.is_dir() else "file"
        previous = path_kinds.get(key)
        if previous == kind == "file":
            raise ValueError(
                f"ZIP contains the same file more than once: {rel_path.as_posix()!r}."
            )
        if previous and previous != kind:
            raise ValueError(
                f"ZIP contains conflicting entries for {rel_path.as_posix()!r}."
            )

        for depth in range(1, len(rel_path.parts)):
            parent = Path(*rel_path.parts[:depth])
            parent_key = unicodedata.normalize("NFC", parent.as_posix()).casefold()
            if path_kinds.get(parent_key) == "file":
                raise ValueError(
                    f"ZIP file {parent.as_posix()!r} conflicts with a containing directory."
                )
            path_kinds.setdefault(parent_key, "directory")
        if kind == "file" and previous == "directory":
            raise ValueError(
                f"ZIP file {rel_path.as_posix()!r} conflicts with a directory."
            )
        path_kinds[key] = kind
        planned.append((member, rel_path))

    file_count = 0
    extracted_bytes = 0
    CHUNK = 65536  # 64 KB streaming chunks

    for member, rel_path in planned:
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

        try:
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
        except (RuntimeError, NotImplementedError) as exc:
            raise ValueError(
                f"ZIP member {member.filename!r} could not be extracted: {exc}"
            ) from exc

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
