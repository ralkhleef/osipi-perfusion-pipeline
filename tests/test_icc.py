"""ICC, checked against the paper that defines it.

The proposal names ICC alongside RMSE, bias and CoV. Unlike those three it has
six defensible definitions, so the module implements all six and applies none
by default; the model is configuration. That makes two things worth testing
hard: that the arithmetic is right, and that the default really does compute
nothing.

The arithmetic is verified against Shrout & Fleiss (1979), whose Table 1 is
the canonical worked example for exactly this. Their published values for the
six models, and the ANOVA mean squares behind them, are reproduced here as
constants. A test suite that only checked internal consistency could agree
with itself while being wrong about ICC; this one cannot.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from osipi_pipeline.scoring import icc  # noqa: E402

#: Shrout & Fleiss (1979), Table 1: six targets rated by four judges.
SF_TABLE = [
    [9, 2, 5, 8],
    [6, 1, 3, 2],
    [8, 4, 6, 8],
    [7, 1, 2, 6],
    [10, 5, 6, 9],
    [6, 2, 4, 7],
]

#: Their Table 2 mean squares, to two decimals as published.
SF_MSR, SF_MSC, SF_MSE, SF_MSW = 11.24, 32.49, 1.02, 6.26

#: Their published ICCs for the same data.
SF_EXPECTED = {
    icc.MODEL_1_1: 0.17,
    icc.MODEL_2_1: 0.29,
    icc.MODEL_3_1: 0.71,
    icc.MODEL_1_K: 0.44,
    icc.MODEL_2_K: 0.62,
    icc.MODEL_3_K: 0.91,
}


# ── The ANOVA behind every model ───────────────────────────────────────────

def test_the_anova_matches_the_published_mean_squares() -> None:
    table = icc.anova_table(SF_TABLE)
    assert table.targets == 6
    assert table.sessions == 4
    assert table.msr == pytest.approx(SF_MSR, abs=0.005)
    assert table.msc == pytest.approx(SF_MSC, abs=0.005)
    assert table.mse == pytest.approx(SF_MSE, abs=0.005)
    assert table.msw == pytest.approx(SF_MSW, abs=0.005)


def test_a_ragged_table_is_refused_rather_than_padded() -> None:
    with pytest.raises(ValueError, match="one observation per target"):
        icc.anova_table([[1.0, 2.0], [3.0]])


def test_a_table_too_small_to_partition_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 2 targets"):
        icc.anova_table([[1.0, 2.0]])
    with pytest.raises(ValueError, match="at least 2 sessions"):
        icc.anova_table([[1.0], [2.0]])


# ── The six models ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("model, expected", sorted(SF_EXPECTED.items()))
def test_each_model_reproduces_the_published_value(model: str, expected: float) -> None:
    result = icc.compute_icc(SF_TABLE, model=model)
    assert result.status == icc.STATUS_AVAILABLE
    assert result.value == pytest.approx(expected, abs=0.005)
    assert result.model == model
    assert result.model_description, "a value must carry its assumption"


def test_the_models_stand_in_their_documented_order() -> None:
    """Model 3 ignores session offsets, so it cannot fall below model 2.

    A sign error or a swapped mean square would usually break this ordering
    before it broke any single value, which makes it a cheap structural check.
    """
    values = {
        model: icc.compute_icc(SF_TABLE, model=model).value
        for model in icc.MODELS
    }
    assert values[icc.MODEL_1_1] < values[icc.MODEL_2_1] < values[icc.MODEL_3_1]
    # Averaging k measurements is always at least as reliable as one.
    for single, average in (
        (icc.MODEL_1_1, icc.MODEL_1_K),
        (icc.MODEL_2_1, icc.MODEL_2_K),
        (icc.MODEL_3_1, icc.MODEL_3_K),
    ):
        assert values[average] > values[single]


def test_perfect_agreement_is_one_and_pure_noise_is_not() -> None:
    identical = [[1.0, 1.0], [5.0, 5.0], [9.0, 9.0]]
    assert icc.compute_icc(identical, model=icc.MODEL_3_1).value == pytest.approx(1.0)

    # Sessions that disagree as much as targets do carry no reliability.
    mixed = [[1.0, 9.0], [9.0, 1.0], [5.0, 5.0]]
    assert icc.compute_icc(mixed, model=icc.MODEL_3_1).value < 0.0


def test_a_constant_table_reports_no_variance_rather_than_a_ratio() -> None:
    """Nothing varies, so there is nothing to partition; zero would be a lie."""
    result = icc.compute_icc([[4.0, 4.0], [4.0, 4.0]], model=icc.MODEL_3_1)
    assert result.status == icc.STATUS_NO_VARIANCE
    assert result.value is None


def test_an_unknown_model_is_refused_not_defaulted() -> None:
    with pytest.raises(ValueError, match="unknown ICC model"):
        icc.compute_icc(SF_TABLE, model="icc4_2")


# ── Confidence intervals ───────────────────────────────────────────────────

def test_the_inverse_f_matches_published_tables() -> None:
    """The interval is only as good as the quantile underneath it."""
    assert icc.f_quantile(0.95, 5, 15) == pytest.approx(2.9013, abs=5e-4)
    assert icc.f_quantile(0.95, 3, 10) == pytest.approx(3.7083, abs=5e-4)
    assert icc.f_quantile(0.975, 2, 8) == pytest.approx(6.0595, abs=5e-4)
    assert icc.f_quantile(0.99, 4, 20) == pytest.approx(4.4307, abs=5e-4)


def test_the_f_distribution_round_trips() -> None:
    for d1, d2 in ((2, 8), (5, 15), (3, 30)):
        for probability in (0.05, 0.5, 0.9, 0.975):
            quantile = icc.f_quantile(probability, d1, d2)
            assert icc.f_cdf(quantile, d1, d2) == pytest.approx(probability, abs=1e-6)


#: Shrout & Fleiss's published 95% intervals for the same table.
SF_INTERVALS = {
    icc.MODEL_1_1: (-0.13, 0.72),
    icc.MODEL_2_1: (0.02, 0.76),
    icc.MODEL_3_1: (0.34, 0.95),
}


@pytest.mark.parametrize("model, expected", sorted(SF_INTERVALS.items()))
def test_the_intervals_match_the_published_ones(
    model: str, expected: tuple[float, float],
) -> None:
    result = icc.compute_icc(SF_TABLE, model=model, confidence_level=0.95)
    low, high = expected
    assert result.confidence_low == pytest.approx(low, abs=0.01)
    assert result.confidence_high == pytest.approx(high, abs=0.01)


@pytest.mark.parametrize("model", icc.MODELS)
def test_every_interval_brackets_its_estimate_and_stays_in_range(model: str) -> None:
    """The bug this catches: a model-2 interval that came out inverted."""
    result = icc.compute_icc(SF_TABLE, model=model, confidence_level=0.95)
    assert result.confidence_low is not None
    assert result.confidence_high is not None
    assert result.confidence_low <= result.value <= result.confidence_high, (
        f"{model} interval does not contain its own estimate"
    )
    assert -1.0 <= result.confidence_low <= 1.0
    assert -1.0 <= result.confidence_high <= 1.0


def test_a_wider_level_gives_a_wider_interval() -> None:
    narrow = icc.compute_icc(SF_TABLE, model=icc.MODEL_3_1, confidence_level=0.90)
    wide = icc.compute_icc(SF_TABLE, model=icc.MODEL_3_1, confidence_level=0.99)
    assert wide.confidence_low < narrow.confidence_low
    assert wide.confidence_high > narrow.confidence_high


def test_the_interval_can_be_switched_off() -> None:
    result = icc.compute_icc(SF_TABLE, model=icc.MODEL_3_1, confidence_level=None)
    assert result.value is not None
    assert result.confidence_low is None
    assert result.confidence_high is None
    assert result.confidence_level is None


# ── No model means no number ───────────────────────────────────────────────

def test_the_default_model_computes_nothing() -> None:
    """The whole point of the design: the decision stays with the organisers."""
    result = icc.compute_icc(SF_TABLE, model=icc.MODEL_NONE)
    assert result.status == icc.STATUS_NOT_CONFIGURED
    assert result.value is None
    assert result.unavailable_reason == icc.STATUS_NOT_CONFIGURED


def test_no_model_produces_no_rows_at_all() -> None:
    rows = [
        {"participant": "1", "repeat": "1", "roi_id": "gm", "roi_median": 1.0},
        {"participant": "1", "repeat": "2", "roi_id": "gm", "roi_median": 1.1},
    ]
    assert icc.compute_icc_for_rows(rows, model=icc.MODEL_NONE) == []


def test_the_shipped_configuration_chooses_no_model() -> None:
    """Adding ICC must not change what any challenge currently reports."""
    pytest.importorskip("yaml")
    from osipi_pipeline.config.rules import icc_settings_by_challenge

    settings = icc_settings_by_challenge()
    assert settings, "no challenges configured"
    for challenge, spec in sorted(settings.items()):
        assert spec["model"] == icc.MODEL_NONE, (
            f"{challenge} applies an ICC model that nobody has approved"
        )


# ── Building tables from per-scan rows ─────────────────────────────────────

def _scan(participant: str, repeat: str, median: float, **extra):
    return dict(
        {"challenge": "dce", "dataset": "synthetic", "participant": participant,
         "repeat": repeat, "site": "1", "map_type": "ktrans", "roi_id": "gm",
         "roi_label": "gray matter", "units": "min^-1", "roi_median": median},
        **extra,
    )


def test_a_table_is_built_from_participants_and_repeats() -> None:
    rows = [
        _scan("1", "1", 0.20), _scan("1", "2", 0.22),
        _scan("2", "1", 0.31), _scan("2", "2", 0.30),
        _scan("3", "1", 0.15), _scan("3", "2", 0.17),
    ]
    matrix, sessions, excluded = icc.build_table(
        rows, session_field="repeat", source="roi_median",
    )
    assert sessions == ["1", "2"]
    assert matrix == [[0.20, 0.22], [0.31, 0.30], [0.15, 0.17]]
    assert excluded == 0


def test_a_participant_missing_a_session_is_dropped_not_imputed() -> None:
    """Filling a gap would manufacture agreement the data never showed."""
    rows = [
        _scan("1", "1", 0.20), _scan("1", "2", 0.22),
        _scan("2", "1", 0.31),  # no second visit
        _scan("3", "1", 0.15), _scan("3", "2", 0.17),
    ]
    matrix, sessions, excluded = icc.build_table(
        rows, session_field="repeat", source="roi_median",
    )
    assert len(matrix) == 2
    assert excluded == 1
    assert all(len(row) == len(sessions) for row in matrix)


def test_non_finite_and_unidentified_scans_are_left_out() -> None:
    rows = [
        _scan("1", "1", 0.20), _scan("1", "2", 0.22),
        _scan("2", "1", float("nan")), _scan("2", "2", 0.30),
        _scan(None, "1", 0.99), _scan("4", None, 0.99),
    ]
    matrix, _sessions, excluded = icc.build_table(
        rows, session_field="repeat", source="roi_median",
    )
    assert matrix == [[0.20, 0.22]]
    assert excluded == 1  # participant 2 lost its NaN cell


def test_rows_produce_one_result_per_roi_and_map_type() -> None:
    rows = []
    for participant, base in (("1", 0.20), ("2", 0.31), ("3", 0.15)):
        for repeat, offset in (("1", 0.0), ("2", 0.02)):
            rows.append(_scan(participant, repeat, base + offset))
            rows.append(_scan(participant, repeat, base + offset,
                              roi_id="wm", roi_label="white matter"))
    results = icc.compute_icc_for_rows(
        rows, model=icc.MODEL_3_1, axes=["inter_repeat"],
    )
    assert {r.roi_id for r in results} == {"gm", "wm"}
    for result in results:
        assert result.status == icc.STATUS_AVAILABLE
        assert result.target_count == 3
        assert result.session_count == 2
        assert result.axis == "inter_repeat"
        # A near-constant per-participant offset is near-perfect consistency.
        assert result.value > 0.9


def test_participants_cannot_be_the_session_axis() -> None:
    """They are the targets ICC measures over; there is no dimension left."""
    rows = [_scan("1", "1", 0.2), _scan("2", "1", 0.3)]
    assert icc.compute_icc_for_rows(
        rows, model=icc.MODEL_3_1, axes=["inter_participant"],
    ) == []
    assert "inter_participant" not in icc.AXIS_SESSION_FIELD


def test_too_few_participants_is_reported_not_omitted() -> None:
    """A reviewer must see that a table was too small, not find it missing."""
    rows = [_scan("1", "1", 0.20), _scan("1", "2", 0.22)]
    (result,) = icc.compute_icc_for_rows(
        rows, model=icc.MODEL_3_1, axes=["inter_repeat"],
    )
    assert result.status == icc.STATUS_TOO_FEW_TARGETS
    assert result.value is None


# ── Export shape ───────────────────────────────────────────────────────────

def test_a_csv_row_matches_the_declared_columns() -> None:
    result = icc.compute_icc(
        SF_TABLE, model=icc.MODEL_3_1, roi_id="gm", roi_label="gray matter",
        map_type="ktrans", axis="inter_repeat",
    )
    row = icc.csv_row(result)
    assert len(row) == len(icc.CSV_COLUMNS)
    assert row[icc.CSV_COLUMNS.index("model")] == icc.MODEL_3_1
    assert isinstance(row[icc.CSV_COLUMNS.index("icc")], float)


def test_an_unavailable_row_is_blank_never_zero() -> None:
    result = icc.compute_icc([[4.0, 4.0], [4.0, 4.0]], model=icc.MODEL_3_1)
    row = icc.csv_row(result)
    assert row[icc.CSV_COLUMNS.index("icc")] == ""
    assert row[icc.CSV_COLUMNS.index("status")] == icc.STATUS_NO_VARIANCE


def test_every_model_has_a_description() -> None:
    for model in icc.MODELS:
        assert model in icc.MODEL_DESCRIPTIONS
        assert len(icc.MODEL_DESCRIPTIONS[model]) > 40
    assert icc.MODEL_NONE not in icc.MODELS


def test_the_result_survives_a_json_round_trip() -> None:
    import json

    result = icc.compute_icc(SF_TABLE, model=icc.MODEL_2_1, roi_id="gm")
    payload = json.loads(json.dumps(result.to_dict()))
    assert payload["value"] == pytest.approx(result.value)
    assert payload["model"] == icc.MODEL_2_1
    assert math.isfinite(payload["confidence_low"])


# ── Through the configuration and the scoring layer ───────────────────────
#
# The module above is only useful if a challenge can actually turn it on. These
# check the seam: the schema accepts a valid block and rejects a bad one, and a
# configured model reaches the scoring result while `none` leaves the existing
# "not configured" wording exactly as it was.

@pytest.fixture()
def challenge_icc(tmp_path):
    """Temporarily set the DCE ICC block, then put the configuration back."""
    yaml = pytest.importorskip("yaml")
    from osipi_pipeline.config import rules

    config_path = REPO_ROOT / "config" / "validation_rules.yaml"
    original = config_path.read_text(encoding="utf-8")

    def configure(**settings):
        data = yaml.safe_load(original)
        data["challenges"]["dce"]["grouped_statistics"]["icc"] = settings
        config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        rules.clear_config_cache()

    yield configure
    config_path.write_text(original, encoding="utf-8")
    rules.clear_config_cache()


def test_the_schema_accepts_a_configured_model(challenge_icc) -> None:
    from osipi_pipeline.config.rules import icc_settings_by_challenge

    challenge_icc(model=icc.MODEL_2_1, axes=["inter_site"], confidence_level=0.9)
    spec = icc_settings_by_challenge()["dce"]
    assert spec["model"] == icc.MODEL_2_1
    assert spec["axes"] == ("inter_site",)
    assert spec["confidence_level"] == 0.9


def test_the_schema_rejects_a_model_that_does_not_exist(challenge_icc) -> None:
    """A typo must fail loudly, not silently publish a different statistic."""
    from osipi_pipeline.config import rules

    challenge_icc(model="icc2_2")
    with pytest.raises(Exception) as raised:
        rules.validation_rules()
    assert "unknown ICC model" in str(raised.value)


def test_the_schema_rejects_participants_as_an_icc_axis(challenge_icc) -> None:
    from osipi_pipeline.config import rules

    challenge_icc(model=icc.MODEL_3_1, axes=["inter_participant"])
    with pytest.raises(Exception) as raised:
        rules.validation_rules()
    assert "no session dimension" in str(raised.value)


def test_the_schema_rejects_an_impossible_confidence_level(challenge_icc) -> None:
    from osipi_pipeline.config import rules

    challenge_icc(model=icc.MODEL_3_1, confidence_level=95)
    with pytest.raises(Exception) as raised:
        rules.validation_rules()
    assert "between 0 and 1" in str(raised.value)


def test_scoring_reports_icc_as_unconfigured_by_default() -> None:
    """The wording a reviewer sees today must not change until a model is set."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import scoring as backend_scoring

    definition = backend_scoring._icc_definition("dce")
    assert "no ICC model is configured" in definition

    result: dict = {}
    backend_scoring._attach_icc(result, "dce", [])
    assert result["icc_status"] == "not_configured"
    assert result["icc_statistics"] == []
    assert "grouped_statistics.icc.model" in result["icc_unavailable_reason"]


