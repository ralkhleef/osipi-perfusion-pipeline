#!/usr/bin/env python3
"""Check the pipeline's DCE error statistics against the challenge answer key."""

from __future__ import annotations

import argparse
import json
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

TOLERANCE = 1e-6


def load(path: Path) -> np.ndarray:
    return np.asarray(nib.load(str(path)).dataobj, dtype=np.float64)


def exclusive_masks(masks: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], list[str]]:
    """Make nested ROIs disjoint, smallest wins."""
    names = sorted(masks, key=lambda n: int(masks[n].sum()))
    result: dict[str, np.ndarray] = {}
    notes: list[str] = []
    for i, outer in enumerate(names):
        keep = masks[outer].copy()
        for inner in names[:i]:
            overlap = int((keep & masks[inner]).sum())
            if overlap and overlap == int(masks[inner].sum()):
                keep &= ~masks[inner]
                notes.append(f"{outer} excludes {inner} ({overlap} voxels)")
        result[outer] = keep
    return result, notes


def scan_dirs(root: Path):
    """Every scan present under submission/, in a stable order."""
    base = root / "submission"
    for participant in sorted(p for p in base.iterdir() if p.is_dir()):
        for site in sorted(s for s in participant.iterdir() if s.is_dir()):
            for scan in sorted(s for s in site.iterdir() if s.is_dir()):
                yield participant.name, site.name, scan.name


def answer_key(root: Path, participant: str, site: str, scan: str) -> dict | None:
    """The answer key for one scan."""
    for candidate in (
        root / "known_error" / participant / site / scan / "known_error_params.json",
        root / "known_error" / participant / site / "known_error_params.json",
    ):
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    return None


def check_scan(root: Path, participant: str, site: str, scan: str,
               verbose: bool) -> tuple[int, int, int, list[str]]:
    key = answer_key(root, participant, site, scan)
    if key is None or "achieved" not in key:
        return 0, 0, 0, [f"{participant}/{site}/{scan}: no answer key"]
    achieved = key["achieved"]

    sub = root / "submission" / participant / site / scan
    gt = root / "hidden_ground_truth" / participant / site / scan
    masks_dir = root / "shared_masks" / site

    matched = checked = explained = 0
    problems: list[str] = []

    raw = {r: load(masks_dir / f"{r}_mask.nii.gz") > 0.5
           for r in REGIONS
           if (masks_dir / f"{r}_mask.nii.gz").exists() and r in achieved}
    masks, _notes = exclusive_masks(raw)

    for region in REGIONS:
        if region not in masks:
            continue
        mask = masks[region]

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
            submitted = load(sub_file)
            error = (submitted - load(gt_file))[mask]
            clipped = int((submitted[mask] == 0).sum())
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
                elif clipped:
                    explained += 1
                    if verbose:
                        print(f"    {region:<5} {param}_{stat:<5} off by {rel:.1e}, "
                              f"explained by {clipped} clipped voxel(s)")
                else:
                    problems.append(
                        f"{participant}/{site}/{scan} {region} {param}_{stat}: "
                        f"key {theirs:.10g}, computed {value:.10g}, off by {rel:.2e}")
    return matched, checked, explained, problems


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

    total_matched = total_checked = total_explained = 0
    all_problems: list[str] = []
    scans = list(scan_dirs(root))
    if args.limit:
        scans = scans[: args.limit]
    print(f"\n  Checking {len(scans)} scan(s) under {root}\n")

    for participant, site, scan in scans:
        if args.verbose:
            print(f"  {participant}/{site}/{scan}")
        matched, checked, explained, problems = check_scan(
            root, participant, site, scan, args.verbose)
        total_matched += matched
        total_checked += checked
        total_explained += explained
        all_problems.extend(problems)

    print(f"\n  {total_matched} of {total_checked} statistics matched the answer "
          f"key to better than {TOLERANCE:g} relative")
    if total_explained:
        print(f"  {total_explained} more differ only where a submitted value was "
              f"clipped to zero, so part of the injected error was never realised.")
    print()
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
