"""Phase 3: configuration-driven submission completeness.

Exercises the pure checker against normalized artifacts. Structural validity
only — no scientific quantity is computed anywhere in this phase.
"""

from __future__ import annotations

import pytest

from osipi_pipeline.ingestion.models import IdentityConflict, SubmissionArtifact
from osipi_pipeline.validation.completeness import (
    suppressed_legacy_map_ids,
    validate_completeness,
)


# ── Builders ──────────────────────────────────────────────────────────────

def _map(map_type="ktrans", *, dataset="synthetic", participant="1",
         repeat="1", site="1", dims=3, path=None):
    return SubmissionArtifact(
        path=path or f"{dataset}/p{participant}/s{site}/r{repeat}/{map_type}.nii.gz",
        role="parameter_map", challenge="dce", dataset=dataset,
        participant=participant, repeat=repeat, site=site,
        map_type=map_type, dimensions=dims,
    )


def _signal(*, dataset="synthetic", participant="1", repeat="1", site="1",
            dims=4, path=None):
    return SubmissionArtifact(
        path=path or f"{dataset}/p{participant}/s{site}/r{repeat}/modelled_st.nii.gz",
        role="fitted_signal", challenge="dce", dataset=dataset,
        participant=participant, repeat=repeat, site=site,
        artifact_type="modelled_st", dimensions=dims,
    )


def _methods(path="methods.docx"):
    return SubmissionArtifact(path=path, role="methods", challenge="dce",
                              artifact_type="methods")


def _scan(**kw):
    """One complete scan: required map + fitted signal."""
    return [_map(**kw), _signal(**kw)]


def _synthetic_participant(participant="1"):
    """A complete synthetic participant: 2 repeats x 3 sites."""
    out = []
    for repeat in ("1", "2"):
        for site in ("1", "2", "3"):
            out += _scan(participant=participant, repeat=repeat, site=site)
    return out


def _clinical_participant(participant="1"):
    out = []
    for repeat in ("1", "2"):
        out += _scan(dataset="clinical", participant=participant,
                     repeat=repeat, site=None)
    return out


def _codes(issues, severity=None):
    return [i["code"] for i in issues
            if severity is None or i["severity"] == severity]


def _run(artifacts, conflicts=(), challenge="dce"):
    return validate_completeness(artifacts, challenge=challenge,
                                 identity_conflicts=conflicts)


def _complete_synthetic():
    return _synthetic_participant("1") + [_methods()]


# ── Baseline ──────────────────────────────────────────────────────────────

def test_complete_synthetic_participant_passes() -> None:
    assert _codes(_run(_complete_synthetic()), "error") == []


def test_complete_clinical_submission_passes() -> None:
    artifacts = [_methods()]
    for p in ("1", "2", "3", "4", "5"):
        artifacts += _clinical_participant(p)
    assert _codes(_run(artifacts), "error") == []


# ── Required maps ─────────────────────────────────────────────────────────

def test_missing_ktrans_blocks() -> None:
    artifacts = [a for a in _complete_synthetic()
                 if a.map_type != "ktrans" or a.repeat != "1" or a.site != "1"]
    issues = _run(artifacts)
    assert "REQUIRED_MAP_MISSING" in _codes(issues, "error")


def test_missing_ktrans_message_identifies_the_scan() -> None:
    artifacts = [a for a in _complete_synthetic()
                 if not (a.map_type == "ktrans" and a.repeat == "1" and a.site == "2")]
    issue = next(i for i in _run(artifacts) if i["code"] == "REQUIRED_MAP_MISSING")
    assert "Ktrans" in issue["message"]
    assert issue["participant"] == "1"
    assert issue["repeat"] == "1"
    assert issue["site"] == "2"


def test_ktrans_required_for_every_scan() -> None:
    """Removing Ktrans from two scans yields two distinct errors."""
    artifacts = [a for a in _complete_synthetic()
                 if not (a.map_type == "ktrans" and a.repeat == "1"
                         and a.site in {"1", "2"})]
    missing = [i for i in _run(artifacts) if i["code"] == "REQUIRED_MAP_MISSING"]
    assert len(missing) == 2


