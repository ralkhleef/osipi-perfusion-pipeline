"""A blinded report reveals no submitter identity, anywhere.

Regression tests for CODE_WALKTHROUGH.md §B5. The issues table rendered
``Path(issue["path"]).name``; for submission-level issues that path is the
submission directory, whose name *is* the submission id, derived from the
uploaded archive name. Both renderers had their own copy of that line, so the
HTML leaked while the PDF happened not to.

The team name here is deliberately hostile: a single unusual token that cannot
occur by accident, checked in every derived form the pipeline produces —
spaced, slugged, underscored, upper, lower, and as a path component. Assertions
are made against the *whole* output, not the visible table cell, so metadata,
comments, embedded JSON, and download filenames are all covered.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi")

# One token, no separators, so PDF byte assertions are reliable: ReportLab
# splits wrapped text across operators, and a multi-word name is never
# contiguous in the file.
SECRET = "SECRETEAMOMEGA"
TEAM = SECRET          # the team name itself, so every derived form contains it
SID = f"{SECRET}_Clinical"
EMAIL = "omega@secret-lab.example"

# Every form the application itself can derive from the name above.
DERIVED = [
    SECRET,
    SECRET.lower(),
    SECRET.title(),
    f"{SECRET.lower()}.zip",
    f"{SECRET}_Clinical",
    f"{SECRET.lower()}-clinical",
    f"{SECRET.lower()}_clinical",
    f"{SECRET}.zip",
    EMAIL,
]

BLINDED_LABEL = "Submission 1"


def _summary(*, errors=None, warnings=None, roi=None, status="available") -> dict:
    """A summary shaped exactly like _gather_summary() returns.

    The base dict comes from the production gatherer for an id with nothing on
    disk, so every key the exporters read is present and the fixture cannot
    drift as those exporters grow.
    """
    root = f"/var/data/submissions/extracted/{SID}"
    base = dict(_base_summary())
    base.update({
        "submission_id": SID,
        "team_name": TEAM,
        "contact_email": EMAIL,
        "source_folder": SID,
        "challenge_type": "dce",
        "mode": "result_only",
        "val_passed": not errors,
        "error_count": len(errors or []),
        "warning_count": len(warnings or []),
        "errors": errors if errors is not None else [
            {"severity": "error", "code": "REQUIRED_MAP_MISSING",
             "message": "Required map missing.", "path": root},
        ],
        "warnings": warnings if warnings is not None else [
            {"severity": "warning", "code": "README_MISSING",
             "message": "No README or SOP file was found.", "path": root},
            {"severity": "warning", "code": "DUPLICATE_FILENAME",
             "message": "Filename appears more than once: ktrans.nii.gz",
             "path": f"{root}/Clinical/Participant1/Site1/Repeat1/Ktrans.nii.gz, "
                     f"{root}/Clinical/Participant1/Site1/Repeat2/Ktrans.nii.gz"},
        ],
        "nifti_count": 4,
        "has_validation": True,
        "run_readiness": "result_only",
        "exec_status": "skipped_result_maps",
        "generated_files": [],
        "score": None,
        "metrics": {},
        "numeric_metrics": {},
        "nifti_analysis": {
            "reference_scoring": {
                "roi_descriptive_statistics": roi or [],
                "roi_descriptive_status": status,
            },
        },
        "analysis_fields": _analysis_fields(),
    })
    return base


_BASE_CACHE: dict | None = None


def _base_summary() -> dict:
    """The production gatherer's shape for an id with nothing on disk.

    Captured once and cached, because the client fixture monkeypatches
    ``_gather_summary`` to return this very fixture — calling it again from
    inside would recurse forever.
    """
    global _BASE_CACHE
    if _BASE_CACHE is None:
        import main
        _BASE_CACHE = dict(main._gather_summary("__fixture_absent__"))
    return _BASE_CACHE


def _analysis_fields() -> dict:
    """Built by the production helper, so the fixture cannot drift from it."""
    import main

    fields = dict(main._analysis_summary_fields({}))
    fields.update({"parameter_maps_detected": "Ktrans", "map_count": 1,
                   "finite_voxels_percent": 100.0})
    return fields


def _assert_clean(text: str, what: str) -> None:
    """No derived form of the identity appears anywhere in ``text``."""
    haystack = text.lower()
    for form in DERIVED:
        assert form.lower() not in haystack, f"{what} leaked {form!r}"
    # Normalised comparison catches separators the list above may not predict.
    squashed = "".join(ch for ch in haystack if ch.isalnum())
    assert SECRET.lower() not in squashed, f"{what} leaked a normalised form"


@pytest.fixture()
def client(monkeypatch):
    import main

    # Capture the real shape BEFORE patching. _summary() calls _base_summary(),
    # which calls _gather_summary() on its first use only — so if the patch
    # lands first, that call reaches the patch and recurses. It survived only
    # because some earlier test in the file happened to warm the cache. Running
    # a fixture test on its own hit the recursion.
    _base_summary()

    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: [SID])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: _summary())
    from fastapi.testclient import TestClient
    return main, TestClient(main.app)


# ── The shared selection logic ────────────────────────────────────────────

def test_affected_display_returns_a_safe_relative_path() -> None:
    from services.pdf_report_service import affected_display

    summary = _summary()
    value = affected_display(
        f"/var/data/submissions/extracted/{SID}/Clinical/Participant1/Site1/Repeat1/Ktrans.nii.gz",
        summary, BLINDED_LABEL, blinded=True)
    assert value == "Clinical/Participant1/Site1/Repeat1/Ktrans.nii.gz"
    _assert_clean(value, "affected_display")


def test_a_submission_root_path_becomes_the_blinded_label() -> None:
    from services.pdf_report_service import affected_display

    value = affected_display(f"/var/data/submissions/extracted/{SID}",
                             _summary(), BLINDED_LABEL, blinded=True)
    assert value == BLINDED_LABEL


def test_an_unrecognised_path_degrades_to_the_label(monkeypatch) -> None:
    """A path outside the submission root must not fall back to its basename."""
    from services.pdf_report_service import affected_display

    value = affected_display(f"/somewhere/else/{SECRET}.zip",
                             _summary(), BLINDED_LABEL, blinded=True)
    assert value == BLINDED_LABEL
    _assert_clean(value, "affected_display fallback")


def test_multiple_paths_are_all_made_safe() -> None:
    from services.pdf_report_service import affected_display

    summary = _summary()
    value = affected_display(summary["warnings"][1]["path"], summary,
                             BLINDED_LABEL, blinded=True)
    _assert_clean(value, "multi-path affected")
    assert "Repeat1/Ktrans.nii.gz" in value and "Repeat2/Ktrans.nii.gz" in value


def test_empty_path_is_not_specified() -> None:
    from services.pdf_report_service import affected_display

    assert affected_display("", _summary(), BLINDED_LABEL, blinded=True) == "Not specified"


def test_unblinded_keeps_the_informative_value() -> None:
    from services.pdf_report_service import affected_display

    value = affected_display(f"/var/data/submissions/extracted/{SID}",
                             _summary(), SID, blinded=False)
    assert SID in value


@pytest.mark.parametrize("form", DERIVED)
def test_identity_is_recognised_in_every_derived_form(form: str) -> None:
    from services.pdf_report_service import identity_tokens, reveals_identity

    assert reveals_identity(form, identity_tokens(_summary()))


def test_unrelated_text_is_not_flagged() -> None:
    from services.pdf_report_service import identity_tokens, reveals_identity

    tokens = identity_tokens(_summary())
    for safe in ("Clinical/Participant1/Site1/Repeat1/Ktrans.nii.gz",
                 "Submission 1", "Ktrans", "methods.txt"):
        assert not reveals_identity(safe, tokens), safe


# ── HTML ──────────────────────────────────────────────────────────────────

def test_blinded_html_is_free_of_identity(client) -> None:
    _, api = client
    response = api.get("/api/report", params={"submission_id": SID, "blinded": "true"})
    assert response.status_code == 200
    _assert_clean(response.text, "blinded HTML body")


def test_blinded_html_still_identifies_the_submission(client) -> None:
    _, api = client
    body = api.get("/api/report", params={"submission_id": SID, "blinded": "true"}).text
    assert BLINDED_LABEL in body


def test_blinded_html_headers_are_clean(client) -> None:
    _, api = client
    response = api.get("/api/report", params={"submission_id": SID, "blinded": "true"})
    _assert_clean(str(dict(response.headers)), "blinded HTML headers")


def test_blinded_html_download_filename_is_neutral(client) -> None:
    _, api = client
    response = api.get("/api/report", params={"submission_id": SID, "blinded": "true"})
    assert "osipi_report_blinded.html" in response.headers["content-disposition"]


def test_unblinded_html_retains_the_team(client) -> None:
    _, api = client
    body = api.get("/api/report", params={"submission_id": SID, "blinded": "false"}).text
    assert SID in body
    assert TEAM in body
    assert EMAIL in body


def test_unblinded_html_filename_still_names_the_submission(client) -> None:
    _, api = client
    response = api.get("/api/report", params={"submission_id": SID, "blinded": "false"})
    assert SID in response.headers["content-disposition"]


# ── PDF ───────────────────────────────────────────────────────────────────

def _pdf_text(payload: bytes) -> str:
    return payload.decode("latin-1", errors="ignore")


def test_blinded_pdf_is_free_of_identity(client) -> None:
    _, api = client
    response = api.get("/api/export/report/pdf",
                       params={"submission_id": SID, "blinded": "true"})
    assert response.status_code == 200 and response.content[:4] == b"%PDF"
    _assert_clean(_pdf_text(response.content), "blinded PDF")


def test_blinded_pdf_filename_is_neutral(client) -> None:
    _, api = client
    response = api.get("/api/export/report/pdf",
                       params={"submission_id": SID, "blinded": "true"})
    assert "osipi_report_blinded.pdf" in response.headers["content-disposition"]


def test_blinded_pdf_document_properties_are_clean(client) -> None:
    """Title/subject/author metadata, not just page text."""
    from services.pdf_report_service import generate_pdf_report

    payload = generate_pdf_report([_summary()], tag="blinded", blinded=True)
    body = _pdf_text(payload)
    marker = body.find("/Subject")
    assert marker != -1, "no document properties present to check"
    _assert_clean(body[marker:marker + 400], "PDF document properties")


def test_unblinded_pdf_retains_the_team(client) -> None:
    _, api = client
    response = api.get("/api/export/report/pdf",
                       params={"submission_id": SID, "blinded": "false"})
    assert SECRET in _pdf_text(response.content)


def test_the_plain_text_pdf_fallback_stays_blinded(monkeypatch) -> None:
    """The no-ReportLab path builds its own document and could bypass the model."""
    import services.pdf_report_service as pdf

    def boom(model):
        raise RuntimeError("forced fallback")

    monkeypatch.setattr(pdf, "_reportlab_pdf_bytes", boom)
    payload = pdf.generate_pdf_report([_summary()], tag="blinded", blinded=True)
    assert payload[:4] == b"%PDF"
    _assert_clean(_pdf_text(payload), "fallback PDF")


# ── Shared model ──────────────────────────────────────────────────────────

def test_the_blinded_model_carries_no_identity() -> None:
    """The model is what both renderers read; it must be safe before rendering."""
    from services.pdf_report_service import _build_report_model

    model = _build_report_model([_summary()], tag="blinded", blinded=True)
    _assert_clean(json.dumps(model, default=str), "blinded report model")


def test_the_unblinded_model_keeps_identity() -> None:
    from services.pdf_report_service import _build_report_model

    model = _build_report_model([_summary()], tag=SID, blinded=False)
    assert SECRET in json.dumps(model, default=str)


def test_the_submission_label_switches_on_the_blinded_flag() -> None:
    """Pins both directions of the one decision every table label reads.

    Asserting only that the team name appears *somewhere* in an unblinded
    report is too weak: the metadata table prints team and contact separately,
    so a label that stayed blinded in unblinded mode would go unnoticed.
    """
    from services.pdf_report_service import _submission_label

    summary = _summary()
    assert _submission_label(summary, 1, blinded=True) == BLINDED_LABEL
    assert _submission_label(summary, 1, blinded=False) == SID


def test_unblinded_tables_label_rows_with_the_real_submission() -> None:
    from services.pdf_report_service import _build_report_model

    model = _build_report_model([_summary()], tag=SID, blinded=False)
    assert model["submission_metadata_rows"][0][0] == SID
    # The issues table carries the same label in its second column.
    assert model["issues"], "no issues to check"
    assert model["issues"][0][1] == SID


def test_blinded_tables_label_rows_with_the_blinded_id() -> None:
    from services.pdf_report_service import _build_report_model

    model = _build_report_model([_summary()], tag="blinded", blinded=True)
    assert model["submission_metadata_rows"][0][0] == BLINDED_LABEL
    assert model["issues"][0][1] == BLINDED_LABEL


# ── CSV and JSON exports ──────────────────────────────────────────────────

@pytest.mark.parametrize("params", [
    {"format": "csv", "shape": "wide"},
    {"format": "csv", "shape": "long"},
    {"format": "json", "shape": "wide"},
])
def test_blinded_exports_are_free_of_identity(client, params) -> None:
    _, api = client
    response = api.get("/api/export-combined",
                       params={"submission_id": SID, "blinded": "true", **params})
    assert response.status_code == 200
    _assert_clean(response.text, f"blinded {params}")
    _assert_clean(str(dict(response.headers)), f"blinded {params} headers")


def test_blinded_json_nulls_identity_rather_than_hiding_it(client) -> None:
    """Metadata must be absent, not merely undisplayed."""
    _, api = client
    payload = api.get("/api/export-combined",
                      params={"submission_id": SID, "blinded": "true",
                              "format": "json"}).json()
    item = payload["submissions"][0]
    assert item["submission_id"] is None
    assert item["team_name"] is None
    assert item["contact_email"] is None
    assert item["original_submission_name"] is None
    assert item["blinded_submission_id"]


def test_blinded_csv_has_no_identity_columns(client) -> None:
    _, api = client
    body = api.get("/api/export-combined",
                   params={"submission_id": SID, "blinded": "true",
                           "format": "csv"}).text
    header = next(csv.reader(io.StringIO(body)))
    for column in ("team_name", "contact_email", "submission_id",
                   "original_submission_name"):
        assert column not in header
    assert "blinded_submission_id" in header


def test_unblinded_exports_retain_identity(client) -> None:
    _, api = client
    body = api.get("/api/export-combined",
                   params={"submission_id": SID, "blinded": "false",
                           "format": "csv"}).text
    assert SECRET in body


def test_roi_export_filename_can_be_blinded(client) -> None:
    _, api = client
    blinded = api.get("/api/export-roi-descriptive",
                      params={"submission_id": SID, "blinded": "true"})
    assert blinded.status_code == 200
    _assert_clean(str(dict(blinded.headers)), "blinded ROI CSV headers")
    _assert_clean(blinded.text, "blinded ROI CSV body")

    default = api.get("/api/export-roi-descriptive", params={"submission_id": SID})
    assert SID in default.headers["content-disposition"]


@pytest.mark.parametrize("endpoint,params,expected", [
    ("/api/export-combined", {"format": "csv"}, "osipi_combined_blinded.csv"),
    ("/api/export-combined", {"format": "json"}, "osipi_combined_blinded.json"),
    ("/api/export-combined", {"format": "csv", "shape": "long"},
     "osipi_results_long_blinded.csv"),
])
def test_blinded_downloads_do_not_say_blinded_twice(client, endpoint, params,
                                                    expected) -> None:
    """The neutral tag and the blinded suffix are the same word.

    Blinding the filename replaced the submission id with "blinded", but the
    suffix already said "blinded", so exports downloaded as
    ``osipi_combined_blinded_blinded.csv``. Cosmetic, but it is the filename
    a reviewer sees.
    """
    _, api = client
    response = api.get(endpoint,
                       params={"submission_id": SID, "blinded": "true", **params})
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert "blinded_blinded" not in disposition, disposition
    assert expected in disposition, disposition


def test_unblinded_downloads_still_name_the_submission(client) -> None:
    """Collapsing the duplicate must not collapse the useful half."""
    _, api = client
    response = api.get("/api/export-combined",
                       params={"submission_id": SID, "blinded": "false",
                               "format": "csv"})
    disposition = response.headers["content-disposition"]
    assert SID in disposition and "unblinded" in disposition, disposition


# ── Degraded and error paths ──────────────────────────────────────────────

@pytest.mark.parametrize("summary_kwargs", [
    {"errors": [], "warnings": []},                                  # clean run
    {"roi": [], "status": "calculation_error"},                      # ROI failed
    {"roi": [], "status": "no_roi_configured"},                      # no masks
    {"errors": [{"severity": "error", "code": "X", "message": "Broken."}]},  # no path
    {"warnings": ["a bare string warning"]},                         # non-dict issue
])
def test_degraded_paths_stay_blinded(monkeypatch, summary_kwargs) -> None:
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: [SID])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: _summary(**summary_kwargs))
    api = TestClient(main.app)

    html = api.get("/api/report", params={"submission_id": SID, "blinded": "true"})
    assert html.status_code == 200
    _assert_clean(html.text, f"blinded HTML {summary_kwargs}")

    pdf = api.get("/api/export/report/pdf",
                  params={"submission_id": SID, "blinded": "true"})
    assert pdf.status_code == 200
    _assert_clean(_pdf_text(pdf.content), f"blinded PDF {summary_kwargs}")


def test_a_batch_report_does_not_print_the_batch_id(monkeypatch) -> None:
    import main
    from fastapi.testclient import TestClient

    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: [SID, SID])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: _summary())
    api = TestClient(main.app)

    response = api.get("/api/report",
                       params={"batch_id": f"{SECRET}_batch", "blinded": "true"})
    assert response.status_code == 200
    _assert_clean(response.text, "blinded batch HTML")
