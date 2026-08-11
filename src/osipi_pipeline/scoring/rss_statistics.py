"""Residual Sum of Squares for measured and modelled 4-D signal curves.

RSS is computed independently at every spatial voxel across the time axis::

    RSS = sum_t((S_measured,t - S_modelled,t) ** 2)

This is deliberately named RSS, not deviance. It is an unnormalised,
descriptive prototype measure and is not an official OSIPI score.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Sequence

import numpy as np


@dataclass(frozen=True)
class RssSummary:
    median: float | None
    mean: float | None
    standard_deviation: float | None
    voxel_count: int
    status: str = "available"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def voxelwise_rss(
    measured_values: Sequence[float],
    modelled_values: Sequence[float],
    shape: Sequence[int],
) -> np.ndarray:
    """Return a 3-D RSS array; a voxel is NaN if any time point is invalid."""
    dims = tuple(int(value) for value in shape)
    if len(dims) != 4:
        raise ValueError("RSS requires measured and modelled 4-D signals")
    measured = np.asarray(measured_values, dtype=np.float64).reshape(dims)
    modelled = np.asarray(modelled_values, dtype=np.float64).reshape(dims)
    finite = np.isfinite(measured).all(axis=-1) & np.isfinite(modelled).all(axis=-1)
    residual = measured - modelled
    rss = np.sum(residual * residual, axis=-1)
    return np.where(finite, rss, np.nan)


def summarize_rss(rss: Any, selector: Any | None = None) -> RssSummary:
    """Summarize finite voxelwise RSS values, optionally inside an ROI mask."""
    values = np.asarray(rss, dtype=np.float64)
    if selector is not None:
        mask = np.asarray(selector, dtype=bool)
        if mask.shape != values.shape:
            raise ValueError("ROI mask shape does not match the RSS spatial shape")
        values = values[mask]
    else:
        values = values.reshape(-1)
    values = values[np.isfinite(values)]
    if values.size == 0:
        return RssSummary(None, None, None, 0, "no_finite_voxels")
    return RssSummary(
        median=float(np.median(values)),
        mean=float(np.mean(values)),
        standard_deviation=float(np.std(values, ddof=0)),
        voxel_count=int(values.size),
    )


METHODOLOGY = {
    "name": "Residual Sum of Squares (RSS)",
    "formula": "sum_t((S_measured,t - S_modelled,t)^2) per voxel",
    "normalization": "raw, unnormalised RSS",
    "roi_summary": "median, mean, population SD (ddof=0), and finite voxel count",
    "scope": "descriptive prototype analysis; not deviance or official OSIPI scoring",
}
