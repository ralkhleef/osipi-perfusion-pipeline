"""Every row says which scan it came from.

The DCE-2026 layout gives every scan the same filenames by design:
``Ktrans.nii.gz`` and ``Ct.nii.gz`` appear once in each of sixty scan
directories. A results table keyed on the filename therefore prints
"Ktrans.nii.gz" sixty times and tells a reader nothing at all -- and the
reference-comparison table is the worst case, because it has one row per map
per region per scan, so a single participant already fills it with rows that
look identical.

The identity is not missing: ingestion resolves dataset, participant, repeat
and site for every file. It was simply not carried through to the results. These
tests hold it there, in the row data, in the CSV export and in the report.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
for extra in ("src", "backend"):
    path = str(ROOT / extra)
    if path not in sys.path:
        sys.path.insert(0, path)

import scoring as backend_scoring  # noqa: E402


# ── The label itself ───────────────────────────────────────────────────────

def test_a_full_identity_reads_as_one_short_phrase() -> None:
    label = backend_scoring._scan_label("Synthetic", "1", "2", "3")
    assert label == "P1 · Site 3 · Repeat 2"


def test_the_order_is_participant_site_repeat() -> None:
    """Widest grouping first, so a sorted column groups the way a reader reads."""
    label = backend_scoring._scan_label(None, "07", "1", "2")
    assert label.index("P07") < label.index("Site 2") < label.index("Repeat 1")


def test_a_partial_identity_omits_what_it_does_not_know() -> None:
    """"P1 - Site 2" is honest about a missing repeat in a way that
    "P1 - Site 2 - Repeat ?" is not."""
    label = backend_scoring._scan_label("Clinical", "1", None, "2")
    assert "Repeat" not in label
    assert "P1" in label and "Site 2" in label


def test_nothing_identifying_produces_no_label_rather_than_a_blank_one() -> None:
    """A row that cannot be placed must say so, not print an empty cell that
    looks like a rendering fault."""
    assert backend_scoring._scan_label(None, None, None, None) is None
    assert backend_scoring._scan_label("", "", "", "") is None


def test_a_non_numeric_participant_is_not_given_a_p_prefix() -> None:
    assert backend_scoring._scan_label(None, "control", None, None) == "control"


# ── It reaches the rows ────────────────────────────────────────────────────

@pytest.fixture()
def compared(tmp_path, monkeypatch):
    """Two scans of one participant, same filenames, different directories."""
    nib = pytest.importorskip("nibabel")
    import numpy as np

    affine = np.diag([2.0, 2.0, 4.0, 1.0])
    truth = np.linspace(0.02, 0.2, 4 * 4 * 2, dtype=np.float32).reshape(4, 4, 2)

    def save(path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        nib.save(nib.Nifti1Image(np.asarray(data, np.float32), affine), str(path))

    submission = tmp_path / "extracted" / "team"
    reference = tmp_path / "reference_data"
    # The real DCE-2026 layout: identity in the directories, one filename
    # reused in every scan folder.
    for site, repeat, offset in (("1", "1", 0.0), ("2", "1", 0.01)):
        save(submission / "P01" / f"site_{site}" / f"scan_{repeat}" / "Ktrans.nii.gz",
             truth + offset)
    save(reference / "dce" / "maps" / "Ktrans.nii.gz", truth)

    import services.path_config as pc
    for name, value in (("EXTRACTED_DIR", tmp_path / "extracted"),
                        ("REFERENCE_DATA_DIR", reference)):
        monkeypatch.setattr(pc, name, value, raising=False)
        monkeypatch.setattr(backend_scoring, name, value, raising=False)

    maps = [
        {"detected_map_type": "ktrans", "file_name": "Ktrans.nii.gz",
         "path": str(path), "parameter_label": "Ktrans", "units": "1/min"}
        for path in sorted(submission.rglob("Ktrans.nii.gz"))
    ]
    return backend_scoring._score_reference_maps("team", "dce", maps), tmp_path


def test_two_scans_with_one_filename_are_told_apart(compared) -> None:
    """The case the whole change exists for."""
    result, _ = compared
    rows = result.get("maps") or []
    assert len(rows) == 2

    filenames = {row["submitted_file"] for row in rows}
    assert filenames == {"Ktrans.nii.gz"}, "the fixture no longer reuses one filename"

    labels = sorted(row["scan_label"] for row in rows)
    assert labels == ["P1 · Site 1 · Repeat 1", "P1 · Site 2 · Repeat 1"]


def test_the_parts_are_kept_separately_as_well_as_joined(compared) -> None:
    """A label is for reading; the fields are for sorting and filtering, and a
    spreadsheet needs the second kind."""
    result, _ = compared
    for row in result["maps"]:
        assert row["participant"] == "1"
        assert row["repeat"] == "1"
        assert row["site"] in {"1", "2"}


def test_the_csv_export_leads_with_the_scan(compared, tmp_path) -> None:
    """Opened in a spreadsheet, the first columns are what you sort on."""
    import csv

    result, _ = compared
    out = tmp_path / "artifacts"
    backend_scoring._write_reference_scoring_artifacts(out, result)
    with (out / "reference_scoring.csv").open(encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        assert reader.fieldnames[:5] == [
            "scan_label", "dataset", "participant", "site", "repeat"]
        rows = list(reader)

    assert rows, "the export wrote no rows"
    assert all(row["scan_label"] for row in rows)
    # Every row, including the per-mask ones, carries its scan.
    assert len({row["scan_label"] for row in rows}) == 2


def test_the_report_table_has_a_scan_column() -> None:
    """The 120-row table a reviewer actually reads."""
    source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert "<th>Submission</th><th>Scan</th><th>Map</th><th>ROI</th>" in source
    assert "reference_row.get('scan_label')" in source


def test_an_unidentified_row_says_so_rather_than_showing_an_empty_cell() -> None:
    source = (ROOT / "backend" / "main.py").read_text(encoding="utf-8")
    assert "reference_row.get('scan_label') or 'not identified'" in source


# ── The report tables a reviewer actually reads ────────────────────────────

def _model_for(maps_rows, *, challenge="dce"):
    """A report model built from one submission's analysis."""
    import sys as _sys
    _sys.path.insert(0, str(ROOT / "backend"))
    from services.pdf_report_service import _build_report_model

    summary = {
        "submission_id": "team",
        "challenge_type": challenge,
        "error_count": 0, "warning_count": 0, "errors": [], "warnings": [],
        "nifti_analysis": {
            "summary": {},
            "maps": maps_rows,
            "reference_scoring": {"status": "available", "maps": maps_rows},
        },
        "analysis_fields": {},
    }
    return _build_report_model([summary], tag="t", blinded=True)


