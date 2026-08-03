"""PDF report generation for OSIPI export summaries."""

from __future__ import annotations

import io
import json
import logging
import textwrap
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from services.ingest_service import make_safe_id
from services.path_config import OUTPUTS_DIR
from services.report_branding import (
    BRAND,
    PDF_MONO,
    PDF_SANS,
    PDF_MONO_BOLD,
    lockup_aspect,
    lockup_reportlab_path,
    logo_reportlab_path,
    status_tone,
)
from osipi_pipeline.scoring.descriptive_statistics import (
    METHODOLOGY as DESCRIPTIVE_METHODOLOGY,
)
from services.report_figures import bland_altman_figure, to_drawing
from osipi_pipeline.config.rules import challenge_labels, map_type_specs

logger = logging.getLogger(__name__)


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


CONTENTS_CAPTION = (
    "Table 1. What the submission contains, grouped by dataset and type. Parameter maps, fitted signals and documents are counted separately; organiser reference data is not counted as submitted content."
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
                str(row.get("dimensions") or "—"),
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


ROI_TABLE_HEADERS = (
    "Dataset", "Participant", "Repeat", "Site", "ROI",
    "Median", "SD", "CoV", "Voxels", "Units", "Status",
)

_UNAVAILABLE = "Unavailable"

#: Shared by both formats so the wording cannot drift.
ROI_METHOD_TEXT = (
    "ROI statistics were calculated from finite Ktrans voxels within each "
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


def _roi_descriptive_model(
    summaries: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    """Collect the canonical ROI records and render them once.

    Reads ``reference_scoring.roi_descriptive_statistics`` — the records
    computed during scoring. Nothing here recalculates a statistic.
    """
    records: list[dict] = []
    for summary in summaries:
        analysis = summary.get("nifti_analysis")
        analysis = analysis if isinstance(analysis, Mapping) else {}
        scoring = analysis.get("reference_scoring")
        scoring = scoring if isinstance(scoring, Mapping) else {}
        for record in scoring.get("roi_descriptive_statistics") or ():
            if isinstance(record, Mapping):
                records.append(dict(record))

    # Deterministic order, shared by both formats.
    records.sort(key=lambda r: tuple(
        str(r.get(k) or "") for k in
        ("dataset", "participant", "repeat", "site", "roi_id")
    ))

    rows = [[
        str(r.get("dataset") or "—"),
        str(r.get("participant") or "—"),
        str(r.get("repeat") or "—"),
        # Clinical datasets leave the site implicit; shown as a dash, not "0".
        str(r.get("site") or "—"),
        str(r.get("roi_label") or r.get("roi_id") or "—"),
        _roi_number(r.get("roi_median")),
        _roi_number(r.get("roi_within_scan_sd")),
        _roi_percent(r.get("roi_within_scan_cov")),
        _fmt(r.get("voxel_count") or 0, 0),
        str(r.get("units") or "—"),
        str(r.get("unavailable_reason") or r.get("status") or "—").replace("_", " "),
    ] for r in records]

    available = sum(1 for r in records if r.get("status") == "available")
    return {
        "roi_descriptive_rows": rows,
        "roi_descriptive_headers": list(ROI_TABLE_HEADERS),
        "roi_descriptive_records": records,
        "roi_descriptive_methodology": dict(DESCRIPTIVE_METHODOLOGY),
        "roi_descriptive_summary": {
            "total_rows": len(records),
            "available_rows": available,
            "unavailable_rows": len(records) - available,
            "datasets": sorted({str(r.get("dataset") or "") for r in records} - {""}),
        },
    }


def agreement_points(summaries: Sequence[Mapping[str, Any]], *,
                     blinded: bool) -> dict[str, list[dict]]:
    """Collect per-region agreement points, keyed by map type.

    Reads the full ``reference_scoring`` block, which keeps richer stats than
    ``reference_metric_rows`` surfaces — ``mean_submitted``,
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


def _status_fields(validation: str, execution: str, qc: str,
                   readiness: str) -> dict[str, str]:
    """Keep only the status fields that carry information for this run.

    All four derive from the same two counters, so on most runs three of them
    restate each other — "Unable to continue" appearing twice teaches the
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


def build_limitations(
    *,
    reference_available: bool,
    map_types: Sequence[str],
    challenges: Sequence[str],
    cov_reported: bool,
) -> list[str]:
    """Build the caveat list, including only caveats that actually apply.

    Shared by the HTML and PDF renderers. Previously both printed the same
    eight bullets on every report, which trained readers to skip the section
    — a caveat about repeatability CoV is noise on a run that computed no
    reference metrics at all. Wording is derived from the run rather than
    hardcoded, so a DCE-only report no longer claims something about ASL.
    """
    items = [
        "Basic NIfTI QC checks readability and generic voxel statistics; "
        "it is not full BIDS validation.",
    ]
    if reference_available:
        items.append(
            "Generic reference metrics are not official OSIPI scores unless "
            "an official scoring provider is configured."
        )
        items.append(UNAVAILABLE_METRICS_NOTE)
        if cov_reported:
            items.append(
                "The reported coefficient of variation is an accuracy "
                "error-CoV, not a repeatability CoV."
            )
    # Only meaningful once more than one parameter type is present, and it
    # should name the types actually found rather than assume CBF and ATT.
    named = [str(m).strip() for m in map_types if str(m).strip()]
    if len(named) > 1:
        items.append(
            f"{' and '.join((', '.join(named[:-1]), named[-1]))} are reported "
            "separately and never averaged together, because their units differ."
        )
    known = [str(c).strip() for c in challenges if str(c).strip()]
    if known:
        items.append(
            f"No official overall {'/'.join(known)} score has been defined; "
            "no pass/fail scientific threshold is applied."
        )
    items.append(
        "Missing values are reported as Not available and are never "
        "converted to zero."
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

    Collapses the derived forms one name takes across the pipeline —
    ``Secret Team Omega``, ``secret_team_omega``, ``SECRET-TEAM-OMEGA``,
    ``secret team omega.zip`` — to a single comparable token, so a check
    cannot be defeated by a formatting difference.
    """
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


def identity_tokens(summary: Mapping[str, Any]) -> frozenset[str]:
    """Normalised forms of everything that could name the submitter.

    Used only as a final safety net *after* structural selection has already
    chosen a safe value — never as a search-and-replace over rendered output,
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
    """The value the "Affected" column may show for one issue.

    Issue records carry an absolute filesystem path. Its basename — which both
    renderers used — is the submission directory for submission-level issues,
    and that directory name *is* the submission id, derived in turn from the
    uploaded archive name. A blinded report therefore printed the team's name
    in the issues table while blinding it everywhere else, and leaked the
    reviewer's local directory layout besides. See CODE_WALKTHROUGH.md §B5.

    Selection is structural, in order:

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
    """A download filename fragment that respects blinding.

    Export filenames were built from the raw submission or batch id, so a
    blinded report downloaded as ``osipi_report_team_gamma_Clinical.html``.
    Blinded exports get a neutral fragment instead; unblinded exports are
    unchanged.
    """
    if not blinded:
        return str(tag or "report")
    return "blinded"


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
        # The "grouped by challenge" caveat is a sentence, not a measure, and
        # the leader paragraph already states it; it no longer occupies a row.
        out = {}
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
                    affected = affected_display(
                        msg.get("path"), s, label, blinded=blinded)
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
        ])
        rows.append(row)

    # Leader paragraph for the PDF: the same facts as the old bullet list,
    # set as prose so the report opens the way a paper opens.
    lead_lines = [
        f"This report covers {len(summaries)} "
        f"submission{'s' if len(summaries) != 1 else ''} comprising {map_count} "
        f"parameter map{'s' if map_count != 1 else ''}"
        + (f" across {', '.join(challenges)}." if challenges else "."),
    ]
    if errors:
        lead_lines.append(
            f"{errors} blocking error{'s' if errors != 1 else ''} prevented "
            "completion; affected submissions are listed in Table 4."
        )
    elif warnings:
        lead_lines.append(
            f"No blocking errors were recorded. {warnings} "
            f"warning{'s' if warnings != 1 else ''} require review before sharing."
        )
    else:
        lead_lines.append("No blocking errors or warnings were recorded.")
    lead_lines.append(
        "Reference maps were available and reference metrics are reported."
        if reference_available else REFERENCE_UNAVAILABLE_NOTE
    )
    if is_mixed_challenge:
        lead_lines.append(
            "Because this batch spans more than one challenge, aggregates are "
            "reported per challenge; no cross-challenge totals are computed."
        )

    # Methods: what was actually done to this batch, in prose. A reader has
    # to be able to tell what "bias" here means and what it was measured
    # against before the numbers mean anything.
    compared = sum(1 for af in fields if _reference_available(af))
    methods_lines = [
        "Each submission was checked for readable NIfTI volumes and "
        "summarised by voxel-level statistics: the proportion of finite "
        "voxels, counts of NaN and infinite values, and the proportion of "
        "negative voxels."
    ]
    if reference_available:
        methods_lines.append(
            f"Reference maps were available for {compared} of "
            f"{len(summaries)} submission{'s' if len(summaries) != 1 else ''}. "
            "For those, each submitted map was compared against its reference "
            "over the whole image and over each supplied region of interest. "
            "Bias is the mean of submitted minus reference; MAE and RMSE are "
            "the mean absolute and root-mean-square voxelwise errors; the "
            "coefficient of variation is the standard deviation of that error "
            "divided by the reference mean."
        )
    else:
        methods_lines.append(
            "No matching reference maps were available, so no agreement "
            "metrics were computed and only quality-control statistics are "
            "reported below."
        )
    methods_lines.append(
        "All metrics carry the units of the parameter they describe and are "
        "never pooled across parameters or challenges."
    )
    methods_lines.append(
        "Team and contact details were withheld from this report."
        if blinded else
        "This is an unblinded report and includes team and contact details."
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
        # these exact rows in this exact order — neither reformats, refilters,
        # or recomputes, which is what kept the two formats in step before.
        **_roi_descriptive_model(summaries),
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
            "Map means": ("Reported per challenge" if is_mixed_challenge else map_means),
        },
        # "Reference comparisons" was a second field carrying the same
        # available/not-available value as "Reference status"; dropped.
        "scoring": {
            "Reference status": reference_status,
            **_reference_metric_items(),
        },
        "reference_status": reference_status,
        "notes": notes,
        "issues": issues,
        "limitations": build_limitations(
            reference_available=reference_available,
            map_types=[m.strip() for m in _detected_map_types(fields).split(",")
                       if m.strip() and m.strip() != "Not available"],
            challenges=challenges,
            cov_reported=cov is not None,
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
    """Render the branded landscape PDF.

    Layout notes:
      * Page 1 uses a tall masthead (logo + gradient band); later pages use a
        slim running header. That needs two PageTemplates, hence
        BaseDocTemplate rather than SimpleDocTemplate.
      * "Page X of Y" requires knowing the total up front, so pages are
        buffered by ``NumberedCanvas`` and the footer is stamped in a second
        pass once the count is known.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import landscape, letter
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas as pdfcanvas
    from reportlab.platypus import (
        BaseDocTemplate, Frame, Image, KeepTogether, NextPageTemplate, PageBreak,
        PageTemplate, Paragraph, Spacer, Table, TableStyle,
    )

    C = {k: colors.HexColor(v) for k, v in BRAND.items()}
    # Portrait, like a paper. The body was landscape only because one table
    # is wide, which meant every other page ran a 10in measure and left
    # ~45% of itself empty. That table now gets its own landscape page
    # instead (a mixed-orientation PDF is standard for wide tables).
    PAGE_W, PAGE_H = letter
    WIDE_W, WIDE_H = landscape(letter)
    MARGIN = 0.75 * inch
    CONTENT_W = PAGE_W - 2 * MARGIN
    WIDE_CONTENT_W = WIDE_W - 2 * MARGIN
    # Must clear the whole canvas-drawn masthead: 0.50in top gap + the
    # lockup height + 0.13in to the rule + 0.58in down to the deck
    # baseline, plus descenders and a gap before the leader paragraph.
    FIRST_HEADER_H = 1.88 * inch    # lockup + rule + title block
    LATER_HEADER_H = 0.50 * inch    # slim running head thereafter
    FOOTER_H = 0.42 * inch

    logo_path = logo_reportlab_path()

    # ── Styles ────────────────────────────────────────────────────────────
    # Times carries display type (the journal signal); Helvetica carries data,
    # where tabular figures matter more than voice. Both are base-14 fonts, so
    # no font embedding or external files are required.
    styles = getSampleStyleSheet()
    body_style = ParagraphStyle(
        "OsipiBody", parent=styles["BodyText"],
        fontName="Helvetica", fontSize=8.5, leading=11.5, textColor=C["ink"],
    )
    lead_style = ParagraphStyle(
        "OsipiLead", parent=body_style,
        fontName="Times-Roman", fontSize=10.5, leading=14.5,
        textColor=C["ink_soft"], spaceAfter=0,
    )
    # Section headings: letterspaced small caps over a hairline. The rule is
    # drawn by the wrapper table in section(), not by the paragraph.
    heading_style = ParagraphStyle(
        "OsipiHeading", parent=body_style,
        fontName=PDF_MONO_BOLD, fontSize=7.5, leading=9.5,
        textColor=C["subtle"], charSpace=1.1,
    )
    caption_style = ParagraphStyle(
        "OsipiCaption", parent=body_style,
        fontName="Times-Italic", fontSize=8, leading=10.5,
        textColor=C["muted"], spaceBefore=5, spaceAfter=11,
    )
    bullet_style = ParagraphStyle(
        "OsipiBullet", parent=body_style,
        fontSize=8, leading=11, leftIndent=11, bulletIndent=2, spaceAfter=2,
        textColor=C["ink_soft"],
    )
    label_style = ParagraphStyle(
        "OsipiLabel", parent=body_style,
        fontName=PDF_MONO, fontSize=6.2, leading=8,
        textColor=C["subtle"], charSpace=0.5,
    )
    table_header_style = ParagraphStyle(
        "OsipiTableHeader", parent=body_style,
        fontName="Helvetica", fontSize=6.3, leading=8, textColor=C["subtle"],
        charSpace=0.35,
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
        fontName="Times-Italic", fontSize=8, leading=10.5, textColor=C["muted"],
    )

    def esc(text: Any) -> str:
        return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def para(text: Any, style=body_style) -> Paragraph:
        return Paragraph(esc(text), style)

    def section(title: str, width: float | None = None) -> Table:
        """Letterspaced small-caps heading sitting on a hairline rule.

        ``width`` defaults to the portrait measure; the landscape results
        page passes its own so the rule spans the frame rather than stopping
        short of the table beneath it.
        """
        table = Table([[Paragraph(esc(str(title).upper()), heading_style)]],
                      colWidths=[width or CONTENT_W], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 16),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LINEBELOW", (0, 0), (-1, -1), 0.5, C["hairline"]),
        ]))
        return KeepTogether([table, Spacer(1, 5)])

    def status_para(value: Any, style=table_cell_style) -> Paragraph:
        """Coloured dot plus plain text — never a filled pill.

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

    def data_table(headers: Sequence[Any], rows: Sequence[Sequence[Any]],
                   col_widths: Sequence[float] | None = None,
                   tone_col: int | None = None,
                   num_cols: Sequence[int] = ()) -> Table:
        """A booktabs table: rule above the header, below it, and at the foot.

        ``tone_col`` renders that column as a coloured status dot plus text.
        ``num_cols`` right-aligns those columns so magnitudes compare down
        the column rather than ragging against the left edge.
        """
        n_cols = max(1, len(headers))
        widths = col_widths or [CONTENT_W / n_cols] * n_cols
        numeric = set(num_cols)
        cells = [[
            Paragraph(esc(str(h).upper()),
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

    def two_up(left, right, ratio: float = 0.5, gap: float = 0.22 * inch) -> Table:
        """Place two flowables side by side.

        The page is landscape, so a lone key/value table leaves ~3.5in of dead
        space to its right. Pairing related blocks keeps the report compact.
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
            # Size to fit. A long value (a six-item map-type list, a
            # seven-digit voxel count) has to wrap, and the label paragraph's
            # leading is far too tight for a 14pt line — the wrapped lines
            # used to sit on top of each other. Each figure therefore gets a
            # value style whose leading matches its own size.
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
                Paragraph(esc(str(label).upper()), label_style),
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
        """Current page size. Varies: the results table page is landscape."""
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
        # and the tagline, so nothing is set alongside it — typesetting our
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
                    logo_bottom + lockup_h / 2, "OSIPI", PDF_MONO_BOLD, 13, 2.4)

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
        canvas.setFont("Times-Roman", 26)
        canvas.drawString(MARGIN, rule_y - 0.40 * inch, "Evaluation report")
        canvas.setFillColor(C["muted"])
        canvas.setFont("Times-Italic", 10)
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
        end = _spaced(canvas, x, y + 0.12 * inch, "OSIPI", PDF_MONO_BOLD, 8.5, 1.8)
        canvas.setFillColor(C["muted"])
        canvas.setFont("Times-Italic", 8)
        canvas.drawString(end + 0.10 * inch, y + 0.12 * inch, "Evaluation report")
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
            self.restoreState()

    buffer = io.BytesIO()
    doc = BaseDocTemplate(
        buffer, pagesize=landscape(letter),
        leftMargin=MARGIN, rightMargin=MARGIN,
        topMargin=MARGIN, bottomMargin=FOOTER_H + 0.12 * inch,
        title=str(model["title"]), author="OSIPI Perfusion Pipeline",
        subject=f"Evaluation report — {model['session_name']}",
        # Deliberately uncompressed. Compression saves ~25% but hides page
        # text inside Flate streams, and the blinded-report guarantee is
        # verified by grepping the PDF bytes for team names and folder paths.
        # With compression on that check passes vacuously even if a name
        # leaks, so the size is worth paying for a privacy property that can
        # actually be tested.
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
        # The per-submission results table has a dozen columns and will not
        # read at a 7in measure, so it gets a landscape page of its own.
        PageTemplate(
            id="wide", pagesize=(WIDE_W, WIDE_H), onPage=draw_later_header,
            frames=[Frame(MARGIN, body_bottom, WIDE_CONTENT_W,
                          WIDE_H - LATER_HEADER_H - 0.16 * inch - body_bottom,
                          id="f3", **_pad)],
        ),
    ])

    # ── Story ─────────────────────────────────────────────────────────────
    story: list = [NextPageTemplate("later")]

    def constrained(flowable, width: float) -> Table:
        """Hold a flowable to a set measure.

        The page is landscape, so text left to fill the frame runs to ~110
        characters a line — roughly half again the length at which the eye
        reliably finds the next line. This caps the leader at about 80.
        """
        table = Table([[flowable]], colWidths=[width], hAlign="LEFT")
        table.setStyle(TableStyle([
            ("LEFTPADDING", (0, 0), (-1, -1), 0),
            ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ("TOPPADDING", (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        return table

    # Leader paragraph: the outcome in sentences, the way a paper states it,
    # rather than as a row of status tiles.
    # ── Summary ───────────────────────────────────────────────────────────
    # The status band and the key-figures band both went: the band repeated
    # the results table, and the four statuses restated what this paragraph
    # already says in sentences.
    story.append(section("Summary"))
    story.append(constrained(
        Paragraph(esc(" ".join(model["lead_lines"])), lead_style), CONTENT_W))

    story.append(section("Methods"))
    for line in model["methods_lines"]:
        story.append(constrained(Paragraph(esc(line), lead_style), CONTENT_W))
        story.append(Spacer(1, 0.06 * inch))

    # The "Submissions" table used to sit here listing submission, challenge,
    # map types, and map count. Every one of those columns also appears in the
    # results table, so it has been removed rather than shown twice.
    #
    # QC and reference agreement are closely related and each is narrow, so
    # they sit side by side in one splittable table.
    # Now that the duplicated voxel rows live only in the figures band, the
    # QC side holds a single row, so the two-column split has been folded
    # back into one table. It stays splittable across pages.
    # Always emitted, empty or not, so the two formats number tables alike.
    contents = model.get("submission_contents") or [
        ["Not available for this submission.", "", "", "", "", ""]]
    story.append(section("Submission contents"))
    story.append(data_table(model["submission_contents_headers"], contents))
    story.append(caption(CONTENTS_CAPTION))

    story.append(section("Results"))
    story.append(kv_table({**model["qc"], **model["scoring"]},
                          width=CONTENT_W,
                          tone_keys=["Reference status"]))
    story.append(caption(
        "Table 2. Aggregate quality-control statistics and reference agreement. "
        + CAPTION_AGGREGATE_TAIL))

    # ── Figures ───────────────────────────────────────────────────────────
    # One agreement figure per challenge: RMSE, MAE, and bias carry the units
    # of the map they describe, so ASL and DCE cannot share an axis.
    # One figure per parameter: Bland-Altman. The RMSE/MAE dot plot, the
    # identity plot, and the finite-voxel plot were all cut — the first two
    # restate what Bland-Altman and the results table already show, and a
    # three-point plot of values between 98% and 99% is not worth a figure.
    fig_w = (CONTENT_W - 0.30 * inch) / 2
    blocks: list[list] = []
    for map_type, pts in (model.get("agreement_points") or {}).items():
        units = (model.get("map_units") or {}).get(map_type, "map units")
        ba = bland_altman_figure(pts, units=units, width=fig_w)
        if not ba:
            continue
        n = len(blocks) + 1
        limits = ba.get("limits")
        interval = (
            f" Limits of agreement: {_fmt(limits[0], 2)} to "
            f"{_fmt(limits[1], 2)} {units}." if limits else ""
        )
        blocks.append([
            sub_label(f"Figure {n} · Bland-Altman, {map_type}"),
            Spacer(1, 4), to_drawing(ba),
            caption(f"Figure {n}. Agreement between submitted and reference "
                    f"{map_type}. Each point is one region of one submission; "
                    "the solid line is zero bias and the dashed lines are the "
                    "pooled 95% limits of agreement." + interval),
        ])
    if blocks:
        # Two across; a trailing odd figure pairs with an empty cell. Each
        # row is a nested table and so cannot split, so the heading is bound
        # to the first row — otherwise it strands at the foot of a page with
        # its figures overleaf.
        rows_out = [
            two_up(pair[0], pair[1] if len(pair) > 1 else [Spacer(1, 1)])
            for pair in (blocks[i:i + 2] for i in range(0, len(blocks), 2))
        ]
        story.append(KeepTogether([section("Figures"), rows_out[0]]))
        for row in rows_out[1:]:
            # A caption's spaceAfter is absorbed by its table cell, so without
            # an explicit gap the next row's label butts against it.
            story.append(Spacer(1, 0.20 * inch))
            story.append(row)

    # ── Submitted outputs & reference comparison, per submission and per map ──
    ref_cols = ["ROI", "RMSE", "MAE", "Bias", "Error CoV", "Corr", "Valid vox", "Excl vox"]
    for sec in model.get("per_map_sections") or []:
        story.append(section(
            f"Appendix · submitted outputs — {sec['label']}"
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
                    "Difference map generated (submitted − reference), "
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

    # Switch to the landscape template for the wide table, then switch back.
    story.append(NextPageTemplate("wide"))
    story.append(PageBreak())
    story.append(section("Results by submission", WIDE_CONTENT_W))
    headers = list(model["table_headers"])
    # Column positions shift with blinding (Team/Contact are dropped), so
    # locate them by name rather than hard-coding indices.
    ref_col = headers.index("Reference status") if "Reference status" in headers else None
    numeric_names = {"Maps", "Finite voxels", "NaN / Inf", "Negative voxels",
                     "RMSE", "MAE", "Bias"}
    num_cols = [i for i, h in enumerate(headers)
                if h in numeric_names or str(h).startswith("Mean ")]
    story.append(data_table(headers, model["rows"], tone_col=ref_col,
                            num_cols=num_cols,
                            col_widths=[WIDE_CONTENT_W / max(1, len(headers))]
                                       * len(headers)))
    story.append(caption(
        "Table 3. Per-submission quality control and reference agreement. "
        "Measures that could not be computed are reported as Not available "
        "and are never converted to zero."))

    # Permanently numbered Table 3, present whether or not rows exist, so a
    # cross-reference means the same thing in both formats.
    story.append(section("ROI Ktrans statistics"))
    roi_rows = model.get("roi_descriptive_rows") or []
    roi_summary = model.get("roi_descriptive_summary") or {}
    story.append(data_table(
        model.get("roi_descriptive_headers") or list(ROI_TABLE_HEADERS),
        roi_rows or [["—"] * len(ROI_TABLE_HEADERS)],
        num_cols=[5, 6, 7, 8],
    ))
    if roi_rows:
        story.append(caption(
            f"Table 4. Within-ROI Ktrans statistics: "
            f"{roi_summary.get('available_rows', 0)} of "
            f"{roi_summary.get('total_rows', 0)} scan-ROI combinations "
            f"available. {ROI_METHOD_TEXT}"))
    else:
        story.append(caption(
            "Table 4. Within-ROI Ktrans statistics. None were available for "
            f"this submission. {ROI_METHOD_TEXT}"))

    story.append(NextPageTemplate("later"))
    story.append(PageBreak())
    # Always a captioned table, even when empty. Dropping it on a clean run
    # made the PDF number its tables 1-2 while the HTML numbered 1-3, so a
    # caption reference meant different things in the two formats.
    story.append(section("Errors and warnings"))
    story.append(data_table(
        ["Severity", "Submission", "Message", "Affected file", "Recommended action"],
        model["issues"][:24] or [["None recorded", "—", "—", "—", "—"]],
        col_widths=[0.85 * inch, 1.05 * inch, CONTENT_W - 4.35 * inch,
                    1.05 * inch, 1.40 * inch],
        tone_col=0,
    ))
    omitted = (
        f" {len(model['issues']) - 24} further issues are omitted here; see "
        "the HTML report or CSV export for the full list."
        if len(model["issues"]) > 24 else ""
    )
    story.append(caption(
        "Table 5. Errors and warnings raised during validation, with the "
        "action required before the submission can be shared." + omitted))

    story.append(section("Limitations"))
    story.extend(
        Paragraph(esc(item), bullet_style, bulletText="—")
        for item in list(model["notes"]) + list(model["limitations"])
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
        # The fallback keeps report export working when ReportLab is missing,
        # but it also used to hide genuine rendering bugs behind a plausible
        # looking plain-text PDF. Log the traceback so those surface.
        logger.exception(
            "ReportLab PDF rendering failed; falling back to the plain-text PDF."
        )
        return _simple_pdf_bytes(_report_lines(model))
