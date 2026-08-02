"""Within-ROI descriptive statistics for a single parameter map.

Scope is deliberately narrow: this describes the **spatial spread of one map
inside one ROI of one scan**. It is not repeatability, not reproducibility,
and not inter-participant variability — those compare *across* scans and
belong to a later phase. The field names carry `within_scan` so the two can
never be confused in code or in an export.

Conventions, all centralised here so nothing drifts:

* **median** — median of the finite voxels in the ROI.
* **standard deviation** — *population* SD, ``sqrt(Σ(x-mean)²/N)``
  (``np.std(..., ddof=0)``), matching what the rest of the pipeline already
  uses for whole-image statistics. Mixing population and sample SD across
  outputs would make numbers silently incomparable.
* **coefficient of variation** — ``SD / abs(mean)``, using the *arithmetic
  mean* as denominator, matching the pipeline's existing CoV. Not the median,
  even though the median is the headline statistic.

All three remain subject to confirmation by OSIPI; see
:data:`METHODOLOGY`.

Values are stored as numbers, never formatted strings. CoV is a ratio
(``0.2295``); rendering it as ``22.95%`` is a presentation concern.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Sequence

# A mean this close to zero makes CoV meaningless — the ratio explodes and
# reports a precision the data does not have. One documented tolerance,
# used everywhere, rather than a clamp or an infinity.
COV_MEAN_TOLERANCE = 1e-12

STATUS_AVAILABLE = "available"
STATUS_EMPTY_ROI = "empty_roi"
STATUS_NO_FINITE_VALUES = "no_finite_values"
STATUS_GEOMETRY_MISMATCH = "geometry_mismatch"
STATUS_MASK_UNREADABLE = "mask_unreadable"
STATUS_MAP_UNREADABLE = "map_unreadable"
STATUS_NO_ROI_CONFIGURED = "no_roi_configured"

REASON_MEAN_NEAR_ZERO = "mean_near_zero"

#: Emitted once per export rather than repeated on every row.
METHODOLOGY: dict[str, str] = {
    "median": "median of finite voxels within the ROI",
    "standard_deviation": "population SD, ddof=0",
    "coefficient_of_variation": "SD / absolute arithmetic mean",
    "cov_near_zero_behavior": f"unavailable when abs(mean) <= {COV_MEAN_TOLERANCE}",
    "excluded_values": "NaN, +inf and -inf; finite negatives and zeros are retained",
    "scope": "within-ROI spatial variability for one scan; not repeatability, "
             "reproducibility, or inter-participant variability",
    "status": "conventions subject to confirmation by OSIPI",
}


@dataclass(frozen=True)
class DescriptiveStatistics:
    """Statistics over one set of values, with the QC counts behind them."""

    voxel_count: int = 0
    mask_voxel_count: int = 0
    excluded_non_finite_count: int = 0
    negative_count: int = 0
    zero_count: int = 0
    median: float | None = None
    standard_deviation: float | None = None
    coefficient_of_variation: float | None = None
    mean: float | None = None
    status: str = STATUS_AVAILABLE
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoiDefinition:
    """One ROI mask that statistics may be computed inside."""

    roi_id: str
    label: str
    mask_path: str
    source: str = "reference"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RoiDescriptiveResult:
    """One ROI's statistics for one scan, carrying its full scan identity."""

    challenge: str | None
    dataset: str | None
    participant: str | None
    repeat: str | None
    site: str | None
    map_type: str | None
    roi_id: str
    roi_label: str
    units: str | None = None
    path: str | None = None
    # Named `within_scan` so this is never mistaken for a grouped statistic.
    roi_median: float | None = None
    roi_within_scan_sd: float | None = None
    roi_within_scan_cov: float | None = None
    voxel_count: int = 0
    mask_voxel_count: int = 0
    excluded_non_finite_count: int = 0
    negative_count: int = 0
    zero_count: int = 0
    status: str = STATUS_AVAILABLE
    unavailable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _finite(values: Iterable[Any]) -> list[float]:
    """Finite floats only. NaN and ±inf are dropped; negatives and zeros stay.

    Negative Ktrans is physically implausible but OSIPI has not declared it
    invalid, so discarding it here would silently alter a submission's
    statistics on our own authority.
    """
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def describe_values(
    values: Sequence[Any], *, mask_voxel_count: int | None = None
) -> DescriptiveStatistics:
    """Compute median, population SD, and CoV over ``values``.

    Testable directly from arrays — no file fixtures required.
    """
    raw = list(values)
    total = len(raw) if mask_voxel_count is None else int(mask_voxel_count)

    if not raw:
        return DescriptiveStatistics(
            mask_voxel_count=total, status=STATUS_EMPTY_ROI,
            unavailable_reason=STATUS_EMPTY_ROI,
        )

    finite = _finite(raw)
    excluded = len(raw) - len(finite)
    if not finite:
        return DescriptiveStatistics(
            mask_voxel_count=total,
            excluded_non_finite_count=excluded,
            status=STATUS_NO_FINITE_VALUES,
            unavailable_reason=STATUS_NO_FINITE_VALUES,
        )

    count = len(finite)
    ordered = sorted(finite)
    middle = count // 2
    median = (
        ordered[middle] if count % 2
        else (ordered[middle - 1] + ordered[middle]) / 2.0
    )

    mean = sum(finite) / count
    # Population SD: divide by N, not N-1.
    variance = sum((value - mean) ** 2 for value in finite) / count
    sd = math.sqrt(variance)

    if abs(mean) <= COV_MEAN_TOLERANCE:
        cov: float | None = None
        reason: str | None = REASON_MEAN_NEAR_ZERO
    else:
        cov = sd / abs(mean)
        reason = None

    return DescriptiveStatistics(
        voxel_count=count,
        mask_voxel_count=total,
        excluded_non_finite_count=excluded,
        negative_count=sum(1 for v in finite if v < 0),
        zero_count=sum(1 for v in finite if v == 0),
        median=median,
        standard_deviation=sd,
        coefficient_of_variation=cov,
        mean=mean,
        status=STATUS_AVAILABLE,
        unavailable_reason=reason,
    )


