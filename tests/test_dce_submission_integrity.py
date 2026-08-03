"""A DCE-2026 team submission must survive upload intact.

Regression tests for the four defects a manual upload exposed
(CODE_WALKTHROUGH.md §B1, §B2, §3.7, §B6). Every one of them was invisible to
the existing suite because no test routed a multi-dataset submission through
the real uploader — these do.

The expected end state for the fixture below, stated once:

    1 submission
    ├── Clinical    5 participants x 2 repeats x 1 site  = 10 scans
    ├── Synthetic   1 participant  x 2 repeats x 3 sites =  6 scans
    └── methods.txt

    16 Ktrans maps · 16 modelled S(t) files · 1 methods document
    no missing-dataset-identity errors
    no duplicate-filename warnings for standard per-scan names
    reference files excluded from participant completeness
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from osipi_pipeline.testing import (
    VOLUME_SHAPE as SHAPE,
    VOLUME_VALUES,
    build_dce_submission,
    write_nifti,
    zip_directory,
)

VALUES = list(VOLUME_VALUES)


def _write(path: Path, values=VALUES, shape=SHAPE) -> None:
    """Thin alias so the tests below read as tests, not as plumbing."""
    write_nifti(path, values, shape)


@pytest.fixture()
def dce_zip(tmp_path: Path) -> Path:
    submission = build_dce_submission(tmp_path / "stage", "DCE Test Clean")
    return zip_directory(submission, tmp_path / "DCE Test Clean.zip")


@pytest.fixture()
def uploaded(dce_zip: Path, tmp_path: Path, monkeypatch):
    """Run the real upload path against an isolated extraction directory."""
    import services.ingest_service as ingest

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(ingest, "EXTRACTED_DIR", extracted)

    result = ingest.save_and_extract_batch_from_path(dce_zip, dce_zip.name)
    return result, extracted


# ── B1: dataset directories stay together ─────────────────────────────────

def test_dataset_directories_do_not_split_the_submission(uploaded) -> None:
    result, _ = uploaded
    assert result["batch"] is False, (
        "Clinical/ and Synthetic/ were split into separate submissions; "
        f"got {result.get('submission_count')} submissions"
    )


def test_single_submission_keeps_both_datasets(uploaded) -> None:
    result, extracted = uploaded
    root = extracted / result["submission_id"]
    assert (root / "Clinical").is_dir()
    assert (root / "Synthetic").is_dir()


def test_dataset_names_are_read_from_configuration_not_hardcoded() -> None:
    from osipi_pipeline.config.rules import datasets_by_challenge
    from services.ingest_service import _dataset_dir_names

    configured = {name.lower() for spec in datasets_by_challenge().values()
                  for name in spec}
    assert _dataset_dir_names() == configured
    assert "synthetic" in configured and "clinical" in configured


def test_a_real_team_batch_still_splits(tmp_path: Path) -> None:
    """The fix must not disable batch detection for actual multi-team ZIPs."""
    from services.ingest_service import detect_batch_boundaries

    staged = tmp_path / "staged"
    for team in ("Team_A", "Team_B"):
        _write(staged / team / "Ktrans.nii.gz", VALUES, SHAPE)
    found = detect_batch_boundaries(staged)
    assert found is not None and len(found) == 2


def test_mixed_dataset_and_team_names_still_split(tmp_path: Path) -> None:
    """Only an all-dataset set is one submission; a stray team dir is a batch."""
    from services.ingest_service import detect_batch_boundaries

    staged = tmp_path / "staged"
    for name in ("Synthetic", "Team_B"):
        _write(staged / name / "Ktrans.nii.gz", VALUES, SHAPE)
    found = detect_batch_boundaries(staged)
    assert found is not None and len(found) == 2


# ── B2: shared root files survive ─────────────────────────────────────────

def test_methods_document_survives_upload(uploaded) -> None:
    result, extracted = uploaded
    root = extracted / result["submission_id"]
    assert (root / "methods.txt").is_file(), "the required methods document was lost"


def test_shared_root_files_reach_every_carved_submission(tmp_path: Path, monkeypatch) -> None:
    """A genuine batch with a shared README gives each submission a copy."""
    import services.ingest_service as ingest

    staged = tmp_path / "stage" / "batch"
    for team in ("Team_A", "Team_B"):
        _write(staged / team / "Ktrans.nii.gz", VALUES, SHAPE)
    (staged / "methods.txt").write_text("Shared methods.", encoding="utf-8")

    archive = tmp_path / "batch.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(staged.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staged.parent))

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(ingest, "EXTRACTED_DIR", extracted)
    result = ingest.save_and_extract_batch_from_path(archive, archive.name)

    assert result["batch"] is True and result["submission_count"] == 2
    for submission in result["submissions"]:
        shared = extracted / submission["submission_id"] / "methods.txt"
        assert shared.is_file(), f"{submission['submission_id']} lost the shared file"
        assert shared.read_text(encoding="utf-8") == "Shared methods."


def test_a_submissions_own_file_wins_over_the_shared_copy(tmp_path: Path, monkeypatch) -> None:
    import services.ingest_service as ingest

    staged = tmp_path / "stage" / "batch"
    for team in ("Team_A", "Team_B"):
        _write(staged / team / "Ktrans.nii.gz", VALUES, SHAPE)
    (staged / "Team_A" / "methods.txt").write_text("Team A methods.", encoding="utf-8")
    (staged / "methods.txt").write_text("Shared methods.", encoding="utf-8")

    archive = tmp_path / "batch.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in sorted(staged.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(staged.parent))

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(ingest, "EXTRACTED_DIR", extracted)
    ingest.save_and_extract_batch_from_path(archive, archive.name)

    own = extracted / "batch_Team_A" / "methods.txt"
    assert own.read_text(encoding="utf-8") == "Team A methods."


# ── Identity and counts on the assembled submission ───────────────────────

@pytest.fixture()
def artifacts(uploaded):
    from osipi_pipeline.ingestion.manifest import load_manifest
    from osipi_pipeline.ingestion.models import SubmissionArtifact

    result, extracted = uploaded
    root = extracted / result["submission_id"]
    manifest = load_manifest(root, refresh_if_stale=False) or {}
    return [SubmissionArtifact(**item) for item in manifest.get("artifacts", [])]


def test_every_scan_artifact_resolves_its_dataset(artifacts) -> None:
    """Files inside a dataset directory must know which dataset they are in.

    The methods document sits at the submission root and legitimately belongs
    to no single dataset, so it is excluded rather than asserted against.
    """
    scan_artifacts = [a for a in artifacts if "/" in a.path]
    assert scan_artifacts
    unresolved = [a.path for a in scan_artifacts if a.dataset is None]
    assert not unresolved, f"{len(unresolved)} artifacts lost dataset identity"


def test_the_methods_document_is_recorded_as_an_artifact(artifacts) -> None:
    assert [a for a in artifacts if a.artifact_type == "methods"]


def test_scan_counts_match_the_configured_grid(artifacts) -> None:
    scans = {(a.dataset, a.participant, a.site, a.repeat)
             for a in artifacts if a.map_type == "ktrans"}
    assert len([s for s in scans if s[0] == "clinical"]) == 10
    assert len([s for s in scans if s[0] == "synthetic"]) == 6


def test_sixteen_ktrans_and_sixteen_modelled_signal_files(artifacts) -> None:
    assert len([a for a in artifacts if a.map_type == "ktrans"]) == 16
    assert len([a for a in artifacts if a.artifact_type == "modelled_st"]) == 16


def test_identity_is_fully_resolved_for_every_parameter_map(artifacts) -> None:
    maps = [a for a in artifacts if a.role == "parameter_map"]
    assert maps
    for artifact in maps:
        assert artifact.participant is not None
        assert artifact.site is not None
        assert artifact.repeat is not None


# ── §3.7: reference data is not submission content ────────────────────────

def test_reference_tree_is_excluded_from_artifacts(uploaded) -> None:
    from osipi_pipeline.ingestion.manifest import refresh_manifest
    from osipi_pipeline.ingestion.models import SubmissionArtifact

    result, extracted = uploaded
    root = extracted / result["submission_id"]
    _write(root / "reference" / "maps" / "Ktrans.nii.gz", VALUES, SHAPE)
    _write(root / "reference" / "masks" / "tumour.nii.gz", [1] * 8, SHAPE)

    manifest = refresh_manifest(root, submission_id=result["submission_id"],
                                challenge_type="dce", original_path="x")
    artifacts = [SubmissionArtifact(**item) for item in manifest.get("artifacts", [])]

    assert not [a for a in artifacts if a.path.startswith("reference/")]
    # The count is unchanged by the presence of reference data.
    assert len([a for a in artifacts if a.map_type == "ktrans"]) == 16


@pytest.mark.parametrize("path,expected", [
    ("reference/maps/Ktrans.nii.gz", True),
    ("reference/masks/tumour.nii.gz", True),
    ("Reference/Maps/Ktrans.nii.gz", True),
    ("masks/tumour.nii.gz", True),
    ("Clinical/Participant1/Site1/Repeat1/Ktrans.nii.gz", False),
    # A *file* named reference is content, not a reference directory.
    ("Clinical/reference.nii.gz", False),
])
def test_reference_path_detection(path: str, expected: bool) -> None:
    from osipi_pipeline.ingestion.manifest import is_reference_path

    assert is_reference_path(path) is expected


# ── B6: duplicate detection is scoped by scan ─────────────────────────────

def test_standard_per_scan_filenames_are_not_duplicates(uploaded) -> None:
    from osipi_pipeline.validation.validate import duplicate_filename_groups

    result, extracted = uploaded
    root = extracted / result["submission_id"]
    files = [p for p in root.rglob("*") if p.is_file()]
    assert duplicate_filename_groups(files, root, "dce") == []


def test_a_genuine_duplicate_within_one_scan_is_still_reported(uploaded) -> None:
    from osipi_pipeline.validation.validate import duplicate_filename_groups

    result, extracted = uploaded
    root = extracted / result["submission_id"]
    scan = root / "Clinical" / "Participant1" / "Site1" / "Repeat1"
    _write(scan / "copy" / "Ktrans.nii.gz", VALUES, SHAPE)

    files = [p for p in root.rglob("*") if p.is_file()]
    found = duplicate_filename_groups(files, root, "dce")
    assert [name for name, _ in found] == ["ktrans.nii.gz"]


def test_flat_submissions_keep_the_original_behaviour(tmp_path: Path) -> None:
    """No resolvable identity means one bucket — the pre-fix semantics."""
    from osipi_pipeline.validation.validate import duplicate_filename_groups

    root = tmp_path / "flat"
    _write(root / "Ktrans_map.nii.gz", VALUES, SHAPE)
    _write(root / "nested" / "Ktrans_map.nii.gz", VALUES, SHAPE)

    files = [p for p in root.rglob("*") if p.is_file()]
    found = duplicate_filename_groups(files, root, "dce")
    assert [name for name, _ in found] == ["ktrans_map.nii.gz"]


# ── End-to-end: validation is clean ───────────────────────────────────────

def test_validation_reports_no_identity_or_duplicate_issues(uploaded, tmp_path, monkeypatch) -> None:
    import services.validation_service as vs

    result, extracted = uploaded
    monkeypatch.setattr(vs, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(vs, "OUTPUTS_DIR", tmp_path / "outputs")

    report = vs.validate_submission(result["submission_id"], challenge_type="dce")
    codes = [i.get("code") for i in (report.get("errors") or []) + (report.get("warnings") or [])]

    assert "INCOMPLETE_ARTIFACT_IDENTITY" not in codes
    assert "DUPLICATE_FILENAME" not in codes
    assert "REQUIRED_ARTIFACT_MISSING" not in codes
    assert "DATASET_COUNT_MISMATCH" not in codes
