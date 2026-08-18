"""Reference maps and ROI masks are discovered once per physical file."""

from __future__ import annotations

from pathlib import Path

import pytest

from osipi_pipeline.testing import VOLUME_SHAPE as SHAPE, write_nifti

KTRANS = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8]
TUMOUR_MASK = [1, 1, 1, 1, 0, 0, 0, 0]


def _write(path: Path, values: list[float], shape: tuple[int, ...] = SHAPE) -> None:
    write_nifti(path, values, shape)


def _reference_tree(root: Path) -> Path:
    """A reference root with two masks and one map."""
    _write(root / "masks" / "tumour.nii.gz", TUMOUR_MASK)
    _write(root / "masks" / "whole_brain.nii.gz", [1] * 8)
    _write(root / "maps" / "Ktrans.nii.gz", KTRANS)
    return root


def _alias(root: Path, real: str, alias: str) -> bool:
    """Make ``alias`` name the same directory as ``real``.

    On a case-insensitive filesystem the alias already exists and nothing is
    created. On a case-sensitive one a symlink reproduces the same condition:
    two paths, one inode.
    """
    target = root / alias
    if target.exists():
        return True
    try:
        target.symlink_to(root / real, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        return False


# Canonical path identity

def test_two_paths_to_one_file_share_a_key(tmp_path: Path) -> None:
    from scoring import canonical_path_key

    root = _reference_tree(tmp_path / "reference")
    if not _alias(root, "masks", "Masks"):
        pytest.skip("filesystem supports neither case-folding nor symlinks")

    assert canonical_path_key(root / "masks" / "tumour.nii.gz") == \
        canonical_path_key(root / "Masks" / "tumour.nii.gz")


def test_distinct_files_do_not_share_a_key(tmp_path: Path) -> None:
    from scoring import canonical_path_key

    root = _reference_tree(tmp_path / "reference")
    assert canonical_path_key(root / "masks" / "tumour.nii.gz") != \
        canonical_path_key(root / "masks" / "whole_brain.nii.gz")


def test_key_falls_back_when_the_file_is_absent(tmp_path: Path) -> None:
    """A missing path must still produce a usable, stable key, not raise."""
    from scoring import canonical_path_key

    missing = tmp_path / "gone.nii.gz"
    assert canonical_path_key(missing) == canonical_path_key(missing)


def test_key_is_case_insensitive_in_the_fallback(tmp_path: Path) -> None:
    """The stat-less fallback normalises case, matching macOS semantics."""
    import os

    from scoring import canonical_path_key

    if os.path.normcase("A") == "A":       # POSIX: normcase is identity
        pytest.skip("normcase is identity on this platform")
    assert canonical_path_key(tmp_path / "Masks" / "x.nii.gz") == \
        canonical_path_key(tmp_path / "masks" / "x.nii.gz")


# ── Mask discovery ────────────────────────────────────────────────────────

def test_aliased_mask_directory_yields_each_mask_once(tmp_path: Path) -> None:
    import scoring

    root = _reference_tree(tmp_path / "reference")
    if not _alias(root, "masks", "Masks"):
        pytest.skip("filesystem supports neither case-folding nor symlinks")

    masks = scoring._reference_masks(root)
    assert len(masks) == 2, [m["name"] for m in masks]
    assert sorted(m["name"] for m in masks) == ["tumour.nii.gz", "whole_brain.nii.gz"]


def test_case_sensitive_filesystem_keeps_both_directories(tmp_path: Path) -> None:
    """Two real directories holding different masks must both be read."""
    import scoring

    root = tmp_path / "reference"
    _write(root / "masks" / "tumour.nii.gz", TUMOUR_MASK)
    if (root / "Masks").exists():
        pytest.skip("case-insensitive filesystem: the two names are one directory")
    _write(root / "Masks" / "cortex.nii.gz", [1] * 8)

    names = sorted(m["name"] for m in scoring._reference_masks(root))
    assert names == ["cortex.nii.gz", "tumour.nii.gz"]


def test_an_aliased_directory_is_only_scanned_once(tmp_path: Path, monkeypatch) -> None:
    """Deduplicating the directory list is a real saving, not decoration.

    Filtering the file results alone would produce the right answer, so
    without this the directory-level guard could be deleted unnoticed. Each
    scan refreshes a manifest, so scanning one directory twice is genuine
    duplicated I/O on a large reference tree.
    """
    import scoring

    root = _reference_tree(tmp_path / "reference")
    if not _alias(root, "masks", "Masks"):
        pytest.skip("filesystem supports neither case-folding nor symlinks")

    scanned: list[Path] = []
    real = scoring._nifti_file_list
    monkeypatch.setattr(scoring, "_nifti_file_list",
                        lambda p: (scanned.append(p), real(p))[1])

    scoring._reference_masks(root)
    assert len(scanned) == 1, [str(p) for p in scanned]


def test_masks_without_a_mask_directory_still_resolve(tmp_path: Path) -> None:
    """The flat fallback path is unaffected by the deduplication change."""
    import scoring

    root = tmp_path / "reference"
    _write(root / "tumour_mask.nii.gz", TUMOUR_MASK)
    assert [m["name"] for m in scoring._reference_masks(root)] == ["tumour_mask.nii.gz"]


def test_symlinked_duplicate_mask_is_not_counted_twice(tmp_path: Path) -> None:
    """Hard/symlinked copies inside one directory collapse too."""
    import scoring

    root = tmp_path / "reference"
    _write(root / "masks" / "tumour.nii.gz", TUMOUR_MASK)
    try:
        (root / "masks" / "tumour_copy.nii.gz").symlink_to(root / "masks" / "tumour.nii.gz")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    assert len(scoring._reference_masks(root)) == 1


# ── Reference map discovery ───────────────────────────────────────────────

def test_aliased_maps_directory_yields_each_map_once(tmp_path: Path) -> None:
    import scoring

    root = _reference_tree(tmp_path / "reference")
    if not _alias(root, "maps", "Maps"):
        pytest.skip("filesystem supports neither case-folding nor symlinks")

    by_type = scoring._reference_maps_by_type(root)
    assert len(by_type.get("Ktrans", [])) == 1, by_type


def test_reference_roots_collapse_aliased_directories(tmp_path: Path, monkeypatch) -> None:
    import scoring

    extracted = tmp_path / "extracted"
    root = _reference_tree(extracted / "sub" / "reference")
    monkeypatch.setattr(scoring, "EXTRACTED_DIR", extracted)
    monkeypatch.setattr(scoring, "REFERENCE_DATA_DIR", root)

    roots = scoring._reference_roots("sub", "dce")
    keys = {scoring.canonical_path_key(r) for r in roots}
    assert len(keys) == len(roots), [str(r) for r in roots]


# ── The count that reaches the user ───────────────────────────────────────

def test_sixteen_scans_and_two_masks_give_exactly_thirty_two_rows(tmp_path: Path) -> None:
    """16 scans x 2 ROIs = 32 rows, the number a reviewer reads.

    Runs the real calculator against masks discovered by the real production
    search, with the aliased directory present, so a regression in either the
    discovery or the deduplication shows up as a wrong row count.
    """
    import scoring
    from services.roi_descriptive_service import (
        compute_roi_descriptive_statistics,
        roi_definitions_from_masks,
    )
    from osipi_pipeline.ingestion.models import SubmissionArtifact

    root = tmp_path / "submission"
    reference = _reference_tree(root / "reference")
    if not _alias(reference, "masks", "Masks"):
        pytest.skip("filesystem supports neither case-folding nor symlinks")

    scans = ([("clinical", str(p), "1", str(r)) for p in range(1, 6) for r in range(1, 3)]
             + [("synthetic", "1", str(s), str(r)) for s in range(1, 4) for r in range(1, 3)])
    assert len(scans) == 16

    artifacts = []
    for dataset, participant, site, repeat in scans:
        rel = (f"{dataset.capitalize()}/Participant{participant}"
               f"/Site{site}/Repeat{repeat}/Ktrans.nii.gz")
        _write(root / rel, KTRANS)
        artifacts.append(SubmissionArtifact(
            path=rel, role="parameter_map", challenge="dce", dataset=dataset,
            participant=participant, repeat=repeat, site=site,
            map_type="ktrans", dimensions=3,
        ))

    masks = scoring._reference_masks(reference)
    assert len(masks) == 2, "mask discovery returned duplicates"

    results = compute_roi_descriptive_statistics(
        artifacts, roi_definitions_from_masks(masks), challenge="dce", root=root)

    assert len(results) == 32, f"expected 32 ROI rows, got {len(results)}"
    pairs = [(r.path, r.roi_id) for r in results]
    assert len(set(pairs)) == 32, "duplicate (scan, ROI) rows present"
    assert sorted({r.roi_id for r in results}) == ["tumour", "whole_brain"]