def result_from_statistics(
    stats: DescriptiveStatistics,
    *,
    artifact: Any,
    roi: RoiDefinition,
    units: str | None = None,
) -> RoiDescriptiveResult:
    """Attach a scan's identity to computed statistics."""
    return RoiDescriptiveResult(
        challenge=getattr(artifact, "challenge", None),
        dataset=getattr(artifact, "dataset", None),
        participant=getattr(artifact, "participant", None),
        repeat=getattr(artifact, "repeat", None),
        site=getattr(artifact, "site", None),
        map_type=getattr(artifact, "map_type", None),
        roi_id=roi.roi_id,
        roi_label=roi.label,
        units=units,
        path=getattr(artifact, "path", None),
        roi_median=stats.median,
        roi_within_scan_sd=stats.standard_deviation,
        roi_within_scan_cov=stats.coefficient_of_variation,
        voxel_count=stats.voxel_count,
        mask_voxel_count=stats.mask_voxel_count,
        excluded_non_finite_count=stats.excluded_non_finite_count,
        negative_count=stats.negative_count,
        zero_count=stats.zero_count,
        status=stats.status,
        unavailable_reason=stats.unavailable_reason,
    )


def unavailable_result(
    *, artifact: Any, roi: RoiDefinition, status: str,
    units: str | None = None,
) -> RoiDescriptiveResult:
    """A result that records why statistics could not be produced.

    An unavailable ROI is reported as unavailable, never as zero — zero is a
    measurement, absence is not.
    """
    return RoiDescriptiveResult(
        challenge=getattr(artifact, "challenge", None),
        dataset=getattr(artifact, "dataset", None),
        participant=getattr(artifact, "participant", None),
        repeat=getattr(artifact, "repeat", None),
        site=getattr(artifact, "site", None),
        map_type=getattr(artifact, "map_type", None),
        roi_id=roi.roi_id,
        roi_label=roi.label,
        units=units,
        path=getattr(artifact, "path", None),
        status=status,
        unavailable_reason=status,
    )


#: Stable CSV column order for the ROI descriptive export.
CSV_COLUMNS: tuple[str, ...] = (
    "challenge", "dataset", "participant", "repeat", "site",
    "map_type", "roi_id", "roi_label", "voxel_count",
    "roi_median", "roi_within_scan_sd", "roi_within_scan_cov",
    "units", "status", "unavailable_reason",
)


def csv_row(result: RoiDescriptiveResult) -> list[Any]:
    """One CSV row. Numbers stay numbers; a blank site stays blank."""
    payload = result.to_dict()
    return ["" if payload.get(column) is None else payload.get(column)
            for column in CSV_COLUMNS]
