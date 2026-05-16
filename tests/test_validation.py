"""Tests for validation v1."""

from __future__ import annotations

import json
from pathlib import Path

from osipi_pipeline.validation.validate import validate_submission


def test_valid_minimal_dce_submission(tmp_path: Path) -> None:
    submission = _make_submission(tmp_path / "dce_team_alpha", include_docker=True, include_code=True)

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
    submission = _make_submission(tmp_path / "minimal_warning")

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert [issue.code for issue in result.warnings] == ["DOCKERFILE_MISSING", "NO_CODE_FILES"]


def _make_submission(root: Path, *, include_docker: bool = False, include_code: bool = False) -> Path:
    root.mkdir()
    (root / "Ktrans_map.nii.gz").write_text("fake nifti", encoding="utf-8")
    (root / "README.md").write_text("# Submission\n", encoding="utf-8")
    if include_docker:
        (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    if include_code:
        (root / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return root

