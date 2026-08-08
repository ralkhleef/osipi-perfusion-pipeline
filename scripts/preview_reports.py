"""Preview the HTML and PDF evaluation reports without a live submission.

A design/QA tool, not part of the running app. It feeds synthetic summaries,
shaped exactly like ``_gather_summary`` output, through the real renderers so
you can iterate on report styling quickly.

Usage (from the repo root):

    python scripts/preview_reports.py                    # all scenarios, blinded
    python scripts/preview_reports.py batch_mixed        # one scenario
    python scripts/preview_reports.py batch_mixed --unblinded

Files are written to ``data/outputs/report_preview/``. Open the .html in a
browser; use the browser's Print to PDF to check the print stylesheet.
"""
import argparse
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "src"))

import main  # noqa: E402
from services.pdf_report_service import generate_pdf_report  # noqa: E402

OUT_DIR = ROOT / "data" / "outputs" / "report_preview"


def _ref_map(map_type, idx, *, ref_mean, sub_mean, sd, rmse, mae):
    """One scored map, shaped like backend/scoring.py's output.

    Grey matter is offset slightly from the whole-image figures so the
    Bland-Altman and identity plots show more than one point per submission.
    """
    def region(scale):
        submitted = sub_mean * scale
        reference = ref_mean * scale
        return {
            "status": "compared",
            "mean_submitted": round(submitted, 4),
            "mean_reference": round(reference, 4),
            "bias": round(submitted - reference, 4),
            "mean_error": round(submitted - reference, 4),
            "standard_deviation_error": round(sd * scale, 4),
            "rmse": round(rmse * scale, 4),
            "mae": round(mae * scale, 4),
            "error_coefficient_of_variation": 0.18,
            "coefficient_of_variation": 0.18,
            "correlation": 0.912,
            "voxel_count": 41233,
            "total_voxel_count": 42045,
        }

    return {
        "detected_map_type": map_type,
        "status": "compared",
        "difference_map": True,
        "whole_map": region(1.0),
        "masks": [{"mask_label": "Grey matter", "metrics": region(1.12)}],
    }


def make_summary(idx, challenge, *, ref=True, warns=0, errs=0):
    """Build one summary in the shape _gather_summary returns."""
    maps = []
    ref_rows = []
    for mt, unit, mean in (("CBF", "ml/100g/min", 58.4), ("ATT", "s", 1.32)):
        maps.append({
            "file_name": f"sub-{idx:02d}_{mt.lower()}.nii.gz",
            "detected_map_type": mt,
            "parameter_label": mt,
            "units": unit,
            "metadata": {"shape": [64, 64, 24], "voxel_size": [3.4, 3.4, 5.0],
                         "nan_count": 12 * idx, "inf_count": 0},
            "stats": {"finite_percent": 99.4 - idx * 0.3, "negative_voxel_percent": 0.42,
                      "mean": mean, "coefficient_of_variation": 0.31},
        })
        if ref:
            for scope in ("Whole image", "Grey matter"):
                ref_rows.append({
                    "detected_map_type": mt, "scope": scope, "status": "compared",
                    "rmse": 7.21 + idx, "mae": 5.03 + idx, "bias": -1.12,
                    "coefficient_of_variation": 0.18, "correlation": 0.912,
                    "voxel_count": 41233, "excluded_voxel_count": 812,
                })
    return {
        "submission_id": f"sub-{idx:02d}", "source_folder": f"team_alpha_{idx}",
        "challenge_type": challenge, "team_name": f"Team {idx}",
        "contact_email": f"team{idx}@example.org",
        "warning_count": warns, "error_count": errs,
        "warnings": [{"message": "Voxel size differs from the reference grid.",
                      "path": f"sub-{idx:02d}_cbf.nii.gz"}] * warns,
        "errors": [{"message": "Map could not be read as a valid NIfTI volume.",
                    "path": f"sub-{idx:02d}_att.nii.gz"}] * errs,
        "exec_status": "skipped_result_maps",
        "numeric_metrics": {"icc": None},
        "nifti_analysis": {
            "maps": maps,
            # Mirrors what backend/scoring.py actually records per region,
            # including mean_submitted / mean_reference /
            # standard_deviation_error, which the agreement figures need.
            "reference_scoring": {"maps": [
                _ref_map("CBF", idx, ref_mean=58.0, sub_mean=58.0 + 1.6 * idx - 2.4,
                         sd=6.4 + 0.4 * idx, rmse=7.21 + idx, mae=5.03 + idx),
                _ref_map("ATT", idx, ref_mean=1.30, sub_mean=1.30 + 0.05 * idx - 0.08,
                         sd=0.21 + 0.02 * idx, rmse=0.28 + 0.02 * idx,
                         mae=0.19 + 0.02 * idx),
            ] if ref else []},
        },
        "analysis_fields": {
            "parameter_maps_detected": "CBF, ATT", "map_count": 2,
            "finite_voxels_percent": 99.4 - idx * 0.3, "nan_count": 12 * idx, "inf_count": 0,
            "negative_voxels_percent": 0.42, "finite_voxel_count": 96_000, "total_voxel_count": 98_304,
            "negative_voxel_count": 403, "means_by_map_type": {"CBF": 58.4, "ATT": 1.32},
            "mean_coefficient_of_variation": 0.31,
            "reference_based_scoring_available": ref,
            "reference_compared_map_count": 2 if ref else 0,
            "reference_scoring_status": "available" if ref else "reference_not_available",
            "reference_mean_rmse": 7.21 if ref else None,
            "reference_mean_mae": 5.03 if ref else None,
            "reference_mean_bias": -1.12 if ref else None,
            "reference_metric_rows": ref_rows,
        },
    }