def test_scoring_computes_icc_once_a_model_is_configured(challenge_icc) -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import scoring as backend_scoring

    challenge_icc(model=icc.MODEL_3_1, axes=["inter_repeat"], confidence_level=0.95)

    # Between-participant spread far larger than within-participant scatter,
    # with the scatter *varying* so the residual is not identically zero. A
    # constant offset would give ICC exactly 1 and no interval at all, which is
    # correct but degenerate and would not exercise the interval path.
    visits = {
        "1": (0.201, 0.219),
        "2": (0.314, 0.298),
        "3": (0.148, 0.161),
        "4": (0.262, 0.271),
    }
    rows = [
        _scan(participant, repeat, value)
        for participant, pair in visits.items()
        for repeat, value in zip(("1", "2"), pair)
    ]

    result: dict = {}
    backend_scoring._attach_icc(result, "dce", rows)

    assert result["icc_model"] == icc.MODEL_3_1
    assert result["icc_status"] == "available"
    (row,) = result["icc_statistics"]
    assert row["target_count"] == 4
    assert row["session_count"] == 2
    assert row["value"] > 0.9
    assert row["confidence_low"] is not None
    assert row["confidence_low"] <= row["value"] <= row["confidence_high"]
    # The definition string now states the model rather than the absence of one.
    assert "ICC(3,1)" in backend_scoring._icc_definition("dce")


