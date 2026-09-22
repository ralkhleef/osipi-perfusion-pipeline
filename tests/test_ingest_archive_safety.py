"""Boundary and hostile-archive coverage for local ZIP intake."""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


@pytest.mark.parametrize("raw", [
    "/tmp/result.nii.gz",
    "C:\\temp\\result.nii.gz",
    "../../result.nii.gz",
    "folder/../result.nii.gz",
])
def test_untrusted_paths_must_be_relative_and_non_traversing(raw: str) -> None:
    from services.path_config import safe_relative_path

    with pytest.raises(ValueError, match="Unsafe file path"):
        safe_relative_path(raw)


def _isolate(tmp_path: Path, monkeypatch):
    from services import ingest_service, path_config

    extracted = tmp_path / "extracted"
    extracted.mkdir()
    monkeypatch.setattr(path_config, "EXTRACTED_DIR", extracted, raising=False)
    monkeypatch.setattr(ingest_service, "EXTRACTED_DIR", extracted, raising=False)
    return ingest_service, extracted


@pytest.mark.parametrize("members", [
    [("node", b"file"), ("node/child.txt", b"child")],
    [("node/child.txt", b"child"), ("node", b"file")],
    [("maps/CBF.nii.gz", b"first"), ("maps/cbf.nii.gz", b"second")],
    [("maps/cafe\N{COMBINING ACUTE ACCENT}.nii.gz", b"first"),
     ("maps/caf\N{LATIN SMALL LETTER E WITH ACUTE}.nii.gz", b"second")],
])
def test_conflicting_archive_entries_fail_cleanly(
    tmp_path: Path, monkeypatch, members
) -> None:
    ingest_service, extracted = _isolate(tmp_path, monkeypatch)
    archive = tmp_path / "conflict.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, contents in members:
            zf.writestr(name, contents)

    result = ingest_service.save_and_extract_batch_from_path(archive, archive.name)

    assert result["success"] is False
    assert "conflict" in result["error"].lower() or "more than once" in result["error"].lower()
    assert not any(extracted.iterdir()), "failed extraction left a partial submission"


def test_traversing_archive_member_rejects_the_whole_upload(
    tmp_path: Path, monkeypatch
) -> None:
    ingest_service, extracted = _isolate(tmp_path, monkeypatch)
    archive = tmp_path / "traversal.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("safe/CBF.nii.gz", b"valid-looking")
        zf.writestr("../../outside.nii.gz", b"hostile")

    result = ingest_service.save_and_extract_batch_from_path(archive, archive.name)

    assert result["success"] is False
    assert "unsafe path" in result["error"].lower()
    assert not (tmp_path / "outside.nii.gz").exists()
    assert not any(extracted.iterdir()), "failed extraction left safe-looking partial data"


def test_file_count_limit_accepts_exactly_the_limit_and_rejects_one_more(
    tmp_path: Path, monkeypatch
) -> None:
    ingest_service, extracted = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(ingest_service, "EXTRACT_MAX_FILES", 2)

    exact = tmp_path / "exact.zip"
    with zipfile.ZipFile(exact, "w") as zf:
        zf.writestr("CBF.nii.gz", b"a")
        zf.writestr("ATT.nii.gz", b"b")
    accepted = ingest_service.save_and_extract_batch_from_path(exact, exact.name)
    assert accepted["success"] is True, accepted
    assert accepted["file_count"] == 2

    over = tmp_path / "over.zip"
    with zipfile.ZipFile(over, "w") as zf:
        for index in range(3):
            zf.writestr(f"map-{index}.nii.gz", b"x")
    rejected = ingest_service.save_and_extract_batch_from_path(over, over.name)
    assert rejected["success"] is False
    assert "too many files" in rejected["error"].lower()
    assert not (extracted / "_batch_temp_over").exists()


def test_extracted_byte_limit_accepts_exactly_the_limit_and_rejects_one_more(
    tmp_path: Path, monkeypatch
) -> None:
    ingest_service, extracted = _isolate(tmp_path, monkeypatch)
    monkeypatch.setattr(ingest_service, "EXTRACT_MAX_BYTES", 8)

    exact = tmp_path / "exact-bytes.zip"
    with zipfile.ZipFile(exact, "w") as zf:
        zf.writestr("CBF.nii.gz", b"12345678")
    accepted = ingest_service.save_and_extract_batch_from_path(exact, exact.name)
    assert accepted["success"] is True, accepted

    over = tmp_path / "over-bytes.zip"
    with zipfile.ZipFile(over, "w") as zf:
        zf.writestr("CBF.nii.gz", b"123456789")
    rejected = ingest_service.save_and_extract_batch_from_path(over, over.name)
    assert rejected["success"] is False
    assert "size limit" in rejected["error"].lower()
    assert not (extracted / "_batch_temp_over-bytes").exists()
