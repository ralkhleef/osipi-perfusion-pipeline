#!/usr/bin/env python3
"""Demo scoring script for the OSIPI Perfusion Pipeline.

=============================================================
  ⚠  DEMO / TEST ONLY — NOT OFFICIAL OSIPI SCORING
=============================================================

This script outputs SYNTHETIC, RANDOMLY-GENERATED metrics
to demonstrate the scoring package interface.  It does NOT
perform any real scientific evaluation against OSIPI reference
data.

For real challenge scoring you need:
  - The official OSIPI TF6.2 challengeScoring.py
  - The OSIPI DRO Ktrans NIfTI reference maps
  - The official mask files

See README.md in this package for details.

Interface (standard call_mode):
    python scoring.py \\
        --submission-dir  <path to execution output NIfTIs>  \\
        --output-dir      <path where scoring results are written> \\
        [--reference-dir  <path to reference data (unused in demo)]

Outputs written to --output-dir:
    metrics.json   — metric values (DEMO only)
    scoring_log.txt — human-readable summary

Exit code:
    0  success
    1  error (e.g. no NIfTI files found in submission-dir)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path


def _find_niftis(directory: Path) -> list[Path]:
    return sorted(
        f for f in directory.rglob("*")
        if f.suffix in (".nii", ".gz") and f.is_file()
    )


def _demo_metric(seed_bytes: bytes, lo: float, hi: float) -> float:
    """Generate a deterministic pseudo-metric in [lo, hi] from seed bytes."""
    digest = int(hashlib.md5(seed_bytes).hexdigest(), 16)
    ratio  = (digest % 10000) / 10000.0
    return round(lo + ratio * (hi - lo), 4)


def main() -> int:
    parser = argparse.ArgumentParser(description="Demo scoring script (NOT official OSIPI scoring)")
    parser.add_argument("--submission-dir", required=True,  type=Path, help="Directory with submission NIfTI files")
    parser.add_argument("--output-dir",     required=True,  type=Path, help="Directory for scoring output files")
    parser.add_argument("--reference-dir",  required=False, type=Path, help="Reference data directory (unused in demo)")
    args = parser.parse_args()

    submission_dir: Path = args.submission_dir
    output_dir:     Path = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    # ── Find NIfTI files ──────────────────────────────────────────────────────
    nifti_files = _find_niftis(submission_dir)
    if not nifti_files:
        print(f"[demo_scoring] ERROR: No NIfTI files found in {submission_dir}", file=sys.stderr)
        return 1

    print(f"[demo_scoring] Found {len(nifti_files)} NIfTI file(s)")
    for f in nifti_files:
        print(f"  {f.name}")

    # ── Generate synthetic demo metrics ────────────────────────────────────────
    # Seed the metrics deterministically from file names so they are reproducible.
    seed = "".join(sorted(f.name for f in nifti_files)).encode()
    metrics = {
        "demo_rmse":  _demo_metric(seed + b"rmse",  0.05, 0.50),
        "demo_bias":  _demo_metric(seed + b"bias", -0.20, 0.20),
        "demo_cv":    _demo_metric(seed + b"cv",    0.01, 0.30),
        "demo_score": _demo_metric(seed + b"scor",  0.0, 100.0),
        "_demo_note": (
            "DEMO METRICS ONLY. These are synthetic, randomly-generated values "
            "for pipeline testing. They do NOT reflect any real scientific evaluation."
        ),
    }

    # ── Write outputs ──────────────────────────────────────────────────────────
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"[demo_scoring] Wrote metrics to {metrics_path}")

    log_lines = [
        "=" * 60,
        "DEMO SCORING RESULTS  (NOT OFFICIAL OSIPI SCORING)",
        "=" * 60,
        f"Scored at:       {datetime.now(timezone.utc).isoformat()}",
        f"NIfTI files:     {len(nifti_files)}",
        f"RMSE (demo):     {metrics['demo_rmse']}",
        f"Bias (demo):     {metrics['demo_bias']}",
        f"CV   (demo):     {metrics['demo_cv']}",
        f"Score (demo):    {metrics['demo_score']} / 100",
        "",
        "⚠ These are SYNTHETIC test metrics, not real scientific scores.",
        "  Replace this script with the official OSIPI TF6.2 scoring",
        "  script and reference data for actual challenge evaluation.",
    ]
    log_path = output_dir / "scoring_log.txt"
    log_path.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    print("\n".join(log_lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
