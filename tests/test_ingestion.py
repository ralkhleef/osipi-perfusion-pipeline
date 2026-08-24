"""Tests for the ingestion stage.

The tests use tiny fake submissions so the repo stays lightweight and does not
need real MRI challenge data.
"""

from __future__ import annotations

import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from osipi_pipeline.ingestion import sources
from osipi_pipeline.ingestion.detector import detect_challenge_type
from osipi_pipeline.ingestion.ingest import ingest_submission
from osipi_pipeline.ingestion.manifest import build_manifest, save_manifest
from osipi_pipeline.ingestion.sources import resolve_source


def test_detects_dce_from_submission_names(tmp_path: Path) -> None:
    """DCE detection should find DCE keywords in file and folder names."""

    submission = tmp_path / "team_alpha_DCE"
    submission.mkdir()
    (submission / "Ktrans_map.nii.gz").write_text("fake nifti", encoding="utf-8")
    (submission / "parameters_kep.json").write_text("{}", encoding="utf-8")

    assert detect_challenge_type(submission) == "dce"


def test_detects_asl_from_submission_names(tmp_path: Path) -> None:
    """ASL detection should find ASL keywords in file and folder names."""

    submission = tmp_path / "asl_submission"
    submission.mkdir()
    (submission / "CBF_map.nii.gz").write_text("fake nifti", encoding="utf-8")
    (submission / "arterial spin labeling notes.txt").write_text("notes", encoding="utf-8")

    assert detect_challenge_type(submission) == "asl"


def test_detects_asl_from_common_perfmap_and_attmap_filenames(tmp_path: Path) -> None:
    """Neutral folders should still resolve from the submitted map set."""

    submission = tmp_path / "team_alpha"
    submission.mkdir()
    (submission / "sub-001_acq-002_Perfmap_32float.nii.gz").write_text(
        "fake nifti", encoding="utf-8"
    )
    (submission / "sub-001_acq-002_ATTmap_32float.nii.gz").write_text(
        "fake nifti", encoding="utf-8"
    )

    assert detect_challenge_type(submission) == "asl"


def test_detects_dsc_from_complete_map_set_in_neutral_folder(tmp_path: Path) -> None:
    submission = tmp_path / "team_beta"
    submission.mkdir()
    for map_name in ("CBV", "CBF", "MTT"):
        (submission / f"sub-001_{map_name}.nii").write_text("fake nifti", encoding="utf-8")

    assert detect_challenge_type(submission) == "dsc"


def test_shared_cbf_map_alone_does_not_guess_asl_or_dsc(tmp_path: Path) -> None:
    """CBF belongs to ASL and DSC, so one neutral filename is ambiguous."""

    submission = tmp_path / "team_gamma"
    submission.mkdir()
    (submission / "sub-001_CBF.nii.gz").write_text("fake nifti", encoding="utf-8")

    assert detect_challenge_type(submission) == "unknown"


def test_ingests_folder_and_writes_manifest_outputs(tmp_path: Path) -> None:
    """Folder ingestion should copy files and write both manifest formats."""

    submission = _make_dce_submission(tmp_path / "team_alpha")
    manifests_dir = tmp_path / "manifests"

    manifest = ingest_submission(
        submission,
        extracted_root=tmp_path / "extracted",
        manifests_dir=manifests_dir,
    )

    assert manifest.submission_id == "team_alpha"
    assert manifest.challenge_type == "dce"
    assert Path(manifest.extracted_path) == tmp_path / "extracted" / "dce" / "team_alpha"
    assert manifest.file_count == 6
    assert "maps/Ktrans_map.nii.gz" in manifest.nifti_files
    assert "metadata/submission.json" in manifest.metadata_files
    assert "src/process.py" in manifest.code_files
    assert "Dockerfile" in manifest.docker_files
    assert "README.md" in manifest.readme_files
    assert (manifests_dir / "dce_team_alpha_manifest.json").exists()
    assert (manifests_dir / "dce_team_alpha_manifest.csv").exists()


