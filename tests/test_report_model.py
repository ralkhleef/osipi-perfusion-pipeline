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


@pytest.mark.parametrize("use_path", [True, False])
def test_multiscan_results_keep_their_own_metrics(use_path):
    summary = _summary(challenge="dce")
    summary["nifti_analysis"]["maps"] = [
        {"file_name": "Ktrans.nii.gz", "path": f"/submission/P0{i}/Ktrans.nii.gz",
         "scan_label": f"P0{i}", "detected_map_type": "Ktrans", "stats": {}}
        for i in (1, 2)]
    summary["analysis_fields"]["reference_metric_rows"] = [
        {"submitted_file": "Ktrans.nii.gz", "scan_label": f"P0{i}",
         "submitted_path": f"/submission/P0{i}/Ktrans.nii.gz" if use_path else "",
         "detected_map_type": "Ktrans", "scope": "whole image",
         "rmse": i, "mae": i, "bias": i, "correlation": 0.9}
        for i in (1, 2)]
    if use_path:
        summary["nifti_analysis"]["reference_scoring"] = {"maps": [
            {"submitted_path": f"/submission/P0{i}/Ktrans.nii.gz",
             "whole_map": {"rmse": i, "mae": i, "bias": i, "correlation": 0.9}}
            for i in (1, 2)]}
        # Force path-based matching rather than allowing the scan fallback.
        for row in summary["analysis_fields"]["reference_metric_rows"]:
            row.pop("submitted_path")
            row.pop("scan_label")
    model = _build_report_model([summary], tag="regression", blinded=True)
    column = model["main_map_metric_headers"].index("RMSE")
    assert [row[column] for row in model["main_map_metric_rows"]] == ["1", "2"]



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
    assert "finite-voxel" in methods
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
    assert "Bias is the mean" not in methods


def test_map_results_hide_reference_columns_when_no_reference_exists():
    summary = _summary(ref=False)
    summary["nifti_analysis"]["maps"] = [{
        "detected_map_type": "CBF",
        "units": "ml/100g/min",
        "metadata": {},
        "stats": {
            "finite_percent": 99.5,
            "negative_voxel_percent": 0.2,
            "mean": 58.1,
        },
    }]
    model = _build_report_model([summary], tag="t", blinded=True)

    assert model["main_map_metric_headers"] == [
        "Map", "Units", "Finite", "Negative", "Mean",
    ]
    assert len(model["main_map_metric_rows"][0]) == 5
    assert "Not available" not in model["main_map_metric_rows"][0]


def test_map_results_add_reference_columns_only_when_comparisons_exist():
    summary = _scored()
    summary["analysis_fields"]["reference_metric_rows"] = [{
        "detected_map_type": "CBF",
        "scope": "Whole image",
        "rmse": 7.2,
        "mae": 5.0,
        "bias": -2.0,
        "correlation": 0.9,
    }]
    summary["nifti_analysis"]["maps"][0]["stats"] = {
        "finite_percent": 99.5,
        "negative_voxel_percent": 0.2,
        "mean": 58.0,
    }
    model = _build_report_model([summary], tag="t", blinded=True)

    assert model["main_map_metric_headers"][-4:] == [
        "RMSE", "MAE", "Bias", "Corr.",
    ]
    assert model["main_map_metric_rows"][0][-4:] == ["7.2", "5", "-2", "0.9"]


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


# ── ICC has two different reasons for being blank ─────────────────────────

def test_the_icc_caveat_names_the_missing_decision_not_missing_data() -> None:
    """A reader must not be sent looking for data when a choice is missing.

    Before ICC existed there was one reason it was blank: no repeated scans.
    There are now two, and they call for different actions. Reporting "requires
    repeated datasets" when the real blocker is that nobody has picked a model
    sends someone to collect data that would change nothing.
    """
    from services.pdf_report_service import build_limitations

    items = build_limitations(
        reference_available=True, map_types=["CBF"], challenges=["ASL"],
        cov_reported=True, icc_status="not_configured",
    )
    text = " ".join(items)
    assert "has not selected an ICC model" in text
    assert "grouped_statistics.icc.model" in text
    # Repeatability CoV genuinely does still need the data.
    assert "repeated" in text


def test_the_old_caveat_still_applies_once_a_model_is_chosen() -> None:
    from services.pdf_report_service import build_limitations

    items = build_limitations(
        reference_available=True, map_types=["CBF"], challenges=["ASL"],
        cov_reported=True, icc_status="no_groups",
    )
    text = " ".join(items)
    assert "require repeated" in text
    assert "has not selected an ICC model" not in text


