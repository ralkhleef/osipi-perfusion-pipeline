"""Which ROI columns a reader is actually shown.

A submission with one scan has nothing to put in Dataset, Participant, Repeat
or Site, so the table printed four columns of dashes on every row. They took
about a third of the width and pushed Status off the right edge, which is how
a table ends up being read with a horizontal scrollbar.

Columns whose value never varies carry no information per row, so they are
lifted out and stated once above the table. The rule is about variation, not
about emptiness: a DCE submission where every scan is from the clinical
dataset lifts "clinical" out too, and keeps Participant because that differs.

The full rows stay as they were. The CSV export is built from the records and
must keep every column whatever a particular submission happens to look like,
and these tests check that it still does.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend")]

from services.pdf_report_service import _roi_descriptive_model  # noqa: E402

METRICS = ["mean", "median", "standard_deviation", "range", "coefficient_of_variation"]


def record(**overrides):
    base = dict(
        dataset="", participant="", repeat="", site="",
        map_type="cbf", roi_id="gm", roi_label="gray matter",
        roi_mean=51.83, roi_median=52.03, roi_within_scan_sd=2.84,
        roi_minimum=45.37, roi_maximum=57.91, roi_within_scan_cov=0.0547,
        voxel_count=1938260, units="mL/100g/min", status="available",
    )
    base.update(overrides)
    return base


def model(records):
    return _roi_descriptive_model([{
        "nifti_analysis": {"reference_scoring": {
            "roi_descriptive_statistics": records,
            "roi_descriptive_report_metrics": METRICS,
        }},
    }])


ONE_SCAN = [
    record(map_type="att", roi_id="gm", roi_label="gray matter"),
    record(map_type="att", roi_id="lesion", roi_label="lesion"),
    record(map_type="cbf", roi_id="wm", roi_label="white matter"),
]

MANY_SCANS = [
    record(dataset="clinical", participant="1", repeat="1", site="1", map_type="ktrans"),
    record(dataset="clinical", participant="2", repeat="1", site="1", map_type="ktrans"),
]


# ── The columns a reader sees ─────────────────────────────────────────────

def test_a_single_scan_drops_the_identity_columns() -> None:
    """Four columns of dashes is four columns of nothing."""
    display = model(ONE_SCAN)["roi_descriptive_display_headers"]
    for column in ("Dataset", "Participant", "Repeat", "Site"):
        assert column not in display, f"{column} carries no information here"
    assert display[:2] == ["Map", "ROI"]


def test_the_dropped_columns_are_stated_once_instead() -> None:
    """Lifting a column must not lose what it said."""
    result = model(MANY_SCANS)
    assert result["roi_descriptive_scope"].get("Dataset") == "clinical"
    assert result["roi_descriptive_scope"].get("Repeat") == "1"


def test_a_column_that_varies_is_kept() -> None:
    """The rule is about variation, not about being an identity field."""
    display = model(MANY_SCANS)["roi_descriptive_display_headers"]
    assert "Participant" in display
    assert "Dataset" not in display


def test_an_empty_column_is_not_claimed_to_have_a_value() -> None:
    """A column that is empty everywhere is dropped and says nothing."""
    scope = model(ONE_SCAN)["roi_descriptive_scope"]
    for column in ("Dataset", "Participant", "Repeat", "Site"):
        assert column not in scope


def test_units_and_status_are_lifted_when_they_never_differ() -> None:
    result = model(ONE_SCAN)
    assert result["roi_descriptive_scope"]["Units"] == "mL/100g/min"
    assert "Units" not in result["roi_descriptive_display_headers"]


def test_a_mixed_status_keeps_the_status_column() -> None:
    """Lifting a status that is not universal would hide a failure."""
    records = [record(), record(roi_id="wm", status="empty_roi",
                                unavailable_reason="empty_roi")]
    result = model(records)
    assert "Status" in result["roi_descriptive_display_headers"]
    assert "Status" not in result["roi_descriptive_scope"]


def test_mixed_units_are_not_collapsed_into_one_claim() -> None:
    """CBF and ATT do not share units and must not appear to."""
    records = [record(map_type="cbf", units="mL/100g/min"),
               record(map_type="att", roi_id="wm", units="seconds")]
    result = model(records)
    assert "Units" in result["roi_descriptive_display_headers"]
    assert "Units" not in result["roi_descriptive_scope"]


# ── The export is unaffected ──────────────────────────────────────────────

def test_the_full_rows_still_carry_every_column() -> None:
    """The CSV is built from these and must not lose a column."""
    result = model(ONE_SCAN)
    headers = result["roi_descriptive_headers"]
    assert headers[:4] == ["Dataset", "Participant", "Repeat", "Site"]
    assert all(len(row) == len(headers) for row in result["roi_descriptive_rows"])


def test_the_records_are_untouched() -> None:
    result = model(ONE_SCAN)
    assert len(result["roi_descriptive_records"]) == len(ONE_SCAN)
    assert result["roi_descriptive_records"][0]["units"] == "mL/100g/min"


# ── Shape ─────────────────────────────────────────────────────────────────

def test_display_rows_match_display_headers() -> None:
    """A row and its header drifting apart shifts every value one column."""
    for records in (ONE_SCAN, MANY_SCANS):
        result = model(records)
        width = len(result["roi_descriptive_display_headers"])
        assert all(len(row) == width for row in result["roi_descriptive_display_rows"])


def test_display_rows_keep_one_row_per_record() -> None:
    result = model(ONE_SCAN)
    assert len(result["roi_descriptive_display_rows"]) == len(ONE_SCAN)


def test_no_records_produces_no_rows_and_no_scope() -> None:
    result = model([])
    assert result["roi_descriptive_display_rows"] == []
    assert result["roi_descriptive_scope"] == {}


def test_a_single_record_does_not_lift_everything_into_the_caption() -> None:
    """With one row every column is constant, so the rule needs a floor:
    Map and ROI are what the row is about and must stay in the table."""
    display = model([record()])["roi_descriptive_display_headers"]
    assert "Map" in display and "ROI" in display
    assert "Mean" in display


@pytest.mark.parametrize("column", ["Mean", "Median", "SD", "Range", "CoV", "Voxels"])
def test_measured_values_are_never_lifted(column: str) -> None:
    """A statistic is the point of the table even if it happens to repeat."""
    identical = [record(), record(roi_id="wm", roi_label="white matter")]
    result = model(identical)
    assert column in result["roi_descriptive_display_headers"]
    assert column not in result["roi_descriptive_scope"]
