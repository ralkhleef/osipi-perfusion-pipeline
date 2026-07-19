"""Check whether .nii / .nii.gz files can actually be opened and read."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from osipi_pipeline.ingestion.manifest import config_fingerprint
from osipi_pipeline.performance import configured_worker_limit, timed

try:
    import nibabel as nib
    import numpy as np

    _NIBABEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NIBABEL_AVAILABLE = False

_CACHE_LOCK = threading.Lock()
_VALIDATION_CACHE: dict[tuple[str, int, int, str, str], dict[str, Any]] = {}
_LAST_WORKER_COUNT = 1


def clear_validation_cache() -> None:
    with _CACHE_LOCK:
        _VALIDATION_CACHE.clear()


def last_worker_count() -> int:
    return _LAST_WORKER_COUNT


def validate_nifti_files(
    nifti_paths: list[Path],
    *,
    force_refresh: bool = False,
    quick: bool = False,
    workers: int | None = None,
) -> list[dict[str, Any]]:
    """Validate NIfTI files and return results in input order."""

    global _LAST_WORKER_COUNT
    if not nifti_paths:
        _LAST_WORKER_COUNT = 1
        return []
    worker_count = workers or configured_worker_limit("nifti_validation_workers", 3, ceiling=6)
    worker_count = max(1, min(int(worker_count), len(nifti_paths)))
    _LAST_WORKER_COUNT = worker_count

    with timed("validation.nifti.batch", file_count=len(nifti_paths), workers=worker_count, quick=quick):
        if worker_count <= 1:
            return [
                _validate_single_cached(path, force_refresh=force_refresh, quick=quick)
                for path in nifti_paths
            ]
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="nifti-validate") as executor:
            return list(executor.map(
                lambda path: _validate_single_cached(path, force_refresh=force_refresh, quick=quick),
                nifti_paths,
            ))


def _make_result(path: Path) -> dict[str, Any]:
    return {
        "file_path": str(path),
        "valid": False,
        "errors": [],
        "warnings": [],
        "shape": None,
        "dtype": None,
        "min": None,
        "max": None,
        "mean": None,
        "nan_count": None,
        "inf_count": None,
        "validation_mode": "deep",
        "cache_hit": False,
    }


def _cache_key(path: Path, *, quick: bool) -> tuple[str, int, int, str, str] | None:
    try:
        stat = path.stat()
        resolved = str(path.resolve())
    except OSError:
        return None
    mode = "quick" if quick else "deep"
    return resolved, int(stat.st_size), int(stat.st_mtime_ns), mode, config_fingerprint()


def _validate_single_cached(path: Path, *, force_refresh: bool, quick: bool) -> dict[str, Any]:
    key = _cache_key(path, quick=quick)
    if key and not force_refresh:
        with _CACHE_LOCK:
            cached = _VALIDATION_CACHE.get(key)
        if cached is not None:
            result = copy.deepcopy(cached)
            result["cache_hit"] = True
            return result

    result = _validate_single(path, quick=quick)
    if key:
        with _CACHE_LOCK:
            _VALIDATION_CACHE[key] = copy.deepcopy(result)
    return result


def _validate_single(path: Path, *, quick: bool = False) -> dict[str, Any]:
    result = _make_result(path)
    result["validation_mode"] = "quick" if quick else "deep"

    if not _NIBABEL_AVAILABLE:  # pragma: no cover
        result["errors"].append("nibabel is not installed; cannot validate NIfTI files.")
        return result

    try:
        stat = path.stat()
    except OSError as exc:
        result["errors"].append(f"Could not stat file: {exc}")
        return result

    if stat.st_size == 0:
        result["errors"].append("File is 0 bytes; skipping nibabel load.")
        return result

    try:
        with timed("validation.nifti.open", path=str(path), quick=quick):
            img = nib.load(str(path))
    except Exception as exc:
        result["errors"].append(f"nibabel could not load file: {exc}")
        return result

    shape = img.shape
    if not shape:
        result["errors"].append("NIfTI image has no shape.")
        return result
    result["shape"] = list(shape)

    if len(shape) < 3:
        result["warnings"].append(
            f"Image shape {tuple(shape)} has fewer than 3 dimensions; "
            "expected at least 3D for a perfusion parameter map."
        )

    affine = img.affine
    if affine is None:
        result["errors"].append("NIfTI image has no affine matrix.")
        return result
    if affine.shape != (4, 4):
        result["errors"].append(f"Affine matrix has shape {affine.shape}; expected (4, 4).")
        return result

    result["dtype"] = str(img.get_data_dtype())
    if quick:
        result["valid"] = True
        return result

    try:
        with timed("validation.nifti.voxels", path=str(path)):
            data = np.asarray(img.dataobj, dtype=np.float32)
    except Exception as exc:
        result["warnings"].append(f"Could not read image data array: {exc}")
        result["valid"] = True
        return result

    result["dtype"] = str(data.dtype)
    try:
        finite_mask = np.isfinite(data)
        nan_count = int(np.sum(np.isnan(data)))
        inf_count = int(np.sum(np.isinf(data)))
        result["nan_count"] = nan_count
        result["inf_count"] = inf_count

        if nan_count > 0:
            result["warnings"].append(f"Image contains {nan_count} NaN value(s).")
        if inf_count > 0:
            result["warnings"].append(f"Image contains {inf_count} infinite value(s).")

        if bool(finite_mask.any()):
            finite_data = data[finite_mask]
            result["min"] = float(np.min(finite_data))
            result["max"] = float(np.max(finite_data))
            result["mean"] = float(np.mean(finite_data, dtype=np.float64))
        else:
            result["warnings"].append("Image contains no finite values (all NaN or infinite).")
    except Exception as exc:
        result["warnings"].append(f"Could not compute image statistics: {exc}")

    result["valid"] = True
    return result
