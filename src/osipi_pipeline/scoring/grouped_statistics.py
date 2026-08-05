"""Variability of an ROI statistic *across* scans.

The within-scan statistics in :mod:`descriptive_statistics` describe one map in
one ROI of one scan. This module aggregates those per-scan values across a
grouping axis — repeats of one participant at one site, sites for one
participant, or participants within a dataset — to describe how much the
measurement moves when only that one factor changes.

**This is disabled by default and produces nothing until a challenge enables
it.** The arithmetic below is unambiguous, but the scientific choices around it
are not, and OSIPI has not confirmed them:

* whether to aggregate scan-level ROI medians or pooled voxel values,
* whether repeats and sites are paired within a participant before aggregating,
* what minimum group size is meaningful.

The first two are configuration (``source`` and ``pairing``) so that enabling
this is a decision recorded in ``validation_rules.yaml`` rather than an
assumption buried in code. Nothing here is presented as accuracy, deviance,
repeatability, reproducibility or ICC; those need definitions this module does
not have.

Conventions match the within-scan statistics exactly, so the two are
comparable: population SD (``ddof=0``) and CoV as SD over the absolute
arithmetic mean, unavailable when that mean is near zero.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any, Iterable, Sequence

from osipi_pipeline.scoring.descriptive_statistics import COV_MEAN_TOLERANCE

#: Grouping axes. Each names the field that varies while the others are held
#: fixed, so a group isolates one source of variation.
AXIS_REPEAT = "inter_repeat"
AXIS_SITE = "inter_site"
AXIS_PARTICIPANT = "inter_participant"

AXES = (AXIS_REPEAT, AXIS_SITE, AXIS_PARTICIPANT)

#: Which identity fields are held fixed for each axis. Everything not listed —
#: and not the axis itself — would confound the comparison.
_HELD_FIXED = {
    AXIS_REPEAT: ("dataset", "participant", "site"),
    AXIS_SITE: ("dataset", "participant", "repeat"),
    AXIS_PARTICIPANT: ("dataset", "site", "repeat"),
}
_VARIES = {
    AXIS_REPEAT: "repeat",
    AXIS_SITE: "site",
    AXIS_PARTICIPANT: "participant",
}

STATUS_AVAILABLE = "available"
STATUS_TOO_FEW_SCANS = "too_few_scans"
STATUS_NO_FINITE_VALUES = "no_finite_values"
REASON_MEAN_NEAR_ZERO = "mean_near_zero"

#: A group of one cannot show variation; two is the minimum that can.
MIN_GROUP_SIZE = 2

#: Emitted once per export, like the within-scan methodology.
METHODOLOGY: dict[str, str] = {
    "source": "scan-level ROI medians, unless the challenge configures otherwise",
    "standard_deviation": "population SD, ddof=0, across the scans in the group",
    "coefficient_of_variation": "SD / absolute arithmetic mean of the group",
    "grouping": "one axis varies; dataset, ROI, map type and the remaining "
                "identity fields are held fixed",
    "minimum_group_size": str(MIN_GROUP_SIZE),
    "scope": "variation across scans along one axis; not accuracy, deviance, "
             "repeatability, reproducibility or ICC",
    "status": "conventions subject to confirmation by OSIPI",
}


@dataclass(frozen=True)
class GroupedResult:
    """Variation of one ROI statistic along one axis, for one group."""

    axis: str
    challenge: str | None
    dataset: str | None
    roi_id: str
    roi_label: str
    map_type: str | None
    units: str | None = None
    #: The identity fields held fixed, e.g. {"participant": "1", "site": "1"}.
    held_fixed: dict[str, str | None] = field(default_factory=dict)
    #: The distinct values the axis took, in sorted order.
    varied_over: tuple[str, ...] = ()
    scan_count: int = 0
    mean: float | None = None
    standard_deviation: float | None = None
    coefficient_of_variation: float | None = None
    status: str = STATUS_AVAILABLE
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        # `varied_over` is a tuple so the dataclass stays hashable and
        # immutable, but JSON has no tuple: without this the record would not
        # survive a write-and-read round trip, which every export depends on.
        data = asdict(self)
        data["varied_over"] = list(self.varied_over)
        return data


CSV_COLUMNS: tuple[str, ...] = (
    "axis", "challenge", "dataset", "roi_id", "roi_label", "map_type",
    "participant", "site", "repeat", "scan_count",
    "group_mean", "group_sd", "group_cov", "units", "status",
    "unavailable_reason",
)


def csv_row(result: GroupedResult) -> list[Any]:
    """One export row. Numbers stay numbers; formatting is a display concern."""
    fixed = result.held_fixed
    return [
        result.axis, result.challenge, result.dataset, result.roi_id,
        result.roi_label, result.map_type,
        fixed.get("participant"), fixed.get("site"), fixed.get("repeat"),
        result.scan_count, result.mean, result.standard_deviation,
        result.coefficient_of_variation, result.units, result.status,
        result.unavailable_reason,
    ]


def _finite(values: Iterable[Any]) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def _describe(values: Sequence[float]) -> tuple[float, float, float | None, str | None]:
    """Mean, population SD and CoV, matching the within-scan conventions."""
    count = len(values)
    mean = sum(values) / count
    variance = sum((value - mean) ** 2 for value in values) / count
    sd = math.sqrt(variance)
    if abs(mean) <= COV_MEAN_TOLERANCE:
        return mean, sd, None, REASON_MEAN_NEAR_ZERO
    return mean, sd, sd / abs(mean), None


def _value_of(row: Any, source: str) -> Any:
    """The per-scan number being aggregated, named by the configured source."""
    if isinstance(row, dict):
        return row.get(source)
    return getattr(row, source, None)


def _field(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    return getattr(row, name, None)


def compute_grouped_statistics(
    roi_rows: Iterable[Any],
    *,
    axes: Sequence[str] = AXES,
    source: str = "roi_median",
    minimum_group_size: int = MIN_GROUP_SIZE,
) -> list[GroupedResult]:
    """Aggregate per-scan ROI statistics along each requested axis.

    ``roi_rows`` are the records the within-scan layer already produced, as
    dicts or dataclasses; every field needed is on them, so no file is read
    here and no geometry is recomputed.

    Groups smaller than ``minimum_group_size`` are reported with an explicit
    status rather than omitted — a reviewer needs to see that a participant had
    only one repeat, not silently find them missing from the table.
    """
    rows = [row for row in roi_rows if _field(row, "roi_id")]
    results: list[GroupedResult] = []

    for axis in axes:
        if axis not in _HELD_FIXED:
            raise ValueError(f"unknown grouping axis: {axis!r}")
        held = _HELD_FIXED[axis]
        varies = _VARIES[axis]

        groups: dict[tuple, list[Any]] = {}
        for row in rows:
            # A row that cannot state the axis it varies over cannot be placed
            # in a group; including it would silently merge distinct scans.
            if _field(row, varies) is None:
                continue
            key = (
                tuple(_field(row, name) for name in held)
                + (_field(row, "roi_id"), _field(row, "map_type"))
            )
            groups.setdefault(key, []).append(row)

        for key, members in sorted(groups.items(), key=lambda item: str(item[0])):
            fixed = dict(zip(held, key[: len(held)]))
            first = members[0]
            varied = tuple(sorted({str(_field(m, varies)) for m in members}))

            base = dict(
                axis=axis,
                challenge=_field(first, "challenge"),
                dataset=_field(first, "dataset"),
                roi_id=str(_field(first, "roi_id")),
                roi_label=str(_field(first, "roi_label") or _field(first, "roi_id")),
                map_type=_field(first, "map_type"),
                units=_field(first, "units"),
                held_fixed=fixed,
                varied_over=varied,
            )

            # Distinct axis values, not row count: two rows for the same repeat
            # are one scan repeated, not two repeats.
            if len(varied) < minimum_group_size:
                results.append(GroupedResult(
                    **base, scan_count=len(varied),
                    status=STATUS_TOO_FEW_SCANS,
                    unavailable_reason=STATUS_TOO_FEW_SCANS))
                continue

            values = _finite(_value_of(m, source) for m in members)
            if len(values) < minimum_group_size:
                results.append(GroupedResult(
                    **base, scan_count=len(values),
                    status=STATUS_NO_FINITE_VALUES,
                    unavailable_reason=STATUS_NO_FINITE_VALUES))
                continue

            mean, sd, cov, reason = _describe(values)
            results.append(GroupedResult(
                **base, scan_count=len(values), mean=mean,
                standard_deviation=sd, coefficient_of_variation=cov,
                status=STATUS_AVAILABLE, unavailable_reason=reason))

    return results
