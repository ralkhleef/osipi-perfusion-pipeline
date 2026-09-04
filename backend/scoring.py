"""backend/scoring.py, Scoring framework for OSIPI pipeline.

Provider system
--------------
Two providers are registered:

    osipi_tf62_dce_ktrans          [OFFICIAL]
        OSIPI Task Force 6.2 DCE-MRI Ktrans challenge scoring.
        Required directory layout under data/scoring/providers/osipi_tf62_dce_ktrans/:
            challengeScoring.py       ← official scoring script
            reference/                ← DRO / reference Ktrans NIfTI maps
            masks/                    ← binary mask NIfTI files

        If masks/ does not exist, the code falls back to searching reference/
        for any file containing "mask" in its name.

        Expected participant output filenames (from Docker execution):
            Synthetic_P<n>_Visit<n>.nii[.gz]
            Clinical_P<n>_Visit<n>.nii[.gz]

        The script uses no CLI arguments, it reads hardcoded relative paths
        (entryDirectories/, DROKtransNifti/, Masks/, scoringOutputs/) from its
        cwd.  score_submission() patches entry_list in the script source and
        runs it with cwd=provider_dir.

    osipi_codecollection_dce_testdata   [DEVELOPMENT ONLY, never runs scoring]
        CSV test data from OSIPI/DCE-DSC-MRI_CodeCollection.
        Used only to test provider-discovery UI. NOT for scoring NIfTI maps.

This module NEVER returns or fabricates metric values.
"""

from __future__ import annotations

import copy
import csv
import json
import gzip
import hashlib
import math
import os
import re
import threading
import shutil
import subprocess
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from services.path_config import (
    CODECOLLECTION_DIR,
    EXTRACTED_DIR,
    OSIPI_TF62_DIR,
    OUTPUTS_DIR,
    REFERENCE_DATA_DIR,
    SCORING_DIR,
    SCORING_OUTPUTS_DIR,
)
from services.scoring_package_service import (
    check_package_ready,
    get_active_entry,
    get_package_manifest,
    list_packages,
    run_package_scoring,
)
from osipi_pipeline.config.rules import (
    analysis_by_challenge,
    artifact_type_specs,
    grouped_statistics_by_challenge,
    icc_settings_by_challenge,
    performance_settings,
    thresholds_by_challenge,
    map_type_specs,
    mask_label_rules,
    mask_name_patterns,
    output_map_subpaths,
    private_path_parts,
    tuple_setting,
)
from osipi_pipeline.ingestion.manifest import manifest_files

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict] = {
    # ── Official: OSIPI TF6.2 DCE Ktrans ─────────────────────────────────────
    "osipi_tf62_dce_ktrans": {
        "provider_id":   "osipi_tf62_dce_ktrans",
        "display_name":  "OSIPI TF6.2 DCE Ktrans",
        # legacy key kept for API compat
        "provider_name": "OSIPI TF6.2 DCE Ktrans",
        "category":      "official",
        "official":      True,
        "challenge_type": "dce",
        "map_type":      "ktrans",
        "description":   "OSIPI Task Force 6.2 DCE-MRI Ktrans challenge scoring",
        "not_for_scoring": False,
        "metrics":       ["accuracy", "repeatability", "reproducibility",
                          "osipi_silver_score", "osipi_gold_score"],
        # Paths: derived from OSIPI_TF62_DIR
        "provider_dir":  OSIPI_TF62_DIR,
        "script_file":   OSIPI_TF62_DIR / "challengeScoring.py",
        # challengeScoring.py uses hardcoded relative paths from its cwd.
        # Directory names must match exactly (case-sensitive on Linux).
        "ref_data_dir":  OSIPI_TF62_DIR / "DROKtransNifti",
        "masks_dir":     OSIPI_TF62_DIR / "Masks",
        "setup_note": (
            "Place the following inside "
            "data/scoring/providers/osipi_tf62_dce_ktrans/ to enable scoring:\n"
            "  challengeScoring.py , from OSIPI/TF6.2_DCE-DSC-MRI_Challenges Scoring/\n"
            "  DROKtransNifti/     , DRO Ktrans NIfTI maps "
            "(additionalDROData/NIfTI/ from the same repo)\n"
            "  Masks/              , NIfTI mask files (Scoring/Masks/ from the same repo)"
        ),
    },

    # ── Development / test-data only ─────────────────────────────────────────
    "osipi_codecollection_dce_testdata": {
        "provider_id":   "osipi_codecollection_dce_testdata",
        "display_name":  "OSIPI CodeCollection Test Data",
        "provider_name": "OSIPI DCE/DSC CodeCollection, Test Data",
        "category":      "development",
        "official":      False,
        "challenge_type": "dce",
        "map_type":      None,
        "description": (
            "CSV pharmacokinetic-model test data from OSIPI/DCE-DSC-MRI_CodeCollection. "
            "For provider-discovery UI testing only. NOT official challenge scoring."
        ),
        "not_for_scoring": True,
        "metrics":       [],
        "provider_dir":  CODECOLLECTION_DIR,
        "test_data_dir": CODECOLLECTION_DIR / "test" / "DCEmodels" / "data",
        "expected_csv_files": [
            "dce_DRO_data_tofts.csv",
            "dce_DRO_data_extended_tofts.csv",
        ],
        "setup_note": (
            "Clone or copy test/DCEmodels/data/ from "
            "github.com/OSIPI/DCE-DSC-MRI_CodeCollection into "
            "data/scoring/providers/osipi_codecollection_dce/test/DCEmodels/data/"
        ),
    },
}




def get_provider(challenge_type: str, map_type: str) -> Optional[dict]:
    """Return the official provider matching challenge + map type, or None.

    Skips development-only (not_for_scoring=True) providers.
    """
    ct = (challenge_type or "").lower().strip()
    mt = (map_type or "").lower().strip()
    for p in PROVIDERS.values():
        if p.get("not_for_scoring"):
            continue
        if p.get("challenge_type") == ct and p.get("map_type") == mt:
            return p
    return None


def _resolve_provider(
    provider_id: Optional[str],
    challenge_type: str,
    map_type: str,
) -> tuple[Optional[dict], str]:
    """Return (provider_dict, error_message).

    Prefers provider_id lookup; falls back to challenge/map lookup.
    Returns (None, error) if nothing matches or provider is dev-only.
    """
    if provider_id:
        p = PROVIDERS.get(provider_id)
        if p is None:
            return None, f"Unknown provider_id: {provider_id!r}"
        if p.get("not_for_scoring"):
            return None, f"Provider {provider_id!r} is a development-only provider and cannot score submissions."
        return p, ""
    p = get_provider(challenge_type, map_type)
    if p is None:
        return None, (
            f"No official scoring provider configured for "
            f"challenge_type={challenge_type!r}, map_type={map_type!r}."
        )
    return p, ""


# ---------------------------------------------------------------------------
# Path helpers: mirror docker_runner._safe_name
# ---------------------------------------------------------------------------

def _safe_name(value: str) -> str:
    """Convert to filesystem-safe lowercase-hyphenated form."""
    safe = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in safe.split("-") if part) or "submission"


def _exec_output_dir(submission_id: str, challenge_type: str) -> Path:
    """Return the outputs/ dir written by execution_service for this submission."""
    key = f"{_safe_name(challenge_type)}_{_safe_name(submission_id)}"
    return OUTPUTS_DIR / "execution" / key / "outputs"


NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")
_PRIVATE_PATH_PARTS = private_path_parts()
_MASK_NAME_PATTERNS = mask_name_patterns()


def _is_nifti_path(path: Path) -> bool:
    name = path.name.lower()
    return path.is_file() and any(name.endswith(suffix) for suffix in NIFTI_SUFFIXES)


def _strip_nifti_suffix(name: str) -> str:
    lower = name.lower()
    for suffix in sorted(NIFTI_SUFFIXES, key=len, reverse=True):
        if lower.endswith(suffix):
            return lower[: -len(suffix)]
    return Path(lower).stem


def _is_organiser_asset(path: Path) -> bool:
    """Whether a file belongs to the organiser rather than to the submitter.

    A submission may carry its own ``reference/`` directory, and the pipeline
    reads it as a reference root. Those files are ground truth and ROI masks:
    they are not the team's answer and must never be scored as one.

    Without this check a ground-truth map is picked up as a submitted map and
    compared against itself, producing a row with bias 0, RMSE 0 and
    correlation 1. Lena's ASL data reported four maps for two submitted
    files, two of them perfect, which is what made it unclear what the
    numbers were computed from.
    """
    return bool({part.lower() for part in path.parts} & _PRIVATE_PATH_PARTS)


def _find_output_niftis(submission_id: str, challenge_type: str) -> list[Path]:
    """Return NIfTI files that represent the output maps for this submission.

    Priority order:
    1. Docker execution output dir  (OUTPUTS_DIR/execution/{key}/outputs/)
    2. Configured submitted-map subdirectories from config/settings.yaml

    Organiser assets are excluded from both, see ``_is_organiser_asset``.
    """
    # IDs are single directory names, never caller-supplied paths. Preserve
    # valid underscores/case rather than silently resolving a different ID.
    if not submission_id or submission_id in {".", ".."} or any(c in submission_id for c in ("/", "\\", "\x00")):
        return []
    extracted_base = EXTRACTED_DIR / submission_id
    if not _path_is_relative_to(extracted_base, EXTRACTED_DIR):
        return []
    exec_dir = _exec_output_dir(submission_id, challenge_type)
    if exec_dir.exists():
        niftis = [
            f for f in manifest_files(exec_dir, refresh_if_stale=True, submission_id=exec_dir.name)
            if _is_nifti_path(f) and not _is_organiser_asset(f)
        ]
        if niftis:
            return niftis

    extracted_files = manifest_files(extracted_base, refresh_if_stale=True, submission_id=submission_id)
    for subpath in output_map_subpaths():
        candidate = extracted_base / subpath if subpath else extracted_base
        if candidate.exists():
            niftis = [
                f for f in extracted_files
                if _is_nifti_path(f)
                and _path_is_relative_to(f, candidate)
                and not _is_organiser_asset(f)
            ]
            if niftis:
                return niftis

    return []


def _path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _score_artifact_dir(submission_id: str) -> Path:
    """Return the directory where scoring artifacts are stored for this submission."""
    d = SCORING_OUTPUTS_DIR / _safe_name(submission_id)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _scoring_result_path(submission_id: str) -> Path:
    SCORING_OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    return SCORING_OUTPUTS_DIR / f"{_safe_name(submission_id)}_score.json"


