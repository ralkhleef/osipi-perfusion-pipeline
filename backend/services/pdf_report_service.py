"""PDF report generation for OSIPI export summaries."""

from __future__ import annotations

import io
import logging
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.report_branding import (
    BRAND,
    PDF_SANS,
    lockup_aspect,
    lockup_reportlab_path,
    logo_reportlab_path,
    status_tone,
)
from osipi_pipeline.scoring.descriptive_statistics import (
    METHODOLOGY as DESCRIPTIVE_METHODOLOGY,
)
from services.provenance_service import analysis_provenance
from osipi_pipeline.config.rules import challenge_labels, map_type_specs

logger = logging.getLogger(__name__)


REFERENCE_UNAVAILABLE_NOTE = (
    "Compatible reference maps were not available, so reference-comparison "
    "metrics were not calculated."
)

UNAVAILABLE_METRICS_NOTE = (
    "Repeatability CoV and ICC are unavailable: they require repeated "
    "(noise-varied) datasets, which have not been provided."
)

#: ICC now has two distinct reasons for being blank and a reader has to be able
#: to tell them apart: the challenge has chosen no model, or it has chosen one
#: and this submission has no repeated scans to apply it to. Reporting the
#: second when the first is true would send someone looking for data when what
#: is missing is a decision.
ICC_NOT_CONFIGURED_NOTE = (
    "ICC was not calculated: this challenge has not selected an ICC model. "
    "All six Shrout & Fleiss models are implemented; set "
    "grouped_statistics.icc.model to enable one."
)


def _repeatability_note(icc_status: str = "") -> str:
    """The accurate reason ICC and repeatability CoV are blank."""
    if str(icc_status) == "available":
        return "ICC results and unavailable-table reasons are reported separately by model. Repeatability CoV is not computed; no pass/fail threshold is applied."
    if str(icc_status) == "no_groups":
        return "ICC models require repeated scans. ICC could not be computed for these tables; see each model's status and data requirements. Repeatability CoV is not computed."
    if str(icc_status) == "not_configured":
        return (
            "Repeatability CoV is unavailable: it requires repeated "
            "(noise-varied) datasets, which have not been provided. "
            + ICC_NOT_CONFIGURED_NOTE
        )
    return UNAVAILABLE_METRICS_NOTE


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


CONTENTS_CAPTION = (
    "What the submission contains, grouped by dataset and type. Parameter "
    "maps, fitted signals and documents are counted separately; organiser "
    "reference data is not counted as submitted content."
)

CAPTION_AGGREGATE_TAIL = (
    "Values are weighted across included maps; parameter types with different units are reported separately and never averaged together."
)


def _submission_contents_rows(summaries: Sequence[Mapping[str, Any]]) -> list[list[str]]:
    """What the submission contains, grouped by dataset and artifact type.

    Rows come from the canonical role-based counts computed during validation,
    so a fitted signal is never counted as a parameter map and reference data
    is never counted as submitted content. Labels and units are read from
    configuration; nothing challenge-specific is written here.

    Falls back to an empty table when a summary predates the counts field, so
    an older stored validation result still renders.
    """
    rows: list[list[str]] = []
    for summary in summaries:
        counts = summary.get("counts") if isinstance(summary.get("counts"), Mapping) else {}
        for row in counts.get("contents") or []:
            rows.append([
                str(row.get("dataset") or "Not specified"),
                str(row.get("label") or ""),
                str(row.get("count") or 0),
                str(row.get("dimensions") or "Not available"),
                _units_display(row.get("units"), row.get("units_configured")),
                str(row.get("status") or "Valid"),
            ])
    return rows


def _units_display(units: Any, configured: Any = None) -> str:
    """Units as configured, distinguishing "unitless" from "not configured".

    A blank in the report should not silently mean the same thing for a
    quantity that is genuinely dimensionless (a volume fraction) and one whose
    unit nobody has recorded yet.
    """
    text = str(units).strip() if units is not None else ""
    if text:
        return text
    if configured is True:
        return "Unitless"
    return "Not configured"


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


ROI_METRIC_COLUMNS = (
    ("mean", "Mean"),
    ("median", "Median"),
    ("standard_deviation", "SD"),
    ("range", "Range"),
    ("coefficient_of_variation", "CoV"),
)
ROI_TABLE_HEADERS = (
    "Dataset", "Participant", "Repeat", "Site", "Map", "ROI",
    *(label for _, label in ROI_METRIC_COLUMNS),
    "Voxels", "Units", "Status",
)

_UNAVAILABLE = "Unavailable"

#: Shared by both formats so the wording cannot drift.
ROI_METHOD_TEXT = (
    "ROI statistics were calculated from finite parameter-map voxels within each "
    "configured ROI. Standard deviation uses the population definition "
    "(ddof=0). CoV is standard deviation divided by the absolute arithmetic "
    "mean and is unavailable when the mean is near zero. These conventions "
    "remain subject to final confirmation by OSIPI."
)


def _roi_number(value: Any, digits: int = 4) -> str:
    """Display a statistic, or say it is unavailable. Never renders as zero."""
    if value is None:
        return _UNAVAILABLE
    return _fmt(value, digits)


def _roi_percent(value: Any) -> str:
    """CoV is stored as a ratio and displayed as a percentage."""
    if value is None:
        return _UNAVAILABLE
    try:
        return f"{float(value) * 100:.2f}%"
    except (TypeError, ValueError):
        return _UNAVAILABLE


def _roi_range(record: Mapping[str, Any]) -> str:
    """Display the observed minimum-to-maximum interval for an ROI."""
    low = record.get("roi_minimum")
    high = record.get("roi_maximum")
    if low is None or high is None:
        return _UNAVAILABLE
    return f"{_fmt(low, 4)} to {_fmt(high, 4)}"


