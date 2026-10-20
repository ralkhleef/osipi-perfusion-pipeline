"""Mask overlays, and the fact that masks are private."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
nib = pytest.importorskip("nibabel")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from services import nifti_preview_service as preview  # noqa: E402


MASK_FILENAME = "gm_mask.nii.gz"


@pytest.fixture()
def overlaid(tmp_path):
    """One parameter map with one geometrically matching mask, overlaid."""
    affine = np.diag([1.0, 1.0, 1.0, 1.0])
    shape = (6, 6, 6)

    map_path = tmp_path / "submission_cbf.nii.gz"
    nib.save(nib.Nifti1Image(np.random.rand(*shape).astype(np.float32), affine), map_path)

    mask = np.zeros(shape, dtype=np.uint8)
    mask[2:4, 2:4, 2:4] = 1
    mask_path = tmp_path / MASK_FILENAME
    nib.save(nib.Nifti1Image(mask, affine), mask_path)

    item = {
        "map_id": "submission-cbf-test",
        "source_path": str(map_path),
        "is_parameter_map": True,
    }
    return preview._attach_mask_overlay(item, "SUB", [mask_path]), mask_path


# ── It has to work at all ─────────────────────────────────────────────────

def test_a_matching_mask_produces_an_overlay(overlaid):
    item, _ = overlaid
    assert item["mask_overlay_status"] == "available"
    assert len(item["mask_overlays"]) == 1


def test_the_overlay_is_labelled_the_way_the_tables_are(overlaid):
    """A reviewer has to be able to match an overlay to a row.

    The label used to be derived from the filename, so an overlay read
    "gm mask" beside a table row reading "gray matter".
    """
    item, _ = overlaid
    assert item["mask_overlays"][0]["label"] == "gray matter"


def _overlay_with_mask(tmp_path, mask_affine, mask_shape=(6, 6, 6), name="other_mask.nii.gz"):
    map_affine = np.diag([1.0, 1.0, 1.0, 1.0])
    map_path = tmp_path / "map.nii.gz"
    nib.save(nib.Nifti1Image(np.random.rand(6, 6, 6).astype(np.float32), map_affine), map_path)

    mask_path = tmp_path / name
    nib.save(nib.Nifti1Image(np.ones(mask_shape, dtype=np.uint8), mask_affine), mask_path)

    return preview._attach_mask_overlay(
        {"map_id": "m", "source_path": str(map_path), "is_parameter_map": True},
        "SUB", [mask_path])


def test_a_mask_of_the_wrong_shape_is_refused(tmp_path):
    item = _overlay_with_mask(tmp_path, np.diag([1.0, 1.0, 1.0, 1.0]), mask_shape=(8, 8, 8))
    assert item["mask_overlay_status"] == "no_compatible_mask"
    assert item["mask_overlays"] == []


@pytest.mark.parametrize("label,affine", [
    ("left-right flipped", np.diag([-1.0, 1.0, 1.0, 1.0])),
    ("wrong voxel size", np.diag([2.0, 2.0, 2.0, 1.0])),
    ("translated", np.array([[1.0, 0, 0, 30.0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]])),
])
def test_a_mask_that_shares_the_shape_but_not_the_grid_is_refused(tmp_path, label, affine):
    """The case a shape check cannot see, and the reason the overlay exists."""
    item = _overlay_with_mask(tmp_path, affine)
    assert item["mask_overlay_status"] == "no_compatible_mask", \
        f"a {label} mask was drawn as if it aligned"
    assert item["mask_overlays"] == []


def test_a_stale_overlay_does_not_survive_the_mask_being_removed(overlaid):
    """A cached item must not keep showing an overlay for a deleted mask."""
    item, _ = overlaid
    refreshed = preview._attach_mask_overlay(item, "SUB", [])
    assert refreshed["mask_overlays"] == []
    assert refreshed["mask_overlay_url"] is None
    assert refreshed["mask_overlay_status"] == "mask_not_available"


# ── Privacy ───────────────────────────────────────────────────────────────

def test_no_mask_filename_or_path_reaches_the_client(overlaid):
    """The browser gets a picture and a label, never an organiser filename."""
    item, mask_path = overlaid

    sent = repr({key: item[key] for key in
                 ("mask_overlays", "mask_overlay_url", "mask_overlay_label",
                  "mask_overlay_status")})

    assert MASK_FILENAME not in sent, "the mask filename is exposed"
    assert str(mask_path) not in sent, "the mask path is exposed"

    for fragment in ("gm-mask", "gm_mask", "gm mask"):
        assert fragment not in sent, f"the mask filename leaks as {fragment!r}"


def test_the_plane_id_is_a_digest_not_a_reversible_name(overlaid):
    """Two different masks must get different ids without naming either."""
    item, _ = overlaid
    plane = item["mask_overlays"][0]["plane"]
    assert plane.startswith(preview.MASK_OVERLAY_PLANE)
    assert plane != preview.MASK_OVERLAY_PLANE


def test_overlays_are_written_only_under_the_ignored_outputs_directory(overlaid):
    """Rendered private data must land somewhere git will not take it."""
    item, _ = overlaid
    plane = item["mask_overlays"][0]["plane"]
    written = preview._mask_overlay_path("SUB", item["map_id"], plane)

    assert preview.PREVIEW_ROOT in written.parents, \
        f"overlay written outside the previews root: {written}"
    assert "outputs" in {part.lower() for part in preview.PREVIEW_ROOT.parts}


def test_the_repository_ignores_everything_overlays_are_written_into() -> None:
    """The guarantee above is only worth anything while this holds."""
    import shutil
    import subprocess

    if not shutil.which("git") or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")

    probe = "data/outputs/previews/SUB/any-overlay.png"
    result = subprocess.run(["git", "check-ignore", probe],
                            cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, f"{probe} is not gitignored, a rendered mask could be committed"


def test_no_report_embeds_a_mask_overlay() -> None:
    """Reports get circulated. Overlays are for the interactive app only."""
    for module in ("backend/main.py", "backend/services/pdf_report_service.py"):
        source = (ROOT / module).read_text()
        assert "mask_overlay" not in source, f"{module} references a mask overlay"


def test_the_published_example_bundle_carries_no_rendered_imagery() -> None:
    """Whatever ships in docs/ is public. Nothing rendered belongs there."""
    downloads = ROOT / "docs" / "downloads"
    if not downloads.is_dir():
        pytest.skip("no downloads directory")

    images = [p.name for p in downloads.rglob("*")
              if p.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".nii", ".gz"}]
    assert not images, f"rendered or imaging files published in docs/downloads: {images}"


def test_plain_previews_never_render_a_mask_on_its_own(tmp_path) -> None:
    """A mask must not be previewable as an image in its own right."""
    base = tmp_path / "sub"
    (base / "reference" / "masks").mkdir(parents=True)
    mask = base / "reference" / "masks" / MASK_FILENAME
    mask.write_bytes(b"not really a nifti")

    assert preview._is_private_or_mask_path(mask, base) is True
