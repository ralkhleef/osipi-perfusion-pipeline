"""Tests for nifti_validator.py.

Covers: valid files pass, fake files fail, NaN/inf produce warnings not errors,
result dict always includes shape/dtype/stats.
"""

from __future__ import annotations

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from osipi_pipeline.validation.nifti_validator import validate_nifti_files


def _save_nifti(path: Path, data: np.ndarray) -> Path:
    """Save a NIfTI image to disk and return the path."""
    img = nib.Nifti1Image(data, affine=np.eye(4))
    nib.save(img, str(path))
    return path


def test_valid_nifti_passes(tmp_path: Path) -> None:
    data = np.zeros((4, 4, 4), dtype=np.float32)
    nifti_path = _save_nifti(tmp_path / "clean.nii.gz", data)

    results = validate_nifti_files([nifti_path])

    assert len(results) == 1
    r = results[0]
    assert r["valid"] is True
    assert r["errors"] == []
    assert r["warnings"] == []


def test_valid_nifti_includes_shape_dtype_stats(tmp_path: Path) -> None:
    data = np.arange(27, dtype=np.float32).reshape(3, 3, 3)
    nifti_path = _save_nifti(tmp_path / "stats.nii.gz", data)

    results = validate_nifti_files([nifti_path])
    r = results[0]

    assert r["shape"] == [3, 3, 3]
    assert r["dtype"] is not None
    assert r["min"] == pytest.approx(0.0)
    assert r["max"] == pytest.approx(26.0)
    assert r["mean"] == pytest.approx(13.0)
    assert r["nan_count"] == 0
    assert r["inf_count"] == 0


def test_fake_nifti_file_fails(tmp_path: Path) -> None:
    fake = tmp_path / "not_a_nifti.nii.gz"
    fake.write_text("this is plain text, not a NIfTI file", encoding="utf-8")

    results = validate_nifti_files([fake])
    r = results[0]

    assert r["valid"] is False
    assert len(r["errors"]) > 0
    assert any("nibabel" in err.lower() or "load" in err.lower() for err in r["errors"])


def test_nan_inf_image_warns_not_crashes(tmp_path: Path) -> None:
    data = np.zeros((3, 3, 3), dtype=np.float32)
    data[0, 0, 0] = float("nan")
    data[1, 1, 1] = float("inf")
    nifti_path = _save_nifti(tmp_path / "nan_inf.nii.gz", data)

    results = validate_nifti_files([nifti_path])
    r = results[0]

    # File is still structurally valid, no errors, just warnings.
    assert r["valid"] is True
    assert r["errors"] == []
    assert r["nan_count"] == 1
    assert r["inf_count"] == 1
    warning_text = " ".join(r["warnings"])
    assert "NaN" in warning_text
    assert "infinite" in warning_text


def test_nan_only_image_reports_correct_counts(tmp_path: Path) -> None:
    data = np.full((2, 2, 2), float("nan"), dtype=np.float32)
    nifti_path = _save_nifti(tmp_path / "all_nan.nii.gz", data)

    results = validate_nifti_files([nifti_path])
    r = results[0]

    assert r["valid"] is True
    assert r["nan_count"] == 8
    assert r["inf_count"] == 0
    # No finite values means min/max/mean stay None.
    assert r["min"] is None
    assert r["max"] is None
    assert r["mean"] is None
    assert any("no finite" in w.lower() for w in r["warnings"])


def test_multiple_files_all_returned(tmp_path: Path) -> None:
    paths = []
    for i in range(3):
        data = np.zeros((2, 2, 2), dtype=np.float32) + i
        paths.append(_save_nifti(tmp_path / f"map_{i}.nii.gz", data))

    results = validate_nifti_files(paths)

    assert len(results) == 3
    for i, r in enumerate(results):
        assert r["valid"] is True
        assert r["mean"] == pytest.approx(float(i))


def test_2d_image_warns_about_dimensionality(tmp_path: Path) -> None:
    data = np.zeros((5, 5), dtype=np.float32)
    nifti_path = _save_nifti(tmp_path / "flat.nii", data)

    results = validate_nifti_files([nifti_path])
    r = results[0]

    assert r["valid"] is True
    assert r["shape"] == [5, 5]
    assert any("fewer than 3" in w for w in r["warnings"])


def test_zero_byte_file_reports_error_in_summary(tmp_path: Path) -> None:
    empty = tmp_path / "empty.nii.gz"
    empty.write_bytes(b"")

    results = validate_nifti_files([empty])
    r = results[0]

    assert r["valid"] is False
    assert len(r["errors"]) > 0
