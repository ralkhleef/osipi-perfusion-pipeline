"""PDF report generation for OSIPI export summaries."""

from __future__ import annotations

import io
import json
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.ingest_service import make_safe_id
from services.path_config import OUTPUTS_DIR
from osipi_pipeline.config.rules import challenge_labels, map_type_specs


REFERENCE_UNAVAILABLE_NOTE = (
    "Reference maps were not available, so this report shows QC metrics only."
)

UNAVAILABLE_METRICS_NOTE = (
    "Repeatability CoV and ICC are unavailable: they require repeated "
    "(noise-varied) datasets, which have not been provided."
)


def _pipeline_version() -> str:
    try:
        import re as _re
        text = (Path(__file__).resolve().parents[2] / "pyproject.toml").read_text(encoding="utf-8")
        m = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def _configuration_version() -> str:
    try:
        from osipi_pipeline.config.rules import validation_rules as _vr
        v = _vr().get("version")
        return str(v) if v is not None else "unknown"
    except Exception:
        return "unknown"


def _err_cov(metrics: Mapping[str, Any]):
    return metrics.get("error_coefficient_of_variation", metrics.get("coefficient_of_variation"))


def _per_map_sections(summaries: Sequence[Mapping[str, Any]], *, blinded: bool) -> list[dict]:
    """Per-submission, per-map scientific detail: submitted-output properties
    (units/dimensions/shape/voxel size + QC) and reference metrics per ROI, with
    valid/excluded voxel counts. CBF and ATT are always separate map entries."""
    sections: list[dict] = []
    for idx, s in enumerate(summaries, start=1):
        analysis = s.get("nifti_analysis") if isinstance(s.get("nifti_analysis"), dict) else {}
        ref = analysis.get("reference_scoring") if isinstance(analysis.get("reference_scoring"), dict) else {}
        ref_by_type = {}
        for row in ref.get("maps") or []:
            if isinstance(row, dict) and row.get("detected_map_type"):
                ref_by_type.setdefault(str(row["detected_map_type"]), row)
        maps_out = []
        for qc in analysis.get("maps") or []:
            if not isinstance(qc, dict):
                continue
            mtype = str(qc.get("detected_map_type") or "Unknown")
            if mtype == "Unknown":
                continue
            meta = qc.get("metadata") or {}
            stats = qc.get("stats") or {}
            shape = meta.get("shape") or []
            ref_row = ref_by_type.get(mtype, {})
            roi_rows = []
            scopes = []
            if ref_row.get("whole_map"):
                scopes.append(("Whole image", ref_row.get("whole_map")))
            for mask in ref_row.get("masks") or []:
                if isinstance(mask, dict):
                    scopes.append((str(mask.get("mask_label") or mask.get("mask_name") or "ROI"),
                                   mask.get("metrics") or {}))
            for roi_name, m in scopes:
                valid = m.get("voxel_count")
                total = m.get("total_voxel_count")
                excluded = (total - valid) if isinstance(total, (int, float)) and isinstance(valid, (int, float)) else None
                roi_rows.append({
                    "roi": roi_name,
                    "rmse": m.get("rmse"), "mae": m.get("mae"), "bias": m.get("bias"),
                    "error_cov": _err_cov(m), "correlation": m.get("correlation"),
                    "valid": valid, "excluded": excluded,
                    "status": m.get("status"),
                })
            maps_out.append({
                "map_type": mtype,
                "display": qc.get("parameter_label") or mtype,
                "units": qc.get("units") or "not provided",
                "dimensions": len(shape) if shape else None,
                "shape": "×".join(str(x) for x in shape) if shape else "Not available",
                "voxel_size": "×".join(str(x) for x in (meta.get("voxel_size") or [])) or "Not available",
                "finite_percent": stats.get("finite_percent"),
                "nan_count": meta.get("nan_count"),
                "inf_count": meta.get("inf_count"),
                "negative_percent": stats.get("negative_voxel_percent"),
                "reference_status": ref_row.get("status") or "reference_not_available",
                "difference_map": bool(ref_row.get("difference_map")),
                "roi_rows": roi_rows,
            })
        if maps_out:
            sections.append({
                "label": _submission_label(s, idx, blinded=blinded),
                "challenge": str(s.get("challenge_type") or "").upper(),
                "maps": maps_out,
            })
    return sections


def _fmt(value: Any, digits: int = 3) -> str:
    if value is None or value == "":
        return "Not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            return (f"{value:.{digits}f}").rstrip("0").rstrip(".")
        return str(value)
    return str(value)


