"""Challenge type keywords used by ingestion.

Add future challenge types here instead of spreading detection rules throughout
the ingestion code. The detector uses these keywords to make a best guess from
file and folder names before validation runs.
"""

from __future__ import annotations

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
    "dsc": {
        "keywords": (
            "cbv",
            "mtt",
            "dsc",
            "dynamic susceptibility contrast",
        )
    },
}
