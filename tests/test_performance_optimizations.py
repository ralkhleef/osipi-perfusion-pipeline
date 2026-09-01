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


# ── The analysis is computed once, not once per reader ────────────────────
#
# The report, the HTML and PDF renderers, every export route and the frontend
# each ask for the same analysis. On the DCE lead's real submission it reads a
# gigabyte of 4-D data and takes about a minute, so recomputing it per request
# made opening a report cost as much as producing it. It is now memoised on
# its inputs.

@pytest.fixture()
def analysis_workspace(tmp_path, monkeypatch):
    import scoring

    monkeypatch.setattr(scoring, "EXTRACTED_DIR", tmp_path / "extracted")
    monkeypatch.setattr(scoring, "REFERENCE_DATA_DIR", tmp_path / "ref")
    monkeypatch.setattr(scoring, "SCORING_DIR", tmp_path / "scoring")
    monkeypatch.setattr(scoring, "OUTPUTS_DIR", tmp_path / "outputs")
    for name in ("extracted", "ref", "scoring", "outputs"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    scoring.clear_analysis_cache()
    root = tmp_path / "extracted" / "sub"
    root.mkdir(parents=True)
    submitted = root / "Ktrans.nii.gz"
    submitted.write_bytes(b"not really a nifti")
    yield tmp_path, submitted
    scoring.clear_analysis_cache()


def _count_analyses(monkeypatch, scoring):
    calls = {"n": 0}
    real = scoring._score_reference_maps

    def counting(*args, **kwargs):
        calls["n"] += 1
        return real(*args, **kwargs)

    monkeypatch.setattr(scoring, "_score_reference_maps", counting)
    return calls


def test_a_repeated_analysis_is_served_from_cache(analysis_workspace, monkeypatch) -> None:
    import scoring

    calls = _count_analyses(monkeypatch, scoring)
    first = scoring.analyze_submission_niftis("sub", "dce")
    second = scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 1, "the analysis was recomputed for an unchanged submission"
    assert second.get("cache_hit") == "memory"
    assert first.get("cache_hit") is None
    first.pop("cache_hit", None)
    second.pop("cache_hit", None)
    assert first == second


def test_a_changed_submission_is_analysed_again(analysis_workspace, monkeypatch) -> None:
    """A cache that cannot notice new data is worse than no cache."""
    import os
    import scoring

    _tmp, submitted = analysis_workspace
    calls = _count_analyses(monkeypatch, scoring)
    scoring.analyze_submission_niftis("sub", "dce")

    submitted.write_bytes(b"different content entirely")
    os.utime(submitted, (0, 0))
    scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 2, "a modified map was served from cache"


def test_changing_the_configuration_invalidates_the_cache(
    analysis_workspace, monkeypatch,
) -> None:
    import scoring
    from osipi_pipeline.ingestion import manifest

    calls = _count_analyses(monkeypatch, scoring)
    scoring.analyze_submission_niftis("sub", "dce")

    monkeypatch.setattr(manifest, "config_fingerprint", lambda: "a-different-config")
    scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 2, "a configuration change did not invalidate the cache"


def test_a_run_that_writes_artifacts_is_never_cached(
    analysis_workspace, monkeypatch, tmp_path,
) -> None:
    """Those runs are wanted for their side effects, not only their numbers."""
    import scoring

    calls = _count_analyses(monkeypatch, scoring)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    scoring.analyze_submission_niftis("sub", "dce", artifact_dir=artifacts)
    scoring.analyze_submission_niftis("sub", "dce", artifact_dir=artifacts)

    assert calls["n"] == 2


def test_the_cache_can_be_switched_off(analysis_workspace, monkeypatch) -> None:
    import scoring

    monkeypatch.setattr(
        scoring, "performance_settings",
        lambda: {"analysis_cache_enabled": False},
    )
    calls = _count_analyses(monkeypatch, scoring)
    scoring.analyze_submission_niftis("sub", "dce")
    scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 2


def test_a_cached_result_cannot_be_mutated_by_its_reader(analysis_workspace) -> None:
    """Two callers must not share one dict; the report mutates what it reads."""
    import scoring

    first = scoring.analyze_submission_niftis("sub", "dce")
    first["reference_scoring"]["maps"] = ["tampered"]
    second = scoring.analyze_submission_niftis("sub", "dce")
    assert second["reference_scoring"].get("maps") != ["tampered"]


# ── The cache survives a restart ──────────────────────────────────────────
#
# An in-memory cache empties when the app stops. A reviewer who restarts the
# server and reopens yesterday's report should not wait a minute to be told
# what it already computed, so the analysis is also written beside the
# validation results and read back on a cold start.

def test_a_cold_start_reads_the_saved_analysis(analysis_workspace, monkeypatch) -> None:
    import scoring

    calls = _count_analyses(monkeypatch, scoring)
    scoring.analyze_submission_niftis("sub", "dce")
    assert calls["n"] == 1

    # A new process has the same files on disk and an empty memory cache.
    scoring.clear_analysis_cache()
    restarted = scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 1, "the analysis was recomputed after a restart"
    assert restarted.get("cache_hit") == "disk"


def test_the_saved_analysis_is_reachable_only_for_the_same_inputs(
    analysis_workspace, monkeypatch,
) -> None:
    """A saved file must never answer for a submission that has changed."""
    import os
    import scoring

    _tmp, submitted = analysis_workspace
    calls = _count_analyses(monkeypatch, scoring)
    scoring.analyze_submission_niftis("sub", "dce")

    submitted.write_bytes(b"a different map entirely")
    os.utime(submitted, (0, 0))
    scoring.clear_analysis_cache()
    scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 2


def test_superseded_files_are_removed_rather_than_accumulating(
    analysis_workspace, monkeypatch,
) -> None:
    """Re-uploading a submission repeatedly must not fill the disk."""
    import os
    import scoring

    _tmp, submitted = analysis_workspace
    directory = scoring._analysis_cache_dir()

    for i in range(4):
        submitted.write_bytes(b"content %d" % i)
        os.utime(submitted, (i + 1, i + 1))
        scoring.clear_analysis_cache()
        scoring.analyze_submission_niftis("sub", "dce")

    assert len(list(directory.glob("*.json"))) == 1


def test_a_corrupt_saved_analysis_is_a_miss_not_a_crash(
    analysis_workspace, monkeypatch,
) -> None:
    """A truncated write must cost seconds, never a broken report."""
    import scoring

    calls = _count_analyses(monkeypatch, scoring)
    scoring.analyze_submission_niftis("sub", "dce")
    for path in scoring._analysis_cache_dir().glob("*.json"):
        path.write_text("{ this is not json", encoding="utf-8")

    scoring.clear_analysis_cache()
    result = scoring.analyze_submission_niftis("sub", "dce")

    assert calls["n"] == 2
    assert result.get("cache_hit") is None


def test_a_read_only_cache_directory_does_not_break_analysis(
    analysis_workspace, monkeypatch,
) -> None:
    """Failing to cache is not failing to analyse."""
    import scoring

    def refuse(*_args, **_kwargs):
        raise OSError("read-only file system")

    monkeypatch.setattr(scoring.Path, "mkdir", refuse)
    result = scoring.analyze_submission_niftis("sub", "dce")
    assert result["submission_id"] == "sub"


def test_no_temporary_files_are_left_behind(analysis_workspace) -> None:
    import scoring

    scoring.analyze_submission_niftis("sub", "dce")
    leftovers = [p.name for p in scoring._analysis_cache_dir().iterdir()
                 if p.suffix == ".tmp" or ".tmp" in p.name]
    assert not leftovers, leftovers


def test_clearing_on_disk_empties_the_folder(analysis_workspace) -> None:
    import scoring

    scoring.analyze_submission_niftis("sub", "dce")
    assert list(scoring._analysis_cache_dir().glob("*.json"))
    scoring.clear_analysis_cache(on_disk=True)
    assert not list(scoring._analysis_cache_dir().glob("*.json"))
