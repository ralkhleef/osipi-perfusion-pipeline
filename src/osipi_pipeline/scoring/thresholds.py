"""Advisory thresholds for reported metrics.

A challenge lead mentioned using "a rough threshold of acceptable performance
as having a CoV below 15%". That is a useful thing for a reviewer to see at a
glance, and a dangerous thing to hard-code: the same sentence also said there
is no pass/fail and no ranking in these challenges, so a threshold here marks a
row for a human to look at and nothing more.

The distinction is carried in the vocabulary rather than left to convention.
Nothing in this module returns a boolean "pass": a value is either
:data:`WITHIN` the threshold, :data:`ABOVE` it, or :data:`NOT_ASSESSED`. A row
that exceeds a threshold is *flagged for attention*, never failed, never
excluded, never ranked.

Thresholds are configuration and no challenge ships one, so by default every
metric is ``NOT_ASSESSED`` and reports look exactly as they did. A challenge
opts in per metric::

    analysis:
      thresholds:
        roi_within_scan_cov:
          warn_above: 0.15
          note: Rough guide only; not a pass/fail criterion.

Units follow the stored data, so a CoV threshold is the ratio ``0.15``, not
``15``. Percentages are a presentation concern everywhere else in the pipeline
and this is no exception; a threshold written as ``15`` would silently never
fire, so values above 1 for a ratio metric are rejected by the schema rather
than accepted and quietly ignored.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Mapping

#: The value is at or below the configured threshold.
WITHIN = "within_threshold"
#: The value exceeds the threshold. A prompt to look, not a failure.
ABOVE = "above_threshold"
#: No threshold configured, or no value to compare. The default everywhere.
NOT_ASSESSED = "not_assessed"

#: Metrics that are ratios in stored data, so a threshold above 1 is almost
#: certainly a percentage written by mistake.
RATIO_METRICS: frozenset[str] = frozenset({
    "roi_within_scan_cov",
    "coefficient_of_variation",
    "error_coefficient_of_variation",
    "group_cov",
})

METHODOLOGY: dict[str, str] = {
    "purpose": "flag rows for a reviewer to look at",
    "not": "not a pass/fail criterion, not a ranking, not an exclusion rule",
    "units": "thresholds use stored units; a CoV threshold is a ratio (0.15), "
             "not a percentage (15)",
    "comparison": "a value is above the threshold only when strictly greater",
    "default": "no challenge configures a threshold; every metric is "
               "not_assessed unless one is added",
    "status": "values are the challenge's own; none is supplied by the pipeline",
}


@dataclass(frozen=True)
class ThresholdAssessment:
    """One metric compared against its configured threshold."""

    metric: str
    value: float | None = None
    warn_above: float | None = None
    status: str = NOT_ASSESSED
    note: str | None = None

    @property
    def flagged(self) -> bool:
        """Whether a reviewer should look at this row. Never "failed"."""
        return self.status == ABOVE

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["flagged"] = self.flagged
        return data


def assess(
    metric: str, value: Any, thresholds: Mapping[str, Any] | None,
) -> ThresholdAssessment:
    """Compare one metric value against the challenge's threshold for it.

    An unavailable value is ``NOT_ASSESSED``, never ``WITHIN``: a metric that
    could not be computed has not demonstrated anything, and treating absence
    as compliance is the one reading that would mislead a reviewer.
    """
    spec = (thresholds or {}).get(metric) or {}
    warn_above = spec.get("warn_above")
    note = spec.get("note")

    if warn_above is None:
        return ThresholdAssessment(metric=metric, value=_number(value))

    limit = _number(warn_above)
    number = _number(value)
    if limit is None or number is None:
        return ThresholdAssessment(
            metric=metric, value=number, warn_above=limit, note=note,
        )
    return ThresholdAssessment(
        metric=metric,
        value=number,
        warn_above=limit,
        status=ABOVE if number > limit else WITHIN,
        note=note,
    )


def assess_row(
    row: Mapping[str, Any], thresholds: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    """Every configured threshold applied to one result row.

    Returns ``{}`` when the challenge configures none, so a caller can treat
    "no thresholds" as "nothing to render" without inspecting statuses.
    """
    if not thresholds:
        return {}
    return {
        metric: assess(metric, row.get(metric), thresholds).to_dict()
        for metric in sorted(thresholds)
    }


def flagged_metrics(assessments: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Which metrics in a row exceeded their threshold, in a stable order."""
    return sorted(
        metric for metric, data in assessments.items()
        if data.get("status") == ABOVE
    )


def summarize(rows: Iterable[Mapping[str, Any]], thresholds: Mapping[str, Any] | None) -> dict:
    """How many rows a reviewer should look at, and for which metrics.

    Deliberately a count of rows *to look at*, not a score, a percentage
    passed, or anything that could be ordered between submissions.
    """
    if not thresholds:
        return {"configured": False, "assessed_rows": 0, "flagged_rows": 0,
                "flagged_metrics": {}}

    assessed = 0
    flagged_rows = 0
    per_metric: dict[str, int] = {}
    for row in rows:
        assessments = assess_row(row, thresholds)
        statuses = [data.get("status") for data in assessments.values()]
        if any(status != NOT_ASSESSED for status in statuses):
            assessed += 1
        flags = flagged_metrics(assessments)
        if flags:
            flagged_rows += 1
            for metric in flags:
                per_metric[metric] = per_metric.get(metric, 0) + 1
    return {
        "configured": True,
        "assessed_rows": assessed,
        "flagged_rows": flagged_rows,
        "flagged_metrics": per_metric,
    }


def _number(value: Any) -> float | None:
    """A finite float, or ``None``. Booleans are not numbers here."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number and number not in (float("inf"), float("-inf")) else None
