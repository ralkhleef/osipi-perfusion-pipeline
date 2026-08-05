"""Aggregating per-scan ROI statistics across one axis.

Disabled by default: the arithmetic is settled but the scientific choices —
medians versus pooled voxels, pairing, minimum group size — are not, so nothing
is computed until a challenge opts in. These tests pin both the arithmetic and
the fact that it stays off.

Expected values are hand-calculated. For medians 0.10, 0.20, 0.30:

    mean          = 0.20
    population SD = sqrt(((0.1)^2 + 0 + (0.1)^2) / 3) = sqrt(0.0066666...)
                  = 0.0816496580...
    CoV           = SD / 0.20 = 0.4082482904...
"""

from __future__ import annotations

import math

import pytest

from osipi_pipeline.scoring.grouped_statistics import (
    AXES,
    AXIS_PARTICIPANT,
    AXIS_REPEAT,
    AXIS_SITE,
    CSV_COLUMNS,
    METHODOLOGY,
    MIN_GROUP_SIZE,
    STATUS_AVAILABLE,
    STATUS_TOO_FEW_SCANS,
    compute_grouped_statistics,
    csv_row,
)

EXPECTED_MEAN = 0.20
EXPECTED_SD = math.sqrt(((0.1) ** 2 + 0 + (0.1) ** 2) / 3)
EXPECTED_COV = EXPECTED_SD / 0.20


def row(dataset="clinical", participant="1", site="1", repeat="1",
        median=0.1, roi="tumour", map_type="ktrans", units="min^-1"):
    return {
        "challenge": "dce", "dataset": dataset, "participant": participant,
        "site": site, "repeat": repeat, "roi_id": roi, "roi_label": roi.title(),
        "map_type": map_type, "units": units, "roi_median": median,
    }


def only(results, axis):
    return [r for r in results if r.axis == axis]


# ── Arithmetic ────────────────────────────────────────────────────────────

def test_sd_and_cov_across_repeats() -> None:
    rows = [row(repeat=str(i + 1), median=v)
            for i, v in enumerate((0.1, 0.2, 0.3))]
    (result,) = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)

    assert result.scan_count == 3
    assert result.mean == pytest.approx(EXPECTED_MEAN)
    assert result.standard_deviation == pytest.approx(EXPECTED_SD)
    assert result.coefficient_of_variation == pytest.approx(EXPECTED_COV)
    assert result.status == STATUS_AVAILABLE


def test_population_sd_not_sample_sd() -> None:
    """Guards ddof=0, matching the within-scan statistics."""
    rows = [row(repeat=str(i + 1), median=v)
            for i, v in enumerate((0.1, 0.2, 0.3))]
    (result,) = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    sample_sd = math.sqrt(((0.1) ** 2 + 0 + (0.1) ** 2) / 2)
    assert result.standard_deviation != pytest.approx(sample_sd)


def test_cov_uses_the_absolute_mean() -> None:
    rows = [row(repeat=str(i + 1), median=v)
            for i, v in enumerate((-0.1, -0.2, -0.3))]
    (result,) = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    assert result.mean == pytest.approx(-0.20)
    assert result.coefficient_of_variation == pytest.approx(EXPECTED_COV)
    assert result.coefficient_of_variation > 0


def test_cov_unavailable_when_the_mean_is_near_zero() -> None:
    rows = [row(repeat="1", median=-0.1), row(repeat="2", median=0.1)]
    (result,) = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    assert result.coefficient_of_variation is None
    assert result.unavailable_reason == "mean_near_zero"
    assert result.standard_deviation is not None


# ── Grouping ──────────────────────────────────────────────────────────────

def test_each_axis_holds_the_others_fixed() -> None:
    """Two participants, two repeats each: repeats group within a participant."""
    rows = [row(participant=p, repeat=r, median=m)
            for p, r, m in (("1", "1", 0.1), ("1", "2", 0.3),
                            ("2", "1", 0.5), ("2", "2", 0.7))]
    results = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)

    assert len(results) == 2, "repeats must not be pooled across participants"
    for result in results:
        assert result.scan_count == 2
        assert result.held_fixed["participant"] in {"1", "2"}


def test_participants_group_within_a_site_and_repeat() -> None:
    rows = [row(participant=p, repeat="1", median=m)
            for p, m in (("1", 0.1), ("2", 0.2), ("3", 0.3))]
    (result,) = only(compute_grouped_statistics(rows, axes=[AXIS_PARTICIPANT]),
                     AXIS_PARTICIPANT)
    assert result.scan_count == 3
    assert result.varied_over == ("1", "2", "3")
    assert result.held_fixed == {"dataset": "clinical", "site": "1", "repeat": "1"}


def test_sites_group_within_a_participant_and_repeat() -> None:
    rows = [row(site=s, median=m) for s, m in (("1", 0.1), ("2", 0.2), ("3", 0.3))]
    (result,) = only(compute_grouped_statistics(rows, axes=[AXIS_SITE]), AXIS_SITE)
    assert result.scan_count == 3
    assert result.held_fixed["participant"] == "1"


def test_datasets_are_never_pooled() -> None:
    rows = [row(dataset="clinical", repeat="1", median=0.1),
            row(dataset="clinical", repeat="2", median=0.2),
            row(dataset="synthetic", repeat="1", median=9.0),
            row(dataset="synthetic", repeat="2", median=9.1)]
    results = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)
    assert {r.dataset for r in results} == {"clinical", "synthetic"}
    assert len(results) == 2


def test_rois_are_never_pooled() -> None:
    rows = [row(roi="tumour", repeat="1", median=0.1),
            row(roi="tumour", repeat="2", median=0.2),
            row(roi="cortex", repeat="1", median=5.0),
            row(roi="cortex", repeat="2", median=5.2)]
    results = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)
    assert {r.roi_id for r in results} == {"tumour", "cortex"}


