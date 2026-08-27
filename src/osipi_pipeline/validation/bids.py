"""Structural BIDS checks for a submission folder.

The proposal named BIDS twice, and until now the only mention of it in the
code was a disclaimer saying that NIfTI QC is not BIDS validation. That
disclaimer was accurate, which is the problem.

This is deliberately a *subset*, and the documentation says so. Full BIDS
validation covers hundreds of rules across every modality and needs the
schema to do it properly; reimplementing that here would be a worse version
of a tool that already exists. What is checked instead is the part a
challenge organiser actually relies on, the layout and naming that let a
reader work out which subject, session and run a file belongs to:

  - ``dataset_description.json`` exists, parses, and declares Name and
    BIDSVersion
  - subject directories are ``sub-<label>`` with an alphanumeric label
  - session directories, where present, are ``ses-<label>``
  - data filenames are made of ``key-value`` entities plus a suffix
  - entity keys are ones BIDS defines
  - entities appear in the order BIDS fixes them in
  - the ``sub`` entity in a filename matches the directory holding it, and
    likewise for ``ses``

Anything outside that is not reported, so a submission passing these checks
must not be described as BIDS valid. It is described as structurally
consistent, which is what was measured.

Nothing here runs unless a challenge asks for it. See ``bids_validation`` in
``config/validation_rules.yaml``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from osipi_pipeline.validation.models import ValidationIssue

#: The order BIDS fixes for filename entities. A file may use any subset, but
#: the ones it does use have to appear in this sequence, which is what makes a
#: name mechanically parseable rather than merely readable.
ENTITY_ORDER: tuple[str, ...] = (
    "sub", "ses", "task", "acq", "ce", "trc", "stain", "rec", "dir",
    "run", "mod", "echo", "flip", "inv", "mt", "part", "proc", "hemi",
    "space", "split", "recording", "chunk", "seg", "res", "den", "label",
    "desc",
)

_ENTITY_INDEX = {name: position for position, name in enumerate(ENTITY_ORDER)}

#: A label is alphanumeric. BIDS forbids the separators it uses structurally,
#: so a hyphen or underscore inside a label makes the name ambiguous.
_LABEL = re.compile(r"^[A-Za-z0-9]+$")
_ENTITY_PAIR = re.compile(r"^([A-Za-z]+)-([A-Za-z0-9]+)$")

_DATA_SUFFIXES = (".nii", ".nii.gz", ".json", ".tsv")


def _issue(severity: str, code: str, message: str, path: Path | None = None) -> ValidationIssue:
    return ValidationIssue(
        severity=severity, code=code, message=message,
        path=str(path) if path is not None else None,
    )


def _strip_extension(name: str) -> tuple[str, str]:
    """Split a filename into its stem and extension, honouring .nii.gz."""
    for suffix in (".nii.gz", ".nii", ".json", ".tsv", ".tsv.gz"):
        if name.endswith(suffix):
            return name[: -len(suffix)], suffix
    return Path(name).stem, Path(name).suffix


def parse_entities(stem: str) -> tuple[dict[str, str], str | None, list[str]]:
    """Entities, suffix and any malformed parts of a BIDS filename stem.

    ``sub-01_ses-1_run-2_cbf`` yields ``{"sub": "01", "ses": "1", "run": "2"}``
    with the suffix ``cbf``. Parts that are not ``key-value`` are returned
    rather than dropped, because silently ignoring them is how an unnoticed
    typo becomes an unnoticed missing file.
    """
    entities: dict[str, str] = {}
    malformed: list[str] = []
    suffix: str | None = None
    parts = stem.split("_")
    for index, part in enumerate(parts):
        match = _ENTITY_PAIR.match(part)
        if match:
            entities[match.group(1)] = match.group(2)
            continue
        # The trailing part with no hyphen is the suffix, for example cbf.
        if index == len(parts) - 1 and part and "-" not in part:
            suffix = part
            continue
        malformed.append(part)
    return entities, suffix, malformed


def _check_dataset_description(root: Path, severity: str) -> list[ValidationIssue]:
    description = root / "dataset_description.json"
    if not description.exists():
        return [_issue(severity, "BIDS_DATASET_DESCRIPTION_MISSING",
                       "A BIDS dataset needs dataset_description.json at its root.",
                       root)]
    try:
        data = json.loads(description.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [_issue(severity, "BIDS_DATASET_DESCRIPTION_INVALID",
                       f"dataset_description.json could not be read as JSON: {exc}",
                       description)]
    if not isinstance(data, dict):
        return [_issue(severity, "BIDS_DATASET_DESCRIPTION_INVALID",
                       "dataset_description.json must contain a JSON object.",
                       description)]
    missing = [key for key in ("Name", "BIDSVersion") if not str(data.get(key) or "").strip()]
    if missing:
        return [_issue(severity, "BIDS_DATASET_DESCRIPTION_INCOMPLETE",
                       f"dataset_description.json is missing {' and '.join(missing)}.",
                       description)]
    return []


def _check_directory_names(root: Path, severity: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.name.startswith("."):
            continue
        # derivatives/, code/ and sourcedata/ are BIDS directories in their own
        # right and are not subjects, so they are left alone here.
        if entry.name in {"derivatives", "code", "sourcedata", "stimuli", "phenotype"}:
            continue
        if not entry.name.startswith("sub-"):
            issues.append(_issue(
                severity, "BIDS_UNEXPECTED_TOP_LEVEL_DIRECTORY",
                f"{entry.name!r} is not a subject directory. BIDS expects sub-<label>.",
                entry))
            continue
        if not _LABEL.match(entry.name[len("sub-"):]):
            issues.append(_issue(
                severity, "BIDS_INVALID_LABEL",
                f"Subject label in {entry.name!r} must be alphanumeric.", entry))
            continue
        for child in sorted(entry.iterdir()):
            if not child.is_dir() or not child.name.startswith("ses-"):
                continue
            if not _LABEL.match(child.name[len("ses-"):]):
                issues.append(_issue(
                    severity, "BIDS_INVALID_LABEL",
                    f"Session label in {child.name!r} must be alphanumeric.", child))
    return issues


def _directory_entity(path: Path, root: Path, prefix: str) -> str | None:
    """The sub- or ses- label taken from the directories above a file."""
    for parent in path.relative_to(root).parts[:-1]:
        if parent.startswith(prefix):
            return parent[len(prefix):]
    return None


def _check_file(path: Path, root: Path, severity: str) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    stem, _extension = _strip_extension(path.name)
    entities, suffix, malformed = parse_entities(stem)

    if malformed:
        issues.append(_issue(
            severity, "BIDS_FILENAME_NOT_PARSEABLE",
            f"{path.name!r} contains {', '.join(repr(p) for p in malformed)}, "
            f"which is neither a key-value entity nor a trailing suffix.", path))

    if suffix is None:
        issues.append(_issue(
            severity, "BIDS_SUFFIX_MISSING",
            f"{path.name!r} has no suffix. A BIDS filename ends with one, "
            f"such as _cbf or _bold.", path))

    unknown = [key for key in entities if key not in _ENTITY_INDEX]
    if unknown:
        issues.append(_issue(
            severity, "BIDS_UNKNOWN_ENTITY",
            f"{path.name!r} uses {', '.join(sorted(unknown))}, which BIDS does "
            f"not define as an entity.", path))

    known = [key for key in entities if key in _ENTITY_INDEX]
    positions = [_ENTITY_INDEX[key] for key in known]
    if positions != sorted(positions):
        expected = [key for key in ENTITY_ORDER if key in entities]
        issues.append(_issue(
            severity, "BIDS_ENTITY_ORDER",
            f"{path.name!r} orders its entities {'_'.join(known)}; BIDS fixes "
            f"the order as {'_'.join(expected)}.", path))

    for prefix, key in (("sub-", "sub"), ("ses-", "ses")):
        from_directory = _directory_entity(path, root, prefix)
        from_name = entities.get(key)
        if from_directory and from_name and from_directory != from_name:
            issues.append(_issue(
                severity, "BIDS_ENTITY_DIRECTORY_MISMATCH",
                f"{path.name!r} says {key}-{from_name} but sits under "
                f"{prefix}{from_directory}.", path))
    return issues


def validate_bids_structure(root: str | Path, *, severity: str = "warning") -> list[ValidationIssue]:
    """Check the BIDS layout and naming of ``root``.

    ``severity`` decides whether findings are advisory or blocking, so a
    challenge can adopt BIDS gradually rather than rejecting every submission
    on the day it is switched on.
    """
    path = Path(root)
    if not path.is_dir():
        return []
    severity = "error" if str(severity).lower() == "error" else "warning"

    issues = _check_dataset_description(path, severity)
    issues += _check_directory_names(path, severity)

    for file_path in sorted(path.rglob("*")):
        if not file_path.is_file() or file_path.name.startswith("."):
            continue
        if file_path.parent == path:
            continue  # root-level files such as README or the description
        if not file_path.name.endswith(_DATA_SUFFIXES):
            continue
        issues += _check_file(file_path, path, severity)
    return issues


def looks_like_bids(root: str | Path) -> bool:
    """Whether a folder is plausibly a BIDS dataset.

    Used so that a challenge can enable the checks without them firing on
    every submission that simply does not use BIDS: a folder with neither a
    description nor a subject directory is not an invalid BIDS dataset, it is
    not one at all, and saying otherwise would be noise.
    """
    path = Path(root)
    if not path.is_dir():
        return False
    if (path / "dataset_description.json").exists():
        return True
    return any(child.is_dir() and child.name.startswith("sub-")
               for child in path.iterdir())