def _pct(value: Any) -> str:
    if value is None or value == "":
        return "Not available"
    if isinstance(value, str):
        return value if value.endswith("%") else value
    if isinstance(value, (int, float)):
        return f"{_fmt(value, 2)}%"
    return str(value)


def _status_text(status: Any) -> str:
    raw = str(status or "").strip().lower()
    if raw == "skipped_result_maps":
        return "Execution not required"
    if raw in {"", "not_scored", "not_run", "reference_not_available", "not_available", "unavailable"}:
        return "Not available"
    if raw in {"reference_not_available", "not_available", "unavailable"}:
        return "Not available"
    return raw.replace("_", " ").title()


def _mean(values: Iterable[Any]) -> float | None:
    nums: list[float] = []
    for value in values:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            nums.append(float(value))
    if not nums:
        return None
    return sum(nums) / len(nums)


def _weighted_percent(values: Iterable[tuple[Any, Any]]) -> float | None:
    numerator_total = 0.0
    denominator_total = 0.0
    for numerator, denominator in values:
        if (
            isinstance(numerator, (int, float))
            and not isinstance(numerator, bool)
            and isinstance(denominator, (int, float))
            and not isinstance(denominator, bool)
        ):
            numerator_total += float(numerator)
            denominator_total += float(denominator)
    if denominator_total <= 0:
        return None
    return (numerator_total / denominator_total) * 100.0


def _summary_title(summaries: Sequence[Mapping[str, Any]], tag: str, *, blinded: bool = True) -> str:
    if len(summaries) == 1:
        # Blinded reports must not reveal identifying folder/submission names.
        if blinded:
            return "Submission 1"
        first = summaries[0]
        return str(first.get("source_folder") or first.get("submission_id") or tag)
    return f"Batch ({len(summaries)} submissions)" if blinded else f"Batch {tag} ({len(summaries)} submissions)"


def _challenge_text(summaries: Sequence[Mapping[str, Any]]) -> str:
    values = sorted({
        str(s.get("challenge_type") or "").strip().upper()
        for s in summaries
        if str(s.get("challenge_type") or "").strip()
    })
    return ", ".join(values) if values else "not available"


def _challenge_label(value: Any) -> str:
    key = str(value or "").strip().lower()
    return challenge_labels().get(key, key.upper() if key else "not available")


def _means_by_map_type(summary: Mapping[str, Any], key: str) -> Any:
    analysis = summary.get("nifti_analysis")
    analysis = analysis if isinstance(analysis, Mapping) else {}
    analysis_summary = analysis.get("summary")
    analysis_summary = analysis_summary if isinstance(analysis_summary, Mapping) else {}
    means = analysis_summary.get("means_by_map_type")
    means = means if isinstance(means, Mapping) else {}
    return means.get(key)


def _configured_map_displays() -> list[str]:
    return [str(spec.get("display") or key) for key, spec in map_type_specs().items()]


def _format_map_means(summary: Mapping[str, Any]) -> str:
    parts = []
    for display in _configured_map_displays():
        value = _means_by_map_type(summary, display)
        if value is not None:
            parts.append(f"{display}: {_fmt(value)}")
    return "; ".join(parts) if parts else "Not available"


def _analysis_fields(summary: Mapping[str, Any]) -> Mapping[str, Any]:
    fields = summary.get("analysis_fields")
    return fields if isinstance(fields, Mapping) else {}


def _preview_manifest_path(submission_id: str) -> Path:
    return OUTPUTS_DIR / "previews" / make_safe_id(submission_id) / "preview_manifest.json"


def _preview_image_path(submission_id: str, map_id: str, plane: str = "axial") -> Path:
    return OUTPUTS_DIR / "previews" / make_safe_id(submission_id) / f"{map_id}_{plane}.png"


def _preview_is_parameter_map(item: Mapping[str, Any]) -> bool:
    """True when a preview item is a 3-D recognized parameter map (CBF/ATT/…)."""
    if isinstance(item.get("is_parameter_map"), bool):
        return bool(item["is_parameter_map"])
    shape = [d for d in (item.get("shape") or []) if d]
    map_type = str(item.get("detected_map_type") or "").strip().lower()
    return len(shape) == 3 and map_type not in {"", "unknown", "mixed/other"}


