"""ASL-specific scoring rules added per the mentor (Lena) clarifications.

Covers, without inventing any official scoring formula or threshold:
  * real Lena filenames (Perfmap -> CBF, ATTmap -> ATT) are detected,
  * parameter maps must be exactly 3-D; a 4-D file is a fitted-model role,
  * same-shape-but-different-grid maps are refused (spatial_grid_mismatch),
  * CBF and ATT metrics are reported per map type, never averaged together,
  * repeatability/ICC are explicitly reported as unavailable from one map,
  * difference maps preserve the submitted affine.
"""
from __future__ import annotations

import gzip
import math
import struct
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import scoring  # noqa: E402


def _nifti_bytes(values, shape=(2, 2, 1), translation=None) -> bytes:
    assert len(values) == math.prod(shape)
    header = bytearray(348)
    header[0:4] = (348).to_bytes(4, "little")
    header[344:348] = b"n+1\x00"
    header[40:42] = len(shape).to_bytes(2, "little", signed=True)
    for i, size in enumerate(shape, start=1):
        header[40 + i * 2 : 42 + i * 2] = int(size).to_bytes(2, "little", signed=True)
    header[70:72] = (16).to_bytes(2, "little", signed=True)
    header[72:74] = (32).to_bytes(2, "little", signed=True)
    header[108:112] = struct.pack("<f", 352.0)
    for i in range(1, min(len(shape), 3) + 1):
        header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)
    if translation is not None:
        tx, ty, tz = translation
        header[252:254] = (0).to_bytes(2, "little", signed=True)   # qform_code
        header[254:256] = (1).to_bytes(2, "little", signed=True)   # sform_code
        header[280:296] = struct.pack("<4f", 1.0, 0.0, 0.0, float(tx))
        header[296:312] = struct.pack("<4f", 0.0, 1.0, 0.0, float(ty))
        header[312:328] = struct.pack("<4f", 0.0, 0.0, 1.0, float(tz))
    raw = bytes(header) + b"\x00\x00\x00\x00" + struct.pack(f"<{len(values)}f", *[float(v) for v in values])
    return gzip.compress(raw)


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch):
    extracted = tmp_path / "extracted"
    outputs = tmp_path / "outputs"
    reference = tmp_path / "reference"
    scoring_dir = tmp_path / "scoring"
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(scoring, "OUTPUTS_DIR", outputs)
    monkeypatch.setattr(scoring, "REFERENCE_DATA_DIR", reference)
    monkeypatch.setattr(scoring, "SCORING_DIR", scoring_dir)
    for path in (extracted, outputs, reference, scoring_dir):
        path.mkdir(parents=True, exist_ok=True)
    return tmp_path


def _submit(ws, values, name, shape=(2, 2, 1), translation=None):
    p = ws / "extracted" / "sub-001" / "results" / "maps" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_nifti_bytes(values, shape, translation))
    return p


def _ref(ws, values, name, shape=(2, 2, 1), translation=None):
    p = ws / "reference" / "maps" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(_nifti_bytes(values, shape, translation))
    return p


def _score(ws):
    return scoring.analyze_submission_niftis("sub-001", "asl")["reference_scoring"]


def _row(result, map_type):
    for row in result["maps"]:
        if row["detected_map_type"] == map_type:
            return row
    raise AssertionError(f"No {map_type} row: {[r.get('detected_map_type') for r in result['maps']]}")


# ── Real Lena filenames ────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("sub-001_acq-002_Perfmap_32float.nii.gz", "CBF"),
        ("sub-001_acq-002_ATTmap_32float.nii.gz", "ATT"),
    ],
)
def test_real_lena_filenames_detected(filename, expected):
    assert scoring._detect_map_type(Path(filename))["detected_map_type"] == expected


