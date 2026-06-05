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
