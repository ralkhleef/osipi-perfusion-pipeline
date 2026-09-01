"""The NumPy and pure-Python metric paths must agree, voxel for voxel.

``scoring._comparison_metrics`` computes bias, MAE, RMSE, error SD, error CoV
and Pearson correlation with NumPy, and falls back to
``scoring._comparison_metrics_py`` when NumPy cannot be imported. Its docstring
claims the two paths "compute the identical statistics (verified by the
reference-scoring tests)", but no test referenced the fallback at all: it sat
at zero coverage while the claim stood. A machine without NumPy would have
scored submissions through untested arithmetic, and a divergence would have
surfaced as two reviewers disagreeing about a submission rather than as a
failing test.

These are differential tests. They assert nothing about what the right answer
*is*, only that both implementations give the same one, which is exactly the
promise the fallback makes. The edge cases are the ones that actually separate
a vectorised implementation from a loop: NaN and infinity on either side, an
ROI selector that excludes voxels, no finite overlap at all, a zero reference
mean (CoV undefined), a constant map (correlation undefined), and a single
voxel (correlation needs two).
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import scoring  # noqa: E402

pytest.importorskip("numpy", reason="parity needs the NumPy path to compare against")

NAN = float("nan")
INF = float("inf")


def assert_same(submitted, reference, selector=None) -> dict:
    """Both paths agree on every key. Returns the result for further checks."""
    fast = scoring._comparison_metrics(submitted, reference, selector)
    slow = scoring._comparison_metrics_py(submitted, reference, selector)

    assert set(fast) == set(slow), (
        f"key sets differ: NumPy-only={set(fast) - set(slow)}, "
        f"Python-only={set(slow) - set(fast)}"
    )
    for key in fast:
        a, b = fast[key], slow[key]
        if isinstance(a, float) and isinstance(b, float):
            assert math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-12), (
                f"{key}: NumPy={a!r} Python={b!r}"
            )
        else:
            assert a == b, f"{key}: NumPy={a!r} Python={b!r}"
    return fast


# ── The ordinary case ──────────────────────────────────────────────────────

def test_the_two_paths_agree_on_a_plain_comparison() -> None:
    result = assert_same([1.0, 2.0, 3.0, 4.0], [1.5, 1.5, 3.5, 3.5])
    assert result["status"] == "compared"
    assert result["voxel_count"] == 4


def test_the_two_paths_agree_inside_an_roi_selector() -> None:
    result = assert_same(
        [1.0, 2.0, 3.0, 4.0], [0.5, 2.5, 2.0, 5.0],
        [True, False, True, False],
    )
    assert result["voxel_count"] == 2
    assert result["total_voxel_count"] == 2


# ── Non-finite values, where a loop and a vectorised mask can diverge ───────

@pytest.mark.parametrize("submitted, reference", [
    ([NAN, 2.0, 3.0], [1.0, 2.0, 3.0]),
    ([1.0, 2.0, 3.0], [NAN, 2.0, 3.0]),
    ([INF, 2.0, 3.0], [1.0, 2.0, 3.0]),
    ([1.0, -INF, 3.0], [1.0, 2.0, 3.0]),
    ([NAN, INF, -INF], [NAN, INF, -INF]),
    ([NAN, 2.0, INF], [1.0, NAN, 3.0]),
])
def test_the_two_paths_agree_when_voxels_are_not_finite(submitted, reference) -> None:
    assert_same(submitted, reference)


def test_both_paths_report_the_same_reason_for_no_overlap() -> None:
    """An unusable comparison must be unavailable in both, never zero in one."""
    for submitted, reference, expected in [
        ([NAN, NAN], [1.0, 2.0], "submitted_invalid"),
        ([1.0, 2.0], [NAN, NAN], "reference_invalid"),
        ([NAN, 2.0], [1.0, NAN], "no_finite_overlap"),
    ]:
        result = assert_same(submitted, reference)
        assert result["status"] == expected
        assert result["rmse"] is None
        assert result["bias"] is None


def test_an_empty_selector_is_the_same_nothing_in_both_paths() -> None:
    result = assert_same([1.0, 2.0], [1.0, 2.0], [False, False])
    assert result["voxel_count"] == 0
    assert result["total_voxel_count"] == 0


# ── Undefined statistics stay undefined in both paths ──────────────────────

def test_a_zero_reference_mean_leaves_cov_unavailable_in_both_paths() -> None:
    """CoV divides by the reference mean; a zero mean has no ratio to report."""
    result = assert_same([1.0, -1.0, 2.0], [1.0, -1.0, 0.0])
    assert result["mean_reference"] == 0
    assert result["error_coefficient_of_variation"] is None
    assert result["rmse"] is not None  # everything else still computes


def test_a_constant_map_leaves_correlation_unavailable_in_both_paths() -> None:
    result = assert_same([2.0, 2.0, 2.0], [1.0, 3.0, 5.0])
    assert result["correlation"] is None


def test_a_single_voxel_leaves_correlation_unavailable_in_both_paths() -> None:
    """Pearson r needs two points; one voxel is not a trend."""
    result = assert_same([3.0], [1.0])
    assert result["voxel_count"] == 1
    assert result["correlation"] is None


# ── Shape handling ─────────────────────────────────────────────────────────

def test_the_two_paths_truncate_a_length_mismatch_identically() -> None:
    """Mismatched lengths should never make the paths score different voxels."""
    assert_same([1.0, 2.0, 3.0, 4.0], [1.0, 2.5], [True, True, True, True])
    assert_same([1.0, 2.0], [1.0, 2.5, 9.0, 9.0], [True, True, True, True])


# ── Randomised differential test ───────────────────────────────────────────

def test_the_two_paths_agree_across_randomised_maps() -> None:
    """Fixed seed: a failure here is reproducible, not a flake."""
    rng = random.Random(20240817)

    def value() -> float:
        roll = rng.random()
        if roll < 0.08:
            return NAN
        if roll < 0.12:
            return INF
        if roll < 0.15:
            return -INF
        if roll < 0.20:
            return 0.0
        return rng.uniform(-5.0, 10.0)

    for _ in range(200):
        size = rng.randint(1, 50)
        submitted = [value() for _ in range(size)]
        reference = [value() for _ in range(size)]
        selector = (
            None if rng.random() < 0.3
            else [rng.random() < 0.7 for _ in range(size)]
        )
        assert_same(submitted, reference, selector)


# ── The fallback is reachable, not dead code ───────────────────────────────

def test_the_fallback_is_used_when_numpy_cannot_be_imported(monkeypatch) -> None:
    """Guards the try/except that selects the path, not just the arithmetic."""
    import builtins

    real_import = builtins.__import__

    def no_numpy(name, *args, **kwargs):
        if name == "numpy":
            raise ImportError("numpy is unavailable")
        return real_import(name, *args, **kwargs)

    submitted, reference = [1.0, 2.0, 3.0, NAN], [1.5, 1.5, 3.5, 2.0]
    with_numpy = scoring._comparison_metrics(submitted, reference)

    monkeypatch.setattr(builtins, "__import__", no_numpy)
    without_numpy = scoring._comparison_metrics(submitted, reference)

    assert without_numpy == with_numpy
    assert without_numpy["status"] == "compared"
