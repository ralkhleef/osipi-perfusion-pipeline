"""Within-ROI descriptive statistics.

Scope is one map, one ROI, one scan. Nothing here compares across scans,
no repeatability, reproducibility, or inter-participant variability, and no
accuracy, deviance, or RSS is computed here.

Formula tests run directly on arrays; only the masking and geometry tests
need file-shaped fixtures, and those use in-memory dicts rather than NIfTI
files.
"""

from __future__ import annotations

import json
import math

import pytest

from osipi_pipeline.ingestion.models import SubmissionArtifact
from osipi_pipeline.scoring.descriptive_statistics import (
    COV_MEAN_TOLERANCE,
    CSV_COLUMNS,
    METHODOLOGY,
    REASON_MEAN_NEAR_ZERO,
    STATUS_EMPTY_ROI,
    STATUS_GEOMETRY_MISMATCH,
    STATUS_NO_FINITE_VALUES,
    RoiDefinition,
    csv_row,
    describe_values,
)

from services.roi_descriptive_service import (  # noqa: E402
    compute_roi_descriptive_statistics,
    eligible_artifacts,
    roi_definitions_from_masks,
)

INF = float("inf")
NAN = float("nan")


# ── Pure formulas ─────────────────────────────────────────────────────────

def test_median_of_odd_length() -> None:
    assert describe_values([3.0, 1.0, 2.0]).median == 2.0


def test_median_of_even_length_is_the_midpoint() -> None:
    assert describe_values([1.0, 2.0, 3.0, 4.0]).median == 2.5


def test_median_is_not_the_mean() -> None:
    """A skewed set separates the two; hand-computed."""
    stats = describe_values([1.0, 1.0, 1.0, 97.0])
    assert stats.median == 1.0
    assert stats.mean == 25.0


def test_population_standard_deviation() -> None:
    """sqrt(((1-2.5)^2+(2-2.5)^2+(3-2.5)^2+(4-2.5)^2)/4) = sqrt(1.25)."""
    stats = describe_values([1.0, 2.0, 3.0, 4.0])
    assert stats.standard_deviation == pytest.approx(math.sqrt(1.25))


def test_population_sd_differs_from_sample_sd() -> None:
    """Guards the ddof=0 choice: sample SD would be sqrt(5/3)."""
    stats = describe_values([1.0, 2.0, 3.0, 4.0])
    assert stats.standard_deviation != pytest.approx(math.sqrt(5.0 / 3.0))


def test_cov_with_positive_mean() -> None:
    stats = describe_values([1.0, 2.0, 3.0, 4.0])
    assert stats.coefficient_of_variation == pytest.approx(math.sqrt(1.25) / 2.5)


def test_cov_uses_absolute_mean_for_negative_data() -> None:
    """A negative mean must not flip the CoV sign."""
    stats = describe_values([-1.0, -2.0, -3.0, -4.0])
    assert stats.mean == pytest.approx(-2.5)
    assert stats.coefficient_of_variation == pytest.approx(math.sqrt(1.25) / 2.5)
    assert stats.coefficient_of_variation > 0


def test_cov_denominator_is_the_mean_not_the_median() -> None:
    """Skewed data separates mean from median; CoV must follow the mean."""
    stats = describe_values([1.0, 1.0, 1.0, 97.0])
    assert stats.coefficient_of_variation == pytest.approx(
        stats.standard_deviation / abs(stats.mean))
    assert stats.coefficient_of_variation != pytest.approx(
        stats.standard_deviation / abs(stats.median))


def test_cov_unavailable_at_zero_mean() -> None:
    stats = describe_values([-1.0, 1.0])
    assert stats.coefficient_of_variation is None
    assert stats.unavailable_reason == REASON_MEAN_NEAR_ZERO
    assert stats.median is not None and stats.standard_deviation is not None


def test_cov_unavailable_within_near_zero_tolerance() -> None:
    stats = describe_values([COV_MEAN_TOLERANCE / 4, COV_MEAN_TOLERANCE / 4])
    assert stats.coefficient_of_variation is None
    assert stats.unavailable_reason == REASON_MEAN_NEAR_ZERO


