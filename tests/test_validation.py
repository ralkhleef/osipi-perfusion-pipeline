"""Tests for the validation module."""

from __future__ import annotations

import json
from pathlib import Path

import nibabel as nib
import numpy as np

from osipi_pipeline.validation.validate import main, validate_submission


def _write_minimal_nifti(path: Path) -> None:
    """Write a small valid NIfTI file that nibabel can load."""
    data = np.zeros((3, 3, 3), dtype=np.float32)
    img = nib.Nifti1Image(data, affine=np.eye(4))
    nib.save(img, str(path))


def _make_submission(
    root: Path,
    *,
    maps: tuple[str, ...] = ("Ktrans_map.nii.gz",),
    include_docker: bool = False,
    include_code: bool = False,
) -> Path:
    root.mkdir()
    for map_name in maps:
        _write_minimal_nifti(root / map_name)
    (root / "README.md").write_text("# Submission\n", encoding="utf-8")
    if include_docker:
        (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    if include_code:
        (root / "run.py").write_text("print('ok')\n", encoding="utf-8")
    return root


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
    # Overwrite one map with an empty file. 0-byte files are skipped by the
    # nibabel step so this stays a warning, not an error.
    (submission / "Ktrans_map.nii.gz").write_bytes(b"")

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
    # Use a real NIfTI so the duplicate does not also trigger NIFTI_UNREADABLE.
    _write_minimal_nifti(nested / "Ktrans_map.nii.gz")

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


def test_nifti_summary_present_in_result(tmp_path: Path) -> None:
    submission = _make_submission(
        tmp_path / "summary_check",
        maps=("Ktrans_map.nii.gz", "kep_map.nii.gz", "vp_map.nii.gz"),
        include_docker=True,
        include_code=True,
    )

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is True
    assert len(result.nifti_summary) == 3
    for entry in result.nifti_summary:
        assert entry["valid"] is True
        assert entry["shape"] == [3, 3, 3]
        assert entry["errors"] == []


def test_nifti_summary_in_json_output(tmp_path: Path) -> None:
    submission = _make_submission(
        tmp_path / "json_summary",
        maps=("Ktrans_map.nii.gz",),
        include_docker=True,
        include_code=True,
    )

    validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    saved = tmp_path / "validation" / "dce_json_summary_validation.json"
    data = json.loads(saved.read_text(encoding="utf-8"))
    assert "nifti_summary" in data
    assert isinstance(data["nifti_summary"], list)
    assert len(data["nifti_summary"]) == 1
    entry = data["nifti_summary"][0]
    assert entry["valid"] is True
    assert "shape" in entry
    assert "dtype" in entry
    assert "min" in entry
    assert "max" in entry
    assert "mean" in entry
    assert "nan_count" in entry
    assert "inf_count" in entry


def test_fake_nifti_causes_validation_error(tmp_path: Path) -> None:
    submission = tmp_path / "fake_nifti_team"
    submission.mkdir()
    (submission / "Ktrans_map.nii.gz").write_text("this is not a nifti", encoding="utf-8")
    (submission / "README.md").write_text("# readme\n", encoding="utf-8")

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert result.passed is False
    assert "NIFTI_UNREADABLE" in [issue.code for issue in result.errors]


def test_nan_in_nifti_is_warning_not_error(tmp_path: Path) -> None:
    data = np.zeros((3, 3, 3), dtype=np.float32)
    data[0, 0, 0] = float("nan")
    submission = tmp_path / "nan_team"
    submission.mkdir()
    img = nib.Nifti1Image(data, affine=np.eye(4))
    nib.save(img, str(submission / "Ktrans_map.nii.gz"))
    (submission / "README.md").write_text("# readme\n", encoding="utf-8")
    (submission / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (submission / "run.py").write_text("print('ok')\n", encoding="utf-8")

    result = validate_submission(submission, challenge_type="dce", output_dir=tmp_path / "validation")

    assert "NIFTI_UNREADABLE" not in [issue.code for issue in result.errors]
    assert "NIFTI_WARNING" in [issue.code for issue in result.warnings]
    assert result.passed is True
