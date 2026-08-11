"""Defined prototype DCE measured-vs-modelled signal RSS analysis."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from osipi_pipeline.scoring.rss_statistics import summarize_rss, voxelwise_rss

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import scoring  # noqa: E402


def test_voxelwise_rss_sums_squared_residuals_across_time() -> None:
    rss = voxelwise_rss([2, 4, 6], [1, 2, 3], (1, 1, 1, 3))
    assert rss.shape == (1, 1, 1)
    assert rss[0, 0, 0] == pytest.approx(14.0)


def test_rss_roi_summary_has_median_mean_population_sd_and_count() -> None:
    rss = np.asarray([1.0, 4.0, 9.0, 16.0]).reshape(2, 2, 1)
    summary = summarize_rss(rss, np.asarray([1, 1, 0, 0]).reshape(2, 2, 1))
    assert summary.median == pytest.approx(2.5)
    assert summary.mean == pytest.approx(2.5)
    assert summary.standard_deviation == pytest.approx(1.5)
    assert summary.voxel_count == 2


def test_nonfinite_timepoint_excludes_the_whole_voxel() -> None:
    rss = voxelwise_rss([1, np.nan, 3, 4], [1, 2, 3, 4], (2, 1, 1, 2))
    assert np.isnan(rss[0, 0, 0])
    assert rss[1, 0, 0] == pytest.approx(0.0)


def _artifact(kind: str, path: str):
    return SimpleNamespace(
        artifact_type=kind, path=path, dataset="synthetic",
        participant="1", repeat="1", site="1",
    )


def test_rss_orchestration_is_conditional_on_measured_signal(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(scoring, "submission_artifacts", lambda sid: [
        _artifact("modelled_st", "modelled_st.nii.gz")
    ])
    target = {}
    scoring._score_dce_signal_rss(target, "sub-1", "dce")
    assert target["dce_signal_rss"]["status"] == "measured_signal_not_available"
    assert target["dce_signal_rss"]["available"] is False


def test_rss_orchestration_compares_one_clear_scan_pair(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(scoring, "submission_artifacts", lambda sid: [
        _artifact("modelled_st", "modelled_st.nii.gz"),
        _artifact("measured_st", "measured_st.nii.gz"),
    ])

    def load(path):
        values = [1, 2, 3] if "modelled" in str(path) else [2, 4, 6]
        return {"shape": [1, 1, 1, 3], "values": values, "affine": None, "voxel_size": None}

    monkeypatch.setattr(scoring, "_load_nifti_values", load)
    target = {}
    scoring._score_dce_signal_rss(target, "sub-1", "dce")
    result = target["dce_signal_rss"]
    assert result["status"] == "available"
    assert result["records"][0]["whole_image"]["median"] == pytest.approx(14.0)
    assert result["methodology"]["name"] == "Residual Sum of Squares (RSS)"
    assert "deviance" in result["methodology"]["scope"]


def test_rss_accepts_a_clearly_matched_organiser_reference_signal(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(scoring, "submission_artifacts", lambda sid: [
        _artifact("modelled_st", "Synthetic_P1_Visit1_Site1_modelled_st.nii.gz"),
    ])
    reference_root = tmp_path / "reference"
    measured_path = reference_root / "Synthetic_P1_Visit1_Site1_measured_st.nii.gz"
    monkeypatch.setattr(scoring, "_nifti_file_list", lambda root: [measured_path])
    monkeypatch.setattr(scoring, "_reference_masks", lambda root: [])

    def load(path):
        values = [2, 4, 6] if "measured" in str(path) else [1, 2, 3]
        return {"shape": [1, 1, 1, 3], "values": values, "affine": None, "voxel_size": None}

    monkeypatch.setattr(scoring, "_load_nifti_values", load)
    target = {"reference_root": str(reference_root)}
    scoring._score_dce_signal_rss(target, "sub-1", "dce")
    assert target["dce_signal_rss"]["status"] == "available"
    assert target["dce_signal_rss"]["records"][0]["whole_image"]["mean"] == pytest.approx(14.0)
