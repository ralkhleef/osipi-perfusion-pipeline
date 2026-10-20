"""Residual Sum of Squares for measured and modelled 4-D signal curves."""

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


MAX_VOXELS_PER_READ = 8 * 1024 * 1024


def _time_chunks(shape: Sequence[int], max_voxels: int):
    """Yield (start, stop) along the time axis, each at most ``max_voxels``."""
    last = int(shape[-1])
    plane = 1
    for dim in shape[:-1]:
        plane *= int(dim)
    if last <= 0 or plane <= 0:
        return
    step = max(1, int(max_voxels // plane))
    for start in range(0, last, step):
        yield start, min(start + step, last)


def streaming_voxelwise_rss(
    measured_dataobj: Any,
    modelled_dataobj: Any,
    shape: Sequence[int],
    max_voxels: int = MAX_VOXELS_PER_READ,
) -> np.ndarray:
    """Voxelwise RSS from two 4-D array proxies, without loading either whole."""
    dims = tuple(int(value) for value in shape)
    if len(dims) != 4:
        raise ValueError("RSS requires measured and modelled 4-D signals")

    spatial = dims[:3]
    total = np.zeros(spatial, dtype=np.float64)
    valid = np.ones(spatial, dtype=bool)

    for start, stop in _time_chunks(dims, max_voxels):
        measured = np.asarray(measured_dataobj[..., start:stop], dtype=np.float64)
        modelled = np.asarray(modelled_dataobj[..., start:stop], dtype=np.float64)
        finite = np.isfinite(measured).all(axis=-1) & np.isfinite(modelled).all(axis=-1)
        valid &= finite
        residual = measured - modelled
        np.multiply(residual, residual, out=residual)
        if not finite.all():
            np.nan_to_num(residual, copy=False, nan=0.0, posinf=0.0, neginf=0.0)
        total += residual.sum(axis=-1)
        del measured, modelled, residual, finite

    return np.where(valid, total, np.nan)


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
    "reading": "4-D signals are streamed a slab of timepoints at a time; the "
               "result is identical to reading them whole",
    "masking": "a 3-D ROI mask selects a voxel-by-time concentration matrix; "
               "RSS is calculated along each matrix row",
    "formula": "sum_t((S_measured,t - S_modelled,t)^2) per voxel",
    "normalization": "raw, unnormalised RSS",
    "roi_summary": "median, mean, population SD (ddof=0), and finite voxel count",
    "scope": "descriptive prototype analysis; not deviance or official OSIPI scoring",
}
