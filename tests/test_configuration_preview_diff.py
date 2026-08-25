"""What Preview Changes reports, and at what granularity.

The comparison recursed into dictionaries but treated lists as opaque values.
``maps`` is a list, so changing one map's state produced a single row holding
both entire lists serialized as JSON, and a reviewer had to read a wall of
objects to work out that one map had moved from unused to required.

Lists whose entries carry an ``id`` are matched up by that id instead, so the
report says ``maps.ktrans.state`` and nothing else. Order is not identity in
these lists, and the tests below pin that: reordering is not a change.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "backend")]

from services.configuration_manager_service import (  # noqa: E402
    _change_rows,
    _identified,
)


def maps(*states):
    """Map entries shaped like the ones the manager actually sends."""
    return [
        {"id": map_id, "display": map_id.upper(), "state": state,
         "dimensions": 3, "aliases": [map_id]}
        for map_id, state in states
    ]


def fields(rows):
    return [row["field"] for row in rows]


# ── Recognising a list that has identity ──────────────────────────────────

def test_a_list_of_identified_objects_is_recognised() -> None:
    assert list(_identified([{"id": "a"}, {"id": "b"}]) or {}) == ["a", "b"]


def test_a_plain_list_is_not_treated_as_identified() -> None:
    """Aliases are a list of strings and must still compare as a whole."""
    assert _identified(["cbf", "perfusion"]) is None
    assert _identified([1, 2, 3]) is None


def test_objects_without_an_id_are_not_matched_up() -> None:
    assert _identified([{"name": "a"}]) is None


def test_duplicate_ids_fall_back_rather_than_dropping_entries() -> None:
    """Keying by a duplicated id would silently lose one of them."""
    assert _identified([{"id": "a"}, {"id": "a"}]) is None


def test_an_empty_list_is_not_identified() -> None:
    assert _identified([]) is None


# ── One row per setting ───────────────────────────────────────────────────

def test_one_changed_map_produces_one_row() -> None:
    before = maps(("cbf", "required"), ("ktrans", "unused"))
    after = maps(("cbf", "required"), ("ktrans", "required"))
    rows = _change_rows({"maps": before}, {"maps": after})
    assert fields(rows) == ["maps.ktrans.state"]


def test_the_row_carries_the_two_values_and_nothing_else() -> None:
    """The point of the change: the values are scalars, not whole objects."""
    before = maps(("ktrans", "unused"))
    after = maps(("ktrans", "required"))
    (row,) = _change_rows({"maps": before}, {"maps": after})
    assert row["before"] == "unused"
    assert row["after"] == "required"


def test_two_changed_maps_produce_two_rows() -> None:
    before = maps(("cbf", "unused"), ("ktrans", "unused"))
    after = maps(("cbf", "required"), ("ktrans", "optional"))
    assert fields(_change_rows({"maps": before}, {"maps": after})) == [
        "maps.cbf.state", "maps.ktrans.state",
    ]


def test_reordering_is_not_a_change() -> None:
    """Order in these lists is presentation, not meaning."""
    before = maps(("cbf", "required"), ("ktrans", "unused"))
    after = maps(("ktrans", "unused"), ("cbf", "required"))
    assert _change_rows({"maps": before}, {"maps": after}) == []


def test_required_artifacts_are_reported_per_artifact() -> None:
    before = [{"id": "methods", "label": "Methods document", "required": False}]
    after = [{"id": "methods", "label": "Methods document", "required": True}]
    (row,) = _change_rows({"required_artifacts": before}, {"required_artifacts": after})
    assert row["field"] == "required_artifacts.methods.required"
    assert (row["before"], row["after"]) == (False, True)


def test_an_alias_list_is_still_compared_as_a_whole() -> None:
    """A list of strings has no identity to match on, and reads fine whole."""
    before = maps(("cbf", "required"))
    after = [dict(before[0], aliases=["cbf", "perfmap"])]
    (row,) = _change_rows({"maps": before}, {"maps": after})
    assert row["field"] == "maps.cbf.aliases"
    assert row["after"] == ["cbf", "perfmap"]


# ── Items appearing and disappearing ──────────────────────────────────────

def test_a_new_map_is_reported_once_rather_than_field_by_field() -> None:
    before = maps(("cbf", "required"))
    after = maps(("cbf", "required"), ("ve", "optional"))
    assert fields(_change_rows({"maps": before}, {"maps": after})) == ["maps.ve"]


def test_a_removed_map_is_reported() -> None:
    before = maps(("cbf", "required"), ("ve", "optional"))
    after = maps(("cbf", "required"))
    (row,) = _change_rows({"maps": before}, {"maps": after})
    assert row["field"] == "maps.ve"
    assert row["after"] is None


# ── Everything else keeps working ─────────────────────────────────────────

def test_scalars_are_unaffected() -> None:
    rows = _change_rows({"code_execution_required": False},
                        {"code_execution_required": True})
    assert rows == [{"field": "code_execution_required",
                     "before": False, "after": True}]


def test_nested_dictionaries_still_recurse() -> None:
    rows = _change_rows({"scoring": {"mode": "none", "package_id": None}},
                        {"scoring": {"mode": "custom", "package_id": "pkg"}})
    assert fields(rows) == ["scoring.mode", "scoring.package_id"]


def test_dataset_counts_are_reported_per_field() -> None:
    rows = _change_rows({"datasets": {"clinical": {"participants": 5}}},
                        {"datasets": {"clinical": {"participants": 6}}})
    assert fields(rows) == ["datasets.clinical.participants"]


def test_an_unchanged_configuration_reports_nothing() -> None:
    config = {"maps": maps(("cbf", "required")), "code_execution_required": True}
    assert _change_rows(config, config) == []


def test_the_whole_realistic_edit_stays_readable() -> None:
    """The case from the screenshot: five settings, five short rows."""
    before = {
        "maps": maps(("cbf", "required"), ("att", "required"), ("ktrans", "unused")),
        "required_artifacts": [{"id": "methods", "required": False}],
        "code_execution_required": False,
        "scoring": {"mode": "none", "package_id": None},
    }
    after = {
        "maps": maps(("cbf", "required"), ("att", "required"), ("ktrans", "required")),
        "required_artifacts": [{"id": "methods", "required": True}],
        "code_execution_required": True,
        "scoring": {"mode": "custom", "package_id": "asl_qc_demo"},
    }
    rows = _change_rows(before, after)
    assert fields(rows) == [
        "code_execution_required",
        "maps.ktrans.state",
        "required_artifacts.methods.required",
        "scoring.mode",
        "scoring.package_id",
    ]
    # No row may carry a nested structure: that is what made it unreadable.
    assert all(not isinstance(row["after"], (dict, list)) for row in rows)