def _two_scans():
    """Two scans, one filename, one map type: the shape that broke."""
    def scan(site, label):
        whole = {"status": "compared", "bias": 0.0, "mae": 0.0, "rmse": 0.0,
                 "error_coefficient_of_variation": 0.1, "correlation": 0.99,
                 "voxel_count": 100, "total_voxel_count": 100}
        return {
            "submitted_file": "Ktrans.nii.gz",
            "detected_map_type": "ktrans",
            "scan_label": label, "participant": "1", "site": site, "repeat": "1",
            "status": "compared",
            "stats": {"finite_percent": 100.0, "negative_voxel_percent": 0.0, "mean": 1.0},
            "units": "min^-1",
            "whole_map": whole,
            "masks": [{"mask_label": "gray matter", "status": "compared",
                       "metrics": dict(whole)}],
        }
    return [scan("1", "P1 · Site 1 · Repeat 1"), scan("2", "P1 · Site 2 · Repeat 1")]


def test_the_map_qc_table_names_the_scan() -> None:
    """180 rows of "Ktrans" with nothing to tell them apart was the complaint."""
    model = _model_for(_two_scans())
    headers = model["main_map_metric_headers"]
    assert "Scan" in headers, headers
    column = headers.index("Scan")
    labels = [row[column] for row in model["main_map_metric_rows"]]
    assert labels == ["P1 · Site 1 · Repeat 1", "P1 · Site 2 · Repeat 1"], labels


def test_the_region_table_names_the_scan() -> None:
    model = _model_for(_two_scans())
    headers = model["reference_region_headers"]
    assert "Scan" in headers, headers
    column = headers.index("Scan")
    labels = {row[column] for row in model["reference_region_rows"]}
    assert labels == {"P1 · Site 1 · Repeat 1", "P1 · Site 2 · Repeat 1"}


def test_a_column_that_would_say_the_same_thing_on_every_row_is_not_added() -> None:
    """The point is telling rows apart, not adding columns."""
    rows = _two_scans()
    for row in rows:
        row.pop("scan_label")
    model = _model_for(rows)
    assert "Scan" not in model["main_map_metric_headers"]
    assert "Scan" not in model["reference_region_headers"]
    # One challenge, so naming it on every row would be noise too.
    assert "Challenge" not in model["main_map_metric_headers"]


def test_a_file_that_was_never_a_parameter_map_is_not_called_unknown() -> None:
    """The 4-D fitted curve. It read "Unknown" with every metric "Not
    available", which looks like a failure rather than a file that map
    detection correctly declined to name."""
    rows = _two_scans()
    rows[0]["detected_map_type"] = "Unknown"
    rows[0]["role_label"] = "Fitted signal (4-D)"
    model = _model_for(rows)
    headers = model["main_map_metric_headers"]
    column = headers.index("Map")
    names = [row[column] for row in model["main_map_metric_rows"]]
    assert "Fitted signal (4-D)" in names, names
    assert "Unknown" not in names


def test_an_unnamed_non_map_says_what_it_is_not() -> None:
    rows = _two_scans()
    rows[0]["detected_map_type"] = "Unknown"
    model = _model_for(rows)
    column = model["main_map_metric_headers"].index("Map")
    names = [row[column] for row in model["main_map_metric_rows"]]
    assert "Not a parameter map" in names
    assert "Unknown" not in names
