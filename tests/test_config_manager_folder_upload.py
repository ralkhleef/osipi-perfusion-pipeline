from __future__ import annotations

import gzip
import io

import nibabel as nib
import numpy as np
import pytest

from osipi_pipeline.config import rules as rules_module
from services import configuration_manager_service as manager
from services import path_config as paths


@pytest.fixture()
def reference_root(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "REFERENCE_DATA_DIR", tmp_path / "reference_data")
    rules_module.clear_config_cache()
    yield tmp_path / "reference_data"
    rules_module.clear_config_cache()


def _nifti(value: float = 1.0) -> bytes:
    image = nib.Nifti1Image(np.full((3, 3, 3), value, dtype=np.float32), np.eye(4))
    raw = image.to_bytes()
    return gzip.compress(raw)


def test_ground_truth_folder_keeps_participant_site_scan(reference_root):
    entries = [
        ("P01/site_1/scan_1/Ktrans.nii.gz", _nifti(1)),
        ("P01/site_1/scan_1/Ct.nii.gz", _nifti(2)),
        ("P02/site_3/scan_2/vp.nii.gz", _nifti(3)),
    ]
    result = manager.store_private_folder("dce", "auto", "hidden_ground_truth", entries)
    assert result["counts"]["reference"] == 3
    assert (reference_root / "dce/maps/P01/site_1/scan_1/Ktrans.nii.gz").exists()
    assert (reference_root / "dce/maps/P01/site_1/scan_1/Ct.nii.gz").exists()
    assert (reference_root / "dce/maps/P02/site_3/scan_2/vp.nii.gz").exists()


def test_mask_folder_is_detected_and_kept_per_site(reference_root):
    entries = [
        ("site_1/GM_mask.nii.gz", _nifti()),
        ("site_1/WM_mask.nii.gz", _nifti()),
        ("site_2/GM_mask.nii.gz", _nifti()),
    ]
    result = manager.store_private_folder("dce", "auto", "shared_masks", entries)
    assert result["counts"]["mask"] == 3
    assert (reference_root / "dce/masks/site_1/GM_mask.nii.gz").exists()
    assert (reference_root / "dce/masks/site_2/GM_mask.nii.gz").exists()


def test_flat_mask_named_by_site_goes_under_that_site(reference_root):
    manager.store_private_folder("dce", "auto", "masks", [("site_2_WM_mask.nii.gz", _nifti())])
    assert (reference_root / "dce/masks/site_2/site_2_WM_mask.nii.gz").exists()


def test_explicit_kind_overrides_detection(reference_root):
    result = manager.store_private_folder("dce", "mask", "regions", [("site_1/GM.nii.gz", _nifti())])
    assert result["counts"]["mask"] == 1
    assert (reference_root / "dce/masks/site_1/GM.nii.gz").exists()


def test_macos_files_and_non_nifti_are_ignored(reference_root):
    entries = [
        ("__MACOSX/P01/._Ktrans.nii.gz", b"junk"),
        ("P01/site_1/scan_1/._Ktrans.nii.gz", b"junk"),
        ("P01/notes.txt", b"hello"),
        ("P01/site_1/scan_1/Ktrans.nii.gz", _nifti()),
    ]
    result = manager.store_private_folder("dce", "auto", "hidden_ground_truth", entries)
    assert result["counts"]["reference"] == 1
    assert result["skipped"] == ["P01/notes.txt"]
    assert not list(reference_root.rglob("._*"))


def test_reupload_is_unchanged_and_different_file_is_not_overwritten(reference_root):
    first = _nifti(1)
    manager.store_private_folder("dce", "auto", "gt", [("P01/site_1/scan_1/Ktrans.nii.gz", first)])
    again = manager.store_private_folder("dce", "auto", "gt", [("P01/site_1/scan_1/Ktrans.nii.gz", first)])
    assert again["unchanged"] == ["P01/site_1/scan_1/Ktrans.nii.gz"]
    clash = manager.store_private_folder("dce", "auto", "gt", [("P01/site_1/scan_1/Ktrans.nii.gz", _nifti(9))])
    assert clash["conflicts"] == ["P01/site_1/scan_1/Ktrans.nii.gz"]
    assert (reference_root / "dce/maps/P01/site_1/scan_1/Ktrans.nii.gz").read_bytes() == first


def test_unreadable_nifti_is_reported_and_not_kept(reference_root):
    result = manager.store_private_folder("dce", "auto", "gt", [("P01/bad.nii.gz", b"not a nifti")])
    assert result["unreadable"] == ["P01/bad.nii.gz"]
    assert not (reference_root / "dce/maps/P01/bad.nii.gz").exists()


@pytest.mark.parametrize("path", ["../escape.nii.gz", "P01/../../escape.nii.gz"])
def test_paths_cannot_escape_the_asset_folder(reference_root, path):
    with pytest.raises(ValueError, match="Unsafe path"):
        manager.store_private_folder("dce", "auto", "gt", [(path, _nifti())])


def test_unknown_challenge_is_rejected(reference_root):
    with pytest.raises(ValueError):
        manager.store_private_folder("nope", "auto", "gt", [("a.nii.gz", _nifti())])


def test_folder_upload_endpoint(reference_root):
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)
    data = _nifti()
    response = client.post(
        "/api/configuration-manager/assets/upload-folder",
        data={
            "challenge_type": "dce",
            "asset_kind": "auto",
            "source_folder": "shared_masks",
            "relative_paths": ["site_1/GM_mask.nii.gz", "site_1/WM_mask.nii.gz"],
        },
        files=[
            ("files", ("GM_mask.nii.gz", io.BytesIO(data), "application/gzip")),
            ("files", ("WM_mask.nii.gz", io.BytesIO(data), "application/gzip")),
        ],
    )
    assert response.status_code == 200, response.text
    assert response.json()["counts"]["mask"] == 2
    assert (reference_root / "dce/masks/site_1/WM_mask.nii.gz").exists()


def test_endpoint_requires_a_path_for_every_file(reference_root):
    from fastapi.testclient import TestClient
    import main

    client = TestClient(main.app)
    response = client.post(
        "/api/configuration-manager/assets/upload-folder",
        data={"challenge_type": "dce", "relative_paths": ["a.nii.gz", "b.nii.gz"]},
        files=[("files", ("a.nii.gz", io.BytesIO(_nifti()), "application/gzip"))],
    )
    assert response.status_code == 400


def test_measured_signal_folder_is_detected(reference_root):
    result = manager.store_private_folder("dce", "auto", "measured_signals", [("P01/site_1/scan_1/Ct.nii.gz", _nifti())])
    assert result["counts"]["measured_signal"] == 1
    assert (reference_root / "dce/signals/P01/site_1/scan_1/Ct.nii.gz").exists()
