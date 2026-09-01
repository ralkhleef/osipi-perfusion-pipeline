"""Cached lightweight PNG previews for submitted/result NIfTI maps."""

from __future__ import annotations

import binascii
import hashlib
import json
import math
import re
import struct
import zlib
from pathlib import Path
from typing import Optional

from services.ingest_service import make_safe_id
from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR, REFERENCE_DATA_DIR
from scoring import _detect_map_type, _safe_name
from osipi_pipeline.ingestion.manifest import config_fingerprint, manifest_files
from osipi_pipeline.config.rules import (
    challenge_types,
    mask_name_patterns,
    output_map_subpaths,
    private_path_parts,
    tuple_setting,
)

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")
PREVIEW_PLANES = ("axial", "coronal", "sagittal")
MASK_OVERLAY_PLANE = "mask-overlay"
PREVIEW_ROOT = OUTPUTS_DIR / "previews"
MANIFEST_NAME = "preview_manifest.json"
PREVIEW_SCHEMA_VERSION = 2

_PRIVATE_PATH_PARTS = private_path_parts()
_MASK_NAME_PATTERNS = mask_name_patterns()


def _mask_display_label(mask_filename: str) -> str:
    """The label the scoring tables give this mask.

    Imported lazily: this module is imported by ``backend.scoring`` in some
    paths, and a module-level import back the other way would be circular.

    Falls back to a tidied filename only if scoring cannot be reached, and
    even then the fallback is a name, never a path.
    """
    try:
        from scoring import _mask_label_for_name  # noqa: PLC0415

        label = _mask_label_for_name(mask_filename)
        if label:
            return str(label)
    except Exception:
        pass
    return _strip_nifti_suffix(mask_filename).replace("_", " ")


def _is_nifti(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and any(name.endswith(suffix) for suffix in NIFTI_SUFFIXES)


def _is_private_or_mask_path(path: Path, base: Path) -> bool:
    try:
        rel = path.relative_to(base)
    except ValueError:
        rel = path
    parts = {part.lower() for part in rel.parts}
    if parts.intersection(_PRIVATE_PATH_PARTS):
        return True
    name = path.name.lower()
    return any(pattern in name for pattern in _MASK_NAME_PATTERNS)


def _exec_output_dir(submission_id: str, challenge_type: str) -> Path:
    key = f"{_safe_name(challenge_type)}_{_safe_name(submission_id)}"
    return OUTPUTS_DIR / "execution" / key / "outputs"


def _collect_niftis(base: Path, scan_root: Path) -> list[Path]:
    if not scan_root.exists():
        return []
    root = base if base.exists() else scan_root
    files = manifest_files(root, refresh_if_stale=True, submission_id=root.name)
    return sorted(
        path for path in files
        if _is_nifti(path)
        and _is_under(path, scan_root)
        and not _is_private_or_mask_path(path, base)
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _candidate_nifti_paths(submission_id: str, challenge_type: Optional[str]) -> list[Path]:
    """Return submitted/result map paths only, excluding reference/mask data."""
    safe_id = make_safe_id(submission_id)
    challenges = [challenge_type] if challenge_type else [*challenge_types(), "other"]
    candidates: list[Path] = []

    for challenge in challenges:
        if not challenge:
            continue
        exec_dir = _exec_output_dir(safe_id, challenge)
        candidates.extend(_collect_niftis(exec_dir, exec_dir))

    extracted_base = EXTRACTED_DIR / safe_id
    for subpath in output_map_subpaths():
        scan_root = extracted_base / subpath if subpath else extracted_base
        found = _collect_niftis(extracted_base, scan_root)
        if found:
            candidates.extend(found)
            break

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return unique


def _preview_dir(submission_id: str) -> Path:
    path = PREVIEW_ROOT / make_safe_id(submission_id)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _manifest_path(submission_id: str) -> Path:
    return _preview_dir(submission_id) / MANIFEST_NAME


def _strip_nifti_suffix(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".nii.gz"):
        return name[:-7]
    if lower.endswith(".nii"):
        return name[:-4]
    return Path(name).stem


def _map_id_for_path(path: Path) -> str:
    stem = re.sub(r"[^a-zA-Z0-9]+", "-", _strip_nifti_suffix(path.name)).strip("-").lower()
    digest = hashlib.sha1(str(path.resolve()).encode("utf-8")).hexdigest()[:10]
    return f"{stem or 'map'}-{digest}"


def _json_float(value, ndigits: int = 6):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, ndigits)




def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack("!I", len(data))
        + tag
        + data
        + struct.pack("!I", binascii.crc32(tag + data) & 0xFFFFFFFF)
    )


