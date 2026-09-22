"""Acceptance coverage for every local intake shape used by the web UI.

These tests exercise the HTTP endpoints, not just ingestion helpers. The
1,800-file case is intentionally made of valid, tiny NIfTI images: it checks
multipart parsing, path preservation, indexing, completeness, and deep NIfTI
validation without adding a large binary fixture to the repository.
"""

from __future__ import annotations

import gzip
import sys
from pathlib import Path
from typing import Generator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from osipi_pipeline.testing.synthetic import (  # noqa: E402
    build_dce_submission,
    nifti_bytes,
    zip_directory,
)


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> Generator[TestClient, None, None]:
    repo_root = Path(__file__).resolve().parents[1]
    for extra in (str(repo_root / "src"), str(repo_root / "backend")):
        if extra not in sys.path:
            sys.path.insert(0, extra)

    import services.path_config as pc

    mapping = {
        "INCOMING_DIR": tmp_path / "incoming",
        "EXTRACTED_DIR": tmp_path / "extracted",
        "OUTPUTS_DIR": tmp_path / "outputs",
        "REFERENCE_DATA_DIR": tmp_path / "reference",
        "SCORING_DIR": tmp_path / "scoring",
        "SCORING_OUTPUTS_DIR": tmp_path / "scoring_outputs",
        "SCORING_RESULTS_DIR": tmp_path / "scoring_outputs",
        "OSIPI_TF62_DIR": tmp_path / "tf62",
        "CODECOLLECTION_DIR": tmp_path / "codecollection",
        "VALIDATION_SUBDIR": tmp_path / "outputs" / "validation",
        "PREVIEW_ROOT": tmp_path / "outputs" / "previews",
        "SCORING_PACKAGES_DIR": tmp_path / "scoring_packages",
        "SCORING_ACTIVE_CONFIG": tmp_path / "scoring" / "active.json",
    }
    for attr, value in mapping.items():
        monkeypatch.setattr(pc, attr, value, raising=False)
    for module in list(sys.modules.values()):
        for attr, value in mapping.items():
            if hasattr(module, attr):
                monkeypatch.setattr(module, attr, value, raising=False)
    for directory in set(mapping.values()):
        if Path(directory).suffix != ".json":
            Path(directory).mkdir(parents=True, exist_ok=True)

    import backend.main as app_module

    with TestClient(app_module.app, raise_server_exceptions=True) as test_client:
        yield test_client


_NIFTI_GZ = gzip.compress(nifti_bytes((1.0,) * 8, (2, 2, 2)))


def _asl_files(root: str, count: int) -> list[tuple[str, tuple[str, bytes, str]]]:
    files = []
    for index in range(count):
        participant = index // 2 + 1
        map_name = "cbf" if index % 2 == 0 else "att"
        relative = (
            f"{root}/sub-{participant:04d}/results/maps/"
            f"sub-{participant:04d}_{map_name}.nii.gz"
        )
        files.append(("files", (relative, _NIFTI_GZ, "application/gzip")))
    return files


def _upload_folder(client: TestClient, files, *, methods: str = "no") -> dict:
    response = client.post(
        "/api/upload-folder-batch",
        files=files,
        data={"methods_document": methods},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    assert body["batch"] is False
    assert body["submission_id"]
    return body


@pytest.mark.parametrize("count", [1, 2, 10, 1800])
def test_asl_individual_file_matrix(client: TestClient, count: int) -> None:
    upload = _upload_folder(client, _asl_files(f"qa_asl_{count}", count))
    assert upload["file_count"] == count
    assert upload["nifti_count"] == count
    if count >= 2:
        assert upload["detected_challenge_type"] == "asl"

    response = client.post("/api/validate", json={
        "submission_id": upload["submission_id"],
        "challenge_type": "asl",
        "mode": "result_only",
        "team_name": f"ASL {count}-file acceptance",
        "contact_email": "qa@example.org",
    })
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["submission_id"] == upload["submission_id"]
    assert result["nifti_count"] == count
    assert len(result["nifti_summary"]) == count
    assert result["team_name"] == f"ASL {count}-file acceptance"
    assert result["contact_email"] == "qa@example.org"
    assert result["challenge_type"] == "ASL"
    if count == 1:
        assert result["passed"] is False
        assert any(issue["code"] == "REQUIRED_MAP_MISSING" for issue in result["errors"])
    else:
        assert result["passed"] is True, result["errors"]
        assert result["errors"] == []


def test_asl_zip_uses_the_same_metadata_contract(client: TestClient, tmp_path: Path) -> None:
    source = tmp_path / "asl_zip_acceptance"
    for _field, (relative, payload, _media_type) in _asl_files(source.name, 2):
        destination = tmp_path / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)
    archive = zip_directory(source, tmp_path / "asl_zip_acceptance.zip")
    response = client.post(
        "/api/upload-batch",
        files={"file": (archive.name, archive.read_bytes(), "application/zip")},
        data={"methods_document": "no"},
    )
    assert response.status_code == 200, response.text
    upload = response.json()
    assert upload["success"] is True
    assert upload["batch"] is False
    assert upload["file_count"] == 2
    assert upload["nifti_count"] == 2
    assert upload["detected_challenge_type"] == "asl"
    assert upload["methods_document_declared"] == "no"


def _multipart_tree(root: Path) -> list[tuple[str, tuple[str, bytes, str]]]:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            media_type = "application/gzip" if path.name.endswith(".gz") else "text/plain"
            files.append(("files", (str(path.relative_to(root.parent)), path.read_bytes(), media_type)))
    return files


def test_complete_dce_folder_and_zip_paths(client: TestClient, tmp_path: Path) -> None:
    source = build_dce_submission(tmp_path / "source", "qa_dce_complete")

    folder_upload = _upload_folder(client, _multipart_tree(source), methods="yes")
    assert folder_upload["detected_challenge_type"] == "dce"
    folder_validation = client.post("/api/validate", json={
        "submission_id": folder_upload["submission_id"],
        "challenge_type": "dce",
        "mode": "result_only",
        "team_name": "DCE folder acceptance",
        "contact_email": "qa@example.org",
    }).json()
    assert folder_validation["passed"] is True, folder_validation["errors"]
    assert folder_validation["errors"] == []
    assert folder_validation["nifti_count"] == 64

    archive = zip_directory(source, tmp_path / "qa_dce_complete.zip")
    response = client.post(
        "/api/upload-batch",
        files={"file": ("qa_dce_zip_complete.zip", archive.read_bytes(), "application/zip")},
        data={"methods_document": "yes"},
    )
    assert response.status_code == 200, response.text
    zip_upload = response.json()
    assert zip_upload["detected_challenge_type"] == "dce"
    assert zip_upload["nifti_count"] == 64
    assert zip_upload["methods_document_declared"] == "yes"

