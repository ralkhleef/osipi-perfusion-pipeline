#!/usr/bin/env python3
"""Build the DCE error-statistics scoring package.

This is the one that computes what the DCE challenge lead actually asked for.
Her ``known_error_params.json`` files define the metrics: signed bias and
population variance of (submitted minus ground truth), per parameter, inside
each ROI. Running it against her synthetic submission reproduces her recorded
values to about 1e-8.

Unlike ``make_example_scoring_package.py``, which produces scaffolding to edit,
this package is the real calculation. It exists as a package rather than a
script so it runs inside the application on any DCE submission, and so it can
be handed to her to check herself.

    python3 scripts/make_dce_error_package.py
    python3 scripts/make_dce_error_package.py --out somewhere.zip

Two things it gets right that a first attempt would not:

*Nested masks.* Her shipped masks overlap: every one of the 262 hippocampus
voxels is also grey matter. Her statistics do not overlap. Reading the masks
literally made grey matter wrong by 10% on bias and 35% on variance while white
matter and hippocampus matched exactly, which is the worst kind of error
because most of the numbers still agree.

*Clipping.* Where a submitted value is exactly zero and the ground truth is
not, the injected error was truncated at the floor, because the parameter
cannot go negative. Those voxels are counted and reported rather than silently
skewing the result.

No real or private data is involved. Nothing is written outside the output path.
"""

from __future__ import annotations

import argparse
import json
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT / "src")]

PACKAGE_ID = "dce_error_statistics"
PACKAGE_NAME = "DCE error statistics"
PACKAGE_VERSION = "1.0.0"

#: Always present, whatever the submission contains, so none can read to the
#: pipeline as a metric the package failed to produce.
METRICS = (
    "scan_count",
    "roi_count",
    "compared_map_count",
    "mean_ktrans_bias",
    "mean_ktrans_var",
    "mean_vp_bias",
    "mean_vp_var",
    "clipped_voxel_count",
)

SCRIPT = '''#!/usr/bin/env python3
"""DCE error statistics against a ground truth.

For every submitted parameter map with a matching ground-truth map, reports
inside each ROI:

    bias = mean(submitted - ground truth)
    var  = population variance of that difference, ddof=0

which is what the challenge lead's known_error_params.json records.

Not official OSIPI scoring. It computes error statistics; it does not rank.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

PACKAGE_NAME = {name!r}
PACKAGE_VERSION = {version!r}
OFFICIAL_OSIPI_SCORING = False

#: Parameter file stems to compare, and the prefix used in the output. Matching
#: is case insensitive so Ktrans.nii.gz and ktrans.nii.gz both work.
PARAMETERS = (("ktrans", "ktrans"), ("vp", "vp"))


def read_map(path):
    """Voxels as float64, or None when the file will not open.

    nibabel is imported here rather than at module scope because the pipeline
    validates a package by importing its entry point in an isolated
    interpreter, which cannot see user-installed packages.
    """
    try:
        import nibabel as nib  # noqa: PLC0415
        return np.asarray(nib.load(str(path)).dataobj, dtype=np.float64)
    except Exception:
        return None


def find_masks(reference_dir):
    """Named boolean masks from anywhere under the reference directory."""
    masks = {{}}
    if not reference_dir:
        return masks
    for path in sorted(Path(reference_dir).rglob("*")):
        name = path.name.lower()
        if not path.is_file() or "mask" not in name:
            continue
        if not name.endswith((".nii", ".nii.gz")):
            continue
        data = read_map(path)
        if data is None:
            continue
        label = path.name.split(".")[0]
        for suffix in ("_mask", "-mask"):
            if label.lower().endswith(suffix):
                label = label[: -len(suffix)]
        masks[label] = data > 0.5
    return masks


def make_exclusive(masks):
    """Make nested ROIs disjoint, smallest wins.

    The challenge lead's masks are nested: every hippocampus voxel is also grey
    matter. Her statistics are not. Reading them literally mixes hippocampus
    voxels into grey matter, and grey matter alone comes out wrong while the
    others match, so nothing looks broken.

    Only a mask wholly contained in another is subtracted. Partial overlap is
    left alone, because that is a different situation and guessing at it would
    be worse than reporting it as it is.
    """
    names = sorted(masks, key=lambda n: int(masks[n].sum()))
    result, notes = {{}}, []
    for i, outer in enumerate(names):
        keep = masks[outer].copy()
        for inner in names[:i]:
            overlap = int((keep & masks[inner]).sum())
            if overlap and overlap == int(masks[inner].sum()):
                keep &= ~masks[inner]
                notes.append(f"{{outer}} excludes {{inner}} ({{overlap}} voxels)")
        result[outer] = keep
    return result, notes


def find_ground_truth(relative, reference_dir):
    """The ground-truth file for a submitted map.

    Prefers the same relative path under the reference directory, which is how
    a per-scan ground truth is laid out. Falls back to a unique file of the
    same name anywhere beneath it, which covers a flat reference folder. An
    ambiguous fallback is refused rather than guessed at: comparing against the
    wrong participant's map would produce numbers that look entirely plausible.
    """
    if not reference_dir:
        return None
    root = Path(reference_dir)
    direct = root / relative
    if direct.is_file():
        return direct
    matches = [p for p in root.rglob(Path(relative).name) if p.is_file()]
    return matches[0] if len(matches) == 1 else None


def compare(submitted, truth, masks):
    """Per-ROI bias and variance of the error, plus clipped voxels."""
    rois = {{}}
    finite = np.isfinite(submitted) & np.isfinite(truth)
    for label, mask in masks.items():
        if mask.shape != submitted.shape:
            continue
        inside = mask & finite
        if not inside.any():
            continue
        error = submitted[inside] - truth[inside]
        # A submitted value of exactly zero where the truth is not means the
        # error was clipped at the floor: the parameter cannot go negative, so
        # part of it was never realised. Reported, not silently included.
        clipped = int(((submitted[inside] == 0) & (truth[inside] != 0)).sum())
        rois[label] = {{
            "n_voxels": int(inside.sum()),
            "bias": float(error.mean()),
            "var": float(error.var(ddof=0)),
            "clipped_voxels": clipped,
        }}
    return rois


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--submission-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--reference-dir", default=None)
    args = parser.parse_args()

    submission_dir = Path(args.submission_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    raw_masks = find_masks(args.reference_dir)
    masks, mask_notes = make_exclusive(raw_masks)

    scans = {{}}
    compared = 0
    for path in sorted(submission_dir.rglob("*")):
        if not path.is_file() or not path.name.lower().endswith((".nii", ".nii.gz")):
            continue
        stem = path.name.split(".")[0].lower()
        parameter = next((out for key, out in PARAMETERS if stem == key), None)
        if parameter is None:
            continue

        relative = path.relative_to(submission_dir)
        truth_path = find_ground_truth(str(relative), args.reference_dir)
        if truth_path is None:
            continue
        submitted, truth = read_map(path), read_map(truth_path)
        if submitted is None or truth is None or submitted.shape != truth.shape:
            continue

        scan = str(relative.parent) or "."
        scans.setdefault(scan, {{}})[parameter] = compare(submitted, truth, masks)
        compared += 1

    def gather(parameter, stat):
        values = [roi[stat]
                  for scan in scans.values()
                  for roi in (scan.get(parameter) or {{}}).values()]
        return float(np.mean(values)) if values else 0.0

    clipped_total = sum(roi.get("clipped_voxels", 0)
                        for scan in scans.values()
                        for params in scan.values()
                        for roi in params.values())

    summary = {{
        "package": PACKAGE_NAME,
        "package_version": PACKAGE_VERSION,
        "official_osipi_scoring": OFFICIAL_OSIPI_SCORING,
        "status": "completed" if compared else "failed",
        "scan_count": len(scans),
        "roi_count": len(masks),
        "compared_map_count": compared,
        "mean_ktrans_bias": gather("ktrans", "bias"),
        "mean_ktrans_var": gather("ktrans", "var"),
        "mean_vp_bias": gather("vp", "bias"),
        "mean_vp_var": gather("vp", "var"),
        "clipped_voxel_count": int(clipped_total),
        "notes": ["Error statistics against a ground truth. Not official OSIPI scoring."] + mask_notes,
    }}

    if not compared:
        summary["notes"].append(
            "No submitted map had a matching ground-truth map under the "
            "reference directory, so nothing could be compared.")

    (output_dir / "metrics.json").write_text(
        json.dumps({{"summary": summary, "per_scan": scans}}, indent=2), encoding="utf-8")
    (output_dir / "results.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")

    print(json.dumps(summary, indent=2))
    return 0 if compared else 2


if __name__ == "__main__":
    raise SystemExit(main())
'''