def load_scoring_result(submission_id: str) -> Optional[dict]:
    path = _scoring_result_path(submission_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_scoring_result(submission_id: str, result: dict) -> None:
    try:
        from services.provenance_service import analysis_provenance
        result.setdefault(
            "analysis_provenance",
            analysis_provenance(str(result.get("challenge_type") or "")),
        )
        _scoring_result_path(submission_id).write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NIfTI map metadata + QC analysis
# ---------------------------------------------------------------------------

def _perfusion_map_types() -> dict[str, dict[str, object]]:
    return {
        key: {
            "short": str(spec.get("display") or key),
            "label": str(spec.get("label") or key),
            "units": spec.get("units"),
            "dimensions": spec.get("dimensions"),
            "tokens": set(str(item).lower() for item in (spec.get("patterns") or [key])),
        }
        for key, spec in map_type_specs().items()
    }


def _expected_dimensions(map_type_short: str) -> Optional[int]:
    """Configured expected dimensionality for a parameter map (e.g. 3 for CBF/ATT).

    Returns None when the map type has no configured dimension rule, in which
    case dimensionality is not enforced.
    """
    for spec in _perfusion_map_types().values():
        if str(spec.get("short")) == str(map_type_short):
            dims = spec.get("dimensions")
            try:
                return int(dims) if dims is not None else None
            except (TypeError, ValueError):
                return None
    return None

_FALLBACK_DTYPE_MAP: dict[int, tuple[str, str, int]] = {
    2: ("uint8", "B", 1),
    4: ("int16", "h", 2),
    8: ("int32", "i", 4),
    16: ("float32", "f", 4),
    64: ("float64", "d", 8),
    256: ("int8", "b", 1),
    512: ("uint16", "H", 2),
    768: ("uint32", "I", 4),
}


def _json_float(value, ndigits: Optional[int] = None):
    """Preserve scientific precision; round only when explicitly requested."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, ndigits) if ndigits is not None else f


def _pct(num: int | float, den: int | float):
    if not den:
        return None
    return _json_float((float(num) / float(den)) * 100.0)


def _mean(values: list[float]):
    values = [float(v) for v in values if v is not None and math.isfinite(float(v))]
    if not values:
        return None
    return sum(values) / len(values)


def _detect_map_type(path: Path) -> dict:
    stem = _strip_nifti_suffix(path.name)

    tokenized = re.sub(r"[^a-z0-9]+", " ", stem).strip()
    tokens = set(tokenized.split())
    compact = tokenized.replace(" ", "")

    for spec in _perfusion_map_types().values():
        aliases = set(spec["tokens"])  # type: ignore[arg-type]
        compact_aliases = [
            alias.replace("-", "").replace("_", "")
            for alias in aliases
            if len(alias.replace("-", "").replace("_", "")) > 3
        ]
        if tokens.intersection(aliases) or any(alias in compact for alias in compact_aliases):
            return {
                "detected_map_type": spec["short"],
                "parameter_label": spec["label"],
                "units": spec["units"],
                "units_note": "" if spec["units"] else "units not provided",
            }

    return {
        "detected_map_type": "Unknown",
        "parameter_label": "Unknown parameter map",
        "units": None,
        "units_note": "units not provided",
    }


def _stats_from_values(values: list[float], total_voxel_count: int) -> tuple[dict, int, int, int, int]:
    finite_values: list[float] = []
    nan_count = 0
    inf_count = 0
    negative_count = 0

    for value in values:
        if math.isnan(value):
            nan_count += 1
            continue
        if math.isinf(value):
            inf_count += 1
            continue
        finite_values.append(value)
        if value < 0:
            negative_count += 1

    finite_count = len(finite_values)
    if finite_count:
        finite_sorted = sorted(finite_values)
        mid = finite_count // 2
        median = (
            finite_sorted[mid]
            if finite_count % 2
            else (finite_sorted[mid - 1] + finite_sorted[mid]) / 2.0
        )
        mean = sum(finite_values) / finite_count
        variance = sum((v - mean) ** 2 for v in finite_values) / finite_count
        std = math.sqrt(variance)
        min_val = min(finite_values)
        max_val = max(finite_values)
        cv = std / abs(mean) if mean else None
    else:
        mean = median = std = min_val = max_val = cv = None

    stats = {
        "mean": _json_float(mean),
        "median": _json_float(median),
        "standard_deviation": _json_float(std),
        "min": _json_float(min_val),
        "max": _json_float(max_val),
        "finite_percent": _pct(finite_count, total_voxel_count),
        "negative_voxel_count": negative_count,
        "negative_voxel_percent": _pct(negative_count, finite_count),
        "coefficient_of_variation": _json_float(cv),
    }
    return stats, finite_count, nan_count, inf_count, negative_count


#: Above this voxel count a map is summarised in slabs instead of whole. A
#: real DCE concentration curve is 250 million voxels; loading it, masking it
#: and taking a median over the survivors peaked past 3 GB and the kernel
#: killed the server. Parameter maps are far below this and take the exact
#: path unchanged.
_QC_STREAM_VOXELS = 32 * 1024 * 1024


def _streamed_nifti_summary(img, total_voxel_count: int) -> dict:
    """Voxel statistics for an image too large to hold, read a slab at a time.

    Mean and standard deviation are accumulated with Chan's parallel form,
    which combines per-slab moments exactly rather than summing squares, so
    the answer matches the whole-array path to floating-point noise.

    The median is the one statistic that cannot be accumulated: it needs every
    finite value at once, which is the allocation being avoided. It is
    reported as unavailable rather than approximated, because a silently
    sampled median in a column headed "median" is worse than an empty cell.
    """
    import numpy as np  # noqa: PLC0415

    shape = tuple(int(v) for v in img.shape)
    plane = 1
    for dim in shape[:-1]:
        plane *= int(dim)
    step = max(1, int(_QC_STREAM_VOXELS // max(plane, 1)))

    count = 0
    mean = 0.0
    m2 = 0.0
    nan_count = inf_count = negative_count = 0
    minimum = math.inf
    maximum = -math.inf

    for start in range(0, shape[-1], step):
        chunk = np.asarray(img.dataobj[..., start:start + step], dtype=np.float32).ravel()
        nan_count += int(np.isnan(chunk).sum())
        inf_count += int(np.isinf(chunk).sum())
        finite = chunk[np.isfinite(chunk)]
        if finite.size:
            negative_count += int((finite < 0).sum())
            minimum = min(minimum, float(finite.min()))
            maximum = max(maximum, float(finite.max()))
            batch_n = int(finite.size)
            batch_mean = float(finite.mean(dtype=np.float64))
            batch_m2 = float(((finite.astype(np.float64) - batch_mean) ** 2).sum())
            delta = batch_mean - mean
            total = count + batch_n
            mean += delta * batch_n / total
            m2 += batch_m2 + delta * delta * count * batch_n / total
            count = total
        del chunk, finite

    if not count:
        return {"total_voxel_count": total_voxel_count, "finite_count": 0,
                "nan_count": nan_count, "inf_count": inf_count,
                "negative_count": 0, "mean": None, "median": None, "std": None,
                "min": None, "max": None, "cv": None,
                "median_status": "unavailable_streamed"}
    std = math.sqrt(m2 / count)
    return {
        "total_voxel_count": total_voxel_count, "finite_count": count,
        "nan_count": nan_count, "inf_count": inf_count,
        "negative_count": negative_count,
        "mean": mean, "median": None, "std": std,
        "min": minimum, "max": maximum,
        "cv": (std / abs(mean)) if mean else None,
        "median_status": "unavailable_streamed",
    }


def _analyse_nifti_with_nibabel(path: Path) -> dict:
    import numpy as np  # type: ignore
    import nibabel as nib  # type: ignore

    img = nib.load(str(path))
    total_voxel_count = 1
    for dim in img.shape:
        total_voxel_count *= int(dim)

    median_status = None
    if total_voxel_count > _QC_STREAM_VOXELS:
        summary = _streamed_nifti_summary(img, total_voxel_count)
        finite_count = summary["finite_count"]
        nan_count = summary["nan_count"]
        inf_count = summary["inf_count"]
        negative_count = summary["negative_count"]
        mean, median, std = summary["mean"], summary["median"], summary["std"]
        min_val, max_val, cv = summary["min"], summary["max"], summary["cv"]
        median_status = summary["median_status"]
    else:
        data = np.asarray(img.dataobj, dtype=np.float32)
        flat = data.ravel()
        total_voxel_count = int(flat.size)

        finite_mask = np.isfinite(flat)
        finite_values = flat[finite_mask]
        finite_count = int(finite_values.size)
        nan_count = int(np.isnan(flat).sum())
        inf_count = int(np.isinf(flat).sum())
        negative_count = int((finite_values < 0).sum()) if finite_count else 0

        if finite_count:
            mean = float(np.mean(finite_values, dtype=np.float64))
            median = float(np.median(finite_values))
            std = float(np.std(finite_values, dtype=np.float64))
            min_val = float(np.min(finite_values))
            max_val = float(np.max(finite_values))
            cv = std / abs(mean) if mean else None
        else:
            mean = median = std = min_val = max_val = cv = None

    try:
        orientation = "".join(ax or "?" for ax in nib.aff2axcodes(img.affine))
    except Exception:
        orientation = ""
    affine = [[_json_float(v, 5) for v in row] for row in img.affine.tolist()]
    affine_diag = [_json_float(img.affine[i][i], 5) for i in range(3)]
    affine_translation = [_json_float(img.affine[i][3], 5) for i in range(3)]
    orientation_summary = (
        f"Orientation {orientation}; affine diagonal {affine_diag}; translation {affine_translation}"
        if orientation else
        f"Affine diagonal {affine_diag}; translation {affine_translation}"
    )

    metadata = {
        "shape": [int(v) for v in img.shape],
        "voxel_size": [_json_float(v, 5) for v in img.header.get_zooms()[: len(img.shape)]],
        "data_type": str(img.get_data_dtype()),
        "orientation": orientation or None,
        "affine_orientation_summary": orientation_summary,
        "affine": affine,
        "total_voxel_count": total_voxel_count,
        "finite_voxel_count": finite_count,
        "nan_count": nan_count,
        "inf_count": inf_count,
    }
    stats = {
        "mean": _json_float(mean),
        "median": _json_float(median),
        "standard_deviation": _json_float(std),
        "min": _json_float(min_val),
        "max": _json_float(max_val),
        "finite_percent": _pct(finite_count, total_voxel_count),
        "negative_voxel_count": negative_count,
        "negative_voxel_percent": _pct(negative_count, finite_count),
        "coefficient_of_variation": _json_float(cv),
    }
    if median_status:
        # Says why the cell is empty. A blank median next to a populated mean
        # otherwise reads as a defect rather than a deliberate omission.
        stats["median_status"] = median_status
    return {"metadata": metadata, "stats": stats}


def _analyse_nifti_fallback(path: Path) -> dict:
    raw = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
    if len(raw) < 348:
        raise ValueError("NIfTI header is shorter than 348 bytes")

    sizeof_hdr_le = struct.unpack("<i", raw[0:4])[0]
    sizeof_hdr_be = struct.unpack(">i", raw[0:4])[0]
    if sizeof_hdr_le == 348:
        endian = "<"
    elif sizeof_hdr_be == 348:
        endian = ">"
    else:
        raise ValueError("NIfTI header sizeof_hdr is not 348")

    dims = struct.unpack(endian + "8h", raw[40:56])
    ndim = max(0, min(int(dims[0]), 7))
    shape = [int(v) for v in dims[1 : 1 + ndim] if int(v) > 0]
    if not shape:
        shape = [0]
    total_voxel_count = 1
    for size in shape:
        total_voxel_count *= size

    datatype = struct.unpack(endian + "h", raw[70:72])[0]
    bitpix = struct.unpack(endian + "h", raw[72:74])[0]
    dtype_name, fmt_code, dtype_size = _FALLBACK_DTYPE_MAP.get(
        int(datatype),
        (f"datatype_{datatype}", "", max(1, int(bitpix) // 8 if bitpix else 1)),
    )
    pixdim = struct.unpack(endian + "8f", raw[76:108])
    voxel_size = [_json_float(abs(v), 5) for v in pixdim[1 : 1 + len(shape)]]
    vox_offset = struct.unpack(endian + "f", raw[108:112])[0]
    scl_slope = struct.unpack(endian + "f", raw[112:116])[0]
    scl_inter = struct.unpack(endian + "f", raw[116:120])[0]
    data_offset = max(352, int(vox_offset or 352))

    qform_code = struct.unpack(endian + "h", raw[252:254])[0]
    sform_code = struct.unpack(endian + "h", raw[254:256])[0]
    affine = None
    if sform_code > 0 and len(raw) >= 328:
        srow_x = list(struct.unpack(endian + "4f", raw[280:296]))
        srow_y = list(struct.unpack(endian + "4f", raw[296:312]))
        srow_z = list(struct.unpack(endian + "4f", raw[312:328]))
        affine = [srow_x, srow_y, srow_z, [0.0, 0.0, 0.0, 1.0]]
        orientation_summary = "sform affine present; orientation labels unavailable without nibabel"
    elif qform_code > 0:
        orientation_summary = "qform transform present; orientation labels unavailable without nibabel"
    else:
        orientation_summary = "Orientation unavailable; voxel spacing read from header"

    values: list[float] = []
    if fmt_code and total_voxel_count > 0:
        expected = total_voxel_count * dtype_size
        payload = raw[data_offset : data_offset + expected]
        if len(payload) < expected:
            raise ValueError("NIfTI data payload is shorter than expected from header")
        slope = float(scl_slope) if scl_slope and math.isfinite(float(scl_slope)) else 1.0
        intercept = float(scl_inter) if math.isfinite(float(scl_inter)) else 0.0
        unpacker = struct.iter_unpack(endian + fmt_code, payload)
        for item in unpacker:
            values.append(float(item[0]) * slope + intercept)

    if not fmt_code:
        stats = {
            "mean": None,
            "median": None,
            "standard_deviation": None,
            "min": None,
            "max": None,
            "finite_percent": None,
            "negative_voxel_count": None,
            "negative_voxel_percent": None,
            "coefficient_of_variation": None,
        }
        finite_count = nan_count = inf_count = 0
    else:
        stats, finite_count, nan_count, inf_count, _ = _stats_from_values(values, total_voxel_count)

    metadata = {
        "shape": shape,
        "voxel_size": voxel_size,
        "data_type": dtype_name,
        "orientation": None,
        "affine_orientation_summary": orientation_summary,
        "affine": [[_json_float(v, 5) for v in row] for row in affine] if affine else None,
        "total_voxel_count": total_voxel_count,
        "finite_voxel_count": finite_count,
        "nan_count": nan_count,
        "inf_count": inf_count,
    }
    return {"metadata": metadata, "stats": stats}


def _analyse_nifti_file(path: Path) -> dict:
    map_info = _detect_map_type(path)
    base = {
        "file_name": path.name,
        "path": str(path),
        **map_info,
    }
    try:
        try:
            analysed = _analyse_nifti_with_nibabel(path)
            analysed["metadata"]["reader"] = "nibabel"
        except ImportError:
            analysed = _analyse_nifti_fallback(path)
            analysed["metadata"]["reader"] = "nifti_header_fallback"
        except Exception:
            analysed = _analyse_nifti_fallback(path)
            analysed["metadata"]["reader"] = "nifti_header_fallback"
        return {**base, **analysed}
    except Exception as exc:
        return {
            **base,
            "metadata": {
                "shape": [],
                "voxel_size": [],
                "data_type": None,
                "orientation": None,
                "affine_orientation_summary": None,
                "affine": None,
                "total_voxel_count": 0,
                "finite_voxel_count": 0,
                "nan_count": 0,
                "inf_count": 0,
                "reader": "unreadable",
            },
            "stats": {
                "mean": None,
                "median": None,
                "standard_deviation": None,
                "min": None,
                "max": None,
                "finite_percent": None,
                "negative_voxel_count": None,
                "negative_voxel_percent": None,
                "coefficient_of_variation": None,
            },
            "error": str(exc),
        }


def _nifti_file_list(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(
        f for f in manifest_files(root, refresh_if_stale=True, submission_id=root.name)
        if _is_nifti_path(f)
    )


def _nifti_geometry(path: Path) -> dict:
    """Header, affine and an unread array proxy, without loading the data.

    The counterpart to :func:`_load_nifti_values` for images too large to hold.
    ``dataobj`` is nibabel's lazy proxy: slicing it reads only that slice, so a
    caller can stream a 4-D volume it could never materialise. Keys match
    ``_load_nifti_values`` where they overlap so ``_grids_compatible`` works on
    either.
    """
    import nibabel as nib  # noqa: PLC0415
    import numpy as np  # noqa: PLC0415

    img = nib.load(str(path))
    header = img.header
    affine = [[float(v) for v in row]
              for row in np.asarray(img.affine, dtype=np.float64).tolist()]
    try:
        voxel_size = [float(z) for z in header.get_zooms()[:3]]
    except Exception:
        voxel_size = None
    return {
        "shape": [int(v) for v in img.shape],
        "affine": affine,
        "voxel_size": voxel_size,
        "dataobj": img.dataobj,
    }


def _load_nifti_values(path: Path) -> dict:
    """Load a NIfTI map as a flat numeric array plus shape metadata.

    Uses nibabel when installed; otherwise reads simple NIfTI-1 scalar data via
    the same pure-Python fallback used by the QC metadata extractor.
    """
    try:
        import numpy as np  # type: ignore
        import nibabel as nib  # type: ignore

        img = nib.load(str(path))
        data = np.asarray(img.dataobj, dtype=np.float64)
        affine = [[float(v) for v in row] for row in np.asarray(img.affine, dtype=np.float64).tolist()]
        zooms = [float(z) for z in img.header.get_zooms()[: len(data.shape)]]
        return {
            "shape": [int(v) for v in data.shape],
            # Keep the flat float64 NumPy array (not a Python list): the previous
            # per-element list conversion cost seconds per map on full-resolution
            # data. Consumers (_comparison_metrics, diff maps) handle arrays.
            "values": data.reshape(-1),
            "affine": affine,
            "voxel_size": zooms,
            # Header facts a reviewer needs to see rather than infer. The
            # values are read as loaded, before any conversion, so the dtype
            # is the submitter's, not float64 from the line above.
            "axis_codes": [str(code) for code in nib.aff2axcodes(img.affine)],
            "dtype": str(img.header.get_data_dtype()),
            "reader": "nibabel",
        }
    except Exception:
        raw = gzip.decompress(path.read_bytes()) if path.name.endswith(".gz") else path.read_bytes()
        if len(raw) < 348:
            raise ValueError("NIfTI header is shorter than 348 bytes")

        sizeof_hdr_le = struct.unpack("<i", raw[0:4])[0]
        sizeof_hdr_be = struct.unpack(">i", raw[0:4])[0]
        if sizeof_hdr_le == 348:
            endian = "<"
        elif sizeof_hdr_be == 348:
            endian = ">"
        else:
            raise ValueError("NIfTI header sizeof_hdr is not 348")

        dims = struct.unpack(endian + "8h", raw[40:56])
        ndim = max(0, min(int(dims[0]), 7))
        shape = [int(v) for v in dims[1 : 1 + ndim] if int(v) > 0]
        if not shape:
            raise ValueError("NIfTI header does not define a valid shape")
        total_voxel_count = 1
        for size in shape:
            total_voxel_count *= size

        datatype = struct.unpack(endian + "h", raw[70:72])[0]
        bitpix = struct.unpack(endian + "h", raw[72:74])[0]
        _, fmt_code, dtype_size = _FALLBACK_DTYPE_MAP.get(
            int(datatype),
            (f"datatype_{datatype}", "", max(1, int(bitpix) // 8 if bitpix else 1)),
        )
        if not fmt_code:
            raise ValueError(f"Unsupported NIfTI datatype for reference scoring: {datatype}")

        vox_offset = struct.unpack(endian + "f", raw[108:112])[0]
        scl_slope = struct.unpack(endian + "f", raw[112:116])[0]
        scl_inter = struct.unpack(endian + "f", raw[116:120])[0]
        data_offset = max(352, int(vox_offset or 352))
        expected = total_voxel_count * dtype_size
        payload = raw[data_offset : data_offset + expected]
        if len(payload) < expected:
            raise ValueError("NIfTI data payload is shorter than expected from header")
        slope = float(scl_slope) if scl_slope and math.isfinite(float(scl_slope)) else 1.0
        intercept = float(scl_inter) if math.isfinite(float(scl_inter)) else 0.0
        values = [
            float(item[0]) * slope + intercept
            for item in struct.iter_unpack(endian + fmt_code, payload)
        ]
        # Best-effort affine/voxel size from the fallback header (sform srows if
        # present, else pixdim). None when unavailable, callers must treat a
        # missing affine as "cannot verify grid" rather than "grids match".
        affine = None
        try:
            srow_x = struct.unpack(endian + "4f", raw[280:296])
            srow_y = struct.unpack(endian + "4f", raw[296:312])
            srow_z = struct.unpack(endian + "4f", raw[312:328])
            if any(any(v for v in row) for row in (srow_x, srow_y, srow_z)):
                affine = [list(srow_x), list(srow_y), list(srow_z), [0.0, 0.0, 0.0, 1.0]]
        except Exception:
            affine = None
        try:
            pixdim = struct.unpack(endian + "8f", raw[76:108])
            voxel_size = [abs(float(v)) for v in pixdim[1 : 1 + len(shape)]]
        except Exception:
            voxel_size = None
        # Orientation needs an affine to derive, and the datatype code has
        # already been resolved to a name above. Both stay None when the
        # header does not carry them, so a check can report "unknown" rather
        # than claim a match it never verified.
        axis_codes = None
        if affine is not None:
            try:
                import nibabel as nib  # type: ignore
                import numpy as np  # type: ignore

                axis_codes = [str(c) for c in nib.aff2axcodes(np.asarray(affine, dtype=float))]
            except Exception:
                axis_codes = None
        dtype_name, _fmt, _size = _FALLBACK_DTYPE_MAP.get(int(datatype), (None, "", 0))

        return {
            "shape": shape,
            "values": values,
            "affine": affine,
            "voxel_size": voxel_size,
            "axis_codes": axis_codes,
            "dtype": dtype_name,
            "reader": "nifti_header_fallback",
        }


#: Rounding for the voxel size comparison, in millimetres. Two headers
#: written by different tools disagree in the sixth decimal place routinely,
#: and reporting that as a mismatch would train reviewers to ignore the check.
_VOXEL_SIZE_TOLERANCE_MM = 1e-3


def _header_check(submitted: dict, reference: dict) -> dict:
    """Compare a submitted map's header against the ground truth's.

    Requested by both challenge leads. A submission can be the right shape,
    score plausibly, and still be flipped or at the wrong voxel size, in which
    case every number computed from it is wrong in a way no metric reveals.
    Orientation is the case that matters most: a left-right flip leaves the
    shape identical and the statistics believable.

    Each field is reported with both values and a verdict, rather than a
    single pass/fail, so a reviewer can see *what* differs. A field neither
    file declares is ``None``, meaning not verified, which is deliberately not
    the same as ``True``.
    """
    def compare(key, normalise=lambda v: v):
        mine, theirs = submitted.get(key), reference.get(key)
        if mine is None or theirs is None:
            return {"submitted": mine, "reference": theirs, "matches": None}
        return {
            "submitted": mine,
            "reference": theirs,
            "matches": normalise(mine) == normalise(theirs),
        }

    def rounded(sizes):
        digits = max(0, -int(math.floor(math.log10(_VOXEL_SIZE_TOLERANCE_MM))))
        return [round(float(v), digits) for v in sizes]

    fields = {
        "shape": compare("shape", lambda v: [int(x) for x in v]),
        "voxel_size": compare("voxel_size", rounded),
        "orientation": compare("axis_codes", lambda v: [str(x).upper() for x in v]),
        "dtype": compare("dtype", lambda v: str(v)),
    }

    checked = [f["matches"] for f in fields.values() if f["matches"] is not None]
    mismatched = sorted(name for name, f in fields.items() if f["matches"] is False)

    # dtype differing is normal and harmless: a team may submit float64 where
    # the ground truth is float32. Geometry differing is not.
    geometry = [name for name in mismatched if name != "dtype"]
    # A file that could not be read is not the same as one nobody checked.
    # Both used to arrive here with nothing comparable and both were reported
    # as "not verified", which reads as "we did not look" when the truth is
    # "we looked and the file is broken".
    unreadable = [
        side for side, data in (("submitted", submitted), ("reference", reference))
        if data.get("read_error")
    ]
    if unreadable:
        status = "unreadable"
    elif not checked:
        status = "not_verified"
    elif geometry:
        status = "geometry_mismatch"
    elif mismatched:
        status = "dtype_differs"
    else:
        status = "matches"

    result = {"status": status, "mismatched_fields": mismatched, "fields": fields}
    if unreadable:
        result["unreadable_sides"] = unreadable
    return result


def _grids_compatible(a: dict, b: dict, *, vox_tol: float = 1e-3, aff_tol: float = 1e-2) -> Optional[bool]:
    """Whether two loaded NIfTI maps share a physical-space grid.

    Compares voxel sizes and the affine (rotation + translation) within
    tolerance. Returns None when either affine is unavailable (grid cannot be
    verified) so callers can decide whether to proceed. Same shape alone is NOT
    sufficient: two identically shaped volumes 100 mm apart are not comparable.
    """
    aff_a, aff_b = a.get("affine"), b.get("affine")
    vox_a, vox_b = a.get("voxel_size"), b.get("voxel_size")
    if aff_a is None or aff_b is None:
        return None
    try:
        for ra, rb in zip(aff_a, aff_b):
            for va, vb in zip(ra, rb):
                if abs(float(va) - float(vb)) > aff_tol:
                    return False
        if vox_a and vox_b:
            for va, vb in zip(vox_a, vox_b):
                if abs(float(va) - float(vb)) > vox_tol:
                    return False
    except (TypeError, ValueError):
        return None
    return True


def _write_float32_nifti(
    path: Path,
    shape: list[int],
    values: list[float],
    affine: Optional[list[list[float]]] = None,
) -> None:
    """Write a minimal NIfTI-1 float32 map.

    When ``affine`` (a 4x4 list, e.g. the submitted map's affine) is provided it
    is written as the sform so difference maps land in the same physical space as
    the source in imaging software. Without it, an identity grid is used.
    """
    header = bytearray(348)
    header[0:4] = (348).to_bytes(4, "little")
    header[344:348] = b"n+1\x00"
    ndim = min(len(shape), 7)
    header[40:42] = int(ndim).to_bytes(2, "little", signed=True)
    for i, size in enumerate(shape[:ndim], start=1):
        header[40 + i * 2 : 42 + i * 2] = int(size).to_bytes(2, "little", signed=True)
    header[70:72] = (16).to_bytes(2, "little", signed=True)
    header[72:74] = (32).to_bytes(2, "little", signed=True)
    header[108:112] = struct.pack("<f", 352.0)
    if affine is not None and len(affine) >= 3:
        # Preserve source placement: pixdim from affine columns + sform srows.
        try:
            for i in range(3):
                col = [float(affine[r][i]) for r in range(3)]
                vox = math.sqrt(sum(c * c for c in col)) or 1.0
                header[76 + (i + 1) * 4 : 76 + (i + 1) * 4 + 4] = struct.pack("<f", vox)
            header[252:254] = (1).to_bytes(2, "little", signed=True)  # qform_code
            header[254:256] = (1).to_bytes(2, "little", signed=True)  # sform_code
            header[280:296] = struct.pack("<4f", *[float(v) for v in affine[0][:4]])
            header[296:312] = struct.pack("<4f", *[float(v) for v in affine[1][:4]])
            header[312:328] = struct.pack("<4f", *[float(v) for v in affine[2][:4]])
        except Exception:
            for i in range(1, min(ndim, 3) + 1):
                header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)
    else:
        for i in range(1, min(ndim, 3) + 1):
            header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import numpy as np  # type: ignore
        # ``values`` comes from NumPy's default C-order flattening, while the
        # NIfTI on-disk convention stores the first spatial axis fastest
        # (Fortran order). Reorder explicitly so the difference image retains
        # the voxel layout of the submitted/reference maps.
        arr = np.asarray(values, dtype=np.float32).reshape(tuple(shape), order="C")
        arr = np.where(np.isfinite(arr), arr, np.float32("nan"))
        arr = np.asarray(arr, dtype="<f4").ravel(order="F")
        payload = arr.tobytes()
    except Exception:
        safe_values = [
            float(v) if isinstance(v, (int, float)) and math.isfinite(float(v)) else float("nan")
            for v in values
        ]
        # Pure-Python equivalent of C-shaped input -> Fortran-order NIfTI
        # payload, used only when NumPy is unavailable.
        reordered = []
        for flat_index in range(len(safe_values)):
            remainder = flat_index
            coordinates = []
            for size in shape:
                coordinates.append(remainder % int(size))
                remainder //= int(size)
            c_index = 0
            for coordinate, size in zip(coordinates, shape):
                c_index = c_index * int(size) + coordinate
            reordered.append(safe_values[c_index])
        payload = struct.pack(f"<{len(reordered)}f", *reordered)
    path.write_bytes(bytes(header) + b"\x00\x00\x00\x00" + payload)


def _filename_tokens(path: Path) -> set[str]:
    name = _strip_nifti_suffix(path.name)
    return set(t for t in re.split(r"[^a-z0-9]+", name) if t)


def _is_mask_like(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    return (
        bool(parts.intersection(_PRIVATE_PATH_PARTS).intersection({"mask", "masks"}))
        or any(pattern in name for pattern in _MASK_NAME_PATTERNS)
    )


def canonical_path_key(path: Path):
    """Return one identity for physical aliases, symlinks, and hard links."""
    try:
        stat = path.stat()
        return (stat.st_dev, stat.st_ino)
    except OSError:
        return os.path.normcase(os.path.realpath(str(path)))


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    """Keep the first path naming each distinct physical file, in order."""
    seen: set = set()
    unique: list[Path] = []
    for path in paths:
        key = canonical_path_key(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def _reference_roots(submission_id: str, challenge_type: str) -> list[Path]:
    roots: list[Path] = []
    extracted_ref = EXTRACTED_DIR / submission_id / "reference"
    roots.extend([
        extracted_ref,
        REFERENCE_DATA_DIR,
        REFERENCE_DATA_DIR / "reference",
        REFERENCE_DATA_DIR / challenge_type.lower().strip(),
        SCORING_DIR / "reference",
        SCORING_DIR / challenge_type.lower().strip() / "reference",
    ])
    try:
        active = get_active_entry(challenge_type)
        if active.get("mode") == "custom" and active.get("package_id"):
            manifest = get_package_manifest(active["package_id"])
            installed = manifest.get("installed_path") if isinstance(manifest, dict) else None
            if installed:
                roots.append(Path(installed) / "reference")
    except Exception:
        pass

    # Case-insensitive filesystems make two spellings of one directory look
    # like two roots; canonical_path_key collapses them.
    seen: set = set()
    existing: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        key = canonical_path_key(root)
        if key in seen:
            continue
        seen.add(key)
        existing.append(root)
    return existing


def _reference_maps_by_type(root: Path) -> dict[str, list[Path]]:
    """Reference maps grouped by detected type, one entry per physical file.

    Both spellings of the maps directory are searched because either may be
    what a provider shipped; on a case-insensitive filesystem they are the
    same directory, so results are deduplicated by physical file identity
    rather than by path text.
    """
    map_dirs = _dedupe_paths([root / "maps", root / "Maps", root])
    by_type: dict[str, list[Path]] = {}
    seen: set = set()
    for map_dir in map_dirs:
        for path in _nifti_file_list(map_dir):
            key = canonical_path_key(path)
            if key in seen or _is_mask_like(path):
                continue
            detected = _detect_map_type(path).get("detected_map_type")
            if not detected or detected == "Unknown":
                continue
            seen.add(key)
            by_type.setdefault(str(detected), []).append(path)
    return by_type


def _mask_overlaps(masks: list[dict]) -> list[dict]:
    """Pairs of ROI masks that share voxels, with how many.

    The DCE challenge ships nested regions: every one of the 262 hippocampus
    voxels is also grey matter. That is a perfectly reasonable way to define
    ROIs, and it means the per-region statistics are not independent, which a
    reader has no way to see from a table of one row per region. The
    challenge's own answer key uses disjoint regions, so its grey-matter bias
    is computed over 4698 voxels while the pipeline reports the 4960-voxel
    mask as supplied, and the two differ for a reason nothing on the page
    explains.

    Nothing here changes a number. Making the regions exclusive would be a
    scientific decision about what "grey matter" means in this challenge, and
    that belongs to the organisers. This only says what overlaps.
    """
    try:
        import numpy as np  # noqa: PLC0415
    except Exception:  # pragma: no cover - numpy is a hard dependency in practice
        return []

    loaded: list[tuple[str, Any]] = []
    for mask in masks:
        try:
            data = _load_nifti_values(Path(mask["path"]))
            selector = np.asarray(_mask_selector(data["values"]), dtype=bool)
        except Exception:
            continue
        loaded.append((str(mask.get("label") or mask.get("name")), selector))

    overlaps: list[dict] = []
    for i, (label_a, a) in enumerate(loaded):
        for label_b, b in loaded[i + 1:]:
            if a.shape != b.shape:
                continue
            shared = int(np.logical_and(a, b).sum())
            if not shared:
                continue
            count_a, count_b = int(a.sum()), int(b.sum())
            overlaps.append({
                "regions": [label_a, label_b],
                "shared_voxels": shared,
                "voxels": [count_a, count_b],
                # "nested" when one region sits entirely inside the other,
                # which is the case a reader is most likely to misread.
                "nested": shared in (count_a, count_b),
            })
    return overlaps


def _mask_label_for_name(name: str) -> str:
    low = name.lower()
    stem = _strip_nifti_suffix(name).replace("_", " ").replace("-", " ").strip()
    for rule in mask_label_rules():
        label = str(rule.get("label") or "").strip()
        patterns = rule.get("patterns") or ()
        if label and any(str(pattern).lower() in low for pattern in patterns):
            return label
    return stem or name


import logging as _logging

_LOGGER = _logging.getLogger(__name__)


def submission_artifacts(submission_id: str) -> list:
    """Normalized artifacts for a submission, from the existing manifest.

    Reuses the manifest the scoring path has already refreshed, no second
    traversal of the submission tree.
    """
    from osipi_pipeline.ingestion.manifest import load_manifest
    from osipi_pipeline.ingestion.models import SubmissionArtifact

    root = EXTRACTED_DIR / submission_id
    manifest = load_manifest(root, refresh_if_stale=False) or {}
    return [
        SubmissionArtifact(**item)
        for item in manifest.get("artifacts", [])
        if isinstance(item, dict)
    ]


def _attach_roi_descriptives(
    reference_scoring: dict, submission_id: str, challenge_type: str
) -> None:
    """Populate ROI descriptive statistics on the reference-scoring result.

    Distinguishes *expected scientific unavailability*, no masks, no
    eligible Ktrans, from an unexpected internal error. The former is a
    normal outcome recorded as a status, not an application fault, so it is
    not logged as a crash.
    """
    from services.roi_descriptive_service import (
        eligible_artifacts,
    )

    try:
        artifacts = submission_artifacts(submission_id)
        eligible = eligible_artifacts(artifacts, challenge=challenge_type)
        masks = masks_for_submission(submission_id, challenge_type)

        if not masks:
            reference_scoring["roi_descriptive_status"] = "no_roi_configured"
            return
        if not eligible:
            reference_scoring["roi_descriptive_status"] = "no_eligible_maps"
            return

        attach_roi_descriptive_statistics(
            reference_scoring, artifacts,
            challenge_type=challenge_type,
            root=EXTRACTED_DIR / submission_id,
        )
        reference_scoring["roi_descriptive_status"] = "available"
    except Exception:
        # Unexpected only. Existing QC and reference metrics are preserved;
        # the ROI layer degrades to an explicit unavailable status.
        _LOGGER.exception(
            "ROI descriptive statistics failed for %s", submission_id)
        reference_scoring.setdefault("roi_descriptive_statistics", [])
        reference_scoring["roi_descriptive_status"] = "calculation_error"


def _artifact_identity(artifact) -> tuple:
    return tuple(
        getattr(artifact, field, None)
        for field in ("dataset", "participant", "repeat", "site")
    )


def _unique_signal_match(model_path: Path, candidates: list[Path]) -> Optional[Path]:
    """Return a reference signal only when filename/path tokens identify it clearly."""
    if len(candidates) == 1:
        return candidates[0]
    model_tokens = _filename_tokens(model_path)
    ranked = sorted(
        ((len(model_tokens.intersection(_filename_tokens(path))), path) for path in candidates),
        key=lambda item: (item[0], str(item[1])), reverse=True,
    )
    if not ranked or ranked[0][0] == 0:
        return None
    if len(ranked) > 1 and ranked[0][0] == ranked[1][0]:
        return None
    return ranked[0][1]


def _score_signal_rss(
    reference_scoring: dict,
    submission_id: str,
    challenge_type: str,
    *,
    artifact_dir: Optional[Path] = None,
) -> None:
    """Attach conditional measured-vs-modelled 4-D RSS analysis.

    The measured signal is optional. Absence is an explicit normal status, not
    an error and not a reason to suppress map QC or other analyses.
    """
    from osipi_pipeline.scoring.rss_statistics import (
        METHODOLOGY as RSS_METHODOLOGY,
        streaming_voxelwise_rss,
        summarize_rss,
    )

    analysis = analysis_by_challenge().get((challenge_type or "").strip().lower(), {})
    rss_config = analysis.get("signal_rss") or {}
    rss_enabled = bool(rss_config.get("enabled", False))
    modelled_artifact = str(rss_config.get("modelled_artifact") or "").strip().lower()
    measured_artifact = str(rss_config.get("measured_artifact") or "").strip().lower()
    result = {
        "status": "measured_signal_not_available" if rss_enabled else "not_applicable",
        "available": False,
        "methodology": dict(RSS_METHODOLOGY),
        "records": [],
        "warnings": [],
    }
    reference_scoring["signal_rss"] = result
    # The analysis has always been challenge-generic; it was named for DCE
    # because DCE enabled it first. The old key is kept so an existing saved
    # result or an external reader does not break on the rename.
    reference_scoring["dce_signal_rss"] = result
    if not rss_enabled:
        return

    artifacts = submission_artifacts(submission_id)
    modelled = [
        a for a in artifacts
        if getattr(a, "artifact_type", None) == modelled_artifact
    ]
    measured = [
        a for a in artifacts
        if getattr(a, "artifact_type", None) == measured_artifact
    ]
    if not modelled:
        result["status"] = "modelled_signal_not_available"
        return

    models_by_id: dict[tuple, list] = {}
    measured_by_id: dict[tuple, list] = {}
    for artifact in modelled:
        models_by_id.setdefault(_artifact_identity(artifact), []).append(artifact)
    for artifact in measured:
        measured_by_id.setdefault(_artifact_identity(artifact), []).append(artifact)

    root = EXTRACTED_DIR / submission_id
    reference_root = reference_scoring.get("reference_root")
    masks = masks_for_submission(submission_id, challenge_type)
    measured_spec = artifact_type_specs().get(measured_artifact) or {}
    measured_patterns = tuple(str(value).lower() for value in measured_spec.get("patterns") or ())
    reference_measured = [
        path for path in (_nifti_file_list(Path(reference_root)) if reference_root else [])
        if any(pattern in path.name.lower() for pattern in measured_patterns)
        and not _is_mask_like(path)
    ]
    if not measured and not reference_measured:
        return

    for identity in sorted(set(models_by_id) | set(measured_by_id), key=str):
        model_items = models_by_id.get(identity, [])
        measured_items = measured_by_id.get(identity, [])
        if not measured_items and len(model_items) == 1 and reference_measured:
            from types import SimpleNamespace
            model_path_for_match = root / str(model_items[0].path)
            matched = _unique_signal_match(model_path_for_match, reference_measured)
            if matched is not None:
                measured_items = [SimpleNamespace(path=str(matched))]
        record = {
            "dataset": identity[0], "participant": identity[1],
            "repeat": identity[2], "site": identity[3],
            "status": "not_compared", "whole_image": None, "rois": [],
            "rss_map": None,
        }
        if len(model_items) != 1 or len(measured_items) != 1:
            record["status"] = "ambiguous_or_missing_pair"
            record["error"] = (
                f"Expected one measured and one modelled signal for this scan; "
                f"found {len(measured_items)} measured and {len(model_items)} modelled."
            )
            result["records"].append(record)
            continue

        model_artifact, measured_artifact = model_items[0], measured_items[0]
        model_path = root / str(model_artifact.path)
        measured_path = root / str(measured_artifact.path)
        record["modelled_file"] = str(model_artifact.path)
        record["measured_file"] = str(measured_artifact.path)
        try:
            # Headers and array proxies only. A real DCE concentration curve is
            # 1 GB as float32 and 2 GB as float64, so materialising the measured
            # and modelled volumes here, as this used to, asked for about 6 GB
            # for one scan pair and the kernel killed the process. RSS sums over
            # time, so it streams.
            model_meta = _nifti_geometry(model_path)
            measured_meta = _nifti_geometry(measured_path)
            if len(model_meta["shape"]) != 4 or len(measured_meta["shape"]) != 4:
                raise ValueError("RSS requires measured and modelled 4-D signals")
            if model_meta["shape"] != measured_meta["shape"]:
                raise ValueError(
                    f"Measured shape {measured_meta['shape']} does not match "
                    f"modelled shape {model_meta['shape']}"
                )
            grid_ok = _grids_compatible(measured_meta, model_meta)
            if grid_ok is False:
                raise ValueError("Measured and modelled signals use different spatial grids")
            rss = streaming_voxelwise_rss(
                measured_meta["dataobj"], model_meta["dataobj"], measured_meta["shape"]
            )
            record["whole_image"] = summarize_rss(rss).to_dict()
            spatial_shape = tuple(measured_meta["shape"][:3])
            for mask in masks:
                roi = {"mask_name": mask["name"], "roi_label": mask["label"]}
                try:
                    mask_data = _load_nifti_values(mask["path"])
                    if tuple(mask_data["shape"]) != spatial_shape:
                        raise ValueError(
                            f"Mask shape {mask_data['shape']} does not match RSS shape {spatial_shape}"
                        )
                    selector = _mask_selector(mask_data["values"])
                    roi.update(summarize_rss(rss, selector).to_dict())
                except Exception as exc:
                    roi.update({"status": "unavailable", "error": str(exc)})
                record["rois"].append(roi)
            if artifact_dir is not None:
                rss_dir = artifact_dir / "signal_rss"
                label = "_".join(str(value or "unknown") for value in identity)
                rss_path = rss_dir / f"{label}_rss.nii"
                _write_float32_nifti(
                    rss_path, spatial_shape, rss.reshape(-1), affine=measured_meta.get("affine")
                )
                record["rss_map"] = str(rss_path.relative_to(artifact_dir))
            record["status"] = "available"
        except Exception as exc:
            record["status"] = "calculation_error"
            record["error"] = str(exc)
        result["records"].append(record)

    available = sum(1 for row in result["records"] if row.get("status") == "available")
    result["available"] = available > 0
    result["status"] = "available" if available == len(result["records"]) else (
        "partial" if available else "unavailable"
    )


def _attach_threshold_flags(reference_scoring: dict, challenge_type: str) -> None:
    """Mark ROI rows a reviewer should look at, when a challenge asks for it.

    Advisory only. Nothing here fails, excludes or ranks a submission; it
    annotates rows so a reviewer scanning a long table knows where to start.
    No challenge ships a threshold, so this normally records that none is
    configured and changes nothing.
    """
    from osipi_pipeline.scoring.thresholds import (
        METHODOLOGY as THRESHOLD_METHODOLOGY,
        assess_row,
        summarize,
    )

    thresholds = thresholds_by_challenge().get((challenge_type or "").strip().lower(), {}) or {}
    reference_scoring["threshold_methodology"] = dict(THRESHOLD_METHODOLOGY)
    reference_scoring["thresholds"] = dict(thresholds)

    rows = reference_scoring.get("roi_descriptive_statistics") or []
    if thresholds:
        for row in rows:
            if isinstance(row, dict):
                row["threshold_assessments"] = assess_row(row, thresholds)
    reference_scoring["threshold_summary"] = summarize(
        [row for row in rows if isinstance(row, dict)], thresholds,
    )


def _icc_definition(challenge_type: str) -> str:
    """What the ICC column means for this challenge, in the reader's terms.

    Two very different situations both produce a blank ICC: no model has been
    chosen, or a model is chosen but this submission has no repeated scans to
    apply it to. They are not the same and a reader has to be able to tell.
    """
    from osipi_pipeline.scoring.icc import MODEL_DESCRIPTIONS, MODEL_NONE

    settings = icc_settings_by_challenge().get((challenge_type or "").strip().lower(), {}) or {}
    models = settings.get("models", ())
    if not models:
        return (
            "Not computed: no ICC model is configured for this challenge. "
            "Requires repeated datasets and a challenge-approved ICC model."
        )
    return " ".join(MODEL_DESCRIPTIONS.get(model, model) for model in models) + (
        " Computed from scan-level ROI medians across a participant x session "
        "table; blank where a submission has too few repeated scans."
    )


def _attach_grouped_roi_statistics(reference_scoring: dict, challenge_type: str) -> None:
    """Attach configured descriptive grouping of scan-level ROI statistics."""
    from osipi_pipeline.scoring.grouped_statistics import METHODOLOGY, compute_grouped_statistics

    spec = grouped_statistics_by_challenge().get((challenge_type or "").strip().lower(), {})
    reference_scoring["grouped_roi_methodology"] = dict(METHODOLOGY)
    reference_scoring["grouped_roi_statistics"] = []
    if not spec.get("enabled"):
        reference_scoring["grouped_roi_status"] = "disabled"
        return
    rows = reference_scoring.get("roi_descriptive_statistics") or []
    results = compute_grouped_statistics(
        rows,
        axes=spec.get("axes") or (),
        source=str(spec.get("source") or "roi_median"),
        minimum_group_size=int(spec.get("minimum_group_size") or 2),
    )
    reference_scoring["grouped_roi_statistics"] = [item.to_dict() for item in results]
    reference_scoring["grouped_roi_status"] = "available" if results else "no_groups"


def _attach_icc(reference_scoring: dict, challenge_type: str, roi_rows: list) -> None:
    """Attach ICC when the challenge has chosen a model, and say so when not.

    ICC needs the same per-scan ROI rows the grouping uses, arranged as a
    participants x sessions table. The model is configuration because six
    defensible models exist; with none chosen this records why the field is
    empty rather than leaving a reader to guess whether it was zero, missing,
    or not applicable.
    """
    from osipi_pipeline.scoring.icc import (
        METHODOLOGY as ICC_METHODOLOGY,
        MODEL_NONE,
        REASON_NOT_CONFIGURED,
        compute_icc_for_rows,
        IccResult,
        MODEL_DESCRIPTIONS,
        STATUS_LABELS,
    )

    challenge_type = (challenge_type or "").strip().lower()
    settings = icc_settings_by_challenge().get(challenge_type, {})
    models = settings.get("models", ())
    reference_scoring["icc_methodology"] = dict(ICC_METHODOLOGY)
    reference_scoring["icc_model"] = models[0] if len(models) == 1 else None
    reference_scoring["icc_models"] = list(models)
    reference_scoring["icc_statistics"] = []

    if not models:
        reference_scoring["icc_status"] = "not_configured"
        reference_scoring["icc_unavailable_reason"] = REASON_NOT_CONFIGURED
        return

    results = [result for model in models for result in compute_icc_for_rows(
        roi_rows,
        model=model,
        axes=settings.get("axes") or ("inter_repeat",),
        source=str(
            (grouped_statistics_by_challenge().get(challenge_type, {}) or {})
            .get("source") or "roi_median"
        ),
        confidence_level=settings.get("confidence_level"),
    )]
    for model in models:
        if not any(result.model == model for result in results):
            results.append(IccResult(
                model=model, model_description=MODEL_DESCRIPTIONS[model],
                status="no_groups", unavailable_reason="Not enough compatible repeated scans.",
                challenge=challenge_type,
            ))
    reference_scoring["icc_statistics"] = [
        {**item.to_dict(), "status_label": STATUS_LABELS.get(item.status, item.status.replace("_", " "))}
        for item in results
    ]
    usable = [item for item in results if item.value is not None]
    reference_scoring["icc_status"] = "available" if usable else "no_groups"
    reference_scoring["icc_unavailable_reason"] = (
        None if usable else "No participant x session table had enough scans."
    )


def _reference_scoring_result_keys_probe() -> dict:
    """Return an empty reference-scoring result. Test seam for shape checks.

    Calls the real builder with no maps, so the asserted key set is the one
    production actually produces rather than a copy that could drift.
    """
    return _score_reference_maps("__probe__", "dce", [])


def _roi_methodology() -> dict:
    """Formula conventions for the ROI descriptive statistics.

    Emitted once per result rather than repeated on every row.
    """
    from services.roi_descriptive_service import methodology

    return methodology()


def attach_roi_descriptive_statistics(
    reference_result: dict,
    artifacts,
    *,
    challenge_type: str,
    root: Path,
) -> dict:
    """Populate ``roi_descriptive_statistics`` on a reference-scoring result.

    ROI definitions come from :func:`masks_for_submission`, the single mask
    discovery used by every scoring path. Computed once here; the API, JSON,
    CSV and report model all read these records rather than recomputing.

    Failure is non-fatal: descriptive statistics are additive, and existing
    reference metrics must not be lost because an ROI could not be read.
    """
    from services.roi_descriptive_service import (
        compute_roi_descriptive_statistics,
        roi_definitions_from_masks,
    )

    # root is EXTRACTED_DIR / submission_id, so the id is its final component.
    masks = masks_for_submission(Path(root).name, challenge_type)
    rois = roi_definitions_from_masks(masks)
    # Let the caller record failures and availability accurately.
    results = compute_roi_descriptive_statistics(
        artifacts, rois, challenge=challenge_type, root=root,
    )
    reference_result["roi_descriptive_statistics"] = [
        item.to_dict() for item in results
    ]
    return reference_result


def masks_for_submission(submission_id: str, challenge_type: str) -> list[dict]:
    """Every ROI mask visible to this submission, from every reference root.

    The one place mask discovery happens. Four call sites used to re-derive
    masks from ``reference_root`` alone, which silently limited them to the
    root that happened to hold the ground-truth maps.
    """
    return _reference_masks_across_roots(
        _reference_roots(submission_id, challenge_type)
    )


def _reference_masks_across_roots(roots: Sequence[Path]) -> list[dict]:
    """ROI masks from *every* reference root, once per physical file.

    Masks and ground-truth maps are independent assets and organisers do not
    reliably keep them together: a shared ``masks/`` folder alongside a
    per-challenge ``asl/maps/`` folder is a natural layout, and so is a mask
    dropped in the reference root while the maps sit one level down. The map
    root has to be a single choice, mixing two ground truths would be wrong,
    but a mask found anywhere the pipeline is already allowed to look is a
    mask the reviewer meant to provide.

    Searching only the map root is what made a supplied mask silently do
    nothing: no ROI rows, and whole-image bias and MAE reported with no
    indication that the region breakdown was missing rather than empty.
    """
    seen: set = set()
    masks: list[dict] = []
    for root in roots:
        for mask in _reference_masks(root):
            key = canonical_path_key(Path(mask["path"]))
            if key in seen:
                continue
            seen.add(key)
            masks.append(mask)
    return masks


def _reference_masks(root: Path) -> list[dict]:
    """Find ROI masks under one reference root, once per physical file."""
    mask_dirs = _dedupe_paths([root / "masks", root / "Masks"])
    paths: list[Path] = []
    for mask_dir in mask_dirs:
        paths.extend(_nifti_file_list(mask_dir))
    if not paths:
        paths = [p for p in _nifti_file_list(root) if _is_mask_like(p)]

    masks = []
    for path in _dedupe_paths(paths):
        name = path.name
        masks.append({
            "name": name,
            "label": _mask_label_for_name(name),
            "path": path,
        })
    return masks


def _scan_identity(
    path: Path,
    root: Optional[Path] = None,
    challenge: Optional[str] = None,
) -> tuple:
    """(dataset, participant, repeat, site) implied by a file's location.

    Resolved from the path relative to ``root`` when one is given, so the
    submission root or reference root is not itself mistaken for identity.

    ``challenge`` unlocks that challenge's ``filename_identity_patterns``. It is
    optional because the original caller -- matching a submitted map to its
    reference -- works from directory structure alone; a layout that encodes the
    scan in the filename instead (``Synthetic_P001_Visit1_Site1``) resolves to
    nothing without it.
    """
    from osipi_pipeline.ingestion.identity_parser import resolve_identity

    relative = path
    if root is not None:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
    resolved, _conflicts = resolve_identity(
        str(relative).replace(os.sep, "/"), challenge=challenge)
    return (
        resolved.get("dataset"), resolved.get("participant"),
        resolved.get("repeat"), resolved.get("site"),
    )


def _scan_label(dataset, participant, repeat, site) -> Optional[str]:
    """A short human name for one scan, or None when nothing identifies it.

    The DCE-2026 layout gives every scan the same filenames by design, so a
    table keyed on the filename repeats "Ktrans.nii.gz" sixty times and tells a
    reader nothing about which scan a row belongs to. The identity is already
    resolved during ingestion; this turns it into something printable so it can
    travel with the numbers into reports and CSVs.

    Parts that are unknown are left out rather than filled in with a
    placeholder: "P01 - Site 2" is honest about a missing repeat in a way that
    "P01 - Site 2 - Repeat ?" is not.
    """
    parts = []
    if participant:
        parts.append(f"P{participant}" if str(participant).isdigit() else str(participant))
    if site:
        parts.append(f"Site {site}")
    if repeat:
        parts.append(f"Repeat {repeat}")
    if not parts:
        return None
    label = " \u00b7 ".join(parts)
    if dataset:
        label = f"{dataset} {label}" if len(parts) < 3 else label
    return label


def _choose_reference_match(
    submitted_path: Path,
    candidates: list[Path],
    *,
    submission_root: Optional[Path] = None,
    reference_root: Optional[Path] = None,
    challenge_type: Optional[str] = None,
) -> Optional[Path]:
    """The ground-truth file belonging to this scan.

    Scan identity is checked before the filename, because in a real challenge
    submission the filename carries none. The DCE lead's data is laid out as
    ``P05/site_2/scan_1/Ktrans.nii.gz`` with the ground truth in the same shape,
    so all sixty candidates share the single token ``ktrans`` and tie. The
    previous filename-only rule then broke the tie on path length, which is
    arbitrary: it would score participant 5 at site 2 against participant 1 at
    site 1 and report the result as a comparison. Wrong ground truth produces
    numbers that look entirely reasonable, which is the worst kind of wrong.

    A candidate whose participant, site, repeat and dataset all match is used.
    Partial identities must not conflict. Filename evidence can disambiguate
    compatible candidates, but ties and conflicting identities return None.
    """
    if not candidates:
        return None
    wanted = _scan_identity(submitted_path, submission_root, challenge=challenge_type)
    identities = {candidate: _scan_identity(candidate, reference_root, challenge=challenge_type)
                  for candidate in candidates}
    # Known conflicting identities must never be overridden by a filename.
    candidates = [candidate for candidate in candidates
                  if not any(a is not None and b is not None and a != b
                             for a, b in zip(wanted, identities[candidate]))]
    if not candidates:
        return None
    if any(value is not None for value in wanted):
        matched = [
            candidate for candidate in candidates
            if identities[candidate] == wanted
        ]
        if len(matched) == 1:
            return matched[0]
        if matched:
            candidates = matched
        else:
            partial = [candidate for candidate in candidates
                       if any(a is not None and a == b
                              for a, b in zip(wanted, identities[candidate]))]
            if partial:
                candidates = partial

    if len(candidates) == 1:
        return candidates[0]

    sub_tokens = _filename_tokens(submitted_path)
    best = sorted(
        candidates,
        key=lambda p: (len(sub_tokens.intersection(_filename_tokens(p))), -len(str(p))),
        reverse=True,
    )
    # Equal filename evidence is ambiguous, not permission to choose by path
    # length or directory ordering.
    if len(sub_tokens.intersection(_filename_tokens(best[0]))) == len(sub_tokens.intersection(_filename_tokens(best[1]))):
        return None
    return best[0]


def _correlation(xs: list[float], ys: list[float]):
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    sx = math.sqrt(sum(x * x for x in dx))
    sy = math.sqrt(sum(y * y for y in dy))
    if sx == 0 or sy == 0:
        return None
    return sum(x * y for x, y in zip(dx, dy)) / (sx * sy)


def _comparison_metrics(
    submitted_values,
    reference_values,
    selector=None,
) -> dict:
    """Voxelwise comparison metrics.

    Uses a vectorised NumPy path (100x+ faster on full-resolution maps) and falls
    back to the pure-Python implementation when NumPy is unavailable. The two
    paths compute the identical statistics, so scoring values are unchanged,
    only faster. That equivalence is pinned by
    ``tests/test_comparison_metrics_parity.py``, which compares the two paths
    key by key over the cases that separate a vectorised implementation from a
    loop: NaN and infinity on either side, ROI selectors, no finite overlap, a
    zero reference mean, a constant map, and randomised maps.
    """
    try:
        import numpy as np  # type: ignore
    except Exception:
        return _comparison_metrics_py(submitted_values, reference_values, selector)

    sub = np.asarray(submitted_values, dtype=np.float64).reshape(-1)
    ref = np.asarray(reference_values, dtype=np.float64).reshape(-1)
    if selector is None:
        sel = np.ones(sub.shape[0], dtype=bool)
    else:
        sel = np.asarray(selector, dtype=bool).reshape(-1)
    total_count = int(sel.sum())

    m = min(sub.shape[0], ref.shape[0], sel.shape[0])
    sub_s = sub[:m][sel[:m]]
    ref_s = ref[:m][sel[:m]]
    sub_fin = np.isfinite(sub_s)
    ref_fin = np.isfinite(ref_s)
    submitted_finite_count = int(sub_fin.sum())
    reference_finite_count = int(ref_fin.sum())
    both = sub_fin & ref_fin
    sf = sub_s[both]
    rf = ref_s[both]
    n = int(sf.shape[0])

    if not n:
        status = "no_finite_overlap"
        error = "No finite submitted/reference voxel pairs were available in the scored region."
        if total_count > 0 and reference_finite_count == 0:
            status = "reference_invalid"
            error = "Reference map has no finite voxels in the scored region."
        elif total_count > 0 and submitted_finite_count == 0:
            status = "submitted_invalid"
            error = "Submitted map has no finite voxels in the scored region."
        return {
            "status": status,
            "error": error,
            "voxel_count": 0,
            "total_voxel_count": total_count,
            "finite_voxel_percent": _pct(0, total_count),
            "negative_voxel_percent": None,
            "bias": None,
            "mean_error": None,
            "mae": None,
            "rmse": None,
            "standard_deviation_error": None,
            "coefficient_of_variation": None,
            "correlation": None,
        }

    errors = sf - rf
    negative_count = int((sf < 0).sum())
    bias = float(errors.mean())
    mae = float(np.abs(errors).mean())
    rmse = float(np.sqrt(np.mean(errors * errors)))
    std_error = float(np.sqrt(np.mean((errors - bias) ** 2)))
    ref_mean = float(rf.mean())
    cov = std_error / abs(ref_mean) if ref_mean else None

    # Pearson correlation (sample form, matching _correlation()).
    corr = None
    if n >= 2:
        dx = sf - sf.mean()
        dy = rf - rf.mean()
        sx = float(np.sqrt(np.sum(dx * dx)))
        sy = float(np.sqrt(np.sum(dy * dy)))
        if sx and sy:
            corr = float(np.sum(dx * dy) / (sx * sy))

    return {
        "status": "compared",
        "voxel_count": n,
        "total_voxel_count": total_count,
        "finite_voxel_percent": _pct(n, total_count),
        "negative_voxel_percent": _pct(negative_count, n),
        "mean_submitted": _json_float(float(sf.mean())),
        "mean_reference": _json_float(ref_mean),
        "bias": _json_float(bias),
        "mean_error": _json_float(bias),
        "mae": _json_float(mae),
        "rmse": _json_float(rmse),
        "standard_deviation_error": _json_float(std_error),
        "error_coefficient_of_variation": _json_float(cov),
        "coefficient_of_variation": _json_float(cov),  # backward-compat alias
        "cov_kind": "error_cov",
        "correlation": _json_float(corr),
    }


def _comparison_metrics_py(
    submitted_values: list[float],
    reference_values: list[float],
    selector: Optional[list[bool]] = None,
) -> dict:
    if selector is None:
        selector = [True] * len(submitted_values)
    total_count = sum(1 for flag in selector if flag)
    sub_finite: list[float] = []
    ref_finite: list[float] = []
    errors: list[float] = []
    negative_count = 0
    submitted_finite_count = 0
    reference_finite_count = 0

    for sub, ref, include in zip(submitted_values, reference_values, selector):
        if not include:
            continue
        sub_is_finite = isinstance(sub, (int, float)) and math.isfinite(float(sub))
        ref_is_finite = isinstance(ref, (int, float)) and math.isfinite(float(ref))
        if sub_is_finite:
            submitted_finite_count += 1
        if ref_is_finite:
            reference_finite_count += 1
        if not (sub_is_finite and ref_is_finite):
            continue
        sf = float(sub)
        rf = float(ref)
        if sf < 0:
            negative_count += 1
        sub_finite.append(sf)
        ref_finite.append(rf)
        errors.append(sf - rf)

    n = len(errors)
    if not n:
        status = "no_finite_overlap"
        error = "No finite submitted/reference voxel pairs were available in the scored region."
        if total_count > 0 and reference_finite_count == 0:
            status = "reference_invalid"
            error = "Reference map has no finite voxels in the scored region."
        elif total_count > 0 and submitted_finite_count == 0:
            status = "submitted_invalid"
            error = "Submitted map has no finite voxels in the scored region."
        return {
            "status": status,
            "error": error,
            "voxel_count": 0,
            "total_voxel_count": total_count,
            "finite_voxel_percent": _pct(0, total_count),
            "negative_voxel_percent": None,
            "bias": None,
            "mean_error": None,
            "mae": None,
            "rmse": None,
            "standard_deviation_error": None,
            "coefficient_of_variation": None,
            "correlation": None,
        }

    bias = sum(errors) / n
    mae = sum(abs(e) for e in errors) / n
    rmse = math.sqrt(sum(e * e for e in errors) / n)
    std_error = math.sqrt(sum((e - bias) ** 2 for e in errors) / n)
    ref_mean = sum(ref_finite) / n
    cov = std_error / abs(ref_mean) if ref_mean else None
    return {
        "status": "compared",
        "voxel_count": n,
        "total_voxel_count": total_count,
        "finite_voxel_percent": _pct(n, total_count),
        "negative_voxel_percent": _pct(negative_count, n),
        "mean_submitted": _json_float(sum(sub_finite) / n),
        "mean_reference": _json_float(ref_mean),
        "bias": _json_float(bias),
        "mean_error": _json_float(bias),
        "mae": _json_float(mae),
        "rmse": _json_float(rmse),
        "standard_deviation_error": _json_float(std_error),
        # Spread of voxelwise errors over the reference mean. This is an accuracy
        # spread ratio, NOT a repeatability CoV (which needs repeated datasets).
        "error_coefficient_of_variation": _json_float(cov),
        "coefficient_of_variation": _json_float(cov),  # backward-compat alias
        "cov_kind": "error_cov",
        "correlation": _json_float(_correlation(sub_finite, ref_finite)),
    }


def _difference_values(submitted_values, reference_values):
    """Voxelwise (submitted - reference); NaN where either voxel is non-finite."""
    try:
        import numpy as np  # type: ignore
        s = np.asarray(submitted_values, dtype=np.float64).reshape(-1)
        r = np.asarray(reference_values, dtype=np.float64).reshape(-1)
        m = min(s.shape[0], r.shape[0])
        s, r = s[:m], r[:m]
        return np.where(np.isfinite(s) & np.isfinite(r), s - r, np.nan)
    except Exception:
        return [
            (float(s) - float(r)) if math.isfinite(float(s)) and math.isfinite(float(r)) else float("nan")
            for s, r in zip(submitted_values, reference_values)
        ]


def _mask_selector(mask_values):
    """Boolean selector for a mask: include voxels that are finite and > 0."""
    try:
        import numpy as np  # type: ignore
        m = np.asarray(mask_values, dtype=np.float64).reshape(-1)
        return np.isfinite(m) & (m > 0)
    except Exception:
        return [
            isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0
            for v in mask_values
        ]


def _score_reference_maps(
    submission_id: str,
    challenge_type: str,
    maps: list[dict],
    artifact_dir: Optional[Path] = None,
) -> dict:
    roots = _reference_roots(submission_id, challenge_type)
    submitted_maps = [m for m in maps if m.get("detected_map_type") and m.get("detected_map_type") != "Unknown"]
    roi_analysis = (
        analysis_by_challenge().get((challenge_type or "").strip().lower(), {})
        .get("roi_descriptive") or {}
    )
    result = {
        "status": "reference_not_available",
        "available": False,
        "reference_root": None,
        "masks_available": False,
        "mask_count": 0,
        "warnings": [],
        "maps": [],
        # ROI descriptive statistics are separate from reference-error metrics.
        "roi_descriptive_statistics": [],
        "roi_descriptive_methodology": _roi_methodology(),
        "roi_descriptive_report_metrics": list(
            roi_analysis.get("report_metrics") or (
                "mean", "median", "standard_deviation", "range",
                "coefficient_of_variation",
            )
        ),
        "roi_descriptive_status": "not_calculated",
        "summary": {
            "reference_map_count": 0,
            "compared_map_count": 0,
            "mean_rmse": None,
            "mean_mae": None,
            "mean_bias": None,
            "mean_coefficient_of_variation": None,
        },
    }
    if not submitted_maps:
        result["warnings"].append("No submitted parameter maps with detectable map types were available for reference scoring.")
        return result
    if not roots:
        result["warnings"].append("No reference folder found; QC/map statistics only.")
        for submitted in submitted_maps:
            result["maps"].append({
                "submitted_file": submitted.get("file_name"),
                "detected_map_type": submitted.get("detected_map_type"),
                "status": "reference_not_available",
            })
        return result

    selected_root = next((root for root in roots if _reference_maps_by_type(root)), roots[0])
    refs_by_type = _reference_maps_by_type(selected_root)
    # Maps come from one root; masks come from all of them. See
    # _reference_masks_across_roots for why the two differ.
    masks = _reference_masks_across_roots(roots)
    result["reference_root"] = str(selected_root)
    result["mask_roots"] = sorted({str(Path(m["path"]).parent) for m in masks})
    result["mask_overlaps"] = _mask_overlaps(masks)
    result["masks_available"] = bool(masks)
    result["mask_count"] = len(masks)
    result["summary"]["reference_map_count"] = sum(len(v) for v in refs_by_type.values())
    if not masks:
        result["warnings"].append("No masks found; whole-volume reference metrics may be affected by background voxels.")

    compared_metrics = []
    for submitted in submitted_maps:
        map_type = str(submitted.get("detected_map_type"))
        submitted_path = Path(str(submitted.get("path") or ""))
        ref_path = _choose_reference_match(
            submitted_path, refs_by_type.get(map_type, []),
            submission_root=EXTRACTED_DIR / submission_id,
            reference_root=selected_root,
            challenge_type=challenge_type,
        )
        # Which scan this map came from. Without it every row in the table
        # below is labelled with the same filename as every other row.
        _dataset, _participant, _repeat, _site = _scan_identity(
            submitted_path, EXTRACTED_DIR / submission_id, challenge=challenge_type)
        row = {
            "submitted_file": submitted.get("file_name"),
            "submitted_path": str(submitted_path),
            "dataset": _dataset,
            "participant": _participant,
            "repeat": _repeat,
            "site": _site,
            "scan_label": _scan_label(_dataset, _participant, _repeat, _site),
            "detected_map_type": map_type,
            "parameter_label": submitted.get("parameter_label"),
            "units": submitted.get("units") or "units not provided",
            "reference_file": ref_path.name if ref_path else None,
            "reference_path": str(ref_path) if ref_path else None,
            "status": "reference_not_available",
            "whole_map": None,
            "masks": [],
            "difference_map": None,
        }
        if ref_path is None:
            result["maps"].append(row)
            continue

        try:
            sub_data = _load_nifti_values(submitted_path)
        except Exception as exc:
            row["status"] = "submitted_invalid"
            row["error"] = str(exc)
            result["maps"].append(row)
            continue

        try:
            ref_data = _load_nifti_values(ref_path)
        except Exception as exc:
            row["status"] = "reference_invalid"
            row["error"] = str(exc)
            result["maps"].append(row)
            continue

        # Recorded before any dimensionality or shape check rejects the map,
        # because a header mismatch is the most likely reason it was rejected
        # and the reviewer needs to see the numbers either way.
        row["header_check"] = _header_check(sub_data, ref_data)

        # Parameter maps (CBF/ATT) must be exactly the configured dimensionality
        # (3-D). A 4-D file with a parameter-map name is a fitted model / time
        # series, not a parameter map, and must not be scored voxelwise here.
        expected_dims = _expected_dimensions(map_type)
        sub_ndim = len(sub_data["shape"])
        row["submitted_dimensions"] = sub_ndim
        if expected_dims is not None and sub_ndim != expected_dims:
            row["status"] = "unexpected_dimensions"
            if sub_ndim == 4:
                row["file_role"] = "fitted_model"
                row["error"] = (
                    f"{map_type} map is {sub_ndim}-D; parameter maps must be {expected_dims}-D. "
                    "A 4-D file is treated as a fitted model / time series and is not scored "
                    "against the 3-D reference parameter map."
                )
            else:
                row["error"] = (
                    f"{map_type} map is {sub_ndim}-D; parameter maps must be {expected_dims}-D."
                )
            result["maps"].append(row)
            continue

        if sub_data["shape"] != ref_data["shape"]:
            row["status"] = "shape_mismatch"
            row["error"] = (
                f"Submitted shape {sub_data['shape']} does not match reference shape {ref_data['shape']}. "
                "Resampling is not performed yet."
            )
            row["submitted_shape"] = sub_data["shape"]
            row["reference_shape"] = ref_data["shape"]
            # TODO: add explicit, affine-aware optional resampling before enabling shape reconciliation.
            result["maps"].append(row)
            continue

        # Same shape is not enough: verify the submitted and reference maps share
        # a physical-space grid (affine + voxel size) before voxelwise scoring.
        # Two identically shaped volumes offset in space would otherwise produce
        # misleading metrics. We do not silently resample.
        grid_ok = _grids_compatible(sub_data, ref_data)
        if grid_ok is False:
            row["status"] = "spatial_grid_mismatch"
            row["error"] = (
                "Submitted and reference maps have the same shape but different spatial grids "
                "(affine/voxel size). Voxelwise comparison was skipped to avoid misleading metrics. "
                "Resampling is not performed yet."
            )
            row["submitted_voxel_size"] = sub_data.get("voxel_size")
            row["reference_voxel_size"] = ref_data.get("voxel_size")
            result["maps"].append(row)
            continue
        if grid_ok is None:
            row["grid_check"] = "unverified_no_affine"

        sub_values = sub_data["values"]
        ref_values = ref_data["values"]
        whole_metrics = _comparison_metrics(sub_values, ref_values)
        row["status"] = whole_metrics.get("status", "compared")
        row["whole_map"] = whole_metrics
        if whole_metrics.get("error"):
            row["error"] = whole_metrics.get("error")
        if whole_metrics.get("status") == "compared":
            compared_metrics.append(whole_metrics)

        # Difference map is only built when an artifact dir is provided (it is not
        # needed for the report/QC path), so compute it lazily here.
        if artifact_dir is not None:
            diff_dir = artifact_dir / "reference_difference_maps"
            diff_name = submitted_path.name
            if diff_name.endswith(".nii.gz"):
                diff_name = diff_name[:-7]
            elif diff_name.endswith(".nii"):
                diff_name = diff_name[:-4]
            try:
                source_key = submitted_path.relative_to(EXTRACTED_DIR / submission_id).as_posix()
            except ValueError:
                source_key = submitted_path.as_posix()
            scan_key = hashlib.sha256(source_key.encode("utf-8")).hexdigest()[:20]
            diff_path = diff_dir / f"{diff_name}_{scan_key}_difference.nii"
            try:
                diff_values = _difference_values(sub_values, ref_values)
                _write_float32_nifti(
                    diff_path, sub_data["shape"], diff_values, affine=sub_data.get("affine")
                )
                row["difference_map"] = str(diff_path.relative_to(artifact_dir))
            except Exception as exc:
                row["difference_map_error"] = str(exc)

        for mask in masks:
            mask_row = {
                "mask_name": mask["name"],
                "mask_label": mask["label"],
                "mask_path": str(mask["path"]),
                "status": "not_scored",
                "metrics": None,
            }
            try:
                mask_data = _load_nifti_values(mask["path"])
            except Exception as exc:
                mask_row["status"] = "mask_unreadable"
                mask_row["error"] = str(exc)
                row["masks"].append(mask_row)
                continue
            if mask_data["shape"] != sub_data["shape"]:
                mask_row["status"] = "shape_mismatch"
                mask_row["error"] = f"Mask shape {mask_data['shape']} does not match submitted/reference shape {sub_data['shape']}."
                row["masks"].append(mask_row)
                continue
            mask_grid_ok = _grids_compatible(mask_data, sub_data)
            if mask_grid_ok is False:
                mask_row["status"] = "spatial_grid_mismatch"
                mask_row["error"] = (
                    "Mask and submitted/reference maps have different physical grids "
                    "(affine/orientation or voxel size); the mask was not applied."
                )
                row["masks"].append(mask_row)
                continue
            if mask_grid_ok is None:
                mask_row["grid_check"] = "unverified_no_affine"
            selector = _mask_selector(mask_data["values"])
            mask_row["metrics"] = _comparison_metrics(sub_values, ref_values, selector)
            mask_row["status"] = mask_row["metrics"].get("status", "compared")
            row["masks"].append(mask_row)

        result["maps"].append(row)

    compared_count = len(compared_metrics)
    result["summary"]["compared_map_count"] = compared_count

    # Aggregate metrics PER MAP TYPE. CBF (mL/100g/min) and ATT (seconds) have
    # different units, so averaging their RMSE/MAE/bias together is meaningless.
    # Only same-map-type values (across subjects/repeats) may be averaged.
    by_map_type: dict[str, dict] = {}
    for map_row in result["maps"]:
        whole = map_row.get("whole_map") or {}
        if whole.get("status") != "compared":
            continue
        mt = str(map_row.get("detected_map_type") or "Unknown")
        bucket = by_map_type.setdefault(mt, {"rmse": [], "mae": [], "bias": [], "error_cov": []})
        if whole.get("rmse") is not None:
            bucket["rmse"].append(whole.get("rmse"))
        if whole.get("mae") is not None:
            bucket["mae"].append(whole.get("mae"))
        if whole.get("bias") is not None:
            bucket["bias"].append(whole.get("bias"))
        cov_val = whole.get("error_coefficient_of_variation", whole.get("coefficient_of_variation"))
        if cov_val is not None:
            bucket["error_cov"].append(cov_val)

    summary_by_map_type = {
        mt: {
            "units": next((str(s.get("units")) for s in _perfusion_map_types().values() if str(s.get("short")) == mt and s.get("units")), None),
            "compared_count": len(vals["rmse"]),
            "mean_rmse": _json_float(_mean(vals["rmse"])),
            "mean_mae": _json_float(_mean(vals["mae"])),
            "mean_bias": _json_float(_mean(vals["bias"])),
            "mean_error_coefficient_of_variation": _json_float(_mean(vals["error_cov"])),
        }
        for mt, vals in by_map_type.items()
    }
    result["summary"]["by_map_type"] = summary_by_map_type

    # Flat cross-map aggregates are only meaningful within a single map type/unit.
    # With more than one compared map type present, expose them as None (reports
    # render "Not available") and rely on by_map_type instead of mixing units.
    distinct_types = list(summary_by_map_type.keys())
    if len(distinct_types) == 1:
        only = summary_by_map_type[distinct_types[0]]
        result["summary"]["mean_rmse"] = only["mean_rmse"]
        result["summary"]["mean_mae"] = only["mean_mae"]
        result["summary"]["mean_bias"] = only["mean_bias"]
        result["summary"]["mean_coefficient_of_variation"] = only["mean_error_coefficient_of_variation"]
        result["summary"]["aggregate_map_type"] = distinct_types[0]
    else:
        result["summary"]["mean_rmse"] = None
        result["summary"]["mean_mae"] = None
        result["summary"]["mean_bias"] = None
        result["summary"]["mean_coefficient_of_variation"] = None
        if len(distinct_types) > 1:
            result["summary"]["aggregate_map_type"] = "mixed"
            result["warnings"].append(
                "Multiple map types were scored (e.g. CBF and ATT). They have different units, "
                "so no combined average is reported, see per-map-type results in summary.by_map_type."
            )

    # Repeatability/ICC require repeated (e.g. noise-varied) datasets, which a
    # single submitted map cannot provide. State this explicitly so the reported
    # coefficient of variation is never mistaken for a repeatability measure.
    result["repeatability_status"] = "unavailable_requires_repeated_datasets"
    result["metric_definitions"] = {
        "rmse": "Root-mean-square voxelwise error vs. reference (same units as the map).",
        "mae": "Mean absolute voxelwise error vs. reference.",
        "bias": "Mean signed voxelwise error (submitted minus reference).",
        "error_coefficient_of_variation": "Std. dev. of voxelwise errors divided by the reference mean; a spread-of-error ratio, NOT a repeatability CoV.",
        "correlation": "Pearson correlation between submitted and reference voxels.",
        "repeatability_cov": "Not computed: requires repeated (noise-varied) datasets provided by the challenge.",
        "icc": _icc_definition(challenge_type),
    }

    map_statuses = [str(m.get("status") or "") for m in result["maps"]]
    error_statuses = {
        "shape_mismatch",
        "spatial_grid_mismatch",
        "unexpected_dimensions",
        "scoring_error",
        "reference_invalid",
        "submitted_invalid",
        "no_finite_overlap",
    }
    if compared_count:
        result["available"] = True
        if compared_count == len(submitted_maps) and all(status == "compared" for status in map_statuses):
            result["status"] = "available"
        else:
            result["status"] = "partial_reference_scoring"
            result["warnings"].append(
                "Only some submitted parameter maps had valid reference comparisons; see per-map statuses."
            )
    elif any(status in error_statuses for status in map_statuses):
        result["status"] = "scoring_error"
    return result


def _write_reference_scoring_artifacts(artifact_dir: Path, reference_scoring: dict) -> list[str]:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    json_path = artifact_dir / "reference_scoring.json"
    json_path.write_text(json.dumps(reference_scoring, indent=2, default=str), encoding="utf-8")
    csv_path = artifact_dir / "reference_scoring.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=[
            "scan_label", "dataset", "participant", "site", "repeat",
            "submitted_file", "reference_file", "detected_map_type", "scope", "mask_name",
            "status", "voxel_count", "total_voxel_count", "finite_voxel_percent",
            "negative_voxel_percent", "bias", "mae", "rmse", "standard_deviation_error",
            "coefficient_of_variation", "correlation", "difference_map",
        ])
        writer.writeheader()
        for item in reference_scoring.get("maps") or []:
            whole = item.get("whole_map") or {}
            writer.writerow({
                "scan_label": item.get("scan_label"),
                "dataset": item.get("dataset"),
                "participant": item.get("participant"),
                "site": item.get("site"),
                "repeat": item.get("repeat"),
                "submitted_file": item.get("submitted_file"),
                "reference_file": item.get("reference_file"),
                "detected_map_type": item.get("detected_map_type"),
                "scope": "whole map",
                "mask_name": "",
                "status": item.get("status"),
                "voxel_count": whole.get("voxel_count"),
                "total_voxel_count": whole.get("total_voxel_count"),
                "finite_voxel_percent": whole.get("finite_voxel_percent"),
                "negative_voxel_percent": whole.get("negative_voxel_percent"),
                "bias": whole.get("bias"),
                "mae": whole.get("mae"),
                "rmse": whole.get("rmse"),
                "standard_deviation_error": whole.get("standard_deviation_error"),
                "coefficient_of_variation": whole.get("coefficient_of_variation"),
                "correlation": whole.get("correlation"),
                "difference_map": item.get("difference_map"),
            })
            for mask in item.get("masks") or []:
                metrics = mask.get("metrics") or {}
                writer.writerow({
                    "scan_label": item.get("scan_label"),
                    "dataset": item.get("dataset"),
                    "participant": item.get("participant"),
                    "site": item.get("site"),
                    "repeat": item.get("repeat"),
                    "submitted_file": item.get("submitted_file"),
                    "reference_file": item.get("reference_file"),
                    "detected_map_type": item.get("detected_map_type"),
                    "scope": mask.get("mask_label") or "mask",
                    "mask_name": mask.get("mask_name"),
                    "status": mask.get("status"),
                    "voxel_count": metrics.get("voxel_count"),
                    "total_voxel_count": metrics.get("total_voxel_count"),
                    "finite_voxel_percent": metrics.get("finite_voxel_percent"),
                    "negative_voxel_percent": metrics.get("negative_voxel_percent"),
                    "bias": metrics.get("bias"),
                    "mae": metrics.get("mae"),
                    "rmse": metrics.get("rmse"),
                    "standard_deviation_error": metrics.get("standard_deviation_error"),
                    "coefficient_of_variation": metrics.get("coefficient_of_variation"),
                    "correlation": metrics.get("correlation"),
                    "difference_map": item.get("difference_map"),
                })
    return [str(json_path.relative_to(artifact_dir)), str(csv_path.relative_to(artifact_dir))]


def _format_visible_value(label: str, raw_value, units: Optional[str] = None, total=None) -> str:
    if raw_value is None:
        return "not available"
    if label in {"Finite voxels", "Negative voxels"}:
        if total:
            return f"{_json_float(raw_value, 2)}%"
        return f"{_json_float(raw_value, 2)}%"
    if isinstance(raw_value, float):
        text = str(_json_float(raw_value, 3))
    else:
        text = str(raw_value)
    return f"{text} {units}" if units else text


def _build_nifti_summary(maps: list[dict]) -> dict:
    map_count = len(maps)
    total_voxel_count = sum(int((m.get("metadata") or {}).get("total_voxel_count") or 0) for m in maps)
    finite_voxel_count = sum(int((m.get("metadata") or {}).get("finite_voxel_count") or 0) for m in maps)
    nan_count = sum(int((m.get("metadata") or {}).get("nan_count") or 0) for m in maps)
    inf_count = sum(int((m.get("metadata") or {}).get("inf_count") or 0) for m in maps)
    negative_voxel_count = sum(int((m.get("stats") or {}).get("negative_voxel_count") or 0) for m in maps)

    detected = []
    for m in maps:
        mt = m.get("detected_map_type")
        if mt and mt != "Unknown" and mt not in detected:
            detected.append(str(mt))

    cv_values = [
        float((m.get("stats") or {}).get("coefficient_of_variation"))
        for m in maps
        if isinstance((m.get("stats") or {}).get("coefficient_of_variation"), (int, float))
    ]
    std_values = [
        float((m.get("stats") or {}).get("standard_deviation"))
        for m in maps
        if isinstance((m.get("stats") or {}).get("standard_deviation"), (int, float))
    ]

    means_by_map_type: dict[str, float | None] = {}
    std_by_map_type: dict[str, float | None] = {}
    for spec in _perfusion_map_types().values():
        short = str(spec.get("short") or "")
        if not short:
            continue
        mean_vals = [
            float((m.get("stats") or {}).get("mean"))
            for m in maps
            if m.get("detected_map_type") == short and isinstance((m.get("stats") or {}).get("mean"), (int, float))
        ]
        std_vals = [
            float((m.get("stats") or {}).get("standard_deviation"))
            for m in maps
            if m.get("detected_map_type") == short and isinstance((m.get("stats") or {}).get("standard_deviation"), (int, float))
        ]
        if mean_vals:
            means_by_map_type[short] = _json_float(_mean(mean_vals))
        if std_vals:
            std_by_map_type[short] = _json_float(_mean(std_vals))

    finite_percent = _pct(finite_voxel_count, total_voxel_count)
    negative_percent = _pct(negative_voxel_count, finite_voxel_count)
    mean_cv = _json_float(_mean(cv_values))
    mean_std = _json_float(_mean(std_values))

    visible_metrics = [
        {
            "label": "Map count",
            "value": str(map_count),
            "raw_key": "map_count",
            "raw_value": map_count,
        },
        {
            "label": "Finite voxels",
            "value": f"{_json_float(finite_percent, 2)}%" if finite_percent is not None else "not available",
            "raw_key": "finite_percent",
            "raw_value": finite_percent,
        },
        {
            "label": "Negative voxels",
            "value": f"{_json_float(negative_percent, 2)}%" if negative_percent is not None else "not available",
            "raw_key": "negative_voxel_percent",
            "raw_value": negative_percent,
        },
        {
            "label": "Spatial CoV (map variability)",
            "value": str(_json_float(mean_cv, 3)) if mean_cv is not None else "not available",
            "raw_key": "spatial_coefficient_of_variation",
            "raw_value": mean_cv,
        },
    ]

    for spec in _perfusion_map_types().values():
        short = str(spec.get("short") or "")
        if not short:
            continue
        label = f"Mean {short}"
        value = means_by_map_type.get(short)
        if value is None:
            continue
        units = spec.get("units") if isinstance(spec, dict) else None
        visible_metrics.append({
            "label": label,
            "value": _format_visible_value(label, value, str(units) if units else None),
            "raw_key": f"mean_{short.lower()}",
            "raw_value": value,
            "units": units,
        })
        if len(visible_metrics) >= 6:
            break

    if len(visible_metrics) < 6:
        visible_metrics.append({
            "label": "Standard deviation",
            "value": str(_json_float(mean_std, 3)) if mean_std is not None else "not available",
            "raw_key": "mean_standard_deviation",
            "raw_value": mean_std,
        })

    return {
        "map_count": map_count,
        "parameter_maps_detected": detected,
        "total_voxel_count": total_voxel_count,
        "finite_voxel_count": finite_voxel_count,
        "nan_count": nan_count,
        "inf_count": inf_count,
        "negative_voxel_count": negative_voxel_count,
        "finite_percent": finite_percent,
        "mean_finite_percent": finite_percent,
        "negative_voxel_percent": negative_percent,
        "mean_negative_voxel_percent": negative_percent,
        "mean_coefficient_of_variation": mean_cv,  # backward-compat (spatial CoV)
        "spatial_coefficient_of_variation": mean_cv,
        "coefficient_of_variation_kind": "spatial_map_variability",
        "mean_standard_deviation": mean_std,
        "means_by_map_type": means_by_map_type,
        "standard_deviation_by_map_type": std_by_map_type,
        "visible_metrics": visible_metrics[:6],
    }


_ANALYSIS_CACHE: dict[tuple, dict] = {}
_ANALYSIS_CACHE_LOCK = threading.Lock()


def clear_analysis_cache(*, on_disk: bool = False) -> None:
    """Drop memoised analyses. Called when configuration changes.

    The saved copies are keyed by a configuration fingerprint, so a config
    change makes them unreachable without deleting them; ``on_disk`` is for
    tests and for anyone who wants the folder actually emptied.
    """
    with _ANALYSIS_CACHE_LOCK:
        _ANALYSIS_CACHE.clear()
    if on_disk:
        for path in _analysis_cache_dir().glob("*.json"):
            try:
                path.unlink()
            except OSError:
                pass


def _analysis_cache_key(
    submission_id: str, challenge_type: str, files: list[Path],
) -> Optional[tuple]:
    """Identity of an analysis: its inputs, not just its submission id.

    Everything the result depends on and nothing else: the submitted maps, the
    ground truth and masks it is compared against, and the challenge
    configuration. Any of them changing produces a different key, so a stale
    answer cannot be served; none of them changing means the answer is the one
    already computed.

    Only file identity is read (path, size, mtime), never contents, so building
    a key costs a handful of stat calls against the ~60 seconds it can save.
    """
    from osipi_pipeline.ingestion.manifest import config_fingerprint

    def stamp(paths) -> tuple:
        out = []
        for path in sorted(paths, key=str):
            try:
                stat = Path(path).stat()
            except OSError:
                return ()
            out.append((str(path), int(stat.st_size), int(stat.st_mtime_ns)))
        return tuple(out)

    submitted = stamp(files)
    if not submitted:
        return None
    try:
        masks = stamp(m["path"] for m in masks_for_submission(submission_id, challenge_type))
        roots = _reference_roots(submission_id, challenge_type)
        references = stamp(
            path for root in roots
            for paths in _reference_maps_by_type(root).values()
            for path in paths
        )
    except Exception:  # noqa: BLE001 - a key we cannot build is simply no key
        return None
    # Invalidate cached results rounded to six decimals and old artifact names.
    return ("multi-model-icc-v3", submission_id, challenge_type, config_fingerprint(),
            submitted, masks, references)


#: Where memoised analyses live between runs, beside the validation results
#: they belong with.
def _analysis_cache_dir() -> Path:
    return OUTPUTS_DIR / "analysis_cache"


def _analysis_cache_file(submission_id: str, cache_key: tuple) -> Path:
    """One file per (submission, inputs) pair.

    The submission id leads the filename so a human can see what a file is
    for, and the digest of the full key follows so a changed input lands on a
    different file rather than overwriting a valid one.
    """
    digest = hashlib.sha1(
        json.dumps(cache_key, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return _analysis_cache_dir() / f"{_safe_name(submission_id)}.{digest}.json"


def _read_cached_analysis(path: Path) -> Optional[dict]:
    """A previously saved analysis, or nothing.

    Any failure to read is a cache miss, never an error: a truncated file from
    an interrupted write, a partial disk, or a file written by an older
    version of this code must all end in recomputing rather than in a broken
    report.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_cached_analysis(path: Path, result: dict, submission_id: str) -> None:
    """Save an analysis, replacing whatever this submission had before.

    Written to a temporary name and moved into place, so a reader never sees a
    half-written file. Older entries for the same submission are removed: a
    new key means the submitted maps, the references or the configuration
    changed, which makes every earlier entry unreachable rather than merely
    old, and keeping them would grow the folder for every re-upload.

    Failing to cache is not failing to analyse. A read-only disk or a full one
    costs the next reader some seconds and nothing else, so nothing here
    propagates.
    """
    directory = _analysis_cache_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.tmp")
        temporary.write_text(json.dumps(result, default=str), encoding="utf-8")
        temporary.replace(path)
    except (OSError, TypeError, ValueError):
        return
    prefix = f"{_safe_name(submission_id)}."
    for stale in directory.glob(f"{prefix}*.json"):
        if stale != path:
            try:
                stale.unlink()
            except OSError:
                pass


def _attach_scan_identity(maps: list[dict], submission_id: str, challenge_type: str) -> None:
    """Say which scan each analysed file came from, and what it is.

    The DCE-2026 layout reuses one set of filenames in every scan directory by
    design, so a QC table keyed on the map type prints "Ktrans" sixty times and
    a reader cannot tell one row from another -- nor which of them, on a mixed
    upload, belongs to which challenge.

    ``role_label`` exists for the same reason. A 4-D fitted concentration curve
    is not a parameter map, so map detection correctly declines to name one and
    the row read "Unknown" with every reference metric "Not available" -- which
    looks like a failure rather than a file that was never a parameter map.
    """
    root = EXTRACTED_DIR / submission_id
    challenge = (challenge_type or "").strip().lower()
    roles_by_path: dict[str, str] = {}
    try:
        for artifact in submission_artifacts(submission_id):
            label = _artifact_role_label(artifact)
            if label:
                roles_by_path[str(Path(str(artifact.path)).name).lower()] = label
    except Exception:
        _LOGGER.exception("Could not read artifact roles for %s", submission_id)

    for item in maps:
        path = Path(str(item.get("path") or ""))
        dataset, participant, repeat, site = _scan_identity(path, root, challenge=challenge)
        item.setdefault("dataset", dataset)
        item.setdefault("participant", participant)
        item.setdefault("repeat", repeat)
        item.setdefault("site", site)
        item["scan_label"] = _scan_label(dataset, participant, repeat, site)
        item["challenge_type"] = challenge
        detected = str(item.get("detected_map_type") or "").strip()
        if not detected or detected.lower() == "unknown":
            item["role_label"] = roles_by_path.get(path.name.lower())


def _artifact_role_label(artifact) -> Optional[str]:
    """A readable name for a file that is not a parameter map."""
    artifact_type = str(getattr(artifact, "artifact_type", "") or "").strip().lower()
    role = str(getattr(artifact, "role", "") or "").strip().lower()
    known = {
        "modelled_st": "Fitted signal (4-D)",
        "measured_st": "Measured signal (4-D)",
        "methods": "Methods document",
    }
    if artifact_type in known:
        return known[artifact_type]
    if role == "fitted_signal":
        return "Fitted signal (4-D)"
    if role == "measured_signal":
        return "Measured signal (4-D)"
    return None


def analyze_submission_niftis(
    submission_id: str,
    challenge_type: str,
    artifact_dir: Optional[Path] = None,
) -> dict:
    """Extract per-map scientific metadata and QC stats from output NIfTI maps.

    This includes descriptive QC/statistics and, when matching reference maps
    exist, reference-based comparison metrics. It is not official OSIPI scoring.

    The result is memoised on its inputs. Every export route, the report, the
    HTML and PDF renderers and the frontend each ask for this analysis, and on
    a real DCE submission it reads a gigabyte of 4-D data and takes about a
    minute: recomputing it per request made opening a report cost as much as
    producing it in the first place. ``artifact_dir`` runs are never served
    from cache, because those write difference maps and RSS volumes to disk as
    a side effect and the caller wants the files, not only the numbers.
    """
    files = _find_output_niftis(submission_id, challenge_type)

    cache_key = None
    if artifact_dir is None and performance_settings().get("analysis_cache_enabled", True):
        cache_key = _analysis_cache_key(submission_id, challenge_type, files)
        if cache_key is not None:
            with _ANALYSIS_CACHE_LOCK:
                cached = _ANALYSIS_CACHE.get(cache_key)
            if cached is not None:
                result = copy.deepcopy(cached)
                result["cache_hit"] = "memory"
                return result
            # Nothing in this process, but the work may already have been done
            # by a previous one. Restarting the app should not cost a minute
            # per submission to tell a reviewer what it told them yesterday.
            on_disk = _read_cached_analysis(_analysis_cache_file(submission_id, cache_key))
            if on_disk is not None:
                with _ANALYSIS_CACHE_LOCK:
                    _ANALYSIS_CACHE[cache_key] = copy.deepcopy(on_disk)
                on_disk["cache_hit"] = "disk"
                return on_disk

    maps = [_analyse_nifti_file(path) for path in files]
    _attach_scan_identity(maps, submission_id, challenge_type)
    reference_scoring = _score_reference_maps(submission_id, challenge_type, maps, artifact_dir)
    # ROI descriptive statistics are computed exactly once, here, using the
    # masks the reference scoring just discovered. Every downstream consumer
    # (API, JSON, CSV, HTML, PDF, frontend) reads the records off this result
    # rather than recomputing them.
    _attach_roi_descriptives(reference_scoring, submission_id, challenge_type)
    # Three independent annotations of the same ROI rows. Each has its own
    # enable switch, so none may be nested inside another: a nested call
    # inherits the outer function's early returns and silently stops running
    # in exactly the configurations where the outer feature is off.
    _attach_grouped_roi_statistics(reference_scoring, challenge_type)
    _attach_icc(
        reference_scoring, challenge_type,
        reference_scoring.get("roi_descriptive_statistics") or [],
    )
    _attach_threshold_flags(reference_scoring, challenge_type)
    _score_signal_rss(
        reference_scoring, submission_id, challenge_type, artifact_dir=artifact_dir
    )
    if artifact_dir is not None:
        try:
            _write_reference_scoring_artifacts(artifact_dir, reference_scoring)
        except Exception as exc:
            reference_scoring.setdefault("warnings", []).append(f"Could not write reference scoring artifacts: {exc}")
    errors = [
        {"file_name": m.get("file_name"), "error": m.get("error")}
        for m in maps
        if m.get("error")
    ]
    result = {
        "submission_id": submission_id,
        "challenge_type": challenge_type,
        "reference_based_scoring_available": bool(reference_scoring.get("available")),
        "reference_scoring": reference_scoring,
        "map_quality": "available" if maps else "no_nifti_maps_found",
        "maps": maps,
        "summary": _build_nifti_summary(maps),
        "errors": errors,
    }
    if cache_key is not None:
        with _ANALYSIS_CACHE_LOCK:
            _ANALYSIS_CACHE[cache_key] = copy.deepcopy(result)
        _write_cached_analysis(
            _analysis_cache_file(submission_id, cache_key), result, submission_id,
        )
    return result


def _attach_nifti_analysis(
    result: dict,
    submission_id: str,
    challenge_type: str,
    reference_based_scoring_available: bool = False,
    artifact_dir: Optional[Path] = None,
) -> dict:
    analysis = analyze_submission_niftis(submission_id, challenge_type, artifact_dir=artifact_dir)
    ref_available = bool(analysis.get("reference_based_scoring_available"))
    analysis["reference_based_scoring_available"] = ref_available
    result["nifti_analysis"] = analysis
    result["map_qc_summary"] = analysis.get("summary", {})
    result["reference_scoring"] = analysis.get("reference_scoring", {})
    result["reference_based_scoring_available"] = ref_available
    detail = result.get("metrics_detail")
    if isinstance(detail, dict):
        detail.setdefault("nifti_analysis", analysis)
        detail.setdefault("reference_scoring", analysis.get("reference_scoring", {}))
    elif detail is None:
        result["metrics_detail"] = {
            "nifti_analysis": analysis,
            "reference_scoring": analysis.get("reference_scoring", {}),
        }
    return result


def _with_nifti_status(
    payload: dict,
    submission_id: str,
    challenge_type: str,
    reference_based_scoring_available: bool = False,
) -> dict:
    analysis = analyze_submission_niftis(submission_id, challenge_type)
    ref_available = bool(analysis.get("reference_based_scoring_available"))
    analysis["reference_based_scoring_available"] = ref_available
    payload["nifti_analysis"] = analysis
    payload["map_qc_summary"] = analysis.get("summary", {})
    payload["reference_scoring"] = analysis.get("reference_scoring", {})
    payload["reference_based_scoring_available"] = ref_available
    return payload


# ---------------------------------------------------------------------------
# Infrastructure checks
# ---------------------------------------------------------------------------

def _check_tf62_infrastructure() -> dict:
    """Check TF6.2 provider infrastructure: script + reference NIfTI + masks.

    Does NOT check submission-specific outputs.
    Returns:
        all_present     : bool
        missing         : list[str]
        ref_nifti_count : int
        mask_count      : int
    """
    prov     = PROVIDERS["osipi_tf62_dce_ktrans"]
    script   = prov["script_file"]
    ref_dir  = prov["ref_data_dir"]
    masks_dir = prov["masks_dir"]
    missing: list[str] = []

    if not script.exists():
        missing.append("challengeScoring.py")

    ref_nifti: list[Path] = []
    if ref_dir.exists():
        ref_nifti = [f for f in ref_dir.rglob("*") if _is_nifti_path(f)]
    if not ref_nifti:
        missing.append("reference_data (NIfTI maps in DROKtransNifti/)")

    # Masks: prefer dedicated masks/ dir, fall back to reference/ search
    mask_files: list[Path] = []
    if masks_dir.exists():
        mask_files = [f for f in masks_dir.rglob("*") if _is_nifti_path(f)]
    if not mask_files and ref_dir.exists():
        mask_files = [
            f for f in ref_dir.rglob("*")
            if _is_nifti_path(f) and any(pattern in f.name.lower() for pattern in _MASK_NAME_PATTERNS)
        ]
    if not mask_files:
        missing.append("mask_files (NIfTI masks in Masks/ or DROKtransNifti/)")

    return {
        "all_present":      len(missing) == 0,
        "missing":          missing,
        "ref_nifti_count":  len(ref_nifti),
        "mask_count":       len(mask_files),
    }


def _check_codecollection_infrastructure() -> dict:
    """Check whether the CodeCollection CSV test data files are present."""
    prov      = PROVIDERS["osipi_codecollection_dce_testdata"]
    data_dir  = prov["test_data_dir"]
    expected  = prov["expected_csv_files"]

    if not data_dir.exists():
        return {
            "all_present":     False,
            "missing":         ["Test data directory not found"],
            "available_files": [],
        }

    available = sorted(f.name for f in data_dir.glob("*.csv"))
    not_found = [f for f in expected if f not in available]

    return {
        "all_present":     (len(not_found) == 0 and len(available) > 0),
        "missing":         not_found or (["No CSV files found"] if not available else []),
        "available_files": available,
    }


# ---------------------------------------------------------------------------
# all_providers_status(), infrastructure snapshot (no submission needed)
# ---------------------------------------------------------------------------

def all_providers_status() -> list[dict]:
    """Return infrastructure-level status for every registered provider and installed package."""
    result: list[dict] = []

    # ── Built-in: OSIPI TF6.2 DCE Ktrans ──────────────────────────────────────
    prov  = PROVIDERS["osipi_tf62_dce_ktrans"]
    infra = _check_tf62_infrastructure()
    result.append({
        "provider_id":     prov["provider_id"],
        "provider_name":   prov["provider_name"],
        "display_name":    prov["display_name"],
        "category":        "official",
        "official":        True,
        "not_for_scoring": False,
        "source":          "builtin",
        "status":          "ready" if infra["all_present"] else "not_configured",
        "message": (
            "All infrastructure requirements met. Ready to score."
            if infra["all_present"] else
            "Scoring script, reference data, or mask files are missing. "
            "See setup_note for instructions."
        ),
        "missing":          infra["missing"],
        "description":      prov["description"],
        "metrics":          prov["metrics"],
        "setup_note":       prov["setup_note"],
        "ref_nifti_count":  infra["ref_nifti_count"],
        "mask_count":       infra["mask_count"],
        "challenge_type":   prov["challenge_type"],
        "map_type":         prov["map_type"],
    })

    # ── Development provider ───────────────────────────────────────────────────
    prov = PROVIDERS["osipi_codecollection_dce_testdata"]
    cc   = _check_codecollection_infrastructure()
    result.append({
        "provider_id":     prov["provider_id"],
        "provider_name":   prov["provider_name"],
        "display_name":    prov["display_name"],
        "category":        "development",
        "official":        False,
        "not_for_scoring": True,
        "source":          "builtin",
        "status":          "dev_data_available" if cc["all_present"] else "not_configured",
        "message": (
            "Development test data available. NOT official challenge scoring."
            if cc["all_present"] else
            "CSV test data not found. See setup_note for instructions. Development provider only."
        ),
        "missing":          cc["missing"],
        "available_files":  cc["available_files"],
        "description":      prov["description"],
        "metrics":          [],
        "setup_note":       prov["setup_note"],
    })

    # ── Uploaded custom packages ───────────────────────────────────────────────
    for pkg in list_packages():
        status_info = pkg.get("status", {})
        result.append({
            "provider_id":     pkg["package_id"],
            "provider_name":   f"{pkg['name']} v{pkg['version']}",
            "display_name":    pkg["name"],
            "category":        "custom",
            "official":        bool(pkg.get("official", False)),
            "not_for_scoring": False,
            "source":          "package",
            "status":          "ready" if status_info.get("ready") else "not_configured",
            "message":         (
                "Package ready to score."
                if status_info.get("ready") else
                "Package installed but not fully configured. "
                + " ".join(status_info.get("missing", []))
            ),
            "missing":         status_info.get("missing", []),
            "description":     pkg.get("description", ""),
            "metrics":         pkg.get("metrics", []),
            "challenge_type":  pkg.get("challenge_type", ""),
            "map_type":        pkg.get("map_type", ""),
            "version":         pkg.get("version", ""),
            "entry_point":     pkg.get("entry_point", "scoring.py"),
            "call_mode":       pkg.get("call_mode", "standard"),
        })

    return result


# ---------------------------------------------------------------------------
# Submission-level prerequisite check (TF6.2 official provider)
# ---------------------------------------------------------------------------

_OSIPI_FNAME_RE = re.compile(
    r"^(Synthetic|Clinical)_P\d+_Visit\d+\.nii(\.gz)?$",
    re.IGNORECASE,
)


def _check_submission_prerequisites(
    submission_id: str,
    challenge_type: str,
    provider: dict,
) -> dict:
    """Check every prerequisite for scoring a single submission.

    Returns:
        all_present  : bool
        missing      : list[str]
        outputs_ready: bool   , execution produced ≥1 NIfTI file
        ktrans_compat: bool   , at least one file matches OSIPI naming pattern
        nifti_files  : list[Path]
    """
    # First: infrastructure
    infra   = _check_tf62_infrastructure()
    missing = list(infra["missing"])

    # Second: submission-specific outputs
    exec_out = _exec_output_dir(submission_id, challenge_type)
    if not exec_out.exists():
        missing.append("Execution outputs (run the submission first)")
        return {
            "all_present":   False,
            "missing":       missing,
            "outputs_ready": False,
            "ktrans_compat": False,
            "nifti_files":   [],
        }

    nifti_files   = [f for f in exec_out.rglob("*") if _is_nifti_path(f)]
    outputs_ready = len(nifti_files) > 0
    if not outputs_ready:
        missing.append("Execution outputs (no NIfTI files found, run the submission first)")

    ktrans_compat = any(_OSIPI_FNAME_RE.match(f.name) for f in nifti_files)
    if outputs_ready and not ktrans_compat:
        missing.append(
            "OSIPI-compatible output filenames "
            "(expected: Synthetic_P#_Visit#.nii or Clinical_P#_Visit#.nii)"
        )

    return {
        "all_present":   len(missing) == 0,
        "missing":       missing,
        "outputs_ready": outputs_ready,
        "ktrans_compat": ktrans_compat,
        "nifti_files":   nifti_files,
    }


# ---------------------------------------------------------------------------
# scoring_status(), per-submission
# ---------------------------------------------------------------------------

def scoring_status(
    submission_id: str,
    challenge_type: str,
    map_type: str,
    output_files: Optional[list[str]] = None,
    provider_id: Optional[str] = None,
) -> dict:
    """Return scoring status for a single submission.

    Checks the active configuration first:
    - mode="none"    → not_configured (scoring disabled by reviewer)
    - mode="builtin" → uses built-in TF6.2 provider (existing logic)
    - mode="custom"  → checks the active custom package readiness

    Never fabricates metric values. Returns status="not_configured" if any
    prerequisite is missing.
    """
    providers_snap = all_providers_status()

    # ── Check active config unless a specific provider_id was requested ────────
    if not provider_id:
        active = get_active_entry(challenge_type)
        mode = active.get("mode", "none")

        if mode == "none":
            nifti_out = _find_output_niftis(submission_id, challenge_type)
            return _with_nifti_status({
                "provider_id":    None,
                "provider_name":  "No scoring configured",
                "status":         "not_configured",
                "message":        "Scoring is not set up. A reviewer or admin can configure a scoring package in the Scoring Setup panel.",
                "missing":        [],
                "outputs_ready":  len(nifti_out) > 0,
                "outputs_count":  len(nifti_out),
                "score_result":   None,
                "providers":      providers_snap,
                "active_mode":    "none",
            }, submission_id, challenge_type)

        if mode == "custom":
            pkg_id = active.get("package_id")
            if not pkg_id:
                return _with_nifti_status({
                    "provider_id":   None,
                    "provider_name": "Custom package",
                    "status":        "not_configured",
                    "message":       "Custom scoring selected but no package is installed.",
                    "missing":       ["Upload a scoring package in the Scoring Setup panel."],
                    "outputs_ready": False,
                    "outputs_count": 0,
                    "score_result":  None,
                    "providers":     providers_snap,
                    "active_mode":   "custom",
                }, submission_id, challenge_type)
            manifest = get_package_manifest(pkg_id)
            if manifest is None:
                return _with_nifti_status({
                    "provider_id":   pkg_id,
                    "provider_name": "Custom package",
                    "status":        "not_configured",
                    "message":       f"Scoring package {pkg_id!r} not found. Re-upload it.",
                    "missing":       [f"Package {pkg_id!r} missing from packages directory."],
                    "outputs_ready": False,
                    "outputs_count": 0,
                    "score_result":  None,
                    "providers":     providers_snap,
                    "active_mode":   "custom",
                }, submission_id, challenge_type)
            readiness = check_package_ready(pkg_id)
            # Use _find_output_niftis so ASL results/maps/ are recognised as outputs
            nifti_out = _find_output_niftis(submission_id, challenge_type)
            # Check saved result
            saved = load_scoring_result(submission_id)
            if saved and saved.get("package_id") == pkg_id:
                ref_available = bool(saved.get("reference_based_scoring_available") or readiness.get("has_reference"))
                if not saved.get("nifti_analysis"):
                    _attach_nifti_analysis(saved, submission_id, challenge_type, ref_available)
                return _with_nifti_status({
                    "provider_id":   pkg_id,
                    "provider_name": f"{manifest['name']} v{manifest['version']}",
                    "status":        saved.get("status", "scored"),
                    "message":       saved.get("message", "Analysis complete."),
                    "missing":       [],
                    "outputs_ready": True,
                    "outputs_count": len(nifti_out),
                    "score_result":  saved,
                    "providers":     providers_snap,
                    "active_mode":   "custom",
                }, submission_id, challenge_type, ref_available)
            pkg_missing = list(readiness.get("missing", []))
            if pkg_missing:
                return _with_nifti_status({
                    "provider_id":   pkg_id,
                    "provider_name": f"{manifest['name']} v{manifest['version']}",
                    "status":        "not_configured",
                    "message":       "Custom package prerequisites not met.",
                    "missing":       pkg_missing,
                    "outputs_ready": len(nifti_out) > 0,
                    "outputs_count": len(nifti_out),
                    "score_result":  None,
                    "providers":     providers_snap,
                    "active_mode":   "custom",
                }, submission_id, challenge_type, bool(readiness.get("has_reference")))
            # Package is ready: return ready even if exec outputs don't exist yet
            return _with_nifti_status({
                "provider_id":   pkg_id,
                "provider_name": f"{manifest['name']} v{manifest['version']}",
                "status":        "ready",
                "message":       "Custom scoring package ready.",
                "missing":       [],
                "outputs_ready": len(nifti_out) > 0,
                "outputs_count": len(nifti_out),
                "score_result":  None,
                "providers":     providers_snap,
                "active_mode":   "custom",
            }, submission_id, challenge_type, bool(readiness.get("has_reference")))
        # mode == "builtin" → fall through to existing built-in logic below

    provider, err = _resolve_provider(provider_id, challenge_type, map_type)

    if provider is None:
        nifti_out = _find_output_niftis(submission_id, challenge_type)
        return _with_nifti_status({
            "provider_id":   None,
            "provider_name": "No official provider",
            "status":        "not_configured",
            "message":       err,
            "missing":       [],
            "outputs_ready": len(nifti_out) > 0,
            "outputs_count": len(nifti_out),
            "score_result":  None,
            "providers":     providers_snap,
        }, submission_id, challenge_type)

    pid = provider["provider_id"]

    # Already scored?
    saved = load_scoring_result(submission_id)
    if saved and saved.get("provider_id") == pid:
        out_count = len(_find_output_niftis(submission_id, challenge_type))
        ref_available = bool(
            saved.get("reference_based_scoring_available")
            or (saved.get("status") == "scored" and provider.get("official"))
        )
        if not saved.get("nifti_analysis"):
            _attach_nifti_analysis(saved, submission_id, challenge_type, ref_available)
        return _with_nifti_status({
            "provider_id":   pid,
            "provider_name": provider["provider_name"],
            "status":        saved.get("status", "scored"),
            "message":       saved.get("message", "Analysis complete."),
            "missing":       [],
            "outputs_ready": True,
            "outputs_count": out_count,
            "score_result":  saved,
            "providers":     providers_snap,
        }, submission_id, challenge_type, ref_available)

    # Prerequisite check
    pre = _check_submission_prerequisites(submission_id, challenge_type, provider)
    out_count = len(_find_output_niftis(submission_id, challenge_type))

    if not pre["all_present"]:
        return _with_nifti_status({
            "provider_id":   pid,
            "provider_name": provider["provider_name"],
            "status":        "not_configured",
            "message": (
                "DCE Ktrans scoring requires: the OSIPI TF6.2 scoring script, "
                "reference NIfTI maps, mask files, and correctly named Ktrans outputs."
            ),
            "missing":       pre["missing"],
            "outputs_ready": pre["outputs_ready"],
            "outputs_count": out_count,
            "score_result":  None,
            "providers":     providers_snap,
        }, submission_id, challenge_type)

    return _with_nifti_status({
        "provider_id":   pid,
        "provider_name": provider["provider_name"],
        "status":        "ready",
        "message":       "All prerequisites met. Ready to score.",
        "missing":       [],
        "outputs_ready": True,
        "outputs_count": out_count,
        "score_result":  None,
        "providers":     providers_snap,
    }, submission_id, challenge_type, True)


# ---------------------------------------------------------------------------
# Helpers for challengeScoring.py invocation
# ---------------------------------------------------------------------------

def _write_patched_runner(script: Path, entry_name: str) -> Path:
    """Write a temp copy of challengeScoring.py with entry_list set to [entry_name].

    challengeScoring.py uses no CLI arguments, it reads ``entry_list`` from
    its own source.  We patch it so only the specified submission is scored.
    The caller must delete the returned path when done.
    """
    source = script.read_text(encoding="utf-8")
    # Override entry_list and skip reproducibility (requires separate neutral dir)
    source = re.sub(
        r"^entry_list\s*=\s*\[.*?\]",
        f"entry_list = {repr([entry_name])}",
        source,
        flags=re.MULTILINE,
    )
    source = re.sub(
        r"^entry_list_rep\s*=\s*\[.*?\]",
        "entry_list_rep = []",
        source,
        flags=re.MULTILINE,
    )
    runner_path = script.parent / f"_runner_{entry_name}.py"
    runner_path.write_text(source, encoding="utf-8")
    return runner_path


def _setup_entry_directory(provider_dir: Path, entry_name: str, nifti_files: list) -> Path:
    """Create entryDirectories/{entry_name}/ and link/copy submission NIfTIs there.

    challengeScoring.py reads submission files from
    ``entryDirectories/{entry_name}/`` relative to its cwd (provider_dir).
    Uses symlinks where possible; falls back to file copy.
    """
    entry_dir = provider_dir / "entryDirectories" / entry_name
    entry_dir.mkdir(parents=True, exist_ok=True)
    for src in nifti_files:
        src_path = src if isinstance(src, Path) else Path(src)
        dst = entry_dir / src_path.name
        if dst.exists() or dst.is_symlink():
            dst.unlink()
        try:
            os.symlink(src_path.resolve(), dst)
        except OSError:
            shutil.copy2(src_path, dst)
    return entry_dir


def _parse_osipi_tabular(tabular_path: Path, entry_name: str) -> dict:
    """Parse OSIPI_score_tabular.txt and return metrics dict for entry_name.

    Expected format (tab-separated)::

        Team  Accuracy  Repeatability  Reproducibility  OSIPI score silver  OSIPI score gold
        team1  0.812     0.934          nan               75.8                nan

    Returns an empty dict if the file is missing or the entry is not found.
    NaN values are returned as ``None`` so they serialise cleanly to JSON.
    """
    if not tabular_path.exists():
        return {}
    try:
        lines = tabular_path.read_text(encoding="utf-8").splitlines()
        if len(lines) < 2:
            return {}
        headers = [h.strip() for h in lines[0].split("\t")]
        for line in lines[1:]:
            parts = [p.strip() for p in line.split("\t")]
            if not parts or parts[0] != entry_name:
                continue
            metrics: dict = {}
            for key, val in zip(headers[1:], parts[1:]):
                clean_key = key.lower().replace(" ", "_")
                try:
                    fval = float(val)
                    # Convert NaN to None for clean JSON serialisation
                    metrics[clean_key] = None if fval != fval else round(fval, 4)
                except (ValueError, TypeError):
                    metrics[clean_key] = val or None
            return metrics
    except Exception:
        pass
    return {}


# ---------------------------------------------------------------------------
# score_submission(), run the real scoring script
# ---------------------------------------------------------------------------

def score_submission(
    submission_id: str,
    challenge_type: str,
    map_type: str,
    provider_id: Optional[str] = None,
) -> dict:
    """Run scoring for a single submission.

    Dispatches to custom package, built-in TF6.2 provider, or returns
    not_configured based on the active configuration.

    Returns a result dict that is also written to
    data/outputs/scoring/{safe_id}_score.json.

    NEVER fabricates metric values. Returns status='not_configured'
    or status='not_ready' if any prerequisite is absent.
    """
    # ── Custom package dispatch ────────────────────────────────────────────────
    if not provider_id:
        active = get_active_entry(challenge_type)
        mode = active.get("mode", "none")
        if mode == "none":
            artifact_dir = _score_artifact_dir(submission_id)
            result = _attach_nifti_analysis({
                "success":    False,
                "submission_id": submission_id,
                "status":     "not_configured",
                "provider_id": None,
                "challenge_type": challenge_type,
                "map_type":    map_type,
                "message":    "Scoring is disabled. Configure a scoring provider in Scoring Setup.",
                "metrics":    {},
                "artifacts":  [],
            }, submission_id, challenge_type, artifact_dir=artifact_dir)
            result["artifacts"] = _collect_artifacts(artifact_dir)
            result["artifact_count"] = len(result["artifacts"])
            _save_scoring_result(submission_id, result)
            return result
        if mode == "custom":
            pkg_id = active.get("package_id")
            if not pkg_id:
                artifact_dir = _score_artifact_dir(submission_id)
                result = _attach_nifti_analysis({
                    "success":    False,
                    "submission_id": submission_id,
                    "status":     "not_configured",
                    "provider_id": None,
                    "challenge_type": challenge_type,
                    "map_type":    map_type,
                    "message":    "Custom scoring selected but no package is configured.",
                    "metrics":    {},
                    "artifacts":  [],
                }, submission_id, challenge_type, artifact_dir=artifact_dir)
                result["artifacts"] = _collect_artifacts(artifact_dir)
                result["artifact_count"] = len(result["artifacts"])
                _save_scoring_result(submission_id, result)
                return result
            exec_out   = _exec_output_dir(submission_id, challenge_type)
            score_out  = _score_artifact_dir(submission_id)
            result = run_package_scoring(pkg_id, submission_id, exec_out, score_out)
            manifest = get_package_manifest(pkg_id)
            readiness = check_package_ready(pkg_id)
            result.setdefault("challenge_type", challenge_type)
            result.setdefault("map_type", map_type)
            result.setdefault("official", bool((manifest or {}).get("official", False)))
            ref_available = bool(
                result.get("reference_based_scoring_available")
                or readiness.get("has_reference")
            )
            _attach_nifti_analysis(result, submission_id, challenge_type, ref_available, artifact_dir=score_out)
            result["artifacts"] = _collect_artifacts(score_out)
            result["artifact_count"] = len(result["artifacts"])
            _save_scoring_result(submission_id, result)
            return result
        # mode == "builtin" → fall through

    provider, err = _resolve_provider(provider_id, challenge_type, map_type)
    if provider is None:
        artifact_dir = _score_artifact_dir(submission_id)
        result = _attach_nifti_analysis({
            "success":    False,
            "submission_id": submission_id,
            "status":     "not_configured",
            "provider_id": provider_id or "none",
            "challenge_type": challenge_type,
            "map_type":    map_type,
            "message":    err,
            "metrics":    {},
            "artifacts":  [],
        }, submission_id, challenge_type, artifact_dir=artifact_dir)
        result["artifacts"] = _collect_artifacts(artifact_dir)
        result["artifact_count"] = len(result["artifacts"])
        _save_scoring_result(submission_id, result)
        return result

    pid = provider["provider_id"]
    pre = _check_submission_prerequisites(submission_id, challenge_type, provider)

    if not pre["all_present"]:
        result = {
            "success":       False,
            "submission_id": submission_id,
            "provider_id":   pid,
            "status":        "not_configured",
            "message":       "Prerequisites not met, see missing list.",
            "missing":       pre["missing"],
            "metrics":       {},
            "artifacts":     [],
        }
        artifact_dir = _score_artifact_dir(submission_id)
        _attach_nifti_analysis(result, submission_id, challenge_type, artifact_dir=artifact_dir)
        result["artifacts"] = _collect_artifacts(artifact_dir)
        result["artifact_count"] = len(result["artifacts"])
        _save_scoring_result(submission_id, result)
        return result

    provider_dir = provider["provider_dir"]
    script       = provider["script_file"]
    artifact_dir = _score_artifact_dir(submission_id)
    entry_name   = _safe_name(submission_id)

    # ── Set up challengeScoring.py entry directory ────────────────────────────
    # The script reads files from entryDirectories/{entry_name}/ relative to cwd.
    _setup_entry_directory(provider_dir, entry_name, pre["nifti_files"])
    # Neutral dir is required by the script (reproducibility); empty = skipped.
    (provider_dir / "entryDirectories" / f"{entry_name}_neutral").mkdir(
        parents=True, exist_ok=True
    )
    # scoringOutputs/ must exist before the script opens files for writing.
    (provider_dir / "scoringOutputs").mkdir(parents=True, exist_ok=True)

    # ── Write patched runner (entry_list overridden in source) ────────────────
    runner_path = _write_patched_runner(script, entry_name)

    scored_at = datetime.now(timezone.utc).isoformat()

    try:
        proc = subprocess.run(
            [sys.executable, str(runner_path)],
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(provider_dir),
        )

        # Parse structured metrics from tabular output file
        tabular_path = provider_dir / "scoringOutputs" / "OSIPI_score_tabular.txt"
        metrics = _parse_osipi_tabular(tabular_path, entry_name)

        # Copy scoring outputs into per-submission artifact dir for archival
        score_out_src = provider_dir / "scoringOutputs"
        if score_out_src.exists():
            shutil.copytree(score_out_src, artifact_dir / "scoringOutputs", dirs_exist_ok=True)

        artifacts = _collect_artifacts(artifact_dir)

        if proc.returncode != 0:
            result = {
                "success":        False,
                "submission_id":  submission_id,
                "provider_id":    pid,
                "challenge_type": challenge_type,
                "map_type":       map_type,
                "status":         "failed",
                "scored_at":      scored_at,
                "message":        "Scoring script exited with a non-zero return code.",
                "stdout":         proc.stdout[:4096],
                "stderr":         proc.stderr[:4096],
                "metrics":        {},
                "official":       bool(provider.get("official", False)),
                "artifacts":      artifacts,
                "artifact_count": len(artifacts),
            }
            _attach_nifti_analysis(result, submission_id, challenge_type, True, artifact_dir=artifact_dir)
            result["artifacts"] = _collect_artifacts(artifact_dir)
            result["artifact_count"] = len(result["artifacts"])
            _save_scoring_result(submission_id, result)
            return result

        result = {
            "success":        True,
            "submission_id":  submission_id,
            "provider_id":    pid,
            "challenge_type": challenge_type,
            "map_type":       map_type,
            "status":         "scored",
            "scored_at":      scored_at,
            "message": (
                "Analysis complete, metrics parsed."
                if metrics else
                "Analysis complete, artifacts saved. Metrics could not be parsed from output."
            ),
            "stdout":         proc.stdout[:4096],
            "metrics":        metrics,
            "official":       bool(provider.get("official", False)),
            "artifacts":      artifacts,
            "artifact_count": len(artifacts),
            "score_dir":      str(artifact_dir),
        }
        _attach_nifti_analysis(result, submission_id, challenge_type, True, artifact_dir=artifact_dir)
        result["artifacts"] = _collect_artifacts(artifact_dir)
        result["artifact_count"] = len(result["artifacts"])
        _save_scoring_result(submission_id, result)
        return result

    except subprocess.TimeoutExpired:
        result = {
            "success":       False,
            "submission_id": submission_id,
            "provider_id":   pid,
            "status":        "failed",
            "scored_at":     scored_at,
            "message":       "Scoring script timed out after 300 seconds.",
            "metrics":       {},
            "official":      bool(provider.get("official", False)),
            "artifacts":     [],
            "artifact_count": 0,
        }
        _attach_nifti_analysis(result, submission_id, challenge_type, True, artifact_dir=artifact_dir)
        result["artifacts"] = _collect_artifacts(artifact_dir)
        result["artifact_count"] = len(result["artifacts"])
        _save_scoring_result(submission_id, result)
        return result

    except Exception as exc:
        result = {
            "success":       False,
            "submission_id": submission_id,
            "provider_id":   pid,
            "status":        "failed",
            "scored_at":     scored_at,
            "message":       f"Unexpected error while running scoring script: {exc}",
            "metrics":       {},
            "official":      bool(provider.get("official", False)),
            "artifacts":     [],
            "artifact_count": 0,
        }
        _attach_nifti_analysis(result, submission_id, challenge_type, True, artifact_dir=artifact_dir)
        result["artifacts"] = _collect_artifacts(artifact_dir)
        result["artifact_count"] = len(result["artifacts"])
        _save_scoring_result(submission_id, result)
        return result

    finally:
        # Remove the patched runner: it's ephemeral
        runner_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Artifact + metric collection
# ---------------------------------------------------------------------------

def _collect_artifacts(score_dir: Path) -> list[str]:
    """Return a list of artifact filenames produced by the scoring script.

    Looks for JSON, CSV, PNG, PDF files in the output directory.
    Returns relative paths (relative to score_dir) as strings.
    """
    if not score_dir.exists():
        return []
    extensions = {".json", ".csv", ".png", ".pdf", ".txt", ".html", ".nii"}
    artifacts: list[str] = []
    for f in sorted(score_dir.rglob("*")):
        if f.is_file() and (f.suffix.lower() in extensions or f.name.lower().endswith(NIFTI_SUFFIXES)):
            artifacts.append(str(f.relative_to(score_dir)))
    return artifacts




# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------

def score_batch(
    submission_ids: list[str],
    challenge_type: str,
    map_type: str,
    provider_id: Optional[str] = None,
) -> list[dict]:
    """Score multiple submissions sequentially. One failure does not stop the rest."""
    results = []
    for sid in submission_ids:
        try:
            r = score_submission(sid, challenge_type, map_type, provider_id)
        except Exception as exc:
            r = {
                "success":       False,
                "submission_id": sid,
                "status":        "failed",
                "message":       f"Unexpected error: {exc}",
                "metrics":       {},
                "artifacts":     [],
            }
        r["submission_id"] = sid  # ensure it's always set
        results.append(r)
    return results


def batch_scoring_status(
    submission_ids: list[str],
    challenge_type: str,
    map_type: str,
    provider_id: Optional[str] = None,
) -> dict:
    """Aggregated scoring status for a list of submissions, plus provider snapshot."""
    results = []
    for sid in submission_ids:
        st = scoring_status(sid, challenge_type, map_type, provider_id=provider_id)
        st["submission_id"] = sid
        results.append(st)

    providers_snap = all_providers_status()
    provider, _    = _resolve_provider(provider_id, challenge_type, map_type)

    outputs_ready   = sum(1 for r in results if r.get("outputs_ready"))
    ready_to_score  = sum(1 for r in results if r.get("status") == "ready")
    scored_count    = sum(1 for r in results if r.get("status") == "scored")
    failed_count    = sum(1 for r in results if r.get("status") == "failed")

    return {
        "provider_id":     provider["provider_id"]   if provider else None,
        "provider_name":   provider["provider_name"] if provider else "No official provider",
        "total":           len(submission_ids),
        "outputs_ready":   outputs_ready,
        "ready_to_score":  ready_to_score,
        "scored":          scored_count,
        "failed":          failed_count,
        "results":         results,
        "providers":       providers_snap,
    }
