"""Tests for the shared report model and figure builders.

The report had no direct coverage: the API tests exercised the endpoints and
grepped the rendered output for section headings, so a wrong *number* would
sail through while a renamed heading failed the build. These tests assert the
content rules instead, de-duplication, conditional caveats, per-challenge
scoping, and the invariant that missing data is never rendered as zero.
"""

from __future__ import annotations

import pytest

from services.pdf_report_service import (
    _build_report_model,
    _map_units,
    _status_fields,
    agreement_points,
    build_limitations,
)
from services.report_branding import BRAND
from services.report_figures import (
    bland_altman_figure,
    identity_figure,
    to_svg,
)


def _scored(map_type="CBF", *, sub=58.0, ref=60.0, sd=6.0, units="ml/100g/min"):
    """A summary carrying a scored map, shaped like backend/scoring.py output."""
    metrics = {
        "status": "compared", "mean_submitted": sub, "mean_reference": ref,
        "bias": sub - ref, "standard_deviation_error": sd,
        "rmse": 7.2, "mae": 5.0, "correlation": 0.9,
        "voxel_count": 100, "total_voxel_count": 110,
    }
    summary = _summary()
    summary["nifti_analysis"] = {
        "maps": [{"detected_map_type": map_type, "units": units,
                  "metadata": {}, "stats": {}}],
        "reference_scoring": {"maps": [{
            "detected_map_type": map_type, "status": "compared",
            "whole_map": metrics,
            "masks": [{"mask_label": "Grey matter", "metrics": dict(metrics)}],
        }]},
    }
    return summary


def _summary(idx=1, challenge="asl", *, ref=True, warns=0, errs=0, maps="CBF, ATT"):
    return {
        "submission_id": f"sub-{idx:02d}",
        "source_folder": f"team_{idx}_folder",
        "team_name": f"Team {idx}",
        "contact_email": f"team{idx}@example.org",
        "challenge_type": challenge,
        "warning_count": warns,
        "error_count": errs,
        "warnings": [{"message": "check voxel size", "path": "a.nii.gz"}] * warns,
        "errors": [{"message": "unreadable volume", "path": "b.nii.gz"}] * errs,
        "exec_status": "skipped_result_maps",
        "nifti_analysis": {"maps": [], "summary": {"means_by_map_type": {}}},
        "analysis_fields": {
            "parameter_maps_detected": maps,
            "map_count": 2,
            "finite_voxels_percent": 99.0,
            "nan_count": 5,
            "inf_count": 0,
            "negative_voxels_percent": 0.4,
            "finite_voxel_count": 990,
            "total_voxel_count": 1000,
            "negative_voxel_count": 4,
            "means_by_map_type": {},
            "mean_coefficient_of_variation": 0.3 if ref else None,
            "reference_based_scoring_available": ref,
            "reference_compared_map_count": 2 if ref else 0,
            "reference_scoring_status": "available" if ref else "reference_not_available",
            "reference_mean_rmse": 7.2 if ref else None,
            "reference_mean_mae": 5.0 if ref else None,
            "reference_mean_bias": -1.1 if ref else None,
            "reference_metric_rows": [],
        },
    }


# ── De-duplication ────────────────────────────────────────────────────────

def test_results_table_supersedes_the_removed_submissions_table():
    """Every column of the old submissions table lives in the results table."""
    model = _build_report_model([_summary()], tag="t", blinded=True)
    for header in model["submission_metadata_headers"]:
        assert header in model["table_headers"], header


def test_qc_and_scoring_do_not_repeat_each_other():
    model = _build_report_model([_summary()], tag="t", blinded=True)
    assert not set(model["qc"]) & set(model["scoring"])


def test_aggregate_statistics_live_in_the_results_table():
    """The key-figures band was removed; a paper puts its numbers in a table.

    The band repeated the results table, so the aggregates belong in the
    summary table and must appear there exactly once.
    """
    model = _build_report_model([_summary()], tag="t", blinded=True)
    for stat in ("Submissions", "Maps", "Finite voxels", "NaN / Inf",
                 "Negative voxels"):
        assert stat in model["qc"], stat
    # Still no overlap with the agreement metrics beside them.
    assert not set(model["qc"]) & set(model["scoring"])


