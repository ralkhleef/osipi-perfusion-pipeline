"""backend/services/scoring_package_service.py, Scoring package manager.

A scoring package is a ZIP archive or directory containing:
    manifest.json     (required)
    scoring.py        (entry point, filename from manifest.entry_point)
    reference/        (optional: reference NIfTI maps / ground-truth data)
    masks/            (optional: mask NIfTI files)
    README.md         (optional)

manifest.json required fields:
    package_id     , filesystem-safe identifier, e.g. "my_challenge_v1"
    name           , human-readable display name
    challenge_type , any challenge id configured in config/validation_rules.yaml

manifest.json optional fields:
    version        , semver string, default "1.0.0"
    map_type       , configured map id/display, e.g. "ktrans" or "cbf"
    description    , free-text description
    metrics        , list of metric names the script produces
    entry_point    , filename of the scoring script, default "scoring.py"
    call_mode      , "standard" (default) | "osipi_cwd"
                      "standard"  → python scoring.py --submission-dir <dir>
                                      --output-dir <dir> [--reference-dir <dir>]
                      "osipi_cwd" → run with cwd=package_dir; script reads
                                    hardcoded relative paths (legacy TF6.2 style)

Active configuration (data/scoring/active.json):
    {
      "<challenge_id>": { "mode": "none" | "builtin" | "custom", "package_id": null | "..." }
    }

NEVER fabricates scoring results.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from services.path_config import (
    EXTRACTED_DIR,
    SCORING_ACTIVE_CONFIG,
    SCORING_OUTPUTS_DIR,
    SCORING_PACKAGES_DIR,
)
from osipi_pipeline.config.rules import challenge_types, default_challenge_type, output_map_subpaths, tuple_setting

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")

def _known_challenge_types() -> tuple[str, ...]:
    return tuple(challenge_types())


def _default_challenge_type() -> str:
    return default_challenge_type()


def _default_active_config() -> dict:
    return {ct: {"mode": "none", "package_id": None} for ct in _known_challenge_types()}


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _validate_manifest(manifest: dict) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []
    for field in ("package_id", "name", "challenge_type"):
        if not manifest.get(field, "").strip():
            errors.append(f"manifest.json missing required field: {field!r}")
    ct = manifest.get("challenge_type", "").lower().strip()
    known = _known_challenge_types()
    if ct and ct not in known:
        errors.append(
            f"challenge_type must be one of {known}, got {ct!r}"
        )
    pid = manifest.get("package_id", "")
    if pid and not re.match(r"^[a-zA-Z0-9_\-]+$", pid):
        errors.append(
            "package_id must contain only letters, digits, underscores, hyphens"
        )
    return errors


def _normalise_manifest(raw: dict) -> dict:
    """Fill optional fields with defaults."""
    return {
        "package_id":     raw.get("package_id", "").strip().lower(),
        "name":           raw.get("name", "").strip(),
        "version":        raw.get("version", "1.0.0").strip(),
        "challenge_type": raw.get("challenge_type", _default_challenge_type()).strip().lower(),
        "map_type":       raw.get("map_type", "").strip().lower(),
        "description":    raw.get("description", ""),
        "metrics":        raw.get("metrics") or [],
        "entry_point":    raw.get("entry_point", "scoring.py").strip(),
        "call_mode":      raw.get("call_mode", "standard").strip().lower(),
        "official":       bool(raw.get("official", False)),
        "expected_input_pattern": raw.get("expected_input_pattern", ""),
        "readme":         raw.get("readme", ""),
    }


# ---------------------------------------------------------------------------
# Active config I/O
# ---------------------------------------------------------------------------

def load_active_config() -> dict:
    """Load active.json, returning defaults for any missing challenge types."""
    try:
        if SCORING_ACTIVE_CONFIG.exists():
            raw = json.loads(SCORING_ACTIVE_CONFIG.read_text(encoding="utf-8"))
            config = _default_active_config()
            config.update(raw)
            return config
    except Exception:
        pass
    return _default_active_config()


def save_active_config(config: dict) -> None:
    """Persist active.json atomically."""
    SCORING_ACTIVE_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    SCORING_ACTIVE_CONFIG.write_text(
        json.dumps(config, indent=2, default=str), encoding="utf-8"
    )


def get_active_entry(challenge_type: str) -> dict:
    """Return the active config entry for a challenge type.

    Returns {"mode": "none", "package_id": null} if not found.
    """
    cfg = load_active_config()
    return cfg.get(challenge_type.lower().strip(), {"mode": "none", "package_id": None})


def set_active_entry(challenge_type: str, mode: str, package_id: Optional[str] = None) -> dict:
    """Set active mode for a challenge type and persist.

    mode: "none" | "builtin" | "custom"
    Returns the updated entry.
    """
    mode = mode.strip().lower()
    if mode not in ("none", "builtin", "custom"):
        raise ValueError(f"Invalid mode: {mode!r}. Must be 'none', 'builtin', or 'custom'.")
    if mode == "custom" and not package_id:
        raise ValueError("package_id is required when mode='custom'.")
    cfg = load_active_config()
    entry = {
        "mode":       mode,
        "package_id": package_id if mode == "custom" else None,
        "set_at":     datetime.now(timezone.utc).isoformat(),
    }
    cfg[challenge_type.lower().strip()] = entry
    save_active_config(cfg)
    return entry


# ---------------------------------------------------------------------------
# Package registry
# ---------------------------------------------------------------------------

def list_packages() -> list[dict]:
    """Return manifests for all installed packages, sorted by name."""
    if not SCORING_PACKAGES_DIR.exists():
        return []
    packages = []
    for pkg_dir in sorted(SCORING_PACKAGES_DIR.iterdir()):
        if not pkg_dir.is_dir():
            continue
        manifest_path = pkg_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = _normalise_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
            manifest["installed_path"] = str(pkg_dir)
            manifest["status"] = _check_package_ready_internal(pkg_dir, manifest)
            packages.append(manifest)
        except Exception:
            continue
    return packages


def get_package_manifest(package_id: str) -> Optional[dict]:
    """Return the manifest for a specific package, or None."""
    pkg_dir = SCORING_PACKAGES_DIR / package_id
    if not pkg_dir.exists():
        return None
    manifest_path = pkg_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        m = _normalise_manifest(json.loads(manifest_path.read_text(encoding="utf-8")))
        m["installed_path"] = str(pkg_dir)
        m["status"] = _check_package_ready_internal(pkg_dir, m)
        return m
    except Exception:
        return None


def install_package(zip_path: Path) -> dict:
    """Extract and install a scoring package from a ZIP file.

    Returns {"success": True, "package_id": ..., "manifest": ...} or
            {"success": False, "error": ...}
    """
    # 1. Validate ZIP
    if not zipfile.is_zipfile(zip_path):
        return {"success": False, "error": "Uploaded file is not a valid ZIP archive."}

    # 2. Find manifest.json in ZIP (may be at root or one level deep)
    manifest: Optional[dict] = None
    manifest_prefix = ""
    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            for name in names:
                if name.endswith("manifest.json") and name.count("/") <= 1:
                    raw = json.loads(zf.read(name).decode("utf-8"))
                    manifest = _normalise_manifest(raw)
                    manifest_prefix = name[: -len("manifest.json")]
                    break
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"manifest.json is invalid JSON: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"Could not read ZIP: {exc}"}

    if manifest is None:
        return {
            "success": False,
            "error": (
                "No manifest.json found in the ZIP. "
                "A scoring package must include manifest.json at the archive root "
                "or one level deep."
            ),
        }

    # 3. Validate manifest
    errors = _validate_manifest(manifest)
    if errors:
        return {"success": False, "error": " | ".join(errors)}

    pkg_id = manifest["package_id"]

    # 4. Extract to packages dir (overwrite if exists)
    SCORING_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    pkg_dir = SCORING_PACKAGES_DIR / pkg_id
    if pkg_dir.exists():
        shutil.rmtree(pkg_dir)
    pkg_dir.mkdir(parents=True)

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if not name.startswith(manifest_prefix):
                    continue
                # Strip the prefix to get relative path within package
                rel = name[len(manifest_prefix):]
                if not rel or rel.endswith("/"):
                    continue
                # Safety: reject path traversal
                rel_path = Path(rel)
                if any(part == ".." for part in rel_path.parts):
                    continue
                dest = pkg_dir / rel_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))
    except Exception as exc:
        shutil.rmtree(pkg_dir, ignore_errors=True)
        return {"success": False, "error": f"Failed to extract ZIP: {exc}"}

    # 5. Re-check entry point
    script = pkg_dir / manifest["entry_point"]
    if not script.exists():
        # Check for challengeScoring.py as a fallback
        alt = pkg_dir / "challengeScoring.py"
        if alt.exists():
            manifest["entry_point"] = "challengeScoring.py"
            (pkg_dir / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
        else:
            shutil.rmtree(pkg_dir, ignore_errors=True)
            return {
                "success": False,
                "error": (
                    f"Entry point {manifest['entry_point']!r} not found in package. "
                    "Add a scoring.py (or specify entry_point in manifest.json)."
                ),
            }

    # 6. Persist manifest (normalised)
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    manifest["installed_path"] = str(pkg_dir)
    manifest["status"] = _check_package_ready_internal(pkg_dir, manifest)

    return {"success": True, "package_id": pkg_id, "manifest": manifest}


def remove_package(package_id: str) -> dict:
    """Remove an installed package.

    Also clears any active-config entries pointing to this package.
    Returns {"success": True} or {"success": False, "error": ...}
    """
    pkg_dir = SCORING_PACKAGES_DIR / package_id
    if not pkg_dir.exists():
        return {"success": False, "error": f"Package {package_id!r} not found."}
    try:
        shutil.rmtree(pkg_dir)
    except Exception as exc:
        return {"success": False, "error": str(exc)}

    # Clear any active config that points to this package
    cfg = load_active_config()
    changed = False
    for ct in list(cfg.keys()):
        entry = cfg[ct]
        if entry.get("package_id") == package_id:
            cfg[ct] = {"mode": "none", "package_id": None}
            changed = True
    if changed:
        save_active_config(cfg)

    return {"success": True}


# ---------------------------------------------------------------------------
# Package readiness check
# ---------------------------------------------------------------------------

def _check_package_ready_internal(pkg_dir: Path, manifest: dict) -> dict:
    """Internal check, returns a status dict.

    A package is "ready" when:
    - The scoring script (entry_point) exists.
    - For packages needing reference data: a reference/ dir or reference files exist.
    """
    missing: list[str] = []
    entry_point = pkg_dir / manifest.get("entry_point", "scoring.py")
    if not entry_point.exists():
        missing.append(f"Scoring script not found: {manifest.get('entry_point', 'scoring.py')}")

    # Check for reference data (any NIfTI in reference/, masks/, or root)
    nifti_count = _count_niftis_in(pkg_dir)
    has_reference = (pkg_dir / "reference").exists() or (pkg_dir / "masks").exists() or nifti_count > 0
    # Reference data is advisory: don't mark as missing unless package has a reference/ dir placeholder
    ref_dir = pkg_dir / "reference"
    mask_dir = pkg_dir / "masks"
    if ref_dir.exists() and not _count_niftis_in(ref_dir):
        missing.append("reference/ directory is empty (no NIfTI files found)")
    if mask_dir.exists() and not _count_niftis_in(mask_dir):
        missing.append("masks/ directory is empty (no NIfTI mask files found)")

    return {
        "ready":          len(missing) == 0,
        "missing":        missing,
        "nifti_count":    nifti_count,
        "has_reference":  has_reference,
    }


def _count_niftis_in(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for f in path.rglob("*") if f.is_file() and f.name.lower().endswith(NIFTI_SUFFIXES))


def check_package_ready(package_id: str) -> dict:
    """Public wrapper, check readiness of an installed package."""
    pkg_dir = SCORING_PACKAGES_DIR / package_id
    if not pkg_dir.exists():
        return {"ready": False, "missing": [f"Package {package_id!r} not installed."]}
    manifest = get_package_manifest(package_id)
    if manifest is None:
        return {"ready": False, "missing": ["manifest.json not found or invalid."]}
    return _check_package_ready_internal(pkg_dir, manifest)


# ---------------------------------------------------------------------------
# Custom package scoring
# ---------------------------------------------------------------------------

def run_package_scoring(
    package_id: str,
    submission_id: str,
    exec_output_dir: Path,
    score_output_dir: Path,
) -> dict:
    """Run a custom installed package's scoring script.

    NEVER fabricates metrics. Returns status='not_configured' or status='failed'
    if prerequisites are absent. Returns status='scored' on success.
    """
    manifest = get_package_manifest(package_id)
    if manifest is None:
        return {
            "success":    False,
            "status":     "not_configured",
            "message":    f"Package {package_id!r} not found.",
            "metrics":    {},
            "artifacts":  [],
        }

    readiness = check_package_ready(package_id)
    if not readiness["ready"]:
        return {
            "success":    False,
            "status":     "not_configured",
            "message":    "Scoring package is not ready.",
            "missing":    readiness["missing"],
            "metrics":    {},
            "artifacts":  [],
        }

    # Fall back to configured submitted-map locations if exec outputs don't exist yet.
    if not exec_output_dir.exists():
        extracted_base = EXTRACTED_DIR / submission_id
        fallback = None
        for subpath in output_map_subpaths():
            candidate = (extracted_base / subpath) if subpath else extracted_base
            if candidate.exists():
                fallback = candidate
                break
        if fallback is not None:
            exec_output_dir = fallback
        else:
            return {
                "success":    False,
                "status":     "not_configured",
                "message":    "Submission files not found. Upload the submission first.",
                "metrics":    {},
                "artifacts":  [],
            }

    score_output_dir.mkdir(parents=True, exist_ok=True)
    pkg_dir = SCORING_PACKAGES_DIR / package_id
    entry_point = pkg_dir / manifest["entry_point"]
    call_mode = manifest.get("call_mode", "standard")
    ref_dir = pkg_dir / "reference" if (pkg_dir / "reference").exists() else None

    scored_at = datetime.now(timezone.utc).isoformat()

    try:
        if call_mode == "osipi_cwd":
            cmd = [sys.executable, str(entry_point)]
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=300,
                cwd=str(pkg_dir),
            )
        else:
            # Standard interface
            cmd = [
                sys.executable, str(entry_point),
                "--submission-dir", str(exec_output_dir),
                "--output-dir",    str(score_output_dir),
            ]
            if ref_dir:
                cmd += ["--reference-dir", str(ref_dir)]
            proc = subprocess.run(
                cmd,
                capture_output=True, text=True, timeout=300,
                cwd=str(pkg_dir),
            )

        artifacts     = _collect_artifacts(score_output_dir)
        metrics_full  = _parse_metrics_from_output(score_output_dir)
        # Flat numeric view for the metrics table / summary cards; the full
        # nested structure (summary + per_file) is kept under metrics_detail
        # for the advanced view.  Booleans/strings are excluded so the UI shows
        # only real numeric metric values (RMSE, CoV, finite %, …).
        metrics       = _flatten_metrics(metrics_full)

        if proc.returncode != 0:
            return {
                "success":        False,
                "submission_id":  submission_id,
                "package_id":     package_id,
                "status":         "failed",
                "scored_at":      scored_at,
                "message":        "Scoring script exited with a non-zero return code.",
                "stdout":         proc.stdout[:4096],
                "stderr":         proc.stderr[:4096],
                "metrics":        {},
                "artifacts":      artifacts,
                "artifact_count": len(artifacts),
            }

        return {
            "success":        True,
            "submission_id":  submission_id,
            "package_id":     package_id,
            "package_name":   manifest["name"],
            "package_version": manifest["version"],
            "status":         "scored",
            "scored_at":      scored_at,
            "message": (
                "Analysis complete, metrics parsed."
                if metrics else
                "Analysis complete, artifacts saved. No metrics.json found in output."
            ),
            "stdout":         proc.stdout[:4096],
            "metrics":        metrics,
            "metrics_detail": metrics_full,
            "official":       bool(manifest.get("official", False)),
            "artifacts":      artifacts,
            "artifact_count": len(artifacts),
            "score_dir":      str(score_output_dir),
        }

    except subprocess.TimeoutExpired:
        return {
            "success":        False,
            "submission_id":  submission_id,
            "package_id":     package_id,
            "status":         "failed",
            "scored_at":      scored_at,
            "message":        "Scoring script timed out after 300 seconds.",
            "metrics":        {},
            "artifacts":      [],
            "artifact_count": 0,
        }
    except Exception as exc:
        return {
            "success":        False,
            "submission_id":  submission_id,
            "package_id":     package_id,
            "status":         "failed",
            "scored_at":      scored_at,
            "message":        f"Unexpected error: {exc}",
            "metrics":        {},
            "artifacts":      [],
            "artifact_count": 0,
        }


# ---------------------------------------------------------------------------
# Artifact + metric helpers
# ---------------------------------------------------------------------------

def _collect_artifacts(score_dir: Path) -> list[str]:
    if not score_dir.exists():
        return []
    extensions = {".json", ".csv", ".png", ".pdf", ".txt", ".html"}
    return [
        str(f.relative_to(score_dir))
        for f in sorted(score_dir.rglob("*"))
        if f.is_file() and f.suffix.lower() in extensions
    ]


def _flatten_metrics(parsed: dict) -> dict:
    """Return a flat dict of numeric metric values from a parsed metrics dict.

    Custom packages (e.g. the ASL QC demo) write nested JSON shaped like
    ``{"summary": {...}, "per_file": [...], "package": "...", ...}``.  The UI
    metrics table and summary cards only display numeric values, so this pulls
    scalar ints/floats from the top level and from a nested ``summary`` object.

    Booleans (e.g. ``official_osipi_scoring``) and strings (e.g. ``package``)
    are excluded so no string metadata leaks into the metrics table.  NaN/inf
    and ``None`` values are dropped.  Never fabricates values.
    """
    flat: dict = {}

    def _add(d: dict) -> None:
        for key, val in d.items():
            if isinstance(val, bool) or val is None:
                continue
            if isinstance(val, (int, float)):
                # Drop NaN / inf (val != val is True only for NaN)
                if val != val or val in (float("inf"), float("-inf")):
                    continue
                flat[key] = val

    if isinstance(parsed, dict):
        _add(parsed)
        summary = parsed.get("summary")
        if isinstance(summary, dict):
            _add(summary)
    return flat


def _parse_metrics_from_output(score_dir: Path) -> dict:
    """Parse metrics from metrics.json or results.json written by the scoring script.

    Never fabricates values. Returns {} if no parseable JSON is found.
    """
    if not score_dir.exists():
        return {}
    # Prefer metrics.json, then results.json, then any JSON
    for name in ("metrics.json", "results.json"):
        p = score_dir / name
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                pass
    return {}
