"""Resolve dataset / participant / repeat / site identity for a submitted file.

Precedence is deliberate and fixed:

1. **Directory structure.** A path such as
   ``Synthetic/Participant001/Site1/Repeat1/Ktrans.nii.gz`` states the
   identity unambiguously, and directories are far less prone to collision
   than filename substrings.
2. **Configured filename patterns.** Applied to the filename stem (with
   ``.nii``/``.nii.gz`` removed) only for fields the directory did not
   supply. Patterns come from ``challenges.<id>.filename_identity_patterns``;
   none are hardcoded here.
3. **Unresolved.** Fields stay ``None``. Identity is never inferred from
   file ordering, sibling files, or position in a listing.

Where both sources supply a field and disagree, the directory wins and the
disagreement is reported as an :class:`IdentityConflict` rather than being
silently resolved.

Directory matching is conservative on purpose. A folder is only classified
when it matches a known prefix followed by an identifier, ``patient-004``
is a participant, ``processed`` is not, even though both begin with "p".
"""

from __future__ import annotations

import re
from functools import lru_cache

from osipi_pipeline.config.rules import (
    datasets_by_challenge,
    filename_identity_patterns_by_challenge,
)
from osipi_pipeline.ingestion.models import IdentityConflict

# Prefix vocabularies. Each entry must be followed by a separator and/or an
# identifier, bare "processed" or "session" never classifies.
_PARTICIPANT_PREFIXES = (
    "participant", "subject", "patient", "sub", "subj", "pat", "p",
)
_REPEAT_PREFIXES = (
    "repeat", "visit", "session", "scan", "ses", "rep", "acq",
)
_SITE_PREFIXES = (
    "site", "center", "centre", "scanner", "institution",
)
# Repeat labels that are words rather than numbers. Kept as normalized
# strings: "retest" is not a number and must not be coerced into one.
_REPEAT_WORDS = ("baseline", "followup", "follow-up", "retest", "test")

# <prefix><optional separator><identifier>. The identifier may be numeric
# (001) or alphanumeric (a1), because participant labels may gain letters.
_TOKEN_RE = re.compile(r"^(?P<prefix>[a-z]+)[-_ ]?(?P<value>[0-9]+[a-z0-9]*|[a-z]?[0-9]+)$")

_NII_SUFFIXES = (".nii.gz", ".nii")


def _normalize_number(value: str) -> str:
    """Strip leading zeros but keep the value a string.

    ``001`` and ``1`` are the same participant; ``P01A`` is left intact.
    """
    digits = value.lstrip("0")
    if value.isdigit():
        return digits or "0"
    return value


def _match_prefixed(token: str, prefixes: tuple[str, ...]) -> str | None:
    """Return the normalized identifier if ``token`` uses one of ``prefixes``."""
    match = _TOKEN_RE.match(token)
    if not match:
        return None
    prefix = match.group("prefix")
    if prefix not in prefixes:
        return None
    return _normalize_number(match.group("value"))


@lru_cache(maxsize=32)
def _dataset_names(challenge: str | None) -> tuple[str, ...]:
    """Configured dataset ids for a challenge, or all known ids.

    Dataset names are not hardcoded to synthetic/clinical: a challenge that
    declares its own dataset names has them recognised automatically.
    """
    datasets = datasets_by_challenge()
    if challenge and challenge in datasets:
        return tuple(datasets[challenge])
    return tuple({name for spec in datasets.values() for name in spec})


@lru_cache(maxsize=32)
def _compiled_patterns(challenge: str | None) -> tuple[re.Pattern[str], ...]:
    """Compile the challenge's identity patterns once.

    Validity is already guaranteed by config validation, so compilation
    cannot fail here; the cache keeps a large submission from recompiling
    the same expressions per file.
    """
    if not challenge:
        return ()
    return tuple(
        re.compile(pattern)
        for pattern in filename_identity_patterns_by_challenge().get(challenge, ())
    )


def strip_nifti_suffix(name: str) -> str:
    """Filename stem with .nii/.nii.gz removed, other suffixes preserved."""
    lowered = name.lower()
    for suffix in _NII_SUFFIXES:
        if lowered.endswith(suffix):
            return name[: -len(suffix)]
    return name


def parse_directory_identity(
    relative_parts: tuple[str, ...], *, challenge: str | None = None
) -> dict[str, str]:
    """Identity implied by the directories containing a file.

    ``relative_parts`` is the path relative to the submission root, including
    the filename; only the directory components are inspected. Unrecognised
    directories are skipped rather than guessed at.
    """
    known_datasets = {name.lower() for name in _dataset_names(challenge)}
    found: dict[str, str] = {}
    for raw in relative_parts[:-1]:
        token = raw.strip().lower()
        if not token:
            continue
        if "dataset" not in found and token in known_datasets:
            found["dataset"] = token
            continue
        if "repeat" not in found and token in _REPEAT_WORDS:
            found["repeat"] = token
            continue
        for field, prefixes in (
            ("participant", _PARTICIPANT_PREFIXES),
            ("repeat", _REPEAT_PREFIXES),
            ("site", _SITE_PREFIXES),
        ):
            if field in found:
                continue
            value = _match_prefixed(token, prefixes)
            if value is not None:
                found[field] = value
                break
    return found


def parse_filename_identity(
    filename: str, *, challenge: str | None = None
) -> dict[str, str]:
    """Identity from the configured filename patterns; first match wins."""
    stem = strip_nifti_suffix(filename)
    for pattern in _compiled_patterns(challenge):
        match = pattern.match(stem)
        if not match:
            continue
        groups = {k: v for k, v in match.groupdict().items() if v is not None}
        if not groups:
            continue
        result = {}
        for key, value in groups.items():
            result[key] = (
                value.lower() if key == "dataset" else _normalize_number(value)
            )
        return result
    return {}


def resolve_identity(
    relative_path: str, *, challenge: str | None = None
) -> tuple[dict[str, str | None], list[IdentityConflict]]:
    """Combine directory and filename identity under the documented precedence.

    Returns the resolved fields (missing ones as ``None``) and any conflicts
    where the two sources disagreed.
    """
    parts = tuple(part for part in relative_path.split("/") if part)
    if not parts:
        return {"dataset": None, "participant": None, "repeat": None, "site": None}, []

    from_dir = parse_directory_identity(parts, challenge=challenge)
    from_name = parse_filename_identity(parts[-1], challenge=challenge)

    resolved: dict[str, str | None] = {}
    conflicts: list[IdentityConflict] = []
    for field in ("dataset", "participant", "repeat", "site"):
        dir_value = from_dir.get(field)
        name_value = from_name.get(field)
        if dir_value is not None and name_value is not None and dir_value != name_value:
            conflicts.append(IdentityConflict(
                path=relative_path,
                field=field,
                directory_value=dir_value,
                filename_value=name_value,
            ))
        # Directory wins; the filename only fills gaps.
        resolved[field] = dir_value if dir_value is not None else name_value
    return resolved, conflicts


def clear_identity_caches() -> None:
    """Drop cached datasets and compiled patterns after a config change."""
    _dataset_names.cache_clear()
    _compiled_patterns.cache_clear()
