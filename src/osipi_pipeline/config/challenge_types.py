"""Challenge type keywords used by ingestion.

Add future challenge types here instead of spreading detection rules throughout
the ingestion code.
"""

# TODO: This file keeps challenge keyword rules in one place.
# TODO: Later, add DSC or other challenge types without changing ingestion logic.
# TODO: These settings help ingestion organize submissions before validation and scoring.

from __future__ import annotations

# Each challenge type has keywords that may appear in submitted file or folder
# names. The detector uses these words to make a simple best guess.
CHALLENGE_TYPES: dict[str, dict[str, tuple[str, ...]]] = {
    "dce": {
        "keywords": (
            "ktrans",
            "kep",
            "vp",
            "dce",
            "dynamic contrast enhanced",
        )
    },
    "asl": {
        "keywords": (
            "cbf",
            "att",
            "asl",
            "arterial spin labeling",
        )
    },
}
