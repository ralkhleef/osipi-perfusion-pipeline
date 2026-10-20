"""Header and orientation agreement between a submission and the ground truth."""

from __future__ import annotations

import pytest

np = pytest.importorskip("numpy")
nib = pytest.importorskip("nibabel")

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import scoring  # noqa: E402


RAS = np.diag([1.0, 1.0, 1.0, 1.0])


@pytest.fixture()
def build(tmp_path):
    """Write a NIfTI and load it the way the scorer does."""
    counter = {"n": 0}

    def _build(affine=RAS, shape=(8, 8, 8), dtype=np.float32):
        counter["n"] += 1
        path = tmp_path / f"map_{counter['n']}.nii.gz"
        data = np.arange(int(np.prod(shape)), dtype=np.float64).reshape(shape)
        nib.save(nib.Nifti1Image(data.astype(dtype), affine), path)
        return scoring._load_nifti_values(path)

    return _build


def test_a_matching_header_reports_matches(build):
    assert scoring._header_check(build(), build())["status"] == "matches"


def test_a_left_right_flip_is_caught(build):
    flipped = build(affine=np.diag([-1.0, 1.0, 1.0, 1.0]))
    check = scoring._header_check(flipped, build())

    assert check["status"] == "geometry_mismatch"
    assert check["mismatched_fields"] == ["orientation"]
    assert check["fields"]["shape"]["matches"] is True, \
        "shape is unchanged by a flip, which is why orientation must be checked"


def test_a_different_voxel_size_is_caught(build):
    check = scoring._header_check(build(affine=np.diag([2.0, 2.0, 2.0, 1.0])), build())
    assert check["status"] == "geometry_mismatch"
    assert "voxel_size" in check["mismatched_fields"]


def test_a_different_shape_is_caught(build):
    check = scoring._header_check(build(shape=(8, 8, 9)), build())
    assert check["status"] == "geometry_mismatch"
    assert "shape" in check["mismatched_fields"]


def test_a_different_dtype_alone_is_not_a_geometry_problem(build):
    """Submitting float64 against a float32 reference is not an error."""
    check = scoring._header_check(build(dtype=np.float64), build())
    assert check["status"] == "dtype_differs"
    assert check["mismatched_fields"] == ["dtype"]


def test_negligible_voxel_size_differences_are_not_reported(build):
    """Headers written by different tools disagree in the last decimal."""
    almost = np.diag([1.0 + 1e-9, 1.0, 1.0, 1.0])
    assert scoring._header_check(build(affine=almost), build())["status"] == "matches"


def test_both_values_are_reported_not_just_the_verdict(build):
    """A reviewer needs to see what differs, not only that something does."""
    check = scoring._header_check(build(affine=np.diag([-1.0, 1.0, 1.0, 1.0])), build())
    orientation = check["fields"]["orientation"]
    assert orientation["submitted"] == ["L", "A", "S"]
    assert orientation["reference"] == ["R", "A", "S"]


def test_a_field_neither_file_declares_is_not_verified_rather_than_matching(build):
    """Unknown must never be reported as agreement."""
    submitted = dict(build())
    reference = dict(build())
    submitted["axis_codes"] = None
    reference["axis_codes"] = None

    check = scoring._header_check(submitted, reference)
    assert check["fields"]["orientation"]["matches"] is None
    assert "orientation" not in check["mismatched_fields"]


def test_nothing_verifiable_at_all_reports_not_verified(build):
    empty = {"shape": None, "voxel_size": None, "axis_codes": None, "dtype": None}
    assert scoring._header_check(empty, empty)["status"] == "not_verified"


def test_the_loader_records_orientation_and_dtype(build):
    """The check is only as good as what the loader captured."""
    loaded = build(dtype=np.int16)
    assert loaded["axis_codes"] == ["R", "A", "S"]
    assert "int16" in str(loaded["dtype"])