def test_ingests_zip_submission(tmp_path: Path) -> None:
    """Zip ingestion should extract files before building the manifest."""

    source = _make_dce_submission(tmp_path / "team_beta")
    zip_path = tmp_path / "team_beta.zip"
    with zipfile.ZipFile(zip_path, "w") as zip_file:
        for file_path in source.rglob("*"):
            if file_path.is_file():
                zip_file.write(file_path, file_path.relative_to(source))

    manifest = ingest_submission(
        zip_path,
        extracted_root=tmp_path / "extracted",
        manifests_dir=tmp_path / "manifests",
    )

    assert manifest.submission_id == "team_beta"
    assert manifest.challenge_type == "dce"
    assert Path(manifest.extracted_path).is_dir()
    assert sorted(manifest.nifti_files) == ["maps/Ktrans_map.nii.gz", "maps/vp_map.nii"]


def test_resolves_github_repository_url() -> None:
    """A GitHub URL should become a github source type."""

    source = resolve_source("https://github.com/osipi/team-alpha-submission.git")

    assert source.kind == "github"
    assert source.original == "https://github.com/osipi/team-alpha-submission.git"
    assert source.submission_id == "osipi_team-alpha-submission"


def test_ingests_github_repository_url(tmp_path: Path, monkeypatch) -> None:
    """GitHub ingestion should use the same manifest flow as local sources."""

    def fake_clone(_repo_url: str, destination: Path) -> None:
        # Avoid network access in tests by creating the cloned files ourselves.
        _make_dce_submission(destination)

    monkeypatch.setattr(sources, "_clone_github_repository", fake_clone)

    manifest = ingest_submission(
        "https://github.com/osipi/team-alpha-submission",
        extracted_root=tmp_path / "extracted",
        manifests_dir=tmp_path / "manifests",
    )

    assert manifest.submission_id == "osipi_team-alpha-submission"
    assert manifest.challenge_type == "dce"
    assert manifest.original_path == "https://github.com/osipi/team-alpha-submission"
    assert Path(manifest.extracted_path) == tmp_path / "extracted" / "dce" / "osipi_team-alpha-submission"
    assert "maps/Ktrans_map.nii.gz" in manifest.nifti_files


def test_github_clone_timeout_fails_gracefully(tmp_path: Path, monkeypatch, capsys) -> None:
    """A slow GitHub clone should stop and show a clear error."""

    def fake_run(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="git clone", timeout=sources.CLONE_TIMEOUT_SECONDS)

    monkeypatch.setattr(sources.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(sources.subprocess, "run", fake_run)

    with pytest.raises(ValueError, match="GitHub clone timed out after 120 seconds"):
        sources._clone_github_repository("https://github.com/osipi/large-submission", tmp_path / "clone")

    assert "Cloning GitHub repo..." in capsys.readouterr().out
    assert not (tmp_path / "clone").exists()


def test_manifest_creation_and_persistence(tmp_path: Path) -> None:
    """Manifest saving should produce readable JSON and CSV files."""

    submission = _make_dce_submission(tmp_path / "team_gamma")
    manifest = build_manifest(
        submission_id="team_gamma",
        challenge_type="dce",
        original_path=submission,
        extracted_path=submission,
    )
    json_path, csv_path = save_manifest(manifest, tmp_path / "manifests")

    saved = json.loads(json_path.read_text(encoding="utf-8"))
    assert saved["submission_id"] == "team_gamma"
    assert saved["file_count"] == 6
    assert sorted(saved["nifti_files"]) == ["maps/Ktrans_map.nii.gz", "maps/vp_map.nii"]
    assert csv_path.read_text(encoding="utf-8").startswith("submission_id,challenge_type")


def _make_dce_submission(root: Path) -> Path:
    """Create a tiny fake DCE submission for tests."""

    (root / "maps").mkdir(parents=True)
    (root / "metadata").mkdir()
    (root / "src").mkdir()
    (root / "maps" / "Ktrans_map.nii.gz").write_text("fake nifti", encoding="utf-8")
    (root / "maps" / "vp_map.nii").write_text("fake nifti", encoding="utf-8")
    (root / "metadata" / "submission.json").write_text("{}", encoding="utf-8")
    (root / "src" / "process.py").write_text("print('ok')\n", encoding="utf-8")
    (root / "Dockerfile").write_text("FROM python:3.12-slim\n", encoding="utf-8")
    (root / "README.md").write_text("# Submission\n", encoding="utf-8")
    return root