def _roi_descriptive_model(
    summaries: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Collect the canonical ROI records and render them once.

    Reads ``reference_scoring.roi_descriptive_statistics``, the records
    computed during scoring. Nothing here recalculates a statistic.
    """
    records: list[dict] = []
    configured_metrics: list[str] = []
    for summary in summaries:
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        scoring = analysis.get("reference_scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        for metric in scoring.get("roi_descriptive_report_metrics") or ():
            metric = str(metric)
            if metric not in configured_metrics:
                configured_metrics.append(metric)
        for record in scoring.get("roi_descriptive_statistics") or ():
            if isinstance(record, Mapping):
                records.append(dict(record))

    # Deterministic order, shared by both formats.
    records.sort(key=lambda r: tuple(
        str(r.get(k) or "") for k in
        ("dataset", "participant", "repeat", "site", "roi_id")
    ))

    known_metrics = {metric for metric, _ in ROI_METRIC_COLUMNS}
    selected_metrics = [m for m in configured_metrics if m in known_metrics]
    if not selected_metrics:
        selected_metrics = [metric for metric, _ in ROI_METRIC_COLUMNS]
    metric_labels = dict(ROI_METRIC_COLUMNS)

    def metric_cells(record: Mapping[str, Any]) -> list[str]:
        values = {
            "mean": _roi_number(record.get("roi_mean")),
            "median": _roi_number(record.get("roi_median")),
            "standard_deviation": _roi_number(record.get("roi_within_scan_sd")),
            "range": _roi_range(record),
            "coefficient_of_variation": _roi_percent(record.get("roi_within_scan_cov")),
        }
        return [values[metric] for metric in selected_metrics]

    rows = []
    for record in records:
        rows.append([
            str(record.get("dataset") or "Not available"),
            str(record.get("participant") or "Not available"),
            str(record.get("repeat") or "Not available"),
            # Clinical datasets leave the site implicit; shown as a dash, not "0".
            str(record.get("site") or "Not available"),
            str(record.get("map_type") or "Not available").upper(),
            str(record.get("roi_label") or record.get("roi_id") or "Not available"),
            *metric_cells(record),
            _fmt(record.get("voxel_count") or 0, 0),
            str(record.get("units") or "Not available"),
            str(record.get("unavailable_reason") or record.get("status") or "Not available").replace("_", " "),
        ])

    headers = [
        "Dataset", "Participant", "Repeat", "Site", "Map", "ROI",
        *(metric_labels[metric] for metric in selected_metrics),
        "Voxels", "Units", "Status",
    ]

    # A single-scan submission has nothing to put in Dataset, Participant,
    # Repeat or Site, so four columns of dashes took a third of the width and
    # pushed Status off the edge. Any column whose value never varies carries
    # no information per row: it is lifted out and stated once above the table
    # instead. The full rows stay untouched below for the CSV, which must keep
    # every column whatever one submission happens to look like.
    LIFTABLE = ("Dataset", "Participant", "Repeat", "Site", "Units", "Status")
    scope: dict[str, str] = {}
    drop: set[int] = set()
    if rows:
        for index, header in enumerate(headers):
            if header not in LIFTABLE:
                continue
            values = {row[index] for row in rows}
            if len(values) != 1:
                continue
            (only,) = values
            drop.add(index)
            if only and only != "Not available":
                scope[header] = only

    display_headers = [h for i, h in enumerate(headers) if i not in drop]
    display_rows = [[c for i, c in enumerate(row) if i not in drop] for row in rows]

    available = sum(1 for r in records if r.get("status") == "available")
    return {
        "roi_descriptive_rows": rows,
        "roi_descriptive_headers": headers,
        # What both report formats and the app actually render.
        "roi_descriptive_display_rows": display_rows,
        "roi_descriptive_display_headers": display_headers,
        # The lifted columns, in order, for the line above the table.
        "roi_descriptive_scope": scope,
        "roi_descriptive_report_metrics": selected_metrics,
        "roi_descriptive_records": records,
        "roi_descriptive_methodology": dict(DESCRIPTIVE_METHODOLOGY),
        "roi_descriptive_summary": {
            "total_rows": len(records),
            "available_rows": available,
            "unavailable_rows": len(records) - available,
            "datasets": sorted({str(r.get("dataset") or "") for r in records} - {""}),
        },
    }


def _prototype_analysis_model(summaries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pre-format provisional grouped ROI and conditional DCE RSS records."""
    grouped: list[dict] = []
    rss_records: list[dict] = []
    icc_rows = []
    for submission_index, summary in enumerate(summaries, 1):
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        scoring = analysis.get("reference_scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        for row in scoring.get("icc_statistics") or ():
            fixed = ", ".join(f"{k}={v}" for k, v in (row.get("held_fixed") or {}).items() if v is not None)
            label = str(row.get("model_description") or row.get("model") or "Not configured").split(":", 1)[0]
            scope = " / ".join(str(v) for v in (
                f"Submission {submission_index}", row.get("challenge"), row.get("dataset"),
                row.get("map_type"), row.get("axis"), fixed,
            ) if v)
            interval = (
                f"{_roi_number(row.get('confidence_low'))} to {_roi_number(row.get('confidence_high'))} "
                f"({_roi_percent(row.get('confidence_level'))})"
                if row.get("confidence_low") is not None and row.get("confidence_high") is not None
                else "Not available"
            )
            icc_rows.append([scope, str(row.get("roi_label") or row.get("roi_id") or "—"),
                label, _roi_number(row.get("value")), interval,
                str(row.get("target_count", 0)), str(row.get("session_count", 0)),
                str(row.get("status_label") or row.get("unavailable_reason") or row.get("status") or "Not available")])
        grouped.extend(
            dict(row) for row in scoring.get("grouped_roi_statistics") or ()
            if isinstance(row, Mapping)
        )
        rss = scoring.get("signal_rss") or scoring.get("dce_signal_rss")
        rss = rss if isinstance(rss, Mapping) else {}
        rss_records.extend(
            dict(row) for row in rss.get("records") or () if isinstance(row, Mapping)
        )

    grouped_rows = []
    for row in grouped:
        fixed = row.get("held_fixed") if isinstance(row.get("held_fixed"), Mapping) else {}
        fixed_text = ", ".join(
            f"{key}={value}" for key, value in fixed.items() if value not in (None, "")
        ) or "Not available"
        pair = (
            f"{row.get('paired_from')}→{row.get('paired_to')}: "
            f"{_roi_number(row.get('paired_difference'))}"
            if row.get("paired_difference") is not None else "Not available"
        )
        grouped_rows.append([
            str(row.get("axis") or "Not available").replace("inter_", ""),
            fixed_text, str(row.get("roi_label") or row.get("roi_id") or "Not available"),
            str(row.get("map_type") or "Not available"), _fmt(row.get("scan_count") or 0, 0),
            _roi_number(row.get("mean")), _roi_number(row.get("standard_deviation")),
            _roi_percent(row.get("coefficient_of_variation")), pair,
            str(row.get("status") or "Not available").replace("_", " "),
        ])

    rss_rows = []
    for record in rss_records:
        scopes = [("Whole image", record.get("whole_image") or {})]
        scopes.extend(
            (str(roi.get("roi_label") or roi.get("mask_name") or "ROI"), roi)
            for roi in record.get("rois") or () if isinstance(roi, Mapping)
        )
        for scope, values in scopes:
            rss_rows.append([
                str(record.get("dataset") or "Not available"), str(record.get("participant") or "Not available"),
                str(record.get("repeat") or "Not available"), str(record.get("site") or "Not available"), scope,
                _roi_number(values.get("median")), _roi_number(values.get("mean")),
                _roi_number(values.get("standard_deviation")),
                _fmt(values.get("voxel_count") or 0, 0),
                str(values.get("status") or record.get("status") or "Not available").replace("_", " "),
            ])
    return {
        "icc_headers": ["Scope", "ROI", "Model", "ICC", "Interval", "Targets", "Sessions", "Status"],
        "icc_rows": icc_rows,
        "grouped_roi_headers": ["Axis", "Held fixed", "ROI", "Map", "Scans", "Mean", "SD", "CoV", "Pair Δ", "Status"],
        "grouped_roi_rows": grouped_rows,
        "dce_rss_headers": ["Dataset", "Participant", "Repeat", "Site", "Region", "RSS median", "RSS mean", "RSS SD", "Voxels", "Status"],
        "dce_rss_rows": rss_rows,
    }


def agreement_points(summaries: Sequence[Mapping[str, Any]], *,
                     blinded: bool) -> dict[str, list[dict]]:
    """Collect per-region agreement points, keyed by map type.

    Reads the full ``reference_scoring`` block, which keeps richer stats than
    ``reference_metric_rows`` surfaces, ``mean_submitted``,
    ``mean_reference``, and ``standard_deviation_error`` are all recorded by
    the scorer per region and were simply never carried into the report. That
    is enough for Bland-Altman and identity plots without re-reading a single
    voxel.

    Keyed by map type rather than by challenge because bias and mean level
    carry the units of the specific parameter: CBF (ml/100 g/min) and ATT
    (seconds) must not share an axis any more than ASL and DCE may.
    """
    grouped: dict[str, list[dict]] = {}
    for idx, summary in enumerate(summaries, start=1):
        label = _submission_label(summary, idx, blinded=blinded)
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        scoring = analysis.get("reference_scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        for row in scoring.get("maps") or []:
            if not isinstance(row, Mapping):
                continue
            map_type = str(row.get("detected_map_type") or "").strip()
            if not map_type:
                continue
            regions = []
            if isinstance(row.get("whole_map"), Mapping):
                regions.append(("Whole image", row["whole_map"], "solid"))
            for mask in row.get("masks") or []:
                if isinstance(mask, Mapping) and isinstance(mask.get("metrics"), Mapping):
                    regions.append((
                        str(mask.get("mask_label") or mask.get("mask_name") or "ROI"),
                        mask["metrics"], "hollow",
                    ))
            for roi, metrics, style in regions:
                submitted = metrics.get("mean_submitted")
                reference = metrics.get("mean_reference")
                bias = metrics.get("bias")
                mean_level = None
                if isinstance(submitted, (int, float)) and isinstance(reference, (int, float)):
                    mean_level = (float(submitted) + float(reference)) / 2.0
                grouped.setdefault(map_type, []).append({
                    "submission": label,
                    "roi": roi,
                    "style": style,
                    "mean_submitted": submitted,
                    "mean_reference": reference,
                    "bias": bias,
                    "sd": metrics.get("standard_deviation_error"),
                    "mean_level": mean_level,
                })
    return grouped


def _map_units(summaries: Sequence[Mapping[str, Any]], map_type: str) -> str:
    """Units for a map type, read from the submitted maps' own metadata."""
    for summary in summaries:
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        for entry in analysis.get("maps") or []:
            if not isinstance(entry, Mapping):
                continue
            if str(entry.get("detected_map_type") or "") != map_type:
                continue
            units = str(entry.get("units") or "").strip()
            if units and units.lower() not in {"not provided", "unknown"}:
                return units
    return "map units"


#: Below this, a fixed-decimal format has nothing left to show and the value
#: is rendered in scientific notation instead.
_SMALL_VALUE = 5e-4


def _fmt(value: Any, digits: int = 3) -> str:
    """Format a number for a report table without rounding it out of existence.

    Fixed decimals suit the quantities most of this pipeline reports, and
    destroy the one that matters most in DCE. Ktrans is of order 1e-4, so a
    real bias of 5.5e-05 printed to three decimals is "0", and a real bias of
    -2e-05 is "-0". A reviewer reading that column sees a submission that
    matched the ground truth perfectly, when what they are actually looking at
    is the formatter.

    Anything smaller than the smallest value three decimals can distinguish is
    therefore shown in scientific notation. Zero stays "0", because zero is a
    measurement and not a rounding artifact.
    """
    if value is None or value == "":
        return "Not available"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            if value != value or value in (float("inf"), float("-inf")):
                return "Not available"
            if value != 0 and abs(value) < _SMALL_VALUE:
                # Two significant figures: enough to compare magnitudes and to
                # see a sign, without implying precision the fit does not have.
                return f"{value:.2e}"
            return (f"{value:.{digits}f}").rstrip("0").rstrip(".") or "0"
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


def _status_fields(validation: str, execution: str, qc: str,
                   readiness: str) -> dict[str, str]:
    """Keep only the status fields that carry information for this run.

    All four derive from the same two counters, so on most runs three of them
    restate each other, "Unable to continue" appearing twice teaches the
    reader that the band is decorative. Execution is dropped when nothing
    required execution (the normal case for result-only submissions), and QC
    is dropped when it agrees with validation.
    """
    fields = {"Validation": validation}
    if str(execution).strip().lower() not in {
        "execution not required", "not available", "",
    }:
        fields["Execution"] = execution
    if str(qc).strip().lower() != str(validation).strip().lower():
        fields["QC"] = qc
    fields["Export readiness"] = readiness
    return fields


def _first_mask_overlaps(summaries: Sequence[Mapping[str, Any]]) -> list:
    """Mask overlaps from the first summary that reports any.

    One organiser mask set applies to the whole run, so one list describes it.
    """
    for summary in summaries or ():
        if not isinstance(summary, Mapping):
            continue
        fields = summary.get("analysis_fields")
        if not isinstance(fields, Mapping):
            continue
        scoring = fields.get("reference_scoring")
        overlaps = scoring.get("mask_overlaps") if isinstance(scoring, Mapping) else None
        if overlaps:
            return list(overlaps)
    return []


def _overlap_notes(overlaps: Sequence[Mapping[str, Any]]) -> list[str]:
    """Say which ROIs share voxels, so the rows are not read as independent.

    A table with one row per region invites the reader to treat the regions as
    a partition. The DCE challenge's regions are nested, so grey matter carries
    the hippocampus inside it and the two rows are not separate measurements.
    Silence here is what makes the pipeline's grey-matter bias look like a
    disagreement with the challenge's own answer key rather than a different
    and clearly stated definition.
    """
    notes: list[str] = []
    for overlap in overlaps or ():
        regions = list(overlap.get("regions") or [])
        if len(regions) != 2:
            continue
        shared = int(overlap.get("shared_voxels") or 0)
        counts = list(overlap.get("voxels") or [0, 0])
        if overlap.get("nested"):
            inner, outer = (
                (regions[0], regions[1]) if shared == counts[0]
                else (regions[1], regions[0])
            )
            notes.append(
                f"Regions overlap: every {inner} voxel ({shared:,}) is also "
                f"inside {outer}, so those two rows are not independent "
                f"measurements."
            )
        else:
            notes.append(
                f"Regions overlap: {regions[0]} and {regions[1]} share "
                f"{shared:,} voxels, so those two rows are not independent "
                f"measurements."
            )
    return notes


def _methods_document_status(summaries: Sequence[Mapping[str, Any]]) -> str:
    """What the submitters said about their methods documents.

    A methods document is not required, so its absence is not a finding. It is
    still worth stating, because "no methods document" and "nobody recorded
    whether there is one" are different things and a blank line reads as
    either. The declaration is what is reported: a blank template the pipeline
    inserted itself is a file, not a document, and validation has already
    excluded it by content.
    """
    labels: list[str] = []
    for summary in summaries or ():
        if not isinstance(summary, Mapping):
            continue
        record = summary.get("methods_document")
        label = str((record or {}).get("label") or "").strip() if isinstance(record, Mapping) else ""
        if label and label not in labels:
            labels.append(label)
    if not labels:
        return "Not recorded"
    if len(labels) == 1:
        return labels[0]
    return "; ".join(labels)


def _first_icc_status(summaries: Sequence[Mapping[str, Any]]) -> str:
    """Do not call ICC wholly unavailable when any submission computed it."""
    statuses = []
    for summary in summaries or ():
        if not isinstance(summary, Mapping):
            continue
        analysis = summary.get("nifti_analysis") or {}
        fields = summary.get("analysis_fields") or {}
        scoring = analysis.get("reference_scoring") or fields.get("reference_scoring")
        status = scoring.get("icc_status") if isinstance(scoring, Mapping) else None
        if status:
            statuses.append(str(status))
    if "available" in statuses:
        return "available"
    return statuses[0] if statuses else ""


def build_limitations(
    *,
    reference_available: bool,
    map_types: Sequence[str],
    challenges: Sequence[str],
    cov_reported: bool,
    icc_status: str = "",
    mask_overlaps: Sequence[Mapping[str, Any]] = (),
) -> list[str]:
    """Build the caveat list, including only caveats that actually apply.

    Shared by the HTML and PDF renderers. Previously both printed the same
    eight bullets on every report, which trained readers to skip the section.
    A caveat about repeatability CoV is noise on a run that computed no
    reference metrics at all. Wording is derived from the run rather than
    hardcoded, so a DCE-only report no longer claims something about ASL.
    """
    items = [
        "QC checks NIfTI readability and voxel statistics. BIDS checking, "
        "where a challenge enables it, covers layout and naming only, "
        "not the full specification.",
    ]
    known = [str(c).strip() for c in challenges if str(c).strip()]
    if len(set(known)) > 1:
        items.append(
            "Because this batch spans more than one challenge, results are "
            "reported per challenge; no cross-challenge totals are computed."
        )
    if reference_available:
        official_note = (
            "Reference metrics are generic comparisons, not official OSIPI "
            "scoring."
        )
        if known:
            official_note += (
                f" No official overall {'/'.join(known)} score, pass/fail "
                "result, or ranking was calculated."
            )
        items.append(official_note)
        repeatability_note = _repeatability_note(icc_status)
        if cov_reported:
            repeatability_note += (
                " The reported coefficient of variation is an accuracy "
                "error-CoV, not a repeatability CoV."
            )
        items.append(repeatability_note)
    for note in _overlap_notes(mask_overlaps):
        items.append(note)
    # Only meaningful once more than one parameter type is present, and it
    # should name the types actually found rather than assume CBF and ATT.
    named = [str(m).strip() for m in map_types if str(m).strip()]
    if len(named) > 1:
        items.append(
            f"{' and '.join((', '.join(named[:-1]), named[-1]))} keep their "
            "own units and are never averaged because their units differ. "
            "Missing values remain Not available."
        )
    if known and not reference_available:
        items.append(
            "No compatible reference or official scoring data were available, "
            f"so no official {'/'.join(known)} score, pass/fail result, or "
            "ranking was calculated."
        )
    if len(named) <= 1:
        items.append(
            "Missing values remain Not available; they are not converted to zero."
        )
    return items


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


# Map preview thumbnails were removed from the printable report, so the cached
# preview helpers that fed them are gone too.


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


def _normalize_identity(value: str) -> str:
    """Reduce a string to comparable letters and digits.

    Collapses the derived forms one name takes across the pipeline,
    ``Secret Team Omega``, ``secret_team_omega``, ``SECRET-TEAM-OMEGA``,
    ``secret team omega.zip``, to a single comparable token, so a check
    cannot be defeated by a formatting difference.
    """
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def identity_tokens(summary: Mapping[str, Any]) -> frozenset[str]:
    """Normalised forms of everything that could name the submitter.

    Used only as a final safety net *after* structural selection has already
    chosen a safe value, never as a search-and-replace over rendered output,
    which would miss metadata and could corrupt unrelated text.
    """
    raw = [
        summary.get("team_name"),
        summary.get("contact_email"),
        summary.get("contact_name"),
        summary.get("submission_id"),
        summary.get("source_folder"),
        summary.get("original_submission_name"),
    ]
    tokens = {_normalize_identity(value) for value in raw}
    # Very short tokens would match almost anything; a two-character team name
    # cannot be protected this way and structural selection has to carry it.
    return frozenset(token for token in tokens if len(token) >= 4)


def reveals_identity(text: str, tokens: frozenset[str]) -> bool:
    """True when ``text`` contains any identity token in any derived form."""
    if not tokens:
        return False
    normalized = _normalize_identity(text)
    return any(token in normalized for token in tokens if token)


def affected_display(
    raw_path: Any,
    summary: Mapping[str, Any],
    label: str,
    *,
    blinded: bool,
) -> str:
    """Return an identity-safe value for the report's Affected column.

    Selection order:

    1. no path → ``Not specified``
    2. a path *below* the submission root → that relative path, which by
       construction contains no owner name
    3. anything else (the root itself, or an unrecognised path) → the
       submission's display label, already blinded by the caller

    A final check re-blinds the result if an identity token survives in any
    derived form, so an unusual layout degrades to the safe label rather than
    leaking.
    """
    text = str(raw_path or "").strip()
    if not text:
        return "Not specified"

    # DUPLICATE_FILENAME and friends record several paths in one field.
    parts = [part.strip() for part in text.split(",") if part.strip()]
    rendered = [_relative_to_submission(part, summary) for part in parts]
    rendered = [item for item in rendered if item]
    value = ", ".join(dict.fromkeys(rendered)) if rendered else ""

    if not value:
        return label if blinded else (text.rsplit("/", 1)[-1] or "Not specified")
    if blinded and reveals_identity(value, identity_tokens(summary)):
        return label
    return value


def _relative_to_submission(path_text: str, summary: Mapping[str, Any]) -> str:
    """The portion of ``path_text`` strictly below the submission root.

    Returns "" when the path *is* the submission root or the root cannot be
    located, which the caller turns into the safe label.
    """
    parts = [part for part in str(path_text).replace("\\", "/").split("/") if part]
    if not parts:
        return ""
    roots = {
        _normalize_identity(summary.get("submission_id")),
        _normalize_identity(summary.get("source_folder")),
    }
    roots.discard("")
    for index in range(len(parts) - 1, -1, -1):
        if _normalize_identity(parts[index]) in roots:
            return "/".join(parts[index + 1:])
    return ""


def report_filename_tag(tag: str, *, blinded: bool) -> str:
    """Return a neutral filename fragment for blinded downloads."""
    if not blinded:
        return str(tag or "report")
    return "blinded"


def export_filename(stem: str, tag: str, *, blinded: bool, extension: str) -> str:
    """Build an export filename without duplicating the blinding label."""
    suffix = "blinded" if blinded else "unblinded"
    parts = [stem, tag] if tag == suffix else [stem, tag, suffix]
    return "_".join(p for p in parts if p) + f".{extension}"


def _submission_label(summary: Mapping[str, Any], index: int, *, blinded: bool) -> str:
    if blinded:
        return f"Submission {index}"
    return str(summary.get("source_folder") or summary.get("submission_id") or f"Submission {index}")




#: How each header check verdict reads to a reviewer. Keyed by the status
#: ``scoring._header_check`` returns, so a new status shows up as itself
#: rather than being silently reported as a pass.
_HEADER_CHECK_VERDICTS = {
    "matches": "Matches",
    "dtype_differs": "Data type differs",
    "geometry_mismatch": "Geometry differs",
    "not_verified": "Not verified",
    # Distinct from "not verified" on purpose. One means nobody looked, the
    # other means we looked and the file would not open.
    "unreadable": "File could not be read",
}


def _header_field_text(field: Any, *, joiner: str = " x ") -> str:
    """One header field rendered for a reviewer.

    A field that matches shows its value once. A field that differs shows both
    values, because "differs" on its own does not tell a reviewer whether the
    submission is flipped or merely at a different voxel size. A field neither
    file declares reads as not verified, which is not the same as a pass.

    ``joiner`` exists because axis codes are conventionally written joined,
    as LAS, while shapes and voxel sizes are written separated.
    """
    if not isinstance(field, Mapping):
        return "Not verified"
    submitted, reference = field.get("submitted"), field.get("reference")
    matches = field.get("matches")

    def text(value: Any) -> str:
        if value is None:
            return "not declared"
        if isinstance(value, (list, tuple)):
            return joiner.join(str(part) for part in value)
        return str(value)

    if matches is None:
        return "Not verified"
    if matches:
        return text(submitted)
    return f"{text(submitted)} vs {text(reference)}"


def _header_check_model(
    summaries: Sequence[Mapping[str, Any]], *, blinded: bool,
) -> dict[str, Any]:
    """Rows for the header and orientation check, one per compared map.

    Both challenge leads asked for this. The check itself has been computed
    since the scorer gained ``_header_check``, but it was stored on the row
    and never rendered, so a flipped submission was caught internally and
    then not mentioned to the person reviewing it.

    Only maps that were actually compared against a reference appear here,
    because there is nothing to compare a header against otherwise.
    """
    rows: list[list[str]] = []
    for index, summary in enumerate(summaries, start=1):
        label = _submission_label(summary, index, blinded=blinded)
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        scoring = analysis.get("reference_scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        for row in scoring.get("maps") or []:
            if not isinstance(row, Mapping):
                continue
            check = row.get("header_check")
            if not isinstance(check, Mapping):
                continue
            fields = check.get("fields")
            fields = fields if isinstance(fields, Mapping) else {}
            status = str(check.get("status") or "not_verified")
            rows.append([
                label,
                str(row.get("detected_map_type") or "Unknown").upper(),
                _header_field_text(fields.get("shape")),
                _header_field_text(fields.get("voxel_size")),
                _header_field_text(fields.get("orientation"), joiner=""),
                _header_field_text(fields.get("dtype")),
                _HEADER_CHECK_VERDICTS.get(status, status),
            ])
    return {
        "header_check_headers": [
            "Submission", "Map", "Shape", "Voxel size",
            "Orientation", "Data type", "Verdict",
        ],
        "header_check_rows": rows,
    }


def _reference_by_region_model(
    summaries: Sequence[Mapping[str, Any]], *, blinded: bool,
) -> dict[str, Any]:
    """Comparison against ground truth, broken down by region.

    Both challenge leads asked for exactly this and the report did not show
    it. The scorer has computed it per mask for a while, but the only place
    it appeared was an appendix that is off by default, so a reader saw the
    whole-image bias and nothing else.

    That difference is not cosmetic. On the challenge lead's own ASL data the
    whole-image CBF bias is +0.83, which looks like close agreement, while
    grey matter is +7.99 and white matter is -4.20. Averaged over the brain
    the two nearly cancel. The regional rows are the result; the single
    number hides it.

    The whole image is kept as its own row so the two sit together and the
    cancellation is visible rather than inferred.
    """
    rows: list[list[str]] = []
    # Columns are added only when they would tell two rows apart. A "Scan"
    # column on a single-scan submission is noise; on this one it is the
    # difference between 480 readable rows and 480 identical ones.
    scan_labels_available = any(
        isinstance(entry, Mapping) and entry.get("scan_label")
        for item in summaries
        if isinstance(item.get("nifti_analysis"), Mapping)
        for entry in (((item.get("nifti_analysis") or {}).get("reference_scoring") or {}).get("maps") or [])
    )
    challenge_names = {str(item.get("challenge_type") or "").strip().upper()
                       for item in summaries if str(item.get("challenge_type") or "").strip()}
    mixed_challenges = len(challenge_names) > 1
    for index, summary in enumerate(summaries, start=1):
        label = _submission_label(summary, index, blinded=blinded)
        challenge_label = str(summary.get("challenge_type") or "").upper() or "Not recorded"
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        scoring = analysis.get("reference_scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        for row in scoring.get("maps") or []:
            if not isinstance(row, Mapping):
                continue
            map_type = str(row.get("detected_map_type") or "").upper()
            regions: list[tuple[str, Mapping]] = []
            whole = row.get("whole_map")
            if isinstance(whole, Mapping) and whole.get("status") == "compared":
                regions.append(("Whole image", whole))
            for mask in row.get("masks") or []:
                metrics = mask.get("metrics") if isinstance(mask, Mapping) else None
                if isinstance(metrics, Mapping) and metrics.get("status") == "compared":
                    regions.append((
                        str(mask.get("mask_label") or mask.get("mask_name") or "ROI"),
                        metrics,
                    ))
            scan = str(row.get("scan_label") or "Not identified")
            for region, metrics in regions:
                rows.append([
                    label,
                    *([challenge_label] if mixed_challenges else []),
                    *([scan] if scan_labels_available else []),
                    map_type, region,
                    _fmt(metrics.get("bias")),
                    _fmt(metrics.get("mae")),
                    _fmt(metrics.get("rmse")),
                    _roi_percent(metrics.get("error_coefficient_of_variation")),
                    _fmt(metrics.get("correlation")),
                    _fmt(metrics.get("voxel_count") or 0, 0),
                ])
    return {
        "reference_region_headers": [
            "Submission",
            *(["Challenge"] if mixed_challenges else []),
            *(["Scan"] if scan_labels_available else []),
            "Map", "Region", "Bias", "MAE", "RMSE",
            "Error CoV", "Corr.", "Voxels",
        ],
        "reference_region_rows": rows,
    }


def _build_report_model(
    summaries: Sequence[Mapping[str, Any]],
    *,
    tag: str,
    blinded: bool,
    include_map_appendix: bool = False,
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

    map_mean_items: dict[str, str] = {}
    for display in _configured_map_displays():
        value = _mean(_means_by_map_type(s, display) for s in summaries)
        if value is not None:
            map_mean_items[f"Mean {display}"] = _fmt(value)
    cov = _mean(af.get("mean_coefficient_of_variation") for af in fields)
    reference_available = any(_reference_available(af) for af in fields)
    reference_status = "Available" if reference_available else "Not available"
    rmse = _mean(af.get("reference_mean_rmse") for af in fields if _reference_available(af))
    mae = _mean(af.get("reference_mean_mae") for af in fields if _reference_available(af))
    bias = _mean(af.get("reference_mean_bias") for af in fields if _reference_available(af))
    roi_model = _roi_descriptive_model(summaries)
    prototype_model = _prototype_analysis_model(summaries)
    header_check_model = _header_check_model(summaries, blinded=blinded)
    region_model = _reference_by_region_model(summaries, blinded=blinded)
    roi_available = bool(roi_model.get("roi_descriptive_rows"))
    grouped_available = bool(prototype_model.get("grouped_roi_rows"))
    rss_available = bool(prototype_model.get("dce_rss_rows"))

    # Challenge scoping: never combine RMSE/MAE/Bias/CoV across challenges.
    challenges = sorted({
        str(s.get("challenge_type") or "").strip().upper()
        for s in summaries
        if str(s.get("challenge_type") or "").strip()
    })
    provenance = analysis_provenance(
        [str(s.get("challenge_type") or "") for s in summaries],
        generated=generated,
    )
    is_mixed_challenge = len(challenges) > 1

    # Keep mixed-challenge aggregates separate because map units cannot be pooled.
    reference_metrics_by_challenge: dict[str, dict[str, str]] = {}
    for challenge in challenges:
        scoped_fields = [
            af for summary, af in zip(summaries, fields)
            if str(summary.get("challenge_type") or "").strip().upper() == challenge
            and _reference_available(af)
        ]
        def _scoped_metric(key: str) -> str:
            value = _mean(af.get(key) for af in scoped_fields)
            return _fmt(value) if value is not None else "Not available"
        reference_metrics_by_challenge[challenge] = {
            "RMSE": _scoped_metric("reference_mean_rmse"),
            "MAE": _scoped_metric("reference_mean_mae"),
            "Bias": _scoped_metric("reference_mean_bias"),
        }

    def _reference_metric_items() -> dict:
        if not is_mixed_challenge:
            return {
                "RMSE": _fmt(rmse) if reference_available else "Not available",
                "MAE": _fmt(mae) if reference_available else "Not available",
                "Bias": _fmt(bias) if reference_available else "Not available",
                "Spatial CoV": _fmt(cov),
            }
        # A compact overview must not become a second results appendix. Mixed
        # challenges use different units, so the complete per-challenge values
        # stay in the expandable HTML and structured exports.
        return {
            "Reference metrics": "Reported separately by challenge in HTML/JSON"
        }
    execution_statuses = sorted({
        _status_text(s.get("exec_status"))
        for s in summaries
        if str(s.get("exec_status") or "").strip()
    })
    execution_status = ", ".join(execution_statuses) if execution_statuses else "Not available"
    validation_status = "Unable to continue" if errors else ("Needs review" if warnings else "Complete")
    qc_status = "QC complete" if map_count and not errors else ("Unable to continue" if errors else "Not available")
    export_readiness = "Ready with limitations" if errors or warnings or not reference_available else "Ready"

    modes = {
        str(s.get("mode") or "").strip().lower()
        for s in summaries if str(s.get("mode") or "").strip()
    }
    if not modes and all(str(s.get("exec_status") or "").lower() == "skipped_result_maps"
                         for s in summaries):
        modes = {"result_only"}
    mode_labels = {
        "result_only": "Result maps provided",
        "result_validation": "Result maps provided",
        "reproducible": "Reproducible code",
        "reproducible_execution": "Reproducible code",
    }
    submission_type = (
        next(iter({mode_labels.get(mode, mode.replace("_", " ").title()) for mode in modes}))
        if len(modes) == 1 else
        "Mixed submission types" if modes else "Not recorded"
    )
    execution_review_status = (
        "Skipped - result maps provided"
        if execution_statuses == ["Execution not required"] else
        execution_status
    )
    validation_review_status = (
        "Failed" if errors else "Passed with review items" if warnings else "Passed"
    )
    qc_review_status = "Available" if map_count else "Not available"

    available_analysis = ["Map QC"] if map_count else []
    if roi_available:
        available_analysis.append("ROI statistics")
    if reference_available:
        available_analysis.append("reference comparison")
    if grouped_available:
        available_analysis.append("grouped descriptive analysis")
    if rss_available:
        available_analysis.append("signal RSS")
    analysis_finding = (
        ", ".join(available_analysis) + "."
        if available_analysis else "No map analysis was available."
    )
    if not reference_available and map_count:
        analysis_finding += " No compatible reference was provided."
    reviewer_summary = [
        ["Result", (
            "Submission cannot complete review until blocking errors are resolved."
            if errors else
            "Structural validation passed; review the warnings before sharing."
            if warnings else
            "Submission passed structural validation."
        )],
        ["Maps", (
            f"{_detected_map_types(fields)} available ({map_count} total)."
            if map_count else "No readable parameter maps were available."
        )],
        ["Analysis", analysis_finding],
        ["Methods document", _methods_document_status(summaries)],
        ["Review items", (
            f"{errors} blocking error{'s' if errors != 1 else ''}; "
            f"{warnings} warning{'s' if warnings != 1 else ''}."
            if errors or warnings else "None."
        )],
    ]

    main_map_metric_rows: list[list[str]] = []
    # A column that would be the same word on every row is noise; one that
    # tells two otherwise identical rows apart is the point. Both are decided
    # from the data rather than assumed.
    scan_labels_available = any(
        isinstance(item, Mapping) and item.get("scan_label")
        for summary in summaries
        for item in ((summary.get("nifti_analysis") or {}).get("maps") or [])
        if isinstance(summary.get("nifti_analysis"), Mapping)
    )
    mixed_challenges = is_mixed_challenge
    for idx, summary in enumerate(summaries, start=1):
        af = _analysis_fields(summary)
        reference_rows = af.get("reference_metric_rows") or []
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        maps = analysis.get("maps")
        maps = maps if isinstance(maps, list) else []
        for item in maps:
            if not isinstance(item, Mapping):
                continue
            # A 4-D fitted curve is not a parameter map, so detection declines
            # to name one and the row used to read "Unknown" with every
            # reference metric "Not available" -- which reads as a failure
            # rather than as a file that was never a parameter map.
            map_type = str(
                item.get("detected_map_type")
                or item.get("parameter_label")
                or ""
            ).strip()
            if not map_type or map_type.lower() == "unknown":
                map_type = str(item.get("role_label") or "Not a parameter map")
            stats = item.get("stats") if isinstance(item.get("stats"), Mapping) else {}
            candidates = [
                row for row in reference_rows
                if isinstance(row, Mapping)
                and str(row.get("detected_map_type") or "").lower() == map_type.lower()
                and str(row.get("scope") or "").lower() in {"whole image", "whole map", "whole"}
            ]
            # Basenames repeat between scans. Prefer the source path, then
            # scan identity for older summaries; never reuse another scan.
            path_matches = [r for r in candidates if item.get("path") and r.get("submitted_path") == item.get("path")]
            # Raw analysis already carries the path. Keep that private join
            # key out of the shared fields serialized into CSV/JSON exports.
            raw_matches = [r for r in ((analysis.get("reference_scoring") or {}).get("maps") or [])
                           if isinstance(r, Mapping) and item.get("path")
                           and r.get("submitted_path") == item.get("path")]
            if len(raw_matches) == 1:
                path_matches = [raw_matches[0].get("whole_map") or {}]
            scan_matches = [r for r in candidates if item.get("scan_label") and r.get("scan_label") == item.get("scan_label")]
            matches = path_matches or scan_matches
            if not matches and len(candidates) == 1:
                candidate = candidates[0]
                if not candidate.get("submitted_path") and not candidate.get("scan_label"):
                    matches = candidates
            whole = matches[0] if len(matches) == 1 else None
            row = []
            if len(summaries) > 1:
                row.append(_submission_label(summary, idx, blinded=blinded))
            if mixed_challenges:
                row.append(str(summary.get("challenge_type") or "").upper() or "Not recorded")
            # Which scan. Without it the DCE layout prints one hundred and
            # eighty rows that differ only in their numbers.
            if scan_labels_available:
                row.append(str(item.get("scan_label") or "Not identified"))
            row.extend([
                map_type,
                str(item.get("units") or "Not provided"),
                _pct(stats.get("finite_percent")),
                _pct(stats.get("negative_voxel_percent")),
                _fmt(stats.get("mean")),
            ])
            if reference_available:
                row.extend([
                    _fmt(whole.get("rmse") if whole else None),
                    _fmt(whole.get("mae") if whole else None),
                    _fmt(whole.get("bias") if whole else None),
                    _fmt(whole.get("correlation") if whole else None),
                ])
            main_map_metric_rows.append(row)
    issues: list[list[str]] = []
    seen_issues: set[tuple[str, str, str, str, str]] = set()
    for idx, s in enumerate(summaries, start=1):
        label = _submission_label(s, idx, blinded=blinded)
        for severity, key in (("Blocking error", "errors"), ("Needs review", "warnings")):
            for msg in (s.get(key) or []):
                if isinstance(msg, Mapping):
                    message = str(msg.get("message") or msg.get("code") or "Issue recorded.")
                    affected = affected_display(
                        msg.get("path"), s, label, blinded=blinded)
                else:
                    message = str(msg)
                    affected = "Not specified"
                row = [
                    severity,
                    label,
                    message,
                    affected,
                    "Fix and validate again." if severity == "Blocking error" else "Review before sharing.",
                ]
                signature = tuple(row)
                if signature not in seen_issues:
                    seen_issues.add(signature)
                    issues.append(row)

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
        ])
        rows.append(row)

    lead_lines = [
        f"{len(summaries)} submission{'s' if len(summaries) != 1 else ''}; "
        f"{map_count} readable parameter map{'s' if map_count != 1 else ''}"
        + (f" across {', '.join(challenges)}." if challenges else "."),
    ]
    if errors:
        lead_lines.append(
            f"{errors} blocking error{'s' if errors != 1 else ''} prevented "
            "completion; affected submissions are listed under Items requiring review."
        )
    elif warnings:
        lead_lines.append(
            f"No blocking errors were recorded. {warnings} "
            f"warning{'s' if warnings != 1 else ''} require review before sharing."
        )
    else:
        lead_lines.append("No blocking errors or warnings were recorded.")
    lead_lines.append(
        "Compatible reference maps were available; comparison metrics are reported."
        if reference_available else REFERENCE_UNAVAILABLE_NOTE
    )
    if is_mixed_challenge:
        lead_lines.append(
            "Because this batch spans more than one challenge, aggregates are "
            "reported per challenge; no cross-challenge totals are computed."
        )

    compared = sum(1 for af in fields if _reference_available(af))
    methods_lines = [
        "Readable NIfTI maps were summarised using finite-voxel, NaN/Inf, "
        "negative-voxel, and map-level descriptive checks."
    ]
    if reference_available:
        methods_lines.append(
            f"Reference comparisons were available for {compared} of "
            f"{len(summaries)} submission{'s' if len(summaries) != 1 else ''}: "
            "bias is submitted minus reference; MAE and RMSE summarise "
            "voxelwise error, with ROI results reported where masks match."
        )
    methods_lines.append(
        "Values retain their configured map units and are not pooled across "
        "parameters or challenges. Team and contact details were withheld."
        if blinded else
        "Values retain their configured map units and are not pooled across "
        "parameters or challenges. This organiser report includes identity fields."
    )

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
            "Results are aggregated per challenge, no cross-challenge totals are computed."
        )
    if warnings:
        summary_lines.append(f"{warnings} warning{'s' if warnings != 1 else ''} reported.")
    if errors:
        summary_lines.append(f"{errors} error{'s' if errors != 1 else ''} reported.")

    notes = []
    if warnings:
        notes.append(
            "Warnings indicate files or metadata that may need review but did "
            "not prevent report export."
        )

    return {
        "title": "OSIPI Submission Review Report",
        "session_name": _summary_title(summaries, tag, blinded=blinded),
        "challenge_type": _challenge_text(summaries),
        "generated": generated.strftime("%Y-%m-%d %H:%M UTC"),
        "export_date": generated.strftime("%Y-%m-%d"),
        "pipeline_version": provenance["pipeline_version"],
        "configuration_version": provenance["challenge_configuration"],
        "analysis_provenance": provenance,
        # Submission contents, grouped. A clean 16-scan DCE submission is
        # eight rows here where the per-map appendix was sixty-seven cards
        # across a dozen pages, which buried the results it was meant to
        # support. The detail remains available in JSON and CSV, and the
        # appendix can still be requested explicitly.
        "submission_contents": _submission_contents_rows(summaries),
        "submission_contents_headers": [
            "Dataset", "Type", "Count", "Dimensions", "Units", "Status"],
        "per_map_sections": (
            _per_map_sections(summaries, blinded=blinded) if include_map_appendix else []),
        "blinded": blinded,
        "submission_count": len(summaries),
        "map_count": map_count,
        "map_types": _detected_map_types(fields),
        "submission_metadata_headers": (
            ["Submission", "Challenge", "Map types", "Maps"]
            + ([] if blinded else ["Team", "Contact"])
        ),
        "submission_metadata_rows": _submission_metadata_rows(summaries, blinded=blinded),
        # Map preview thumbnails removed from the printable report at researcher
        # request (previews remain in the interactive app).
        "previews": [],
        # Per-submission series for the figures. Reference metrics are None
        # when no reference was available, and the figure builders skip
        # non-numeric values rather than plotting them as zero.
        "figure_rows": [
            {
                "label": _submission_label(s, i, blinded=blinded),
                # Drives per-challenge figure grouping; without it every
                # submission lands in one unit-mixed axis.
                "challenge": str(s.get("challenge_type") or "").upper(),
                "rmse": af.get("reference_mean_rmse") if _reference_available(af) else None,
                "mae": af.get("reference_mean_mae") if _reference_available(af) else None,
                "bias": af.get("reference_mean_bias") if _reference_available(af) else None,
                "finite": af.get("finite_voxels_percent"),
            }
            for i, (s, af) in enumerate(zip(summaries, fields), start=1)
        ],
        # Per-map-type agreement points plus the units to label their axes.
        # ROI descriptive rows, formatted once here. HTML and PDF both render
        # these exact rows in this exact order, neither reformats, refilters,
        # or recomputes, which is what kept the two formats in step before.
        **roi_model,
        **prototype_model,
        **header_check_model,
        **region_model,
        "analysis_availability": {
            "qc_and_previews": bool(map_count),
            "roi_statistics": roi_available,
            "reference_comparison": reference_available,
            "grouped_descriptive": grouped_available,
            "signal_rss": rss_available,
            "provider_analysis": any(
                bool((s.get("score_result") or s.get("scoring_result") or {}).get("metrics"))
                for s in summaries
            ),
            "official_ranking": False,
        },
        "submission_type": submission_type,
        "methods_document": _methods_document_status(summaries),
        "reviewer_summary": reviewer_summary,
        "review_statuses": {
            "Validation": validation_review_status,
            "Execution": execution_review_status,
            "QC": qc_review_status,
            "Reference comparison": reference_status,
            # Stated even though nothing requires one, because a blank line
            # here reads as "there was none" and as "nobody recorded it"
            # equally well, and those are different facts.
            "Methods document": _methods_document_status(summaries),
        },
        "executive_metrics": {
            "Maps": map_count,
            "Map types": _detected_map_types(fields),
            "Finite voxels": _pct(finite),
            "NaN / Inf": f"{nan_count} / {inf_count}",
            "Negative voxels": _pct(negative),
            "Reference": reference_status,
        },
        "main_map_metric_headers": (
            (["Submission"] if len(summaries) > 1 else [])
            + (["Challenge"] if mixed_challenges else [])
            + (["Scan"] if scan_labels_available else [])
            + ["Map", "Units", "Finite", "Negative", "Mean"]
            + (["RMSE", "MAE", "Bias", "Corr."] if reference_available else [])
        ),
        "main_map_metric_rows": main_map_metric_rows,
        "agreement_points": agreement_points(summaries, blinded=blinded),
        "map_units": {
            mt: _map_units(summaries, mt)
            for mt in agreement_points(summaries, blinded=blinded)
        },
        "lead_lines": lead_lines,
        "methods_lines": methods_lines,
        "summary_lines": summary_lines,
        "status_cards": {
            **_status_fields(validation_status, execution_status,
                             qc_status, export_readiness),
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
        # The key-figures band was removed (a paper puts its numbers in a
        # table, and the band repeated the results table anyway), so the
        # aggregate statistics live here again.
        "qc": {
            "Submissions": len(summaries),
            "Maps": map_count,
            "Map types": _detected_map_types(fields),
            "Finite voxels": _pct(finite),
            "NaN / Inf": f"{nan_count} / {inf_count}",
            "Negative voxels": _pct(negative),
            **(
                {"Map means": "Reported per challenge"}
                if is_mixed_challenge else map_mean_items
            ),
        },
        # "Reference comparisons" was a second field carrying the same
        # available/not-available value as "Reference status"; dropped.
        "scoring": {
            "Reference status": reference_status,
            **_reference_metric_items(),
        },
        "reference_status": reference_status,
        "reference_metrics_by_challenge": reference_metrics_by_challenge,
        "notes": notes,
        "issues": issues,
        "limitations": build_limitations(
            reference_available=reference_available,
            map_types=[m.strip() for m in _detected_map_types(fields).split(",")
                       if m.strip() and m.strip() != "Not available"],
            challenges=challenges,
            cov_reported=cov is not None,
            icc_status=_first_icc_status(summaries),
            mask_overlaps=_first_mask_overlaps(summaries),
        ),
        # "Notes" held counts like "1 warning(s) reported" while the issues
        # table below already lists each warning in full with its fix, so the
        # column was a less useful restatement of the next table.
        "table_headers": (
            ["Submission"]
            + ([] if blinded else ["Team", "Contact"])
            + ["Challenge", "Map types", "Maps", "Finite voxels", "NaN / Inf",
               "Negative voxels", "Map means", "Reference status",
               "RMSE", "MAE", "Bias"]
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
    if model.get("icc_rows"):
        lines.extend(["ICC results", " | ".join(model["icc_headers"])])
        lines.extend(" | ".join(str(cell) for cell in row) for row in model["icc_rows"])
    lines.append(" | ".join(str(h) for h in model["table_headers"]))
    for row in model["rows"]:
        lines.append(" | ".join(str(cell) for cell in row))
    lines.extend(["", "Notes / Limitations"])
    lines.extend(f"- {item}" for item in model["notes"])
    lines.extend(f"- {item}" for item in model["limitations"])
    lines.extend(["", "Analysis provenance"])
    lines.extend(
        f"- {key.replace('_', ' ').title()}: {value}"
        for key, value in (model.get("analysis_provenance") or {}).items()
    )
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
    """Render the branded PDF.

    Layout notes:
      * Every page uses portrait letter dimensions.
      * Page 1 uses a tall masthead (logo + gradient band); later pages use a
        slim running header. That needs two PageTemplates, hence
        BaseDocTemplate rather than SimpleDocTemplate.
      * "Page X of Y" requires knowing the total up front, so pages are
        buffered by ``NumberedCanvas`` and the footer is stamped in a second
        pass once the count is known.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.platypus import (
        BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate,
        PageTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    C = {k: colors.HexColor(v) for k, v in BRAND.items()}
    PAGE_W, PAGE_H = letter
    MARGIN = 0.75 * inch
    CONTENT_W = PAGE_W - 2 * MARGIN
    # Must clear the whole canvas-drawn masthead: 0.50in top gap + the
    # lockup height + 0.13in to the rule + 0.58in down to the deck
    # baseline, plus descenders and a gap before the leader paragraph.
    FIRST_HEADER_H = 1.88 * inch    # lockup + rule + title block
    LATER_HEADER_H = 0.50 * inch    # slim running head thereafter
    FOOTER_H = 0.42 * inch

    logo_path = logo_reportlab_path()

    # ── Styles ────────────────────────────────────────────────────────────
    # Use one clean sans-serif family throughout so the PDF matches the app and
    # the self-contained HTML report. Helvetica is a base-14 PDF font, so the
    # report remains portable without loading an external webfont.
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "OsipiBody", parent=styles["BodyText"],
        fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=C["ink"],
    )
    # Section headings: letterspaced small caps over a hairline. The rule is
    # drawn by the wrapper table in section(), not by the paragraph.
    heading_style = ParagraphStyle(
        "OsipiHeading", parent=body_style,
        fontName="Helvetica-Bold", fontSize=9, leading=11,
        textColor=C["ink"], charSpace=0,
    )
    caption_style = ParagraphStyle(
        "OsipiCaption", parent=body_style,
        fontName="Helvetica-Oblique", fontSize=7.5, leading=10,
        textColor=C["muted"], spaceBefore=5, spaceAfter=11,
    )
    bullet_style = ParagraphStyle(
        "OsipiBullet", parent=body_style,
        fontSize=8, leading=11, leftIndent=11, bulletIndent=2, spaceAfter=2,
        textColor=C["ink_soft"],
    )
    label_style = ParagraphStyle(
        "OsipiLabel", parent=body_style,
        fontName="Helvetica-Bold", fontSize=6.5, leading=8,
        textColor=C["subtle"], charSpace=0,
    )
    table_header_style = ParagraphStyle(
        "OsipiTableHeader", parent=body_style,
        fontName="Helvetica-Bold", fontSize=6.3, leading=8, textColor=C["subtle"],
        charSpace=0,
    )
    table_header_right = ParagraphStyle(
        "OsipiTableHeaderR", parent=table_header_style, alignment=2,
    )
    table_cell_style = ParagraphStyle(
        "OsipiTableCell", parent=body_style, fontSize=6.6, leading=8.4,
        textColor=C["ink_soft"],
    )
    table_cell_right = ParagraphStyle(
        "OsipiTableCellR", parent=table_cell_style, alignment=2,
    )
    note_style = ParagraphStyle(
        "OsipiNote", parent=body_style,
        fontName="Helvetica-Oblique", fontSize=8, leading=10.5, textColor=C["muted"],
    )

    def esc(text: Any) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def para(text: Any, style=body_style) -> Paragraph:
        return Paragraph(esc(text), style)

    def section(title: str, width: float | None = None) -> Table:
        """Letterspaced small-caps heading sitting on a hairline rule.

        ``width`` defaults to the content measure; callers pass their own
        when the rule must span something narrower.
        """
        table = Table([[Paragraph(esc(str(title)), heading_style)]],
                      colWidths=[width or CONTENT_W], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 11),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, C["hairline"]),
        ]))
        return KeepTogether([table, Spacer(1, 4)])

    def status_para(value: Any, style=table_cell_style) -> Paragraph:
        """Coloured dot plus plain text, never a filled pill.

        U+25CF is present in the base-14 Helvetica encoding, so this needs no
        embedded font. Both dot and text carry the tone, which keeps the
        signal legible when the page is printed in greyscale.
        """
        tone = status_tone(value)
        return Paragraph(
            f'<font color="{BRAND[tone]}">●</font> '
            f'<font color="{BRAND[tone]}">{esc(value)}</font>',
            style,
        )

    # booktabs: horizontal rules only. No verticals, no fills, no striping.
    _BASE_TABLE = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
    ]

    def fitted_widths(headers: Sequence[Any], rows: Sequence[Sequence[Any]],
                      total: float) -> list[float]:
        """Share the measure out by how much text each column carries.

        Equal columns are what broke the headings: "Submission" and
        "Reference status" got the same width as "Maps", so ReportLab split
        them mid-word into "SUBMISSI / ON". Weighting by the longest word in
        each column means a column is at least wide enough for its own
        heading, which is the thing that has to stay readable.
        """
        n = max(1, len(headers))
        # Headings are set uppercase and letterspaced, so they need more room
        # per character than the cells beneath them.
        HEADING_WIDTH = 1.9
        PADDING = 2.0
        weights: list[float] = []
        for index in range(n):
            heading = str(headers[index]) if index < len(headers) else ""
            # The longest single word, not the whole heading: "Reference
            # status" may wrap at the space, it just may not break "Reference".
            longest_word = max((len(w) for w in heading.split()), default=len(heading))
            longest_cell = 0
            for row in rows[:60]:
                if index < len(row):
                    longest_cell = max(longest_cell, len(str(row[index] or "")))
            weights.append(PADDING + max(
                4.0,
                min(longest_word * HEADING_WIDTH, 20.0),
                # A little over the longest value: "44.72%" fits exactly at
                # six and then wraps its own percent sign onto a second line.
                min(longest_cell + 1.5, 24.0),
            ))
        scale = total / sum(weights)
        return [w * scale for w in weights]

    def data_table(headers: Sequence[Any], rows: Sequence[Sequence[Any]],
                   col_widths: Sequence[float] | None = None,
                   tone_col: int | None = None,
                   num_cols: Sequence[int] = ()) -> Table:
        """A booktabs table: rule above the header, below it, and at the foot.

        ``tone_col`` renders that column as a coloured status dot plus text.
        ``num_cols`` right-aligns those columns so magnitudes compare down
        the column rather than ragging against the left edge.
        """
        widths = col_widths or fitted_widths(headers, rows, CONTENT_W)
        numeric = set(num_cols)
        cells = [[
                Paragraph(esc(str(h)),
                      table_header_right if c in numeric else table_header_style)
            for c, h in enumerate(headers)
        ]]
        for row in rows:
            rendered = []
            for c, value in enumerate(row):
                if tone_col is not None and c == tone_col:
                    rendered.append(status_para(value))
                elif c in numeric:
                    rendered.append(para(value, table_cell_right))
                else:
                    rendered.append(para(value, table_cell_style))
            cells.append(rendered)
        table = Table(cells, repeatRows=1, hAlign="LEFT", colWidths=list(widths))
        table.setStyle(TableStyle(_BASE_TABLE + [
            ("LINEABOVE", (0, 0), (-1, 0), 0.7, C["rule"]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.45, C["rule"]),
            ("LINEBELOW", (0, 1), (-1, -2), 0.3, C["faint"]),
            ("LINEBELOW", (0, -1), (-1, -1), 0.7, C["rule"]),
            ("TOPPADDING", (0, 0), (-1, 0), 5),
            ("BOTTOMPADDING", (0, 0), (-1, 0), 5),
        ]))
        return table


    def caption(text: str) -> Paragraph:
        """Italic serif caption set below its table, journal-style."""
        return Paragraph(esc(text), caption_style)

    def kv_table(items: Mapping[str, Any], width: float = 6.4 * inch,
                 tone_keys: Sequence[str] = ()) -> Table:
        """Two-column measure/value table, ruled top and bottom only."""
        toned = set(tone_keys)
        rows = []
        for key, value in items.items():
            rows.append([
                para(key, table_cell_style),
                status_para(value, table_cell_right) if key in toned
                else para(value, table_cell_right),
            ])
        if not rows:
            rows = [[para("No data", table_cell_style), para("", table_cell_right)]]
        table = Table(rows, colWidths=[width * 0.52, width * 0.48], hAlign="LEFT")
        table.setStyle(TableStyle(_BASE_TABLE + [
            ("TEXTCOLOR", (0, 0), (0, -1), C["muted"]),
            ("LINEABOVE", (0, 0), (-1, 0), 0.7, C["rule"]),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, C["faint"]),
            ("LINEBELOW", (0, -1), (-1, -1), 0.7, C["rule"]),
        ]))
        return table

    def paired_kv_table(items: Mapping[str, Any], width: float = 6.4 * inch) -> Table:
        """Build a compact two-pair table for availability metadata."""
        pairs = list(items.items())
        rows = []
        for index in range(0, len(pairs), 2):
            left = pairs[index]
            right = pairs[index + 1] if index + 1 < len(pairs) else ("", "")
            rows.append([
                para(left[0], table_cell_style),
                para(left[1], table_cell_right),
                para(right[0], table_cell_style),
                para(right[1], table_cell_right),
            ])
        table = Table(
            rows,
            colWidths=[width * 0.27, width * 0.23, width * 0.27, width * 0.23],
            hAlign="LEFT",
        )
        table.setStyle(TableStyle(_BASE_TABLE + [
            ("TEXTCOLOR", (0, 0), (0, -1), C["muted"]),
            ("TEXTCOLOR", (2, 0), (2, -1), C["muted"]),
            ("LINEABOVE", (0, 0), (-1, 0), 0.7, C["rule"]),
            ("LINEBELOW", (0, 0), (-1, -2), 0.3, C["faint"]),
            ("LINEBELOW", (0, -1), (-1, -1), 0.7, C["rule"]),
            ("LINEBEFORE", (2, 0), (2, -1), 0.3, C["faint"]),
            ("LEFTPADDING", (2, 0), (2, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ]))
        return table

    def two_up(left, right, ratio: float = 0.5, gap: float = 0.22 * inch) -> Table:
        """Place two flowables side by side.

        A lone key/value table leaves dead space to its right. Pairing
        related blocks keeps the report compact.
        """
        usable = CONTENT_W - gap
        table = Table(
            [[left, "", right]],
            colWidths=[usable * ratio, gap, usable * (1 - ratio)],
            hAlign="LEFT",
        )
        table.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        return table

    def sub_label(text: str) -> Paragraph:
        return Paragraph(esc(str(text).upper()), label_style)


    def figures(items: Mapping[str, Any]) -> Table:
        """Key figures as a ruled band: label above, numeral below.

        Hairline verticals separate the figures. This is the one place
        vertical rules are used, because the figures are not tabular data
        and the rules are doing grouping work rather than table work.
        """
        entries = list(items.items())
        if not entries:
            return Spacer(1, 0)
        cells, cmds = [], []
        for col, (label, value) in enumerate(entries):
            text = str(value)
            # Size and leading adapt to long values that need to wrap.
            size = 14.0
            if len(text) > 26:
                size = 9.5
            elif len(text) > 16:
                size = 11.0
            value_style = ParagraphStyle(
                f"OsipiFigVal{col}", parent=body_style,
                fontName=PDF_SANS, fontSize=size, leading=size * 1.18,
                textColor=C["ink"], spaceBefore=2,
            )
            cells.append([
                Paragraph(esc(str(label)), label_style),
                Paragraph(esc(text), value_style),
            ])
            if col:
                cmds.append(("LINEBEFORE", (col, 0), (col, 0), 0.3, C["faint"]))
        width = CONTENT_W / len(entries)
        table = Table([cells], colWidths=[width] * len(entries), hAlign="LEFT")
        table.setStyle(TableStyle(_BASE_TABLE + cmds + [
            ("LINEABOVE", (0, 0), (-1, 0), 0.7, C["rule"]),
            ("LINEBELOW", (0, 0), (-1, 0), 0.5, C["hairline"]),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ("LEFTPADDING", (1, 0), (-1, 0), 9),
        ]))
        return table


    # ── Page furniture ────────────────────────────────────────────────────
    def _draw_logo(canvas, x, y, size):
        if not logo_path:
            return False
        try:
            canvas.drawImage(logo_path, x, y, width=size, height=size,
                             mask="auto", preserveAspectRatio=True, anchor="c")
            return True
        except Exception:
            return False

    def _spaced(canvas, x, y, text, font, size, tracking):
        """Draw letterspaced text, advancing one glyph at a time.

        ReportLab's canvas exposes no public character-spacing setter (only a
        private ``_charSpace``), so rather than reach into internals we
        measure and place each glyph. Returns the final x for chaining.
        """
        canvas.setFont(font, size)
        for ch in str(text):
            canvas.drawString(x, y, ch)
            x += pdfmetrics.stringWidth(ch, font, size) + tracking
        return x

    def _page_dims(canvas):
        """Current page size. Every template is portrait letter."""
        try:
            return canvas._pagesize
        except Exception:
            return (PAGE_W, PAGE_H)

    def draw_first_header(canvas, doc_obj):
        """Running head, then the thick/thin rule pair, then the title block.

        The rule pair is the journal signal: a hairline under the running
        head and a 3pt rule beneath it, with the title hanging below.
        """
        canvas.saveState()
        page_w, page_h = _page_dims(canvas)
        top = page_h
        # The official lockup already contains the mark, the OSIPI wordmark,
        # and the tagline, so nothing is set alongside it, typesetting our
        # own wordmark next to it would duplicate the artwork.
        lockup_w = 2.70 * inch
        lockup_h = lockup_w / max(0.1, lockup_aspect())
        logo_bottom = top - 0.50 * inch - lockup_h
        lockup = lockup_reportlab_path()
        if lockup:
            try:
                canvas.drawImage(lockup, MARGIN, logo_bottom,
                                 width=lockup_w, height=lockup_h,
                                 mask=None, preserveAspectRatio=True,
                                 anchor="sw")
            except Exception:
                lockup = None
        if not lockup:
            # Fall back to the mark plus typeset wordmark.
            _draw_logo(canvas, MARGIN, logo_bottom, lockup_h)
            canvas.setFillColor(C["ink"])
            _spaced(canvas, MARGIN + lockup_h + 0.16 * inch,
                    logo_bottom + lockup_h / 2, "OSIPI", "Helvetica-Bold", 13, 1.2)

        centre = logo_bottom + lockup_h / 2
        canvas.setFillColor(C["muted"])
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(page_w - MARGIN, centre + 0.04 * inch,
                               str(model["export_date"]))
        canvas.drawRightString(
            page_w - MARGIN, centre - 0.13 * inch,
            "Blinded report" if model["blinded"] else "Unblinded report")

        # One thin rule. The old thick bar plus hairline put a heavy black
        # band across the top of every report and dominated the page.
        rule_y = logo_bottom - 0.13 * inch
        canvas.setStrokeColor(C["rule"])
        canvas.setLineWidth(0.8)
        canvas.line(MARGIN, rule_y, page_w - MARGIN, rule_y)

        # Title block hangs below the rule pair.
        canvas.setFillColor(C["ink"])
        canvas.setFont("Helvetica-Bold", 23)
        canvas.drawString(MARGIN, rule_y - 0.40 * inch, "Submission review report")
        canvas.setFillColor(C["muted"])
        canvas.setFont("Helvetica", 9.5)
        deck = str(model["session_name"])
        if model.get("challenge_type"):
            deck += f"  ·  {model['challenge_type']}"
        canvas.drawString(MARGIN, rule_y - 0.58 * inch, deck)
        canvas.restoreState()

    def draw_later_header(canvas, doc_obj):
        """Slim running head: mark, wordmark, hairline. No fills."""
        canvas.saveState()
        page_w, page_h = _page_dims(canvas)
        y = page_h - LATER_HEADER_H
        x = MARGIN
        if _draw_logo(canvas, x, y + 0.01 * inch, 0.34 * inch):
            x += 0.44 * inch
        canvas.setFillColor(C["ink"])
        end = _spaced(canvas, x, y + 0.12 * inch, "OSIPI", "Helvetica-Bold", 8.5, 1.0)
        canvas.setFillColor(C["muted"])
        canvas.setFont("Helvetica", 8)
        canvas.drawString(end + 0.10 * inch, y + 0.12 * inch, "Submission review report")
        canvas.setFont("Helvetica", 7.5)
        canvas.drawRightString(page_w - MARGIN, y + 0.10 * inch,
                               str(model["session_name"]))
        canvas.setStrokeColor(C["rule"])
        canvas.setLineWidth(0.5)
        canvas.line(MARGIN, y - 0.04 * inch, page_w - MARGIN, y - 0.04 * inch)
        canvas.restoreState()

    class NumberedCanvas(pdfcanvas.Canvas):
        """Buffers pages so the footer can print 'Page X of Y'."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._pages: list[dict] = []

        def showPage(self):
            self._pages.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._pages)
            for state in self._pages:
                self.__dict__.update(state)
                self._draw_footer(total)
                super().showPage()
            super().save()

        def _draw_footer(self, total: int):
            self.saveState()
            page_w = self._pagesize[0]
            provenance = model.get("analysis_provenance") or {}

            def _compact_provenance(value: Any) -> str:
                text = str(value or "not configured")
                parts = [part.strip() for part in text.split(";") if part.strip()]
                scoped_values = {
                    part.split(":", 1)[1].strip()
                    for part in parts if ":" in part
                }
                if parts and len(scoped_values) == 1 and len(scoped_values) == len({
                    part.split(":", 1)[1].strip() for part in parts if ":" in part
                }):
                    # ASL/DCE/DSC often share one version or one unconfigured
                    # state. Printing that common value once avoids a footer
                    # longer than the page measure.
                    return next(iter(scoped_values))
                return text if len(text) <= 62 else "per challenge; full details in HTML/JSON"

            self.setStrokeColor(C["hairline"])
            self.setLineWidth(0.5)
            self.line(MARGIN, FOOTER_H, page_w - MARGIN, FOOTER_H)
            self.setFont("Helvetica", 7)
            self.setFillColor(C["muted"])
            self.drawString(
                MARGIN, FOOTER_H - 11,
                f"Pipeline version {model['pipeline_version']}  ·  "
                f"Configuration version {model['configuration_version']}  ·  "
                f"{model['generated']}",
            )
            self.drawRightString(page_w - MARGIN, FOOTER_H - 11,
                                 f"{self._pageNumber} / {total}")
            self.setFont("Helvetica", 6.2)
            self.drawString(
                MARGIN, FOOTER_H - 20,
                "ANALYSIS PROVENANCE  ·  "
                f"Scoring package: {_compact_provenance(provenance.get('scoring_package'))}  ·  "
                f"Reference dataset: {_compact_provenance(provenance.get('reference_dataset'))}  ·  "
                f"Analysis date: {provenance.get('analysis_date', 'not available')}",
            )
            self.restoreState()

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=(PAGE_W, PAGE_H),
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=FOOTER_H + 0.12 * inch,
        title=str(model["title"]), author="OSIPI Perfusion Pipeline",
        subject=f"Submission review report, {model['session_name']}",
        # Keep text uncompressed so blinding tests can inspect PDF bytes.
        pageCompression=0,
    )
    body_bottom = FOOTER_H + 0.12 * inch
    # Frame defaults to 6pt padding on every side, which pushed all flowed
    # content 6pt right of the canvas-drawn masthead and made tables 12pt
    # wider than their frame. Zero it so flow and canvas share one margin.
    _pad = dict(leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    doc.addPageTemplates([
        PageTemplate(
            id="first", pagesize=(PAGE_W, PAGE_H), onPage=draw_first_header,
            frames=[Frame(MARGIN, body_bottom, CONTENT_W,
                          PAGE_H - FIRST_HEADER_H - 0.16 * inch - body_bottom,
                          id="f1", **_pad)],
        ),
        PageTemplate(
            id="later", pagesize=(PAGE_W, PAGE_H), onPage=draw_later_header,
            frames=[Frame(MARGIN, body_bottom, CONTENT_W,
                          PAGE_H - LATER_HEADER_H - 0.16 * inch - body_bottom,
                          id="f2", **_pad)],
        ),
        # Same geometry as "later". Kept as a separate id so the story can
        # still mark where the wide tables begin without rotating the page.
        PageTemplate(
            id="wide", pagesize=(PAGE_W, PAGE_H), onPage=draw_later_header,
            frames=[Frame(MARGIN, body_bottom, CONTENT_W,
                          PAGE_H - LATER_HEADER_H - 0.16 * inch - body_bottom,
                          id="f3", **_pad)],
        ),
    ])

    # ── Story ─────────────────────────────────────────────────────────────
    story: list = [NextPageTemplate("later")]

    story.append(section("Key results"))
    story.append(figures(model["review_statuses"]))
    if str(model.get("reference_status") or "").lower() == "not available":
        story.append(Paragraph(
            "No compatible reference was provided. Reference comparison is "
            "not available for this report.",
            note_style,
        ))
    story.append(paired_kv_table(model["executive_metrics"], width=CONTENT_W))

    story.append(section("Results"))
    map_rows = model.get("main_map_metric_rows") or []
    if map_rows:
        map_headers = model["main_map_metric_headers"]
        story.append(data_table(
            map_headers,
            map_rows,
            num_cols=[i for i, value in enumerate(map_headers)
                      if value in {"Finite", "Mean", "RMSE", "MAE", "Bias", "Corr."}],
        ))
    else:
        story.append(Paragraph("No readable map metrics were available.", note_style))

    # Placed ahead of the results because it qualifies them: a map whose
    # geometry disagrees with the reference produces numbers that look
    # ordinary and mean nothing, so a reviewer needs this before the metrics
    # rather than after them.
    header_rows = model.get("header_check_rows") or []
    if header_rows:
        header_headers = list(model.get("header_check_headers") or [])
        story.append(section("Header and orientation check"))
        story.append(data_table(header_headers, header_rows))
        if any(row and row[-1] == "Geometry differs" for row in header_rows):
            story.append(Paragraph(
                "One or more maps differ from the reference in shape, voxel size "
                "or orientation. Comparison metrics for those maps are not "
                "reliable until the difference is explained.",
                note_style,
            ))

    roi_rows = model.get("roi_descriptive_display_rows") or []
    if roi_rows:
        # Columns that never vary have already been lifted out of the display
        # rows by _roi_descriptive_model, so a single-scan submission no longer
        # prints four columns of dashes. Resolve the rest by name, never by
        # fixed offsets: configurable metrics change both count and order.
        roi_headers = list(model.get("roi_descriptive_display_headers") or [])
        configured_labels = [
            dict(ROI_METRIC_COLUMNS)[metric]
            for metric in model.get("roi_descriptive_report_metrics") or ()
            if metric in dict(ROI_METRIC_COLUMNS)
        ]
        keep = [
            header for header in (
                "Dataset", "Participant", "Repeat", "Site", "Map", "ROI",
                *configured_labels, "Voxels",
            ) if header in roi_headers
        ]
        column_indexes = [roi_headers.index(header) for header in keep]
        compact_roi_rows = [[row[index] for index in column_indexes] for row in roi_rows]
        compact_display_headers = [
            {"Map": "Parameter", "ROI": "Region"}.get(header, header) for header in keep
        ]
        numeric_headers = set(configured_labels) | {"Voxels"}
        story.append(section("ROI results"))
        scope = model.get("roi_descriptive_scope") or {}
        if scope:
            # States once what would otherwise repeat identically on every row.
            story.append(Paragraph(
                "  ·  ".join(f"{label}: {value}" for label, value in scope.items()),
                note_style,
            ))
        story.append(data_table(
            compact_display_headers, compact_roi_rows,
            num_cols=[
                index for index, header in enumerate(keep)
                if header in numeric_headers
            ],
        ))

    region_rows = model.get("reference_region_rows") or []
    if region_rows:
        story.append(section("Comparison against ground truth, by region"))
        story.append(Paragraph(
            "A whole-image figure can hide opposite regional errors that cancel. "
            "The whole image is shown as its own row for that reason.",
            note_style))
        headers = list(model.get("reference_region_headers") or [])
        # The submission column repeats one value on a single-submission
        # report, so it is dropped there the way the ROI table drops its
        # constant identity columns.
        if len({row[0] for row in region_rows}) == 1:
            headers, region_rows = headers[1:], [row[1:] for row in region_rows]
        story.append(data_table(
            headers, region_rows,
            num_cols=[i for i, h in enumerate(headers)
                      if h in {"Bias", "MAE", "RMSE", "Error CoV", "Corr.", "Voxels"}],
        ))

    # ── Submitted outputs & reference comparison, per submission and per map ──
    ref_cols = ["ROI", "RMSE", "MAE", "Bias", "Error CoV", "Corr", "Valid vox", "Excl vox"]
    for sec in model.get("per_map_sections") or []:
        story.append(section(
            f"Appendix · submitted outputs, {sec['label']}"
            + (f"  ({sec['challenge']})" if sec.get("challenge") else "")
        ))
        for m in sec["maps"]:
            story.append(figures({
                "Map type": m["map_type"],
                "Units": m["units"],
                "Shape": m["shape"],
                "Finite voxels": _pct(m["finite_percent"]),
                # _status_text normalises reference_not_available -> Not
                # available; .title() left it as the much longer, wrapping
                # "Reference Not Available".
                "Reference": _status_text(m["reference_status"]),
            }))
            story.append(Spacer(1, 0.1 * inch))
            # "Parameter" is only shown when it differs from the map type in
            # the band directly above; otherwise it repeats "CBF: CBF".
            properties = kv_table({
                **({"Parameter": m["display"]}
                   if str(m["display"]).strip() != str(m["map_type"]).strip() else {}),
                "Dimensions": f"{m['dimensions']}D" if m.get("dimensions") else "Not available",
                "Voxel size": m["voxel_size"],
                "NaN / Inf": f"{m.get('nan_count') or 0} / {m.get('inf_count') or 0}",
                "Negative voxels %": _fmt(m["negative_percent"]),
            }, width=(CONTENT_W - 0.22 * inch) * 0.38)
            if m["roi_rows"]:
                roi_w = (CONTENT_W - 0.22 * inch) * 0.62
                roi_table = data_table(
                    ref_cols,
                    [[r["roi"], _fmt(r["rmse"]), _fmt(r["mae"]), _fmt(r["bias"]),
                      _fmt(r["error_cov"]), _fmt(r["correlation"]),
                      _fmt(r["valid"], 0), _fmt(r["excluded"], 0)] for r in m["roi_rows"]],
                    col_widths=[roi_w * 0.22] + [roi_w * 0.78 / 7] * 7,
                    num_cols=range(1, 8),
                )
                story.append(two_up(
                    [sub_label("Submitted map properties"), properties],
                    [sub_label("Reference comparison by ROI"), roi_table],
                    ratio=0.38,
                ))
            else:
                story.append(sub_label("Submitted map properties"))
                story.append(properties)
            if m.get("difference_map"):
                story.append(Paragraph(
                    "Difference map generated (submitted - reference), "
                    "preserving the source affine.", note_style))
            story.append(Spacer(1, 0.1 * inch))

    preview_rows = []
    for item in model.get("previews") or []:
        path = Path(str(item.get("image_path") or ""))
        if not path.exists():
            continue
        preview_rows.append([
            Image(str(path), width=1.2 * inch, height=1.2 * inch),
            para(f"{item.get('submission', '')}<br/>{item.get('map', '')}<br/>{item.get('file', '')}",
                 table_cell_style),
        ])
    if preview_rows:
        story.append(section("Parameter map previews"))
        preview_table = Table(preview_rows, colWidths=[1.35 * inch, 3.3 * inch], hAlign="LEFT")
        preview_table.setStyle(TableStyle(_BASE_TABLE + [
            ("BOX", (0, 0), (-1, -1), 0.5, C["border"]),
            ("INNERGRID", (0, 0), (-1, -1), 0.3, C["divider"]),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ]))
        story.append(preview_table)

    icc_rows = model.get("icc_rows") or []
    if icc_rows:
        story.append(section("ICC results"))
        story.append(data_table(model["icc_headers"], icc_rows, num_cols=[3, 5, 6]))
        story.append(caption("Models are reported separately. No pass/fail threshold is applied."))
    grouped_rows = model.get("grouped_roi_rows") or []
    rss_rows = model.get("dce_rss_rows") or []
    if grouped_rows or rss_rows:
        story.append(section("Prototype descriptive analyses"))
    if grouped_rows:
        story.append(data_table(
            model["grouped_roi_headers"], grouped_rows, num_cols=[4, 5, 6, 7]
        ))
        story.append(caption(
            "Grouped scan-level ROI medians with population SD and CoV. "
            "A signed pair difference is shown only for two clearly matched repeats or sites. "
            "These values are descriptive and are not ICC, formal repeatability, pass/fail, or ranking."
        ))
    if rss_rows:
        story.append(data_table(
            model["dce_rss_headers"], rss_rows, num_cols=[5, 6, 7, 8]
        ))
        story.append(caption(
            "Residual Sum of Squares (RSS): raw voxelwise sum across time of "
            "(measured - modelled)^2, summarized by region. This is not deviance or official scoring."
        ))

    if model["issues"]:
        story.append(section("Issues and recommendations"))
        pdf_issues = sorted(
            model["issues"],
            key=lambda row: (0 if row and row[0] == "Blocking error" else 1,
                             row[1] if len(row) > 1 else ""),
        )[:3]
        story.append(data_table(
            ["Severity", "Submission", "Message", "Affected file", "Recommended action"],
            pdf_issues,
            col_widths=[0.85 * inch, 1.05 * inch, CONTENT_W - 4.35 * inch,
                        1.05 * inch, 1.40 * inch],
            tone_col=0,
        ))
        if len(model["issues"]) > len(pdf_issues):
            story.append(caption(
                f"{len(model['issues']) - len(pdf_issues)} further item(s) are available in the HTML report."
            ))

    story.append(section("Notes and limitations"))
    story.extend(
        Paragraph(esc(item), bullet_style, bulletText="-")
        for item in (list(model["notes"]) + list(model["limitations"]))[:4]
    )

    doc.build(story, canvasmaker=NumberedCanvas)
    return buffer.getvalue()


def generate_pdf_report(
    summaries: Sequence[Mapping[str, Any]],
    *,
    tag: str,
    blinded: bool = True,
    generated: datetime | None = None,
    include_map_appendix: bool = False,
) -> bytes:
    """Generate a compact OSIPI PDF report from existing export summaries.

    ``include_map_appendix`` restores the per-map detail cards. Off by default:
    a clean 16-scan DCE submission produced a dozen pages of near-identical
    cards that buried the results they were meant to support. The same detail
    remains available in the JSON and CSV exports.
    """
    if not summaries:
        raise ValueError("At least one summary is required.")
    model = _build_report_model(summaries, tag=tag, blinded=blinded,
                                generated=generated,
                                include_map_appendix=include_map_appendix)
    try:
        return _reportlab_pdf_bytes(model)
    except Exception:
        # Log rendering failures before using the plain-text fallback.
        logger.exception(
            "ReportLab PDF rendering failed; falling back to the plain-text PDF."
        )
        return _simple_pdf_bytes(_report_lines(model))
