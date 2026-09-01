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


# ── Threads help small files and hurt large ones ──────────────────────────
#
# Measured on the DCE challenge lead's real submission: four threads over
# sixteen 3-D parameter maps is 2.5x faster than serial, while four threads
# over four 4-D concentration curves is 1.8x SLOWER. gzip decompression holds
# the GIL through the many small reads nibabel makes, so the threads take
# turns while each holds a decompression buffer. Applying one worker count to
# both made a real 60-scan submission take about 16 minutes; reading the large
# files one at a time takes about 9.

import time  # noqa: E402


def test_a_large_file_is_recognised_by_size(tmp_path) -> None:
    from osipi_pipeline.validation.nifti_validator import LARGE_FILE_BYTES, _is_large

    small = tmp_path / "ktrans.nii.gz"
    small.write_bytes(b"0" * 1024)
    large = tmp_path / "ct.nii.gz"
    large.write_bytes(b"0" * (LARGE_FILE_BYTES + 1))

    assert _is_large(small) is False
    assert _is_large(large) is True
    # A real 3-D map is ~50 KB and a real 4-D curve ~8 MB on disk, so the
    # threshold has to sit between them with room to spare.
    assert 64 * 1024 < LARGE_FILE_BYTES < 8 * 1024 * 1024


def test_a_missing_file_is_not_called_large(tmp_path) -> None:
    from osipi_pipeline.validation.nifti_validator import _is_large

    assert _is_large(tmp_path / "gone.nii.gz") is False


def _sized(tmp_path, name: str, payload: int):
    path = tmp_path / name
    path.write_bytes(b"0" * payload)
    return path