@pytest.mark.parametrize("optional", ["vp", "ve", "kep"])
def test_absent_optional_maps_produce_nothing(optional: str) -> None:
    issues = _run(_complete_synthetic())
    assert not any(i.get("map_type") == optional for i in issues)


def test_present_optional_map_is_still_validated() -> None:
    """Optional does not mean unchecked: a 4D vp is still wrong."""
    artifacts = _complete_synthetic() + [_map("vp", dims=4)]
    assert "MAP_DIMENSION_MISMATCH" in _codes(_run(artifacts), "error")


def test_present_valid_optional_map_passes() -> None:
    artifacts = _complete_synthetic() + [_map("vp", dims=3)]
    assert _codes(_run(artifacts), "error") == []


# ── Required artifacts ────────────────────────────────────────────────────

def test_missing_modelled_st_blocks() -> None:
    artifacts = [a for a in _complete_synthetic()
                 if not (a.artifact_type == "modelled_st"
                         and a.repeat == "1" and a.site == "1")]
    issues = _run(artifacts)
    assert "REQUIRED_ARTIFACT_MISSING" in _codes(issues, "error")


def test_modelled_st_is_required_per_scan_not_once() -> None:
    """One modelled_st does not satisfy six scans."""
    artifacts = [a for a in _complete_synthetic() if a.artifact_type != "modelled_st"]
    artifacts.append(_signal(repeat="1", site="1"))
    missing = [i for i in _run(artifacts)
               if i["code"] == "REQUIRED_ARTIFACT_MISSING"
               and i.get("artifact_type") == "modelled_st"]
    assert len(missing) == 5


def test_missing_methods_blocks() -> None:
    artifacts = _synthetic_participant("1")
    issues = _run(artifacts)
    assert any(i["code"] == "REQUIRED_ARTIFACT_MISSING"
               and i.get("artifact_type") == "methods" for i in issues)


def test_one_methods_file_satisfies_the_whole_submission() -> None:
    """Methods is submission-level: one file covers every scan."""
    artifacts = _synthetic_participant("1") + _synthetic_participant("2") + [_methods()]
    assert not any(i.get("artifact_type") == "methods" for i in _run(artifacts))


def test_methods_needs_no_scan_identity() -> None:
    methods = _methods()
    assert (methods.dataset, methods.participant, methods.repeat, methods.site) == \
        (None, None, None, None)
    assert _codes(_run(_synthetic_participant("1") + [methods]), "error") == []


# ── Dimensionality ────────────────────────────────────────────────────────

def test_four_dimensional_ktrans_fails() -> None:
    artifacts = [a for a in _complete_synthetic()
                 if not (a.map_type == "ktrans" and a.repeat == "1" and a.site == "1")]
    artifacts.append(_map("ktrans", repeat="1", site="1", dims=4))
    issue = next(i for i in _run(artifacts) if i["code"] == "MAP_DIMENSION_MISMATCH")
    assert issue["expected"] == 3 and issue["actual"] == 4
    assert "3D" in issue["message"] and "4D" in issue["message"]


def test_three_dimensional_modelled_st_fails() -> None:
    artifacts = [a for a in _complete_synthetic()
                 if not (a.artifact_type == "modelled_st"
                         and a.repeat == "1" and a.site == "1")]
    artifacts.append(_signal(repeat="1", site="1", dims=3))
    issue = next(i for i in _run(artifacts)
                 if i["code"] == "ARTIFACT_DIMENSION_MISMATCH")
    assert issue["expected"] == 4 and issue["actual"] == 3


def test_unreadable_dimensions_produce_no_mismatch() -> None:
    """A corrupt header is reported by the NIfTI validator, not duplicated here."""
    artifacts = [a for a in _complete_synthetic()
                 if not (a.map_type == "ktrans" and a.repeat == "1" and a.site == "1")]
    artifacts.append(_map("ktrans", repeat="1", site="1", dims=None))
    codes = _codes(_run(artifacts))
    assert "MAP_DIMENSION_MISMATCH" not in codes
    assert "ARTIFACT_DIMENSION_MISMATCH" not in codes


