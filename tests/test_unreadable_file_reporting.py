"""An unreadable file must not look like a file with nothing to say.

Found during a walkthrough. A corrupt NIfTI produced a preview item with an
empty shape, no orientation and no dtype, which is exactly what a readable file
with a blank header produces. The header check then reported "Not verified",
which a reviewer reads as "we did not look" when the truth is "we looked and
the file will not open".

That distinction matters here more than in most places. The header check exists
because a submission can be the right shape, score plausibly, and still be
flipped or at the wrong voxel size. A reviewer who sees "not verified" moves
on; one who sees "could not be read" does not.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


@pytest.fixture()
def files(tmp_path):
    import nibabel as nib
    good = tmp_path / "cbf.nii.gz"
    nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), dtype=np.float32), np.eye(4)), str(good))
    bad = tmp_path / "att.nii.gz"
    bad.write_bytes(b"\x1f\x8b\x08 this is not a nifti")
    return good, bad


def test_a_readable_file_reports_no_error(files) -> None:
    from services.nifti_preview_service import _base_preview_item
    good, _bad = files
    item = _base_preview_item("probe", good)
    assert item["read_error"] is None
    assert item["shape"] == [4, 4, 4]


def test_an_unreadable_file_says_why(files) -> None:
    from services.nifti_preview_service import _base_preview_item
    _good, bad = files
    item = _base_preview_item("probe", bad)
    assert item["read_error"], "a corrupt file reported no error at all"
    assert isinstance(item["read_error"], str)


def test_the_two_cases_are_distinguishable(files) -> None:
    """The actual bug: both used to be an empty shape and nothing else."""
    from services.nifti_preview_service import _base_preview_item
    good, bad = files
    a, b = _base_preview_item("p", good), _base_preview_item("p", bad)
    assert (a["read_error"] is None) != (b["read_error"] is None), (
        "a corrupt file is still indistinguishable from a readable one")


# ── The header check verdict ──────────────────────────────────────────────

def _check(submitted: dict, reference: dict) -> dict:
    from scoring import _header_check
    return _header_check(submitted, reference)


def test_matching_headers_still_match() -> None:
    header = {"shape": [4, 4, 4], "voxel_size": [1, 1, 1],
              "axis_codes": ["R", "A", "S"], "dtype": "float32"}
    assert _check(dict(header), dict(header))["status"] == "matches"


def test_a_dtype_difference_is_still_harmless() -> None:
    a = {"shape": [4, 4, 4], "axis_codes": ["R", "A", "S"], "dtype": "float64"}
    b = {"shape": [4, 4, 4], "axis_codes": ["R", "A", "S"], "dtype": "float32"}
    assert _check(a, b)["status"] == "dtype_differs"


def test_nothing_comparable_is_still_not_verified() -> None:
    assert _check({}, {})["status"] == "not_verified"


def test_an_unreadable_submission_is_not_reported_as_not_verified() -> None:
    """The regression this whole file exists for."""
    bad = {"read_error": "invalid stored block lengths"}
    good = {"shape": [4, 4, 4], "axis_codes": ["R", "A", "S"], "dtype": "float32"}
    result = _check(bad, good)
    assert result["status"] == "unreadable", result["status"]
    assert result["unreadable_sides"] == ["submitted"]


def test_an_unreadable_reference_is_named_as_the_reference() -> None:
    """Whose file is broken changes who has to fix it."""
    good = {"shape": [4, 4, 4], "axis_codes": ["R", "A", "S"], "dtype": "float32"}
    bad = {"read_error": "truncated"}
    assert _check(good, bad)["unreadable_sides"] == ["reference"]


def test_unreadable_outranks_a_geometry_mismatch() -> None:
    """Comparing geometry against a file that will not open is meaningless."""
    bad = {"read_error": "boom", "shape": [1, 1, 1], "axis_codes": ["L", "A", "S"]}
    good = {"shape": [4, 4, 4], "axis_codes": ["R", "A", "S"]}
    assert _check(bad, good)["status"] == "unreadable"


def test_a_readable_pair_carries_no_unreadable_marker() -> None:
    header = {"shape": [4, 4, 4], "axis_codes": ["R", "A", "S"], "dtype": "float32"}
    assert "unreadable_sides" not in _check(dict(header), dict(header))


# ── The verdict reaches a human ───────────────────────────────────────────

def test_both_the_report_and_the_interface_can_name_the_new_verdict() -> None:
    """A status no renderer knows about shows up as a raw identifier."""
    from services.pdf_report_service import _HEADER_CHECK_VERDICTS
    assert "unreadable" in _HEADER_CHECK_VERDICTS
    assert _HEADER_CHECK_VERDICTS["unreadable"] != _HEADER_CHECK_VERDICTS["not_verified"], (
        "the two states render identically, so the distinction is invisible")

    app_js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "unreadable: \"File could not be read\"" in app_js, (
        "the interface has no wording for an unreadable file")