def test_cov_never_returns_infinity() -> None:
    stats = describe_values([0.0, 0.0, 0.0])
    assert stats.coefficient_of_variation is None
    assert not any(
        v == INF for v in (stats.coefficient_of_variation or 0,
                           stats.standard_deviation or 0))


def test_single_voxel_roi() -> None:
    stats = describe_values([0.5])
    assert stats.median == 0.5
    assert stats.standard_deviation == 0.0
    assert stats.coefficient_of_variation == 0.0
    assert stats.voxel_count == 1


def test_single_zero_voxel_has_unavailable_cov() -> None:
    stats = describe_values([0.0])
    assert stats.standard_deviation == 0.0
    assert stats.coefficient_of_variation is None


@pytest.mark.parametrize("bad", [NAN, INF, -INF])
def test_non_finite_values_are_excluded(bad: float) -> None:
    stats = describe_values([1.0, bad, 3.0])
    assert stats.voxel_count == 2
    assert stats.excluded_non_finite_count == 1
    assert stats.median == 2.0


def test_finite_negative_values_are_retained() -> None:
    """OSIPI has not declared negative Ktrans invalid, so it is not dropped."""
    stats = describe_values([-1.0, 1.0, 3.0])
    assert stats.voxel_count == 3
    assert stats.negative_count == 1
    assert stats.median == 1.0


def test_zeros_are_retained_and_counted() -> None:
    stats = describe_values([0.0, 2.0, 4.0])
    assert stats.voxel_count == 3
    assert stats.zero_count == 1
    assert stats.median == 2.0


def test_empty_roi_is_unavailable_not_zero() -> None:
    stats = describe_values([])
    assert stats.status == STATUS_EMPTY_ROI
    assert stats.median is None
    assert stats.standard_deviation is None
    assert stats.coefficient_of_variation is None


def test_roi_with_no_finite_values_is_unavailable() -> None:
    stats = describe_values([NAN, INF, -INF])
    assert stats.status == STATUS_NO_FINITE_VALUES
    assert stats.median is None
    assert stats.excluded_non_finite_count == 3


def test_mask_voxel_count_is_preserved() -> None:
    stats = describe_values([1.0, 2.0], mask_voxel_count=10)
    assert stats.mask_voxel_count == 10
    assert stats.voxel_count == 2


# ── Fixtures for masking / geometry ───────────────────────────────────────

def _artifact(map_type="ktrans", *, participant="1", repeat="1", site="1",
              dataset="synthetic", dims=3, path="Ktrans.nii.gz"):
    return SubmissionArtifact(
        path=path, role="parameter_map", challenge="dce", dataset=dataset,
        participant=participant, repeat=repeat, site=site,
        map_type=map_type, dimensions=dims,
    )


def _roi(roi_id="tumour", label="Tumour", mask_path="masks/tumour.nii.gz"):
    return RoiDefinition(roi_id=roi_id, label=label, mask_path=mask_path)


def _loader(table):
    """Return a load_values stub backed by an in-memory table."""
    def load(path):
        key = str(path).replace("\\", "/").lstrip("./")
        for name, payload in table.items():
            if key.endswith(name):
                return payload
        raise FileNotFoundError(key)
    return load


def _vol(values, shape=(2, 2)):
    return {"shape": list(shape), "values": list(values)}


def _compute(artifacts, rois, table, challenge="dce"):
    return compute_roi_descriptive_statistics(
        artifacts, rois, challenge=challenge, load_values=_loader(table))


# ── Masking ───────────────────────────────────────────────────────────────

def test_mask_selects_the_right_voxels() -> None:
    table = {
        "Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
        "tumour.nii.gz": _vol([1, 1, 0, 0]),
    }
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.voxel_count == 2
    assert result.roi_median == 1.5
    assert result.status == "available"


def test_non_binary_mask_values_count_as_inside() -> None:
    table = {
        "Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
        "tumour.nii.gz": _vol([2, 0, 0.5, 0]),
    }
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.voxel_count == 2
    assert result.roi_median == 2.0