def test_perfect_consistency_reports_no_interval_rather_than_a_fake_one() -> None:
    """Zero residual means the F ratio is undefined; the estimate still stands.

    Every participant moving by exactly the same amount between visits is
    perfect consistency, ICC(3,1) = 1. There is no residual variance left to
    build an interval from, so the bounds are unavailable rather than pinned
    to 1 and presented as precision the data cannot support.
    """
    constant_offset = [[0.20, 0.215], [0.31, 0.325], [0.15, 0.165], [0.26, 0.275]]
    result = icc.compute_icc(constant_offset, model=icc.MODEL_3_1)
    assert result.status == icc.STATUS_AVAILABLE
    assert result.value == pytest.approx(1.0)
    assert result.confidence_low is None
    assert result.confidence_high is None


@pytest.mark.parametrize("model", icc.MODELS)
def test_perfect_agreement_reports_no_interval_in_every_model(model: str) -> None:
    """All six models agree that perfect agreement has no interval.

    Model 2 used to bail out of its (1 - ICC) division while model 3's closed
    form still produced bounds, so two descriptions of the same perfect
    agreement disagreed about whether an interval existed. They now agree that
    there is none: an interval of zero width would claim infinite precision
    from a handful of scans, and no residual variance means no interval.
    """
    import numpy as np

    rng = np.random.default_rng(4)
    base = rng.uniform(1.0, 5.0, 12)
    table = np.stack([base, base + 1e-13], axis=1)

    result = icc.compute_icc(table, model=model, confidence_level=0.95)
    assert result.status == icc.STATUS_AVAILABLE
    assert result.value == pytest.approx(1.0, abs=1e-6)
    assert result.confidence_low is None, f"{model} claimed a zero-width interval"
    assert result.confidence_high is None


def test_ordinary_agreement_still_gets_a_real_interval() -> None:
    """The perfect-agreement shortcut must not swallow normal data."""
    result = icc.compute_icc(SF_TABLE, model=icc.MODEL_2_1, confidence_level=0.95)
    assert result.confidence_low == pytest.approx(0.02, abs=0.01)
    assert result.confidence_high == pytest.approx(0.76, abs=0.01)
    assert result.confidence_low < result.value < result.confidence_high
