"""Every map a challenge requires must declare the dimensionality it expects."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

pytest.importorskip("yaml")

from osipi_pipeline.config.rules import (  # noqa: E402
    map_type_specs,
    optional_maps_by_challenge,
    required_maps_by_challenge,
)

VALID_DIMENSIONS = (2, 3, 4, 5, 6, 7)


def test_there_are_challenges_with_required_maps() -> None:
    required = required_maps_by_challenge()
    assert required, "no challenges are configured"
    assert any(maps for maps in required.values()), "no challenge requires a map"


@pytest.mark.parametrize("challenge", sorted(required_maps_by_challenge()))
def test_every_required_map_declares_its_dimensions(challenge: str) -> None:
    specs = map_type_specs()
    undeclared = sorted(
        map_id for map_id in required_maps_by_challenge()[challenge]
        if (specs.get(map_id) or {}).get("dimensions") is None
    )
    assert not undeclared, (
        f"challenge {challenge!r} requires {undeclared} but declares no "
        "`dimensions` for them, so a file of any shape passes the dimension "
        "check while its sibling required maps are validated"
    )


@pytest.mark.parametrize("challenge", sorted(required_maps_by_challenge()))
def test_a_challenge_does_not_check_some_of_its_maps_and_not_others(
    challenge: str,
) -> None:
    specs = map_type_specs()
    configured = [
        *required_maps_by_challenge()[challenge],
        *optional_maps_by_challenge().get(challenge, ()),
    ]
    declared = {
        map_id: (specs.get(map_id) or {}).get("dimensions") is not None
        for map_id in configured
    }
    if not declared:
        pytest.skip(f"{challenge} configures no maps")
    assert len(set(declared.values())) == 1, (
        f"challenge {challenge!r} declares dimensions for some maps but not "
        f"others: {declared}"
    )


def test_declared_dimensions_are_plausible_for_a_parameter_map() -> None:
    for map_id, spec in sorted(map_type_specs().items()):
        dimensions = spec.get("dimensions")
        if dimensions is None:
            continue
        assert dimensions in VALID_DIMENSIONS, (
            f"map {map_id!r} declares dimensions={dimensions!r}"
        )
