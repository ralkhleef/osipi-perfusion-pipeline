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
    version        , readable package version, e.g. "1.2.0"
    challenge_type , any challenge id configured in config/validation_rules.yaml
    metrics        , non-empty list of metric names the script produces
    required_inputs, non-empty list of configured map/artifact ids consumed

manifest.json optional fields:
    map_type       , configured map id/display, e.g. "ktrans" or "cbf"
    description    , free-text description
    required_assets, package-relative files/directories that must exist
    requirements_file, optional dependency declaration, e.g. "requirements.txt"
    entry_point    , filename of the scoring script, default "scoring.py"
    call_mode      , "standard" (default) | "osipi_cwd"
                      "standard"  → python scoring.py --submission-dir <dir>
                                      --output-dir <dir> [--reference-dir <dir>]
                      "osipi_cwd" → run with cwd=package_dir; script reads
                                    hardcoded relative paths (legacy TF6.2 style)

Active configuration (data/scoring/active.json):
    {
      "<challenge_id>": {
        "mode": "none" | "builtin" | "custom",
        "package_id": null | "...",
        "package_version": null | "..."
      }
    }

NEVER fabricates scoring results.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
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
from osipi_pipeline.config.rules import (
    artifact_type_specs,
    challenge_types,
    default_challenge_type,
    map_type_specs,
    optional_maps_by_challenge,
    output_map_subpaths,
    required_artifacts_by_challenge,
    required_maps_by_challenge,
    tuple_setting,
)

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")

def _known_challenge_types() -> tuple[str, ...]:
    return tuple(challenge_types())


def _default_challenge_type() -> str:
    return default_challenge_type()


def _default_active_config() -> dict:
    return {
        ct: {
            "mode": "none",
            "package_id": None,
            "package_version": None,
            "package_name": None,
        }
        for ct in _known_challenge_types()
    }


# ---------------------------------------------------------------------------
# Manifest helpers
# ---------------------------------------------------------------------------

def _validate_manifest(manifest: dict, *, require_declared_inputs: bool = True) -> list[str]:
    """Return a list of validation error strings (empty = valid)."""
    errors: list[str] = []
    for field in ("package_id", "name", "version", "challenge_type"):
        value = manifest.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"manifest.json missing required field: {field!r}")
    ct = str(manifest.get("challenge_type") or "").lower().strip()
    known = _known_challenge_types()
    if ct and ct not in known:
        errors.append(
            f"challenge_type must be one of {known}, got {ct!r}"
        )
    pid = str(manifest.get("package_id") or "")
    if pid and not re.match(r"^[a-zA-Z0-9_\-]+$", pid):
        errors.append(
            "package_id must contain only letters, digits, underscores, hyphens"
        )
    version = str(manifest.get("version") or "")
    if version and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]{0,63}", version):
        errors.append(
            "version must be 1-64 readable characters using letters, digits, '.', '_', '+', or '-'"
        )

    entry_point = manifest.get("entry_point", "scoring.py")
    if not isinstance(entry_point, str) or not _safe_relative_path(entry_point, require_python=True):
        errors.append("entry_point must be a safe package-relative Python file path")

    call_mode = manifest.get("call_mode", "standard")
    if call_mode not in ("standard", "osipi_cwd"):
        errors.append("call_mode must be 'standard' or 'osipi_cwd'")

    metrics = manifest.get("metrics")
    if not isinstance(metrics, list) or not metrics:
        errors.append("metrics must be a non-empty list of metric names")
    elif any(
        not isinstance(metric, str)
        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.\-]{0,127}", metric)
        for metric in metrics
    ):
        errors.append("each metric name must be a readable identifier")
    elif len(set(metrics)) != len(metrics):
        errors.append("metric names must be unique")

    required_inputs = manifest.get("required_inputs") or []
    if require_declared_inputs and (not isinstance(required_inputs, list) or not required_inputs):
        errors.append("required_inputs must be a non-empty list of configured map/artifact ids")
    elif required_inputs and any(not isinstance(item, str) for item in required_inputs):
        errors.append("required_inputs must contain only configured map/artifact ids")
    else:
        configured_for_challenge = (
            set(required_maps_by_challenge().get(ct, ()))
            | set(optional_maps_by_challenge().get(ct, ()))
            | set(required_artifacts_by_challenge().get(ct, ()))
        )
        # Legacy/future challenges may only declare expected_maps. In that
        # compatibility case, allow any globally configured input id.
        known_inputs = configured_for_challenge or (set(map_type_specs()) | set(artifact_type_specs()))
        unknown_inputs = sorted({item.lower().strip() for item in required_inputs} - known_inputs)
        if unknown_inputs:
            errors.append(
                "required_inputs contains ids not configured in validation_rules.yaml: "
                + ", ".join(unknown_inputs)
            )

    required_assets = manifest.get("required_assets") or []
    if not isinstance(required_assets, list) or any(
        not isinstance(item, str) or not _safe_relative_path(item)
        for item in required_assets
    ):
        errors.append("required_assets must be a list of safe package-relative paths")
    requirements_file = manifest.get("requirements_file") or ""
    if requirements_file and (
        not isinstance(requirements_file, str) or not _safe_relative_path(requirements_file)
    ):
        errors.append("requirements_file must be a safe package-relative path")
    return errors


