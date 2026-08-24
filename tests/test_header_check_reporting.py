"""The header check as a reviewer actually sees it.

``tests/test_header_check.py`` covers the comparison itself. This file covers
the step after it: the check was computed and stored on the row for a while
before anything rendered it, so a flipped submission was caught internally and
never mentioned to the person reviewing it. These tests fail if that happens
again in either output format.

The two formats are built from one model, so the rows are asserted on the
model where possible and on the rendered HTML where the rendering is the
thing at risk.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend")]

from services.pdf_report_service import (  # noqa: E402
    _build_report_model,
    _header_check_model,
    _header_field_text,
)


def _field(submitted, reference, matches):
    return {"submitted": submitted, "reference": reference, "matches": matches}


def _summary(check, *, map_type="cbf", team="Team Alpha"):
    """A submission summary carrying one compared map with a header check."""
    return {
        "submission_id": "s1",
        "source_folder": team,
        "challenge_type": "asl",
        "nifti_analysis": {
            "reference_scoring": {
                "status": "available",
                "maps": [{
                    "detected_map_type": map_type,
                    "submitted_file": "submission_cbf.nii.gz",
                    "header_check": check,
                }],
            },
        },
    }


def _matching_check():
    return {
        "status": "matches",
        "mismatched_fields": [],
        "fields": {
            "shape": _field([64, 64, 20], [64, 64, 20], True),
            "voxel_size": _field([3.0, 3.0, 5.0], [3.0, 3.0, 5.0], True),
            "orientation": _field(["L", "A", "S"], ["L", "A", "S"], True),
            "dtype": _field("float32", "float32", True),
        },
    }


def _flipped_check():
    """The case that matters: same shape, opposite first axis."""
    return {
        "status": "geometry_mismatch",
        "mismatched_fields": ["orientation"],
        "fields": {
            "shape": _field([64, 64, 20], [64, 64, 20], True),
            "voxel_size": _field([3.0, 3.0, 5.0], [3.0, 3.0, 5.0], True),
            "orientation": _field(["R", "A", "S"], ["L", "A", "S"], False),
            "dtype": _field("float32", "float32", True),
        },
    }


# ── Field rendering ───────────────────────────────────────────────────────

def test_a_matching_field_shows_its_value_once():
    assert _header_field_text(_field([64, 64, 20], [64, 64, 20], True)) == "64 x 64 x 20"


def test_a_differing_field_shows_both_values():
    """"Differs" alone does not tell a reviewer which way, or by how much."""
    text = _header_field_text(_field([3.0, 3.0, 5.0], [2.0, 2.0, 5.0], False))
    assert text == "3.0 x 3.0 x 5.0 vs 2.0 x 2.0 x 5.0"


def test_axis_codes_join_without_a_separator():
    """Orientation is conventionally written LAS, not L x A x S."""
    field = _field(["L", "A", "S"], ["L", "A", "S"], True)
    assert _header_field_text(field, joiner="") == "LAS"


def test_an_unverified_field_is_not_reported_as_a_pass():
    """None means the file did not declare it. That is not agreement."""
    assert _header_field_text(_field(None, ["L", "A", "S"], None)) == "Not verified"
    assert _header_field_text(None) == "Not verified"


def test_a_declared_value_against_a_missing_one_says_so():
    text = _header_field_text(_field("float32", None, False))
    assert text == "float32 vs not declared"


# ── Model rows ────────────────────────────────────────────────────────────

def test_a_compared_map_produces_one_row():
    model = _header_check_model([_summary(_matching_check())], blinded=False)
    assert len(model["header_check_rows"]) == 1
    row = model["header_check_rows"][0]
    assert row[1] == "CBF"
    assert row[-1] == "Matches"


def test_every_row_has_one_cell_per_header():
    model = _header_check_model([_summary(_flipped_check())], blinded=False)
    width = len(model["header_check_headers"])
    assert all(len(row) == width for row in model["header_check_rows"])


def test_a_map_with_no_reference_produces_no_row():
    """There is nothing to compare a header against without a reference."""
    model = _header_check_model([_summary(None)], blinded=False)
    assert model["header_check_rows"] == []


def test_a_flipped_map_is_reported_as_a_geometry_mismatch():
    model = _header_check_model([_summary(_flipped_check())], blinded=False)
    row = model["header_check_rows"][0]
    assert row[-1] == "Geometry differs"
    assert "RAS vs LAS" in row


def test_a_dtype_difference_is_not_called_a_geometry_mismatch():
    """A team submitting float64 against a float32 reference is harmless."""
    check = _matching_check()
    check["status"] = "dtype_differs"
    check["fields"]["dtype"] = _field("float64", "float32", False)
    model = _header_check_model([_summary(check)], blinded=False)
    assert model["header_check_rows"][0][-1] == "Data type differs"


def test_an_unrecognised_status_is_shown_rather_than_treated_as_a_pass():
    check = _matching_check()
    check["status"] = "some_future_status"
    model = _header_check_model([_summary(check)], blinded=False)
    assert model["header_check_rows"][0][-1] == "some_future_status"


def test_a_blinded_report_does_not_name_the_team():
    model = _header_check_model(
        [_summary(_matching_check(), team="Team Alpha")], blinded=True,
    )
    assert "Team Alpha" not in str(model["header_check_rows"])
    assert model["header_check_rows"][0][0] == "Submission 1"


def test_an_unblinded_report_does_name_the_team():
    model = _header_check_model(
        [_summary(_matching_check(), team="Team Alpha")], blinded=False,
    )
    assert model["header_check_rows"][0][0] == "Team Alpha"


# ── The rows reach the shared report model ────────────────────────────────

def test_the_rows_are_carried_into_the_report_model():
    """Both formats read this key. If it is dropped, both go blank."""
    model = _build_report_model(
        [_summary(_flipped_check())], tag="test", blinded=True,
    )
    assert model.get("header_check_rows")
    assert model.get("header_check_headers")
    assert model["header_check_rows"][0][-1] == "Geometry differs"


@pytest.mark.parametrize("blinded", [True, False])
def test_the_model_builds_with_no_reference_data_at_all(blinded):
    model = _build_report_model([_summary(None)], tag="test", blinded=blinded)
    assert model["header_check_rows"] == []


# ── The warning fires on the exact string the model emits ─────────────────

def test_both_renderers_watch_for_the_verdict_the_model_actually_produces():
    """A parity check between the model and the two things that read it.

    Both renderers show an extra warning when a row reads "Geometry differs".
    They compare against that literal, so if the model's wording is ever
    changed on its own the warning stops appearing and nothing else breaks:
    the table still renders, every existing test still passes, and the one
    case the check exists for goes quiet. This ties the three together.
    """
    repo = Path(__file__).resolve().parents[1]
    model = _header_check_model([_summary(_flipped_check())], blinded=True)
    verdict = model["header_check_rows"][0][-1]
    assert verdict == "Geometry differs"

    for relative in ("backend/services/pdf_report_service.py", "backend/main.py"):
        source = (repo / relative).read_text(encoding="utf-8")
        assert f'== "{verdict}"' in source, (
            f"{relative} no longer watches for {verdict!r}, so the mismatch "
            "warning cannot fire"
        )

    app_js = (repo / "frontend/app.js").read_text(encoding="utf-8")
    assert "geometry_mismatch" in app_js, (
        "the interface no longer reacts to a geometry mismatch"
    )


def test_the_matching_case_does_not_trigger_the_warning():
    model = _header_check_model([_summary(_matching_check())], blinded=True)
    assert model["header_check_rows"][0][-1] != "Geometry differs"
