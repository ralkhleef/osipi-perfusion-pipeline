"""Advisory thresholds flag rows for attention and nothing more.

A challenge lead mentioned "a rough threshold of acceptable performance as
having a CoV below 15%", in the same message that ruled out pass/fail and
ranking. Both halves matter. These tests pin the useful behaviour and, just as
deliberately, pin the absence of the dangerous one: nothing here may fail,
exclude or order a submission.

Two mistakes would be easy to make and hard to notice, so both are tested:
writing a CoV threshold as `15` instead of `0.15`, which would parse and then
never fire; and treating an unavailable metric as compliant, which would let a
map that could not be computed look like one that passed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from osipi_pipeline.scoring import thresholds as th  # noqa: E402

COV = "roi_within_scan_cov"
FIFTEEN_PERCENT = {COV: {"warn_above": 0.15, "note": "Rough guide only."}}


# ── The comparison ─────────────────────────────────────────────────────────

def test_a_value_under_the_threshold_is_within_it() -> None:
    result = th.assess(COV, 0.12, FIFTEEN_PERCENT)
    assert result.status == th.WITHIN
    assert result.flagged is False
    assert result.value == 0.12
    assert result.warn_above == 0.15


def test_a_value_over_the_threshold_is_flagged_for_attention() -> None:
    result = th.assess(COV, 0.23, FIFTEEN_PERCENT)
    assert result.status == th.ABOVE
    assert result.flagged is True
    assert result.note == "Rough guide only."


def test_the_threshold_itself_is_not_exceeded() -> None:
    """"Below 15%" makes 0.15 acceptable; only strictly greater is flagged."""
    assert th.assess(COV, 0.15, FIFTEEN_PERCENT).status == th.WITHIN
    assert th.assess(COV, 0.1500001, FIFTEEN_PERCENT).status == th.ABOVE


def test_nothing_is_assessed_without_a_configured_threshold() -> None:
    """The shipped state: no threshold, so no opinion."""
    result = th.assess(COV, 0.99, {})
    assert result.status == th.NOT_ASSESSED
    assert result.flagged is False
    assert result.warn_above is None


@pytest.mark.parametrize("missing", [None, float("nan"), float("inf"), "", "n/a"])
def test_an_unavailable_metric_is_not_assessed_rather_than_compliant(missing) -> None:
    """Absence is not compliance.

    A map whose CoV could not be computed has demonstrated nothing. Reporting
    it as within threshold would make an unusable result look like a good one.
    """
    result = th.assess(COV, missing, FIFTEEN_PERCENT)
    assert result.status == th.NOT_ASSESSED
    assert result.flagged is False


def test_a_boolean_is_not_a_measurement() -> None:
    assert th.assess(COV, True, FIFTEEN_PERCENT).status == th.NOT_ASSESSED


# ── There is no pass/fail here ─────────────────────────────────────────────

def test_the_vocabulary_offers_no_way_to_fail_a_submission() -> None:
    """The guarantee, enforced rather than documented.

    These challenges have no pass/fail and no ranking. If a `fail`, `pass` or
    `score` ever appears in this module's vocabulary, that decision has been
    made by accident and this test is where it surfaces.
    """
    statuses = {th.WITHIN, th.ABOVE, th.NOT_ASSESSED}
    assert not any(
        word in status for status in statuses for word in ("pass", "fail", "score")
    )
    assert "not a pass/fail" in th.METHODOLOGY["not"]

    public = {name for name in dir(th) if not name.startswith("_")}
    for banned in ("passed", "failed", "score", "rank", "grade"):
        assert not any(banned in name.lower() for name in public), (
            f"{banned!r} appears in the threshold module's public surface"
        )


def test_a_flagged_row_is_still_a_complete_result() -> None:
    """Flagging must annotate, never remove or blank the underlying value."""
    row = {COV: 0.4, "roi_median": 42.0, "roi_label": "gray matter"}
    assessments = th.assess_row(row, FIFTEEN_PERCENT)
    assert th.flagged_metrics(assessments) == [COV]
    assert row[COV] == 0.4
    assert row["roi_median"] == 42.0


# ── Row and summary helpers ────────────────────────────────────────────────

def test_no_thresholds_means_nothing_to_render() -> None:
    assert th.assess_row({COV: 0.9}, {}) == {}
    assert th.assess_row({COV: 0.9}, None) == {}


def test_a_summary_counts_rows_to_look_at() -> None:
    rows = [{COV: 0.05}, {COV: 0.30}, {COV: 0.42}, {COV: None}]
    summary = th.summarize(rows, FIFTEEN_PERCENT)
    assert summary["configured"] is True
    assert summary["flagged_rows"] == 2
    assert summary["assessed_rows"] == 3  # the None row was not assessed
    assert summary["flagged_metrics"] == {COV: 2}


def test_a_summary_without_thresholds_says_so() -> None:
    summary = th.summarize([{COV: 0.9}], {})
    assert summary["configured"] is False
    assert summary["flagged_rows"] == 0


def test_the_summary_is_a_count_not_a_proportion() -> None:
    """A percentage passed would be one step from a ranking."""
    summary = th.summarize([{COV: 0.9}], FIFTEEN_PERCENT)
    assert isinstance(summary["flagged_rows"], int)
    assert not any("percent" in key or "rate" in key for key in summary)


# ── The percentage mistake ─────────────────────────────────────────────────

def test_a_percentage_written_as_a_whole_number_is_rejected() -> None:
    """`warn_above: 15` would parse, load, and silently never fire.

    That is worse than an error: the reviewer would believe the check is
    running. The schema refuses it and says what to write instead.
    """
    pytest.importorskip("yaml")
    from osipi_pipeline.config import rules

    errors: list[str] = []
    rules._validate_thresholds({COV: {"warn_above": 15}}, "analysis.thresholds", errors)
    assert errors
    assert "is a ratio" in errors[0]
    assert "0.15" in errors[0]


def test_a_ratio_threshold_is_accepted() -> None:
    pytest.importorskip("yaml")
    from osipi_pipeline.config import rules

    errors: list[str] = []
    rules._validate_thresholds(
        {COV: {"warn_above": 0.15, "note": "Rough guide."}},
        "analysis.thresholds", errors,
    )
    assert errors == []


def test_a_threshold_without_a_limit_is_rejected() -> None:
    """An empty threshold block reads as configured but checks nothing."""
    pytest.importorskip("yaml")
    from osipi_pipeline.config import rules

    errors: list[str] = []
    rules._validate_thresholds({COV: {"note": "no limit"}}, "analysis.thresholds", errors)
    assert errors and "warn_above is required" in errors[0]


def test_a_non_ratio_metric_may_exceed_one() -> None:
    """RMSE is in map units, so 15 is a perfectly ordinary limit."""
    pytest.importorskip("yaml")
    from osipi_pipeline.config import rules

    errors: list[str] = []
    rules._validate_thresholds({"rmse": {"warn_above": 15}}, "analysis.thresholds", errors)
    assert errors == []


# ── Nothing is configured today ────────────────────────────────────────────

def test_no_shipped_challenge_configures_a_threshold() -> None:
    """The 15% figure was described as a rough personal guide, not a rule.

    Writing it into the shipped configuration would turn one person's habit
    into the challenge's criterion. The mechanism is here; the number is the
    organisers' to add.
    """
    pytest.importorskip("yaml")
    from osipi_pipeline.config.rules import thresholds_by_challenge

    configured = thresholds_by_challenge()
    assert configured, "no challenges are configured"
    for challenge, spec in sorted(configured.items()):
        assert spec == {}, f"{challenge} ships a threshold nobody approved"


def test_scoring_records_that_no_threshold_is_configured() -> None:
    """Reports must say "not assessed", not leave an ambiguous blank."""
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import scoring as backend_scoring

    result: dict = {"roi_descriptive_statistics": [{COV: 0.9}]}
    backend_scoring._attach_threshold_flags(result, "asl")

    assert result["thresholds"] == {}
    assert result["threshold_summary"]["configured"] is False
    # The row is untouched when nothing is configured.
    assert "threshold_assessments" not in result["roi_descriptive_statistics"][0]


def test_scoring_annotates_rows_once_a_threshold_is_configured(monkeypatch) -> None:
    sys.path.insert(0, str(REPO_ROOT / "backend"))
    import scoring as backend_scoring

    monkeypatch.setattr(
        backend_scoring, "thresholds_by_challenge",
        lambda: {"asl": FIFTEEN_PERCENT},
    )
    result: dict = {
        "roi_descriptive_statistics": [
            {COV: 0.05, "roi_label": "gray matter"},
            {COV: 0.42, "roi_label": "lesion"},
        ]
    }
    backend_scoring._attach_threshold_flags(result, "asl")

    calm, flagged = result["roi_descriptive_statistics"]
    assert calm["threshold_assessments"][COV]["status"] == th.WITHIN
    assert flagged["threshold_assessments"][COV]["status"] == th.ABOVE
    assert flagged["threshold_assessments"][COV]["flagged"] is True
    assert result["threshold_summary"]["flagged_rows"] == 1
    # The measured values themselves are never rewritten by a threshold.
    assert flagged[COV] == 0.42
