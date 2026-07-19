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
from typing import Iterable, Optional

from services.ingest_service import make_safe_id
from services.path_config import EXTRACTED_DIR, OUTPUTS_DIR
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
PREVIEW_ROOT = OUTPUTS_DIR / "previews"
MANIFEST_NAME = "preview_manifest.json"

_PRIVATE_PATH_PARTS = private_path_parts()
_MASK_NAME_PATTERNS = mask_name_patterns()


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


def _safe_stat(stats: dict, key: str):
    return _json_float(stats.get(key))


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
    try:
        import nibabel as nib  # type: ignore

        img = nib.load(str(path))
        shape = list(img.shape or [])
        dtype = str(img.get_data_dtype())
        voxel_size = [float(v) for v in img.header.get_zooms()[: min(3, len(img.shape or []))]]
    except Exception:
        pass
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
        "dtype": dtype,
        "finite_percent": None,
        "negative_percent": None,
        "mean": None,
        "std": None,
        "source_mtime": source_mtime,
        "source_size": source_size,
        "preview_config_fingerprint": config_fingerprint(),
    }
    item.update(_urls(submission_id, map_id, False))
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
    maps = [_reuse_or_generate(safe_id, path, cached_by_source) for path in paths]
    manifest = {
        "submission_id": safe_id,
        "challenge_type": challenge_type,
        "preview_count": len(maps),
        "available_count": sum(1 for item in maps if item.get("preview_available")),
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
    hidden = {"source_path", "source_mtime", "source_size", "submission_id"}
    return {key: value for key, value in item.items() if key not in hidden}


def public_preview_manifest(manifest: dict) -> dict:
    return {
        **{key: value for key, value in manifest.items() if key != "maps"},
        "maps": [public_preview_item(item) for item in manifest.get("maps", [])],
    }


def iter_manifest_items(submission_id: str) -> Iterable[dict]:
    manifest = _read_manifest(submission_id) or {}
    for item in manifest.get("maps", []):
        if isinstance(item, dict):
            yield item