def test_the_batch_is_split_by_size(tmp_path, monkeypatch) -> None:
    """Large and small files take different routes, which is the whole point.

    Small files go through the thread pool, where threads measurably help.
    Large ones go to `_validate_large_files`, which runs them in separate
    processes because gzip's hold on the GIL makes threads counterproductive
    there. This asserts the split itself; the concurrency of each half is
    covered separately.
    """
    from osipi_pipeline.validation import nifti_validator as nv

    routed: dict[str, list[str]] = {"large": [], "small": []}

    def fake_large(paths, *, force_refresh, quick, workers):
        routed["large"] = [Path(p).name for p in paths]
        return [{"file_path": str(p), "valid": True, "errors": [], "warnings": []}
                for p in paths]

    def fake_check(path, **_kw):
        routed["small"].append(Path(path).name)
        return {"file_path": str(path), "valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(nv, "_validate_large_files", fake_large)
    monkeypatch.setattr(nv, "_validate_single_cached", fake_check)

    paths = [_sized(tmp_path, f"small{i}.nii.gz", 1024) for i in range(6)]
    paths += [_sized(tmp_path, f"big{i}.nii.gz", nv.LARGE_FILE_BYTES + 1) for i in range(3)]

    results = nv.validate_nifti_files(paths, force_refresh=True, workers=4)

    assert len(results) == len(paths)
    assert sorted(routed["large"]) == ["big0.nii.gz", "big1.nii.gz", "big2.nii.gz"]
    assert sorted(routed["small"]) == [f"small{i}.nii.gz" for i in range(6)]


def test_small_files_are_still_read_in_parallel(tmp_path, monkeypatch) -> None:
    """Threads are a real 2.5x win on 3-D maps and must not be lost."""
    import threading

    from osipi_pipeline.validation import nifti_validator as nv

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_check(path, **_kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        return {"file_path": str(path), "valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(nv, "_validate_single_cached", fake_check)
    paths = [_sized(tmp_path, f"small{i}.nii.gz", 1024) for i in range(6)]
    paths += [_sized(tmp_path, f"big{i}.nii.gz", nv.LARGE_FILE_BYTES + 1) for i in range(2)]

    nv.validate_nifti_files(paths, force_refresh=True, workers=4)
    assert peak > 1, "small files were not read in parallel"


def test_results_stay_in_input_order_when_the_batch_is_split(tmp_path, monkeypatch) -> None:
    """Splitting the batch must not reorder it; callers zip results to inputs."""
    from osipi_pipeline.validation import nifti_validator as nv

    monkeypatch.setattr(
        nv, "_validate_single_cached",
        lambda path, **_kw: {"file_path": str(path), "valid": True,
                             "errors": [], "warnings": []},
    )
    paths = []
    for i in range(8):
        payload = (nv.LARGE_FILE_BYTES + 1) if i % 3 == 0 else 1024
        paths.append(_sized(tmp_path, f"f{i}.nii.gz", payload))

    results = nv.validate_nifti_files(paths, force_refresh=True, workers=4)
    assert [r["file_path"] for r in results] == [str(p) for p in paths]


def test_a_batch_of_only_small_files_is_fully_parallel(tmp_path, monkeypatch) -> None:
    """The split must not cost anything when there is nothing large."""
    import threading

    from osipi_pipeline.validation import nifti_validator as nv

    live = 0
    peak = 0
    lock = threading.Lock()

    def fake_check(path, **_kw):
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        time.sleep(0.02)
        with lock:
            live -= 1
        return {"file_path": str(path), "valid": True, "errors": [], "warnings": []}

    monkeypatch.setattr(nv, "_validate_single_cached", fake_check)
    paths = [_sized(tmp_path, f"s{i}.nii.gz", 1024) for i in range(8)]

    nv.validate_nifti_files(paths, force_refresh=True, workers=4)
    assert peak > 1


# ── Large files go to processes, not threads ──────────────────────────────
#
# Measured on the DCE lead's real 1 GB concentration curves: four threads are
# 1.8x SLOWER than serial because gzip holds the GIL through nibabel's many
# small reads, while four processes are 3.3x FASTER because they do not share
# one. On a 60-scan submission that is ~2 minutes of validation instead of ~9.

def test_large_files_are_read_in_separate_processes(tmp_path, monkeypatch) -> None:
    from osipi_pipeline.validation import nifti_validator as nv

    used = {}

    class RecordingPool:
        def __init__(self, max_workers=None):
            used["workers"] = max_workers

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def map(self, fn, payloads):
            return [fn(p) for p in payloads]

    monkeypatch.setattr(nv, "ProcessPoolExecutor", RecordingPool)
    monkeypatch.setattr(
        nv, "_validate_single_cached",
        lambda path, **_kw: {"file_path": str(path), "valid": True,
                             "errors": [], "warnings": []},
    )
    paths = [_sized(tmp_path, f"big{i}.nii.gz", nv.LARGE_FILE_BYTES + 1) for i in range(3)]
    results = nv.validate_nifti_files(paths, force_refresh=True, workers=4)

    assert used.get("workers") == 3, "the process pool was not used for large files"
    assert [r["file_path"] for r in results] == [str(p) for p in paths]


def test_a_process_pool_that_cannot_start_falls_back_to_serial(
    tmp_path, monkeypatch,
) -> None:
    """Some sandboxes and frozen builds forbid subprocesses.

    Validation must still complete, just more slowly, rather than failing.
    """
    from osipi_pipeline.validation import nifti_validator as nv

    def refuse(*_args, **_kwargs):
        raise OSError("subprocesses are not permitted here")

    monkeypatch.setattr(nv, "ProcessPoolExecutor", refuse)
    monkeypatch.setattr(
        nv, "_validate_single_cached",
        lambda path, **_kw: {"file_path": str(path), "valid": True,
                             "errors": [], "warnings": []},
    )
    paths = [_sized(tmp_path, f"big{i}.nii.gz", nv.LARGE_FILE_BYTES + 1) for i in range(3)]
    results = nv.validate_nifti_files(paths, force_refresh=True, workers=4)

    assert len(results) == 3
    assert all(r["valid"] for r in results)


def test_one_large_file_needs_no_pool_at_all(tmp_path, monkeypatch) -> None:
    """Starting workers to do one thing costs more than doing it."""
    from osipi_pipeline.validation import nifti_validator as nv

    def fail(*_args, **_kwargs):
        raise AssertionError("a pool was started for a single file")

    monkeypatch.setattr(nv, "ProcessPoolExecutor", fail)
    monkeypatch.setattr(
        nv, "_validate_single_cached",
        lambda path, **_kw: {"file_path": str(path), "valid": True,
                             "errors": [], "warnings": []},
    )
    only = _sized(tmp_path, "big.nii.gz", nv.LARGE_FILE_BYTES + 1)
    assert len(nv.validate_nifti_files([only], force_refresh=True, workers=4)) == 1


def test_the_worker_payload_survives_pickling(tmp_path) -> None:
    """A closure or a keyword call cannot cross a process boundary."""
    import pickle

    from osipi_pipeline.validation import nifti_validator as nv

    payload = (str(tmp_path / "x.nii.gz"), True, False)
    assert pickle.loads(pickle.dumps(payload)) == payload
    assert pickle.loads(pickle.dumps(nv._check_one)) is nv._check_one
