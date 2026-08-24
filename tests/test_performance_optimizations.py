from __future__ import annotations

from pathlib import Path

import pytest


def _tiny_nifti(path: Path, value: float = 1.0) -> Path:
    import nibabel as nib
    import numpy as np

    data = np.full((3, 3, 3), value, dtype=np.float32)
    nib.save(nib.Nifti1Image(data, np.eye(4)), str(path))
    return path


def test_manifest_load_does_not_rescan_unchanged_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from osipi_pipeline.ingestion import manifest as manifest_mod

    root = tmp_path / "sub"
    root.mkdir()
    (root / "README.md").write_text("# ok\n", encoding="utf-8")
    (root / "Ktrans_map.nii").write_bytes(b"fake")
    first = manifest_mod.refresh_manifest(root, submission_id="sub", challenge_type="dce")

    def fail_rglob(*_args, **_kwargs):
        raise AssertionError("unchanged manifest should not recursively rescan")

    monkeypatch.setattr(Path, "rglob", fail_rglob)
    second = manifest_mod.load_manifest(root, refresh_if_stale=True)

    assert second is not None
    assert second["file_count"] == first["file_count"]


def test_cached_validation_avoids_reopening_unchanged_nifti(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from osipi_pipeline.validation import nifti_validator as nv

    path = _tiny_nifti(tmp_path / "map.nii.gz")
    nv.clear_validation_cache()
    calls = {"count": 0}
    real_load = nv.nib.load

    def counted_load(*args, **kwargs):
        calls["count"] += 1
        return real_load(*args, **kwargs)

    monkeypatch.setattr(nv.nib, "load", counted_load)
    first = nv.validate_nifti_files([path], workers=1)[0]
    second = nv.validate_nifti_files([path], workers=1)[0]

    assert first["valid"] is True
    assert second["cache_hit"] is True
    assert calls["count"] == 1


def test_modifying_nifti_invalidates_validation_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from osipi_pipeline.validation import nifti_validator as nv

    path = _tiny_nifti(tmp_path / "map.nii.gz", 1.0)
    nv.clear_validation_cache()
    assert nv.validate_nifti_files([path], workers=1)[0]["mean"] == pytest.approx(1.0)
    _tiny_nifti(path, 2.0)

    result = nv.validate_nifti_files([path], workers=1)[0]

    assert result["cache_hit"] is False
    assert result["mean"] == pytest.approx(2.0)


def test_config_fingerprint_change_invalidates_validation_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from osipi_pipeline.validation import nifti_validator as nv

    path = _tiny_nifti(tmp_path / "map.nii.gz")
    nv.clear_validation_cache()
    assert nv.validate_nifti_files([path], workers=1)[0]["cache_hit"] is False
    monkeypatch.setattr(nv, "config_fingerprint", lambda: "different-config")

    assert nv.validate_nifti_files([path], workers=1)[0]["cache_hit"] is False


def test_quick_validation_does_not_load_voxel_array(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from osipi_pipeline.validation import nifti_validator as nv

    path = tmp_path / "quick.nii"
    path.write_bytes(b"x" * 400)

    class ExplodingData:
        def __array__(self, *_args, **_kwargs):
            raise AssertionError("quick validation should not load voxels")

    class FakeImage:
        shape = (3, 3, 3)
        dataobj = ExplodingData()
        affine = nv.np.eye(4)

        def get_data_dtype(self):
            return "float32"

    nv.clear_validation_cache()
    monkeypatch.setattr(nv.nib, "load", lambda _path: FakeImage())

    result = nv.validate_nifti_files([path], quick=True, workers=1)[0]

    assert result["valid"] is True
    assert result["validation_mode"] == "quick"
    assert result["mean"] is None


def test_worker_limit_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from osipi_pipeline.validation import nifti_validator as nv

    paths = [_tiny_nifti(tmp_path / f"map_{i}.nii.gz", float(i)) for i in range(4)]
    nv.clear_validation_cache()
    monkeypatch.setattr(nv, "configured_worker_limit", lambda *_args, **_kwargs: 2)

    results = nv.validate_nifti_files(paths)

    assert [r["valid"] for r in results] == [True, True, True, True]
    assert nv.last_worker_count() == 2


def test_parallel_batch_results_are_ordered_and_failures_are_isolated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import services.validation_service as vs

    monkeypatch.setattr(vs, "VALIDATION_SUBDIR", tmp_path)
    monkeypatch.setattr(vs, "configured_worker_limit", lambda *_args, **_kwargs: 2)

    def fake_validate_submission(sid: str, **_kwargs):
        if sid == "bad":
            raise RuntimeError("boom")
        return {
            "submission_id": sid,
            "passed": True,
            "runnable": False,
            "errors": [],
            "warnings": [],
        }

    monkeypatch.setattr(vs, "validate_submission", fake_validate_submission)

    result = vs.validate_batch(["first", "bad", "last"], challenge_type="dce")

    assert [row["submission_id"] for row in result["results"]] == ["first", "bad", "last"]
    assert result["results"][1]["passed"] is False
    assert result["workers"] == 2


def test_preview_cache_reuses_unchanged_item(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import services.nifti_preview_service as ps

    source = tmp_path / "map.nii.gz"
    source.write_bytes(b"fake")
    monkeypatch.setattr(ps, "PREVIEW_ROOT", tmp_path / "previews")
    monkeypatch.setattr(ps, "_candidate_nifti_paths", lambda *_args, **_kwargs: [source])
    calls = {"count": 0}

    def fake_generate(submission_id: str, path: Path):
        calls["count"] += 1
        stat = path.stat()
        return {
            "submission_id": submission_id,
            "map_id": "map",
            "source_path": str(path),
            "source_mtime": stat.st_mtime,
            "source_size": stat.st_size,
                "preview_available": False,
                "preview_config_fingerprint": ps.config_fingerprint(),
                "preview_schema_version": ps.PREVIEW_SCHEMA_VERSION,
            }

    monkeypatch.setattr(ps, "_generate_preview_item", fake_generate)

    ps.list_submission_previews("sub", "dce")
    ps.list_submission_previews("sub", "dce")

    assert calls["count"] == 1


def test_generated_outputs_refresh_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import services.validation_service as vs

    output_dir = tmp_path / "outputs"
    output_dir.mkdir()
    (output_dir / "ktrans_map.nii").write_bytes(b"fake")

    result = vs.validate_generated_outputs(output_dir, challenge_type="dce", map_type="ktrans")

    assert result["nifti_count"] == 1
    assert (output_dir / ".osipi_manifest.json").exists()


@pytest.mark.parametrize(
    ("challenge", "present_name", "missing_label"),
    [
        ("asl", "CBF_map.nii", "ATT"),
        ("dce", "vp_map.nii", "Ktrans"),
    ],
)
def test_generated_outputs_require_configured_analysis_maps(
    tmp_path: Path, challenge: str, present_name: str, missing_label: str
) -> None:
    import services.validation_service as vs

    output_dir = tmp_path / challenge
    output_dir.mkdir()
    (output_dir / present_name).write_bytes(b"fake")

    result = vs.validate_generated_outputs(output_dir, challenge_type=challenge)

    assert result["passed"] is False
    assert result["output_complete"] is False
    assert any(
        issue["code"] == "REQUIRED_MAP_MISSING"
        and missing_label.lower() in issue["message"].lower()
        for issue in result["errors"]
    )