def test_report_reads_in_paper_order():
    """Summary and Methods must both be present and derived from the run."""
    model = _build_report_model([_summary()], tag="t", blinded=True)
    assert model["lead_lines"], "missing summary"
    methods = " ".join(model["methods_lines"])
    assert "voxel-level statistics" in methods
    assert "withheld" in methods, "blinding must be stated in Methods"


def test_report_model_records_reproducible_analysis_provenance():
    model = _build_report_model([_summary(1, "dce")], tag="t", blinded=True)
    provenance = model["analysis_provenance"]
    assert provenance["challenge"] == "DCE"
    assert provenance["challenge_configuration"]
    assert provenance["scoring_package"]
    assert provenance["pipeline_version"]
    assert provenance["reference_dataset"]
    assert provenance["analysis_date"]


def test_methods_state_when_no_reference_was_available():
    model = _build_report_model([_summary(ref=False)], tag="t", blinded=True)
    methods = " ".join(model["methods_lines"])
    assert "No matching reference maps" in methods
    assert "Bias is the mean" not in methods


def test_grouping_caveat_is_prose_not_a_table_row():
    """The mixed-challenge caveat belongs in the leader, not in the metrics."""
    model = _build_report_model(
        [_summary(1, "asl"), _summary(2, "dce")], tag="t", blinded=True)
    assert "Grouped by challenge" not in model["scoring"]
    assert any("more than one challenge" in line for line in model["lead_lines"])


# ── Status fields ─────────────────────────────────────────────────────────

def test_execution_status_hidden_when_nothing_required_execution():
    fields = _status_fields("Complete", "Execution not required", "QC complete", "Ready")
    assert "Execution" not in fields
    assert fields["Validation"] == "Complete"


def test_qc_status_hidden_when_it_merely_restates_validation():
    fields = _status_fields("Unable to continue", "Execution not required",
                            "Unable to continue", "Ready with limitations")
    assert "QC" not in fields


def test_execution_status_shown_when_it_carries_information():
    fields = _status_fields("Complete", "Failed", "QC complete", "Ready")
    assert fields["Execution"] == "Failed"


# ── Conditional limitations ───────────────────────────────────────────────

def test_reference_caveats_omitted_without_reference_maps():
    items = build_limitations(reference_available=False, map_types=["CBF"],
                              challenges=["ASL"], cov_reported=False)
    joined = " ".join(items)
    assert "Repeatability CoV" not in joined
    assert "official OSIPI scores" not in joined


def test_reference_caveats_present_with_reference_maps():
    items = build_limitations(reference_available=True, map_types=["CBF"],
                              challenges=["ASL"], cov_reported=True)
    joined = " ".join(items)
    assert "Repeatability CoV" in joined
    assert "error-CoV" in joined


def test_unit_caveat_only_when_multiple_map_types():
    one = build_limitations(reference_available=False, map_types=["CBF"],
                            challenges=["ASL"], cov_reported=False)
    two = build_limitations(reference_available=False, map_types=["CBF", "ATT"],
                            challenges=["ASL"], cov_reported=False)
    assert not any("units differ" in i for i in one)
    assert any("units differ" in i for i in two)


def test_challenge_name_is_not_hardcoded_to_asl():
    """A DCE-only report must not claim something about ASL."""
    items = build_limitations(reference_available=False, map_types=["Ktrans"],
                              challenges=["DCE"], cov_reported=False)
    joined = " ".join(items)
    assert "DCE" in joined
    assert "ASL" not in joined


# ── Figures ───────────────────────────────────────────────────────────────









