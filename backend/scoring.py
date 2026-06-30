"""backend/scoring.py — Scoring framework for OSIPI pipeline.

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

        The script uses no CLI arguments — it reads hardcoded relative paths
        (entryDirectories/, DROKtransNifti/, Masks/, scoringOutputs/) from its
        cwd.  score_submission() patches entry_list in the script source and
        runs it with cwd=provider_dir.

    osipi_codecollection_dce_testdata   [DEVELOPMENT ONLY — never runs scoring]
        CSV test data from OSIPI/DCE-DSC-MRI_CodeCollection.
        Used only to test provider-discovery UI. NOT for scoring NIfTI maps.

This module NEVER returns or fabricates metric values.
"""

from __future__ import annotations

import csv
import json
import gzip
import math
import os
import re
import shutil
import subprocess
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
        # Paths — derived from OSIPI_TF62_DIR
        "provider_dir":  OSIPI_TF62_DIR,
        "script_file":   OSIPI_TF62_DIR / "challengeScoring.py",
        # challengeScoring.py uses hardcoded relative paths from its cwd.
        # Directory names must match exactly (case-sensitive on Linux).
        "ref_data_dir":  OSIPI_TF62_DIR / "DROKtransNifti",
        "masks_dir":     OSIPI_TF62_DIR / "Masks",
        "setup_note": (
            "Place the following inside "
            "data/scoring/providers/osipi_tf62_dce_ktrans/ to enable scoring:\n"
            "  challengeScoring.py  — from OSIPI/TF6.2_DCE-DSC-MRI_Challenges Scoring/\n"
            "  DROKtransNifti/      — DRO Ktrans NIfTI maps "
            "(additionalDROData/NIfTI/ from the same repo)\n"
            "  Masks/               — NIfTI mask files (Scoring/Masks/ from the same repo)"
        ),
    },

    # ── Development / test-data only ─────────────────────────────────────────
    "osipi_codecollection_dce_testdata": {
        "provider_id":   "osipi_codecollection_dce_testdata",
        "display_name":  "OSIPI CodeCollection Test Data",
        "provider_name": "OSIPI DCE/DSC CodeCollection — Test Data",
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


def get_provider_by_id(provider_id: str) -> Optional[dict]:
    """Return a provider dict by its exact provider_id, or None."""
    return PROVIDERS.get(provider_id)


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
# Path helpers — mirror docker_runner._safe_name
# ---------------------------------------------------------------------------

def _safe_name(value: str) -> str:
    """Convert to filesystem-safe lowercase-hyphenated form."""
    safe = "".join(c.lower() if c.isalnum() else "-" for c in value)
    return "-".join(part for part in safe.split("-") if part) or "submission"


def _exec_output_dir(submission_id: str, challenge_type: str) -> Path:
    """Return the outputs/ dir written by execution_service for this submission."""
    key = f"{_safe_name(challenge_type)}_{_safe_name(submission_id)}"
    return OUTPUTS_DIR / "execution" / key / "outputs"


def _find_output_niftis(submission_id: str, challenge_type: str) -> list[Path]:
    """Return NIfTI files that represent the output maps for this submission.

    Priority order:
    1. Docker execution output dir  (OUTPUTS_DIR/execution/{key}/outputs/)
    2. Submitted results/maps/      (EXTRACTED_DIR/{id}/results/maps/)
    3. Submitted results/           (EXTRACTED_DIR/{id}/results/)
    4. Extracted submission root    (EXTRACTED_DIR/{id}/)

    This ensures ASL result-only submissions (which put maps in results/maps/)
    are treated as having output maps available, without requiring Docker execution.
    """
    exec_dir = _exec_output_dir(submission_id, challenge_type)
    if exec_dir.exists():
        niftis = [f for f in exec_dir.rglob("*") if (f.suffix == ".nii" or f.name.endswith(".nii.gz")) and f.is_file()]
        if niftis:
            return niftis

    extracted_base = EXTRACTED_DIR / submission_id
    for subpath in ("results/maps", "results", ""):
        candidate = extracted_base / subpath if subpath else extracted_base
        if candidate.exists():
            niftis = [f for f in candidate.rglob("*") if (f.suffix == ".nii" or f.name.endswith(".nii.gz")) and f.is_file()]
            if niftis:
                return niftis

    return []


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
        _scoring_result_path(submission_id).write_text(
            json.dumps(result, indent=2, default=str), encoding="utf-8"
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# NIfTI map metadata + QC analysis
# ---------------------------------------------------------------------------

_PERFUSION_MAP_TYPES: dict[str, dict[str, object]] = {
    "cbf": {
        "short": "CBF",
        "label": "Cerebral blood flow",
        "units": "mL/100g/min",
        "tokens": {"cbf", "cerebralbloodflow"},
    },
    "att": {
        "short": "ATT",
        "label": "Arterial transit time",
        "units": "seconds",
        "tokens": {"att", "arterialtransittime", "arrivaltransittime"},
    },
    "ktrans": {
        "short": "Ktrans",
        "label": "Volume transfer constant",
        "units": "min^-1",
        "tokens": {"ktrans", "k-trans", "k_trans"},
    },
    "ve": {
        "short": "ve",
        "label": "Extravascular extracellular volume fraction",
        "units": None,
        "tokens": {"ve", "v_e"},
    },
    "kep": {
        "short": "Kep",
        "label": "Rate constant",
        "units": None,
        "tokens": {"kep"},
    },
    "vp": {
        "short": "Vp",
        "label": "Plasma volume fraction",
        "units": None,
        "tokens": {"vp"},
    },
}

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


def _json_float(value, ndigits: int = 6):
    """Return a JSON-safe rounded float, or None for NaN/inf/missing values."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return round(f, ndigits)


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
    name = path.name.lower()
    if name.endswith(".nii.gz"):
        stem = name[:-7]
    elif name.endswith(".nii"):
        stem = name[:-4]
    else:
        stem = path.stem.lower()

    tokenized = re.sub(r"[^a-z0-9]+", " ", stem).strip()
    tokens = set(tokenized.split())
    compact = tokenized.replace(" ", "")

    for key in ("cbf", "att", "ktrans", "ve", "kep", "vp"):
        spec = _PERFUSION_MAP_TYPES[key]
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


def _analyse_nifti_with_nibabel(path: Path) -> dict:
    import numpy as np  # type: ignore
    import nibabel as nib  # type: ignore

    img = nib.load(str(path))
    data = np.asarray(img.dataobj, dtype=np.float64)
    flat = data.ravel()
    total_voxel_count = int(flat.size)

    finite_mask = np.isfinite(flat)
    finite_values = flat[finite_mask]
    finite_count = int(finite_values.size)
    nan_count = int(np.isnan(flat).sum())
    inf_count = int(np.isinf(flat).sum())
    negative_count = int((finite_values < 0).sum()) if finite_count else 0

    if finite_count:
        mean = float(np.mean(finite_values))
        median = float(np.median(finite_values))
        std = float(np.std(finite_values))
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
        f for f in root.rglob("*")
        if f.is_file() and (f.suffix == ".nii" or f.name.endswith(".nii.gz"))
    )


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
        return {
            "shape": [int(v) for v in data.shape],
            "values": [float(v) for v in data.ravel()],
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
        return {"shape": shape, "values": values, "reader": "nifti_header_fallback"}


def _write_float32_nifti(path: Path, shape: list[int], values: list[float]) -> None:
    """Write a minimal NIfTI-1 float32 map.

    TODO: preserve source affine/header when adding safe resampling support.
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
    for i in range(1, min(ndim, 3) + 1):
        header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)
    path.parent.mkdir(parents=True, exist_ok=True)
    safe_values = [
        float(v) if isinstance(v, (int, float)) and math.isfinite(float(v)) else float("nan")
        for v in values
    ]
    path.write_bytes(bytes(header) + b"\x00\x00\x00\x00" + struct.pack(f"<{len(safe_values)}f", *safe_values))


def _filename_tokens(path: Path) -> set[str]:
    name = path.name.lower()
    if name.endswith(".nii.gz"):
        name = name[:-7]
    elif name.endswith(".nii"):
        name = name[:-4]
    return set(t for t in re.split(r"[^a-z0-9]+", name) if t)


def _is_mask_like(path: Path) -> bool:
    name = path.name.lower()
    parts = {part.lower() for part in path.parts}
    return (
        "masks" in parts
        or "mask" in parts
        or any(token in name for token in ("mask", "roi", "gray", "grey", "white", "gm", "wm"))
    )


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

    seen: set[str] = set()
    existing: list[Path] = []
    for root in roots:
        try:
            resolved = str(root.resolve())
        except Exception:
            resolved = str(root)
        if resolved in seen or not root.exists():
            continue
        seen.add(resolved)
        existing.append(root)
    return existing


def _reference_maps_by_type(root: Path) -> dict[str, list[Path]]:
    map_dirs = [root / "maps", root / "Maps", root]
    by_type: dict[str, list[Path]] = {}
    for map_dir in map_dirs:
        for path in _nifti_file_list(map_dir):
            if _is_mask_like(path):
                continue
            detected = _detect_map_type(path).get("detected_map_type")
            if not detected or detected == "Unknown":
                continue
            by_type.setdefault(str(detected), [])
            if path not in by_type[str(detected)]:
                by_type[str(detected)].append(path)
    return by_type


def _reference_masks(root: Path) -> list[dict]:
    mask_dirs = [root / "masks", root / "Masks"]
    paths: list[Path] = []
    for mask_dir in mask_dirs:
        paths.extend(_nifti_file_list(mask_dir))
    if not paths:
        paths = [p for p in _nifti_file_list(root) if _is_mask_like(p)]

    masks = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        name = path.name
        low = name.lower()
        if "gray" in low or "grey" in low or re.search(r"(^|[_-])gm([_.-]|$)", low):
            label = "gray matter"
        elif "white" in low or re.search(r"(^|[_-])wm([_.-]|$)", low):
            label = "white matter"
        elif "brain" in low:
            label = "brain mask"
        elif "roi" in low:
            label = name.replace(".nii.gz", "").replace(".nii", "").replace("_", " ")
        else:
            label = name.replace(".nii.gz", "").replace(".nii", "").replace("_", " ")
        masks.append({"name": name, "label": label, "path": path})
    return masks


def _choose_reference_match(submitted_path: Path, candidates: list[Path]) -> Optional[Path]:
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    sub_tokens = _filename_tokens(submitted_path)
    best = sorted(
        candidates,
        key=lambda p: (len(sub_tokens.intersection(_filename_tokens(p))), -len(str(p))),
        reverse=True,
    )
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
        "coefficient_of_variation": _json_float(cov),
        "correlation": _json_float(_correlation(sub_finite, ref_finite)),
    }


