"""Compute within-ROI descriptive statistics for submitted parameter maps.

Orchestration only. The formulas live in
``osipi_pipeline.scoring.descriptive_statistics`` where they can be tested
directly from arrays; ROI discovery, mask loading, and geometry checks reuse
the helpers already in ``backend.scoring`` rather than opening a second
ROI-loading path.

Computed once per scoring run. The resulting records are passed forward to
the API, JSON, CSV, and the report model, no output format recomputes them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from osipi_pipeline.config.rules import map_type_specs
from osipi_pipeline.scoring.descriptive_statistics import (
    METHODOLOGY,
    STATUS_GEOMETRY_MISMATCH,
    STATUS_MAP_UNREADABLE,
    STATUS_MASK_UNREADABLE,
    RoiDefinition,
    RoiDescriptiveResult,
    describe_values,
    result_from_statistics,
    unavailable_result,
)

logger = logging.getLogger(__name__)

#: Cache sentinel. A plain string would be compared against payloads that
#: contain NumPy arrays, where `==` raises instead of returning False.
_MISSING = object()

# Only Ktrans is required for DCE-2026. The machinery below is map-generic,
# so enabling another parameter is a one-line change here rather than a
# rewrite, but this phase deliberately emits Ktrans only.
DESCRIPTIVE_MAP_TYPES_BY_CHALLENGE: dict[str, tuple[str, ...]] = {
    "dce": ("ktrans",),
}


def roi_definitions_from_masks(masks: Iterable[Mapping[str, Any]]) -> list[RoiDefinition]:
    """Adapt the existing reference-mask records into ROI definitions.

    ``backend.scoring._reference_masks`` already yields ``{name, label,
    path}``; this keeps that as the single ROI source rather than inventing
    a parallel discovery mechanism.
    """
    definitions: list[RoiDefinition] = []
    for mask in masks or ():
        name = str(mask.get("name") or "")
        if not name:
            continue
        definitions.append(RoiDefinition(
            roi_id=_roi_id(name),
            label=str(mask.get("label") or name),
            mask_path=str(mask.get("path") or ""),
            source="reference",
        ))
    return definitions


def _roi_id(name: str) -> str:
    """Stable identifier derived from the mask filename."""
    stem = name.lower()
    for suffix in (".nii.gz", ".nii"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    return "".join(ch if ch.isalnum() else "_" for ch in stem).strip("_") or "roi"


def _units_for(map_type: str | None) -> str | None:
    if not map_type:
        return None
    spec = map_type_specs().get(str(map_type).lower()) or {}
    units = spec.get("units")
    return str(units) if units else None


def eligible_artifacts(
    artifacts: Sequence[Any], *, challenge: str
) -> list[Any]:
    """Artifacts this phase computes statistics for.

    Only parameter maps of the configured type, only with a readable
    dimensionality matching the configuration, and only with a resolvable
    scan identity. A wrong-dimensional or unidentified file is a validation
    problem already reported; computing statistics from it would dignify bad
    input with a number.
    """
    challenge = (challenge or "").strip().lower()
    wanted = DESCRIPTIVE_MAP_TYPES_BY_CHALLENGE.get(challenge, ())
    if not wanted:
        return []
    specs = map_type_specs()
    out = []
    for artifact in artifacts:
        if getattr(artifact, "role", None) != "parameter_map":
            continue
        map_type = getattr(artifact, "map_type", None)
        if map_type not in wanted:
            continue
        expected = (specs.get(map_type) or {}).get("dimensions")
        dims = getattr(artifact, "dimensions", None)
        if expected is not None and dims is not None and int(dims) != int(expected):
            continue
        if getattr(artifact, "participant", None) in (None, ""):
            continue
        out.append(artifact)
    return out


def compute_roi_descriptive_statistics(
    artifacts: Sequence[Any],
    roi_definitions: Sequence[RoiDefinition],
    *,
    challenge: str,
    root: Path | None = None,
    load_values=None,
) -> tuple[RoiDescriptiveResult, ...]:
    """Statistics for every eligible map against every ROI.

    ``load_values`` defaults to the loader already used by reference scoring;
    it is injectable so the formulas can be exercised without NIfTI fixtures.
    Each mask is loaded at most once for the whole run, and each map once
    across all of its ROIs.
    """
    selected = eligible_artifacts(artifacts, challenge=challenge)
    if not selected or not roi_definitions:
        return ()

    if load_values is None:
        from scoring import _load_nifti_values as load_values  # local import

    mask_cache: dict[str, Any] = {}
    results: list[RoiDescriptiveResult] = []

    for artifact in selected:
        units = _units_for(getattr(artifact, "map_type", None))
        map_path = Path(root or ".") / str(getattr(artifact, "path", ""))
        try:
            # Loaded once, reused for every ROI of this scan.
            map_data = load_values(map_path)
        except Exception:
            logger.debug("ROI statistics: unreadable map %s", map_path, exc_info=True)
            results.extend(
                unavailable_result(artifact=artifact, roi=roi,
                                   status=STATUS_MAP_UNREADABLE, units=units)
                for roi in roi_definitions
            )
            continue

        for roi in roi_definitions:
            # Identity sentinel, not equality: the cached payloads hold NumPy
            # arrays, and `payload == "__missing__"` on one raises rather than
            # returning False.
            mask_data = mask_cache.get(roi.mask_path, _MISSING)
            if mask_data is _MISSING:
                try:
                    mask_data = load_values(Path(roi.mask_path))
                except Exception:
                    logger.debug("ROI statistics: unreadable mask %s",
                                 roi.mask_path, exc_info=True)
                    mask_data = None
                mask_cache[roi.mask_path] = mask_data
            if mask_data is None:
                results.append(unavailable_result(
                    artifact=artifact, roi=roi,
                    status=STATUS_MASK_UNREADABLE, units=units))
                continue

            # Geometry: same policy as reference scoring, matching shape is
            # required and nothing is resampled. One bad ROI must not stop
            # the others.
            mask_shape = mask_data.get("shape")
            map_shape = map_data.get("shape")
            if list(mask_shape or []) != list(map_shape or []):
                results.append(unavailable_result(
                    artifact=artifact, roi=roi,
                    status=STATUS_GEOMETRY_MISMATCH, units=units))
                continue

            # Explicit None checks: `values or []` raises ValueError when
            # `values` is a NumPy array, which is what the real loader
            # returns. Plain-list fixtures never surfaced this.
            map_values = map_data.get("values")
            mask_values = mask_data.get("values")
            selected_values, mask_count = _apply_mask(
                [] if map_values is None else map_values,
                [] if mask_values is None else mask_values,
            )
            stats = describe_values(selected_values, mask_voxel_count=mask_count)
            results.append(result_from_statistics(
                stats, artifact=artifact, roi=roi, units=units))

    return tuple(results)


def _apply_mask(values: Sequence[Any], mask: Sequence[Any]) -> tuple[list[Any], int]:
    """Select map values where the mask is non-zero.

    Matches the existing mask policy: any non-zero, finite mask value counts
    as inside the ROI, so a non-binary mask is not silently rejected.
    """
    try:
        import numpy as np

        # Boolean indexing, not a Python voxel loop, a synthetic submission
        # can hold millions of voxels per scan.
        map_array = np.asarray(values, dtype=float)
        mask_array = np.asarray(mask, dtype=float)
        inside = np.isfinite(mask_array) & (mask_array != 0)
        return map_array[inside].tolist(), int(inside.sum())
    except Exception:
        selected = []
        count = 0
        for value, flag in zip(values, mask):
            try:
                flag_value = float(flag)
            except (TypeError, ValueError):
                continue
            if flag_value != 0 and flag_value == flag_value:
                count += 1
                selected.append(value)
        return selected, count


def methodology() -> dict[str, str]:
    """Formula conventions, emitted once per export rather than per row."""
    return dict(METHODOLOGY)
