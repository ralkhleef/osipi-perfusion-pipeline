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


def _nifti_bytes(values, shape: tuple[int, ...] = (2, 2, 1)) -> bytes:
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
    raw = bytes(header) + b"\x00\x00\x00\x00" + struct.pack(f"<{len(values)}f", *[float(v) for v in values])
    return gzip.compress(raw)


@pytest.fixture()
def scoring_workspace(tmp_path: Path, monkeypatch):
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


def _write_submitted(workspace: Path, values, name: str = "sub-001_cbf.nii.gz", shape: tuple[int, ...] = (2, 2, 1)) -> Path:
    path = workspace / "extracted" / "sub-001" / "results" / "maps" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_nifti_bytes(values, shape))
    return path


def _write_reference(workspace: Path, values, name: str = "sub-001_cbf.nii.gz", shape: tuple[int, ...] = (2, 2, 1)) -> Path:
    path = workspace / "reference" / "maps" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_nifti_bytes(values, shape))
    return path


def _write_mask(workspace: Path, values, name: str = "brain_mask.nii.gz", shape: tuple[int, ...] = (2, 2, 1)) -> Path:
    path = workspace / "reference" / "masks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_nifti_bytes(values, shape))
    return path


def _reference_result() -> dict:
    return scoring.analyze_submission_niftis("sub-001", "asl")["reference_scoring"]


def _cbf_row(result: dict) -> dict:
    for row in result["maps"]:
        if row["detected_map_type"] == "CBF":
            return row
    raise AssertionError(f"No CBF row found: {result}")


def test_perfect_submitted_map_gives_zero_rmse_and_bias(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [1, 2, 3, 4])
    _write_reference(scoring_workspace, [1, 2, 3, 4])

    row = _cbf_row(_reference_result())

    assert row["status"] == "compared"
    assert row["whole_map"]["rmse"] == pytest.approx(0.0)
    assert row["whole_map"]["bias"] == pytest.approx(0.0)


def test_constant_offset_gives_expected_bias_and_rmse(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [3, 4, 5, 6])
    _write_reference(scoring_workspace, [1, 2, 3, 4])

    metrics = _cbf_row(_reference_result())["whole_map"]

    assert metrics["bias"] == pytest.approx(2.0)
    assert metrics["mae"] == pytest.approx(2.0)
    assert metrics["rmse"] == pytest.approx(2.0)
    assert metrics["standard_deviation_error"] == pytest.approx(0.0)


def test_missing_reference_returns_reference_not_available_and_keeps_qc(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [1, 2, 3, 4])

    analysis = scoring.analyze_submission_niftis("sub-001", "asl")
    result = analysis["reference_scoring"]

    assert analysis["summary"]["finite_percent"] == pytest.approx(100.0)
    assert result["available"] is False
    assert result["status"] == "reference_not_available"
    assert _cbf_row(result)["status"] == "reference_not_available"


def test_mismatched_shape_returns_scoring_error(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [1, 2, 3, 4])
    _write_reference(scoring_workspace, [1, 2, 3, 4, 5, 6, 7, 8], shape=(2, 2, 2))

    result = _reference_result()
    row = _cbf_row(result)

    assert result["status"] == "scoring_error"
    assert row["status"] == "shape_mismatch"
    assert "Resampling is not performed yet" in row["error"]


def test_mask_based_scoring_uses_only_mask_voxels(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    _write_mask(scoring_workspace, [1, 1, 0, 0])

    row = _cbf_row(_reference_result())
    mask = next(item for item in row["masks"] if item["mask_name"] == "brain_mask.nii.gz")

    assert row["whole_map"]["bias"] != pytest.approx(3.0)
    assert mask["metrics"]["voxel_count"] == 2
    assert mask["metrics"]["bias"] == pytest.approx(3.0)
    assert mask["metrics"]["rmse"] == pytest.approx(math.sqrt(10.0))


def test_reference_artifacts_include_json_csv_and_difference_map(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [3, 4, 5, 6])
    _write_reference(scoring_workspace, [1, 2, 3, 4])
    artifact_dir = scoring_workspace / "artifacts"

    analysis = scoring.analyze_submission_niftis("sub-001", "asl", artifact_dir=artifact_dir)
    artifacts = sorted(str(path.relative_to(artifact_dir)) for path in artifact_dir.rglob("*") if path.is_file())

    assert analysis["reference_scoring"]["available"] is True
    assert "reference_scoring.json" in artifacts
    assert "reference_scoring.csv" in artifacts
    assert any(name.endswith("_difference.nii") for name in artifacts)
