"""Check whether .nii / .nii.gz files can actually be opened and read."""

from __future__ import annotations

import copy
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
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

MAX_VOXELS_PER_READ = 32 * 1024 * 1024


class _VoxelStats:
    """Statistics accumulated over an image read in pieces."""

    __slots__ = ("nan_count", "inf_count", "finite_count", "total",
                 "minimum", "maximum")

    def __init__(self) -> None:
        self.nan_count = 0
        self.inf_count = 0
        self.finite_count = 0
        self.total = 0.0
        self.minimum = 0.0
        self.maximum = 0.0


def _last_axis_chunks(shape: tuple[int, ...], max_voxels: int):
    """Yield (start, stop) along the last axis, each at most max_voxels."""
    if not shape:
        return
    last = int(shape[-1])
    plane = 1
    for dim in shape[:-1]:
        plane *= int(dim)
    if last <= 0 or plane <= 0:
        return
    # At least one index per read, even if a single plane exceeds the budget:
    # refusing to read is worse than briefly exceeding it.
    step = max(1, int(max_voxels // plane))
    for start in range(0, last, step):
        yield start, min(start + step, last)


def _streaming_stats(dataobj: Any, shape: tuple[int, ...],
                     max_voxels: int = MAX_VOXELS_PER_READ) -> "_VoxelStats":
    """NaN, infinite, min, max and mean, without holding the whole image."""
    stats = _VoxelStats()
    seen_finite = False
    for start, stop in _last_axis_chunks(shape, max_voxels):
        chunk = np.asarray(dataobj[..., start:stop], dtype=np.float32)
        size = chunk.size
        if not size:
            continue

        with np.errstate(invalid="ignore"):
            low = float(chunk.min())
            high = float(chunk.max())
            total = float(chunk.sum(dtype=np.float64))

        if np.isfinite(low) and np.isfinite(high) and np.isfinite(total):
            finite_count = size          # the common case, and now the cheap one
        else:
            # Something is not finite, so now it is worth paying to say what.
            finite = np.isfinite(chunk)
            finite_count = int(np.count_nonzero(finite))
            nan_count = int(np.count_nonzero(np.isnan(chunk)))
            stats.nan_count += nan_count
            stats.inf_count += (size - finite_count) - nan_count
            if finite_count:
                # where= keeps the non-finite values out of the result without
                # materialising the finite ones.
                low = float(chunk.min(where=finite, initial=np.inf))
                high = float(chunk.max(where=finite, initial=-np.inf))
                total = float(chunk.sum(dtype=np.float64, where=finite))
            del finite

        if finite_count:
            stats.minimum = low if not seen_finite else min(stats.minimum, low)
            stats.maximum = high if not seen_finite else max(stats.maximum, high)
            stats.total += total
            stats.finite_count += finite_count
            seen_finite = True
        del chunk
    return stats


def clear_validation_cache() -> None:
    with _CACHE_LOCK:
        _VALIDATION_CACHE.clear()


def last_worker_count() -> int:
    return _LAST_WORKER_COUNT


LARGE_FILE_BYTES = 2 * 1024 * 1024


def _check_one(payload: tuple) -> dict[str, Any]:
    """Validate one file."""
    path, force_refresh, quick = payload
    return _validate_single_cached(Path(path), force_refresh=force_refresh, quick=quick)


def _validate_large_files(
    paths: list[Path], *, force_refresh: bool, quick: bool, workers: int,
) -> list[dict[str, Any]]:
    """Read the large files in parallel *processes*, falling back to serial."""
    if workers <= 1 or len(paths) <= 1:
        return [_check_one((path, force_refresh, quick)) for path in paths]
    payloads = [(str(path), force_refresh, quick) for path in paths]
    try:
        with ProcessPoolExecutor(max_workers=min(workers, len(paths))) as executor:
            return list(executor.map(_check_one, payloads))
    except Exception:  # noqa: BLE001 - any pool failure is a fallback, not an error
        return [_check_one(payload) for payload in payloads]


def _is_large(path: Path) -> bool:
    """Whether reading this file will dominate a worker for a long time."""
    try:
        return path.stat().st_size >= LARGE_FILE_BYTES
    except OSError:
        return False


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

    def check(path: Path) -> dict[str, Any]:
        return _validate_single_cached(path, force_refresh=force_refresh, quick=quick)

    with timed("validation.nifti.batch", file_count=len(nifti_paths),
               workers=worker_count, quick=quick):
        # `quick` never touches the pixel data, so size is irrelevant there and
        # splitting the batch would only add bookkeeping.
        if worker_count <= 1 or quick:
            if worker_count <= 1:
                return [check(path) for path in nifti_paths]
            with ThreadPoolExecutor(max_workers=worker_count,
                                    thread_name_prefix="nifti-validate") as executor:
                return list(executor.map(check, nifti_paths))

        large = [i for i, path in enumerate(nifti_paths) if _is_large(path)]
        if not large:
            with ThreadPoolExecutor(max_workers=worker_count,
                                    thread_name_prefix="nifti-validate") as executor:
                return list(executor.map(check, nifti_paths))

        results: list[Any] = [None] * len(nifti_paths)
        large_set = set(large)
        small = [i for i in range(len(nifti_paths)) if i not in large_set]
        if small:
            with ThreadPoolExecutor(max_workers=worker_count,
                                    thread_name_prefix="nifti-validate") as executor:
                for index, result in zip(small, executor.map(
                        check, [nifti_paths[i] for i in small])):
                    results[index] = result
        for index, result in zip(large, _validate_large_files(
                [nifti_paths[i] for i in large],
                force_refresh=force_refresh, quick=quick, workers=worker_count)):
            results[index] = result
        return results


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
            img = nib.load(str(path), keep_file_open=True)
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

    source_dtype = str(img.get_data_dtype())
    result["dtype"] = source_dtype
    if quick:
        result["valid"] = True
        return result

    try:
        with timed("validation.nifti.voxels", path=str(path)):
            stats = _streaming_stats(img.dataobj, shape)
    except Exception as exc:
        result["errors"].append(f"Could not read NIfTI voxel data: {exc}")
        return result

    result["dtype"] = source_dtype
    try:
        result["nan_count"] = stats.nan_count
        result["inf_count"] = stats.inf_count

        if stats.nan_count > 0:
            result["warnings"].append(f"Image contains {stats.nan_count} NaN value(s).")
        if stats.inf_count > 0:
            result["warnings"].append(f"Image contains {stats.inf_count} infinite value(s).")

        if stats.finite_count:
            result["min"] = stats.minimum
            result["max"] = stats.maximum
            result["mean"] = stats.total / stats.finite_count
        else:
            result["warnings"].append("Image contains no finite values (all NaN or infinite).")
    except Exception as exc:
        result["warnings"].append(f"Could not compute image statistics: {exc}")

    result["valid"] = True
    return result
