"""Tests for validation v1."""

from __future__ import annotations

import json
from pathlib import Path

from osipi_pipeline.validation.validate import main, validate_submission


def test_valid_minimal_dce_submission(tmp_path: Path) -> None:
    submission = _make_submission(
        tmp_path / "dce_team_alpha",
        maps=("Ktrans_map.nii.gz", "kep_map.nii.gz", "vp_map.nii.gz"),
        include_docker=True,
        include_code=True,
    )

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert result.errors == []
    assert result.warnings == []
    saved = tmp_path / "validation" / "dce_dce_team_alpha_validation.json"
    assert saved.exists()
    assert json.loads(saved.read_text(encoding="utf-8"))["passed"] is True


def test_missing_folder_fails(tmp_path: Path) -> None:
    result = validate_submission(tmp_path / "missing", challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is False
    assert [issue.code for issue in result.errors] == ["SUBMISSION_FOLDER_MISSING"]


def test_empty_folder_fails(tmp_path: Path) -> None:
    submission = tmp_path / "empty"
    submission.mkdir()

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is False
    assert [issue.code for issue in result.errors] == ["SUBMISSION_FOLDER_EMPTY"]


def test_no_nifti_files_fails(tmp_path: Path) -> None:
    submission = tmp_path / "no_nifti"
    submission.mkdir()
    (submission / "README.md").write_text("# Submission\n", encoding="utf-8")

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is False
    assert "NO_NIFTI_FILES" in [issue.code for issue in result.errors]


def test_unknown_challenge_type_fails(tmp_path: Path) -> None:
    submission = _make_submission(tmp_path / "unknown_team")

    result = validate_submission(submission, challenge_type="dsc", output_dir=tmp_path / "validation")

    assert result.passed is False
    assert "UNKNOWN_CHALLENGE_TYPE" in [issue.code for issue in result.errors]


def test_warns_for_missing_dockerfile_and_code_files(tmp_path: Path) -> None:
    submission = _make_submission(tmp_path / "minimal_warning", maps=("Ktrans_map.nii.gz", "kep_map.nii.gz", "vp_map.nii.gz"))

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert [issue.code for issue in result.warnings] == ["DOCKERFILE_MISSING", "NO_CODE_FILES"]


def test_dce_missing_expected_maps_warns(tmp_path: Path) -> None:
    submission = _make_submission(tmp_path / "dce_missing_maps", maps=("Ktrans_map.nii.gz",), include_docker=True, include_code=True)

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert [issue.message for issue in result.warnings] == [
        "Expected kep parameter map was not found.",
        "Expected vp parameter map was not found.",
    ]


def test_asl_missing_expected_maps_warns(tmp_path: Path) -> None:
    submission = _make_submission(tmp_path / "asl_missing_maps", maps=("CBF_map.nii.gz",), include_docker=True, include_code=True)

    result = validate_submission(submission, challenge_type="asl", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert [issue.message for issue in result.warnings] == ["Expected att parameter map was not found."]


def test_empty_nifti_file_warns(tmp_path: Path) -> None:
    submission = _make_submission(
        tmp_path / "empty_nifti",
        maps=("Ktrans_map.nii.gz", "kep_map.nii.gz", "vp_map.nii.gz"),
        include_docker=True,
        include_code=True,
    )
    (submission / "Ktrans_map.nii.gz").write_text("", encoding="utf-8")

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert "EMPTY_NIFTI_FILE" in [issue.code for issue in result.warnings]


def test_duplicate_filename_warns(tmp_path: Path) -> None:
    submission = _make_submission(
        tmp_path / "duplicate_filename",
        maps=("Ktrans_map.nii.gz", "kep_map.nii.gz", "vp_map.nii.gz"),
        include_docker=True,
        include_code=True,
    )
    nested = submission / "nested"
    nested.mkdir()
    (nested / "Ktrans_map.nii.gz").write_text("fake nifti", encoding="utf-8")

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert "DUPLICATE_FILENAME" in [issue.code for issue in result.warnings]


def test_cli_still_passes_with_warnings_only(tmp_path: Path, monkeypatch, capsys) -> None:
    submission = _make_submission(tmp_path / "cli_warning", maps=("Ktrans_map.nii.gz", "kep_map.nii.gz", "vp_map.nii.gz"))
    monkeypatch.chdir(tmp_path)

    exit_code = main(["--input", str(submission), "--challenge", "dce"])

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Validation: PASSED" in output
    assert f"Checked path: {submission}" in output
    assert "Challenge type: dce" in output
    assert "Warnings: 2" in output


def test_hard_errors_still_fail(tmp_path: Path) -> None:
    submission = tmp_path / "hard_error"
    submission.mkdir()
    (submission / "notes.txt").write_text("no required files", encoding="utf-8")

    result = validate_submission(submission, challenge_type="unknown", output_dir=tmp_path / "validation")

    assert result.passed is False
    assert {"UNKNOWN_CHALLENGE_TYPE", "NO_NIFTI_FILES", "NO_README_OR_METADATA"}.issubset(
        {issue.code for issue in result.errors}
    )


def _make_submission(
    root: Path,
    *,
    maps: tuple[str, ...] = ("Ktrans_map.nii.gz",),
    include_docker: bool = False,
    include_code: bool = False,
) -> Path:
    root.mkdir()
    for map_name in maps:
        (root / map_name).write_text("fake nifti", encoding="utf-8")
    (root / "README.md").write_text("# Submission\n", encoding="utf-8")
    if include_docker:
        (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    if include_code:
        (root / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return root
