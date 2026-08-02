"""Phase 4C: ROI statistics through the real production path, on real files.

Everything here uses genuine NIfTI files written to disk and the actual
scoring entry point. Masks are discovered by the production
``_reference_masks()`` search — never handed to the calculator directly —
so this proves the discovery and association path, not just the arithmetic.

Expected values are hand-calculated from a four-voxel ROI:

    selected values : 0.1, 0.2, 0.3, 0.4
    mean            : 0.25
    median          : 0.25
    population SD   : sqrt(((0.15)^2+(0.05)^2+(0.05)^2+(0.15)^2)/4)
                    = sqrt(0.0125) = 0.1118033988...
    CoV             : SD / 0.25 = 0.4472135955...
"""

from __future__ import annotations

import gzip
import json
import math
import struct
from pathlib import Path

import pytest

# Hand-calculated expectations for the four selected voxels.
EXPECTED_MEDIAN = 0.25
EXPECTED_SD = math.sqrt(0.0125)
EXPECTED_COV = EXPECTED_SD / 0.25
EXPECTED_VOXELS = 4


# ── NIfTI writing ─────────────────────────────────────────────────────────

def _nifti(values: list[float], shape: tuple[int, ...]) -> bytes:
    """A valid little-endian NIfTI-1 float32 volume."""
    header = bytearray(352)
    struct.pack_into("<i", header, 0, 348)                 # sizeof_hdr
    struct.pack_into("<h", header, 40, len(shape))         # dim[0]
    for index, size in enumerate(shape):
        struct.pack_into("<h", header, 42 + index * 2, size)
    struct.pack_into("<h", header, 70, 16)                 # datatype float32
    struct.pack_into("<h", header, 72, 32)                 # bitpix
    struct.pack_into("<f", header, 108, 352.0)             # vox_offset
    for index in range(1, len(shape) + 1):                 # pixdim
        struct.pack_into("<f", header, 76 + index * 4, 1.0)
    struct.pack_into("<f", header, 112, 1.0)               # scl_slope
    header[344:348] = b"n+1\x00"
    body = b"".join(struct.pack("<f", float(v)) for v in values)
    return bytes(header) + body


