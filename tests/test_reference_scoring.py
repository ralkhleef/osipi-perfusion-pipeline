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


def test_repeated_basenames_have_distinct_correct_difference_volumes(scoring_workspace):
    import nibabel as nib
    import numpy as np
    root=scoring_workspace
    for i in (1, 2):
        name=f"P01/site_1/scan_{i}/cbf.nii.gz"
        _write_submitted(root, [float(i)] * 4, name=name)
        _write_reference(root, [0.0] * 4, name=name)
    artifact_dir=root/"artifacts"
    analysis=scoring.analyze_submission_niftis("sub-001", "asl", artifact_dir=artifact_dir)
    rows=analysis["reference_scoring"]["maps"]
    assert len(rows)==2
    assert len({r["difference_map"] for r in rows})==2
    for row in rows:
        diff=np.asanyarray(nib.load(artifact_dir/row["difference_map"]).dataobj)
        assert np.all(diff==float(row["repeat"]))


def _map_row(result: dict, map_type: str) -> dict:
    for row in result["maps"]:
        if row["detected_map_type"] == map_type:
            return row
    raise AssertionError(f"No {map_type} row found: {result}")


def _cbf_row(result: dict) -> dict:
    return _map_row(result, "CBF")


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("sub-001_cbf.nii.gz", "CBF"),
        ("sub-001_att.nii.gz", "ATT"),
        ("sub-001_ktrans.nii.gz", "Ktrans"),
        ("sub-001_ve.nii.gz", "ve"),
        ("sub-001_vp.nii.gz", "Vp"),
        ("sub-001_kep.nii.gz", "Kep"),
    ],
)
def test_filename_detection_supports_reference_parameter_types(filename: str, expected: str) -> None:
    assert scoring._detect_map_type(Path(filename))["detected_map_type"] == expected


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


def test_reference_with_no_finite_voxels_is_scoring_error_and_qc_still_works(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [1, 2, 3, 4])
    _write_reference(scoring_workspace, [float("nan"), float("nan"), float("nan"), float("nan")])

    analysis = scoring.analyze_submission_niftis("sub-001", "asl")
    result = analysis["reference_scoring"]
    row = _cbf_row(result)

    assert analysis["summary"]["finite_percent"] == pytest.approx(100.0)
    assert result["available"] is False
    assert result["status"] == "scoring_error"
    assert row["status"] == "reference_invalid"
    assert row["whole_map"]["status"] == "reference_invalid"


def test_no_finite_overlap_is_scoring_error_not_missing(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [1, float("nan"), float("nan"), float("nan")])
    _write_reference(scoring_workspace, [float("nan"), 2, float("nan"), float("nan")])

    analysis = scoring.analyze_submission_niftis("sub-001", "asl")
    result = analysis["reference_scoring"]
    row = _cbf_row(result)

    assert analysis["summary"]["finite_percent"] == pytest.approx(25.0)
    assert result["available"] is False
    assert result["status"] == "scoring_error"
    assert row["status"] == "no_finite_overlap"
    assert row["whole_map"]["status"] == "no_finite_overlap"


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


def test_same_shape_misoriented_mask_is_not_applied(scoring_workspace: Path) -> None:
    nib = pytest.importorskip("nibabel")
    np = pytest.importorskip("numpy")
    submitted = _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    mask_path = _write_mask(scoring_workspace, [1, 1, 0, 0])
    affine = nib.load(str(submitted)).affine.copy()
    affine[0, 3] += 20.0
    nib.save(
        nib.Nifti1Image(np.asarray([1, 1, 0, 0], dtype=np.float32).reshape(2, 2, 1), affine),
        str(mask_path),
    )

    row = _cbf_row(_reference_result())
    mask = next(item for item in row["masks"] if item["mask_name"] == "brain_mask.nii.gz")

    assert mask["status"] == "spatial_grid_mismatch"
    assert mask["metrics"] is None


