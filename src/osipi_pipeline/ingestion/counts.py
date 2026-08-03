"""Canonical, role-based counts for one submission.

Counting NIfTI files is not the same as counting parameter maps. A DCE-2026
submission of 16 scans holds 48 parameter maps, but 67 NIfTI files: the 16
modelled signal-time curves are fitted signals, not parameter maps, and the
reference map and ROI masks are organiser inputs that the team never submitted.
Reporting 67 overstates the submission by 40%.

Every count here is derived from artifact *roles*, which the ingestion layer
has already resolved, so no caller re-walks the tree or re-decides what a file
is. Reference and mask directories are excluded upstream by
``manifest.is_reference_path``, so they cannot reach these counts at all.

Nothing is hardcoded per challenge: roles and artifact types come from
configuration.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

ROLE_PARAMETER_MAP = "parameter_map"
ROLE_FITTED_SIGNAL = "fitted_signal"
ROLE_METHODS = "methods"


@dataclass(frozen=True)
class SubmissionCounts:
    """What a submission actually contains, by role."""

    parameter_maps: int = 0
    #: Parameter-map count per configured map type, e.g. {"ktrans": 16}.
    parameter_maps_by_type: Mapping[str, int] = field(default_factory=dict)
    fitted_signals: int = 0
    methods_documents: int = 0
    #: Files that are neither a parameter map nor a configured artifact.
    unclassified: int = 0
    scans: int = 0
    #: Scan count per dataset, e.g. {"clinical": 10, "synthetic": 6}.
    scans_by_dataset: Mapping[str, int] = field(default_factory=dict)
    datasets: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "parameter_maps": self.parameter_maps,
            "parameter_maps_by_type": dict(self.parameter_maps_by_type),
            "fitted_signals": self.fitted_signals,
            "methods_documents": self.methods_documents,
            "unclassified": self.unclassified,
            "scans": self.scans,
            "scans_by_dataset": dict(self.scans_by_dataset),
            "datasets": list(self.datasets),
        }


def _scan_key(artifact: Any) -> tuple | None:
    """The identity of the scan an artifact belongs to, or None if unresolved."""
    parts = (
        getattr(artifact, "dataset", None),
        getattr(artifact, "participant", None),
        getattr(artifact, "site", None),
        getattr(artifact, "repeat", None),
    )
    return parts if any(part is not None for part in parts) else None


def count_submission(artifacts: Iterable[Any]) -> SubmissionCounts:
    """Role-based counts over already-normalised artifacts.

    A scan is counted once per distinct resolved identity, so three maps in one
    scan directory are three parameter maps but one scan.
    """
    items = list(artifacts)

    by_role: Counter = Counter(getattr(a, "role", None) for a in items)
    parameter_maps = [a for a in items if getattr(a, "role", None) == ROLE_PARAMETER_MAP]
    by_type: Counter = Counter(
        str(getattr(a, "map_type", None) or "unclassified") for a in parameter_maps
    )

    scan_keys = {key for key in (_scan_key(a) for a in items) if key is not None}
    datasets = sorted({
        str(key[0]) for key in scan_keys if key[0] is not None
    })
    scans_by_dataset = Counter(
        str(key[0]) for key in scan_keys if key[0] is not None
    )

    known_roles = {ROLE_PARAMETER_MAP, ROLE_FITTED_SIGNAL, ROLE_METHODS}
    unclassified = sum(
        count for role, count in by_role.items() if role not in known_roles
    )

    return SubmissionCounts(
        parameter_maps=len(parameter_maps),
        parameter_maps_by_type=dict(sorted(by_type.items())),
        fitted_signals=by_role.get(ROLE_FITTED_SIGNAL, 0),
        methods_documents=by_role.get(ROLE_METHODS, 0),
        unclassified=unclassified,
        scans=len(scan_keys),
        scans_by_dataset=dict(sorted(scans_by_dataset.items())),
        datasets=tuple(datasets),
    )


def contents_rows(artifacts: Iterable[Any]) -> list[dict[str, Any]]:
    """One row per (dataset, artifact kind), for the submission-contents table.

    Replaces the per-file appendix: a clean 16-scan submission becomes eight
    rows instead of sixty-seven cards. Units and labels are looked up from
    configuration, never hardcoded here.
    """
    from osipi_pipeline.config.rules import artifact_type_specs, map_type_specs

    map_specs = map_type_specs()
    artifact_specs = artifact_type_specs()

    grouped: dict[tuple, dict[str, Any]] = {}
    for artifact in artifacts:
        role = getattr(artifact, "role", None)
        dataset = getattr(artifact, "dataset", None) or "Not specified"
        map_type = getattr(artifact, "map_type", None)
        artifact_type = getattr(artifact, "artifact_type", None)
        dimensions = getattr(artifact, "dimensions", None)

        if role == ROLE_PARAMETER_MAP and map_type:
            spec = map_specs.get(map_type, {})
            label = str(spec.get("display") or spec.get("label") or map_type)
            units = spec.get("units")
            kind = map_type
        elif artifact_type:
            spec = artifact_specs.get(artifact_type, {})
            label = str(spec.get("label") or artifact_type)
            units = spec.get("units")
            kind = artifact_type
        else:
            label = "Unclassified"
            units = None
            kind = "unclassified"

        key = (dataset, kind)
        row = grouped.setdefault(key, {
            "dataset": dataset,
            "kind": kind,
            "label": label,
            "count": 0,
            "dimensions": set(),
            "units": units,
            "role": role,
        })
        row["count"] += 1
        if dimensions is not None:
            row["dimensions"].add(int(dimensions))

    rows = []
    for row in grouped.values():
        dimensions = sorted(row.pop("dimensions"))
        row["dimensions"] = (
            "".join(f"{d}D" for d in dimensions) if len(dimensions) == 1
            else (", ".join(f"{d}D" for d in dimensions) if dimensions else "—")
        )
        rows.append(row)
    # Dataset first, then parameter maps before other artifacts, then label.
    role_order = {ROLE_PARAMETER_MAP: 0, ROLE_FITTED_SIGNAL: 1, ROLE_METHODS: 2}
    rows.sort(key=lambda r: (str(r["dataset"]).lower(),
                             role_order.get(r["role"], 9),
                             str(r["label"]).lower()))
    return rows