def _cached_preview_items(summaries: Sequence[Mapping[str, Any]], *, blinded: bool) -> list[dict[str, Any]]:
    previews: list[dict[str, Any]] = []
    for idx, summary in enumerate(summaries, start=1):
        sid = str(summary.get("submission_id") or "")
        if not sid:
            continue
        manifest_path = _preview_manifest_path(sid)
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in manifest.get("maps") or []:
            map_id = str(item.get("map_id") or "")
            if not map_id or not item.get("preview_available"):
                continue
            # Only 3-D recognized parameter maps (CBF/ATT/…) — no 4-D ASL/model
            # data or unrecognized files in the report gallery.
            if not _preview_is_parameter_map(item):
                continue
            image_path = _preview_image_path(sid, map_id)
            if not image_path.exists():
                continue
            previews.append({
                "submission": _submission_label(summary, idx, blinded=blinded),
                "map": item.get("detected_map_type") or "Unknown",
                "file": item.get("file_name") or "",
                "image_path": str(image_path),
            })
    return previews[:8]


def _submission_metadata_rows(summaries: Sequence[Mapping[str, Any]], *, blinded: bool) -> list[list[str]]:
    rows: list[list[str]] = []
    for idx, summary in enumerate(summaries, start=1):
        af = _analysis_fields(summary)
        row = [
            _submission_label(summary, idx, blinded=blinded),
            _challenge_label(summary.get("challenge_type")),
            str(af.get("parameter_maps_detected") or "Not available"),
            _fmt(af.get("map_count") or 0),
        ]
        if not blinded:
            row.extend([str(summary.get("team_name") or ""), str(summary.get("contact_email") or "")])
        rows.append(row)
    return rows


def _reference_available(fields: Mapping[str, Any]) -> bool:
    return (
        bool(fields.get("reference_based_scoring_available"))
        or int(fields.get("reference_compared_map_count") or 0) > 0
    )


def _reference_status_label(fields: Mapping[str, Any]) -> str:
    raw = str(fields.get("reference_scoring_status") or "").strip().lower()
    compared = int(fields.get("reference_compared_map_count") or 0)
    if raw == "partial_reference_scoring":
        return "Partial"
    if raw == "available" or compared > 0 or bool(fields.get("reference_based_scoring_available")):
        return "Available"
    if raw in {"shape_mismatch", "submitted_invalid", "reference_invalid", "scoring_error"}:
        return raw.replace("_", " ").title()
    return "Not available"


def _detected_map_types(fields: Iterable[Mapping[str, Any]]) -> str:
    values: list[str] = []
    for item in fields:
        for raw in str(item.get("parameter_maps_detected") or "").split(","):
            value = raw.strip()
            if value and value not in values:
                values.append(value)
    return ", ".join(values) if values else "Not available"


def _submission_label(summary: Mapping[str, Any], index: int, *, blinded: bool) -> str:
    if blinded:
        return f"Submission {index}"
    return str(summary.get("source_folder") or summary.get("submission_id") or f"Submission {index}")


def _row_notes(summary: Mapping[str, Any], *, include_reference_note: bool = False) -> str:
    fields = _analysis_fields(summary)
    notes: list[str] = []
    if include_reference_note and not _reference_available(fields):
        notes.append(REFERENCE_UNAVAILABLE_NOTE)
    warnings = int(summary.get("warning_count") or 0)
    errors = int(summary.get("error_count") or 0)
    if warnings:
        notes.append(f"{warnings} warning(s) reported.")
    if errors:
        notes.append(f"{errors} error(s) reported.")
    return " ".join(notes)


