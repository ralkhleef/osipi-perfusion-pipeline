"""Phase 4B: ROI descriptive statistics wired through the real pipeline.

Integration and presentation only. The formulas are covered by
``test_roi_descriptive.py`` and are not re-tested here; these tests assert
that the canonical records reach every output *once*, identically.
"""

from __future__ import annotations

import csv
import io
import json

import pytest

from services.pdf_report_service import (
    ROI_METHOD_TEXT,
    ROI_TABLE_HEADERS,
    _build_report_model,
    generate_pdf_report,
)


# ── Fixtures ──────────────────────────────────────────────────────────────

def _roi_record(**kw):
    base = {
        "challenge": "dce", "dataset": "synthetic", "participant": "1",
        "repeat": "1", "site": "1", "map_type": "ktrans",
        "roi_id": "tumour", "roi_label": "Tumour", "units": "min^-1",
        "roi_median": 0.183, "roi_within_scan_sd": 0.042,
        "roi_within_scan_cov": 0.2295, "voxel_count": 1250,
        "mask_voxel_count": 1300, "excluded_non_finite_count": 50,
        "negative_count": 2, "zero_count": 7,
        "status": "available", "unavailable_reason": None,
    }
    base.update(kw)
    return base


def _summary(records, idx=1):
    return {
        "submission_id": f"sub-{idx}", "source_folder": f"team_{idx}",
        "challenge_type": "dce", "warning_count": 0, "error_count": 0,
        "warnings": [], "errors": [], "exec_status": "skipped_result_maps",
        "nifti_analysis": {
            "maps": [],
            "reference_scoring": {
                "maps": [],
                "roi_descriptive_statistics": records,
                "roi_descriptive_methodology": {"standard_deviation": "population SD, ddof=0"},
            },
        },
        "analysis_fields": {
            "parameter_maps_detected": "Ktrans", "map_count": 1,
            "finite_voxels_percent": 99.0, "nan_count": 0, "inf_count": 0,
            "negative_voxels_percent": 0.0, "finite_voxel_count": 99,
            "total_voxel_count": 100, "negative_voxel_count": 0,
            "means_by_map_type": {}, "mean_coefficient_of_variation": None,
            "reference_based_scoring_available": False,
            "reference_compared_map_count": 0,
            "reference_scoring_status": "reference_not_available",
            "reference_mean_rmse": None, "reference_mean_mae": None,
            "reference_mean_bias": None, "reference_metric_rows": [],
        },
    }


def _model(records):
    return _build_report_model([_summary(records)], tag="t", blinded=True)


# ── Canonical model ───────────────────────────────────────────────────────

def test_model_carries_rows_headers_and_summary() -> None:
    model = _model([_roi_record()])
    assert model["roi_descriptive_headers"] == list(ROI_TABLE_HEADERS)
    assert len(model["roi_descriptive_rows"]) == 1
    assert model["roi_descriptive_summary"]["available_rows"] == 1


def test_cov_is_a_ratio_in_records_and_a_percentage_in_rows() -> None:
    model = _model([_roi_record()])
    assert model["roi_descriptive_records"][0]["roi_within_scan_cov"] == 0.2295
    assert "22.95%" in model["roi_descriptive_rows"][0]


def test_unavailable_renders_as_unavailable_not_zero() -> None:
    model = _model([_roi_record(
        roi_median=None, roi_within_scan_sd=None, roi_within_scan_cov=None,
        voxel_count=0, status="empty_roi", unavailable_reason="empty_roi")])
    row = model["roi_descriptive_rows"][0]
    assert "Unavailable" in row
    assert "0" not in {row[5], row[6], row[7]}
    assert model["roi_descriptive_summary"]["unavailable_rows"] == 1


def test_clinical_implicit_site_renders_as_unavailable_not_zero() -> None:
    model = _model([_roi_record(dataset="clinical", site=None)])
    assert model["roi_descriptive_rows"][0][3] == "Not available"


def test_rows_are_deterministically_ordered() -> None:
    records = [
        _roi_record(participant="2", roi_id="b", roi_label="B"),
        _roi_record(participant="1", roi_id="a", roi_label="A"),
    ]
    rows = _model(records)["roi_descriptive_rows"]
    assert [r[1] for r in rows] == ["1", "2"]


def test_no_records_yields_no_rows_but_keeps_the_table() -> None:
    model = _model([])
    assert model["roi_descriptive_rows"] == []
    assert model["roi_descriptive_summary"]["total_rows"] == 0
    assert model["roi_descriptive_headers"]


# ── Report presentation ───────────────────────────────────────────────────

def _pdf_text(records):
    pdf = generate_pdf_report([_summary(records)], tag="t", blinded=True)
    return pdf.decode("latin-1", errors="ignore")


