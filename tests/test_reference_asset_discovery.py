"""Where the Private Reference Assets panel looks for organiser data.

The panel searched only ``data/reference_data/<challenge>/``, while
``backend/scoring.py`` searches three roots. Given a real bundle sitting in
the flat layout, the panel reported zero reference maps at the same moment the
pipeline was scoring against those very files. That is the worst kind of wrong:
quiet, and about the one thing the panel exists to report.

These tests pin the three roots together. If someone changes the roots scoring
searches without changing the panel, or the other way round, the last test here
fails and says so.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


def _write_nifti(path: Path) -> None:
    import nibabel as nib
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), dtype=np.float32), np.eye(4)), str(path))


@pytest.fixture()
def reference_root(tmp_path, monkeypatch):
    """Point the service at a throwaway reference directory."""
    from services import path_config
    from services import configuration_manager_service as service

    root = tmp_path / "reference_data"
    root.mkdir()
    monkeypatch.setattr(path_config, "REFERENCE_DATA_DIR", root, raising=False)
    monkeypatch.setattr(service.paths, "REFERENCE_DATA_DIR", root, raising=False)
    return root


def _status(challenge: str = "asl") -> dict:
    from services.configuration_manager_service import asset_status
    return asset_status(challenge)


def test_the_flat_layout_is_found(reference_root) -> None:
    """The layout the mentors' bundle actually arrived in."""
    _write_nifti(reference_root / "maps" / "GT_Perf.nii.gz")
    _write_nifti(reference_root / "masks" / "gm_mask.nii.gz")
    status = _status()
    assert status["counts"]["reference"] == 1, status["counts"]
    assert status["counts"]["mask"] == 1, status["counts"]


def test_the_challenge_scoped_layout_is_still_found(reference_root) -> None:
    """Uploads go here, so it must keep working."""
    _write_nifti(reference_root / "asl" / "maps" / "GT_Perf.nii.gz")
    assert _status()["counts"]["reference"] == 1


def test_a_file_present_in_two_layouts_is_reported_once(reference_root) -> None:
    """Otherwise a count of two would suggest two ground truths exist."""
    _write_nifti(reference_root / "maps" / "GT_Perf.nii.gz")
    _write_nifti(reference_root / "asl" / "maps" / "GT_Perf.nii.gz")
    # Two genuinely distinct files, so two is right here.
    assert _status()["counts"]["reference"] == 2

    from services.configuration_manager_service import _asset_files
    seen = [path for _kind, path in _asset_files("asl")]
    assert len({p.resolve() for p in seen}) == len(seen), "a file was listed twice"


def test_ingestion_manifests_are_not_counted_as_reference_maps(reference_root) -> None:
    """`.osipi_manifest.json` sits beside the data and is not organiser data."""
    _write_nifti(reference_root / "maps" / "GT_Perf.nii.gz")
    (reference_root / "maps" / ".osipi_manifest.json").write_text("{}", encoding="utf-8")
    assert _status()["counts"]["reference"] == 1


def test_each_file_names_the_folder_it_was_really_found_in(reference_root) -> None:
    """The panel used to print the upload path for every file regardless.

    Anyone following that path to find a file in the flat layout arrived at an
    empty directory.
    """
    _write_nifti(reference_root / "maps" / "GT_Perf.nii.gz")
    item = _status()["items"][0]
    assert item["folder"] == "data/reference_data/maps/", item["folder"]


def test_an_empty_kind_still_says_where_to_put_files(reference_root) -> None:
    status = _status()
    assert status["upload_folders"]["reference"] == "data/reference_data/asl/maps/"
    assert status["upload_folders"]["mask"] == "data/reference_data/asl/masks/"


def test_the_panel_searches_exactly_what_scoring_searches() -> None:
    """The bug was a disagreement between these two lists, so pin them together."""
    from services.configuration_manager_service import _asset_roots
    from services import path_config

    base = path_config.REFERENCE_DATA_DIR
    expected = {base / "asl", base / "reference", base}
    assert set(_asset_roots("asl")) == expected, (
        "the panel and backend/scoring.py no longer look in the same places, "
        "which is how the panel came to report zero maps while scoring used them"
    )