def _build_report_model(
    summaries: Sequence[Mapping[str, Any]],
    *,
    tag: str,
    blinded: bool,
    generated: datetime | None = None,
) -> dict[str, Any]:
    generated = generated or datetime.now(timezone.utc)
    # Group submissions by challenge (stable → single-challenge order unchanged).
    summaries = sorted(
        summaries, key=lambda s: str(s.get("challenge_type") or "").strip().upper()
    )
    warnings = sum(int(s.get("warning_count") or 0) for s in summaries)
    errors = sum(int(s.get("error_count") or 0) for s in summaries)
    fields = [_analysis_fields(s) for s in summaries]
    finite = _weighted_percent(
        (af.get("finite_voxel_count"), af.get("total_voxel_count"))
        for af in fields
    )
    if finite is None:
        finite = _mean(af.get("finite_voxels_percent") for af in fields)
    negative = _weighted_percent(
        (af.get("negative_voxel_count"), af.get("finite_voxel_count"))
        for af in fields
    )
    if negative is None:
        negative = _mean(af.get("negative_voxels_percent") for af in fields)
    nan_count = sum(int(af.get("nan_count") or 0) for af in fields)
    inf_count = sum(int(af.get("inf_count") or 0) for af in fields)
    map_count = sum(int(af.get("map_count") or 0) for af in fields)

    map_mean_parts = []
    for display in _configured_map_displays():
        value = _mean(_means_by_map_type(s, display) for s in summaries)
        if value is not None:
            map_mean_parts.append(f"{display}: {_fmt(value)}")
    map_means = "; ".join(map_mean_parts) if map_mean_parts else "Not available"
    cov = _mean(af.get("mean_coefficient_of_variation") for af in fields)
    reference_available = any(_reference_available(af) for af in fields)
    reference_status = "Available" if reference_available else "Not available"
    rmse = _mean(af.get("reference_mean_rmse") for af in fields if _reference_available(af))
    mae = _mean(af.get("reference_mean_mae") for af in fields if _reference_available(af))
    bias = _mean(af.get("reference_mean_bias") for af in fields if _reference_available(af))

    # Challenge scoping: never combine RMSE/MAE/Bias/CoV across challenges.
    challenges = sorted({
        str(s.get("challenge_type") or "").strip().upper()
        for s in summaries
        if str(s.get("challenge_type") or "").strip()
    })
    is_mixed_challenge = len(challenges) > 1

    def _pdf_reference_agg(ch: str) -> dict:
        sub = [
            _analysis_fields(s) for s in summaries
            if str(s.get("challenge_type") or "").strip().upper() == ch
        ]
        ref_sub = [af for af in sub if _reference_available(af)]
        return {
            "available": bool(ref_sub),
            "rmse": _mean(af.get("reference_mean_rmse") for af in ref_sub),
            "mae": _mean(af.get("reference_mean_mae") for af in ref_sub),
            "bias": _mean(af.get("reference_mean_bias") for af in ref_sub),
            "cov": _mean(af.get("mean_coefficient_of_variation") for af in sub),
        }

    per_challenge_reference = {ch: _pdf_reference_agg(ch) for ch in challenges}

    def _reference_metric_items() -> dict:
        if not is_mixed_challenge:
            return {
                "RMSE": _fmt(rmse) if reference_available else "Not available",
                "MAE": _fmt(mae) if reference_available else "Not available",
                "Bias": _fmt(bias) if reference_available else "Not available",
                "Spatial CoV": _fmt(cov),
            }
        out = {"Grouped by challenge": "no cross-challenge totals are computed"}
        for ch in challenges:
            agg = per_challenge_reference[ch]
            avail = agg["available"]
            out[f"{ch} RMSE"] = _fmt(agg["rmse"]) if avail else "Not available"
            out[f"{ch} MAE"] = _fmt(agg["mae"]) if avail else "Not available"
            out[f"{ch} Bias"] = _fmt(agg["bias"]) if avail else "Not available"
            out[f"{ch} Spatial CoV"] = _fmt(agg["cov"])
        return out
    execution_statuses = sorted({
        _status_text(s.get("exec_status"))
        for s in summaries
        if str(s.get("exec_status") or "").strip()
    })
    execution_status = ", ".join(execution_statuses) if execution_statuses else "Not available"
    validation_status = "Unable to continue" if errors else ("Needs review" if warnings else "Complete")
    qc_status = "QC complete" if map_count and not errors else ("Unable to continue" if errors else "Not available")
    export_readiness = "Ready with limitations" if errors or warnings or not reference_available else "Ready"
    issues: list[list[str]] = []
    for idx, s in enumerate(summaries, start=1):
        label = _submission_label(s, idx, blinded=blinded)
        for severity, key in (("Blocking error", "errors"), ("Needs review", "warnings")):
            for msg in (s.get(key) or []):
                if isinstance(msg, Mapping):
                    message = str(msg.get("message") or msg.get("code") or "Issue recorded.")
                    affected = Path(str(msg.get("path") or "")).name or "Not specified"
                else:
                    message = str(msg)
                    affected = "Not specified"
                issues.append([
                    severity,
                    label,
                    message,
                    affected,
                    "Fix and validate again." if severity == "Blocking error" else "Review before sharing.",
                ])

    rows = []
    for idx, s in enumerate(summaries, start=1):
        af = _analysis_fields(s)
        ref_available = _reference_available(af)
        row = [
            _submission_label(s, idx, blinded=blinded),
        ]
        if not blinded:
            row.extend([str(s.get("team_name") or ""), str(s.get("contact_email") or "")])
        row.extend([
            str(s.get("challenge_type") or "not available").upper(),
            str(af.get("parameter_maps_detected") or "Not available"),
            _fmt(af.get("map_count") or 0),
            _pct(af.get("finite_voxels_percent")),
            f"{int(af.get('nan_count') or 0)} / {int(af.get('inf_count') or 0)}",
            _pct(af.get("negative_voxels_percent")),
            _format_map_means(s),
            _reference_status_label(af),
            _fmt(af.get("reference_mean_rmse") if ref_available else None),
            _fmt(af.get("reference_mean_mae") if ref_available else None),
            _fmt(af.get("reference_mean_bias") if ref_available else None),
            _row_notes(s),
        ])
        rows.append(row)

    summary_lines = [
        f"{len(summaries)} submission{'s' if len(summaries) != 1 else ''} reviewed.",
        f"{map_count} map{'s' if map_count != 1 else ''} included.",
        f"Detected map types: {_detected_map_types(fields)}.",
    ]
    # The reference-availability note appears exactly once, in the summary.
    if not reference_available:
        summary_lines.append(REFERENCE_UNAVAILABLE_NOTE)
    else:
        summary_lines.append("Reference maps were available; reference metrics are included.")
    if is_mixed_challenge:
        summary_lines.append(
            "This batch spans multiple challenges (" + ", ".join(challenges) + "). "
            "Results are aggregated per challenge — no cross-challenge totals are computed."
        )
    if warnings:
        summary_lines.append(f"{warnings} warning{'s' if warnings != 1 else ''} reported.")
    if errors:
        summary_lines.append(f"{errors} error{'s' if errors != 1 else ''} reported.")

    notes = []
    if warnings:
        notes.append("Warnings indicate files or metadata that may need review but did not prevent QC export.")
    if not notes:
        notes.append("No additional limitations were reported for this export.")

    return {
        "title": "OSIPI Perfusion Pipeline Report",
        "session_name": _summary_title(summaries, tag, blinded=blinded),
        "challenge_type": _challenge_text(summaries),
        "generated": generated.strftime("%Y-%m-%d %H:%M UTC"),
        "export_date": generated.strftime("%Y-%m-%d"),
        "pipeline_version": _pipeline_version(),
        "configuration_version": _configuration_version(),
        "per_map_sections": _per_map_sections(summaries, blinded=blinded),
        "blinded": blinded,
        "submission_count": len(summaries),
        "map_count": map_count,
        "map_types": _detected_map_types(fields),
        "submission_metadata_headers": (
            ["Submission", "Challenge", "Map types", "Maps"]
            + ([] if blinded else ["Team", "Contact"])
        ),
        "submission_metadata_rows": _submission_metadata_rows(summaries, blinded=blinded),
        "previews": _cached_preview_items(summaries, blinded=blinded),
        "summary_lines": summary_lines,
        "status_cards": {
            "Validation status": validation_status,
            "Execution status": execution_status,
            "QC/reference status": f"{qc_status}; reference {reference_status.lower()}",
            "Export readiness": export_readiness,
        },
        "key_metrics": {
            "Maps available": map_count,
            "Finite voxels": _pct(finite),
            "NaN / Inf": f"{nan_count} / {inf_count}",
            "Negative voxels": _pct(negative),
            "Reference availability": reference_status,
        },
        "chart_rows": [
            ["Validation outcome", "Passed", str(max(0, len(summaries) - errors)), "Warnings", str(warnings), "Blocking errors", str(errors)],
            ["Voxel validity", "Finite", _fmt(sum(int(af.get("finite_voxel_count") or 0) for af in fields), 0), "NaN", str(nan_count), "Inf", str(inf_count)],
        ],
        "qc": {
            "Finite voxels": _pct(finite),
            "NaN / Inf": f"{nan_count} / {inf_count}",
            "Negative voxels": _pct(negative),
            "Map means": ("Reported per challenge" if is_mixed_challenge else map_means),
            "Map count": map_count,
            **_reference_metric_items(),
        },
        "scoring": {
            "Reference status": reference_status,
            **_reference_metric_items(),
            "Reference comparisons": "Available" if reference_available else "Not available",
        },
        "reference_status": reference_status,
        "notes": notes,
        "issues": issues,
        "limitations": [
            "Basic NIfTI QC is not full BIDS validation.",
            "Generic reference metrics are not official OSIPI scoring unless an official provider is configured.",
            "Reference maps, masks, and official metric definitions may be unavailable.",
            "CBF and ATT are reported separately and never averaged together (different units).",
            UNAVAILABLE_METRICS_NOTE,
            "The reported coefficient of variation is an accuracy error-CoV, not a repeatability CoV.",
            "Missing values are shown as Not available, not zero.",
            "No official overall ASL score has been defined; no pass/fail scientific threshold is applied.",
        ],
        "table_headers": (
            ["Submission"]
            + ([] if blinded else ["Team", "Contact"])
            + ["Challenge", "Map types", "Maps", "Finite voxels", "NaN / Inf",
               "Negative voxels", "Map means", "Reference status",
               "RMSE", "MAE", "Bias", "Notes"]
        ),
        "rows": rows,
    }


