#!/usr/bin/env python3
"""Measure how the pipeline behaves as a submission grows.

Grows a submission by number of scans and map size, and records wall time
and peak memory for ingestion, validation and analysis.

    python3 scripts/benchmark_scale.py                 # the default sweep
    python3 scripts/benchmark_scale.py --quick         # a fast sanity run
    python3 scripts/benchmark_scale.py --json out.json

Voxel values are a deterministic ramp. No real or private data is involved,
and nothing is written outside a temporary directory.
"""

from __future__ import annotations

import argparse
import json

import sys
import tempfile
import time
import tracemalloc
from contextlib import contextmanager
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT / "backend"), str(REPO_ROOT / "src")]

import numpy as np  # noqa: E402
import nibabel as nib  # noqa: E402

from osipi_pipeline.ingestion.manifest import refresh_manifest  # noqa: E402
from osipi_pipeline.validation.validate import validate_submission  # noqa: E402

DEFAULT_SWEEP = [
    (1, 64, 0), (4, 64, 0), (16, 64, 0), (4, 128, 0), (16, 128, 0),
    # Roughly the shape of a real DCE submission: a 4-D curve per scan.
    (1, 64, 40), (4, 64, 40), (2, 96, 157),
]
QUICK_SWEEP = [(1, 32, 0), (4, 32, 0), (1, 32, 20)]


@contextmanager
def measured(label: str, results: dict):
    """Time a step and record its peak allocation."""
    tracemalloc.start()
    start = time.perf_counter()
    try:
        yield
    finally:
        elapsed = time.perf_counter() - start
        _current, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        results[label] = {"seconds": round(elapsed, 3),
                          "peak_mb": round(peak / 1024 / 1024, 1)}


def build_submission(root: Path, scans: int, side: int,
                     timepoints: int = 0) -> tuple[int, float]:
    """Write ``scans`` scans of two maps each."""
    rng = np.random.default_rng(0)
    shape = (side, side, side)
    written = 0
    for index in range(scans):
        scan = (root / "Clinical" / f"Participant{index + 1}"
                / "Site1" / "Repeat1")
        scan.mkdir(parents=True, exist_ok=True)
        for name in ("cbf", "att"):
            data = (rng.random(shape) * 100).astype(np.float32)
            nib.save(nib.Nifti1Image(data, np.eye(4)), str(scan / f"{name}.nii.gz"))
            written += 1
        if timepoints > 0:
            curve = (rng.random((*shape, timepoints)) * 0.2).astype(np.float32)
            nib.save(nib.Nifti1Image(curve, np.eye(4)),
                     str(scan / "modelled_st.nii.gz"))
            written += 1
            del curve
    size = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    return written, round(size / 1024 / 1024, 1)


def run_case(scans: int, side: int, timepoints: int = 0) -> dict:
    """One point on the sweep, in its own temporary directory."""
    with tempfile.TemporaryDirectory(prefix="osipi-bench-") as tmp:
        root = Path(tmp)
        submission = root / "extracted" / "bench"
        submission.mkdir(parents=True)

        results: dict = {"scans": scans, "voxels_per_side": side,
                         "timepoints": timepoints}
        with measured("write", results):
            files, megabytes = build_submission(submission, scans, side, timepoints)
        results["files"] = files
        results["input_mb"] = megabytes

        with measured("ingest", results):
            refresh_manifest(submission, submission_id="bench", challenge_type="asl")

        with measured("validate", results):
            validate_submission(submission, challenge_type="asl",
                                output_dir=root / "validation")
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--quick", action="store_true",
                        help="A small sweep, for checking the script itself")
    parser.add_argument("--json", type=Path, default=None,
                        help="Also write the raw measurements here")
    args = parser.parse_args(argv)

    sweep = QUICK_SWEEP if args.quick else DEFAULT_SWEEP
    rows = []
    header = (f"{'scans':>6} {'side':>5} {'time':>5} {'files':>6} {'input MB':>9} "
              f"{'ingest s':>9} {'validate s':>11} {'peak MB':>8}")
    print(header)
    print("-" * len(header))
    for scans, side, timepoints in sweep:
        row = run_case(scans, side, timepoints)
        rows.append(row)
        peak = max(row[step]["peak_mb"] for step in ("ingest", "validate"))
        print(f"{row['scans']:>6} {row['voxels_per_side']:>5} "
              f"{row['timepoints'] or '-':>5} {row['files']:>6} "
              f"{row['input_mb']:>9} {row['ingest']['seconds']:>9} "
              f"{row['validate']['seconds']:>11} {peak:>8}")

    if len(rows) >= 2:
        first, last = rows[0], rows[-1]
        def grew(step: str) -> str:
            base = first[step]["seconds"]
            return "not measurable" if base < 0.01 else f"{last[step]['seconds'] / base:.1f}x"
        print(f"\n  From {first['input_mb']} MB to {last['input_mb']} MB "
              f"({last['input_mb'] / max(first['input_mb'], 0.1):.0f}x the input):")
        print(f"    ingestion  {grew('ingest')}")
        print(f"    validation {grew('validate')}")
        peaks = [max(r[s]['peak_mb'] for s in ('ingest', 'validate')) for r in rows]
        print(f"    peak memory stayed between {min(peaks)} and {max(peaks)} MB")

    if args.json:
        args.json.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print(f"  raw measurements written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
