"""Check whether .nii / .nii.gz files can actually be opened and read.

This does basic readability validation only — it does not score results,
compare against reference data, or apply any clinical thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

try:
    import nibabel as nib
    import numpy as np

    _NIBABEL_AVAILABLE = True
except ImportError:  # pragma: no cover
    _NIBABEL_AVAILABLE = False


def validate_nifti_files(nifti_paths: list[Path]) -> list[dict[str, Any]]:
    """Validate a list of NIfTI files and return one result dict per file."""
    return [_validate_single(path) for path in nifti_paths]


def _make_result(path: Path) -> dict[str, Any]:
    """Start with a blank result for the given file path."""
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
    }


def _validate_single(path: Path) -> dict[str, Any]:
    """Inspect one NIfTI file and return a populated result dict."""

    result = _make_result(path)

    if not _NIBABEL_AVAILABLE:  # pragma: no cover
        result["errors"].append("nibabel is not installed; cannot validate NIfTI files.")
        return result

    # 0-byte files are already flagged by the main validation loop, so we
    # skip nibabel here to avoid reporting the same problem twice.
    if path.stat().st_size == 0:
        result["errors"].append("File is 0 bytes; skipping nibabel load.")
        return result

    # Try to load with nibabel.
    try:
        img = nib.load(str(path))
    except Exception as exc:
        result["errors"].append(f"nibabel could not load file: {exc}")
        return result

    # Check shape — expect at least 3D for a parameter map.
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

    # Check affine — must exist and be 4x4.
    affine = img.affine
    if affine is None:
        result["errors"].append("NIfTI image has no affine matrix.")
        return result

    if affine.shape != (4, 4):
        result["errors"].append(
            f"Affine matrix has shape {affine.shape}; expected (4, 4)."
        )
        return result

    # Collect basic image stats.
    result["dtype"] = str(img.get_data_dtype())

    try:
        data = img.get_fdata()
    except Exception as exc:
        # Header was fine; treat a data-read failure as a warning, not an error.
        result["warnings"].append(f"Could not read image data array: {exc}")
        result["valid"] = True
        return result

    result["dtype"] = str(data.dtype)

    try:
        nan_count = int(np.sum(np.isnan(data)))
        inf_count = int(np.sum(np.isinf(data)))
        result["nan_count"] = nan_count
        result["inf_count"] = inf_count

        # NaN and inf are warnings only — some maps use NaN for masked voxels.
        if nan_count > 0:
            result["warnings"].append(f"Image contains {nan_count} NaN value(s).")
        if inf_count > 0:
            result["warnings"].append(f"Image contains {inf_count} infinite value(s).")

        finite_data = data[np.isfinite(data)]
        if finite_data.size > 0:
            result["min"] = float(np.min(finite_data))
            result["max"] = float(np.max(finite_data))
            result["mean"] = float(np.mean(finite_data))
        else:
            result["warnings"].append("Image contains no finite values (all NaN or infinite).")

    except Exception as exc:
        result["warnings"].append(f"Could not compute image statistics: {exc}")

    result["valid"] = True
    return result
