#!/usr/bin/env python3
"""Small, deterministic scoring-package example (not official scoring)."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def find_niftis(folder: Path) -> list[Path]:
    return sorted(
        path for path in folder.rglob("*")
        if path.is_file() and path.name.lower().endswith((".nii", ".nii.gz"))
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--reference-dir", type=Path)
    args = parser.parse_args()

    files = find_niftis(args.submission_dir)
    if not files:
        parser.error("No NIfTI files were found in --submission-dir")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "metrics.json").write_text(
        json.dumps({"nifti_file_count": len(files)}, indent=2) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
