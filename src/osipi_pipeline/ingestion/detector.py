"""Detect the configured challenge type from submission file and folder names."""

from __future__ import annotations

import re
from pathlib import Path

from osipi_pipeline.config.challenge_types import CHALLENGE_TYPES

def detect_challenge_type(path: Path, challenge_config: dict[str, dict[str, tuple[str, ...]]] | None = None) -> str:
    """Return the best matching challenge type for a submission folder."""

    challenge_config = challenge_config or CHALLENGE_TYPES
    haystack = _path_text(path)

    # Count how many configured keywords each challenge type matches.
    scores: dict[str, int] = {}
    for challenge_type, config in challenge_config.items():
        scores[challenge_type] = sum(
            1 for keyword in config.get("keywords", ()) if _keyword_matches(haystack, keyword)
        )

    best_type, best_score = max(scores.items(), key=lambda item: item[1], default=("unknown", 0))
    return best_type if best_score > 0 else "unknown"

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