def test_svg_output_is_self_contained_and_escaped():
    """Axis labels carry attacker-influenced text and must be escaped.

    The units string is read from the submitted NIfTI's own header, so it is
    participant-supplied and lands directly in an axis label. The HTML report
    embeds this SVG inline, so an unescaped tag here would execute.
    """
    fig = bland_altman_figure(
        [{"mean_level": 1.0, "bias": 0.0, "sd": 0.5}],
        units="<script>alert(1)</script>", width=400)
    svg = to_svg(fig)
    assert svg.startswith("<svg")
    assert "<script>" not in svg
    assert "&lt;script&gt;" in svg
    assert "http://www.w3.org/2000/svg" in svg
    # No external references: the report must render offline.
    assert "src=" not in svg


# ── Agreement plots ───────────────────────────────────────────────────────

def test_agreement_points_read_stats_the_metric_rows_drop():
    """mean_submitted / mean_reference / SD come from reference_scoring."""
    points = agreement_points([_scored()], blinded=True)
    assert set(points) == {"CBF"}
    whole = points["CBF"][0]
    assert whole["mean_submitted"] == 58.0
    assert whole["mean_reference"] == 60.0
    assert whole["sd"] == 6.0
    # Bland-Altman's x axis is the mean of the two measurements.
    assert whole["mean_level"] == pytest.approx(59.0)


def test_agreement_points_include_whole_image_and_each_roi():
    points = agreement_points([_scored()], blinded=True)
    rois = {p["roi"] for p in points["CBF"]}
    assert rois == {"Whole image", "Grey matter"}
    styles = {p["roi"]: p["style"] for p in points["CBF"]}
    assert styles["Whole image"] == "solid" and styles["Grey matter"] == "hollow"


def test_agreement_points_keyed_by_map_type_not_challenge():
    """CBF and ATT differ in units, so they must not share an axis."""
    points = agreement_points([_scored("CBF"), _scored("ATT", units="s")],
                              blinded=True)
    assert set(points) == {"CBF", "ATT"}


def test_map_units_come_from_the_submitted_map():
    summaries = [_scored("ATT", units="s")]
    assert _map_units(summaries, "ATT") == "s"
    assert _map_units(summaries, "CBF") == "map units"


def test_limits_of_agreement_are_bias_plus_minus_1_96_sd():
    pts = [{"mean_level": 59.0, "bias": -2.0, "sd": 6.0, "style": "solid"}]
    fig = bland_altman_figure(pts, units="ml/100g/min", width=300)
    assert fig is not None
    assert fig["mean_bias"] == pytest.approx(-2.0)
    lower, upper = fig["limits"]
    assert lower == pytest.approx(-2.0 - 1.96 * 6.0)
    assert upper == pytest.approx(-2.0 + 1.96 * 6.0)
    # Both limits are drawn, dashed, and labelled.
    dashed = [p for p in fig["prims"] if p["t"] == "line" and p.get("dash")]
    assert len(dashed) == 2
    labels = [p["s"] for p in fig["prims"] if p["t"] == "text"]
    assert "95% limits of agreement" in labels


def test_bland_altman_skipped_without_paired_means():
    pts = [{"mean_level": None, "bias": None, "sd": None}]
    assert bland_altman_figure(pts, width=300) is None


def test_incomplete_region_is_dropped_not_the_whole_figure():
    """One unscored ROI must not suppress the other regions' points."""
    pts = [
        {"mean_level": 59.0, "bias": -2.0, "sd": 6.0, "style": "solid"},
        {"mean_level": None, "bias": None, "sd": None, "style": "hollow"},
    ]
    fig = bland_altman_figure(pts, width=300)
    assert fig is not None
    # Two legend swatches plus exactly one plotted region.
    assert len(_markers(fig)) == 3


def test_incomplete_region_excluded_from_the_limits_of_agreement():
    """A region with no SD must not drag the pooled limits toward zero."""
    complete = [{"mean_level": 59.0, "bias": -2.0, "sd": 6.0}]
    with_gap = complete + [{"mean_level": 61.0, "bias": None, "sd": None}]
    assert (bland_altman_figure(complete, width=300)["limits"]
            == bland_altman_figure(with_gap, width=300)["limits"])


def _markers(fig):
    return [p for p in fig["prims"] if p["t"] == "marker"]