def test_empty_mask_yields_unavailable() -> None:
    table = {
        "Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
        "tumour.nii.gz": _vol([0, 0, 0, 0]),
    }
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.status == STATUS_EMPTY_ROI
    assert result.roi_median is None


def test_mask_over_non_finite_values_yields_unavailable() -> None:
    table = {
        "Ktrans.nii.gz": _vol([NAN, INF, 3.0, 4.0]),
        "tumour.nii.gz": _vol([1, 1, 0, 0]),
    }
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.status == STATUS_NO_FINITE_VALUES


def test_multiple_rois_produce_multiple_rows() -> None:
    table = {
        "Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
        "tumour.nii.gz": _vol([1, 1, 0, 0]),
        "liver.nii.gz": _vol([0, 0, 1, 1]),
    }
    results = _compute([_artifact()],
                       [_roi(), _roi("liver", "Liver", "masks/liver.nii.gz")], table)
    assert {r.roi_id for r in results} == {"tumour", "liver"}
    assert {r.roi_median for r in results} == {1.5, 3.5}


def test_scans_remain_separate() -> None:
    table = {
        "a/Ktrans.nii.gz": _vol([1.0, 1.0, 0.0, 0.0]),
        "b/Ktrans.nii.gz": _vol([9.0, 9.0, 0.0, 0.0]),
        "tumour.nii.gz": _vol([1, 1, 0, 0]),
    }
    results = _compute([
        _artifact(repeat="1", path="a/Ktrans.nii.gz"),
        _artifact(repeat="2", path="b/Ktrans.nii.gz"),
    ], [_roi()], table)
    by_repeat = {r.repeat: r.roi_median for r in results}
    assert by_repeat == {"1": 1.0, "2": 9.0}


def test_synthetic_site_identity_is_retained() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 1.0, 0.0, 0.0]),
             "tumour.nii.gz": _vol([1, 1, 0, 0])}
    (result,) = _compute([_artifact(site="3")], [_roi()], table)
    assert result.site == "3"
    assert result.dataset == "synthetic"


def test_clinical_implicit_site_stays_none() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 1.0, 0.0, 0.0]),
             "tumour.nii.gz": _vol([1, 1, 0, 0])}
    (result,) = _compute([_artifact(dataset="clinical", site=None)], [_roi()], table)
    assert result.site is None
    assert csv_row(result)[CSV_COLUMNS.index("site")] == ""


# ── Geometry ──────────────────────────────────────────────────────────────

def test_matching_geometry_passes() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0], (2, 2)),
             "tumour.nii.gz": _vol([1, 1, 0, 0], (2, 2))}
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.status == "available"


def test_shape_mismatch_is_unavailable_and_not_resampled() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0], (2, 2)),
             "tumour.nii.gz": _vol([1, 0, 1, 0, 1, 0], (2, 3))}
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.status == STATUS_GEOMETRY_MISMATCH
    assert result.roi_median is None


def test_same_shape_but_different_affine_is_geometry_mismatch() -> None:
    identity = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    shifted = [[1, 0, 0, 20], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]
    table = {
        "Ktrans.nii.gz": {**_vol([1.0, 2.0, 3.0, 4.0]), "affine": identity, "voxel_size": [1, 1, 1]},
        "tumour.nii.gz": {**_vol([1, 1, 0, 0]), "affine": shifted, "voxel_size": [1, 1, 1]},
    }
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.status == STATUS_GEOMETRY_MISMATCH
    assert result.roi_mean is None


def test_one_bad_roi_does_not_block_the_others() -> None:
    table = {
        "Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0], (2, 2)),
        "bad.nii.gz": _vol([1, 0, 1, 0, 1, 0], (2, 3)),
        "good.nii.gz": _vol([1, 1, 0, 0], (2, 2)),
    }
    results = _compute([_artifact()], [
        _roi("bad", "Bad", "masks/bad.nii.gz"),
        _roi("good", "Good", "masks/good.nii.gz"),
    ], table)
    by_id = {r.roi_id: r for r in results}
    assert by_id["bad"].status == STATUS_GEOMETRY_MISMATCH
    assert by_id["good"].status == "available"
    assert by_id["good"].roi_median == 1.5


