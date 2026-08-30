"""Configuration-driven submission completeness checks.

Consumes normalized :class:`SubmissionArtifact` records, never filenames,
so map-type detection happens exactly once, in the ingestion classifier.

Scope is decided entirely by configuration. A challenge that declares no
``required_maps``/``required_artifacts``/``datasets`` produces no issues at
all, which is what leaves ASL and DSC behaving exactly as before.

Two levels of scope:

* **Scan level**, one ``(dataset, participant, repeat, site)`` combination.
  Required maps and fitted signals must exist for every observed scan.
* **Submission level**, the methods document, required once for the whole
  submission regardless of how many scans it contains.

Issues are ordered hierarchically so a single missing file does not bury the
reader in consequences: identity problems first, then dataset structure, then
missing files within an otherwise valid scan, then dimensionality.

This module decides whether a submission is *structurally* valid. It computes
no scientific quantity.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace
from typing import Any, Iterable, Mapping, Sequence

from osipi_pipeline.config.rules import (
    artifact_type_specs,
    datasets_by_challenge,
    map_type_specs,
    optional_maps_by_challenge,
    required_artifacts_by_challenge,
    required_maps_by_challenge,
)

# Issue codes. Stable strings: the UI and exports key off these.
REQUIRED_MAP_MISSING = "REQUIRED_MAP_MISSING"
REQUIRED_ARTIFACT_MISSING = "REQUIRED_ARTIFACT_MISSING"
MAP_DIMENSION_MISMATCH = "MAP_DIMENSION_MISMATCH"
ARTIFACT_DIMENSION_MISMATCH = "ARTIFACT_DIMENSION_MISMATCH"
DUPLICATE_PARAMETER_MAP = "DUPLICATE_PARAMETER_MAP"
DUPLICATE_REQUIRED_ARTIFACT = "DUPLICATE_REQUIRED_ARTIFACT"
DUPLICATE_METHODS_DOCUMENT = "DUPLICATE_METHODS_DOCUMENT"
INCOMPLETE_ARTIFACT_IDENTITY = "INCOMPLETE_ARTIFACT_IDENTITY"
DATASET_COUNT_MISMATCH = "DATASET_COUNT_MISMATCH"
IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
UNKNOWN_DATASET = "UNKNOWN_DATASET"

# Artifact ids whose requirement applies to the submission, not to each scan.
_SUBMISSION_LEVEL_ROLES = frozenset({"methods"})

_SCAN_ROLES = frozenset({"parameter_map", "fitted_signal"})


def _issue(severity: str, code: str, message: str, **context: Any) -> dict:
    """Build an issue in the existing shape, with additive structured context.

    The legacy consumers read ``severity``/``code``/``message``/``path``; the
    extra keys are ignored by them and consumed by anything that wants to
    group by scan without re-parsing the message.
    """
    issue = {
        "severity": severity,
        "code": code,
        "message": message,
        "path": context.pop("path", None) or None,
    }
    issue.update({k: v for k, v in context.items() if v is not None})
    return issue


def _scan_key(artifact: Any) -> tuple:
    return (artifact.dataset, artifact.participant, artifact.repeat, artifact.site)


def _describe(dataset, participant, repeat, site) -> str:
    """Human-readable scan identity for messages."""
    parts = []
    if dataset:
        parts.append(str(dataset))
    if participant:
        parts.append(f"participant {participant}")
    if repeat:
        parts.append(f"repeat {repeat}")
    if site:
        parts.append(f"site {site}")
    return ", ".join(parts) if parts else "an unidentified scan"


def _map_label(map_id: str) -> str:
    spec = map_type_specs().get(map_id) or {}
    return str(spec.get("display") or map_id)


def _artifact_label(artifact_id: str) -> str:
    spec = artifact_type_specs().get(artifact_id) or {}
    return str(spec.get("label") or artifact_id)


def _required_identity_fields(dataset_spec: Mapping[str, Any] | None) -> tuple[str, ...]:
    """Identity axes a scan must carry for this dataset.

    ``site`` is only required when the dataset declares more than one; a
    clinical dataset with a single site may leave it implicit rather than
    demanding a ``Site1`` directory that carries no information.
    """
    fields = ["dataset", "participant", "repeat"]
    if dataset_spec and int(dataset_spec.get("sites") or 1) > 1:
        fields.append("site")
    return tuple(fields)


def validate_completeness(
    artifacts: Sequence[Any],
    *,
    challenge: str,
    identity_conflicts: Iterable[Any] = (),
    dataset: str | None = None,
) -> list[dict]:
    """Return validation issues for a submission's normalized artifacts.

    Returns an empty list for any challenge that declares none of the
    DCE-2026 configuration, so existing challenges are untouched.

    ``dataset`` names the dataset a submission belongs to when its folders do
    not. Organisers lay data out as participant, site, scan without a dataset
    level, which is a perfectly reasonable layout that nonetheless failed every
    file as missing a dataset. When it is not given and exactly one declared
    dataset fits the observed grid, that one is used and the report says so.
    """
    challenge = (challenge or "").strip().lower()
    required_maps = required_maps_by_challenge().get(challenge, ())
    required_artifacts = required_artifacts_by_challenge().get(challenge, ())
    datasets = datasets_by_challenge().get(challenge, {})
    optional_maps = optional_maps_by_challenge().get(challenge, ())

    if not (required_maps or required_artifacts or datasets):
        return []

    issues: list[dict] = []

    # ── 1. Identity conflicts ────────────────────────────────────────────
    # Reported first: a misassigned scan makes every later count unreliable.
    issues.extend(_conflict_issues(identity_conflicts))

    scan_artifacts = [a for a in artifacts if a.role in _SCAN_ROLES]

    # ── 2. Dataset identity ──────────────────────────────────────────────
    # A submission with no dataset folder is assigned one, either because the
    # reviewer said which it is or because only one declared dataset fits.
    # Recorded as an informational issue rather than applied silently: the
    # dataset decides which grid the counts below are checked against, so it
    # has to be visible in the report.
    if datasets and not any(getattr(a, "dataset", None) for a in scan_artifacts):
        chosen, reason = (dataset, "selected by the reviewer") if dataset \
            else infer_dataset(scan_artifacts, datasets)
        if chosen and chosen in datasets:
            # SubmissionArtifact is frozen, so this replaces rather than
            # mutates. Assigning in place silently did nothing and every file
            # still failed as missing a dataset, with the exception swallowed.
            scan_artifacts = [replace(a, dataset=chosen) for a in scan_artifacts]
            issues.append(_issue(
                "info", DATASET_INFERRED,
                f"No dataset folder was found, so every scan was treated as "
                f"{chosen!r} ({reason}). Counts below are checked against that "
                f"dataset's grid.",
                dataset=chosen, reason=reason))
        elif scan_artifacts:
            issues.append(_issue(
                "warning", DATASET_AMBIGUOUS,
                f"No dataset folder was found and one could not be chosen: "
                f"{reason}. Name the dataset to have the counts checked.",
                reason=reason))

    issues.extend(_unknown_dataset_issues(scan_artifacts, datasets))

    # ── 3. Identity completeness ─────────────────────────────────────────
    incomplete_paths, complete = _identity_issues(scan_artifacts, datasets, issues)

    # ── 4. Dataset structure ─────────────────────────────────────────────
    issues.extend(_dataset_structure_issues(complete, datasets))

    # ── 5. Per-scan required files ───────────────────────────────────────
    issues.extend(_scan_requirement_issues(complete, required_maps, required_artifacts))

    # ── 6. Submission-level artifacts ────────────────────────────────────
    issues.extend(_submission_artifact_issues(artifacts, required_artifacts))

    # ── 7. Dimensionality (present files only) ───────────────────────────
    issues.extend(_dimension_issues(artifacts, required_maps, optional_maps))

    _ = incomplete_paths
    return issues


def validate_generated_map_completeness(
    artifacts: Sequence[Any], *, challenge: str
) -> list[dict]:
    """Check only the configured result-map requirements after execution.

    Execution output is not a complete submission package, so it must not be
    checked for methods documents or participant input artifacts.  This gate
    answers the narrower question needed by the run step: did the process
    produce the maps required before analysis can continue?
    """
    challenge = (challenge or "").strip().lower()
    required_maps = required_maps_by_challenge().get(challenge, ())
    optional_maps = optional_maps_by_challenge().get(challenge, ())
    maps = [a for a in artifacts if a.role == "parameter_map"]
    if not required_maps:
        return _dimension_issues(maps, required_maps, optional_maps)

    issues: list[dict] = []
    identified = [a for a in maps if any(_scan_key(a))]
    if identified:
        issues.extend(_scan_requirement_issues(identified, required_maps, ()))
    else:
        for map_id in required_maps:
            if not any(a.map_type == map_id for a in maps):
                issues.append(_issue(
                    "error", REQUIRED_MAP_MISSING,
                    f"Required {_map_label(map_id)} map is missing from generated outputs.",
                    map_type=map_id,
                ))

    issues.extend(_dimension_issues(maps, required_maps, optional_maps))
    return issues


def _conflict_issues(identity_conflicts: Iterable[Any]) -> list[dict]:
    """Directory/filename disagreement.

    Blocking for DCE: a disagreement can assign a scan to the wrong
    participant, and the resulting statistics would be quietly wrong.
    """
    issues = []
    for conflict in identity_conflicts or ():
        issues.append(_issue(
            "error", IDENTITY_CONFLICT,
            f"Filename identity conflicts with directory identity for "
            f"{conflict.field}: directory says {conflict.directory_value!r}, "
            f"filename says {conflict.filename_value!r}. The directory value "
            f"was used.",
            path=conflict.path,
            field=conflict.field,
            directory_value=conflict.directory_value,
            filename_value=conflict.filename_value,
        ))
    return issues


def _unknown_dataset_issues(scan_artifacts, datasets) -> list[dict]:
    if not datasets:
        return []
    known = set(datasets)
    seen: dict[str, str] = {}
    for artifact in scan_artifacts:
        if artifact.dataset and artifact.dataset not in known:
            seen.setdefault(artifact.dataset, artifact.path)
    return [
        _issue(
            "error", UNKNOWN_DATASET,
            f"Dataset {name!r} is not defined for this challenge. "
            f"Expected one of: {', '.join(sorted(known))}.",
            path=path, dataset=name,
        )
        for name, path in sorted(seen.items())
    ]


#: Reported when a submission carries no dataset folder and one was inferred.
DATASET_INFERRED = "DATASET_INFERRED"
DATASET_AMBIGUOUS = "DATASET_AMBIGUOUS"


def _observed_grid(scan_artifacts) -> dict[str, int]:
    """How many distinct participants, repeats and sites the scans cover."""
    grid: dict[str, int] = {}
    for field, key in (("participant", "participants"),
                       ("repeat", "repeats"),
                       ("site", "sites")):
        values = {getattr(a, field, None) for a in scan_artifacts}
        values.discard(None)
        values.discard("")
        grid[key] = len(values)
    return grid


def _grid_matches(observed: Mapping[str, int], spec: Mapping[str, Any] | None) -> bool:
    """Whether an observed grid fits a declared one.

    A declared count of ``None`` means the organiser has not decided that axis
    yet, so anything satisfies it. That is deliberately not the same as zero.
    """
    if not spec:
        return False
    for key in ("participants", "repeats", "sites"):
        declared = spec.get(key)
        if declared is None:
            continue
        if int(observed.get(key) or 0) != int(declared):
            return False
    return True


def infer_dataset(scan_artifacts, datasets) -> tuple[str | None, str]:
    """Which declared dataset a submission without a dataset folder must be.

    The DCE challenge lead's submission is laid out as participant, site, scan
    with no dataset level at all, so every file failed as missing a dataset
    even though its grid, 3 sites and 2 repeats, matches exactly one of the
    declared datasets and nothing else.

    Inference only happens when the answer is forced: exactly one declared
    dataset fits the observed grid. Two candidates, or none, and it declines
    and says so, because quietly picking one would attribute a submission to
    the wrong dataset and every grouped statistic after it would be wrong.
    """
    if not datasets:
        return None, "the challenge declares no datasets"
    declared = {a.dataset for a in scan_artifacts if getattr(a, "dataset", None)}
    if declared:
        return None, "the submission already names its dataset"

    observed = _observed_grid(scan_artifacts)
    candidates = [name for name, spec in datasets.items()
                  if _grid_matches(observed, spec)]
    if len(candidates) == 1:
        return candidates[0], (
            f"{observed['participants']} participants, {observed['repeats']} "
            f"repeats and {observed['sites']} sites match only {candidates[0]}")
    if not candidates:
        return None, (
            f"{observed['participants']} participants, {observed['repeats']} "
            f"repeats and {observed['sites']} sites match none of "
            f"{', '.join(sorted(datasets))}")
    return None, (f"the grid fits more than one dataset: {', '.join(sorted(candidates))}")


def _identity_issues(scan_artifacts, datasets, issues) -> tuple[set[str], list]:
    """Split artifacts into those with usable identity and those without."""
    if not datasets:
        return set(), list(scan_artifacts)
    incomplete: set[str] = set()
    complete = []
    # Grouped by which fields are missing, because the cause is a missing
    # folder level rather than a problem with each file. A submission laid out
    # without a dataset folder produced one identical error per file: eighteen
    # lines saying the same thing, which buries every other finding and tells
    # the reader nothing the first line did not.
    grouped: dict[tuple[str, ...], list[Any]] = defaultdict(list)
    for artifact in scan_artifacts:
        spec = datasets.get(artifact.dataset or "")
        required = _required_identity_fields(spec)
        missing = tuple(f for f in required if getattr(artifact, f, None) in (None, ""))
        if missing:
            incomplete.add(artifact.path)
            grouped[missing].append(artifact)
        else:
            complete.append(artifact)

    for missing, affected in sorted(grouped.items()):
        paths = [a.path for a in affected]
        fields = ", ".join(missing)
        if len(paths) == 1:
            message = (f"{paths[0]} could not be assigned to a scan because "
                       f"{fields} could not be determined, so completeness "
                       f"cannot be verified.")
        else:
            message = (f"{len(paths)} files could not be assigned to a scan "
                       f"because {fields} could not be determined, so "
                       f"completeness cannot be verified. Every one is missing "
                       f"the same thing, so this is a missing folder level "
                       f"rather than a problem with the files. For example: "
                       f"{paths[0]}.")
        issues.append(_issue(
            "error", INCOMPLETE_ARTIFACT_IDENTITY, message,
            # The first path stays in ``path`` so anything reading one file
            # name still works; the full list is alongside it.
            path=paths[0],
            paths=paths,
            affected_count=len(paths),
            missing_fields=list(missing),
            map_type=affected[0].map_type,
            artifact_type=affected[0].artifact_type,
        ))
    return incomplete, complete


def _dataset_structure_issues(complete, datasets) -> list[dict]:
    """Validate participant, repeat, and site counts.

    Counts unique identifiers rather than requiring consecutive numbering,
    repeats labelled 1 and 3 satisfy a count of 2 unless OSIPI specifies
    otherwise.
    """
    if not datasets:
        return []
    issues: list[dict] = []
    by_dataset: dict[str, list] = defaultdict(list)
    for artifact in complete:
        if artifact.dataset in datasets:
            by_dataset[artifact.dataset].append(artifact)

    for dataset_name, spec in sorted(datasets.items()):
        found = by_dataset.get(dataset_name, [])
        if not found:
            continue
        expected_participants = spec.get("participants")
        expected_repeats = int(spec.get("repeats") or 1)
        expected_sites = int(spec.get("sites") or 1)

        participants = {a.participant for a in found}
        if expected_participants is not None and len(participants) != int(expected_participants):
            issues.append(_issue(
                "error", DATASET_COUNT_MISMATCH,
                f"{dataset_name} expects {expected_participants} participants "
                f"but {len(participants)} were found.",
                dataset=dataset_name, expected=int(expected_participants),
                actual=len(participants), axis="participants",
            ))

        for participant in sorted(participants, key=str):
            rows = [a for a in found if a.participant == participant]
            repeats = {a.repeat for a in rows}
            if len(repeats) != expected_repeats:
                issues.append(_issue(
                    "error", DATASET_COUNT_MISMATCH,
                    f"{dataset_name} participant {participant} has "
                    f"{len(repeats)} repeat(s) but {expected_repeats} were expected.",
                    dataset=dataset_name, participant=participant,
                    expected=expected_repeats, actual=len(repeats), axis="repeats",
                ))
            # A single-site dataset may leave the axis implicit, so only
            # multi-site datasets are checked for site coverage.
            if expected_sites > 1:
                for repeat in sorted(repeats, key=str):
                    sites = {a.site for a in rows if a.repeat == repeat}
                    if len(sites) != expected_sites:
                        issues.append(_issue(
                            "error", DATASET_COUNT_MISMATCH,
                            f"{dataset_name} participant {participant}, repeat "
                            f"{repeat} has {len(sites)} site(s) but "
                            f"{expected_sites} were expected.",
                            dataset=dataset_name, participant=participant,
                            repeat=repeat, expected=expected_sites,
                            actual=len(sites), axis="sites",
                        ))
            else:
                explicit = {a.site for a in rows if a.site is not None}
                if len(explicit) > 1:
                    issues.append(_issue(
                        "error", DATASET_COUNT_MISMATCH,
                        f"{dataset_name} participant {participant} declares "
                        f"{len(explicit)} sites but the dataset is configured "
                        f"for {expected_sites}.",
                        dataset=dataset_name, participant=participant,
                        expected=expected_sites, actual=len(explicit), axis="sites",
                    ))
    return issues


def _scan_requirement_issues(complete, required_maps, required_artifacts) -> list[dict]:
    """Required maps and fitted signals, per observed scan, plus duplicates."""
    issues: list[dict] = []
    if not (required_maps or required_artifacts):
        return issues

    scan_required_artifacts = tuple(
        a for a in required_artifacts
        if (artifact_type_specs().get(a) or {}).get("role") not in _SUBMISSION_LEVEL_ROLES
    )

    scans: dict[tuple, list] = defaultdict(list)
    for artifact in complete:
        scans[_scan_key(artifact)].append(artifact)

    for key in sorted(scans, key=lambda k: tuple(str(v) for v in k)):
        dataset, participant, repeat, site = key
        found = scans[key]
        where = _describe(dataset, participant, repeat, site)

        for map_id in required_maps:
            matching = [a for a in found
                        if a.role == "parameter_map" and a.map_type == map_id]
            if not matching:
                issues.append(_issue(
                    "error", REQUIRED_MAP_MISSING,
                    f"Required {_map_label(map_id)} map is missing for {where}.",
                    dataset=dataset, participant=participant, repeat=repeat,
                    site=site, map_type=map_id,
                ))
            elif len(matching) > 1:
                issues.append(_issue(
                    "error", DUPLICATE_PARAMETER_MAP,
                    f"{len(matching)} {_map_label(map_id)} maps were submitted "
                    f"for {where}; exactly one is expected: "
                    f"{', '.join(a.path for a in matching)}.",
                    dataset=dataset, participant=participant, repeat=repeat,
                    site=site, map_type=map_id,
                    paths=[a.path for a in matching],
                ))

        for artifact_id in scan_required_artifacts:
            matching = [a for a in found if a.artifact_type == artifact_id]
            if not matching:
                issues.append(_issue(
                    "error", REQUIRED_ARTIFACT_MISSING,
                    f"Required {_artifact_label(artifact_id)} is missing for {where}.",
                    dataset=dataset, participant=participant, repeat=repeat,
                    site=site, artifact_type=artifact_id,
                ))
            elif len(matching) > 1:
                issues.append(_issue(
                    "error", DUPLICATE_REQUIRED_ARTIFACT,
                    f"{len(matching)} {_artifact_label(artifact_id)} files were "
                    f"submitted for {where}; exactly one is expected: "
                    f"{', '.join(a.path for a in matching)}.",
                    dataset=dataset, participant=participant, repeat=repeat,
                    site=site, artifact_type=artifact_id,
                    paths=[a.path for a in matching],
                ))
    return issues


def _submission_artifact_issues(artifacts, required_artifacts) -> list[dict]:
    """Artifacts required once per submission, currently the methods document."""
    issues: list[dict] = []
    specs = artifact_type_specs()
    for artifact_id in required_artifacts:
        role = (specs.get(artifact_id) or {}).get("role")
        if role not in _SUBMISSION_LEVEL_ROLES:
            continue
        matching = [a for a in artifacts if a.artifact_type == artifact_id]
        if not matching:
            issues.append(_issue(
                "error", REQUIRED_ARTIFACT_MISSING,
                f"A {_artifact_label(artifact_id)} is required but was not found "
                f"in the submission.",
                artifact_type=artifact_id,
            ))
        elif len(matching) > 1:
            # Warning, not blocking: a later phase may pick one
            # deterministically, but the duplication must stay visible.
            issues.append(_issue(
                "warning", DUPLICATE_METHODS_DOCUMENT,
                f"{len(matching)} {_artifact_label(artifact_id)} files were "
                f"submitted: {', '.join(a.path for a in matching)}.",
                artifact_type=artifact_id,
                paths=[a.path for a in matching],
            ))
    return issues


def _dimension_issues(artifacts, required_maps, optional_maps) -> list[dict]:
    """Enforce configured dimensionality on files that are actually present.

    ``dimensions is None`` means the header could not be read. That is
    already reported by the NIfTI validator, so emitting a dimension
    mismatch here as well would give two errors for one root cause.
    """
    issues: list[dict] = []
    map_specs = map_type_specs()
    artifact_specs = artifact_type_specs()
    known_maps = set(required_maps) | set(optional_maps)

    for artifact in artifacts:
        if artifact.dimensions is None:
            continue
        expected = None
        label = None
        context: dict[str, Any] = {}
        if artifact.role == "parameter_map" and artifact.map_type in known_maps:
            expected = (map_specs.get(artifact.map_type) or {}).get("dimensions")
            label = _map_label(artifact.map_type)
            context["map_type"] = artifact.map_type
            code = MAP_DIMENSION_MISMATCH
        elif artifact.artifact_type:
            expected = (artifact_specs.get(artifact.artifact_type) or {}).get("dimensions")
            label = _artifact_label(artifact.artifact_type)
            context["artifact_type"] = artifact.artifact_type
            code = ARTIFACT_DIMENSION_MISMATCH
        if expected is None or label is None:
            continue
        if int(artifact.dimensions) != int(expected):
            where = _describe(artifact.dataset, artifact.participant,
                              artifact.repeat, artifact.site)
            issues.append(_issue(
                "error", code,
                f"{label} must be a {expected}D NIfTI, but a "
                f"{artifact.dimensions}D file was submitted for {where}.",
                path=artifact.path,
                dataset=artifact.dataset, participant=artifact.participant,
                repeat=artifact.repeat, site=artifact.site,
                expected=int(expected), actual=int(artifact.dimensions),
                **context,
            ))
    return issues


def suppressed_legacy_map_ids(challenge: str) -> frozenset[str]:
    """Map ids whose legacy EXPECTED_MAP_MISSING warning must be suppressed.

    A challenge that has migrated to ``required_maps``/``optional_maps`` gets
    precise errors from this module, so the legacy warning would either
    duplicate the error or contradict it by warning about an optional map.
    Challenges that have not migrated return an empty set and keep their
    existing behaviour.
    """
    challenge = (challenge or "").strip().lower()
    required = set(required_maps_by_challenge().get(challenge, ()))
    optional = set(optional_maps_by_challenge().get(challenge, ()))
    if not (required or optional):
        return frozenset()
    return frozenset(required | optional)