def test_map_types_are_never_pooled() -> None:
    """Ktrans and vp have different units; averaging them is meaningless."""
    rows = [row(map_type="ktrans", repeat="1", median=0.1),
            row(map_type="ktrans", repeat="2", median=0.2),
            row(map_type="vp", repeat="1", median=0.02),
            row(map_type="vp", repeat="2", median=0.03)]
    results = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)
    assert {r.map_type for r in results} == {"ktrans", "vp"}


# ── Groups too small to show variation ────────────────────────────────────

def test_a_single_repeat_is_reported_not_dropped() -> None:
    """A reviewer must see that a participant had one repeat, not lose them."""
    (result,) = only(compute_grouped_statistics([row()], axes=[AXIS_REPEAT]), AXIS_REPEAT)
    assert result.status == STATUS_TOO_FEW_SCANS
    assert result.standard_deviation is None
    assert result.coefficient_of_variation is None
    assert result.scan_count == 1


def test_repeated_rows_for_one_repeat_are_one_scan() -> None:
    """Two ROIs of the same repeat are not two repeats."""
    rows = [row(repeat="1", median=0.1), row(repeat="1", median=0.3)]
    (result,) = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)
    assert result.status == STATUS_TOO_FEW_SCANS
    assert result.varied_over == ("1",)


def test_rows_missing_the_axis_value_are_not_grouped() -> None:
    rows = [row(repeat="1", median=0.1), row(repeat=None, median=0.9)]
    results = only(compute_grouped_statistics(rows, axes=[AXIS_REPEAT]), AXIS_REPEAT)
    assert all(r.scan_count <= 1 for r in results)


def test_minimum_group_size_is_configurable() -> None:
    rows = [row(repeat=str(i + 1), median=v) for i, v in enumerate((0.1, 0.2))]
    strict = compute_grouped_statistics(rows, axes=[AXIS_REPEAT], minimum_group_size=3)
    assert strict[0].status == STATUS_TOO_FEW_SCANS
    relaxed = compute_grouped_statistics(rows, axes=[AXIS_REPEAT], minimum_group_size=2)
    assert relaxed[0].status == STATUS_AVAILABLE


# ── Configuration ─────────────────────────────────────────────────────────

def test_the_feature_is_off_for_every_configured_challenge() -> None:
    """Nothing is computed until OSIPI confirms the conventions."""
    from osipi_pipeline.config.rules import grouped_statistics_by_challenge

    settings = grouped_statistics_by_challenge()
    assert settings, "no challenges configured"
    for challenge, spec in settings.items():
        assert spec["enabled"] is False, f"{challenge} enables unconfirmed statistics"


def test_defaults_are_the_documented_ones() -> None:
    from osipi_pipeline.config.rules import grouped_statistics_by_challenge

    spec = grouped_statistics_by_challenge()["dce"]
    assert spec["source"] == "roi_median"
    assert tuple(spec["axes"]) == AXES
    assert spec["minimum_group_size"] == MIN_GROUP_SIZE


def test_the_aggregated_field_is_configurable() -> None:
    """`source` names the per-scan field, so the choice is recorded in YAML."""
    rows = [dict(row(repeat=str(i + 1)), roi_within_scan_sd=v)
            for i, v in enumerate((0.1, 0.2, 0.3))]
    (result,) = compute_grouped_statistics(
        rows, axes=[AXIS_REPEAT], source="roi_within_scan_sd")
    assert result.mean == pytest.approx(EXPECTED_MEAN)


def test_an_unknown_axis_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown grouping axis"):
        compute_grouped_statistics([row()], axes=["inter_galactic"])


# ── Export shape ──────────────────────────────────────────────────────────

def test_csv_row_matches_the_column_list() -> None:
    rows = [row(repeat=str(i + 1), median=v) for i, v in enumerate((0.1, 0.2, 0.3))]
    (result,) = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    assert len(csv_row(result)) == len(CSV_COLUMNS)


def test_csv_values_are_numbers_not_formatted_strings() -> None:
    rows = [row(repeat=str(i + 1), median=v) for i, v in enumerate((0.1, 0.2, 0.3))]
    (result,) = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    values = csv_row(result)
    cov = values[CSV_COLUMNS.index("group_cov")]
    assert isinstance(cov, float) and cov < 1.0


def test_results_are_json_serialisable() -> None:
    import json

    rows = [row(repeat=str(i + 1), median=v) for i, v in enumerate((0.1, 0.2, 0.3))]
    results = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    payload = [r.to_dict() for r in results]
    assert json.loads(json.dumps(payload)) == payload


def test_methodology_states_what_this_is_not() -> None:
    scope = METHODOLOGY["scope"]
    for excluded in ("accuracy", "deviance", "repeatability", "reproducibility", "ICC"):
        assert excluded in scope
    assert "subject to confirmation" in METHODOLOGY["status"]


def test_dataclass_rows_work_as_well_as_dicts() -> None:
    """The within-scan layer returns dataclasses; both must be accepted."""
    from osipi_pipeline.scoring.descriptive_statistics import RoiDescriptiveResult

    rows = [
        RoiDescriptiveResult(
            challenge="dce", dataset="clinical", participant="1", repeat=str(i + 1),
            site="1", map_type="ktrans", roi_id="tumour", roi_label="Tumour",
            units="min^-1", roi_median=v)
        for i, v in enumerate((0.1, 0.2, 0.3))
    ]
    (result,) = compute_grouped_statistics(rows, axes=[AXIS_REPEAT])
    assert result.standard_deviation == pytest.approx(EXPECTED_SD)