def _report_lines(model: Mapping[str, Any]) -> list[str]:
    lines = [
        str(model["title"]),
        f"Batch/session name: {model['session_name']}",
        f"Challenge type: {model['challenge_type']}",
        f"Date/time generated: {model['generated']}",
        "Blinded report" if model["blinded"] else "Unblinded report",
        f"Number of submissions: {model['submission_count']}",
        f"Number of maps: {model['map_count']}",
        f"Map types detected: {model['map_types']}",
        "",
        "Report Summary",
    ]
    lines.extend(f"- {item}" for item in model["summary_lines"])
    lines.extend(["", "Status and Key Metrics"])
    lines.extend(f"- {k}: {v}" for k, v in model["status_cards"].items())
    lines.extend(f"- {k}: {v}" for k, v in model["key_metrics"].items())
    lines.extend(["", "Small QC Charts"])
    for row in model["chart_rows"]:
        lines.append(" | ".join(str(cell) for cell in row))
    lines.extend(["", "Submission Metadata"])
    lines.append(" | ".join(str(h) for h in model["submission_metadata_headers"]))
    for row in model["submission_metadata_rows"]:
        lines.append(" | ".join(str(cell) for cell in row))
    lines.extend(["", "QC / Evaluation Metrics"])
    lines.extend(f"- {k}: {v}" for k, v in model["qc"].items())
    lines.extend(["", "Scoring Summary"])
    lines.extend(f"- {k}: {v}" for k, v in model["scoring"].items())
    if model.get("previews"):
        lines.extend(["", "Parameter Map Previews"])
        for item in model["previews"]:
            lines.append(f"- {item['submission']} | {item['map']} | {item['file']}")
    lines.extend(["", "Per-submission results"])
    lines.append(" | ".join(str(h) for h in model["table_headers"]))
    for row in model["rows"]:
        lines.append(" | ".join(str(cell) for cell in row))
    lines.extend(["", "Notes / Limitations"])
    lines.extend(f"- {item}" for item in model["notes"])
    lines.extend(f"- {item}" for item in model["limitations"])
    if model["issues"]:
        lines.extend(["", "Issues and Recommendations"])
        lines.append("Severity | Submission | Message | Affected file | Recommended action")
        for row in model["issues"]:
            lines.append(" | ".join(str(cell) for cell in row))
    return lines


