#!/usr/bin/env python3
"""Private, end-to-end acceptance check for an ASL reference-data bundle.

The source files are copied into a mode-0700 temporary workspace and deleted
when the check ends. Only aggregate pass counts are printed.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

import nibabel as nib
import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO_ROOT / "backend"), str(REPO_ROOT / "src")]

import scoring  # noqa: E402
from osipi_pipeline.ingestion.manifest import refresh_manifest  # noqa: E402
from services import nifti_preview_service as previews  # noqa: E402

EXPECTED_FILES = {
    "GT_ATT.nii.gz", "GT_Perf.nii.gz", "gm_mask.nii.gz",
    "lesion_roi_mask.nii.gz", "submission_att.nii.gz",
    "submission_cbf.nii.gz", "wm_mask.nii.gz",
}
PAIRS = {
    "CBF": ("submission_cbf.nii.gz", "GT_Perf.nii.gz"),
    "ATT": ("submission_att.nii.gz", "GT_ATT.nii.gz"),
}
METRIC_KEYS = (
    "bias", "mae", "rmse", "standard_deviation_error",
    "error_coefficient_of_variation",
)
ROI_KEYS = (
    "roi_mean", "roi_median", "roi_minimum", "roi_maximum", "roi_range",
    "roi_within_scan_sd", "roi_within_scan_cov",
)


def _load(path: Path) -> np.ndarray:
    return np.asarray(nib.load(str(path)).dataobj, dtype=np.float64)


def _close(actual: float, expected: float, context: tuple[str, ...]) -> None:
    if not np.isclose(actual, expected, rtol=0, atol=1e-6):
        raise AssertionError((*context, actual, float(expected)))


def verify(source: Path) -> dict[str, int | bool]:
    found = {path.name for path in source.iterdir() if path.is_file()}
    if found != EXPECTED_FILES:
        raise ValueError(f"Expected the seven ASL bundle files; missing={sorted(EXPECTED_FILES-found)}, extra={sorted(found-EXPECTED_FILES)}")

    with tempfile.TemporaryDirectory(prefix="osipi-asl-acceptance-") as temporary:
        root = Path(temporary)
        root.chmod(0o700)
        extracted, outputs = root / "extracted", root / "outputs"
        private = root / "private_reference"
        submission_id = "asl-private-acceptance"
        submitted_maps = extracted / submission_id / "results" / "maps"
        reference_maps, masks = private / "maps", private / "masks"
        artifacts = root / "artifacts"
        for directory in (submitted_maps, reference_maps, masks):
            directory.mkdir(parents=True)

        for name in ("submission_att.nii.gz", "submission_cbf.nii.gz"):
            shutil.copy2(source / name, submitted_maps / name)
        for name in ("GT_ATT.nii.gz", "GT_Perf.nii.gz"):
            shutil.copy2(source / name, reference_maps / name)
        for name in ("gm_mask.nii.gz", "wm_mask.nii.gz", "lesion_roi_mask.nii.gz"):
            shutil.copy2(source / name, masks / name)

        for module in (scoring, previews):
            module.EXTRACTED_DIR = extracted
            module.REFERENCE_DATA_DIR = private
            module.OUTPUTS_DIR = outputs
        scoring.SCORING_DIR = root / "scoring"
        scoring.SCORING_OUTPUTS_DIR = root / "score-output"
        previews.PREVIEW_ROOT = outputs / "previews"

        refresh_manifest(
            extracted / submission_id,
            submission_id=submission_id,
            challenge_type="asl",
        )
        analysis = scoring.analyze_submission_niftis(
            submission_id, "asl", artifact_dir=artifacts,
        )
        reference = analysis["reference_scoring"]
        assert reference["status"] == "available"
        assert reference["summary"]["compared_map_count"] == 2
        assert reference["mask_count"] == 3

        rows = {row["detected_map_type"]: row for row in reference["maps"]}
        mask_arrays = {
            path.name[:-7]: _load(path) != 0 for path in masks.glob("*.nii.gz")
        }
        numeric_assertions = 0
        for map_type, (submitted_name, reference_name) in PAIRS.items():
            submitted = _load(submitted_maps / submitted_name)
            truth = _load(reference_maps / reference_name)
            row = rows[map_type]
            regions = [("whole", np.ones(submitted.shape, dtype=bool), row["whole_map"])]
            regions.extend(
                (mask["mask_name"], mask_arrays[mask["mask_name"][:-7]], mask["metrics"])
                for mask in row["masks"]
            )
            for label, selector, metrics in regions:
                valid = selector & np.isfinite(submitted) & np.isfinite(truth)
                error = submitted[valid] - truth[valid]
                reference_values = truth[valid]
                expected = (
                    error.mean(), np.abs(error).mean(), np.sqrt(np.mean(error * error)),
                    error.std(ddof=0), error.std(ddof=0) / abs(reference_values.mean()),
                )
                for key, value in zip(METRIC_KEYS, expected):
                    _close(metrics[key], value, (map_type, label, key))
                    numeric_assertions += 1

            difference = _load(artifacts / row["difference_map"])
            if not np.allclose(difference, submitted - truth, equal_nan=True, atol=1e-5):
                raise AssertionError(f"Difference-map voxel layout mismatch for {map_type}")

        roi_records = reference["roi_descriptive_statistics"]
        assert len(roi_records) == 6
        assert all(record["status"] == "available" for record in roi_records)
        for record in roi_records:
            submitted = _load(submitted_maps / PAIRS[record["map_type"].upper()][0])
            values = submitted[mask_arrays[record["roi_id"]]]
            values = values[np.isfinite(values)]
            expected = (
                values.mean(), np.median(values), values.min(), values.max(),
                np.ptp(values), values.std(ddof=0), values.std(ddof=0) / abs(values.mean()),
            )
            assert record["voxel_count"] == values.size
            for key, value in zip(ROI_KEYS, expected):
                _close(record[key], value, (record["map_type"], record["roi_id"], key))
                numeric_assertions += 1

        manifest = previews.list_submission_previews(submission_id, "asl")
        assert len(manifest["maps"]) == 2
        assert all(len(item["mask_overlays"]) == 3 for item in manifest["maps"])
        for item in manifest["maps"]:
            for overlay in item["mask_overlays"]:
                assert previews.get_preview_png_path(
                    submission_id, item["map_id"], overlay["plane"],
                ).exists()
        public_manifest = json.dumps(previews.public_preview_manifest(manifest))
        assert str(root) not in public_manifest and "source_path" not in public_manifest

        return {
            "maps_compared": 2,
            "masks_per_map": 3,
            "roi_records": len(roi_records),
            "numeric_assertions": numeric_assertions,
            "difference_maps_verified": 2,
            "overlay_pngs_verified": 6,
            "private_paths_in_public_manifest": False,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle_directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.bundle_directory.resolve()), sort_keys=True))


if __name__ == "__main__":
    main()
