"""Comparison against ground truth, broken down by region.

Both challenge leads asked for this and the report did not show it. The
scorer had computed it per mask for a while, but the only place it appeared
was an appendix that is off by default, so a reader saw the whole-image bias
and nothing else.

The difference is not cosmetic. On the challenge lead's own ASL data the
whole-image CBF bias is +0.83, which reads as close agreement, while grey
matter is +7.99 and white matter is -4.20: averaged over the brain the two
nearly cancel. These tests pin the rows, and pin the whole image staying
alongside them so that cancellation is visible rather than inferred.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend")]

from services.pdf_report_service import (  # noqa: E402
    _build_report_model,
    _reference_by_region_model,
)


def metrics(**overrides):
    base = dict(status="compared", bias=1.0, mae=2.0, rmse=3.0,
                error_coefficient_of_variation=0.25, correlation=0.9,
                voxel_count=100)
    base.update(overrides)
    return base


def summary(maps, *, team="Team Alpha"):
    return {
        "submission_id": "s1", "source_folder": team, "challenge_type": "asl",
        "nifti_analysis": {"reference_scoring": {"status": "available", "maps": maps}},
    }


LENA = [{
    "detected_map_type": "cbf",
    "whole_map": metrics(bias=0.829, mae=1.372, voxel_count=8675289),
    "masks": [
        {"mask_label": "gray matter", "metrics": metrics(bias=7.993, voxel_count=1193882)},
        {"mask_label": "white matter", "metrics": metrics(bias=-4.203, voxel_count=560547)},
    ],
}]


def rows(model):
    return model["reference_region_rows"]


# ── The rows exist and are complete ───────────────────────────────────────

def test_each_region_gets_a_row() -> None:
    model = _reference_by_region_model([summary(LENA)], blinded=True)
    regions = [row[2] for row in rows(model)]
    assert regions == ["Whole image", "gray matter", "white matter"]


def test_the_whole_image_stays_alongside_the_regions() -> None:
    """Dropping it would remove the comparison that makes the point."""
    model = _reference_by_region_model([summary(LENA)], blinded=True)
    assert "Whole image" in [row[2] for row in rows(model)]


def test_the_regional_split_is_visible(model=None) -> None:
    """The finding itself: opposite signs that cancel over the brain."""
    model = _reference_by_region_model([summary(LENA)], blinded=True)
    by_region = {row[2]: row[3] for row in rows(model)}
    assert by_region["gray matter"].startswith("7.99")
    assert by_region["white matter"].startswith("-4.20")
    assert by_region["Whole image"].startswith("0.8")


def test_every_row_has_one_cell_per_header() -> None:
    model = _reference_by_region_model([summary(LENA)], blinded=True)
    width = len(model["reference_region_headers"])
    assert all(len(row) == width for row in rows(model))


def test_all_the_requested_metrics_are_present() -> None:
    """Lena asked for bias and CoV per region; MAE and RMSE came with it."""
    headers = _reference_by_region_model([summary(LENA)], blinded=True)["reference_region_headers"]
    for column in ("Bias", "MAE", "RMSE", "Error CoV", "Voxels"):
        assert column in headers


# ── What must not appear ──────────────────────────────────────────────────

def test_a_region_that_was_not_compared_is_omitted() -> None:
    """A row of blanks would read as a result of zero."""
    maps = [{
        "detected_map_type": "cbf",
        "whole_map": metrics(),
        "masks": [{"mask_label": "empty", "metrics": metrics(status="empty_roi")}],
    }]
    assert [row[2] for row in rows(_reference_by_region_model([summary(maps)], blinded=True))] \
        == ["Whole image"]


def test_an_uncompared_whole_map_is_omitted() -> None:
    maps = [{"detected_map_type": "cbf",
             "whole_map": metrics(status="reference_not_available"), "masks": []}]
    assert rows(_reference_by_region_model([summary(maps)], blinded=True)) == []


def test_no_reference_at_all_produces_no_table() -> None:
    maps = [{"detected_map_type": "cbf"}]
    assert rows(_reference_by_region_model([summary(maps)], blinded=True)) == []


# ── Blinding ──────────────────────────────────────────────────────────────

def test_a_blinded_report_does_not_name_the_team() -> None:
    model = _reference_by_region_model([summary(LENA, team="Team Alpha")], blinded=True)
    assert "Team Alpha" not in str(rows(model))
    assert rows(model)[0][0] == "Submission 1"


def test_an_unblinded_report_does_name_the_team() -> None:
    model = _reference_by_region_model([summary(LENA, team="Team Alpha")], blinded=False)
    assert rows(model)[0][0] == "Team Alpha"


# ── It reaches the shared model both formats read ─────────────────────────

@pytest.mark.parametrize("blinded", [True, False])
def test_the_rows_are_carried_into_the_report_model(blinded: bool) -> None:
    model = _build_report_model([summary(LENA)], tag="t", blinded=blinded)
    assert model.get("reference_region_rows")
    assert model.get("reference_region_headers")


def test_both_renderers_read_the_same_model_key() -> None:
    """One source, so the PDF and the HTML cannot disagree about a number."""
    repo = Path(__file__).resolve().parents[1]
    for relative in ("backend/services/pdf_report_service.py", "backend/main.py"):
        source = (repo / relative).read_text(encoding="utf-8")
        assert "reference_region_rows" in source, (
            f"{relative} no longer renders the per-region comparison")