def _simple_pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _simple_pdf_bytes(lines: Sequence[str]) -> bytes:
    """Small dependency-free PDF fallback used when ReportLab is unavailable."""
    wrapped: list[str] = []
    for line in lines:
        if not line:
            wrapped.append("")
            continue
        wrapped.extend(textwrap.wrap(line, width=92) or [""])

    pages = [wrapped[i:i + 48] for i in range(0, len(wrapped), 48)] or [[]]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # filled after page objects are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    page_ids: list[int] = []
    for page in pages:
        content_lines = ["BT", "/F1 10 Tf", "14 TL", "50 742 Td"]
        for line in page:
            content_lines.append(f"({_simple_pdf_escape(line)}) Tj")
            content_lines.append("T*")
        content_lines.append("ET")
        stream = "\n".join(content_lines).encode("latin-1", "replace")
        content_obj = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
            + stream
            + b"\nendstream"
        )
        content_id = len(objects) + 1
        objects.append(content_obj)
        page_id = len(objects) + 1
        page_ids.append(page_id)
        objects.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
            .encode("ascii")
        )

    kids = " ".join(f"{pid} 0 R" for pid in page_ids)
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("ascii")

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(f"{idx} 0 obj\n".encode("ascii"))
        out.write(obj)
        out.write(b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        out.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    out.write(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
        .encode("ascii")
    )
    return out.getvalue()


def _reportlab_pdf_bytes(model: Mapping[str, Any]) -> bytes:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    buffer = io.BytesIO()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=landscape(letter),
        leftMargin=0.4 * inch,
        rightMargin=0.4 * inch,
        topMargin=0.45 * inch,
        bottomMargin=0.45 * inch,
        pageCompression=0,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "OsipiTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=17,
        leading=21,
        textColor=colors.HexColor("#111111"),
        spaceAfter=8,
    )
    heading_style = ParagraphStyle(
        "OsipiHeading",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=11.5,
        leading=14,
        textColor=colors.HexColor("#111111"),
        spaceBefore=11,
        spaceAfter=5,
        keepWithNext=1,
    )
    body_style = ParagraphStyle(
        "OsipiBody",
        parent=styles["BodyText"],
        fontName="Helvetica",
        fontSize=9,
        leading=12,
    )
    table_header_style = ParagraphStyle(
        "OsipiTableHeader",
        parent=body_style,
        fontName="Helvetica-Bold",
        fontSize=6.5,
        leading=8,
        textColor=colors.HexColor("#111111"),
    )
    table_cell_style = ParagraphStyle(
        "OsipiTableCell",
        parent=body_style,
        fontName="Helvetica",
        fontSize=6.5,
        leading=8,
    )

    def para(text: Any, style=body_style) -> Paragraph:
        safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Paragraph(safe, style)

    def kv_table(items: Mapping[str, Any]) -> Table:
        table = Table(
            [[para(k), para(v)] for k, v in items.items()],
            colWidths=[2.05 * inch, 4.2 * inch],
            hAlign="LEFT",
        )
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cccccc")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return table

    def meta_table(items: Mapping[str, Any]) -> Table:
        pairs = list(items.items())
        rows = []
        for i in range(0, len(pairs), 2):
            left = pairs[i]
            right = pairs[i + 1] if i + 1 < len(pairs) else ("", "")
            rows.append([para(left[0]), para(left[1]), para(right[0]), para(right[1])])
        table = Table(
            rows,
            colWidths=[1.55 * inch, 3.0 * inch, 1.55 * inch, 3.0 * inch],
            hAlign="LEFT",
        )
        table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#cccccc")),
            ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f2f2f2")),
            ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f2f2f2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
            ("RIGHTPADDING", (0, 0), (-1, -1), 6),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        return table

    story = [
        para(model["title"], title_style),
        meta_table({
            "Batch/session name": model["session_name"],
            "Challenge type": model["challenge_type"],
            "Date/time generated": model["generated"],
            "Export date": model["export_date"],
            "Report type": "Blinded report" if model["blinded"] else "Unblinded report",
            "Pipeline version": model["pipeline_version"],
            "Configuration version": model["configuration_version"],
            "Number of submissions": model["submission_count"],
            "Number of maps": model["map_count"],
            "Map types detected": model["map_types"],
            "Reference status": model["reference_status"],
        }),
        para("Executive Summary", heading_style),
    ]
    story.extend(para(f"- {item}") for item in model["summary_lines"])
    story.extend([
        para("Status and Key QC Metrics", heading_style),
        kv_table(model["status_cards"]),
        Spacer(1, 0.06 * inch),
        kv_table(model["key_metrics"]),
        para("Submission Metadata", heading_style),
    ])
    metadata_rows = [[para(h, table_header_style) for h in model["submission_metadata_headers"]]]
    metadata_rows.extend([[para(cell, table_cell_style) for cell in row] for row in model["submission_metadata_rows"]])
    metadata_table = Table(
        metadata_rows,
        repeatRows=1,
        hAlign="LEFT",
        colWidths=[(10.1 * inch) / max(1, len(model["submission_metadata_headers"]))] * len(model["submission_metadata_headers"]),
    )
    metadata_table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(metadata_table)
    story.extend([
        para("QC / Evaluation Summary", heading_style),
        kv_table(model["qc"]),
        para("Scoring Summary", heading_style),
        kv_table(model["scoring"]),
    ])

    # ── Submitted outputs & reference comparison, per submission and per map ──
    ref_cols = ["ROI", "RMSE", "MAE", "Bias", "Error CoV", "Corr", "Valid vox", "Excl vox"]
    for section in model.get("per_map_sections") or []:
        story.append(para(f"Submitted Outputs — {section['label']}"
                          + (f" ({section['challenge']})" if section.get("challenge") else ""),
                          heading_style))
        for m in section["maps"]:
            story.append(kv_table({
                "Map type": m["map_type"],
                "Parameter": m["display"],
                "Units": m["units"],
                "Dimensions": f"{m['dimensions']}D" if m.get("dimensions") else "Not available",
                "Shape": m["shape"],
                "Voxel size": m["voxel_size"],
                "Finite voxels %": _fmt(m["finite_percent"]),
                "NaN / Inf": f"{m.get('nan_count') if m.get('nan_count') is not None else 0} / {m.get('inf_count') if m.get('inf_count') is not None else 0}",
                "Negative voxels %": _fmt(m["negative_percent"]),
                "Reference status": str(m["reference_status"]).replace("_", " "),
            }))
            if m["roi_rows"]:
                rrows = [[para(h, table_header_style) for h in ref_cols]]
                for r in m["roi_rows"]:
                    rrows.append([para(v, table_cell_style) for v in [
                        r["roi"], _fmt(r["rmse"]), _fmt(r["mae"]), _fmt(r["bias"]),
                        _fmt(r["error_cov"]), _fmt(r["correlation"]),
                        _fmt(r["valid"], 0), _fmt(r["excluded"], 0),
                    ]])
                rtable = Table(rrows, repeatRows=1, hAlign="LEFT",
                               colWidths=[1.7 * inch] + [1.05 * inch] * 7)
                rtable.setStyle(TableStyle([
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
                    ("FONTSIZE", (0, 0), (-1, -1), 6.5),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                ]))
                story.append(rtable)
            if m.get("difference_map"):
                story.append(para("- Difference map generated (submitted − reference), preserving the source affine."))
            story.append(Spacer(1, 0.05 * inch))

    preview_rows = []
    for item in model.get("previews") or []:
        path = Path(str(item.get("image_path") or ""))
        if not path.exists():
            continue
        preview_rows.append([
            Image(str(path), width=1.2 * inch, height=1.2 * inch),
            para(f"{item.get('submission', '')}<br/>{item.get('map', '')}<br/>{item.get('file', '')}", table_cell_style),
        ])
    if preview_rows:
        story.extend([para("Parameter Map Previews", heading_style)])
        preview_table = Table(preview_rows, colWidths=[1.35 * inch, 3.3 * inch], hAlign="LEFT")
        preview_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        story.append(preview_table)
        story.append(Spacer(1, 0.08 * inch))

    story.append(para("Per-Submission Results", heading_style))
    table_rows = [[para(h, table_header_style) for h in model["table_headers"]]]
    table_rows.extend([[para(cell, table_cell_style) for cell in row] for row in model["rows"]])
    table = Table(
        table_rows,
        repeatRows=1,
        hAlign="LEFT",
        colWidths=[(10.1 * inch) / max(1, len(model["table_headers"]))] * len(model["table_headers"]),
    )
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#111111")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 6.5),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]))
    story.append(table)
    story.extend([
        para("Notes / Limitations", heading_style),
    ])
    story.extend(para(f"- {item}") for item in model["notes"])
    story.extend(para(f"- {item}") for item in model["limitations"])
    if model["issues"]:
        story.append(para("Issues and Recommendations", heading_style))
        issue_rows = [[para(h, table_header_style) for h in ["Severity", "Submission", "Message", "Affected file", "Recommended action"]]]
        issue_rows.extend([[para(cell, table_cell_style) for cell in row] for row in model["issues"][:24]])
        issue_table = Table(issue_rows, repeatRows=1, hAlign="LEFT", colWidths=[1.1 * inch, 1.4 * inch, 4.5 * inch, 1.3 * inch, 1.8 * inch])
        issue_table.setStyle(TableStyle([
            ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f2f2f2")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ]))
        story.append(issue_table)

    def footer(canvas, doc_obj):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillColor(colors.HexColor("#6b6b76"))
        canvas.drawString(0.55 * inch, 0.3 * inch, "OSIPI Perfusion Pipeline")
        canvas.drawCentredString(5.5 * inch, 0.3 * inch, f"Export date: {model['export_date']}")
        canvas.drawRightString(10.55 * inch, 0.3 * inch, f"Page {doc_obj.page}")
        canvas.restoreState()

    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def generate_pdf_report(
    summaries: Sequence[Mapping[str, Any]],
    *,
    tag: str,
    blinded: bool = True,
    generated: datetime | None = None,
) -> bytes:
    """Generate a compact OSIPI PDF report from existing export summaries."""
    if not summaries:
        raise ValueError("At least one summary is required.")
    model = _build_report_model(summaries, tag=tag, blinded=blinded, generated=generated)
    try:
        return _reportlab_pdf_bytes(model)
    except Exception:
        return _simple_pdf_bytes(_report_lines(model))
