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
    scoring._score_signal_rss(target, "sub-1", "dce")
    assert target["signal_rss"]["status"] == "measured_signal_not_available"
    assert target["signal_rss"]["available"] is False


def test_rss_orchestration_compares_one_clear_scan_pair(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", tmp_path)
    monkeypatch.setattr(scoring, "submission_artifacts", lambda sid: [
        _artifact("modelled_st", "modelled_st.nii.gz"),
        _artifact("measured_st", "measured_st.nii.gz"),
    ])

    # 4-D signals are streamed from an array proxy rather than loaded whole, so
    # the stub supplies `dataobj`. A NumPy array is a valid proxy: it supports
    # the same `[..., start:stop]` slicing the real nibabel one does.
    def geometry(path):
        import numpy as np
        values = [1, 2, 3] if "modelled" in str(path) else [2, 4, 6]
        return {
            "shape": [1, 1, 1, 3],
            "dataobj": np.asarray(values, dtype=float).reshape(1, 1, 1, 3),
            "affine": None, "voxel_size": None,
        }

    monkeypatch.setattr(scoring, "_nifti_geometry", geometry)
    target = {}
    scoring._score_signal_rss(target, "sub-1", "dce")
    result = target["signal_rss"]
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
    monkeypatch.setattr(scoring, "masks_for_submission", lambda sid, challenge: [])

    def geometry(path):
        import numpy as np
        values = [2, 4, 6] if "measured" in str(path) else [1, 2, 3]
        return {
            "shape": [1, 1, 1, 3],
            "dataobj": np.asarray(values, dtype=float).reshape(1, 1, 1, 3),
            "affine": None, "voxel_size": None,
        }

    monkeypatch.setattr(scoring, "_nifti_geometry", geometry)
    target = {"reference_root": str(reference_root)}
    scoring._score_signal_rss(target, "sub-1", "dce")
    assert target["signal_rss"]["status"] == "available"
    assert target["signal_rss"]["records"][0]["whole_image"]["mean"] == pytest.approx(14.0)


# ── Streaming a 4-D pair must equal reading it whole ───────────────────────
#
# The in-memory path materialised the measured volume, the modelled volume and
# their residual in float64. A real DCE concentration curve from the challenge
# lead is 121 x 145 x 91 x 157: 1 GB as float32, 2 GB as float64. Scoring one
# scan pair therefore asked for roughly 6 GB and the kernel killed the process
# on an ordinary laptop, which is a hard failure with no error message.
#
# RSS sums over time, so it accumulates: only a slab of timepoints needs to be
# resident. These tests exist to guarantee that the cheaper reading strategy
# does not change a single number.

import numpy as _np  # noqa: E402

from osipi_pipeline.scoring.rss_statistics import (  # noqa: E402
    streaming_voxelwise_rss,
)


def _pair(shape, seed=3, nonfinite=False):
    rng = _np.random.default_rng(seed)
    measured = rng.normal(size=shape)
    modelled = measured + rng.normal(scale=0.3, size=shape)
    if nonfinite:
        measured[0, 0, 0, 1] = _np.nan
        modelled[1, 1, 0, 2] = _np.inf
        measured[0, 1, 0, 0] = -_np.inf
    return measured, modelled


def _same(measured, modelled, **kwargs):
    shape = measured.shape
    whole = voxelwise_rss(measured.reshape(-1), modelled.reshape(-1), shape)
    streamed = streaming_voxelwise_rss(measured, modelled, shape, **kwargs)
    assert streamed.shape == whole.shape
    _np.testing.assert_allclose(streamed, whole, rtol=1e-12, atol=1e-12,
                                equal_nan=True)
    return streamed


def test_streaming_matches_reading_the_whole_volume() -> None:
    _same(*_pair((4, 5, 3, 12)))


def test_streaming_matches_with_non_finite_voxels() -> None:
    """A voxel is NaN when any timepoint is bad, across chunk boundaries too."""
    measured, modelled = _pair((4, 5, 3, 12), nonfinite=True)
    streamed = _same(measured, modelled, max_voxels=4 * 5 * 3 * 2)
    assert _np.isnan(streamed[0, 0, 0])
    assert _np.isnan(streamed[1, 1, 0])
    assert _np.isnan(streamed[0, 1, 0])


def test_validity_is_tracked_across_chunks_not_within_one() -> None:
    """The bug a naive chunked version would have.

    A voxel whose only bad timepoint sits in the first chunk must still be NaN
    after later, entirely finite chunks are added.
    """
    shape = (2, 2, 1, 8)
    measured = _np.ones(shape)
    modelled = _np.zeros(shape)
    measured[0, 0, 0, 0] = _np.nan  # bad only in the first chunk
    streamed = streaming_voxelwise_rss(measured, modelled, shape, max_voxels=4 * 2)
    assert _np.isnan(streamed[0, 0, 0])
    assert streamed[1, 1, 0] == pytest.approx(8.0)


@pytest.mark.parametrize("max_voxels", [1, 6, 12, 60, 10**9])
def test_the_chunk_size_never_changes_the_answer(max_voxels) -> None:
    """Including budgets so small that a chunk is a single timepoint."""
    measured, modelled = _pair((3, 2, 2, 7), seed=9, nonfinite=True)
    _same(measured, modelled, max_voxels=max_voxels)


def test_a_three_dimensional_input_is_refused() -> None:
    volume = _np.zeros((3, 3, 3))
    with pytest.raises(ValueError, match="4-D"):
        streaming_voxelwise_rss(volume, volume, volume.shape)


def test_peak_memory_stays_near_the_spatial_size() -> None:
    """The property the fix exists for, measured rather than asserted by hope.

    A 4-D volume large enough that holding it whole in float64 would dwarf the
    accumulator. Reading it in slabs must allocate a small multiple of one
    slab, never a multiple of the whole volume.
    """
    shape = (40, 40, 10, 60)          # 960k voxels, 7.7 MB as float64
    measured, modelled = _pair(shape, seed=5)

    reads: list[int] = []

    class Counting:
        """An array proxy that records how much is read at a time."""

        def __init__(self, array):
            self._array = array
            self.shape = array.shape

        def __getitem__(self, item):
            chunk = self._array[item]
            reads.append(int(_np.prod(chunk.shape)))
            return chunk

    slab = 40 * 40 * 10 * 5           # five timepoints
    streamed = streaming_voxelwise_rss(
        Counting(measured), Counting(modelled), shape, max_voxels=slab,
    )
    whole = voxelwise_rss(measured.reshape(-1), modelled.reshape(-1), shape)
    _np.testing.assert_allclose(streamed, whole, rtol=1e-12, atol=1e-12)

    assert reads, "nothing was read"
    assert max(reads) <= slab, f"a single read took {max(reads)} voxels"
    # Twelve chunks of five timepoints, for each of the two inputs.
    assert len(reads) == 24
