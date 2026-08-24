"""Detect the configured challenge type from submission names and map evidence."""

from __future__ import annotations

import re
from pathlib import Path

from osipi_pipeline.config.rules import (
    challenge_keyword_config,
    expected_maps_by_challenge,
)
from osipi_pipeline.ingestion.artifact_classifier import detect_map_type, is_nifti_name


def detect_challenge_type(
    path: Path,
    challenge_config: dict[str, dict[str, tuple[str, ...]]] | None = None,
) -> str:
    """Return the best matching challenge type for a submission folder.

    Folder/README words are useful hints, but real uploads commonly use a
    neutral team name. Recognized NIfTI map types therefore provide stronger
    evidence. A shared map such as CBF cannot choose between ASL and DSC by
    itself; tied evidence returns ``unknown`` so the reviewer can decide.
    """

    challenge_config = challenge_config or challenge_keyword_config()
    haystack = _path_text(path)
    expected = expected_maps_by_challenge()
    submitted_maps = _submitted_map_types(path)

    # Map matches are weighted above textual hints. This lets Perfmap + ATTmap
    # identify ASL even when the enclosing folder has a generic team name.
    scores: dict[str, int] = {}
    for challenge_type, config in challenge_config.items():
        keyword_score = sum(
            1 for keyword in config.get("keywords", ()) if _keyword_matches(haystack, keyword)
        )
        configured_maps = {
            str(item).lower()
            for item in (config.get("expected_maps") or expected.get(challenge_type, ()))
        }
        map_score = len(submitted_maps.intersection(configured_maps))
        scores[challenge_type] = keyword_score + (2 * map_score)

    if not scores:
        return "unknown"
    best_score = max(scores.values())
    if best_score <= 0:
        return "unknown"
    winners = [challenge for challenge, score in scores.items() if score == best_score]
    return winners[0] if len(winners) == 1 else "unknown"


def _submitted_map_types(path: Path) -> set[str]:
    """Configured map ids recognized from submitted NIfTI filenames."""

    if not path.exists() or not path.is_dir():
        return set()
    found: set[str] = set()
    for child in path.rglob("*"):
        if not child.is_file() or not is_nifti_name(child.name):
            continue
        map_type = detect_map_type(child.name)
        if map_type:
            found.add(map_type)
    return found


def _path_text(path: Path) -> str:
    """Join the folder name and child paths into searchable text."""

    parts: list[str] = [path.name]
    if path.exists() and path.is_dir():
        parts.extend(str(child.relative_to(path)) for child in path.rglob("*"))
    return _normalize(" ".join(parts))

def _keyword_matches(haystack: str, keyword: str) -> bool:
    """Check whether a keyword appears in the normalized path text."""

    normalized_keyword = _normalize(keyword)
    # Short terms like "vp" should match as their own word, not inside another
    # word. Longer phrases can use a simpler substring check.
    if len(normalized_keyword) <= 3 and " " not in normalized_keyword:
        return re.search(rf"(^|[^a-z0-9]){re.escape(normalized_keyword)}([^a-z0-9]|$)", haystack) is not None
    return normalized_keyword in haystack

def _normalize(value: str) -> str:
    """Lowercase text and turn punctuation into spaces for matching."""

    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()
