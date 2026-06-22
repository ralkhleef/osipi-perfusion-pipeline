"""Canonical path definitions for the OSIPI pipeline backend.

All paths are absolute and computed from this file's location, so the
server works correctly no matter which directory uvicorn is started from.

Usage:
    from services.path_config import INCOMING_DIR, REFERENCE_DATA_DIR, ...
"""

from pathlib import Path

# This file lives at backend/services/path_config.py
# Project root is three directory levels up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# --- data/ ---
DATA_DIR           = PROJECT_ROOT / "data"
REFERENCE_DATA_DIR = DATA_DIR / "reference_data"   # Zenodo downloads
OUTPUTS_DIR        = DATA_DIR / "outputs"           # web-app validation results

# --- submissions/ ---
SUBMISSIONS_DIR = PROJECT_ROOT / "submissions"
INCOMING_DIR    = SUBMISSIONS_DIR / "incoming"      # uploaded ZIPs
EXTRACTED_DIR   = SUBMISSIONS_DIR / "extracted"     # extracted submission folders
VALIDATED_DIR   = SUBMISSIONS_DIR / "validated"     # reserved for future use

# --- frontend ---
FRONTEND_DIR = PROJECT_ROOT / "frontend"


# ---------------------------------------------------------------------------
# Shared path-safety utility
# ---------------------------------------------------------------------------


def safe_relative_path(raw_path: str) -> Path:
    """Sanitize an archive or download file path to a safe relative Path.

    Rejects absolute paths and any component that is ``..``.
    Raises ``ValueError`` if the path is empty or unsafe.

    Used by both the ZIP extractor and the Zenodo downloader so that
    the traversal-blocking logic is defined in exactly one place.
    """
    path = Path((raw_path or "").replace("\\", "/"))
    parts = [
        part for part in path.parts
        if part not in ("", ".", "/") and part != path.anchor
    ]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe file path rejected: {raw_path!r}")
    return Path(*parts)