def _stress(summary):
    """Push long strings through every text slot to expose overlap.

    Layout bugs hide behind tidy sample data: short map names and two-letter
    challenges fit anything. This widens the values that feed the figures
    band, the status line, and the table cells so collisions show up here
    rather than on a real submission.
    """
    summary = dict(summary)
    summary["analysis_fields"] = dict(summary["analysis_fields"])
    summary["analysis_fields"]["parameter_maps_detected"] = (
        "CBF, ATT, Ktrans, Ve, Vp, BAT"
    )
    summary["analysis_fields"]["map_count"] = 6
    summary["analysis_fields"]["nan_count"] = 1234567
    summary["analysis_fields"]["inf_count"] = 89012
    summary["analysis_fields"]["finite_voxels_percent"] = 87.654321
    summary["analysis_fields"]["reference_mean_rmse"] = 12345.6789
    summary["source_folder"] = (
        "team_northwestern_perfusion_group_resubmission_v3_final"
    )
    summary["team_name"] = "Northwestern Perfusion Imaging Group (Radiology)"
    summary["contact_email"] = "perfusion.imaging.group@northwestern.example.edu"
    summary["warnings"] = [{
        "message": "Voxel size differs from the reference grid by more than "
                   "the configured tolerance, so resampling was applied "
                   "before comparison; review the resampled output.",
        "path": "sub-01_acq-highres_desc-preproc_cbf.nii.gz",
    }]
    summary["warning_count"] = 1
    return summary


SCENARIOS = {
    "single_clean":  [make_summary(1, "asl")],
    "batch_mixed":   [make_summary(1, "asl", warns=1), make_summary(2, "asl"),
                      make_summary(3, "dce", ref=False, errs=1)],
    "no_reference":  [make_summary(1, "asl", ref=False, warns=2)],
    "stress":        [_stress(make_summary(1, "asl", warns=1)),
                      _stress(make_summary(2, "dce")),
                      _stress(make_summary(3, "dsc", ref=False, errs=1))],
}


def render(name: str, *, blinded: bool) -> None:
    data = SCENARIOS[name]
    # Stub the disk-backed lookups so the renderers run on synthetic data.
    main._collect_export_ids = lambda b, s: [x["submission_id"] for x in data]
    main._gather_summary = lambda sid: next(
        x for x in data if x["submission_id"] == sid
    )

    suffix = "" if blinded else "_unblinded"
    resp = main.export_report(
        submission_id=None, batch_id="BATCH-PREVIEW", blinded=blinded
    )
    html_path = OUT_DIR / f"report_{name}{suffix}.html"
    html_path.write_text(resp.body.decode("utf-8"), encoding="utf-8")

    pdf_path = OUT_DIR / f"report_{name}{suffix}.pdf"
    pdf_path.write_bytes(
        generate_pdf_report(data, tag="BATCH-PREVIEW", blinded=blinded)
    )
    print(f"{name}{suffix}:\n  {html_path}\n  {pdf_path}")


def cli() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "scenario", nargs="?", choices=sorted(SCENARIOS),
        help="Scenario to render (default: all).",
    )
    parser.add_argument(
        "--unblinded", action="store_true",
        help="Include team name and contact columns.",
    )
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    names = [args.scenario] if args.scenario else sorted(SCENARIOS)
    for name in names:
        render(name, blinded=not args.unblinded)


if __name__ == "__main__":
    cli()