# ── 3-D enforcement / 4-D fitted-model role ────────────────────────────────
def test_4d_perfmap_is_not_scored_as_parameter_map(workspace):
    _submit(workspace, [1, 2, 3, 4, 5, 6, 7, 8], "sub-001_Perfmap.nii.gz", shape=(2, 2, 1, 2))
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz", shape=(2, 2, 1))
    row = _row(_score(workspace), "CBF")
    assert row["status"] == "unexpected_dimensions"
    assert row.get("file_role") == "fitted_model"
    assert row["whole_map"] is None


def test_3d_perfmap_still_scores(workspace):
    _submit(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    assert _row(_score(workspace), "CBF")["status"] == "compared"


# ── Spatial grid guard ─────────────────────────────────────────────────────
def test_same_shape_different_grid_is_refused(workspace):
    _submit(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz", translation=(0, 0, 0))
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz", translation=(100, 0, 0))
    row = _row(_score(workspace), "CBF")
    assert row["status"] == "spatial_grid_mismatch"
    assert row["whole_map"] is None


# ── CBF and ATT are never averaged together ────────────────────────────────
def test_cbf_and_att_reported_separately_not_averaged(workspace):
    _submit(workspace, [3, 4, 5, 6], "sub-001_Perfmap.nii.gz")   # CBF, bias +2 vs ref
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    _submit(workspace, [10, 10, 10, 10], "sub-001_ATTmap.nii.gz")  # ATT, bias 0 vs ref
    _ref(workspace, [10, 10, 10, 10], "sub-001_ATTmap.nii.gz")

    result = _score(workspace)
    by_type = result["summary"]["by_map_type"]
    assert set(by_type) == {"CBF", "ATT"}
    assert by_type["CBF"]["mean_bias"] == pytest.approx(2.0)
    assert by_type["ATT"]["mean_bias"] == pytest.approx(0.0)
    # No misleading combined average across different units:
    assert result["summary"]["mean_rmse"] is None
    assert result["summary"]["aggregate_map_type"] == "mixed"


def test_single_map_type_keeps_flat_aggregate(workspace):
    _submit(workspace, [3, 4, 5, 6], "sub-001_Perfmap.nii.gz")
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    result = _score(workspace)
    assert result["summary"]["aggregate_map_type"] == "CBF"
    assert result["summary"]["mean_rmse"] == pytest.approx(2.0)


# ── Repeatability honesty ──────────────────────────────────────────────────
def test_repeatability_reported_unavailable_from_single_map(workspace):
    _submit(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz")
    result = _score(workspace)
    assert result["repeatability_status"] == "unavailable_requires_repeated_datasets"
    assert "repeatability_cov" in result["metric_definitions"]
    row = _row(result, "CBF")
    assert row["whole_map"]["cov_kind"] == "error_cov"


# ── Difference map preserves affine ────────────────────────────────────────
def test_difference_map_preserves_submitted_affine(workspace, tmp_path):
    _submit(workspace, [3, 4, 5, 6], "sub-001_Perfmap.nii.gz", translation=(7, 8, 9))
    _ref(workspace, [1, 2, 3, 4], "sub-001_Perfmap.nii.gz", translation=(7, 8, 9))
    artifact_dir = tmp_path / "artifacts"
    scoring._score_reference_maps(
        "sub-001", "asl",
        [{
            "file_name": "sub-001_Perfmap.nii.gz",
            "detected_map_type": "CBF",
            "path": str(workspace / "extracted" / "sub-001" / "results" / "maps" / "sub-001_Perfmap.nii.gz"),
        }],
        artifact_dir=artifact_dir,
    )
    diffs = list(artifact_dir.rglob("*_difference.nii"))
    assert diffs, "difference map not written"
    nib = pytest.importorskip("nibabel")
    img = nib.load(str(diffs[0]))
    assert img.affine[0][3] == pytest.approx(7.0)
    assert img.affine[1][3] == pytest.approx(8.0)
    assert img.affine[2][3] == pytest.approx(9.0)