def _score_reference_maps(
    submission_id: str,
    challenge_type: str,
    maps: list[dict],
    artifact_dir: Optional[Path] = None,
) -> dict:
    roots = _reference_roots(submission_id, challenge_type)
    submitted_maps = [m for m in maps if m.get("detected_map_type") and m.get("detected_map_type") != "Unknown"]
    result = {
        "status": "reference_not_available",
        "available": False,
        "reference_root": None,
        "masks_available": False,
        "mask_count": 0,
        "warnings": [],
        "maps": [],
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
    masks = _reference_masks(selected_root)
    result["reference_root"] = str(selected_root)
    result["masks_available"] = bool(masks)
    result["mask_count"] = len(masks)
    result["summary"]["reference_map_count"] = sum(len(v) for v in refs_by_type.values())
    if not masks:
        result["warnings"].append("No masks found; whole-volume reference metrics may be affected by background voxels.")

    compared_metrics = []
    for submitted in submitted_maps:
        map_type = str(submitted.get("detected_map_type"))
        submitted_path = Path(str(submitted.get("path") or ""))
        ref_path = _choose_reference_match(submitted_path, refs_by_type.get(map_type, []))
        row = {
            "submitted_file": submitted.get("file_name"),
            "submitted_path": str(submitted_path),
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

        sub_values = sub_data["values"]
        ref_values = ref_data["values"]
        diff_values = [
            (float(s) - float(r)) if math.isfinite(float(s)) and math.isfinite(float(r)) else float("nan")
            for s, r in zip(sub_values, ref_values)
        ]
        whole_metrics = _comparison_metrics(sub_values, ref_values)
        row["status"] = whole_metrics.get("status", "compared")
        row["whole_map"] = whole_metrics
        if whole_metrics.get("error"):
            row["error"] = whole_metrics.get("error")
        if whole_metrics.get("status") == "compared":
            compared_metrics.append(whole_metrics)

        if artifact_dir is not None:
            diff_dir = artifact_dir / "reference_difference_maps"
            diff_name = submitted_path.name
            if diff_name.endswith(".nii.gz"):
                diff_name = diff_name[:-7]
            elif diff_name.endswith(".nii"):
                diff_name = diff_name[:-4]
            diff_path = diff_dir / f"{diff_name}_difference.nii"
            try:
                _write_float32_nifti(diff_path, sub_data["shape"], diff_values)
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
            selector = [
                isinstance(v, (int, float)) and math.isfinite(float(v)) and float(v) > 0
                for v in mask_data["values"]
            ]
            mask_row["metrics"] = _comparison_metrics(sub_values, ref_values, selector)
            mask_row["status"] = mask_row["metrics"].get("status", "compared")
            row["masks"].append(mask_row)

        result["maps"].append(row)

    compared_count = len(compared_metrics)
    result["summary"]["compared_map_count"] = compared_count
    result["summary"]["mean_rmse"] = _json_float(_mean([m.get("rmse") for m in compared_metrics if m.get("rmse") is not None]))
    result["summary"]["mean_mae"] = _json_float(_mean([m.get("mae") for m in compared_metrics if m.get("mae") is not None]))
    result["summary"]["mean_bias"] = _json_float(_mean([m.get("bias") for m in compared_metrics if m.get("bias") is not None]))
    result["summary"]["mean_coefficient_of_variation"] = _json_float(_mean([
        m.get("coefficient_of_variation") for m in compared_metrics if m.get("coefficient_of_variation") is not None
    ]))
    map_statuses = [str(m.get("status") or "") for m in result["maps"]]
    error_statuses = {
        "shape_mismatch",
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
            "submitted_file", "reference_file", "detected_map_type", "scope", "mask_name",
            "status", "voxel_count", "total_voxel_count", "finite_voxel_percent",
            "negative_voxel_percent", "bias", "mae", "rmse", "standard_deviation_error",
            "coefficient_of_variation", "correlation", "difference_map",
        ])
        writer.writeheader()
        for item in reference_scoring.get("maps") or []:
            whole = item.get("whole_map") or {}
            writer.writerow({
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
    for short in ("CBF", "ATT", "Ktrans"):
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
            "label": "Coefficient of variation",
            "value": str(_json_float(mean_cv, 3)) if mean_cv is not None else "not available",
            "raw_key": "mean_coefficient_of_variation",
            "raw_value": mean_cv,
        },
    ]

    for short, label in (("CBF", "Mean CBF"), ("ATT", "Mean ATT"), ("Ktrans", "Mean Ktrans")):
        value = means_by_map_type.get(short)
        if value is None:
            continue
        spec = next((v for v in _PERFUSION_MAP_TYPES.values() if v["short"] == short), {})
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
        "mean_coefficient_of_variation": mean_cv,
        "mean_standard_deviation": mean_std,
        "means_by_map_type": means_by_map_type,
        "standard_deviation_by_map_type": std_by_map_type,
        "visible_metrics": visible_metrics[:6],
    }