def test_the_caveat_is_unchanged_when_nothing_is_known() -> None:
    """An omitted status must not silently change the wording."""
    from services.pdf_report_service import build_limitations

    items = build_limitations(
        reference_available=True, map_types=["CBF"], challenges=["ASL"],
        cov_reported=True,
    )
    assert any("require repeated" in item for item in items)


# ── Overlapping regions are disclosed, not silently averaged ──────────────
#
# The DCE challenge ships nested ROIs: all 262 hippocampus voxels are also
# grey matter. A table with one row per region invites the reader to treat the
# regions as a partition, and the challenge's own answer key does exactly that
# (its grey matter is 4698 voxels, the 4960-voxel mask minus the hippocampus).
# The pipeline reports the mask as supplied, so the two differ for a reason
# nothing on the page used to explain.

def test_a_nested_region_is_called_out_by_name() -> None:
    from services.pdf_report_service import build_limitations

    items = build_limitations(
        reference_available=True, map_types=["Ktrans"], challenges=["DCE"],
        cov_reported=True,
        mask_overlaps=[{
            "regions": ["gray matter", "hippocampus"],
            "shared_voxels": 262, "voxels": [4960, 262], "nested": True,
        }],
    )
    note = next(i for i in items if "overlap" in i.lower())
    assert "every hippocampus voxel" in note
    assert "262" in note
    assert "inside gray matter" in note
    assert "not independent" in note


def test_a_partial_overlap_reports_the_shared_count() -> None:
    from services.pdf_report_service import build_limitations

    items = build_limitations(
        reference_available=True, map_types=["Ktrans"], challenges=["DCE"],
        cov_reported=True,
        mask_overlaps=[{
            "regions": ["gray matter", "lesion"],
            "shared_voxels": 40, "voxels": [4960, 120], "nested": False,
        }],
    )
    note = next(i for i in items if "overlap" in i.lower())
    assert "share 40 voxels" in note
    assert "every" not in note


def test_disjoint_regions_add_no_note() -> None:
    """The caveat list is only useful if it stays short."""
    from services.pdf_report_service import build_limitations

    items = build_limitations(
        reference_available=True, map_types=["Ktrans"], challenges=["DCE"],
        cov_reported=True, mask_overlaps=[],
    )
    assert not any("overlap" in i.lower() for i in items)


# ── Small numbers must survive the formatter ──────────────────────────────
#
# Ktrans is of order 1e-4. Printed to three decimal places, a real bias of
# 5.5e-05 becomes "0" and a real bias of -2e-05 becomes "-0". A reviewer
# reading that column would see a submission that matched the ground truth
# perfectly, when what they were actually looking at was the rounding. This
# was live in the DCE report until it was caught by eye.

def test_a_small_bias_is_not_rounded_to_zero() -> None:
    from services.pdf_report_service import _fmt

    assert _fmt(5.5e-05) == "5.50e-05"
    assert _fmt(-2e-05) == "-2.00e-05"
    assert _fmt(1.5e-04) == "1.50e-04"


def test_the_sign_of_a_small_number_survives() -> None:
    """"-0" is worse than useless: it hides the direction of the error."""
    from services.pdf_report_service import _fmt

    assert _fmt(-2e-05).startswith("-")
    assert "-0" != _fmt(-2e-05)


def test_zero_is_still_zero() -> None:
    """Zero is a measurement, not a rounding artifact, and must read as one."""
    from services.pdf_report_service import _fmt

    assert _fmt(0.0) == "0"
    assert _fmt(0) == "0"


def test_ordinary_numbers_are_unchanged() -> None:
    """The fix must not restyle every number in every report."""
    from services.pdf_report_service import _fmt

    assert _fmt(3.182) == "3.182"
    assert _fmt(-0.002) == "-0.002"
    assert _fmt(0.05) == "0.05"
    assert _fmt(1596595) == "1596595"
    assert _fmt(12.0) == "12"


def test_unusable_numbers_read_as_unavailable() -> None:
    from services.pdf_report_service import _fmt

    assert _fmt(float("nan")) == "Not available"
    assert _fmt(float("inf")) == "Not available"
    assert _fmt(None) == "Not available"


def test_the_threshold_sits_where_three_decimals_give_up() -> None:
    """Just above the boundary keeps decimals; just below switches notation."""
    from services.pdf_report_service import _fmt

    assert "e-" not in _fmt(0.001)
    assert "e-" in _fmt(0.0001)
