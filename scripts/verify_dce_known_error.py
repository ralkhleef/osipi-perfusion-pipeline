#!/usr/bin/env python3
"""Check the pipeline's DCE error statistics against the challenge answer key.

The DCE lead's synthetic submission ships with ``known_error_params.json`` for
every scan. Each file records the error that was deliberately injected
(``preset``) and the error actually present in the delivered voxels
(``achieved``), per ROI. That turns "our numbers look plausible" into a claim
that can be checked: the pipeline should reproduce ``achieved``.

    python3 scripts/verify_dce_known_error.py --root /path/to/unpacked

The folder must contain the four directories as delivered:

    submission/            P01/site_1/scan_1/{Ktrans,vp,Ct}.nii.gz
    hidden_ground_truth/   same layout
    shared_masks/          site_1/{GM,WM,Hipp}_mask.nii.gz
    known_error/           P01/site_1/scan_1/known_error_params.json

Definitions used, which is the part worth arguing about rather than the code:

    bias = mean(submitted - ground truth) inside the ROI
    var  = population variance of that same difference, ddof=0

Nothing is written and nothing is uploaded. Read only.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

try:
    import nibabel as nib
except ImportError:  # pragma: no cover
    print("nibabel is required: pip install nibabel")
    raise SystemExit(2)

#: File stem for each parameter, and the key prefix used in the answer key.
PARAMETERS = (("ktrans", "Ktrans"), ("vp", "vp"))
REGIONS = ("WM", "GM", "Hipp")

#: Agreement below this is a match. The achieved values are recorded to about
#: 15 significant figures, so a correct implementation lands far inside this;
#: anything looser would hide a genuine difference in definition.
TOLERANCE = 1e-6


def load(path: Path) -> np.ndarray:
    return np.asarray(nib.load(str(path)).dataobj, dtype=np.float64)


def scan_dirs(root: Path):
    """Every scan present under submission/, in a stable order."""
    base = root / "submission"
    for participant in sorted(p for p in base.iterdir() if p.is_dir()):
        for site in sorted(s for s in participant.iterdir() if s.is_dir()):
            for scan in sorted(s for s in site.iterdir() if s.is_dir()):
                yield participant.name, site.name, scan.name


def answer_key(root: Path, participant: str, site: str, scan: str) -> dict | None:
    """The answer key for one scan.

    Some deliveries put the file at the site level and some at the scan level,
    so both are accepted rather than assuming one.
    """
    for candidate in (
        root / "known_error" / participant / site / scan / "known_error_params.json",
        root / "known_error" / participant / site / "known_error_params.json",
    ):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def check_scan(root: Path, participant: str, site: str, scan: str,
               verbose: bool) -> tuple[int, int, list[str]]:
    key = answer_key(root, participant, site, scan)
    if key is None or "achieved" not in key:
        return 0, 0, [f"{participant}/{site}/{scan}: no answer key"]
    achieved = key["achieved"]

    sub = root / "submission" / participant / site / scan
    gt = root / "hidden_ground_truth" / participant / site / scan
    masks_dir = root / "shared_masks" / site

    matched = checked = 0
    problems: list[str] = []

    for region in REGIONS:
        mask_path = masks_dir / f"{region}_mask.nii.gz"
        if not mask_path.exists() or region not in achieved:
            continue
        mask = load(mask_path) > 0.5

        # A voxel count that disagrees means the ROIs are not the same set, so
        # every statistic below would be comparing different populations.
        expected_n = achieved[region].get("n_voxels")
        if expected_n is not None and int(mask.sum()) != int(expected_n):
            problems.append(
                f"{participant}/{site}/{scan} {region}: mask has "
                f"{int(mask.sum())} voxels, the key says {expected_n}")

        for param, stem in PARAMETERS:
            sub_file, gt_file = sub / f"{stem}.nii.gz", gt / f"{stem}.nii.gz"
            if not (sub_file.exists() and gt_file.exists()):
                continue
            error = (load(sub_file) - load(gt_file))[mask]
            mine = {"bias": float(error.mean()), "var": float(error.var(ddof=0))}
            for stat, value in mine.items():
                theirs = achieved[region].get(f"{param}_{stat}")
                if theirs is None:
                    continue
                checked += 1
                rel = abs(value - theirs) / max(abs(theirs), 1e-300)
                if rel < TOLERANCE:
                    matched += 1
                    if verbose:
                        print(f"    {region:<5} {param}_{stat:<5} {theirs:>18.10g} "
                              f"matched to {rel:.1e}")
                else:
                    problems.append(
                        f"{participant}/{site}/{scan} {region} {param}_{stat}: "
                        f"key {theirs:.10g}, computed {value:.10g}, off by {rel:.2e}")
    return matched, checked, problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=0,
                        help="Check only the first N scans")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.expanduser().resolve()
    missing = [d for d in ("submission", "hidden_ground_truth", "shared_masks",
                           "known_error") if not (root / d).is_dir()]
    if missing:
        print(f"\n  {root} is missing: {', '.join(missing)}\n")
        return 2

    total_matched = total_checked = 0
    all_problems: list[str] = []
    scans = list(scan_dirs(root))
    if args.limit:
        scans = scans[: args.limit]
    print(f"\n  Checking {len(scans)} scan(s) under {root}\n")

    for participant, site, scan in scans:
        if args.verbose:
            print(f"  {participant}/{site}/{scan}")
        matched, checked, problems = check_scan(root, participant, site, scan,
                                                args.verbose)
        total_matched += matched
        total_checked += checked
        all_problems.extend(problems)

    print(f"\n  {total_matched} of {total_checked} statistics matched the answer "
          f"key to better than {TOLERANCE:g} relative\n")
    if all_problems:
        print(f"  {len(all_problems)} problem(s):")
        for line in all_problems[:40]:
            print(f"    {line}")
        if len(all_problems) > 40:
            print(f"    ... and {len(all_problems) - 40} more")
        print()
        return 1
    if not total_checked:
        print("  Nothing was checked. Are the folder names as delivered?\n")
        return 2
    print("  Every statistic agrees with the answer key.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
