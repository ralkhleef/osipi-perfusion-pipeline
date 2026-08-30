"""Which findings stop a submission is challenge policy, not code.

The DCE challenge lead's synthetic submission is the layout she says teams
will really use, and it failed on rules the pipeline had assumed rather than
been told. Every one of those is a policy decision belonging to the organiser:
whether a methods document is mandatory, whether a submission that cannot be
checked for completeness is still acceptable.

Changing that used to mean editing Python. It is now a mapping of issue code to
severity in the challenge's own configuration, alongside the BIDS settings that
already worked this way.

Three properties matter and are pinned below. Defaults must not move, because a
challenge that never configures this must behave exactly as before. A typo must
be rejected loudly, because a silently ignored override leaves a rule at a
severity the organiser thinks they changed. And nothing indicating unreadable
or corrupt data may be downgraded at all.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]

from osipi_pipeline.config.rules import (  # noqa: E402
    _OVERRIDABLE_ISSUE_CODES,
    issue_severity_by_challenge,
)


def test_no_challenge_configures_overrides_by_default() -> None:
    """The shipped configuration must not rely on this feature."""
    for challenge, overrides in issue_severity_by_challenge().items():
        assert overrides == {}, f"{challenge} ships with severity overrides"


def test_every_overridable_code_is_a_real_one() -> None:
    """A code in the allow-list that no check emits could never take effect."""
    from osipi_pipeline.validation import completeness
    emitted = {
        value for name, value in vars(completeness).items()
        if name.isupper() and isinstance(value, str) and name == value
    }
    unknown = _OVERRIDABLE_ISSUE_CODES - emitted
    assert not unknown, f"listed but never emitted: {sorted(unknown)}"


def test_unreadable_data_cannot_be_downgraded() -> None:
    """Structure is policy. A file that will not open is not.

    Allowing these to be relaxed would let a challenge accept corrupt data by
    configuration, and every number computed from it would be wrong while the
    submission read as acceptable.
    """
    for code in ("NIFTI_UNREADABLE", "NO_NIFTI_FILES", "SUBMISSION_FOLDER_EMPTY"):
        assert code not in _OVERRIDABLE_ISSUE_CODES, code


# ── The configuration schema rejects mistakes ─────────────────────────────

def _rules_with(issue_severity) -> dict:
    """The shipped rules document with one override block added to DCE.

    Built from the real configuration rather than a hand-written stub. The
    schema requires a dozen top-level sections, so a stub small enough to read
    fails for reasons that have nothing to do with what is being tested, and a
    stub large enough to pass is a second copy of the config that drifts.
    """
    import copy
    import yaml

    document = yaml.safe_load(
        (ROOT / "config" / "validation_rules.yaml").read_text(encoding="utf-8"))
    document = copy.deepcopy(document)
    document["challenges"]["dce"]["issue_severity"] = issue_severity
    return document


def _errors(document: dict) -> list[str]:
    """Schema errors for a rules document, as a list.

    The validator raises rather than returning, because a bad configuration
    should stop the application rather than be worked around. Tests want the
    text, so the exception is caught here and nowhere else.
    """
    from osipi_pipeline.config.rules import (
        ConfigValidationError,
        _validate_validation_rules,
    )
    try:
        _validate_validation_rules(document, Path("test-rules.yaml"))
    except ConfigValidationError as exc:
        return str(exc).splitlines()
    return []


def test_a_valid_override_is_accepted() -> None:
    assert _errors(_rules_with({"REQUIRED_ARTIFACT_MISSING": "warning"})) == []


@pytest.mark.parametrize("level", ["error", "warning", "info"])
def test_each_allowed_level_is_accepted(level) -> None:
    assert _errors(_rules_with({"REQUIRED_MAP_MISSING": level})) == []


def test_a_misspelled_code_is_rejected_and_lists_the_real_ones() -> None:
    """Silently ignoring it would leave the rule at a severity nobody chose."""
    errors = _errors(_rules_with({"REQUIRED_ARTIFACT_MISSNG": "warning"}))
    assert errors, "a typo in an issue code was accepted"
    assert "REQUIRED_ARTIFACT_MISSING" in " ".join(errors), (
        "the error does not say what the valid codes are")


def test_an_invalid_severity_is_rejected() -> None:
    errors = _errors(_rules_with({"REQUIRED_MAP_MISSING": "ignore"}))
    assert errors
    assert "must be" in " ".join(errors)


def test_a_non_mapping_is_rejected() -> None:
    assert _errors(_rules_with(["REQUIRED_MAP_MISSING"]))


# ── The override actually changes the outcome ─────────────────────────────

def test_an_override_relabels_the_finding_and_says_where_it_came_from(
        monkeypatch) -> None:
    """A reader has to be able to tell a configured severity from a default."""
    from osipi_pipeline.ingestion.models import SubmissionArtifact
    from osipi_pipeline.validation import completeness

    monkeypatch.setattr(
        completeness, "issue_severity_by_challenge",
        lambda: {"dce": {"REQUIRED_ARTIFACT_MISSING": "warning"}})

    artifacts = [SubmissionArtifact(
        path="P01/site_1/scan_1/ktrans.nii.gz", role="parameter_map",
        map_type="ktrans", dataset="clinical", participant="1",
        repeat="1", site="1")]
    issues = completeness.validate_completeness(artifacts, challenge="dce")
    relabelled = [i for i in issues if i["code"] == "REQUIRED_ARTIFACT_MISSING"]
    assert relabelled, "the finding this test is about was not produced"
    for issue in relabelled:
        assert issue["severity"] == "warning"
        assert issue["severity_source"] == "challenge configuration"


def test_findings_without_an_override_keep_their_default(monkeypatch) -> None:
    from osipi_pipeline.ingestion.models import SubmissionArtifact
    from osipi_pipeline.validation import completeness

    monkeypatch.setattr(
        completeness, "issue_severity_by_challenge",
        lambda: {"dce": {"REQUIRED_ARTIFACT_MISSING": "warning"}})

    artifacts = [SubmissionArtifact(
        path="P01/site_1/scan_1/ktrans.nii.gz", role="parameter_map",
        map_type="ktrans", dataset="clinical", participant="1",
        repeat="1", site="1")]
    issues = completeness.validate_completeness(artifacts, challenge="dce")
    others = [i for i in issues if i["code"] != "REQUIRED_ARTIFACT_MISSING"]
    assert others, "nothing else was produced, so this proves nothing"
    for issue in others:
        assert "severity_source" not in issue, issue["code"]


def test_another_challenge_is_unaffected(monkeypatch) -> None:
    """Overrides are per challenge, so DCE policy must not reach ASL."""
    from osipi_pipeline.validation import completeness
    monkeypatch.setattr(
        completeness, "issue_severity_by_challenge",
        lambda: {"dce": {"REQUIRED_MAP_MISSING": "info"}})
    from osipi_pipeline.ingestion.models import SubmissionArtifact
    artifacts = [SubmissionArtifact(
        path="cbf.nii.gz", role="parameter_map", map_type="cbf")]
    issues = completeness.validate_completeness(artifacts, challenge="asl")
    for issue in issues:
        assert issue.get("severity_source") is None
