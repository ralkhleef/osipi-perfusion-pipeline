"""A folder naming a participant is not a redundant wrapper.

Submission ZIPs commonly wrap everything in one named folder, and that level
carries nothing, so extraction promotes its contents. A ZIP of one participant
extracts to a single ``P01/`` directory, which looks exactly the same and is
not the same at all: promoting it discarded the only thing saying whose scans
these are, and all eighteen files then failed as unidentifiable.

The existing tests covered multi-participant layouts, where two or more top
level folders mean no unwrapping happens. One participant on its own went
straight down the wrapper path and was never exercised, which is how a
reviewer found it and the suite did not.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


def _tree(root: Path, sites=("site_1", "site_2"), scans=("scan_1", "scan_2")):
    for site in sites:
        for scan in scans:
            d = root / site / scan
            d.mkdir(parents=True)
            (d / "Ktrans.nii.gz").write_bytes(b"placeholder")


@pytest.mark.parametrize("name", ["P01", "sub-01", "Participant3", "site_2", "scan_1"])
def test_a_folder_naming_an_identity_level_is_not_unwrapped(tmp_path, name) -> None:
    from services.ingest_service import _redundant_wrapper
    staged = tmp_path / "staged"
    _tree(staged / name)
    assert _redundant_wrapper(staged) is None, (
        f"{name}/ was treated as a redundant wrapper, discarding the level it names")


@pytest.mark.parametrize("name", [
    "Lena_ASL_osipi_named",
    "team_alpha_submission",
    "my_dce_upload",
])
def test_a_genuine_wrapper_is_still_unwrapped(tmp_path, name) -> None:
    """The case this behaviour exists for has to keep working."""
    from services.ingest_service import _redundant_wrapper
    staged = tmp_path / "staged"
    _tree(staged / name)
    inner = _redundant_wrapper(staged)
    assert inner is not None and inner.name == name


def test_structural_folders_are_still_not_unwrapped(tmp_path) -> None:
    from services.ingest_service import _redundant_wrapper
    staged = tmp_path / "staged"
    (staged / "results" / "maps").mkdir(parents=True)
    (staged / "results" / "maps" / "ktrans.nii.gz").write_bytes(b"m")
    assert _redundant_wrapper(staged) is None


def test_two_entries_are_never_a_wrapper(tmp_path) -> None:
    from services.ingest_service import _redundant_wrapper
    staged = tmp_path / "staged"
    _tree(staged / "P01")
    _tree(staged / "P02")
    assert _redundant_wrapper(staged) is None


# ── Through the real extraction path ──────────────────────────────────────

def test_a_zip_of_one_participant_keeps_the_participant(tmp_path, monkeypatch) -> None:
    """End to end: the defect as a reviewer hit it, uploading P01.zip."""
    import zipfile
    from services import ingest_service, path_config

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(path_config, "EXTRACTED_DIR", extracted, raising=False)
    monkeypatch.setattr(ingest_service, "EXTRACTED_DIR", extracted, raising=False)

    source = tmp_path / "source"
    _tree(source / "P01", sites=("site_1", "site_2", "site_3"))
    archive = tmp_path / "P01.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in source.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source))

    result = ingest_service.save_and_extract_batch_from_path(archive, "P01.zip")
    assert result.get("success"), result
    folder = extracted / result["submission_id"]
    names = {p.name for p in folder.iterdir() if p.is_dir()}
    assert names == {"P01"}, (
        f"the participant folder was stripped; top level is {sorted(names)}")


def test_the_participant_can_still_be_resolved_afterwards(tmp_path, monkeypatch) -> None:
    """The point of keeping the folder: identity resolves from it."""
    import zipfile
    from services import ingest_service, path_config
    from osipi_pipeline.ingestion.identity_parser import parse_directory_identity

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(path_config, "EXTRACTED_DIR", extracted, raising=False)
    monkeypatch.setattr(ingest_service, "EXTRACTED_DIR", extracted, raising=False)

    source = tmp_path / "source"
    _tree(source / "P01")
    archive = tmp_path / "P01.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for path in source.rglob("*"):
            if path.is_file():
                zf.write(path, path.relative_to(source))

    result = ingest_service.save_and_extract_batch_from_path(archive, "P01.zip")
    folder = extracted / result["submission_id"]
    sample = next(folder.rglob("Ktrans.nii.gz"))
    identity = parse_directory_identity(sample.relative_to(folder).parts)
    assert identity.get("participant") == "1", identity
