"""Reading a NIfTI image in pieces rather than all at once.

Reading whole images was fine while every image was 3-D. The DCE challenge
sends 4-D concentration curves: one of the challenge lead's ``Ct.nii.gz`` files
is 8 MB on disk and 0.93 GB decompressed, 121x compressed, and reading it whole
cost 2.38 GB because the cast to float32 makes a second copy. Sixty of those
could not be validated on an ordinary machine.

On that real file the chunked reader used 0.54 GB instead of 2.38 GB, ran in
15.3s instead of 22.1s, and produced byte-identical statistics down to the last
digit of the mean.

What these tests protect is the second half of that sentence. Saving memory is
worthless if the numbers move, so most of what follows compares chunked results
against reading the array whole, including the cases where a naive
implementation would be subtly wrong: chunk boundaries that do not divide the
axis evenly, NaN and infinity spread unevenly across chunks, and a final chunk
shorter than the rest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from osipi_pipeline.validation.nifti_validator import (  # noqa: E402
    MAX_VOXELS_PER_READ,
    _last_axis_chunks,
    _streaming_stats,
)


def reference(data: np.ndarray) -> dict:
    """The statistics exactly as the whole-array code computed them."""
    finite = np.isfinite(data)
    values = data[finite]
    return {
        "nan": int(np.isnan(data).sum()),
        "inf": int(np.isinf(data).sum()),
        "n": int(values.size),
        "min": float(values.min()) if values.size else None,
        "max": float(values.max()) if values.size else None,
        "mean": float(np.mean(values, dtype=np.float64)) if values.size else None,
    }


def measured(data: np.ndarray, max_voxels: int) -> dict:
    stats = _streaming_stats(data, data.shape, max_voxels=max_voxels)
    return {
        "nan": stats.nan_count,
        "inf": stats.inf_count,
        "n": stats.finite_count,
        "min": stats.minimum if stats.finite_count else None,
        "max": stats.maximum if stats.finite_count else None,
        "mean": stats.total / stats.finite_count if stats.finite_count else None,
    }


def assert_same(data: np.ndarray, max_voxels: int) -> None:
    want, got = reference(data), measured(data, max_voxels)
    for key in ("nan", "inf", "n"):
        assert got[key] == want[key], f"{key}: {got[key]} != {want[key]}"
    for key in ("min", "max"):
        assert got[key] == want[key], f"{key}: {got[key]!r} != {want[key]!r}"
    if want["mean"] is None:
        assert got["mean"] is None
    else:
        # Summation order differs, so the last bits may. Anything looser than
        # this would let a real error through.
        assert got["mean"] == pytest.approx(want["mean"], rel=1e-12), (
            f"mean: {got['mean']!r} != {want['mean']!r}")


# ── The chunk boundaries themselves ───────────────────────────────────────

def test_chunks_cover_the_axis_exactly_once() -> None:
    """A gap loses voxels, an overlap counts them twice. Both are silent."""
    for last in (1, 2, 7, 16, 17, 100, 157):
        for budget in (1, 3, 10, 1000):
            spans = list(_last_axis_chunks((4, 4, last), budget * 16))
            covered = [i for start, stop in spans for i in range(start, stop)]
            assert covered == list(range(last)), (last, budget, spans)


def test_a_single_plane_larger_than_the_budget_still_reads() -> None:
    """Refusing to read would be worse than briefly exceeding the budget."""
    spans = list(_last_axis_chunks((1000, 1000, 5), max_voxels=1))
    assert spans == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5)]


def test_degenerate_shapes_yield_nothing_rather_than_raising() -> None:
    assert list(_last_axis_chunks((), 10)) == []
    assert list(_last_axis_chunks((4, 4, 0), 10)) == []
    assert list(_last_axis_chunks((0, 4, 4), 10)) == []


# ── The statistics are unchanged ──────────────────────────────────────────

@pytest.mark.parametrize("shape", [(4, 4, 4), (5, 6, 7, 11), (3, 3, 157), (2,)])
def test_matches_whole_array_reading(shape) -> None:
    rng = np.random.default_rng(0)
    data = (rng.random(shape) * 200 - 100).astype(np.float32)
    for budget in (1, 8, 64, 10**9):
        assert_same(data, budget)


def test_a_final_short_chunk_is_not_overweighted() -> None:
    """Averaging per-chunk means would weight a 1-wide tail like a 4-wide one.

    The tail here is deliberately extreme, so a mean-of-means implementation
    is off by a wide margin rather than in the last digits.
    """
    data = np.zeros((2, 2, 5), dtype=np.float32)
    data[..., 4] = 1000.0
    plane = 4
    assert_same(data, max_voxels=plane * 4)   # chunks of 4 then 1
    assert measured(data, plane * 4)["mean"] == pytest.approx(200.0)


def test_nan_and_infinity_split_across_chunks() -> None:
    data = np.arange(60, dtype=np.float32).reshape(2, 2, 15)
    data[..., 0] = np.nan
    data[..., 7] = np.inf
    data[..., 14] = -np.inf
    for budget in (4, 8, 40, 10**9):
        assert_same(data, budget)


def test_an_all_nan_image_reports_no_finite_values() -> None:
    data = np.full((2, 2, 9), np.nan, dtype=np.float32)
    stats = _streaming_stats(data, data.shape, max_voxels=8)
    assert stats.finite_count == 0
    assert stats.nan_count == 36


def test_the_only_finite_value_being_negative_is_not_lost() -> None:
    """min and max start from the first finite value, not from zero.

    Seeding them with 0.0 would report a maximum of 0 for an image whose
    values are all negative, which is the kind of wrong that looks plausible.
    """
    data = np.full((2, 2, 6), np.nan, dtype=np.float32)
    data[..., 3] = -5.0
    stats = _streaming_stats(data, data.shape, max_voxels=4)
    assert stats.minimum == -5.0
    assert stats.maximum == -5.0


def test_the_file_that_caused_this_is_actually_split() -> None:
    """The shape of the challenge lead's Ct.nii.gz. 250 million voxels.

    Without this, a budget raised high enough to swallow the file whole would
    leave every test above passing against a single-chunk read, and the memory
    problem would quietly return.
    """
    spans = list(_last_axis_chunks((121, 145, 91, 157), MAX_VOXELS_PER_READ))
    assert len(spans) > 1, "the default budget no longer splits a real 4-D image"
    # Whole timepoints per chunk, never a partial volume.
    assert all(stop > start for start, stop in spans)
    assert spans[-1][1] == 157


def test_a_small_3d_image_is_read_in_one_piece() -> None:
    """The ASL path must be byte for byte what it was."""
    spans = list(_last_axis_chunks((121, 145, 91), MAX_VOXELS_PER_READ))
    assert spans == [(0, 91)]


def test_reducing_over_nan_does_not_warn() -> None:
    """NaN propagation through min/max is the detection mechanism, not a slip.

    Without errstate, NumPy warns "invalid value encountered in reduce" once
    per chunk for any map that legitimately contains NaN. On a 4-D curve that
    is a wall of warnings, and it breaks anyone running with -W error.

    In a subprocess, because NumPy records a warning as already issued for a
    given source line and stays quiet afterwards. In-process this test passed
    with the guard removed, since an earlier test had already tripped it.
    """
    import subprocess
    import textwrap

    program = textwrap.dedent(f"""
        import sys, numpy as np
        sys.path[:0] = [{str(ROOT / "src")!r}]
        from osipi_pipeline.validation.nifti_validator import _streaming_stats
        # Both infinities in one chunk, so the probe sum is inf + -inf = NaN,
        # which is the case NumPy actually complains about. An all-NaN chunk
        # does not warn, so testing that would prove nothing.
        data = np.arange(60, dtype=np.float32).reshape(2, 2, 15)
        data[..., 0] = np.nan
        data[..., 7] = np.inf
        data[..., 14] = -np.inf
        stats = _streaming_stats(data, data.shape, max_voxels=10**9)
        assert stats.nan_count == 4, stats.nan_count
        assert stats.inf_count == 8, stats.inf_count
        print("ok")
    """)
    result = subprocess.run(
        [sys.executable, "-W", "error::RuntimeWarning", "-c", program],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "reducing over NaN warned, so the errstate guard is gone:\n"
        + result.stderr[-800:])
    assert "ok" in result.stdout
