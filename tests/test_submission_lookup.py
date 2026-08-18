"""Submission lookups use exact ids, including ids with common prefixes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

CLINICAL = "team_gamma_Clinical"
SYNTHETIC = "team_gamma_Synthetic"
PREFIX = "team_gamma"  # A prefix of both ids, but not a submission itself.


def _validation(sid: str, *, errors: int, team: str) -> dict:
    return {
        "submission_id": sid,
        "team_name": team,
        "contact_email": f"{team.lower().replace(' ', '.')}@example.org",
        "challenge_type": "DCE",
        "passed": errors == 0,
        "error_count": errors,
        "warning_count": 0,
        "errors": [{"severity": "error", "code": "X", "message": f"{sid} failure"}
                   for _ in range(errors)],
        "warnings": [],
        "nifti_count": 4,
        "mode": "result_only",
    }


@pytest.fixture()
def outputs(tmp_path: Path, monkeypatch):
    """Two real submissions on disk, sharing a common id prefix."""
    import main

    out = tmp_path / "outputs"
    (out / "validation").mkdir(parents=True)
    extracted = tmp_path / "extracted"
    extracted.mkdir()

    for sid, errors, team in ((CLINICAL, 42, "Gamma Clinical"),
                              (SYNTHETIC, 7, "Gamma Synthetic")):
        (out / "validation" / f"{sid}_validation.json").write_text(
            json.dumps(_validation(sid, errors=errors, team=team)), encoding="utf-8")
        (extracted / sid).mkdir()

    monkeypatch.setattr(main, "OUTPUTS_DIR", out)
    monkeypatch.setattr(main, "EXTRACTED_DIR", extracted)
    return main


# Exact matching

def test_each_real_id_finds_its_own_file(outputs) -> None:
    for sid in (CLINICAL, SYNTHETIC):
        found = outputs._find_validation_files(sid)
        assert [f.stem for f in found] == [f"{sid}_validation"]


def test_a_shared_prefix_matches_nothing(outputs) -> None:
    assert outputs._find_validation_files(PREFIX) == []


def test_a_shared_prefix_loads_no_validation(outputs) -> None:
    assert outputs._load_validation(PREFIX) is None


def test_the_prefix_does_not_inherit_another_submissions_errors(outputs) -> None:
    """The original symptom: 'team_gamma' reported team_gamma_Clinical's 42."""
    clinical = outputs._load_validation(CLINICAL)
    assert clinical["error_count"] == 42
    assert outputs._load_validation(PREFIX) is None


@pytest.mark.parametrize("sid", ["nope", "team", "team_gamma_", "TEAM_GAMMA_CLINICAL",
                                 "team_gamma_Clinic", "_Clinical", "Clinical"])
def test_ids_that_are_not_submissions_match_nothing(outputs, sid: str) -> None:
    assert outputs._find_validation_files(sid) == []


def test_no_id_still_lists_every_result(outputs) -> None:
    """The unfiltered mode /api/outputs relies on is deliberately unchanged."""
    assert len(outputs._find_validation_files()) == 2
    assert len(outputs._find_validation_files("")) == 2


def test_a_suffix_of_a_real_id_matches_nothing(outputs) -> None:
    """Substring matching failed in both directions, not just on prefixes."""
    assert outputs._find_validation_files("gamma_Clinical") == []


# ── Existence gate ────────────────────────────────────────────────────────

def test_known_ids_exist(outputs) -> None:
    assert outputs.submission_exists(CLINICAL)
    assert outputs.submission_exists(SYNTHETIC)


@pytest.mark.parametrize("sid", [PREFIX, "nope", "", "   "])
def test_unknown_ids_do_not_exist(outputs, sid: str) -> None:
    assert not outputs.submission_exists(sid)


def test_a_scored_submission_without_validation_still_exists(outputs, monkeypatch) -> None:
    """Exports may legitimately run before validation output is present."""
    monkeypatch.setattr(outputs, "load_scoring_result",
                        lambda sid: {"submission_id": sid} if sid == "scored_only" else None)
    assert outputs.submission_exists("scored_only")


def test_existence_uses_a_real_scoring_lookup(outputs) -> None:
    """Guards against the lookup being swallowed by a bare except.

    ``_scoring_result_path`` is not in this module's namespace; calling it
    would raise NameError, and a try/except would have turned every
    scored-but-unvalidated submission into 'not found' silently.
    """
    assert callable(outputs.load_scoring_result)
    assert not hasattr(outputs, "_scoring_result_path")


# ── The endpoints ─────────────────────────────────────────────────────────

def test_collect_export_ids_rejects_an_unknown_id(outputs) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        outputs._collect_export_ids(None, PREFIX)
    assert raised.value.status_code == 404


def test_collect_export_ids_accepts_a_real_id(outputs) -> None:
    assert outputs._collect_export_ids(None, CLINICAL) == [CLINICAL]


def test_collect_export_ids_still_requires_an_identifier(outputs) -> None:
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as raised:
        outputs._collect_export_ids(None, None)
    assert raised.value.status_code == 400


def test_report_endpoint_returns_404_for_an_unknown_submission(outputs) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(outputs.app)
    response = client.get("/api/report", params={"submission_id": PREFIX})
    assert response.status_code == 404


def test_a_blinded_report_never_carries_another_teams_data(outputs) -> None:
    """The security-relevant half: no cross-submission leakage, blinded or not."""
    from fastapi.testclient import TestClient

    client = TestClient(outputs.app)
    response = client.get("/api/report",
                          params={"submission_id": SYNTHETIC, "blinded": "true"})
    assert response.status_code == 200
    body = response.text
    assert "Gamma Clinical" not in body
    assert "Gamma Synthetic" not in body       # blinded: neither team is named
    assert CLINICAL not in body
    assert f"{SYNTHETIC} failure" in body      # its own issue text is present


def test_an_unblinded_report_names_only_its_own_team(outputs) -> None:
    from fastapi.testclient import TestClient

    client = TestClient(outputs.app)
    body = client.get("/api/report",
                      params={"submission_id": SYNTHETIC, "blinded": "false"}).text
    assert "Gamma Synthetic" in body
    assert "Gamma Clinical" not in body
