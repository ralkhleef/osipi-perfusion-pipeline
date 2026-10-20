"""Canonical path definitions for the OSIPI pipeline backend."""

from pathlib import Path, PureWindowsPath

# This file lives at backend/services/path_config.py
# Project root is three directory levels up.
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

# --- data/ ---
DATA_DIR           = PROJECT_ROOT / "data"
REFERENCE_DATA_DIR = DATA_DIR / "reference_data"   # Zenodo downloads
OUTPUTS_DIR        = DATA_DIR / "outputs"           # web-app validation results
CONFIG_MANAGER_DIR = DATA_DIR / "configuration_manager"
CONFIG_VERSIONS_DIR = CONFIG_MANAGER_DIR / "versions"
CONFIG_ACTIVE_VERSION = CONFIG_MANAGER_DIR / "active.json"

# --- submissions/ ---
SUBMISSIONS_DIR = PROJECT_ROOT / "submissions"
INCOMING_DIR    = SUBMISSIONS_DIR / "incoming"      # uploaded ZIPs
EXTRACTED_DIR   = SUBMISSIONS_DIR / "extracted"     # extracted submission folders

# --- frontend ---
FRONTEND_DIR = PROJECT_ROOT / "frontend"


SCORING_DIR         = DATA_DIR / "scoring"
PROVIDERS_DIR       = SCORING_DIR / "providers"      # legacy built-in provider scripts + reference data
SCORING_OUTPUTS_DIR = OUTPUTS_DIR / "scoring"        # per-submission score artifacts + result JSON

# OSIPI TF6.2 DCE Ktrans built-in provider
# Place challengeScoring.py + DROKtransNifti/ + Masks/ inside this directory.
OSIPI_TF62_DIR = PROVIDERS_DIR / "osipi_tf62_dce_ktrans"

# OSIPI CodeCollection dev/test-data provider  (NOT used for official scoring)
CODECOLLECTION_DIR = PROVIDERS_DIR / "osipi_codecollection_dce"

# Uploaded / admin-installed scoring packages (one sub-dir per package_id)
SCORING_PACKAGES_DIR = SCORING_DIR / "packages"

# Per-challenge-type active scoring configuration (which mode/package is active)
SCORING_ACTIVE_CONFIG = SCORING_DIR / "active.json"

# Backward-compatible alias so any code that still imports SCORING_RESULTS_DIR keeps working
SCORING_RESULTS_DIR = SCORING_OUTPUTS_DIR


def safe_relative_path(raw_path: str) -> Path:
    """Sanitize an archive or download file path to a safe relative Path."""
    normalized = (raw_path or "").replace("\\", "/")
    path = Path(normalized)
    if path.is_absolute() or PureWindowsPath(normalized).is_absolute():
        raise ValueError(f"Unsafe file path rejected: {raw_path!r}")
    parts = [
        part for part in path.parts
        if part not in ("", ".", "/") and part != path.anchor
    ]
    if not parts or any(part == ".." for part in parts):
        raise ValueError(f"Unsafe file path rejected: {raw_path!r}")
    return Path(*parts)