def _safe_relative_path(value: str, *, require_python: bool = False) -> bool:
    """Return True for a non-empty relative path contained by a package."""
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value.strip())
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        return False
    return not require_python or path.suffix.lower() == ".py"


def _normalise_manifest(raw: dict) -> dict:
    """Fill optional fields with defaults."""
    def _string(field: str, default: str = "") -> str:
        value = raw.get(field, default)
        return value.strip() if isinstance(value, str) else value

    package_id = _string("package_id")
    challenge_type = _string("challenge_type", _default_challenge_type())
    map_type = _string("map_type")
    call_mode = _string("call_mode", "standard")

    return {
        "package_id":     package_id.lower() if isinstance(package_id, str) else package_id,
        "name":           _string("name"),
        "version":        _string("version"),
        "challenge_type": challenge_type.lower() if isinstance(challenge_type, str) else challenge_type,
        "map_type":       map_type.lower() if isinstance(map_type, str) else map_type,
        "description":    raw.get("description", ""),
        "metrics":        raw.get("metrics") or [],
        "required_inputs": raw.get("required_inputs") or [],
        "required_assets": raw.get("required_assets") or [],
        "requirements_file": _string("requirements_file"),
        "entry_point":    _string("entry_point", "scoring.py"),
        "call_mode":      call_mode.lower() if isinstance(call_mode, str) else call_mode,
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
    payload = json.dumps(config, indent=2, default=str)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=SCORING_ACTIVE_CONFIG.parent,
        prefix=f".{SCORING_ACTIVE_CONFIG.name}.",
        suffix=".tmp",
        delete=False,
    ) as temp_file:
        temp_file.write(payload)
        temp_path = Path(temp_file.name)
    try:
        temp_path.replace(SCORING_ACTIVE_CONFIG)
    finally:
        temp_path.unlink(missing_ok=True)


def get_active_entry(challenge_type: str) -> dict:
    """Return the active config entry for a challenge type.

    Returns {"mode": "none", "package_id": null} if not found.
    """
    cfg = load_active_config()
    return cfg.get(
        challenge_type.lower().strip(),
        {
            "mode": "none",
            "package_id": None,
            "package_version": None,
            "package_name": None,
        },
    )