def _html_text(records, monkeypatch):
    import main

    data = [_summary(records)]
    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: ["sub-1"])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: data[0])
    return main.export_report(submission_id="sub-1", batch_id=None,
                              blinded=True).body.decode("utf-8")


@pytest.mark.parametrize("records,label", [
    ([_roi_record()], "synthetic only"),
    ([_roi_record(dataset="clinical", site=None)], "clinical only"),
    ([_roi_record(), _roi_record(dataset="clinical", site=None, participant="9")],
     "mixed datasets"),
    ([_roi_record(), _roi_record(roi_id="x", roi_label="X", status="empty_roi",
                                roi_median=None, roi_within_scan_sd=None,
                                roi_within_scan_cov=None)],
     "available and unavailable"),
    ([], "no ROI results"),
])
def test_html_keeps_full_roi_rows_while_pdf_stays_concise(
    records, label, monkeypatch,
) -> None:
    """Both formats use one model, with detail matched to the medium."""
    model = _model(records)
    expected = len(model["roi_descriptive_rows"])

    html = _html_text(records, monkeypatch)
    pdf = _pdf_text(records)

    assert pdf.startswith("%PDF-"), label
    if expected:
        assert "ROI Results" in html, label
        for record in model["roi_descriptive_records"]:
            if record.get("status") == "available":
                assert record["roi_label"] in html, label
    else:
        assert "ROI Results" not in html, label
    assert expected == len(_model(records)["roi_descriptive_rows"])


def test_dynamic_report_sections_do_not_use_stale_table_numbers(monkeypatch) -> None:
    records = [_roi_record()]
    html = _html_text(records, monkeypatch)
    pdf = _pdf_text(records)
    for number in ("Table 1.", "Table 3.", "Table 4.", "Table 5."):
        assert number not in html, number
        assert number not in pdf, number


def test_limitations_are_not_repeated_in_html(monkeypatch) -> None:
    html = _html_text([_roi_record()], monkeypatch)
    assert html.count("Basic NIfTI QC checks readability") == 1


def test_methodology_text_is_shared_not_duplicated(monkeypatch) -> None:
    records = [_roi_record()]
    assert "population definition" in ROI_METHOD_TEXT
    assert "subject to final confirmation by OSIPI" in ROI_METHOD_TEXT
    assert "ddof=0" in ROI_METHOD_TEXT
    assert ROI_METHOD_TEXT[:40] in _html_text(records, monkeypatch)


def test_report_never_claims_repeatability_or_accuracy(monkeypatch) -> None:
    """These are within-scan spatial summaries and must not imply otherwise."""
    html = _html_text([_roi_record()], monkeypatch)
    section = html[html.index("ROI Results"):]
    section = section[: section.index("Issues &amp; Limitations")]
    for forbidden in ("Repeatability", "Reproducibility", "Accuracy",
                      "Deviance", "Inter-participant", "Inter-site"):
        assert forbidden not in section, forbidden


# ── Escaping ──────────────────────────────────────────────────────────────

def test_hostile_units_and_labels_are_escaped(monkeypatch) -> None:
    """Units and ROI labels come from configurable/submitted metadata."""
    hostile = "<script>alert(1)</script>"
    records = [_roi_record(units=hostile, roi_label=hostile)]
    html = _html_text(records, monkeypatch)
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


# ── Duplicate computation ─────────────────────────────────────────────────

def test_outputs_never_recompute_roi_statistics(monkeypatch) -> None:
    """The architectural guarantee: compute once, read many.

    Any output format calling the calculator would mean the same voxels are
    re-read per export, and worse, that two formats could disagree.
    """
    import services.roi_descriptive_service as svc

    calls = {"n": 0}
    real = svc.compute_roi_descriptive_statistics

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "compute_roi_descriptive_statistics", counting)

    records = [_roi_record()]
    _model(records)                      # report model
    _pdf_text(records)                   # PDF
    _html_text(records, monkeypatch)     # HTML
    json.dumps(_model(records)["roi_descriptive_records"])   # JSON
    assert calls["n"] == 0, (
        "an output format recomputed ROI statistics instead of reading them")


# ── CSV export ────────────────────────────────────────────────────────────

def _csv_rows(text: str) -> list[list[str]]:
    return list(csv.reader(io.StringIO(text)))


def test_csv_export_has_stable_columns_and_raw_values(monkeypatch) -> None:
    import main

    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: ["sub-1"])
    monkeypatch.setattr(main, "_gather_summary",
                        lambda sid: _summary([_roi_record()]))
    body = main.export_roi_descriptive(
        submission_id="sub-1", batch_id=None).body.decode("utf-8")
    rows = _csv_rows(body)
    assert rows[0] == list(main.ROI_CSV_COLUMNS)
    data = rows[1]
    cov = data[main.ROI_CSV_COLUMNS.index("roi_within_scan_cov")]
    assert cov == "0.2295"
    assert "%" not in cov