def _upscale_nearest(image, min_edge: int = 220):
    import numpy as np  # type: ignore

    height, width = image.shape
    if height <= 0 or width <= 0:
        return image
    scale = max(1, int(math.ceil(min_edge / max(height, width))))
    if scale <= 1:
        return image
    return np.repeat(np.repeat(image, scale, axis=0), scale, axis=1)


def _write_png_gray(path: Path, image) -> None:
    """Write an 8-bit grayscale PNG without requiring Pillow/imageio."""
    import numpy as np  # type: ignore

    arr = np.asarray(image, dtype=np.uint8)
    if arr.ndim != 2:
        raise ValueError("PNG preview image must be 2D")
    arr = _upscale_nearest(arr)
    height, width = arr.shape
    raw = b"".join(b"\x00" + arr[row].tobytes() for row in range(height))
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


def _write_png_rgb(path: Path, image) -> None:
    """Write an 8-bit RGB PNG without adding an image-library dependency."""
    import numpy as np  # type: ignore

    arr = np.asarray(image, dtype=np.uint8)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError("RGB preview image must have shape (height, width, 3)")
    channels = [_upscale_nearest(arr[:, :, index]) for index in range(3)]
    arr = np.stack(channels, axis=2)
    height, width, _ = arr.shape
    raw = b"".join(b"\x00" + arr[row].tobytes() for row in range(height))
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack("!IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )
    path.write_bytes(payload)


def _slice_for_plane(data, plane: str):
    import numpy as np  # type: ignore

    if plane == "axial":
        idx = data.shape[2] // 2
        image = data[:, :, idx]
    elif plane == "coronal":
        idx = data.shape[1] // 2
        image = data[:, idx, :]
    elif plane == "sagittal":
        idx = data.shape[0] // 2
        image = data[idx, :, :]
    else:
        raise ValueError(f"Unknown preview plane: {plane}")
    return np.rot90(np.asarray(image, dtype=float))


def _normalize_slice(slice_data, whole_finite_values):
    import numpy as np  # type: ignore

    arr = np.asarray(slice_data, dtype=float)
    finite_slice = arr[np.isfinite(arr)]
    values = finite_slice if finite_slice.size else whole_finite_values
    if values.size == 0:
        raise ValueError("No finite voxels available for preview.")

    low = float(np.percentile(values, 2))
    high = float(np.percentile(values, 98))
    if not math.isfinite(low) or not math.isfinite(high) or high <= low:
        finite = np.isfinite(arr)
        if not finite.any():
            raise ValueError("No finite voxels available in preview slice.")
        return np.where(finite, 128, 0).astype(np.uint8)

    scaled = (arr - low) / (high - low)
    scaled = np.clip(scaled, 0, 1)
    scaled[~np.isfinite(scaled)] = 0
    return (scaled * 255).astype(np.uint8)


def _load_preview_volume(path: Path):
    import nibabel as nib  # type: ignore
    import numpy as np  # type: ignore

    img = nib.load(str(path))
    data = np.asarray(img.dataobj, dtype=np.float32)
    volume_index = None
    if data.ndim < 3:
        raise ValueError(f"NIfTI preview requires at least 3 dimensions; found shape {list(data.shape)}.")
    if data.ndim > 3:
        volume_index = int(data.shape[3] // 2)
        data = data[:, :, :, volume_index]
    if data.ndim != 3:
        raise ValueError(f"Preview volume must be 3D after selection; found shape {list(data.shape)}.")
    finite_values = data[np.isfinite(data)]
    if finite_values.size == 0:
        raise ValueError("No finite voxels available for preview.")
    return data, finite_values, volume_index


def _preview_png_paths(submission_id: str, map_id: str) -> dict[str, Path]:
    folder = _preview_dir(submission_id)
    return {plane: folder / f"{map_id}_{plane}.png" for plane in PREVIEW_PLANES}


def _urls(submission_id: str, map_id: str, available: bool) -> dict:
    base = f"/api/submissions/{make_safe_id(submission_id)}/previews/{map_id}"
    axial = f"{base}/axial.png" if available else None
    return {
        "thumbnail_url": axial,
        "axial_url": axial,
        "coronal_url": f"{base}/coronal.png" if available else None,
        "sagittal_url": f"{base}/sagittal.png" if available else None,
        "download_url": f"/api/submissions/{make_safe_id(submission_id)}/maps/{map_id}/download",
        "full_preview_url": f"/preview/{make_safe_id(submission_id)}/{map_id}",
    }


def _cached_item_is_valid(item: dict) -> bool:
    source = Path(item.get("source_path") or "")
    if not source.exists():
        return False
    try:
        stat = source.stat()
    except OSError:
        return False
    if item.get("source_mtime") != stat.st_mtime or item.get("source_size") != stat.st_size:
        return False
    if item.get("preview_config_fingerprint") != config_fingerprint():
        return False
    if item.get("preview_schema_version") != PREVIEW_SCHEMA_VERSION:
        return False
    if item.get("preview_available"):
        paths = _preview_png_paths(item.get("submission_id") or "", item.get("map_id") or "")
        return all(path.exists() for path in paths.values())
    return True


def _read_manifest(submission_id: str) -> Optional[dict]:
    path = _manifest_path(submission_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    return data


def _write_manifest(submission_id: str, manifest: dict) -> None:
    _manifest_path(submission_id).write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _base_preview_item(submission_id: str, path: Path) -> dict:
    map_info = _detect_map_type(path)
    map_id = _map_id_for_path(path)
    shape = []
    dtype = None
    voxel_size = []
    orientation = None
    read_error = None
    try:
        import nibabel as nib  # type: ignore

        img = nib.load(str(path))
        shape = list(img.shape or [])
        dtype = str(img.get_data_dtype())
        voxel_size = [float(v) for v in img.header.get_zooms()[: min(3, len(img.shape or []))]]
        orientation = "".join(code or "?" for code in nib.aff2axcodes(img.affine))
    except Exception as exc:
        # Record why, rather than returning empty fields. A file that cannot be
        # read used to be indistinguishable from one whose header happened to
        # be blank: both arrived with no shape, no orientation and no dtype. The
        # header check then reported "Not verified", which reads as "we did not
        # look" when the truth is "we looked and the file is broken".
        read_error = str(exc) or exc.__class__.__name__
    try:
        stat = path.stat()
        source_mtime = stat.st_mtime
        source_size = stat.st_size
    except OSError:
        source_mtime = None
        source_size = None
    item = {
        "submission_id": make_safe_id(submission_id),
        "map_id": map_id,
        "file_name": path.name,
        "source_path": str(path),
        "detected_map_type": map_info.get("detected_map_type") or "Unknown",
        "parameter_label": map_info.get("parameter_label"),
        "units": map_info.get("units"),
        "shape": shape,
        "voxel_size": voxel_size,
        "orientation": orientation,
        "dtype": dtype,
        # None when the header was read. A string when it could not be, so
        # every consumer can tell "no header" apart from "unreadable file".
        "read_error": read_error,
        "finite_percent": None,
        "negative_percent": None,
        "mean": None,
        "std": None,
        "source_mtime": source_mtime,
        "source_size": source_size,
        "preview_config_fingerprint": config_fingerprint(),
        "preview_schema_version": PREVIEW_SCHEMA_VERSION,
    }
    item.update(_urls(submission_id, map_id, False))
    item.update({
        "mask_overlay_url": None,
        "mask_overlay_label": None,
        "mask_overlay_status": "mask_not_available",
    })
    return item


def _mask_candidates(submission_id: str, challenge_type: Optional[str]) -> list[Path]:
    roots = [
        EXTRACTED_DIR / make_safe_id(submission_id) / "reference" / "masks",
        REFERENCE_DATA_DIR / str(challenge_type or "").lower() / "masks",
        REFERENCE_DATA_DIR / "masks",
    ]
    candidates: list[Path] = []
    seen: set[Path] = set()
    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if _is_nifti(path) and path.resolve() not in seen:
                seen.add(path.resolve())
                candidates.append(path)
    return candidates


def _mask_overlay_plane(mask_path: Path) -> str:
    """A stable id for one mask's overlay, carrying no part of its name.

    The id used to include the filename stem, so the overlay URL read
    ``mask-overlay-gm-mask-2bb5715a`` and the mask filename reached the
    browser, the address bar and every access log. A mask filename is
    organiser information: it says which regions exist and what they are
    called, which is part of what the challenge holds back.

    The digest alone distinguishes one mask from another, which is all the
    id has to do. It is longer than it needs to be for the handful of masks
    a challenge defines, because a collision would silently show the wrong
    region.
    """
    digest = hashlib.sha256(str(mask_path.resolve()).encode("utf-8")).hexdigest()[:16]
    return f"{MASK_OVERLAY_PLANE}-{digest}"


def _mask_overlay_path(submission_id: str, map_id: str, plane: str = MASK_OVERLAY_PLANE) -> Path:
    if plane != MASK_OVERLAY_PLANE and not plane.startswith(f"{MASK_OVERLAY_PLANE}-"):
        raise ValueError("Unknown mask overlay plane.")
    return _preview_dir(submission_id) / f"{map_id}_{plane}.png"


def _attach_mask_overlay(
    item: dict, submission_id: str, mask_paths: list[Path]
) -> dict:
    """Create private-safe middle axial slices for every compatible mask.

    A mask is compatible only when shape, affine/orientation, and voxel sizes
    agree within the same tolerances used for scoring. No mask path or raw mask
    data is returned to the browser.
    """
    # A cached map item must not retain an overlay after masks are removed or
    # replaced with incompatible geometry.
    item["mask_overlay_url"] = None
    item["mask_overlay_label"] = None
    item["mask_overlays"] = []
    item["mask_overlay_status"] = "mask_not_available"
    item.pop("mask_overlay_error", None)
    if not item.get("is_parameter_map") or not mask_paths:
        return item
    try:
        import nibabel as nib  # type: ignore
        import numpy as np  # type: ignore

        map_img = nib.load(str(item.get("source_path") or ""))
        map_data = np.asarray(map_img.dataobj, dtype=np.float32)
        if map_data.ndim != 3:
            return item
        for mask_path in mask_paths:
            mask_img = nib.load(str(mask_path))
            mask_data = np.asarray(mask_img.dataobj, dtype=np.float32)
            if mask_data.shape != map_data.shape:
                continue
            if not np.allclose(mask_img.affine, map_img.affine, rtol=0, atol=1e-2):
                continue
            map_slice = _slice_for_plane(map_data, "axial")
            mask_slice = _slice_for_plane(mask_data, "axial")
            finite = map_data[np.isfinite(map_data)]
            gray = _normalize_slice(map_slice, finite)
            rgb = np.stack([gray, gray, gray], axis=2).astype(np.float32)
            inside = np.isfinite(mask_slice) & (mask_slice != 0)
            # Magenta tint keeps anatomy visible while making alignment clear.
            rgb[inside, 0] = 0.75 * 255 + 0.25 * rgb[inside, 0]
            rgb[inside, 1] = 0.25 * rgb[inside, 1]
            rgb[inside, 2] = 0.75 * 255 + 0.25 * rgb[inside, 2]
            plane = _mask_overlay_plane(mask_path)
            _write_png_rgb(
                _mask_overlay_path(submission_id, item["map_id"], plane), rgb.astype(np.uint8)
            )
            url = (
                f"/api/submissions/{make_safe_id(submission_id)}/previews/"
                f"{item['map_id']}/{plane}.png"
            )
            # The same label the scoring tables use, so a reviewer can match
            # an overlay to its row. Deriving it from the filename instead
            # gave "gm mask" beside a table row reading "gray matter", and
            # the filename is an organiser asset name that should not be on
            # screen at all.
            label = _mask_display_label(mask_path.name)
            item["mask_overlays"].append({"plane": plane, "label": label, "url": url})
        if item["mask_overlays"]:
            first = item["mask_overlays"][0]
            item["mask_overlay_url"] = first["url"]
            item["mask_overlay_label"] = first["label"]
            item["mask_overlay_status"] = "available"
        else:
            item["mask_overlay_status"] = "no_compatible_mask"
    except Exception as exc:
        item["mask_overlay_status"] = "unavailable"
        item["mask_overlay_error"] = str(exc)
    return item


def _generate_preview_item(submission_id: str, path: Path) -> dict:
    item = _base_preview_item(submission_id, path)
    map_id = item["map_id"]
    png_paths = _preview_png_paths(submission_id, map_id)

    try:
        data, finite_values, volume_index = _load_preview_volume(path)
        _apply_preview_stats(item, data, finite_values)
        for plane in PREVIEW_PLANES:
            plane_slice = _slice_for_plane(data, plane)
            normalized = _normalize_slice(plane_slice, finite_values)
            _write_png_gray(png_paths[plane], normalized)

        item.update({
            "preview_available": True,
            "preview_status": "preview_available",
            "preview_error": "",
            "preview_volume_index": volume_index,
        })
        item.update(_urls(submission_id, map_id, True))
    except Exception as exc:
        item.update({
            "preview_available": False,
            "preview_status": "preview_unavailable",
            "preview_error": str(exc),
            "preview_volume_index": None,
        })
        item.update(_urls(submission_id, map_id, False))
    return item


def _apply_preview_stats(item: dict, data, finite_values) -> None:
    import numpy as np  # type: ignore

    total = int(data.size)
    finite_count = int(finite_values.size)
    negative_count = int(np.sum(finite_values < 0)) if finite_count else 0
    item["finite_percent"] = _json_float((finite_count / total) * 100.0) if total else None
    item["negative_percent"] = _json_float((negative_count / finite_count) * 100.0) if finite_count else None
    item["mean"] = _json_float(np.mean(finite_values, dtype=np.float64)) if finite_count else None
    item["std"] = _json_float(np.std(finite_values, dtype=np.float64)) if finite_count else None


def _reuse_or_generate(submission_id: str, path: Path, cached_by_source: dict[str, dict]) -> dict:
    source_key = str(path)
    cached = cached_by_source.get(source_key)
    if cached and _cached_item_is_valid(cached):
        return cached
    return _generate_preview_item(submission_id, path)


_UNRECOGNIZED_MAP_TYPES = {"", "unknown", "mixed/other"}


def _classify_preview_role(item: dict) -> dict:
    """Tag a preview item with its role so galleries can show only 3-D parameter maps.

    A file is a scored parameter map when it is exactly 3-D and has a recognized
    configured map type (CBF/Perfmap, ATT, …). 4-D files are ASL/model/time-series
    data (kept for download, never scored as a parameter map); anything else is
    an unrecognized submitted file. This is display metadata only, it does not
    change ingestion, validation, or scoring.
    """
    shape = [d for d in (item.get("shape") or []) if d]
    ndim = len(shape)
    map_type = str(item.get("detected_map_type") or "").strip()
    recognized = map_type.lower() not in _UNRECOGNIZED_MAP_TYPES
    if ndim == 3 and recognized:
        item["file_role"] = "parameter_map"
        item["is_parameter_map"] = True
        item["role_label"] = map_type
    elif ndim >= 4:
        item["file_role"] = "fitted_model"
        item["is_parameter_map"] = False
        item["role_label"] = "4D ASL data"
    else:
        item["file_role"] = "unknown"
        item["is_parameter_map"] = False
        item["role_label"] = "Other submitted file"
    return item


def list_submission_previews(submission_id: str, challenge_type: Optional[str] = None) -> dict:
    """Return a manifest for safely previewable submitted/result NIfTI maps."""
    safe_id = make_safe_id(submission_id)
    previous = _read_manifest(safe_id) or {}
    cached_by_source = {
        item.get("source_path"): item
        for item in previous.get("maps", [])
        if isinstance(item, dict) and item.get("source_path")
    }
    paths = _candidate_nifti_paths(safe_id, challenge_type)
    maps = [_classify_preview_role(_reuse_or_generate(safe_id, path, cached_by_source)) for path in paths]
    mask_paths = _mask_candidates(safe_id, challenge_type)
    maps = [_attach_mask_overlay(item, safe_id, mask_paths) for item in maps]
    manifest = {
        "submission_id": safe_id,
        "challenge_type": challenge_type,
        "preview_count": len(maps),
        "available_count": sum(1 for item in maps if item.get("preview_available")),
        "parameter_map_count": sum(1 for item in maps if item.get("is_parameter_map")),
        "maps": maps,
    }
    _write_manifest(safe_id, manifest)
    return manifest


def get_preview_item(submission_id: str, map_id: str) -> Optional[dict]:
    manifest = _read_manifest(submission_id)
    if not manifest:
        return None
    for item in manifest.get("maps", []):
        if isinstance(item, dict) and item.get("map_id") == map_id:
            return item
    return None


def get_preview_png_path(submission_id: str, map_id: str, plane: str) -> Path:
    if plane == MASK_OVERLAY_PLANE or plane.startswith(f"{MASK_OVERLAY_PLANE}-"):
        item = get_preview_item(submission_id, map_id)
        if not item or not item.get("mask_overlay_url"):
            raise FileNotFoundError("Mask overlay is not available.")
        if plane == MASK_OVERLAY_PLANE:
            overlays = item.get("mask_overlays") or []
            plane = overlays[0].get("plane") if overlays else MASK_OVERLAY_PLANE
        elif plane not in {overlay.get("plane") for overlay in item.get("mask_overlays", [])}:
            raise FileNotFoundError("Mask overlay is not available.")
        path = _mask_overlay_path(submission_id, map_id, plane)
        if not path.exists():
            raise FileNotFoundError("Mask overlay image is missing.")
        return path
    if plane not in PREVIEW_PLANES:
        raise ValueError("Unknown preview plane.")
    item = get_preview_item(submission_id, map_id)
    if not item or not item.get("preview_available"):
        raise FileNotFoundError("Preview is not available.")
    path = _preview_png_paths(submission_id, map_id)[plane]
    if not path.exists():
        raise FileNotFoundError("Preview image is missing.")
    return path


def get_preview_download_path(submission_id: str, map_id: str) -> Path:
    item = get_preview_item(submission_id, map_id)
    if not item:
        raise FileNotFoundError("Map is not available for download.")
    path = Path(item.get("source_path") or "")
    if not _is_nifti(path) or not path.exists():
        raise FileNotFoundError("Map is not available for download.")
    safe_id = make_safe_id(submission_id)
    resolved = path.resolve()
    extracted_base = (EXTRACTED_DIR / safe_id).resolve()
    execution_base = (OUTPUTS_DIR / "execution").resolve()
    allowed = False
    try:
        resolved.relative_to(extracted_base)
        allowed = not _is_private_or_mask_path(resolved, extracted_base)
    except ValueError:
        try:
            rel = resolved.relative_to(execution_base)
            allowed = any(safe_id in part.lower() for part in rel.parts) and not _is_private_or_mask_path(resolved, execution_base)
        except ValueError:
            allowed = False
    if not allowed:
        raise PermissionError("Map download is not allowed for this file.")
    return path


def public_preview_item(item: dict) -> dict:
    """Return preview metadata without internal filesystem paths."""
    hidden = {
        "source_path", "source_mtime", "source_size", "submission_id",
        "mask_overlay_error",
    }
    return {key: value for key, value in item.items() if key not in hidden}


def public_preview_manifest(manifest: dict) -> dict:
    return {
        **{key: value for key, value in manifest.items() if key != "maps"},
        "maps": [public_preview_item(item) for item in manifest.get("maps", [])],
    }