def _dashed_lines(fig):
    return [p for p in fig["prims"] if p["t"] == "line" and p.get("dash")]


def _axis_ticks(fig, gutter=42.0):
    """Return (x tick labels, y tick labels) read back off the figure.

    X ticks are centre-anchored under the axis; Y ticks are end-anchored just
    left of it. The axis captions are excluded by position.
    """
    x_ticks, y_ticks = [], []
    for p in fig["prims"]:
        if p["t"] != "text":
            continue
        if p["anchor"] == "middle":
            x_ticks.append(p["s"])
        elif p["anchor"] == "end" and 0 < p["x"] < gutter:
            y_ticks.append(p["s"])
    return x_ticks, y_ticks


def test_identity_plot_uses_a_shared_square_scale():
    """Both axes must span the same range, so a deviation reads honestly.

    Checked via the rendered tick labels rather than the drawn line: because
    both axis mappings are affine, a point where submitted equals reference
    lands on the drawn line whether or not the scales agree, so that test
    would pass on a stretched plot. Equal tick sets is the real property.
    """
    pts = [{"mean_submitted": 60.0, "mean_reference": 60.0, "style": "solid"},
           {"mean_submitted": 40.0, "mean_reference": 95.0, "style": "hollow"}]
    fig = identity_figure(pts, units="ml/100g/min", width=300)
    assert fig is not None
    x_ticks, y_ticks = _axis_ticks(fig)
    assert x_ticks and y_ticks
    assert x_ticks == y_ticks, (
        f"axes are not on a shared scale: x={x_ticks} y={y_ticks}")


def test_identity_plot_draws_the_reference_line_dashed():
    pts = [{"mean_submitted": 58.0, "mean_reference": 60.0}]
    fig = identity_figure(pts, width=300)
    assert _dashed_lines(fig)


def test_negative_bias_plots_below_the_zero_line():
    """Sign matters: under-estimating the reference must read as 'below'."""
    fig = bland_altman_figure(
        [{"mean_level": 59.0, "bias": -2.0, "sd": 6.0, "style": "solid"}],
        width=300)
    zero_lines = [
        p for p in fig["prims"]
        if p["t"] == "line" and not p.get("dash")
        and p["y1"] == p["y2"] and p["color"] == BRAND["subtle"]
    ]
    assert len(zero_lines) == 1, "expected exactly one zero-bias reference line"
    point = _markers(fig)[2]          # after the two legend swatches
    assert point["y"] < zero_lines[0]["y1"], "negative bias drawn above zero"


def test_positive_bias_plots_above_the_zero_line():
    fig = bland_altman_figure(
        [{"mean_level": 59.0, "bias": 3.0, "sd": 6.0, "style": "solid"}],
        width=300)
    zero_line, = [
        p for p in fig["prims"]
        if p["t"] == "line" and not p.get("dash")
        and p["y1"] == p["y2"] and p["color"] == BRAND["subtle"]
    ]
    assert _markers(fig)[2]["y"] > zero_line["y1"]


def test_agreement_figures_survive_svg_and_pdf_rendering():
    from services.report_figures import to_drawing

    pts = [{"mean_level": 59.0, "bias": -2.0, "sd": 6.0,
            "mean_submitted": 58.0, "mean_reference": 60.0, "style": "solid"}]
    for fig in (bland_altman_figure(pts, width=300),
                identity_figure(pts, width=300)):
        assert to_svg(fig).startswith("<svg")
        drawing = to_drawing(fig)
        assert drawing.width == 300


# ── Blinding ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["team_1_folder", "Team 1", "team1@example.org"])
def test_blinded_model_contains_no_identifying_fields(field):
    model = _build_report_model([_summary()], tag="t", blinded=True)
    haystack = repr(model)
    assert field not in haystack


def test_unblinded_model_keeps_team_columns():
    model = _build_report_model([_summary()], tag="t", blinded=False)
    assert "Team" in model["table_headers"]
    assert "Contact" in model["table_headers"]