def test_csv_leaves_unavailable_blank_and_site_blank(monkeypatch) -> None:
    import main

    record = _roi_record(dataset="clinical", site=None, roi_median=None,
                         roi_within_scan_sd=None, roi_within_scan_cov=None,
                         status="empty_roi", unavailable_reason="empty_roi")
    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: ["sub-1"])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: _summary([record]))
    rows = _csv_rows(main.export_roi_descriptive(
        submission_id="sub-1", batch_id=None).body.decode("utf-8"))
    data = rows[1]
    for column in ("roi_median", "roi_within_scan_sd", "roi_within_scan_cov", "site"):
        assert data[main.ROI_CSV_COLUMNS.index(column)] == "", column


def test_csv_with_no_records_is_header_only(monkeypatch) -> None:
    import main

    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: ["sub-1"])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: _summary([]))
    rows = _csv_rows(main.export_roi_descriptive(
        submission_id="sub-1", batch_id=None).body.decode("utf-8"))
    assert rows[0] == list(main.ROI_CSV_COLUMNS)
    assert len(rows) == 1


def test_csv_escapes_hostile_labels(monkeypatch) -> None:
    import main

    record = _roi_record(roi_label='Tumour","injected')
    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: ["sub-1"])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: _summary([record]))
    rows = _csv_rows(main.export_roi_descriptive(
        submission_id="sub-1", batch_id=None).body.decode("utf-8"))
    assert rows[1][main.ROI_CSV_COLUMNS.index("roi_label")] == 'Tumour","injected'


# ── Canonical result shape ────────────────────────────────────────────────

def test_scoring_result_always_has_predictable_roi_keys() -> None:
    import scoring

    result = scoring._reference_scoring_result_keys_probe()
    for key in ("roi_descriptive_statistics", "roi_descriptive_methodology",
                "roi_descriptive_status"):
        assert key in result, key
    assert isinstance(result["roi_descriptive_statistics"], list)


def test_roi_failure_preserves_existing_reference_metrics() -> None:
    """A broken ROI layer must not take the reference metrics with it."""
    import scoring

    result = {"summary": {"mean_rmse": 1.23}, "reference_root": None}
    scoring._attach_roi_descriptives(result, "does-not-exist", "dce")
    assert result["summary"]["mean_rmse"] == 1.23
    assert result["roi_descriptive_status"] == "no_roi_configured"


def test_production_scoring_path_invokes_the_roi_layer(monkeypatch, tmp_path) -> None:
    """The wiring itself: analyze_submission_niftis must call the ROI layer.

    Without this, every other test still passes while nothing is ever
    populated in production, which is exactly how Phase 4 shipped.
    """
    import scoring

    calls = {"n": 0}
    monkeypatch.setattr(scoring, "_find_output_niftis", lambda *a, **k: [])
    monkeypatch.setattr(scoring, "_score_reference_maps",
                        lambda *a, **k: {"roi_descriptive_statistics": []})

    def spy(reference_scoring, submission_id, challenge_type):
        calls["n"] += 1

    monkeypatch.setattr(scoring, "_attach_roi_descriptives", spy)
    scoring.analyze_submission_niftis("sub-1", "dce")
    assert calls["n"] == 1, "the scoring path never invoked the ROI layer"


def _mixed_records():
    """One available and one unavailable ROI, with single-token labels.

    ReportLab emits each wrapped line as its own text operator, so a
    multi-word label is never contiguous in the PDF byte stream.
    """
    return [
        _roi_record(roi_id="tumour", roi_label="Tumour"),
        _roi_record(roi_id="necrosis", roi_label="Necrosis",
                    roi_median=None, roi_within_scan_sd=None,
                    roi_within_scan_cov=None, status="empty_roi",
                    unavailable_reason="empty_roi"),
    ]


def test_concise_pdf_does_not_drop_roi_records_from_shared_model() -> None:
    """The PDF summarizes ROI availability; the complete records stay in the model."""
    records = _mixed_records()
    model = _model(records)
    assert len(model["roi_descriptive_records"]) == 2
    assert model["roi_descriptive_summary"]["unavailable_rows"] == 1
    assert _pdf_text(records).startswith("%PDF-")


def test_html_renders_unavailable_rows_too(monkeypatch) -> None:
    html = _html_text(_mixed_records(), monkeypatch)
    assert "Tumour" in html
    assert "Necrosis" in html
    assert "Unavailable" in html


@pytest.mark.parametrize("challenge", ["asl", "dsc"])
def test_other_challenges_get_no_roi_rows(challenge: str) -> None:
    summary = _summary([])
    summary["challenge_type"] = challenge
    model = _build_report_model([summary], tag="t", blinded=True)
    assert model["roi_descriptive_rows"] == []
