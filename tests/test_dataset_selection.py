"""Choosing a dataset for a submission whose folders do not name one.

The DCE challenge lead lays her data out as participant, site, scan. There is
no dataset level anywhere in it. That is a perfectly reasonable layout, and it
failed every one of her 180 files as missing a dataset, because the challenge
declares two datasets and completeness demanded one.

Her grid, 3 sites and 2 repeats, matches the declared ``synthetic`` dataset and
nothing else, so the answer was forced by the data. That is the only case in
which one is chosen automatically. When two datasets fit, or none do, it
declines and says so, because quietly picking one would attribute the
submission to the wrong dataset and every grouped statistic after it would be
checked against the wrong grid.

A reviewer can also say outright which dataset a submission is, and saying so
does not make it true: the counts are still checked, and a wrong answer is
still reported.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]

from osipi_pipeline.ingestion.models import SubmissionArtifact  # noqa: E402
from osipi_pipeline.validation.completeness import (  # noqa: E402
    DATASET_AMBIGUOUS,
    DATASET_INFERRED,
    infer_dataset,
    validate_completeness,
)


def scans(participants: int, sites: int, repeats: int, *, dataset=None):
    """Parameter maps covering a grid, with no dataset unless asked."""
    out = []
    for p in range(1, participants + 1):
        for s in range(1, sites + 1):
            for r in range(1, repeats + 1):
                for map_type in ("ktrans", "vp"):
                    out.append(SubmissionArtifact(
                        path=f"P{p:02d}/site_{s}/scan_{r}/{map_type}.nii.gz",
                        role="parameter_map", map_type=map_type,
                        dataset=dataset, participant=str(p),
                        repeat=str(r), site=str(s),
                    ))
    return out


DATASETS = {
    "synthetic": {"participants": None, "repeats": 2, "sites": 3},
    "clinical": {"participants": 5, "repeats": 2, "sites": 1},
}


# ── Inference ─────────────────────────────────────────────────────────────

def test_her_grid_resolves_to_the_only_dataset_that_fits() -> None:
    """10 participants, 3 sites, 2 repeats. Only synthetic allows that."""
    chosen, reason = infer_dataset(scans(10, 3, 2), DATASETS)
    assert chosen == "synthetic", reason
    assert "synthetic" in reason


def test_a_declared_participant_count_still_has_to_match() -> None:
    """Clinical declares 5 participants, so 10 cannot be clinical."""
    chosen, _ = infer_dataset(scans(10, 1, 2), DATASETS)
    assert chosen is None


def test_a_null_count_means_undecided_rather_than_zero() -> None:
    """synthetic declares participants: null, so any number of them fits."""
    for participants in (1, 5, 40):
        chosen, _ = infer_dataset(scans(participants, 3, 2), DATASETS)
        assert chosen == "synthetic", participants


def test_a_grid_matching_nothing_is_declined_with_a_reason() -> None:
    chosen, reason = infer_dataset(scans(3, 7, 4), DATASETS)
    assert chosen is None
    assert "7 sites" in reason and "synthetic" in reason and "clinical" in reason


def test_a_grid_matching_two_datasets_is_declined(monkeypatch) -> None:
    """Guessing between two would attribute the submission to the wrong one."""
    ambiguous = {
        "alpha": {"participants": None, "repeats": 2, "sites": 3},
        "beta": {"participants": None, "repeats": 2, "sites": 3},
    }
    chosen, reason = infer_dataset(scans(4, 3, 2), ambiguous)
    assert chosen is None
    assert "alpha" in reason and "beta" in reason


def test_a_submission_that_names_its_dataset_is_left_alone() -> None:
    chosen, reason = infer_dataset(scans(4, 3, 2, dataset="clinical"), DATASETS)
    assert chosen is None
    assert "already names" in reason


def test_no_declared_datasets_means_nothing_to_infer() -> None:
    chosen, _ = infer_dataset(scans(4, 3, 2), {})
    assert chosen is None


# ── Through validate_completeness, on real DCE configuration ──────────────

def _codes(issues):
    return [i["code"] for i in issues]


def test_her_layout_no_longer_fails_every_file() -> None:
    """The bug: 180 files, every one reported as missing a dataset."""
    issues = validate_completeness(scans(10, 3, 2), challenge="dce")
    assert "INCOMPLETE_ARTIFACT_IDENTITY" not in _codes(issues)
    assert DATASET_INFERRED in _codes(issues)


def test_the_choice_is_reported_rather_than_applied_silently() -> None:
    """It decides which grid the counts are checked against, so it must show."""
    issues = validate_completeness(scans(10, 3, 2), challenge="dce")
    note = next(i for i in issues if i["code"] == DATASET_INFERRED)
    assert note["dataset"] == "synthetic"
    assert "synthetic" in note["message"]
    assert note["severity"] == "info", "an inference is not a problem"


def test_a_reviewer_can_name_the_dataset() -> None:
    issues = validate_completeness(scans(10, 3, 2), challenge="dce",
                                   dataset="synthetic")
    note = next(i for i in issues if i["code"] == DATASET_INFERRED)
    assert "reviewer" in note["reason"]


def test_naming_the_wrong_dataset_does_not_make_it_true() -> None:
    """The counts are still checked. Saying clinical does not make it clinical."""
    issues = validate_completeness(scans(10, 3, 2), challenge="dce",
                                   dataset="clinical")
    assert "DATASET_COUNT_MISMATCH" in _codes(issues)


def test_an_undecidable_grid_warns_instead_of_guessing() -> None:
    issues = validate_completeness(scans(3, 7, 4), challenge="dce")
    assert DATASET_AMBIGUOUS in _codes(issues)
    warning = next(i for i in issues if i["code"] == DATASET_AMBIGUOUS)
    assert warning["severity"] == "warning"


def test_a_submission_with_dataset_folders_is_untouched() -> None:
    """Inference must not override what the submission actually says."""
    issues = validate_completeness(scans(5, 1, 2, dataset="clinical"),
                                   challenge="dce")
    assert DATASET_INFERRED not in _codes(issues)


def test_the_folders_win_over_a_reviewer_naming_a_different_dataset() -> None:
    """The data outranks the form field.

    A reviewer picking a dataset is for submissions that do not name one. When
    the folders do say, silently relabelling every scan would rewrite the
    submission's own identity from a dropdown, and the grouped statistics after
    it would describe scans that were never in that dataset.
    """
    declared = scans(5, 1, 2, dataset="clinical")
    issues = validate_completeness(declared, challenge="dce",
                                   dataset="synthetic")
    assert DATASET_INFERRED not in _codes(issues), (
        "a reviewer's choice overrode the dataset the folders declare")


def test_asl_gets_no_dataset_inference() -> None:
    """ASL declares no datasets, so none of this applies to it.

    ASL does declare required maps, so it still reports those, and asserting
    no issues at all would pass for the wrong reason. What matters is that no
    dataset was invented for a challenge that has none.
    """
    codes = _codes(validate_completeness(scans(4, 3, 2), challenge="asl"))
    assert DATASET_INFERRED not in codes
    assert DATASET_AMBIGUOUS not in codes
