"""Canonical analysis provenance shared by stored JSON and reports."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from services.configuration_manager_service import active_configuration_version
from services.scoring_package_service import (
    compatible_builtin_providers,
    get_active_entry,
)
from osipi_pipeline.config.rules import challenge_labels, reference_dataset_versions


def pipeline_version() -> str:
    try:
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
        return match.group(1) if match else "unknown"
    except OSError:
        return "unknown"


def _scoring_label(challenge: str) -> str:
    active = get_active_entry(challenge)
    mode = active.get("mode") or "none"
    if mode == "custom":
        name = active.get("package_name") or active.get("package_id") or "custom package"
        version = active.get("package_version")
        return f"{name} v{version}" if version else str(name)
    if mode == "builtin":
        providers = compatible_builtin_providers(challenge)
        if len(providers) == 1:
            provider = providers[0]
            return str(
                provider.get("display_name")
                or provider.get("provider_name")
                or provider.get("provider_id")
                or "built-in provider"
            )
        return "not configured for this challenge"
    return "not configured"


def analysis_provenance(
    challenges: str | Iterable[str], *, generated: datetime | None = None
) -> dict[str, str]:
    if isinstance(challenges, str):
        ids = [challenges]
    else:
        ids = list(challenges)
    ids = sorted({str(item or "").strip().lower() for item in ids if str(item or "").strip()})
    labels = challenge_labels()
    ref_versions = reference_dataset_versions()
    generated = generated or datetime.now(timezone.utc)

    def joined(get_value) -> str:
        values = [(labels.get(ch, ch.upper()), get_value(ch)) for ch in ids]
        if not values:
            return "not available"
        if len(values) == 1:
            return str(values[0][1])
        return "; ".join(f"{label}: {value}" for label, value in values)

    return {
        "challenge": ", ".join(labels.get(ch, ch.upper()) for ch in ids) or "not available",
        "challenge_configuration": joined(active_configuration_version),
        "scoring_package": joined(_scoring_label),
        "pipeline_version": pipeline_version(),
        "reference_dataset": joined(lambda ch: ref_versions.get(ch) or "not versioned/configured"),
        "analysis_date": generated.strftime("%Y-%m-%d"),
    }
