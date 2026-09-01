"""Structural BIDS checks.

The proposal named BIDS twice and the code mentioned it only in a disclaimer
saying NIfTI QC is not BIDS validation. That was true, which was the problem.

What is checked is a subset: layout and naming, the part that lets a reader
work out which subject, session and run a file belongs to. The tests below
pin both halves of that claim, the rules that are enforced and the fact that
nothing runs unless a challenge asks for it, because a check that silently
starts rejecting submissions is worse than no check.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

sys.path[:0] = [str(Path(__file__).resolve().parents[1] / "src")]

from osipi_pipeline.config import rules  # noqa: E402
from osipi_pipeline.validation.bids import (  # noqa: E402
    ENTITY_ORDER,
    looks_like_bids,
    parse_entities,
    validate_bids_structure,
)
from osipi_pipeline.validation.validate import validate_submission  # noqa: E402

CONFIG = Path(__file__).resolve().parents[1] / "config" / "validation_rules.yaml"


def bids_root(tmp_path: Path, *, description=True) -> Path:
    root = tmp_path / "submission"
    (root / "sub-01" / "perf").mkdir(parents=True)
    if description:
        (root / "dataset_description.json").write_text(
            json.dumps({"Name": "demo", "BIDSVersion": "1.8.0"}), encoding="utf-8")
    return root


def add(root: Path, name: str, subject: str = "sub-01") -> Path:
    path = root / subject / "perf" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * 64)
    return path


def codes(issues) -> list[str]:
    return sorted(issue.code for issue in issues)


# ── Filename parsing ──────────────────────────────────────────────────────

def test_entities_and_suffix_are_separated() -> None:
    entities, suffix, malformed = parse_entities("sub-01_ses-1_run-2_cbf")
    assert entities == {"sub": "01", "ses": "1", "run": "2"}
    assert suffix == "cbf"
    assert malformed == []


def test_a_part_that_is_neither_is_reported_rather_than_dropped() -> None:
    """Silently ignoring a stray part is how a typo becomes a missing file."""
    _, _, malformed = parse_entities("sub-01_oops_cbf")
    assert malformed == ["oops"]


def test_the_entity_order_is_the_one_bids_fixes() -> None:
    assert ENTITY_ORDER[:4] == ("sub", "ses", "task", "acq")
    assert ENTITY_ORDER[-1] == "desc"


# ── What a clean dataset produces ─────────────────────────────────────────

def test_a_correct_dataset_reports_nothing(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    add(root, "sub-01_run-1_cbf.nii.gz")
    assert validate_bids_structure(root) == []


def test_sessions_are_accepted(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    path = root / "sub-01" / "ses-1" / "perf" / "sub-01_ses-1_run-1_cbf.nii.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\0" * 64)
    assert validate_bids_structure(root) == []


# ── Each rule ─────────────────────────────────────────────────────────────

def test_a_missing_description_is_reported(tmp_path: Path) -> None:
    root = bids_root(tmp_path, description=False)
    add(root, "sub-01_run-1_cbf.nii.gz")
    assert "BIDS_DATASET_DESCRIPTION_MISSING" in codes(validate_bids_structure(root))


def test_an_unparseable_description_is_reported(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    (root / "dataset_description.json").write_text("{not json", encoding="utf-8")
    assert "BIDS_DATASET_DESCRIPTION_INVALID" in codes(validate_bids_structure(root))


def test_a_description_missing_required_fields_is_reported(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    (root / "dataset_description.json").write_text(
        json.dumps({"Name": "demo"}), encoding="utf-8")
    issues = validate_bids_structure(root)
    assert "BIDS_DATASET_DESCRIPTION_INCOMPLETE" in codes(issues)
    assert "BIDSVersion" in issues[0].message


def test_entities_out_of_order_are_reported(tmp_path: Path) -> None:
    """The rule that makes a name mechanically parseable rather than merely readable."""
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    issues = validate_bids_structure(root)
    assert "BIDS_ENTITY_ORDER" in codes(issues)
    # The message has to say what the order should be, or it is not actionable.
    assert "sub_run" in " ".join(issue.message for issue in issues)


def test_an_unknown_entity_is_reported(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    add(root, "sub-01_bogus-3_cbf.nii.gz")
    assert "BIDS_UNKNOWN_ENTITY" in codes(validate_bids_structure(root))


def test_a_missing_suffix_is_reported(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    add(root, "sub-01_run-1.nii.gz")
    assert "BIDS_SUFFIX_MISSING" in codes(validate_bids_structure(root))


def test_a_filename_disagreeing_with_its_directory_is_reported(tmp_path: Path) -> None:
    """The case that quietly attaches data to the wrong subject."""
    root = bids_root(tmp_path)
    add(root, "sub-02_run-1_cbf.nii.gz", subject="sub-01")
    issues = validate_bids_structure(root)
    assert "BIDS_ENTITY_DIRECTORY_MISMATCH" in codes(issues)


def test_a_stray_top_level_directory_is_reported(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    add(root, "sub-01_run-1_cbf.nii.gz")
    (root / "notasubject").mkdir()
    assert "BIDS_UNEXPECTED_TOP_LEVEL_DIRECTORY" in codes(validate_bids_structure(root))


@pytest.mark.parametrize("folder", ["derivatives", "code", "sourcedata"])
def test_directories_bids_defines_are_left_alone(tmp_path: Path, folder: str) -> None:
    root = bids_root(tmp_path)
    add(root, "sub-01_run-1_cbf.nii.gz")
    (root / folder).mkdir()
    assert validate_bids_structure(root) == []


def test_a_label_with_a_separator_in_it_is_reported(tmp_path: Path) -> None:
    """A hyphen inside a label makes the name ambiguous to a parser."""
    root = tmp_path / "s"
    (root / "sub-01-b").mkdir(parents=True)
    (root / "dataset_description.json").write_text(
        json.dumps({"Name": "d", "BIDSVersion": "1.8.0"}), encoding="utf-8")
    assert "BIDS_INVALID_LABEL" in codes(validate_bids_structure(root))


# ── Severity ──────────────────────────────────────────────────────────────

def test_severity_is_configurable(tmp_path: Path) -> None:
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    assert all(i.severity == "warning" for i in validate_bids_structure(root))
    assert all(i.severity == "error"
               for i in validate_bids_structure(root, severity="error"))


def test_an_unrecognised_severity_falls_back_to_warning(tmp_path: Path) -> None:
    """Never silently promote a finding to blocking."""
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    assert all(i.severity == "warning"
               for i in validate_bids_structure(root, severity="nonsense"))


# ── Whether it runs at all ────────────────────────────────────────────────

def test_a_folder_with_no_bids_markers_is_not_treated_as_bids(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    (plain / "maps").mkdir(parents=True)
    (plain / "maps" / "ktrans.nii.gz").write_bytes(b"\0" * 64)
    assert looks_like_bids(plain) is False


@pytest.mark.parametrize("marker", ["description", "subject"])
def test_either_marker_identifies_a_bids_dataset(tmp_path: Path, marker: str) -> None:
    root = tmp_path / "s"
    root.mkdir()
    if marker == "description":
        (root / "dataset_description.json").write_text("{}", encoding="utf-8")
    else:
        (root / "sub-01").mkdir()
    assert looks_like_bids(root) is True


# ── Through validate_submission, with the real config ─────────────────────

@pytest.fixture()
def challenge_bids(tmp_path):
    """Temporarily enable BIDS checks for DCE, then put the config back."""
    original = CONFIG.read_text(encoding="utf-8")

    def configure(**settings):
        data = yaml.safe_load(original)
        data["challenges"]["dce"]["bids_validation"] = settings
        CONFIG.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        rules.clear_config_cache()

    yield configure
    CONFIG.write_text(original, encoding="utf-8")
    rules.clear_config_cache()


def _bids_codes(result) -> list[str]:
    return sorted(i.code for i in result.errors + result.warnings
                  if i.code.startswith("BIDS"))


def test_nothing_runs_when_the_challenge_has_not_asked(
    tmp_path: Path, challenge_bids,
) -> None:
    """A challenge that has switched BIDS off gets no BIDS findings at all.

    This used to read the shipped configuration, which had the checks off for
    every challenge. They are now on, so the off state has to be configured
    explicitly here; the behaviour under test is unchanged.
    """
    challenge_bids(enabled=False, severity="warning", require_layout=False)
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    result = validate_submission(root, challenge_type="dce", output_dir=tmp_path / "out")
    assert _bids_codes(result) == []


def test_every_shipped_challenge_enables_bids_advisorily() -> None:
    """The shipped posture, pinned so a silent flip in either direction shows up.

    On, at warning severity, without ``require_layout``: findings are reported
    for submissions that are already BIDS-shaped and nothing is rejected for
    not using BIDS. Turning this into an error, or requiring the layout, is a
    decision about what participants must supply, so it should have to break a
    test rather than slip through as a config tweak.
    """
    settings = rules.bids_validation_by_challenge()
    assert settings, "no challenges are configured"
    for challenge, block in sorted(settings.items()):
        assert block.get("enabled") is True, f"{challenge} does not enable BIDS checks"
        assert block.get("severity") == "warning", (
            f"{challenge} would reject submissions over BIDS conformance"
        )
        assert block.get("require_layout") is False, (
            f"{challenge} would require every submission to be a BIDS dataset"
        )


def test_a_submission_that_is_not_bids_is_left_alone_by_the_shipped_config(
    tmp_path: Path,
) -> None:
    """Enabling the checks must not start reporting ordinary submissions.

    A folder with no ``dataset_description.json`` and no ``sub-<label>``
    directory is not a broken BIDS dataset; it is not one. Reporting it would
    make the finding list noisier for every participant who never opted in,
    which is the cost that kept these checks switched off.
    """
    root = tmp_path / "team_gamma"
    (root / "results" / "maps").mkdir(parents=True)
    (root / "results" / "maps" / "cbf_map.nii.gz").write_bytes(b"not really a nifti")
    (root / "README.md").write_text("# submission\n", encoding="utf-8")

    result = validate_submission(root, challenge_type="asl", output_dir=tmp_path / "out")
    assert _bids_codes(result) == []


def test_enabling_reports_through_the_normal_issue_list(tmp_path: Path, challenge_bids) -> None:
    challenge_bids(enabled=True, severity="warning", require_layout=False)
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    result = validate_submission(root, challenge_type="dce", output_dir=tmp_path / "out")
    assert "BIDS_ENTITY_ORDER" in _bids_codes(result)


def test_a_warning_lands_in_warnings_not_errors(tmp_path: Path, challenge_bids) -> None:
    """Advisory means advisory: it must not turn into a blocking error."""
    challenge_bids(enabled=True, severity="warning", require_layout=False)
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    result = validate_submission(root, challenge_type="dce", output_dir=tmp_path / "out")
    assert "BIDS_ENTITY_ORDER" in [i.code for i in result.warnings]
    assert "BIDS_ENTITY_ORDER" not in [i.code for i in result.errors]


def test_error_severity_lands_in_errors(tmp_path: Path, challenge_bids) -> None:
    challenge_bids(enabled=True, severity="error", require_layout=False)
    root = bids_root(tmp_path)
    add(root, "run-1_sub-01_cbf.nii.gz")
    result = validate_submission(root, challenge_type="dce", output_dir=tmp_path / "out")
    assert "BIDS_ENTITY_ORDER" in [i.code for i in result.errors]


def test_a_non_bids_submission_is_left_alone_when_layout_is_not_required(
        tmp_path: Path, challenge_bids) -> None:
    """Not using BIDS is not the same as using it wrongly."""
    challenge_bids(enabled=True, severity="error", require_layout=False)
    plain = tmp_path / "plain"
    (plain / "maps").mkdir(parents=True)
    (plain / "maps" / "ktrans.nii.gz").write_bytes(b"\0" * 64)
    result = validate_submission(plain, challenge_type="dce", output_dir=tmp_path / "out")
    assert _bids_codes(result) == []


def test_requiring_the_layout_rejects_a_submission_that_lacks_it(
        tmp_path: Path, challenge_bids) -> None:
    challenge_bids(enabled=True, severity="error", require_layout=True)
    plain = tmp_path / "plain"
    (plain / "maps").mkdir(parents=True)
    (plain / "maps" / "ktrans.nii.gz").write_bytes(b"\0" * 64)
    result = validate_submission(plain, challenge_type="dce", output_dir=tmp_path / "out")
    assert _bids_codes(result) == ["BIDS_LAYOUT_MISSING"]


# ── The configuration schema ──────────────────────────────────────────────

def test_the_shipped_config_still_validates() -> None:
    rules.clear_config_cache()
    rules.validate_config_files()


def _rejected(block) -> str:
    """The message raised when this bids_validation block is validated."""
    data = yaml.safe_load(CONFIG.read_text(encoding="utf-8"))
    data["challenges"]["dce"]["bids_validation"] = block
    with pytest.raises(rules.ConfigValidationError) as raised:
        rules.validate_validation_rules_data(data, label="test")
    return str(raised.value)


def test_an_unknown_key_in_the_block_is_rejected() -> None:
    """Consistent with the rest of the schema: a typo is an error, not a no-op."""
    assert "sevrity" in _rejected({"enabled": True, "sevrity": "error"})


def test_an_invalid_severity_is_rejected() -> None:
    assert "severity" in _rejected({"enabled": True, "severity": "loud"})


def test_a_non_boolean_flag_is_rejected() -> None:
    assert "enabled" in _rejected({"enabled": "yes"})


def test_a_block_that_is_not_a_mapping_is_rejected() -> None:
    assert "mapping" in _rejected(["enabled"])
