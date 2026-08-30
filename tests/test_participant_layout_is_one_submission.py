"""Participants are not submissions.

The DCE challenge lead's synthetic submission is one team's work laid out as
``P01`` through ``P10``. Batch detection treated every top-level directory as a
separate submission, so it became ten of them, and each one's paths then began
at ``site_1/``. The participant level had been discarded by the carve, so it
could no longer be determined, and every file failed with
INCOMPLETE_ARTIFACT_IDENTITY. Nothing was wrong with the submission.

The distinction that has to hold is between a batch of separate submissions,
which really is several teams::

    batch_upload/Team_A/  Team_B/

and one submission covering many participants::

    submission/P01/  P02/

Getting this wrong in the other direction would be worse: a real batch merged
into one submission would score several teams together. So the tests below
pin both directions.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


def _dirs(tmp_path: Path, names: list[str]) -> list[Path]:
    for name in names:
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    return sorted(d for d in tmp_path.iterdir() if d.is_dir())


@pytest.mark.parametrize("names", [
    ["P01", "P02"],
    # Sites and repeats are inside a submission too. Recognising only
    # participants meant a submission uploaded one participant at a time still
    # carved on its sites, and the identity broke exactly as before.
    ["site_1", "site_2", "site_3"],
    ["scan_1", "scan_2"],
    ["ses-01", "ses-02"],
    ["visit_1", "visit_2"],
    ["P01", "P02", "P03", "P04", "P05", "P06", "P07", "P08", "P09", "P10"],
    ["sub-01", "sub-02"],
    ["Participant1", "Participant2"],
    ["subject_1", "subject_2"],
])
def test_participant_directories_are_one_submission(tmp_path, names) -> None:
    from services.ingest_service import _is_participant_layout
    assert _is_participant_layout(_dirs(tmp_path, names)) is True


@pytest.mark.parametrize("names", [
    ["Team_A", "Team_B"],
    ["alpha", "beta"],
    # Team names that merely begin with the participant prefix. Matching on the
    # letter alone would swallow a real batch, which is the worse failure.
    ["Pixel", "Photon"],
    ["Perfusion_Lab", "Prague_Group"],
])
def test_team_directories_are_still_a_batch(tmp_path, names) -> None:
    from services.ingest_service import _is_participant_layout
    assert _is_participant_layout(_dirs(tmp_path, names)) is False


def test_a_mixture_is_not_treated_as_participants(tmp_path) -> None:
    """One team folder among participants means the carve is not safe."""
    from services.ingest_service import _is_participant_layout
    assert _is_participant_layout(_dirs(tmp_path, ["P01", "P02", "Team_B"])) is False


def test_a_single_directory_is_never_a_batch(tmp_path) -> None:
    from services.ingest_service import _is_participant_layout
    assert _is_participant_layout(_dirs(tmp_path, ["P01"])) is False


def test_no_directories_at_all(tmp_path) -> None:
    from services.ingest_service import _is_participant_layout
    assert _is_participant_layout([]) is False


# ── End to end through the real detector ──────────────────────────────────

def _submission_tree(root: Path, participants: list[str]) -> None:
    for participant in participants:
        for site in ("site_1", "site_2"):
            for scan in ("scan_1", "scan_2"):
                d = root / participant / site / scan
                d.mkdir(parents=True)
                (d / "Ktrans.nii.gz").write_bytes(b"placeholder")


def test_ten_participants_stay_one_submission(tmp_path) -> None:
    """The shape of the real thing, and the bug this file exists for."""
    from services.ingest_service import detect_batch_boundaries
    root = tmp_path / "extracted"
    root.mkdir()
    _submission_tree(root, [f"P{i:02d}" for i in range(1, 11)])
    assert detect_batch_boundaries(root) is None, (
        "ten participants were carved into ten submissions again")


def test_a_wrapper_around_participants_is_also_one_submission(tmp_path) -> None:
    """Her ZIP has a ``submission/`` wrapper, so the inner path matters too."""
    from services.ingest_service import detect_batch_boundaries
    root = tmp_path / "extracted"
    (root / "submission").mkdir(parents=True)
    _submission_tree(root / "submission", ["P01", "P02", "P03"])
    assert detect_batch_boundaries(root) is None


def test_one_participants_folder_uploaded_alone(tmp_path) -> None:
    """Uploading P01 on its own, to avoid waiting for all sixty scans.

    Its top level is then site_1, site_2, site_3, which recognising only
    participants did not catch, so it carved into three "submissions" whose
    paths began at scan_1 and every file lost its site.
    """
    from services.ingest_service import detect_batch_boundaries
    root = tmp_path / "extracted"
    for site in ("site_1", "site_2", "site_3"):
        for scan in ("scan_1", "scan_2"):
            d = root / site / scan
            d.mkdir(parents=True)
            (d / "Ktrans.nii.gz").write_bytes(b"placeholder")
    assert detect_batch_boundaries(root) is None, (
        "one participant's sites were carved into separate submissions")


def test_a_genuine_batch_is_still_split(tmp_path) -> None:
    """The regression that would matter most: several teams scored as one."""
    from services.ingest_service import detect_batch_boundaries
    root = tmp_path / "extracted"
    root.mkdir()
    for team in ("Team_A", "Team_B", "Team_C"):
        d = root / team / "results" / "maps"
        d.mkdir(parents=True)
        (d / "ktrans.nii.gz").write_bytes(b"placeholder")
    found = detect_batch_boundaries(root)
    assert found is not None and len(found) == 3, found
