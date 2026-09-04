"""Regression cases from the multi-scan/export code review."""
from pathlib import Path

import pytest
import main
import scoring
import json
from osipi_pipeline.ingestion.manifest import refresh_manifest, manifest_files
from services.pdf_report_service import _fmt


def test_preview_reads_only_selected_timepoint(monkeypatch):
    import numpy as np
    import nibabel as nib
    from types import SimpleNamespace
    from services.nifti_preview_service import _load_preview_volume

    class Proxy:
        def __array__(self, *args, **kwargs):
            raise AssertionError("Must not load the entire 4-D series")

        def __getitem__(self, key):
            assert key == (slice(None), slice(None), slice(None), 78)
            return np.ones((12, 14, 9), dtype=np.float32)

    monkeypatch.setattr(nib, "load", lambda _: SimpleNamespace(shape=(12, 14, 9, 157), dataobj=Proxy()))
    volume, finite, index = _load_preview_volume(Path("synthetic.nii.gz"))
    assert volume.shape == (12, 14, 9)
    assert finite.size == volume.size
    assert index == 78


def test_overlay_rejects_4d_map_without_reading_voxels(monkeypatch):
    import nibabel as nib
    from types import SimpleNamespace
    from services.nifti_preview_service import _attach_mask_overlay

    class Proxy:
        def __array__(self, *args, **kwargs):
            raise AssertionError("A 4D map must be rejected from its header")

    monkeypatch.setattr(nib, "load", lambda _: SimpleNamespace(shape=(12, 14, 9, 157), dataobj=Proxy()))
    result = _attach_mask_overlay({"is_parameter_map": True, "source_path": "synthetic.nii.gz"},
                                 "synthetic", [Path("mask.nii.gz")])
    assert "mask_overlay_error" not in result
    assert result["mask_overlays"] == []


def test_leaderboard_reports_qc_separately_from_provider(tmp_path, monkeypatch):
    from services import path_config
    monkeypatch.setattr(path_config, "SCORING_OUTPUTS_DIR", tmp_path)
    payload = {"status": "not_configured", "artifact_count": 4,
               "nifti_analysis": {"challenge_type": "asl", "errors": [],
                   "maps": [{"detected_map_type": "CBF"}, {"detected_map_type": "ATT"}],
                   "reference_scoring": {"status": "available"}}}
    (tmp_path / "demo_score.json").write_text(json.dumps(payload))
    entry = main.get_leaderboard()["entries"][0]
    assert entry["status"] == "not_configured"
    assert entry["analysis_complete"] is True
    assert entry["map_count"] == 2
    assert entry["map_types"] == ["ATT", "CBF"]
    assert entry["reference_scoring_status"] == "available"
    payload["nifti_analysis"]["maps"] = []
    (tmp_path / "demo_score.json").write_text(json.dumps(payload))
    assert main.get_leaderboard()["entries"][0]["analysis_complete"] is False


@pytest.mark.parametrize("value", [5.5e-5, -2e-5, 0.0, 1.234])
def test_small_export_numbers_agree_with_pdf(value):
    assert main._fmt_export_cell(value) == _fmt(value)
    assert main._fmt_report_cell(value) == _fmt(value)


def test_integer_precision_does_not_strip_significant_zeroes():
    assert main._fmt_export_cell(10.0, digits=0) == "10"


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_exports_are_unavailable(value):
    assert main._fmt_export_cell(value) == ""
    assert main._fmt_report_cell(value) == "Not available"


def test_empty_nested_directory_is_tracked(tmp_path):
    folder = tmp_path / "results" / "maps"
    folder.mkdir(parents=True)
    refresh_manifest(tmp_path)
    added = folder / "new.txt"
    added.write_text("synthetic test")
    assert added in manifest_files(tmp_path)


def test_reference_partial_identity_does_not_select_other_participant():
    submitted = Path("/submission/P05/site_2/scan_1/Ktrans.nii.gz")
    refs = [Path(f"/reference/P0{i}/Ktrans.nii.gz") for i in (1, 5)]
    assert scoring._choose_reference_match(submitted, refs,
        submission_root=Path("/submission"), reference_root=Path("/reference")) == refs[1]
    assert scoring._choose_reference_match(submitted, refs[:1],
        submission_root=Path("/submission"), reference_root=Path("/reference")) is None


def test_ambiguous_reference_is_not_guessed():
    assert scoring._choose_reference_match(Path("/submission/Ktrans.nii.gz"),
        [Path("/reference/a/Ktrans.nii.gz"), Path("/reference/b/Ktrans.nii.gz")]) is None


@pytest.mark.parametrize("sid", ["../outside", "/outside", "..", "a\\b"])
def test_output_discovery_rejects_path_ids(tmp_path, monkeypatch, sid):
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(scoring, "OUTPUTS_DIR", tmp_path / "outputs")
    assert scoring._find_output_niftis(sid, "asl") == []
    assert not (outside / ".osipi_manifest.json").exists()


def test_output_discovery_rejects_symlink_escape(tmp_path, monkeypatch):
    extracted = tmp_path / "extracted"
    extracted.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (extracted / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", extracted)
    assert scoring._find_output_niftis("linked", "asl") == []
    assert not (outside / ".osipi_manifest.json").exists()


def test_scientific_values_keep_sub_micro_precision():
    value = 1.5846222574425252e-7
    assert scoring._json_float(value) == value
    result = scoring._comparison_metrics([value, value], [0.0, 0.0])
    assert result["bias"] == pytest.approx(value, abs=1e-20)
    assert result["mae"] == pytest.approx(value, abs=1e-20)
    assert float(main._fmt_export_cell(result["bias"])) > 0


@pytest.mark.parametrize("blinded", [True, False])
def test_long_csv_preserves_scan_identity_and_joins_qc_by_path(blinded):
    maps=[]; refs=[]
    for i in (1, 2):
        identity = dict(dataset="synthetic", participant="5", repeat=str(i), site="2")
        path = f"/private/input/P05/site_2/scan_{i}/Ktrans.nii.gz"
        maps.append(dict(identity, path=path, detected_map_type="Ktrans",
                         metadata={"nan_count": i, "inf_count": 0}, stats={}))
        refs.append(dict(identity, submitted_path=path, submitted_file="Ktrans.nii.gz",
                         detected_map_type="Ktrans", status="compared",
                         whole_map={"status":"compared", "bias":i * 1e-7}))
    summary={"challenge_type":"dce", "nifti_analysis": {
        "maps":maps, "reference_scoring":{"maps":refs}}}
    header, rows = main._long_csv_rows({"test":summary}, ["test"], blinded)
    assert all(len(row)==len(header) for row in rows)
    records=[dict(zip(header,row)) for row in rows]
    biases=[r for r in records if r["metric_name"]=="bias"]
    assert [r["participant"] for r in biases] == ["5", "5"]
    assert [r["subject_id"] for r in biases] == ["5", "5"]
    assert [r["repeat"] for r in biases] == ["1", "2"]
    assert [r["session_or_repeat_id"] for r in biases] == ["1", "2"]
    assert [r["site"] for r in biases] == ["2", "2"]
    assert [r["nan_voxel_count"] for r in biases] == ["1", "2"]
    assert len({r["map_id"] for r in biases}) == 2
    assert "/private/input" not in str(rows)