# ── Identity completeness ─────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["participant", "repeat", "site"])
def test_missing_synthetic_identity_blocks(field: str) -> None:
    artifacts = _complete_synthetic()
    artifacts.append(_map("ktrans", **{field: None}, path="stray.nii.gz"))
    issue = next(i for i in _run(artifacts)
                 if i["code"] == "INCOMPLETE_ARTIFACT_IDENTITY")
    assert field in issue["missing_fields"]


def test_clinical_site_may_be_implicit() -> None:
    """A one-site dataset must not demand a meaningless Site1 directory."""
    artifacts = [_methods()]
    for p in ("1", "2", "3", "4", "5"):
        artifacts += _clinical_participant(p)
    assert "INCOMPLETE_ARTIFACT_IDENTITY" not in _codes(_run(artifacts))


def test_unidentified_files_are_not_grouped_into_a_scan() -> None:
    """A file with no identity must not invent or join a scan."""
    artifacts = [_map("ktrans", participant=None, repeat=None, site=None,
                      path="Ktrans.nii.gz"), _methods()]
    issues = _run(artifacts)
    assert "INCOMPLETE_ARTIFACT_IDENTITY" in _codes(issues, "error")
    # It must not then be reported as a complete scan missing its signal.
    assert "REQUIRED_ARTIFACT_MISSING" not in [
        i["code"] for i in issues if i.get("artifact_type") == "modelled_st"
    ]


# ── Datasets ──────────────────────────────────────────────────────────────

def test_unknown_dataset_is_reported_not_coerced() -> None:
    artifacts = _complete_synthetic() + _scan(dataset="phantom")
    issue = next(i for i in _run(artifacts) if i["code"] == "UNKNOWN_DATASET")
    assert issue["dataset"] == "phantom"


def test_participant_with_one_repeat_fails() -> None:
    artifacts = [_methods()]
    for site in ("1", "2", "3"):
        artifacts += _scan(repeat="1", site=site)
    issue = next(i for i in _run(artifacts)
                 if i["code"] == "DATASET_COUNT_MISMATCH" and i["axis"] == "repeats")
    assert issue["expected"] == 2 and issue["actual"] == 1


def test_participant_with_two_sites_fails() -> None:
    artifacts = [_methods()]
    for repeat in ("1", "2"):
        for site in ("1", "2"):
            artifacts += _scan(repeat=repeat, site=site)
    sites = [i for i in _run(artifacts)
             if i["code"] == "DATASET_COUNT_MISMATCH" and i["axis"] == "sites"]
    assert sites and sites[0]["expected"] == 3


def test_synthetic_accepts_any_participant_count() -> None:
    """participants is null for synthetic: no total is enforced."""
    artifacts = _synthetic_participant("1") + _synthetic_participant("2") + [_methods()]
    assert not any(i.get("axis") == "participants" for i in _run(artifacts))


def test_clinical_participant_count_is_enforced() -> None:
    artifacts = [_methods()]
    for p in ("1", "2", "3", "4"):
        artifacts += _clinical_participant(p)
    issue = next(i for i in _run(artifacts)
                 if i["code"] == "DATASET_COUNT_MISMATCH" and i["axis"] == "participants")
    assert issue["expected"] == 5 and issue["actual"] == 4


def test_non_consecutive_identifiers_satisfy_a_count() -> None:
    """Repeats labelled 1 and 3 are still two repeats."""
    artifacts = [_methods()]
    for repeat in ("1", "3"):
        for site in ("1", "2", "3"):
            artifacts += _scan(repeat=repeat, site=site)
    assert not any(i.get("axis") == "repeats" for i in _run(artifacts))


# ── Duplicates ────────────────────────────────────────────────────────────

def test_duplicate_ktrans_in_one_scan_fails() -> None:
    artifacts = _complete_synthetic() + [
        _map("ktrans", repeat="1", site="1", path="dup/Ktrans_copy.nii.gz")]
    issue = next(i for i in _run(artifacts) if i["code"] == "DUPLICATE_PARAMETER_MAP")
    assert len(issue["paths"]) == 2


