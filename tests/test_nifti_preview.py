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
    assert item["orientation"] == "RAS"
    for plane in previews.PREVIEW_PLANES:
        png = previews.get_preview_png_path("sub-001", item["map_id"], plane)
        assert png.exists()
        assert png.read_bytes().startswith(b"\x89PNG")


def test_compatible_mask_generates_private_safe_overlay(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.arange(27).reshape(3, 3, 3))
    mask = preview_workspace / "extracted" / "sub-001" / "reference" / "masks" / "gm_mask.nii.gz"
    _write_nifti(mask, np.ones((3, 3, 3)))

    item = _manifest(preview_workspace)["maps"][0]

    assert item["mask_overlay_status"] == "available"
    # The label the scoring tables use, so an overlay can be matched to a row.
    # It used to be derived from the filename, which both read badly and put
    # an organiser asset name on screen.
    assert item["mask_overlay_label"] == "gray matter"
    assert len(item["mask_overlays"]) == 1

    public = str(previews.public_preview_item(item))
    assert str(mask) not in public
    # The filename must not survive slugified into the overlay id either.
    assert "gm_mask" not in public and "gm-mask" not in public
    overlay = previews.get_preview_png_path("sub-001", item["map_id"], "mask-overlay")
    assert overlay.read_bytes().startswith(b"\x89PNG")


def test_all_compatible_masks_get_distinct_selectable_overlays(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.arange(27).reshape(3, 3, 3))
    mask_root = preview_workspace / "extracted" / "sub-001" / "reference" / "masks"
    for index, name in enumerate(("gm_mask.nii.gz", "wm_mask.nii.gz", "lesion_roi_mask.nii.gz"), 1):
        data = np.zeros((3, 3, 3), dtype=np.float32)
        data[index - 1, :, :] = 1
        _write_nifti(mask_root / name, data)

    item = _manifest(preview_workspace)["maps"][0]

    assert [overlay["label"] for overlay in item["mask_overlays"]] == [
        "gray matter", "lesion", "white matter",
    ]
    assert len({overlay["plane"] for overlay in item["mask_overlays"]}) == 3
    # Distinct ids must come from the digest, not from the filenames, or
    # distinctness would be bought by leaking three mask names.
    for overlay in item["mask_overlays"]:
        for fragment in ("gm", "wm", "lesion", "mask"):
            assert fragment not in overlay["plane"].removeprefix("mask-overlay-"), \
                f"the overlay id carries {fragment!r} from the filename"
    for overlay in item["mask_overlays"]:
        png = previews.get_preview_png_path("sub-001", item["map_id"], overlay["plane"])
        assert png.read_bytes().startswith(b"\x89PNG")
    assert "source_path" not in previews.public_preview_item(item)


def test_misoriented_mask_is_not_overlaid(preview_workspace: Path) -> None:
    _write_nifti(_result_path(preview_workspace), np.arange(27).reshape(3, 3, 3))
    mask = preview_workspace / "extracted" / "sub-001" / "reference" / "masks" / "gm_mask.nii.gz"
    mask.parent.mkdir(parents=True, exist_ok=True)
    affine = np.eye(4)
    affine[0, 0] = -1
    nib.save(nib.Nifti1Image(np.ones((3, 3, 3), dtype=np.float32), affine), str(mask))

    item = _manifest(preview_workspace)["maps"][0]

    assert item["mask_overlay_status"] == "no_compatible_mask"
    assert item["mask_overlay_url"] is None


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


# ---------------------------------------------------------------------------
# Parameter Map Previews: gallery must show only 3-D recognized parameter maps
# ---------------------------------------------------------------------------

def _lena_style_manifest(workspace: Path) -> dict:
    """4-D ASL input (Unknown) + 3-D Perfmap (CBF) + 3-D ATTmap (ATT)."""
    _write_nifti(_result_path(workspace, "sub-001_acq-001_asl_32float.nii.gz"),
                 np.ones((3, 3, 2, 3), dtype=np.float32))
    _write_nifti(_result_path(workspace, "sub-001_acq-002_Perfmap_32float.nii.gz"),
                 np.arange(18, dtype=np.float32).reshape(3, 3, 2))
    _write_nifti(_result_path(workspace, "sub-001_acq-002_ATTmap_32float.nii.gz"),
                 np.arange(18, dtype=np.float32).reshape(3, 3, 2))
    return _manifest(workspace)


def test_lena_gallery_has_exactly_cbf_and_att(preview_workspace: Path) -> None:
    manifest = _lena_style_manifest(preview_workspace)
    params = [m for m in manifest["maps"] if m.get("is_parameter_map")]
    assert {m["detected_map_type"] for m in params} == {"CBF", "ATT"}
    assert len(params) == 2
    assert manifest["parameter_map_count"] == 2


def test_no_unknown_parameter_map_card(preview_workspace: Path) -> None:
    manifest = _lena_style_manifest(preview_workspace)
    for m in manifest["maps"]:
        if m.get("is_parameter_map"):
            assert str(m["detected_map_type"]) not in ("Unknown", "", "Mixed/Other")


def test_4d_asl_file_kept_but_not_a_parameter_map(preview_workspace: Path) -> None:
    manifest = _lena_style_manifest(preview_workspace)
    four_d = [m for m in manifest["maps"] if len([d for d in m.get("shape") or [] if d]) == 4]
    assert four_d, "4D ASL file must remain listed (for Technical Details / download)"
    f = four_d[0]
    assert f["is_parameter_map"] is False
    assert f["file_role"] == "fitted_model"
    assert f["role_label"] == "4D ASL data"
    assert f.get("download_url")             # still available for download


def test_masks_and_reference_not_in_submission_gallery(preview_workspace: Path) -> None:
    _write_nifti(preview_workspace / "extracted" / "sub-001" / "reference" / "maps" / "sub-001_cbf.nii.gz",
                 np.ones((3, 3, 2), dtype=np.float32))
    _write_nifti(preview_workspace / "extracted" / "sub-001" / "reference" / "masks" / "gray_matter.nii.gz",
                 np.ones((3, 3, 2), dtype=np.float32))
    manifest = _lena_style_manifest(preview_workspace)
    names = {m["file_name"].lower() for m in manifest["maps"]}
    assert not any("mask" in n or "gray" in n for n in names)
    # the reference copy is not previewed as a submitted parameter map
    assert not any(m.get("source_path", "").replace("\\", "/").find("/reference/") >= 0 for m in manifest["maps"])


def test_parameter_map_preview_and_download_still_work(preview_workspace: Path) -> None:
    manifest = _lena_style_manifest(preview_workspace)
    cbf = next(m for m in manifest["maps"] if m["detected_map_type"] == "CBF")
    for plane in previews.PREVIEW_PLANES:
        png = previews.get_preview_png_path("sub-001", cbf["map_id"], plane)
        assert png.exists() and png.read_bytes().startswith(b"\x89PNG")
    dl = previews.get_preview_download_path("sub-001", cbf["map_id"])
    assert Path(dl).exists()