def test_configured_known_mask_alias_is_used(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    _write_mask(scoring_workspace, [1, 1, 0, 0], name="brain_mask.nii.gz")

    row = _cbf_row(_reference_result())
    mask = next(item for item in row["masks"] if item["mask_name"] == "brain_mask.nii.gz")

    assert mask["mask_label"] == "brain mask"
    assert mask["status"] == "compared"


def test_arbitrary_custom_roi_mask_is_accepted(scoring_workspace: Path) -> None:
    """A mask the label rules do not recognise still gets scored.

    Named to match no rule but the generic `roi` one, so this stays a test of
    "any mask an organiser supplies is accepted" rather than a test of a
    particular label.
    """
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    _write_mask(scoring_workspace, [1, 0, 0, 1], name="custom_roi.nii.gz")

    row = _cbf_row(_reference_result())
    mask = next(item for item in row["masks"] if item["mask_name"] == "custom_roi.nii.gz")

    assert mask["mask_label"] == "ROI"
    assert mask["metrics"]["voxel_count"] == 2


def test_a_lesion_mask_is_labelled_lesion_not_roi(scoring_workspace: Path) -> None:
    """`lesion_roi_mask.nii.gz` used to report as "ROI".

    The rules are ordered and the first match wins, so the generic `roi`
    pattern claimed it before anything more specific could. A reviewer with
    several ROIs then could not tell which row was the lesion.
    """
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    _write_mask(scoring_workspace, [1, 0, 0, 0], name="lesion_roi_mask.nii.gz")

    row = _cbf_row(_reference_result())
    mask = next(item for item in row["masks"] if item["mask_name"] == "lesion_roi_mask.nii.gz")

    assert mask["mask_label"] == "lesion"


def test_multiple_masks_are_all_reported(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    _write_mask(scoring_workspace, [1, 1, 0, 0], name="brain_mask.nii.gz")
    _write_mask(scoring_workspace, [0, 0, 1, 1], name="custom_region.nii.gz")

    row = _cbf_row(_reference_result())
    names = {item["mask_name"] for item in row["masks"]}

    assert {"brain_mask.nii.gz", "custom_region.nii.gz"}.issubset(names)


def test_unknown_mask_filename_gets_clean_label(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    _write_mask(scoring_workspace, [1, 1, 0, 0], name="left_hippocampus.nii.gz")

    row = _cbf_row(_reference_result())
    mask = next(item for item in row["masks"] if item["mask_name"] == "left_hippocampus.nii.gz")

    assert mask["mask_label"] == "left hippocampus"


def test_duplicate_mask_display_labels_do_not_drop_masks(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [12, 14, 100, 100])
    _write_reference(scoring_workspace, [10, 10, 10, 10])
    # Both must still resolve to the *same* display label, or this stops
    # testing anything: "tumor_roi" now labels as lesion, which would have
    # left one ROI row and a passing test that had lost its own premise.
    _write_mask(scoring_workspace, [1, 0, 0, 0], name="left_roi.nii.gz")
    _write_mask(scoring_workspace, [0, 1, 0, 0], name="right_roi.nii.gz")

    row = _cbf_row(_reference_result())
    roi_masks = [item for item in row["masks"] if item["mask_label"] == "ROI"]

    assert len(roi_masks) == 2, "two masks sharing a label were collapsed into one"
    assert {item["mask_name"] for item in roi_masks} == {"left_roi.nii.gz", "right_roi.nii.gz"}


def test_negative_voxel_percent_uses_scored_finite_overlap_denominator(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [-1, -2, -3, -4])
    _write_reference(scoring_workspace, [0, float("nan"), float("nan"), float("nan")])

    metrics = _cbf_row(_reference_result())["whole_map"]

    assert metrics["voxel_count"] == 1
    assert 0.0 <= metrics["negative_voxel_percent"] <= 100.0
    assert metrics["negative_voxel_percent"] == pytest.approx(100.0)


def test_partial_reference_scoring_when_some_maps_are_missing(scoring_workspace: Path) -> None:
    _write_submitted(scoring_workspace, [3, 4, 5, 6], name="sub-001_cbf.nii.gz")
    _write_submitted(scoring_workspace, [1, 1, 1, 1], name="sub-001_att.nii.gz")
    _write_reference(scoring_workspace, [1, 2, 3, 4], name="sub-001_cbf.nii.gz")

    result = _reference_result()

    assert result["available"] is True
    assert result["status"] == "partial_reference_scoring"
    assert result["summary"]["compared_map_count"] == 1
    assert _map_row(result, "CBF")["status"] == "compared"
    assert _map_row(result, "ATT")["status"] == "reference_not_available"


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


# ── Matching a scan to its own ground truth ───────────────────────────────
#
# The DCE challenge lays a submission out as P05/site_2/scan_1/Ktrans.nii.gz
# with the ground truth in the same shape, so every one of the sixty candidate
# files is named exactly "Ktrans.nii.gz". Matching on the filename alone left
# all sixty tied and broke the tie on path length, which is arbitrary: measured
# against the real data, 59 of 60 scans were paired with P01/site_1/scan_1.
#
# Where the grids differed that surfaced as a shape mismatch, which is at least
# visible. Where they agreed, within site_1, it produced a full set of accuracy
# metrics computed against another participant's ground truth. Nothing in the
# output would have looked wrong.

def _tree(root: Path, name: str = "Ktrans.nii.gz") -> list[Path]:
    made = []
    for participant in ("P01", "P05", "P10"):
        for site in ("site_1", "site_2"):
            for scan in ("scan_1", "scan_2"):
                path = root / participant / site / scan / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"nifti")
                made.append(path)
    return made


def test_a_scan_is_matched_to_its_own_ground_truth(tmp_path) -> None:
    submission = tmp_path / "submission"
    reference = tmp_path / "hidden_ground_truth"
    _tree(submission)
    candidates = _tree(reference)

    for submitted in sorted(submission.rglob("Ktrans.nii.gz")):
        chosen = scoring._choose_reference_match(
            submitted, candidates,
            submission_root=submission, reference_root=reference,
        )
        assert chosen == reference / submitted.relative_to(submission), (
            f"{submitted.relative_to(submission)} was matched to "
            f"{chosen.relative_to(reference)}"
        )


def test_identity_beats_the_filename_when_they_disagree(tmp_path) -> None:
    """Directory identity is the stronger signal and must win.

    A reference file whose *name* happens to share more tokens with the
    submission must not outrank the one that is actually this scan.
    """
    submission = tmp_path / "submission"
    reference = tmp_path / "gt"
    submitted = submission / "P05" / "site_2" / "scan_1" / "Ktrans.nii.gz"
    submitted.parent.mkdir(parents=True)
    submitted.write_bytes(b"x")

    right = reference / "P05" / "site_2" / "scan_1" / "Ktrans.nii.gz"
    decoy = reference / "P01" / "site_1" / "scan_1" / "Ktrans_P05_site_2.nii.gz"
    for path in (right, decoy):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")

    chosen = scoring._choose_reference_match(
        submitted, [decoy, right], submission_root=submission, reference_root=reference,
    )
    assert chosen == right


def test_the_filename_still_decides_when_there_is_no_identity(tmp_path) -> None:
    """Submissions that encode identity in the filename keep working."""
    flat = tmp_path / "flat"
    flat.mkdir()
    submitted = flat / "Synthetic_P1_Visit1_Site1_Ktrans.nii.gz"
    submitted.write_bytes(b"x")
    right = flat / "Synthetic_P1_Visit1_Site1_Ktrans.nii.gz"
    other = flat / "unrelated_map.nii.gz"
    other.write_bytes(b"x")

    chosen = scoring._choose_reference_match(submitted, [other, right])
    assert chosen == right


def test_a_single_candidate_is_used_without_question(tmp_path) -> None:
    only = tmp_path / "Ktrans.nii.gz"
    only.write_bytes(b"x")
    assert scoring._choose_reference_match(tmp_path / "sub.nii.gz", [only]) == only
    assert scoring._choose_reference_match(tmp_path / "sub.nii.gz", []) is None


def test_scan_identity_is_read_relative_to_its_root(tmp_path) -> None:
    """A root that happens to contain 'P01' must not become the identity."""
    root = tmp_path / "P01_archive"
    path = root / "P05" / "site_2" / "scan_1" / "Ktrans.nii.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"x")

    dataset, participant, repeat, site = scoring._scan_identity(path, root)
    assert (participant, site, repeat) == ("5", "2", "1")
