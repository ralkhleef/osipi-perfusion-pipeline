"""Overruling how an upload was grouped into submissions.

A ZIP containing ``P01`` through ``P10`` is either one submission covering ten
participants or ten separate submissions. Nothing in the files distinguishes
them, so detection has to guess, and a reviewer who can see the folder names
has to be able to say the guess was wrong.

The requirement these tests hold to is that regrouping never loses a file.
A regrouping that half-finishes would leave someone with a submission quietly
missing scans, which is worse than one that refuses to run: the numbers would
still come out, and they would be wrong.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


@pytest.fixture()
def extracted(tmp_path, monkeypatch):
    """Point the ingest service at a throwaway extracted directory."""
    from services import ingest_service, path_config
    root = tmp_path / "extracted"
    root.mkdir()
    monkeypatch.setattr(path_config, "EXTRACTED_DIR", root, raising=False)
    monkeypatch.setattr(ingest_service, "EXTRACTED_DIR", root, raising=False)
    return root


def _one_submission(root: Path, name: str, participants: list[str]) -> Path:
    """One submission whose inner folders are participants."""
    base = root / name
    for participant in participants:
        for site in ("site_1", "site_2"):
            d = base / participant / site
            d.mkdir(parents=True)
            (d / "Ktrans.nii.gz").write_bytes(b"map")
            (d / "vp.nii.gz").write_bytes(b"map")
    (base / "README.md").write_text("shared", encoding="utf-8")
    return base


def _files_under(path: Path) -> set[str]:
    return {str(p.relative_to(path)) for p in path.rglob("*")
            if p.is_file() and not p.name.startswith(".")}


def test_splitting_makes_one_submission_per_inner_folder(extracted) -> None:
    from services.ingest_service import regroup_submissions
    _one_submission(extracted, "upload", ["P01", "P02", "P03"])
    result = regroup_submissions(["upload"], "split")
    assert result["success"], result
    assert result["count"] == 3
    ids = sorted(s["submission_id"] for s in result["submissions"])
    assert ids == ["upload_P01", "upload_P02", "upload_P03"], ids
    for sub_id in ids:
        assert (extracted / sub_id).is_dir()
    assert not (extracted / "upload").exists(), "the original was left behind"


def test_splitting_gives_every_submission_the_shared_files(extracted) -> None:
    """A README beside the participants belongs to all of them, as in the carve."""
    from services.ingest_service import regroup_submissions
    _one_submission(extracted, "upload", ["P01", "P02"])
    regroup_submissions(["upload"], "split")
    for sub_id in ("upload_P01", "upload_P02"):
        assert (extracted / sub_id / "README.md").exists(), sub_id


def test_splitting_loses_no_scan_files(extracted) -> None:
    from services.ingest_service import regroup_submissions
    base = _one_submission(extracted, "upload", ["P01", "P02", "P03"])
    before = {name for name in _files_under(base) if name.endswith(".nii.gz")}
    regroup_submissions(["upload"], "split")
    after = set()
    for sub in sorted(extracted.iterdir()):
        if not sub.is_dir():
            continue
        participant = sub.name.split("_")[-1]
        after |= {f"{participant}/{n}" for n in _files_under(sub)
                  if n.endswith(".nii.gz")}
    assert after == before, sorted(before ^ after)


def test_merging_restores_the_folder_the_carve_consumed(extracted) -> None:
    """The whole point: the participant level has to come back.

    Without it the merged paths start at site_1/ and the participant can no
    longer be determined, which is the bug that started all of this.
    """
    from services.ingest_service import regroup_submissions
    _one_submission(extracted, "upload", ["P01", "P02"])
    regroup_submissions(["upload"], "split")

    merged = regroup_submissions(["upload_P01", "upload_P02"], "merge")
    assert merged["success"], merged
    root = extracted / merged["submission_id"]
    names = _files_under(root)
    assert any(n.startswith("P01/") for n in names), sorted(names)[:6]
    assert any(n.startswith("P02/") for n in names), sorted(names)[:6]


def test_a_split_then_merge_round_trip_keeps_every_scan(extracted) -> None:
    from services.ingest_service import regroup_submissions
    base = _one_submission(extracted, "upload", ["P01", "P02", "P03"])
    before = {n for n in _files_under(base) if n.endswith(".nii.gz")}

    regroup_submissions(["upload"], "split")
    merged = regroup_submissions(
        ["upload_P01", "upload_P02", "upload_P03"], "merge")
    after = {n for n in _files_under(extracted / merged["submission_id"])
             if n.endswith(".nii.gz")}
    assert after == before, sorted(before ^ after)


def test_the_old_submissions_are_gone_after_merging(extracted) -> None:
    """Leaving them would double every count downstream."""
    from services.ingest_service import regroup_submissions
    _one_submission(extracted, "upload", ["P01", "P02"])
    regroup_submissions(["upload"], "split")
    regroup_submissions(["upload_P01", "upload_P02"], "merge")
    assert not (extracted / "upload_P01").exists()
    assert not (extracted / "upload_P02").exists()


# ── The two mechanisms that restore a participant name ────────────────────
#
# The merged folder name comes from the manifest, and falls back to trimming
# the id. Either alone gets P01 right, which is deliberate redundancy but also
# means breaking one is invisible end to end. So both are pinned directly.

@pytest.mark.parametrize("ids,expected", [
    (["upload_P01", "upload_P02"], "upload"),
    (["upload_P01", "upload_P02", "upload_P10"], "upload"),
    # The case that caused the bug: a character-wise prefix gives "upload_P0",
    # which would restore the participants as "1" and "2".
    (["run_2_P01", "run_2_P09"], "run_2"),
    (["a_Team_A", "a_Team_B"], "a_Team"),
    (["solo"], "solo"),
    ([], ""),
])
def test_the_merged_name_stops_at_an_underscore_boundary(ids, expected) -> None:
    from services.ingest_service import _merged_stem
    assert _merged_stem(ids) == expected


def test_the_recorded_folder_name_is_what_merging_uses(extracted) -> None:
    """Not the id. Deriving a name from an id is guesswork; this is recorded.

    refresh_manifest resolves original_path to an absolute path, so what the
    merge needs is its last component. Asserting the raw string here would pin
    an incidental detail of where the test happens to run.
    """
    from services.ingest_service import regroup_submissions
    from osipi_pipeline.ingestion.manifest import load_manifest
    _one_submission(extracted, "upload", ["P01", "P02"])
    regroup_submissions(["upload"], "split")
    recorded = {
        Path(str((load_manifest(extracted / f"upload_{p}") or {}).get("original_path") or "")).name
        for p in ("P01", "P02")
    }
    assert recorded == {"P01", "P02"}, recorded


# ── Refusals ──────────────────────────────────────────────────────────────

def test_an_unknown_mode_is_refused(extracted) -> None:
    from services.ingest_service import regroup_submissions
    assert regroup_submissions(["x"], "shuffle")["success"] is False


def test_splitting_needs_exactly_one_submission(extracted) -> None:
    from services.ingest_service import regroup_submissions
    assert regroup_submissions(["a", "b"], "split")["success"] is False


def test_merging_needs_at_least_two(extracted) -> None:
    from services.ingest_service import regroup_submissions
    assert regroup_submissions(["a"], "merge")["success"] is False


def test_splitting_something_with_no_inner_folders_is_refused(extracted) -> None:
    """Otherwise it would silently produce nothing and look like it worked."""
    from services.ingest_service import regroup_submissions
    flat = extracted / "flat"
    flat.mkdir()
    (flat / "cbf.nii.gz").write_bytes(b"map")
    result = regroup_submissions(["flat"], "split")
    assert result["success"] is False
    assert "split" in result["error"].lower() or "folder" in result["error"].lower()
    assert (flat / "cbf.nii.gz").exists(), "a refused split still moved files"


def test_a_missing_submission_is_refused_by_name(extracted) -> None:
    from services.ingest_service import regroup_submissions
    result = regroup_submissions(["nope"], "split")
    assert result["success"] is False
    assert "nope" in result["error"]


def test_no_staging_directories_are_left_behind(extracted) -> None:
    """The staging dir is how a half-finished move is kept out of sight."""
    from services.ingest_service import regroup_submissions
    _one_submission(extracted, "upload", ["P01", "P02"])
    regroup_submissions(["upload"], "split")
    leftovers = [p.name for p in extracted.iterdir() if p.name.startswith(".regroup-")]
    assert not leftovers, leftovers