def _write(path: Path, values: list[float], shape: tuple[int, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _nifti(values, shape)
    path.write_bytes(gzip.compress(payload) if path.name.endswith(".gz") else payload)


# 2x2x2 Ktrans volume; the mask selects the first four voxels.
KTRANS_VALUES = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
MASK_VALUES = [1, 1, 1, 1, 0, 0, 0, 0]
SHAPE = (2, 2, 2)


@pytest.fixture()
def dce_submission(tmp_path, monkeypatch):
    """A structured DCE submission plus a discoverable reference ROI mask.

    The dataset configuration is overridden to 1 participant x 1 repeat x
    1 site so a small fixture satisfies completeness — production validation
    is not weakened, the *test challenge* is simply smaller.
    """
    import scoring
    from osipi_pipeline.config import rules as config_rules

    sid = "e2e_dce"
    extracted = tmp_path / "extracted"
    root = extracted / sid

    scan = root / "Synthetic" / "Participant1" / "Site1" / "Repeat1"
    _write(scan / "Ktrans.nii.gz", KTRANS_VALUES, SHAPE)
    _write(scan / "modelled_st.nii.gz", KTRANS_VALUES * 2, (2, 2, 2, 2))
    (root / "methods.txt").write_text("Our fitting method.", encoding="utf-8")

    # Reference root discovered by production: <extracted>/<sid>/reference.
    _write(root / "reference" / "maps" / "Ktrans.nii.gz", KTRANS_VALUES, SHAPE)
    _write(root / "reference" / "masks" / "tumour.nii.gz", MASK_VALUES, SHAPE)

    monkeypatch.setattr(scoring, "EXTRACTED_DIR", extracted)
    import services.path_config as path_config
    monkeypatch.setattr(path_config, "EXTRACTED_DIR", extracted, raising=False)

    # Shrink the expected dataset grid for the fixture only.
    rules = config_rules.validation_rules()
    import copy as _copy
    patched = _copy.deepcopy(rules)
    patched["challenges"]["dce"]["datasets"] = {
        "synthetic": {"participants": 1, "repeats": 1, "sites": 1},
    }
    monkeypatch.setattr(config_rules, "validation_rules", lambda: patched)

    return {"sid": sid, "root": root, "extracted": extracted}


# ── Production path ───────────────────────────────────────────────────────

def _analyse(fixture):
    import scoring

    return scoring.analyze_submission_niftis(fixture["sid"], "dce")


def _records(analysis) -> list[dict]:
    return analysis["reference_scoring"].get("roi_descriptive_statistics") or []


def test_production_scoring_populates_roi_records(dce_submission) -> None:
    analysis = _analyse(dce_submission)
    records = _records(analysis)
    assert records, "the production path produced no ROI records"
    assert analysis["reference_scoring"]["roi_descriptive_status"] == "available"


def test_hand_calculated_statistics_match(dce_submission) -> None:
    record = _records(_analyse(dce_submission))[0]
    assert record["voxel_count"] == EXPECTED_VOXELS
    assert record["roi_median"] == pytest.approx(EXPECTED_MEDIAN)
    assert record["roi_within_scan_sd"] == pytest.approx(EXPECTED_SD)
    assert record["roi_within_scan_cov"] == pytest.approx(EXPECTED_COV)


def test_scan_identity_survives_the_whole_path(dce_submission) -> None:
    record = _records(_analyse(dce_submission))[0]
    assert record["dataset"] == "synthetic"
    assert record["participant"] == "1"
    assert record["repeat"] == "1"
    assert record["site"] == "1"
    assert record["map_type"] == "ktrans"


def test_units_come_from_configuration(dce_submission) -> None:
    assert _records(_analyse(dce_submission))[0]["units"] == "min^-1"


def test_mask_came_from_production_discovery(dce_submission) -> None:
    """The ROI label must originate from the discovered mask file."""
    import scoring

    analysis = _analyse(dce_submission)
    reference_root = analysis["reference_scoring"]["reference_root"]
    masks = scoring._reference_masks(Path(reference_root))
    assert masks, "production discovery found no masks"
    discovered = {m["name"] for m in masks}
    assert any("tumour" in name.lower() for name in discovered)
    assert _records(analysis)[0]["roi_id"] == "tumour"


def test_records_are_json_serializable_with_raw_numbers(dce_submission) -> None:
    records = _records(_analyse(dce_submission))
    assert json.loads(json.dumps(records)) == records
    assert isinstance(records[0]["roi_within_scan_cov"], float)
    assert records[0]["roi_within_scan_cov"] < 1.0    # a ratio, not a percentage


def test_methodology_travels_with_the_result(dce_submission) -> None:
    methodology = _analyse(dce_submission)["reference_scoring"][
        "roi_descriptive_methodology"]
    assert "ddof=0" in methodology["standard_deviation"]


# ── Outputs read the stored records ───────────────────────────────────────

def _summary_for(fixture, analysis):
    return {
        "submission_id": fixture["sid"], "source_folder": "team",
        "challenge_type": "dce", "warning_count": 0, "error_count": 0,
        "warnings": [], "errors": [], "exec_status": "skipped_result_maps",
        "nifti_analysis": analysis,
        "analysis_fields": {
            "parameter_maps_detected": "Ktrans", "map_count": 1,
            "finite_voxels_percent": 100.0, "nan_count": 0, "inf_count": 0,
            "negative_voxels_percent": 0.0, "finite_voxel_count": 8,
            "total_voxel_count": 8, "negative_voxel_count": 0,
            "means_by_map_type": {}, "mean_coefficient_of_variation": None,
            "reference_based_scoring_available": False,
            "reference_compared_map_count": 0,
            "reference_scoring_status": "reference_not_available",
            "reference_mean_rmse": None, "reference_mean_mae": None,
            "reference_mean_bias": None, "reference_metric_rows": [],
        },
    }


def test_report_model_and_both_formats_carry_the_same_rows(dce_submission) -> None:
    from services.pdf_report_service import _build_report_model, generate_pdf_report

    analysis = _analyse(dce_submission)
    summary = _summary_for(dce_submission, analysis)
    model = _build_report_model([summary], tag="t", blinded=True)

    assert len(model["roi_descriptive_rows"]) == len(_records(analysis))
    row = model["roi_descriptive_rows"][0]
    # The label comes from the discovered mask filename, so assert against
    # the canonical value rather than a guessed capitalisation.
    roi_label = _records(analysis)[0]["roi_label"]
    assert row[4] == roi_label
    assert "44.72%" in row      # CoV displayed as a percentage

    # PDF: single-token label only. ReportLab emits each wrapped line as its
    # own text operator, so multi-word phrases are never contiguous bytes.
    pdf = generate_pdf_report([summary], tag="t", blinded=True)
    assert pdf.startswith(b"%PDF")
    assert roi_label.split()[0] in pdf.decode("latin-1", errors="ignore")


def test_csv_export_reads_the_stored_records(dce_submission, monkeypatch) -> None:
    import csv as _csv
    import io as _io

    import main

    analysis = _analyse(dce_submission)
    summary = _summary_for(dce_submission, analysis)
    monkeypatch.setattr(main, "_collect_export_ids", lambda b, s: [dce_submission["sid"]])
    monkeypatch.setattr(main, "_gather_summary", lambda sid: summary)

    body = main.export_roi_descriptive(
        submission_id=dce_submission["sid"], batch_id=None).body.decode("utf-8")
    rows = list(_csv.reader(_io.StringIO(body)))
    assert rows[0] == list(main.ROI_CSV_COLUMNS)
    data = rows[1]
    cov = float(data[main.ROI_CSV_COLUMNS.index("roi_within_scan_cov")])
    assert cov == pytest.approx(EXPECTED_COV)
    assert "%" not in data[main.ROI_CSV_COLUMNS.index("roi_within_scan_cov")]


# ── Failure isolation on the real path ────────────────────────────────────

def test_calculator_failure_preserves_scoring(dce_submission, monkeypatch) -> None:
    """An unexpected ROI error must not take the scoring result with it."""
    import services.roi_descriptive_service as svc

    def boom(*args, **kwargs):
        raise RuntimeError("synthetic ROI failure")

    monkeypatch.setattr(svc, "compute_roi_descriptive_statistics", boom)

    analysis = _analyse(dce_submission)
    reference = analysis["reference_scoring"]
    assert reference["roi_descriptive_status"] == "calculation_error"
    assert reference["roi_descriptive_statistics"] == []
    # The rest of the scoring result survived and stays serializable.
    assert "summary" in reference
    assert json.loads(json.dumps(analysis, default=str))


def test_no_masks_yields_a_clear_status(dce_submission, monkeypatch) -> None:
    import scoring

    monkeypatch.setattr(scoring, "_reference_masks", lambda root: [])
    reference = _analyse(dce_submission)["reference_scoring"]
    assert reference["roi_descriptive_status"] == "no_roi_configured"
    assert reference["roi_descriptive_statistics"] == []


# ── Duplicate computation on the real path ────────────────────────────────

def test_calculation_happens_once_per_scoring_run(dce_submission, monkeypatch) -> None:
    """Patches the real calculator used in production, not a wrapper."""
    import services.roi_descriptive_service as svc
    from services.pdf_report_service import _build_report_model, generate_pdf_report

    calls = {"n": 0}
    real = svc.compute_roi_descriptive_statistics

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(svc, "compute_roi_descriptive_statistics", counting)

    analysis = _analyse(dce_submission)
    assert calls["n"] == 1, "scoring should compute exactly once"

    summary = _summary_for(dce_submission, analysis)
    model = _build_report_model([summary], tag="t", blinded=True)
    generate_pdf_report([summary], tag="t", blinded=True)
    json.dumps(model["roi_descriptive_records"])
    assert calls["n"] == 1, "an output format recomputed the statistics"