README = """# DCE error statistics

Signed bias and population variance of (submitted minus ground truth), per
parameter, inside each ROI. This is what `known_error_params.json` records, so
its output can be checked directly against the challenge's own answer key.

**Not official OSIPI scoring.** It computes error statistics; it does not rank.

## Two things it handles that a first attempt would not

**Nested masks.** If one ROI is wholly contained in another, the inner one is
subtracted from the outer. The challenge's shipped masks overlap while its
statistics do not: every hippocampus voxel is also grey matter. Reading them
literally makes grey matter wrong by around 10% on bias and 35% on variance
while the other ROIs match exactly, so nothing looks broken.

**Clipping.** Where a submitted value is exactly zero and the ground truth is
not, the error was truncated at the floor, because the parameter cannot go
negative. Those voxels are counted and reported as `clipped_voxels` rather than
quietly skewing the result.

## Ground truth matching

For a submitted map at `P01/site_1/scan_1/Ktrans.nii.gz`, the same relative path
is looked for under the reference directory first. Failing that, a *uniquely*
named file anywhere beneath it is used. An ambiguous match is refused rather
than guessed at: comparing against the wrong participant's map would produce
numbers that look entirely plausible.

## Output

`results.json` carries the summary the pipeline reads. `metrics.json` also
carries `per_scan`, with bias, variance, voxel count and clipped count for every
ROI of every scan, which is the shape to compare against the answer key.
"""


def build(out: Path) -> Path:
    try:
        from osipi_pipeline.config.rules import required_maps_by_challenge
        required = list(required_maps_by_challenge().get("dce", ()))
    except Exception:
        required = ["ktrans"]

    manifest = {
        "package_id": PACKAGE_ID,
        "name": PACKAGE_NAME,
        "version": PACKAGE_VERSION,
        "challenge_type": "dce",
        "map_type": "",
        "description": (
            "Signed bias and population variance of the error against a ground "
            "truth, per ROI. Not official OSIPI scoring."
        ),
        "metrics": list(METRICS),
        "required_inputs": required,
        "entry_point": "scoring.py",
        "call_mode": "standard",
        "official": False,
        "expected_input_pattern": "*.nii*",
    }

    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2) + "\n")
        zf.writestr("scoring.py", SCRIPT.format(name=PACKAGE_NAME,
                                                version=PACKAGE_VERSION))
        zf.writestr("README.md", README)
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    out = args.out or (REPO_ROOT / "data" / "scoring" / "examples"
                       / f"{PACKAGE_ID}.zip")
    build(out)
    print(f"\n  {out}")
    print(f"  {len(METRICS)} metrics, DCE, compares against a ground truth")
    print("  Upload it in Reviewer settings, Active Analysis Provider.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
