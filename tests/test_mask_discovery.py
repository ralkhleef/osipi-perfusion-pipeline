"""Masks must be found wherever the organiser put them.

A challenge lead reported that masks "don't seem to be applied" and that it was
unclear what bias and MAE were computed over. Both symptoms had one cause:
mask discovery ran against a single reference root, and that root was chosen by
which one contained ground-truth *maps*::

    selected_root = next(r for r in roots if _reference_maps_by_type(r))
    masks = _reference_masks(selected_root)      # only this one

The pipeline offers six candidate roots. An organiser keeping a shared
``masks/`` folder next to a per-challenge ``asl/maps/`` folder, which is a
natural layout and one of the six, had their masks silently ignored: no ROI
rows, and whole-image metrics reported with nothing to say the region
breakdown was missing rather than genuinely empty.

Masks and ground truth are independent assets. The map root must stay a single
choice, mixing two ground truths would be wrong, but a mask found anywhere the
pipeline already looks is a mask the organiser meant to provide.

These tests use real NIfTI files through the real discovery code rather than
stubs, because the bug was in which directories were searched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
for extra in (REPO_ROOT / "src", REPO_ROOT / "backend"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

np = pytest.importorskip("numpy")
nib = pytest.importorskip("nibabel")

import scoring  # noqa: E402

SHAPE = (6, 6, 3)
AFFINE = np.diag([3.0, 3.0, 5.0, 1.0])


def write(path: Path, values) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(np.asarray(values, dtype=np.float32), AFFINE), str(path))
    return path


def cbf_map(scale: float = 1.0):
    rng = np.random.default_rng(11)
    return rng.uniform(20.0, 80.0, SHAPE) * scale


def gm_mask():
    mask = np.zeros(SHAPE, dtype=np.float32)
    mask[1:4, 1:4, :] = 1.0
    return mask


@pytest.fixture()
def organiser(tmp_path, monkeypatch):
    """Pin every organiser-wide reference root inside tmp."""
    roots = {
        "REFERENCE_DATA_DIR": tmp_path / "reference_data",
        "SCORING_DIR": tmp_path / "scoring",
        "EXTRACTED_DIR": tmp_path / "extracted",
    }
    for name, value in roots.items():
        monkeypatch.setattr(scoring, name, value)
        value.mkdir(parents=True, exist_ok=True)
    return roots


# ── The layout that was broken ─────────────────────────────────────────────

def test_a_mask_beside_the_maps_is_found(organiser) -> None:
    """The layout that always worked, kept as the control."""
    reference = organiser["REFERENCE_DATA_DIR"] / "asl"
    write(reference / "maps" / "Perfmap.nii.gz", cbf_map(1.05))
    write(reference / "masks" / "gm_mask.nii.gz", gm_mask())

    masks = scoring.masks_for_submission("any-submission", "asl")
    assert [m["name"] for m in masks] == ["gm_mask.nii.gz"]


def test_a_mask_in_a_different_root_from_the_maps_is_found(organiser) -> None:
    """The reported bug: shared masks, per-challenge ground truth."""
    write(organiser["REFERENCE_DATA_DIR"] / "masks" / "gm_mask.nii.gz", gm_mask())
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz",
          cbf_map(1.05))

    masks = scoring.masks_for_submission("any-submission", "asl")
    assert [m["name"] for m in masks] == ["gm_mask.nii.gz"], (
        "a mask outside the map root was not discovered"
    )


def test_masks_from_several_roots_are_combined(organiser) -> None:
    """An organiser may build up the mask set in more than one place."""
    write(organiser["REFERENCE_DATA_DIR"] / "masks" / "gm_mask.nii.gz", gm_mask())
    write(organiser["REFERENCE_DATA_DIR"] / "masks" / "wm_mask.nii.gz", gm_mask())
    write(organiser["SCORING_DIR"] / "reference" / "masks" / "lesion_roi.nii.gz",
          gm_mask())
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz",
          cbf_map())

    names = {m["name"] for m in scoring.masks_for_submission("s", "asl")}
    assert names == {"gm_mask.nii.gz", "wm_mask.nii.gz", "lesion_roi.nii.gz"}


def test_a_mask_shipped_inside_the_submission_is_found(organiser) -> None:
    """Reviewers testing locally often drop a mask in the submission itself."""
    root = organiser["EXTRACTED_DIR"] / "sub-1"
    write(root / "reference" / "masks" / "gm_mask.nii.gz", gm_mask())

    masks = scoring.masks_for_submission("sub-1", "asl")
    assert [m["name"] for m in masks] == ["gm_mask.nii.gz"]


def test_the_same_mask_seen_through_two_roots_is_counted_once(organiser) -> None:
    """Overlapping roots must not produce duplicate ROI rows."""
    shared = organiser["REFERENCE_DATA_DIR"] / "masks" / "gm_mask.nii.gz"
    write(shared, gm_mask())
    nested = organiser["REFERENCE_DATA_DIR"] / "asl" / "masks" / "gm_mask.nii.gz"
    nested.parent.mkdir(parents=True, exist_ok=True)
    try:
        nested.hardlink_to(shared)
    except (OSError, AttributeError):  # pragma: no cover - platform dependent
        pytest.skip("filesystem does not support hard links")

    masks = scoring.masks_for_submission("s", "asl")
    assert len(masks) == 1, "the same physical mask was discovered twice"


def test_masks_carry_the_label_the_report_shows(organiser) -> None:
    """`gm_mask.nii.gz` has to read as "gray matter" in a table of results."""
    base = organiser["REFERENCE_DATA_DIR"] / "masks"
    write(base / "gm_mask.nii.gz", gm_mask())
    write(base / "wm_mask.nii.gz", gm_mask())
    write(base / "lesion_roi.nii.gz", gm_mask())

    labels = {m["name"]: m["label"] for m in scoring.masks_for_submission("s", "asl")}
    assert labels["gm_mask.nii.gz"] == "gray matter"
    assert labels["wm_mask.nii.gz"] == "white matter"
    assert labels["lesion_roi.nii.gz"] == "lesion"


def test_no_masks_anywhere_is_reported_not_guessed(organiser) -> None:
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz",
          cbf_map())
    assert scoring.masks_for_submission("s", "asl") == []


# ── Through reference scoring, where the numbers are produced ──────────────

def _submitted(path: Path, values) -> dict:
    write(path, values)
    return {
        "file_name": path.name,
        "path": str(path),
        "detected_map_type": "CBF",
        "parameter_label": "Cerebral blood flow",
    }


def test_a_shared_mask_produces_per_region_metrics(organiser) -> None:
    """The whole point: bias and CoV *inside* the region, not just whole-image.

    The mask sits in the shared root and the ground truth in the challenge
    root, the layout that previously yielded whole-image metrics only.
    """
    truth = cbf_map()
    write(organiser["REFERENCE_DATA_DIR"] / "masks" / "gm_mask.nii.gz", gm_mask())
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz", truth)

    submitted_dir = organiser["EXTRACTED_DIR"] / "sub-1"
    submitted = _submitted(submitted_dir / "Perfmap.nii.gz", truth + 4.0)

    result = scoring._score_reference_maps("sub-1", "asl", [submitted])

    assert result["masks_available"] is True
    assert result["mask_count"] == 1
    (row,) = result["maps"]
    assert row["status"] == "compared"

    (mask_row,) = row["masks"]
    assert mask_row["mask_label"] == "gray matter"
    assert mask_row["status"] == "compared"

    metrics = mask_row["metrics"]
    # A constant +4 offset everywhere: the region sees the same bias as the
    # whole image, and far fewer voxels. Both facts have to be reported.
    assert metrics["bias"] == pytest.approx(4.0, abs=1e-4)
    assert metrics["rmse"] == pytest.approx(4.0, abs=1e-4)
    assert metrics["voxel_count"] == int(gm_mask().sum())
    assert metrics["voxel_count"] < row["whole_map"]["voxel_count"]


def test_the_region_is_where_the_difference_actually_is(organiser) -> None:
    """A mask that does nothing would pass the test above; this one it cannot.

    The error is confined to the masked region, so whole-image bias is diluted
    by the untouched background while the ROI reports the real magnitude. If
    the mask were being ignored the two would be equal.
    """
    truth = cbf_map()
    mask = gm_mask()
    submitted_values = truth + mask * 10.0

    write(organiser["REFERENCE_DATA_DIR"] / "masks" / "gm_mask.nii.gz", mask)
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz", truth)
    submitted = _submitted(
        organiser["EXTRACTED_DIR"] / "sub-1" / "Perfmap.nii.gz", submitted_values,
    )

    (row,) = scoring._score_reference_maps("sub-1", "asl", [submitted])["maps"]
    whole_bias = row["whole_map"]["bias"]
    roi_bias = row["masks"][0]["metrics"]["bias"]

    assert roi_bias == pytest.approx(10.0, abs=1e-4)
    assert whole_bias < roi_bias, (
        "region bias equals whole-image bias, so the mask was not applied"
    )


def test_every_mask_gets_its_own_row(organiser) -> None:
    """Lena asked for GM, WM, lesion and "any other mask I provide"."""
    truth = cbf_map()
    base = organiser["REFERENCE_DATA_DIR"] / "masks"
    for name in ("gm_mask.nii.gz", "wm_mask.nii.gz", "lesion_roi.nii.gz"):
        write(base / name, gm_mask())
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz", truth)
    submitted = _submitted(
        organiser["EXTRACTED_DIR"] / "sub-1" / "Perfmap.nii.gz", truth + 1.0,
    )

    (row,) = scoring._score_reference_maps("sub-1", "asl", [submitted])["maps"]
    assert {m["mask_label"] for m in row["masks"]} == {
        "gray matter", "white matter", "lesion",
    }
    assert all(m["status"] == "compared" for m in row["masks"])


def test_a_mask_on_a_different_grid_says_so_rather_than_silently_skipping(
    organiser,
) -> None:
    """Wrong geometry must be a reported status, not an absent row.

    This is the case the orientation QC exists for: a reviewer needs to see
    that their mask did not line up, not an empty region column.
    """
    truth = cbf_map()
    wrong_shape = np.ones((4, 4, 2), dtype=np.float32)
    write(organiser["REFERENCE_DATA_DIR"] / "masks" / "gm_mask.nii.gz", wrong_shape)
    write(organiser["REFERENCE_DATA_DIR"] / "asl" / "maps" / "Perfmap.nii.gz", truth)
    submitted = _submitted(
        organiser["EXTRACTED_DIR"] / "sub-1" / "Perfmap.nii.gz", truth + 1.0,
    )

    (row,) = scoring._score_reference_maps("sub-1", "asl", [submitted])["maps"]
    (mask_row,) = row["masks"]
    assert mask_row["status"] == "shape_mismatch"
    assert mask_row["metrics"] is None
    assert "does not match" in mask_row["error"]


def test_mask_locations_are_not_exposed_to_the_browser() -> None:
    """Where the organiser keeps their masks is as private as the masks."""
    import main

    assert "mask_roots" in main._PRIVATE_SCORING_FIELDS
    assert "mask_path" in main._PRIVATE_SCORING_FIELDS
    scrubbed = main._public_scoring_result(
        {"mask_roots": ["/srv/private/masks"],
         "maps": [{"masks": [{"mask_path": "/srv/private/masks/gm.nii.gz",
                              "mask_label": "gray matter"}]}]}
    )
    assert "mask_roots" not in scrubbed
    assert "mask_path" not in scrubbed["maps"][0]["masks"][0]
    assert scrubbed["maps"][0]["masks"][0]["mask_label"] == "gray matter"
