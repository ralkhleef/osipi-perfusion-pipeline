from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy", reason="numpy is required for NIfTI preview tests")
nib = pytest.importorskip("nibabel", reason="nibabel is required for NIfTI preview tests")

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from services import nifti_preview_service as previews  # noqa: E402


@pytest.fixture()
def preview_workspace(tmp_path: Path, monkeypatch):
    extracted = tmp_path / "extracted"
    outputs = tmp_path / "outputs"
    monkeypatch.setattr(previews, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(previews, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(previews, "PREVIEW_ROOT", outputs / "previews")
    (extracted / "sub-001" / "results" / "maps").mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _write_nifti(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img = nib.Nifti1Image(np.asarray(data, dtype=np.float32), np.eye(4))
    nib.save(img, str(path))
    return path


def _result_path(workspace: Path, name: str = "sub-001_cbf.nii.gz") -> Path:
    return workspace / "extracted" / "sub-001" / "results" / "maps" / name


def _manifest(workspace: Path, challenge_type: str = "asl") -> dict:
    return previews.list_submission_previews("sub-001", challenge_type=challenge_type)


def test_preview_generated_for_simple_3d_nifti(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.arange(27, dtype=np.float32).reshape(3, 3, 3))

    manifest = _manifest(preview_workspace)
    item = manifest["maps"][0]

    assert item["preview_available"] is True
    assert item["preview_status"] == "preview_available"
    assert item["detected_map_type"] == "CBF"
    for plane in previews.PREVIEW_PLANES:
        png = previews.get_preview_png_path("sub-001", item["map_id"], plane)
        assert png.exists()
        assert png.read_bytes().startswith(b"\x89PNG")


def test_preview_generated_for_4d_nifti_uses_middle_volume(preview_workspace: Path) -> None:
    data = np.stack([
        np.zeros((3, 3, 3), dtype=np.float32),
        np.ones((3, 3, 3), dtype=np.float32),
        np.full((3, 3, 3), 2, dtype=np.float32),
    ], axis=3)
    _write_nifti(_result_path(preview_workspace, "sub-001_att.nii.gz"), data)

    item = _manifest(preview_workspace)["maps"][0]

    assert item["preview_available"] is True
    assert item["preview_volume_index"] == 1
    assert item["detected_map_type"] == "ATT"


def test_all_nan_file_returns_preview_unavailable(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.full((3, 3, 3), np.nan, dtype=np.float32))

    item = _manifest(preview_workspace)["maps"][0]

    assert item["preview_available"] is False
    assert item["preview_status"] == "preview_unavailable"
    assert "finite voxels" in item["preview_error"].lower()


def test_unreadable_file_returns_preview_unavailable(preview_workspace: Path) -> None:
    path = _result_path(preview_workspace)
    path.write_bytes(b"not a nifti")

    item = _manifest(preview_workspace)["maps"][0]

    assert item["preview_available"] is False
    assert item["preview_status"] == "preview_unavailable"
    assert item["preview_error"]


def test_all_zero_constant_image_is_previewable(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.zeros((3, 3, 3), dtype=np.float32))

    item = _manifest(preview_workspace)["maps"][0]

    assert item["preview_available"] is True
    assert previews.get_preview_png_path("sub-001", item["map_id"], "axial").exists()


def test_preview_png_files_are_cached_when_source_is_unchanged(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.arange(27, dtype=np.float32).reshape(3, 3, 3))
    first = _manifest(preview_workspace)["maps"][0]
    png = previews.get_preview_png_path("sub-001", first["map_id"], "axial")
    first_mtime = png.stat().st_mtime

    second = _manifest(preview_workspace)["maps"][0]
    second_mtime = previews.get_preview_png_path("sub-001", second["map_id"], "axial").stat().st_mtime

    assert second["map_id"] == first["map_id"]
    assert second_mtime == first_mtime


def test_download_endpoint_does_not_serve_private_reference_maps(preview_workspace: Path) -> None:
    fastapi = pytest.importorskip("fastapi", reason="fastapi is required for endpoint test")
    pytest.importorskip("httpx", reason="httpx is required for TestClient")
    from fastapi.testclient import TestClient  # noqa: E402
    from main import app  # noqa: E402

    _write_nifti(_result_path(preview_workspace), np.arange(27, dtype=np.float32).reshape(3, 3, 3))
    reference = preview_workspace / "extracted" / "sub-001" / "reference" / "maps" / "sub-001_cbf.nii.gz"
    _write_nifti(reference, np.ones((3, 3, 3), dtype=np.float32))

    client = TestClient(app)
    manifest = client.get("/api/submissions/sub-001/previews?challenge_type=asl")
    assert manifest.status_code == 200
    items = manifest.json()["maps"]
    assert [item["file_name"] for item in items] == ["sub-001_cbf.nii.gz"]

    result_map_id = items[0]["map_id"]
    download = client.get(f"/api/submissions/sub-001/maps/{result_map_id}/download")
    assert download.status_code == 200

    reference_map_id = previews._map_id_for_path(reference)
    blocked = client.get(f"/api/submissions/sub-001/maps/{reference_map_id}/download")
    assert blocked.status_code == 404
    assert fastapi is not None