def analyze_submission_niftis(
    submission_id: str,
    challenge_type: str,
    artifact_dir: Optional[Path] = None,
) -> dict:
    """Extract per-map scientific metadata and QC stats from output NIfTI maps.

    This includes descriptive QC/statistics and, when matching reference maps
    exist, reference-based comparison metrics. It is not official OSIPI scoring.
    """
    files = _find_output_niftis(submission_id, challenge_type)
    maps = [_analyse_nifti_file(path) for path in files]
    reference_scoring = _score_reference_maps(submission_id, challenge_type, maps, artifact_dir)
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
    return {
        "submission_id": submission_id,
        "challenge_type": challenge_type,
        "reference_based_scoring_available": bool(reference_scoring.get("available")),
        "reference_scoring": reference_scoring,
        "map_quality": "available" if maps else "no_nifti_maps_found",
        "maps": maps,
        "summary": _build_nifti_summary(maps),
        "errors": errors,
    }


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
        ref_nifti = [
            f for f in ref_dir.rglob("*")
            if (f.suffix == ".nii" or f.name.endswith(".nii.gz")) and f.is_file()
        ]
    if not ref_nifti:
        missing.append("reference_data (NIfTI maps in DROKtransNifti/)")

    # Masks: prefer dedicated masks/ dir, fall back to reference/ search
    mask_files: list[Path] = []
    if masks_dir.exists():
        mask_files = [f for f in masks_dir.rglob("*") if f.is_file()]
    if not mask_files and ref_dir.exists():
        mask_files = [
            f for f in ref_dir.rglob("*")
            if "mask" in f.name.lower() and f.is_file()
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
# all_providers_status() — infrastructure snapshot (no submission needed)
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
            "official":        False,
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
        outputs_ready: bool    — execution produced ≥1 NIfTI file
        ktrans_compat: bool    — at least one file matches OSIPI naming pattern
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

    nifti_files   = [f for f in exec_out.rglob("*") if (f.suffix == ".nii" or f.name.endswith(".nii.gz")) and f.is_file()]
    outputs_ready = len(nifti_files) > 0
    if not outputs_ready:
        missing.append("Execution outputs (no NIfTI files found — run the submission first)")

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
# scoring_status() — per-submission
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
                    "message":       saved.get("message", "Scoring complete."),
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
            # Package is ready — return ready even if exec outputs don't exist yet
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
            "message":       saved.get("message", "Scoring complete."),
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

    challengeScoring.py uses no CLI arguments — it reads ``entry_list`` from
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
# score_submission() — run the real scoring script
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
            "message":       "Prerequisites not met — see missing list.",
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
                "Scoring complete — metrics parsed."
                if metrics else
                "Scoring complete — artifacts saved. Metrics could not be parsed from output."
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
        # Remove the patched runner — it's ephemeral
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
        if f.is_file() and (f.suffix.lower() in extensions or f.name.endswith(".nii.gz")):
            artifacts.append(str(f.relative_to(score_dir)))
    return artifacts


def _parse_metrics_from_artifacts(score_dir: Path) -> dict:
    """Parse metric values from JSON files written by the scoring script.

    Searches score_dir and its scoringOutputs/ subdirectory.
    Returns an empty dict (never fabricates values) if nothing is found.
    """
    if not score_dir.exists():
        return {}
    metrics: dict = {}
    # Try scoringOutputs/ first (common OSIPI script output convention)
    search_dirs = [score_dir / "scoringOutputs", score_dir]
    for search in search_dirs:
        if not search.exists():
            continue
        for jf in sorted(search.glob("*.json")):
            try:
                data = json.loads(jf.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    metrics.update(data)
            except Exception:
                pass
        if metrics:
            break
    return metrics


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