def test_same_map_in_different_repeats_is_not_duplicate() -> None:
    assert "DUPLICATE_PARAMETER_MAP" not in _codes(_run(_complete_synthetic()))


def test_same_map_in_different_sites_is_not_duplicate() -> None:
    artifacts = [_methods()]
    for site in ("1", "2", "3"):
        artifacts += _scan(repeat="1", site=site)
        artifacts += _scan(repeat="2", site=site)
    assert "DUPLICATE_PARAMETER_MAP" not in _codes(_run(artifacts))


def test_duplicate_modelled_st_fails() -> None:
    artifacts = _complete_synthetic() + [
        _signal(repeat="1", site="1", path="dup/modelled_st2.nii.gz")]
    assert "DUPLICATE_REQUIRED_ARTIFACT" in _codes(_run(artifacts), "error")


def test_multiple_methods_documents_warn_but_do_not_block() -> None:
    artifacts = _complete_synthetic() + [_methods("extra/methodology.txt")]
    issues = _run(artifacts)
    assert "DUPLICATE_METHODS_DOCUMENT" in _codes(issues, "warning")
    assert _codes(issues, "error") == []


# ── Identity conflicts ────────────────────────────────────────────────────

@pytest.mark.parametrize("field", ["dataset", "participant", "repeat", "site"])
def test_identity_conflicts_block(field: str) -> None:
    conflict = IdentityConflict(path="a.nii.gz", field=field,
                                directory_value="1", filename_value="2")
    issues = _run(_complete_synthetic(), conflicts=[conflict])
    issue = next(i for i in issues if i["code"] == "IDENTITY_CONFLICT")
    assert issue["severity"] == "error"
    assert issue["field"] == field
    assert issue["directory_value"] == "1"
    assert issue["filename_value"] == "2"


def test_no_conflict_produces_no_issue() -> None:
    assert "IDENTITY_CONFLICT" not in _codes(_run(_complete_synthetic()))


def test_conflict_message_states_directory_won() -> None:
    conflict = IdentityConflict(path="a.nii.gz", field="participant",
                                directory_value="1", filename_value="2")
    issue = next(i for i in _run(_complete_synthetic(), conflicts=[conflict])
                 if i["code"] == "IDENTITY_CONFLICT")
    assert "directory value was used" in issue["message"]


# ── Legacy challenges ─────────────────────────────────────────────────────

@pytest.mark.parametrize("challenge", ["asl", "dsc"])
def test_challenges_without_new_config_produce_no_issues(challenge: str) -> None:
    """ASL and DSC declare none of the new fields, so nothing is enforced."""
    flat = [SubmissionArtifact(path="cbf.nii.gz", role="parameter_map",
                               challenge=challenge, map_type="cbf", dimensions=3)]
    assert _run(flat, challenge=challenge) == []


@pytest.mark.parametrize("challenge,expected", [
    ("dce", {"ktrans", "vp", "ve", "kep"}),
    ("asl", set()),
    ("dsc", set()),
])
def test_legacy_warning_suppression_is_scoped_to_migrated_challenges(
    challenge: str, expected: set
) -> None:
    assert set(suppressed_legacy_map_ids(challenge)) == expected


# ── Serialization ─────────────────────────────────────────────────────────

def test_issues_are_json_safe() -> None:
    import json

    conflict = IdentityConflict(path="a.nii.gz", field="site",
                                directory_value="1", filename_value="2")
    artifacts = _complete_synthetic() + [_map("vp", dims=4)]
    issues = _run(artifacts, conflicts=[conflict])
    assert json.loads(json.dumps(issues)) == issues
    for issue in issues:
        assert set(issue) >= {"severity", "code", "message", "path"}


# ── Scale ─────────────────────────────────────────────────────────────────

def test_large_submission_validates_without_quadratic_blowup() -> None:
    artifacts = [_methods()]
    for p in range(1, 11):
        artifacts += _synthetic_participant(str(p))
    assert len(artifacts) == 1 + 10 * 2 * 3 * 2
    assert _codes(_run(artifacts), "error") == []