# ── Artifact selection ────────────────────────────────────────────────────

@pytest.mark.parametrize("map_type", ["vp", "ve", "kep"])
def test_optional_maps_are_not_in_dce_roi_output(map_type: str) -> None:
    assert eligible_artifacts([_artifact(map_type)], challenge="dce") == []


def test_only_ktrans_is_selected() -> None:
    selected = eligible_artifacts(
        [_artifact("ktrans"), _artifact("vp"), _artifact("ve")], challenge="dce")
    assert [a.map_type for a in selected] == ["ktrans"]


def test_modelled_signal_is_excluded() -> None:
    signal = SubmissionArtifact(path="modelled_st.nii.gz", role="fitted_signal",
                                challenge="dce", artifact_type="modelled_st",
                                participant="1", dimensions=4)
    assert eligible_artifacts([signal], challenge="dce") == []


def test_methods_document_is_excluded() -> None:
    methods = SubmissionArtifact(path="methods.docx", role="methods",
                                 challenge="dce", artifact_type="methods")
    assert eligible_artifacts([methods], challenge="dce") == []


def test_wrong_dimensional_ktrans_is_not_calculated() -> None:
    assert eligible_artifacts([_artifact(dims=4)], challenge="dce") == []


def test_missing_identity_still_allows_within_image_statistics() -> None:
    selected = eligible_artifacts([_artifact(participant=None)], challenge="dce")
    assert len(selected) == 1
    assert selected[0].participant is None


def test_asl_cbf_is_selected_for_roi_output() -> None:
    cbf = SubmissionArtifact(path="cbf.nii.gz", role="parameter_map",
                             challenge="asl", map_type="cbf",
                             participant="1", dimensions=3)
    assert eligible_artifacts([cbf], challenge="asl") == [cbf]


def test_an_unconfigured_challenge_produces_no_roi_output() -> None:
    """Eligibility comes from configuration, never from a built-in default.

    This used to use DSC, which was the unconfigured challenge at the time.
    DSC now has descriptive statistics configured, so the example moved to a
    challenge that does not exist. The rule under test is unchanged: a
    challenge nobody configured yields nothing rather than guessing.
    """
    cbf = SubmissionArtifact(path="cbf.nii.gz", role="parameter_map",
                             challenge="not_a_configured_challenge",
                             map_type="cbf", participant="1", dimensions=3)
    assert eligible_artifacts([cbf], challenge="not_a_configured_challenge") == []


def test_dsc_maps_are_eligible_now_that_dsc_is_configured() -> None:
    """The counterpart: configuration is what turns the analysis on."""
    cbv = SubmissionArtifact(path="cbv.nii.gz", role="parameter_map",
                             challenge="dsc", map_type="cbv",
                             participant="1", dimensions=3)
    assert eligible_artifacts([cbv], challenge="dsc") == [cbv]


def test_no_roi_configuration_yields_no_rows_not_whole_image() -> None:
    """Absent ROIs must not be silently replaced by whole-image statistics."""
    table = {"Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0])}
    assert _compute([_artifact()], [], table) == ()


# ── Serialization ─────────────────────────────────────────────────────────

def test_results_are_json_safe_with_raw_numbers() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
             "tumour.nii.gz": _vol([1, 1, 1, 1])}
    (result,) = _compute([_artifact()], [_roi()], table)
    payload = result.to_dict()
    assert json.loads(json.dumps(payload)) == payload
    assert isinstance(payload["roi_within_scan_cov"], float)
    # A ratio, never a formatted percentage string.
    assert not isinstance(payload["roi_within_scan_cov"], str)
    assert payload["roi_within_scan_cov"] < 1.0
    assert payload["roi_mean"] == pytest.approx(2.5)
    assert payload["roi_minimum"] == pytest.approx(1.0)
    assert payload["roi_maximum"] == pytest.approx(4.0)
    assert payload["roi_range"] == pytest.approx(3.0)


def test_units_come_from_configuration() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
             "tumour.nii.gz": _vol([1, 1, 1, 1])}
    (result,) = _compute([_artifact()], [_roi()], table)
    assert result.units == "min^-1"