def set_active_entry(challenge_type: str, mode: str, package_id: Optional[str] = None) -> dict:
    """Set active mode for a challenge type and persist.

    mode: "none" | "builtin" | "custom"
    Returns the updated entry.
    """
    mode = mode.strip().lower()
    if mode not in ("none", "builtin", "custom"):
        raise ValueError(f"Invalid mode: {mode!r}. Must be 'none', 'builtin', or 'custom'.")
    challenge_type = challenge_type.lower().strip()
    if challenge_type not in _known_challenge_types():
        raise ValueError(f"Unknown challenge type: {challenge_type!r}.")
    if mode == "custom" and not package_id:
        raise ValueError("package_id is required when mode='custom'.")
    package_version = None
    package_name = None
    if mode == "custom":
        manifest = get_package_manifest(str(package_id))
        if manifest is None:
            raise ValueError(
                "Scoring configuration could not be activated; the previous configuration "
                f"remains active. Package {package_id!r} is not installed."
            )
        if manifest.get("challenge_type") != challenge_type:
            raise ValueError(
                "Scoring configuration could not be activated; the previous configuration "
                f"remains active. Package {package_id!r} is for "
                f"{manifest.get('challenge_type')!r}, not {challenge_type!r}."
            )
        status = _check_package_ready_internal(
            SCORING_PACKAGES_DIR / str(package_id),
            manifest,
            perform_import_check=True,
            require_declared_inputs=True,
        )
        if not status.get("ready"):
            details = "; ".join(status.get("missing") or ["package validation failed"])
            raise ValueError(
                "Scoring configuration could not be activated; the previous configuration "
                f"remains active. {details}"
            )
        package_version = manifest.get("version")
        package_name = manifest.get("name")
    cfg = load_active_config()
    entry = {
        "mode":       mode,
        "package_id": package_id if mode == "custom" else None,
        "package_version": package_version,
        "package_name": package_name,
        "set_at":     datetime.now(timezone.utc).isoformat(),
    }
    cfg[challenge_type] = entry
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

    # 4. Extract into an isolated staging directory. Never replace an existing
    # package until every validation check has passed.
    SCORING_PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    pkg_dir = SCORING_PACKAGES_DIR / pkg_id
    if pkg_dir.exists():
        existing = get_package_manifest(pkg_id) or {}
        return {
            "success": False,
            "error": (
                f"Package id {pkg_id!r} is already installed"
                + (f" at version {existing.get('version')!r}" if existing.get("version") else "")
                + ". Use a versioned package_id for a new release so an active package "
                  "cannot change without explicit activation."
            ),
        }
    staging_dir = Path(tempfile.mkdtemp(prefix=".scoring-package-", dir=SCORING_PACKAGES_DIR))

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for name in zf.namelist():
                if not name.startswith(manifest_prefix):
                    continue
                # Strip the prefix to get relative path within package
                rel = name[len(manifest_prefix):]
                if not rel or rel.endswith("/"):
                    continue
                # Safety: reject traversal and absolute archive paths.
                if not _safe_relative_path(rel):
                    raise ValueError(f"Unsafe package path: {rel!r}")
                dest = staging_dir / Path(rel)
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))
    except Exception as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return {"success": False, "error": f"Failed to extract ZIP: {exc}"}

    # 5. Persist the normalised manifest in staging, then validate the complete
    # package (entry point, declared assets, and scorer syntax/importability).
    (staging_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    status = _check_package_ready_internal(
        staging_dir,
        manifest,
        perform_import_check=True,
        require_declared_inputs=True,
    )
    if not status["ready"]:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return {
            "success": False,
            "error": "Package validation failed: " + " | ".join(status["missing"]),
        }

    # 6. The final rename is atomic on the same filesystem. A failed package
    # therefore never becomes installed or active.
    try:
        staging_dir.replace(pkg_dir)
    except OSError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        return {"success": False, "error": f"Could not finalize package installation: {exc}"}
    manifest["installed_path"] = str(pkg_dir)
    manifest["status"] = status

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
            cfg[ct] = {
                "mode": "none",
                "package_id": None,
                "package_version": None,
                "package_name": None,
            }
            changed = True
    if changed:
        save_active_config(cfg)

    return {"success": True}


# ---------------------------------------------------------------------------
# Package readiness check
# ---------------------------------------------------------------------------

def _check_package_ready_internal(
    pkg_dir: Path,
    manifest: dict,
    *,
    perform_import_check: bool = False,
    require_declared_inputs: bool = False,
) -> dict:
    """Internal check, returns a status dict.

    A package is "ready" when:
    - The scoring script (entry_point) exists.
    - For packages needing reference data: a reference/ dir or reference files exist.
    """
    missing: list[str] = _validate_manifest(
        manifest,
        require_declared_inputs=require_declared_inputs,
    )
    entry_point = pkg_dir / manifest.get("entry_point", "scoring.py")
    syntax_valid = False
    if not entry_point.is_file():
        missing.append(f"Scoring script not found: {manifest.get('entry_point', 'scoring.py')}")
    else:
        try:
            source = entry_point.read_text(encoding="utf-8")
            compile(source, str(entry_point), "exec")
        except (OSError, UnicodeError, SyntaxError) as exc:
            missing.append(f"Scoring script import/syntax check failed: {exc}")
        else:
            syntax_valid = True
        if syntax_valid and perform_import_check:
            try:
                import_check = subprocess.run(
                    [
                        sys.executable,
                        "-I",
                        "-c",
                        (
                            "import importlib.util,pathlib,sys;"
                            "sys.dont_write_bytecode=True;"
                            "p=pathlib.Path(sys.argv[1]);sys.path.insert(0,str(p.parent));"
                            "s=importlib.util.spec_from_file_location('_osipi_package_check',p);"
                            "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)"
                        ),
                        str(entry_point),
                    ],
                    cwd=str(pkg_dir),
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                missing.append("Scoring script import/initialization check timed out after 15 seconds")
            else:
                if import_check.returncode != 0:
                    detail = (import_check.stderr or import_check.stdout).strip()[-1000:]
                    missing.append(
                        "Scoring script import/initialization check failed"
                        + (f": {detail}" if detail else "")
                    )

    for asset in manifest.get("required_assets") or []:
        asset_path = pkg_dir / asset
        if not asset_path.exists():
            missing.append(f"Required asset not found: {asset}")
        elif asset_path.is_dir() and not any(item.is_file() for item in asset_path.rglob("*")):
            missing.append(f"Required asset directory is empty: {asset}")

    requirements_file = manifest.get("requirements_file")
    if requirements_file and not (pkg_dir / requirements_file).is_file():
        missing.append(f"Requirements file not found: {requirements_file}")

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


def check_package_ready(
    package_id: str,
    *,
    perform_import_check: bool = False,
    require_declared_inputs: bool = False,
) -> dict:
    """Check an installed package, optionally including scorer import/init."""
    pkg_dir = SCORING_PACKAGES_DIR / package_id
    if not pkg_dir.exists():
        return {"ready": False, "missing": [f"Package {package_id!r} not installed."]}
    manifest = get_package_manifest(package_id)
    if manifest is None:
        return {"ready": False, "missing": ["manifest.json not found or invalid."]}
    return _check_package_ready_internal(
        pkg_dir,
        manifest,
        perform_import_check=perform_import_check,
        require_declared_inputs=require_declared_inputs,
    )


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
            "package_id": package_id,
            "package_name": manifest.get("name"),
            "package_version": manifest.get("version"),
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
                "package_id": package_id,
                "package_name": manifest.get("name"),
                "package_version": manifest.get("version"),
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
        parsed_metrics = _flatten_metrics(metrics_full)
        declared_metrics = set(manifest.get("metrics") or [])
        # The manifest is the public metric contract. Extra numeric fields may
        # remain in metrics_detail as technical metadata, but are not promoted
        # into UI/export metric columns unless declared.
        metrics = {
            name: value for name, value in parsed_metrics.items()
            if name in declared_metrics
        }

        if proc.returncode != 0:
            return {
                "success":        False,
                "submission_id":  submission_id,
                "package_id":     package_id,
                "package_name":   manifest.get("name"),
                "package_version": manifest.get("version"),
                "status":         "failed",
                "scored_at":      scored_at,
                "message":        "Scoring script exited with a non-zero return code.",
                "stdout":         proc.stdout[:4096],
                "stderr":         proc.stderr[:4096],
                "metrics":        {},
                "artifacts":      artifacts,
                "artifact_count": len(artifacts),
            }

        missing_metrics = sorted(declared_metrics - set(metrics))
        if missing_metrics:
            return {
                "success": False,
                "submission_id": submission_id,
                "package_id": package_id,
                "package_name": manifest.get("name"),
                "package_version": manifest.get("version"),
                "status": "failed",
                "scored_at": scored_at,
                "message": (
                    "Scoring package output is missing declared metrics: "
                    + ", ".join(missing_metrics)
                ),
                "stdout": proc.stdout[:4096],
                "stderr": proc.stderr[:4096],
                "metrics": metrics,
                "metrics_detail": metrics_full,
                "artifacts": artifacts,
                "artifact_count": len(artifacts),
            }

        return {
            "success":        True,
            "submission_id":  submission_id,
            "package_id":     package_id,
            "package_name":   manifest.get("name"),
            "package_version": manifest.get("version"),
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
            "package_name":   manifest.get("name"),
            "package_version": manifest.get("version"),
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
