"""Challenge type keywords used by ingestion."""

from __future__ import annotations

from osipi_pipeline.config.rules import challenge_keyword_config


CHALLENGE_TYPES: dict[str, dict[str, tuple[str, ...]]] = challenge_keyword_config()
