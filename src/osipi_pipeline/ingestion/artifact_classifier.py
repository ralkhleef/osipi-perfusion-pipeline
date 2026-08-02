"""Classify a submitted file into a normalized role and map type.

Matching is **token-based, not substring-based**. The legacy detector in
``manifest.py`` (``_detect_parameter_map_id``) asks whether a configured
pattern appears anywhere in the filename, which means ``curve.nii.gz``
matches the ``ve`` pattern and ``developer.nii.gz`` matches it twice. That
behaviour is left untouched for backward compatibility, but it is not
reproduced here: this classifier splits the filename on separators and
requires a whole-token match.

Where several map types match, the longest pattern wins, so a file named
``ktrans_ve.nii.gz`` resolves deterministically rather than by dict order.
"""

from __future__ import annotations

import re
from functools import lru_cache

from osipi_pipeline.config.rules import (
    artifact_type_specs,
    map_type_patterns,
    tuple_setting,
)
from osipi_pipeline.ingestion.identity_parser import strip_nifti_suffix
from osipi_pipeline.ingestion.models import (
    ROLE_CODE,
    ROLE_METADATA,
    ROLE_PARAMETER_MAP,
    ROLE_README,
    ROLE_UNKNOWN,
)

# Filenames are split on anything that is not a letter or digit, then digit
# runs are split off so "sub001" yields ("sub", "001") and "Ktrans" yields
# ("ktrans",).
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_ALPHA_NUM_RE = re.compile(r"[a-z]+|[0-9]+")


def _tokens(stem: str) -> frozenset[str]:
    """Whole-word tokens of a filename stem, lowercased."""
    parts: list[str] = []
    for chunk in _SPLIT_RE.split(stem.lower()):
        if not chunk:
            continue
        parts.append(chunk)
        # "ktrans2" also yields "ktrans"; "curve" yields only "curve".
        pieces = _ALPHA_NUM_RE.findall(chunk)
        if len(pieces) > 1:
            parts.extend(pieces)
    return frozenset(parts)


@lru_cache(maxsize=1)
def _map_pattern_index() -> tuple[tuple[str, str], ...]:
    """(pattern, map_id) pairs, longest pattern first.

    Patterns are normalized the same way filenames are, so a configured
    ``k-trans`` matches a file named ``k_trans``.
    """
    index: list[tuple[str, str]] = []
    for map_id, patterns in map_type_patterns().items():
        for pattern in patterns:
            normalized = "".join(_SPLIT_RE.split(str(pattern).lower()))
            if normalized:
                index.append((normalized, map_id))
    index.sort(key=lambda item: (-len(item[0]), item[0]))
    return tuple(index)


@lru_cache(maxsize=1)
def _artifact_index() -> tuple[tuple[str, str, str], ...]:
    """(pattern, artifact_id, role) triples, longest pattern first."""
    index: list[tuple[str, str, str]] = []
    for artifact_id, spec in artifact_type_specs().items():
        role = str(spec.get("role") or artifact_id)
        for pattern in spec.get("patterns") or ():
            normalized = "".join(_SPLIT_RE.split(str(pattern).lower()))
            if normalized:
                index.append((normalized, artifact_id, role))
    index.sort(key=lambda item: (-len(item[0]), item[0]))
    return tuple(index)


def _collapsed(stem: str) -> str:
    """Separator-free form, so 'modelled_st' matches 'modelled-st'."""
    return "".join(_SPLIT_RE.split(stem.lower()))


def detect_map_type(filename: str) -> str | None:
    """Map-type id for a filename, or ``None``.

    Requires a whole-token match: ``curve.nii.gz`` does not match ``ve``.
    """
    stem = strip_nifti_suffix(filename)
    tokens = _tokens(stem)
    collapsed = _collapsed(stem)
    for pattern, map_id in _map_pattern_index():
        # A single token equal to the pattern, or the whole stem collapsing
        # to it (covers "K_trans" -> "ktrans").
        if pattern in tokens or collapsed == pattern:
            return map_id
    return None


def detect_artifact_type(filename: str) -> tuple[str, str] | None:
    """``(artifact_id, role)`` for a configured non-map artifact, or ``None``.

    Both the pattern *and* one of the artifact's configured suffixes must
    match, so an arbitrary ``notes.txt`` never becomes a methods document.
    """
    lowered = filename.lower()
    stem = strip_nifti_suffix(filename)
    tokens = _tokens(stem)
    collapsed = _collapsed(stem)
    specs = artifact_type_specs()
    for pattern, artifact_id, role in _artifact_index():
        if not (pattern in tokens or collapsed == pattern):
            continue
        suffixes = tuple(
            str(s).lower() for s in (specs.get(artifact_id, {}).get("suffixes") or ())
        )
        if suffixes and not lowered.endswith(suffixes):
            continue
        return artifact_id, role
    return None


def classify(
    filename: str,
    *,
    is_nifti: bool = False,
    is_readme: bool = False,
    is_metadata: bool = False,
    is_code: bool = False,
) -> tuple[str, str | None, str | None]:
    """Return ``(role, map_type, artifact_type)`` for one file.

    Configured artifacts are checked before the legacy categories so a
    ``methods.txt`` is a methods document rather than generic metadata, but
    README detection keeps priority — existing README behaviour is unchanged.
    """
    if is_readme:
        return ROLE_README, None, None

    artifact = detect_artifact_type(filename)
    if artifact is not None:
        artifact_id, role = artifact
        return role, None, artifact_id

    if is_nifti:
        map_type = detect_map_type(filename)
        if map_type is not None:
            return ROLE_PARAMETER_MAP, map_type, None
        return ROLE_UNKNOWN, None, None

    if is_metadata:
        return ROLE_METADATA, None, None
    if is_code:
        return ROLE_CODE, None, None
    return ROLE_UNKNOWN, None, None


def is_nifti_name(filename: str) -> bool:
    """Whether a filename carries a configured NIfTI suffix."""
    return filename.lower().endswith(tuple(tuple_setting("nifti_suffixes")))


def clear_classifier_caches() -> None:
    """Drop cached pattern indexes after a configuration change."""
    _map_pattern_index.cache_clear()
    _artifact_index.cache_clear()