def test_csv_columns_are_stable_and_ordered() -> None:
    assert CSV_COLUMNS[0] == "challenge"
    assert "roi_within_scan_cov" in CSV_COLUMNS
    assert CSV_COLUMNS.index("roi_median") < CSV_COLUMNS.index("units")


def test_csv_row_leaves_unavailable_blank_not_zero() -> None:
    table = {"Ktrans.nii.gz": _vol([1.0, 2.0, 3.0, 4.0]),
             "tumour.nii.gz": _vol([0, 0, 0, 0])}
    (result,) = _compute([_artifact()], [_roi()], table)
    row = csv_row(result)
    assert row[CSV_COLUMNS.index("roi_median")] == ""
    assert row[CSV_COLUMNS.index("roi_within_scan_cov")] == ""
    assert row[CSV_COLUMNS.index("status")] == STATUS_EMPTY_ROI


def test_methodology_states_conventions_and_uncertainty() -> None:
    assert "ddof=0" in METHODOLOGY["standard_deviation"]
    assert "absolute arithmetic mean" in METHODOLOGY["coefficient_of_variation"]
    assert "OSIPI" in METHODOLOGY["status"]
    # Must not claim to be a grouped statistic.
    assert "repeatability" in METHODOLOGY["scope"]


def test_roi_definitions_come_from_existing_mask_records() -> None:
    rois = roi_definitions_from_masks([
        {"name": "gray_matter.nii.gz", "label": "Gray matter",
         "path": "/x/masks/gray_matter.nii.gz"},
    ])
    assert rois[0].roi_id == "gray_matter"
    assert rois[0].label == "Gray matter"


# ── Scale ─────────────────────────────────────────────────────────────────

def test_many_scans_and_rois_compute() -> None:
    table = {"tumour.nii.gz": _vol([1, 1, 0, 0]),
             "liver.nii.gz": _vol([0, 0, 1, 1])}
    artifacts = []
    for participant in range(1, 6):
        for repeat in ("1", "2"):
            for site in ("1", "2", "3"):
                path = f"p{participant}/r{repeat}/s{site}/Ktrans.nii.gz"
                table[path] = _vol([1.0, 2.0, 3.0, 4.0])
                artifacts.append(_artifact(participant=str(participant),
                                           repeat=repeat, site=site, path=path))
    rois = [_roi(), _roi("liver", "Liver", "masks/liver.nii.gz")]
    results = _compute(artifacts, rois, table)
    assert len(results) == len(artifacts) * len(rois) == 60
    assert all(r.status == "available" for r in results)


# ── Integration into the canonical scoring result ─────────────────────────

def test_reference_result_carries_additive_roi_keys() -> None:
    """The new keys are additive; existing reference fields are untouched."""
    import scoring

    result = scoring._reference_scoring_result_keys_probe()
    assert "roi_descriptive_statistics" in result
    assert "roi_descriptive_methodology" in result
    # Existing reference-comparison structure is unchanged.
    for key in ("status", "available", "masks_available", "mask_count",
                "warnings", "maps", "summary"):
        assert key in result
    assert set(result["summary"]) >= {
        "reference_map_count", "compared_map_count",
        "mean_rmse", "mean_mae", "mean_bias",
    }


def test_roi_methodology_is_exposed_once_not_per_row() -> None:
    import scoring

    result = scoring._reference_scoring_result_keys_probe()
    methodology = result["roi_descriptive_methodology"]
    assert "ddof=0" in methodology["standard_deviation"]
    assert isinstance(result["roi_descriptive_statistics"], list)


def test_attach_degrades_without_losing_reference_metrics() -> None:
    """An unreadable ROI must not destroy the reference-comparison result."""
    from pathlib import Path

    import scoring

    original = {"reference_root": "/nonexistent", "summary": {"mean_rmse": 1.0}}
    out = scoring.attach_roi_descriptive_statistics(
        original, [], challenge_type="dce", root=Path("."))
    assert out["summary"]["mean_rmse"] == 1.0
    assert out["roi_descriptive_statistics"] == []
