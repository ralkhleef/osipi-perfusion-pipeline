#!/usr/bin/env python3
"""Calculate simple, input-derived DCE map summaries.

This example is not an official score. It deliberately contains no ranking,
pass/fail rule, or challenge threshold.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np


PACKAGE_NAME = "DCE Map Summary Example"
PACKAGE_VERSION = "1.1.0"


def find_niftis(folder: Path) -> list[Path]:
    """Return actual NIfTI files and ignore similarly named files."""
    return sorted(
        path for path in folder.rglob("*")
        if path.is_file()
        and path.name.lower().endswith((".nii", ".nii.gz"))
        and not path.name.startswith(".")
    )


def read_map(path: Path) -> np.ndarray | None:
    """Read one map as float64, returning None when it cannot be opened."""
    try:
        import nibabel as nib  # Imported here for package validation portability.

        return np.asarray(nib.load(str(path)).dataobj, dtype=np.float64)
    except Exception:
        return None


def describe_map(path: Path, submission_dir: Path) -> dict[str, object]:
    """Calculate descriptive values for one submitted map."""
    relative = str(path.relative_to(submission_dir))
    data = read_map(path)
    if data is None:
        return {"file": relative, "status": "unreadable"}

    finite = np.isfinite(data)
    values = data[finite]
    return {
        "file": relative,
        "status": "ok",
        "voxel_count": int(data.size),
        "finite_percent": (
            float(100.0 * finite.sum() / data.size) if data.size else 0.0
        ),
        "negative_percent": (
            float(100.0 * (values < 0).sum() / values.size)
            if values.size else 0.0
        ),
        "mean": float(values.mean()) if values.size else None,
    }


def summarise(rows: list[dict[str, object]]) -> dict[str, object]:
    """Combine per-map values using equal weighting across readable maps."""
    readable = [row for row in rows if row.get("status") == "ok"]

    def average(key: str) -> float:
        values = [float(row[key]) for row in readable if row.get(key) is not None]
        return float(np.mean(values)) if values else 0.0

    return {
        "package": PACKAGE_NAME,
        "package_version": PACKAGE_VERSION,
        "official_osipi_scoring": False,
        "status": (
            "completed" if readable and len(readable) == len(rows)
            else "completed_with_errors"
        ),
        "file_count": len(rows),
        "readable_file_count": len(readable),
        "mean_finite_percent": average("finite_percent"),
        "mean_negative_percent": average("negative_percent"),
        "mean_of_map_means": average("mean"),
        "notes": ["Descriptive example only; no score or acceptance limit."],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--submission-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path)
    args = parser.parse_args()

    submission_dir = args.submission_dir.resolve()
    files = find_niftis(submission_dir)
    if not files:
        parser.error("No NIfTI files were found in --submission-dir")

    rows = [describe_map(path, submission_dir) for path in files]
    summary = summarise(rows)
    result = {"summary": summary, "per_file": rows}

    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metrics.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8"
    )
    (args.output_dir / "results.json").write_text(
        json.dumps(summary, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(summary, indent=2))
    return 0 if summary["readable_file_count"] == summary["file_count"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
