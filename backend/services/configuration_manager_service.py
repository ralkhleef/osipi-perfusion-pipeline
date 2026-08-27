"""Safe, local challenge-configuration lifecycle for reviewer handoff.

The active repository YAML remains the single source of truth.  This service
adds a non-destructive workflow around it: construct and test an in-memory
candidate, preview a human-readable diff, save an immutable local version,
and only then activate it. Private assets and history are kept
under ignored ``data/`` paths.
"""

from __future__ import annotations

import copy
import io
import json
import re
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

from services import path_config as paths
from services.scoring_package_service import (
    check_package_ready,
    compatible_builtin_providers,
    get_active_entry,
    get_package_manifest,
    list_packages,
    set_active_entry,
)
from osipi_pipeline.config import rules as rules_module


_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ASSET_DIRS = {
    "reference": "maps",
    "mask": "masks",
    "measured_signal": "signals",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _read_active_versions() -> dict[str, Any]:
    try:
        raw = json.loads(paths.CONFIG_ACTIVE_VERSION.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_active_versions(data: dict[str, Any]) -> None:
    _atomic_text(paths.CONFIG_ACTIVE_VERSION, json.dumps(data, indent=2) + "\n")


def _challenge_id(value: Any) -> str:
    challenge = str(value or "").strip().lower()
    if not _SAFE_ID.fullmatch(challenge):
        raise ValueError("A configured challenge id is required.")
    if challenge not in rules_module.challenge_types():
        raise ValueError(f"Unknown challenge type: {challenge!r}.")
    return challenge


def _editable_from_rules(challenge: str, rules: dict[str, Any]) -> dict[str, Any]:
    spec = copy.deepcopy(rules["challenges"][challenge])
    maps = []
    required = {str(item).lower() for item in spec.get("required_maps") or []}
    optional = {str(item).lower() for item in spec.get("optional_maps") or []}
    expected = {str(item).lower() for item in spec.get("expected_maps") or []}
    for map_id, map_spec in rules.get("map_types", {}).items():
        map_id = str(map_id).lower()
        state = "required" if map_id in required else "optional" if map_id in optional else "unused"
        maps.append({
            "id": map_id,
            "display": map_spec.get("display") or map_id,
            "label": map_spec.get("label") or map_id,
            "state": state,
            "expected": map_id in expected,
            "dimensions": map_spec.get("dimensions"),
            "aliases": list(map_spec.get("patterns") or []),
        })
    artifacts = []
    required_artifacts = {str(item).lower() for item in spec.get("required_artifacts") or []}
    for artifact_id, artifact_spec in rules.get("artifact_types", {}).items():
        artifacts.append({
            "id": str(artifact_id).lower(),
            "label": artifact_spec.get("label") or artifact_id,
            "required": str(artifact_id).lower() in required_artifacts,
        })
    return {
        "challenge_type": challenge,
        "label": spec.get("label") or challenge.upper(),
        "description": spec.get("description") or "",
        "maps": maps,
        "required_artifacts": artifacts,
        "datasets": copy.deepcopy(spec.get("datasets") or {}),
        "code_execution_required": bool(spec.get("code_execution_required", False)),
        "reference_dataset_version": str(spec.get("reference_dataset_version") or ""),
        "scoring": copy.deepcopy(get_active_entry(challenge)),
    }


def manager_state(challenge_type: str) -> dict[str, Any]:
    challenge = _challenge_id(challenge_type)
    rules = rules_module.validation_rules()
    from scoring import all_providers_status

    builtin_providers = [
        item
        for item in all_providers_status()
        if item.get("source") == "builtin"
        and not item.get("not_for_scoring")
        and str(item.get("challenge_type") or "").lower() == challenge
    ]
    return {
        "editable": _editable_from_rules(challenge, rules),
        "versions": list_versions(challenge),
        "assets": asset_status(challenge),
        "capabilities": capability_matrix(),
        "packages": [
            {
                "package_id": item.get("package_id"),
                "name": item.get("name"),
                "version": item.get("version"),
                "challenge_type": item.get("challenge_type"),
                "ready": bool((item.get("status") or {}).get("ready")),
            }
            for item in list_packages()
        ],
        "builtin_providers": builtin_providers,
        "private_data_notice": (
            "Private organiser data. These files remain local and are not included "
            "in configuration exports or GitHub."
        ),
    }


def candidate_rules(payload: dict[str, Any]) -> tuple[str, dict[str, Any], dict[str, Any]]:
    challenge = _challenge_id(payload.get("challenge_type"))
    editable = payload.get("configuration") or payload.get("editable") or payload
    if not isinstance(editable, dict):
        raise ValueError("configuration must be an object.")
    candidate = copy.deepcopy(rules_module.validation_rules())
    spec = candidate["challenges"][challenge]

    maps = editable.get("maps")
    if not isinstance(maps, list) or not maps:
        raise ValueError("At least one map definition is required.")
    required: list[str] = []
    optional: list[str] = []
    for item in maps:
        if not isinstance(item, dict):
            raise ValueError("Each map definition must be an object.")
        map_id = str(item.get("id") or "").strip().lower()
        if map_id not in candidate.get("map_types", {}):
            raise ValueError(f"Unknown map id: {map_id!r}.")
        state = str(item.get("state") or "unused").lower()
        if state == "required":
            required.append(map_id)
        elif state == "optional":
            optional.append(map_id)
        elif state != "unused":
            raise ValueError(f"Invalid state for {map_id}: {state!r}.")
        aliases = item.get("aliases")
        if not isinstance(aliases, list) or not aliases:
            raise ValueError(f"{map_id} needs at least one filename alias.")
        candidate["map_types"][map_id]["patterns"] = [str(alias).strip() for alias in aliases]
        dimensions = item.get("dimensions")
        if dimensions in ("", None):
            candidate["map_types"][map_id].pop("dimensions", None)
        else:
            candidate["map_types"][map_id]["dimensions"] = int(dimensions)

    if not required:
        raise ValueError("At least one required map must be selected.")
    spec["required_maps"] = required
    spec["optional_maps"] = optional
    # ``expected_maps`` is the legacy warning list, not a synonym for optional.
    # Preserve its current intent while ensuring every newly-required map is
    # represented and every now-unused map is removed.
    used = set(required + optional)
    expected = [str(item).lower() for item in spec.get("expected_maps") or []]
    spec["expected_maps"] = [item for item in expected if item in used]
    spec["expected_maps"].extend(item for item in required if item not in spec["expected_maps"])

    artifact_rows = editable.get("required_artifacts") or []
    if not isinstance(artifact_rows, list):
        raise ValueError("required_artifacts must be a list.")
    spec["required_artifacts"] = [
        str(item.get("id")).lower() for item in artifact_rows
        if isinstance(item, dict) and item.get("required")
    ]

    datasets = editable.get("datasets") or {}
    if not isinstance(datasets, dict):
        raise ValueError("datasets must be an object.")
    normalized_datasets: dict[str, dict[str, int | None]] = {}
    for name, counts in datasets.items():
        if not isinstance(counts, dict):
            raise ValueError(f"Dataset {name!r} must contain participant/repeat/site counts.")
        normalized_datasets[str(name).lower()] = {}
        for field in ("participants", "repeats", "sites"):
            value = counts.get(field)
            normalized_datasets[str(name).lower()][field] = None if value in (None, "") else int(value)
    if normalized_datasets:
        spec["datasets"] = normalized_datasets
    else:
        spec.pop("datasets", None)

    spec["code_execution_required"] = bool(editable.get("code_execution_required", False))
    reference_version = str(editable.get("reference_dataset_version") or "").strip()
    if reference_version:
        spec["reference_dataset_version"] = reference_version
    else:
        spec.pop("reference_dataset_version", None)

    scoring = editable.get("scoring") or payload.get("scoring") or get_active_entry(challenge)
    if not isinstance(scoring, dict):
        raise ValueError("scoring must be an object.")
    rules_module.validate_validation_rules_data(candidate)
    return challenge, candidate, copy.deepcopy(scoring)


def _identified(items: Any) -> Optional[dict[str, dict]]:
    """A list of objects keyed by ``id``, or None if it is not one.

    ``maps`` and ``required_artifacts`` are lists whose order carries no
    meaning; identity lives in the ``id`` field. Treating them as opaque made
    the preview report a single change containing both entire lists, which is
    how a reviewer ends up reading serialized JSON to find out that one map
    changed state.
    """
    if not isinstance(items, list) or not items:
        return None
    if not all(isinstance(item, dict) and "id" in item for item in items):
        return None
    keyed = {str(item["id"]): item for item in items}
    # Duplicate ids would silently drop entries from the comparison.
    return keyed if len(keyed) == len(items) else None


def _change_rows(before: Any, after: Any, path: str = "") -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if isinstance(before, dict) and isinstance(after, dict):
        for key in sorted(set(before) | set(after)):
            rows.extend(_change_rows(before.get(key), after.get(key), f"{path}.{key}".strip(".")))
        return rows

    before_by_id, after_by_id = _identified(before), _identified(after)
    if before_by_id is not None and after_by_id is not None:
        for key in sorted(set(before_by_id) | set(after_by_id)):
            rows.extend(_change_rows(
                before_by_id.get(key), after_by_id.get(key), f"{path}.{key}".strip("."),
            ))
        return rows

    if before != after:
        rows.append({"field": path, "before": before, "after": after})
    return rows


def preview_configuration(payload: dict[str, Any]) -> dict[str, Any]:
    challenge, candidate, scoring = candidate_rules(payload)
    active_rules = rules_module.validation_rules()
    rows = _change_rows(
        _editable_from_rules(challenge, active_rules),
        {**_editable_from_rules(challenge, candidate), "scoring": scoring},
    )
    return {"valid": True, "challenge_type": challenge, "changes": rows, "change_count": len(rows)}


def _asset_roots(challenge: str) -> list[Path]:
    """The folders reference assets may live in, best first.

    These are deliberately the same three roots backend/scoring.py searches.
    While this function knew only about the challenge-scoped layout, the panel
    reported zero reference maps at the same time as the pipeline was happily
    scoring against files in the flat one. That is the worst kind of wrong:
    quiet, and about the single thing the panel exists to report.
    """
    base = paths.REFERENCE_DATA_DIR
    return [base / challenge, base / "reference", base]


def _asset_files(challenge: str) -> list[tuple[str, Path]]:
    """Every asset across all three layouts, each file reported once.

    Dotfiles are skipped so the ingestion manifests that sit beside the data
    are not counted as reference maps.
    """
    result: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for root in _asset_roots(challenge):
        for kind, directory in _ASSET_DIRS.items():
            folder = root / directory
            if not folder.exists():
                continue
            for file in sorted(folder.rglob("*")):
                if not file.is_file() or file.name.startswith("."):
                    continue
                resolved = file.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
                result.append((kind, file))
    return result


def _inspect_nifti(path: Path) -> dict[str, Any]:
    if not path.name.lower().endswith((".nii", ".nii.gz")):
        return {"readable": True, "shape": None}
    try:
        import nibabel as nib
        image = nib.load(str(path))
        return {"readable": True, "shape": list(image.shape), "affine": "available"}
    except Exception as exc:
        return {"readable": False, "error": str(exc)}


def asset_status(challenge_type: str) -> dict[str, Any]:
    challenge = _challenge_id(challenge_type)
    items = []
    for kind, file in _asset_files(challenge):
        inspected = _inspect_nifti(file)
        relative = file.relative_to(paths.REFERENCE_DATA_DIR)
        items.append({
            "kind": kind,
            "name": file.name,
            "relative_path": str(relative),
            # The folder this file was actually found in, so the panel can name
            # the real location rather than the one an upload would have used.
            "folder": f"data/reference_data/{relative.parent.as_posix()}/",
            **inspected,
        })
    return {
        "challenge_type": challenge,
        "items": items,
        "counts": {
            kind: sum(1 for item in items if item["kind"] == kind)
            for kind in _ASSET_DIRS
        },
        # Where an upload would put each kind. Shown when a kind is empty, so
        # the answer to "where do I put mine" is on screen either way.
        "upload_folders": {
            kind: f"data/reference_data/{challenge}/{directory}/"
            for kind, directory in _ASSET_DIRS.items()
        },
        "local_only": True,
    }


def store_private_asset(challenge_type: str, asset_kind: str, filename: str, content: bytes) -> dict[str, Any]:
    challenge = _challenge_id(challenge_type)
    kind = str(asset_kind or "").strip().lower()
    if kind not in _ASSET_DIRS:
        raise ValueError("asset_kind must be reference, mask, or measured_signal.")
    safe_name = Path(filename or "").name
    if not safe_name or safe_name != filename or not safe_name.lower().endswith((".nii", ".nii.gz")):
        raise ValueError("Private assets must be .nii or .nii.gz files with a safe filename.")
    if len(content) > 1024 * 1024 * 1024:
        raise ValueError("Private asset exceeds the 1 GB local upload limit.")
    target = paths.REFERENCE_DATA_DIR / challenge / _ASSET_DIRS[kind] / safe_name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise ValueError(
            f"A local asset named {safe_name!r} already exists. Use a versioned filename "
            "so private reference data is not overwritten accidentally."
        )
    suffix = ".nii.gz" if safe_name.lower().endswith(".nii.gz") else ".nii"
    with tempfile.NamedTemporaryFile(
        dir=target.parent, prefix=".asset-", suffix=suffix, delete=False
    ) as handle:
        handle.write(content)
        staged = Path(handle.name)
    inspected = _inspect_nifti(staged)
    if not inspected.get("readable"):
        staged.unlink(missing_ok=True)
        raise ValueError(f"The NIfTI file could not be read: {inspected.get('error')}.")
    staged.replace(target)
    return {"stored": True, "asset": {"kind": kind, "name": safe_name, **inspected}}


def test_configuration(payload: dict[str, Any]) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    try:
        challenge, _candidate, scoring = candidate_rules(payload)
        checks.extend([
            {"status": "pass", "name": "YAML schema", "detail": "Configuration is valid."},
            {"status": "pass", "name": "Challenge ID", "detail": f"{challenge.upper()} is recognized."},
            {"status": "pass", "name": "Map definitions", "detail": "Required maps, dimensions, and filename aliases are valid."},
        ])
    except Exception as exc:
        return {
            "ready": False,
            "checks": [{"status": "fail", "name": "Configuration", "detail": str(exc)}],
            "message": "Current active configuration has NOT been changed.",
        }

    mode = str(scoring.get("mode") or "none").lower()
    if mode == "custom":
        package_id = str(scoring.get("package_id") or "")
        manifest = get_package_manifest(package_id)
        status = check_package_ready(
            package_id,
            perform_import_check=True,
            require_declared_inputs=True,
        ) if manifest else {"ready": False, "missing": ["package is not installed"]}
        checks.append({
            "status": "pass" if status.get("ready") else "fail",
            "name": "Scoring package",
            "detail": (
                f"{manifest.get('name')} {manifest.get('version')} loaded successfully."
                if status.get("ready") and manifest else "; ".join(status.get("missing") or [])
            ),
        })
    elif mode == "builtin":
        compatible = compatible_builtin_providers(challenge)
        if len(compatible) != 1:
            checks.append({
                "status": "fail",
                "name": "Built-in provider",
                "detail": (
                    f"No compatible built-in provider is registered for {challenge.upper()}."
                    if not compatible else
                    f"Multiple built-in providers are registered for {challenge.upper()}; "
                    "selecting one implicitly would be unsafe."
                ),
            })
        else:
            from scoring import all_providers_status

            provider_id = compatible[0].get("provider_id")
            provider_status = next(
                (
                    item for item in all_providers_status()
                    if item.get("provider_id") == provider_id
                ),
                None,
            )
            ready = bool(provider_status and provider_status.get("status") == "ready")
            provider_name = str(
                compatible[0].get("display_name")
                or compatible[0].get("provider_name")
                or compatible[0].get("provider_id")
                or "Built-in provider"
            )
            missing = "; ".join((provider_status or {}).get("missing") or [])
            checks.append({
                "status": "pass" if ready else "fail",
                "name": provider_name,
                "detail": (
                    "Built-in provider requirements are available."
                    if ready else missing or "Built-in provider requirements are not installed."
                ),
            })
    else:
        checks.append({
            "status": "pass", "name": "Provider scoring",
            "detail": "Disabled. Generic QC and compatible reference comparisons remain available.",
        })

    assets = asset_status(challenge)
    unreadable = [item["name"] for item in assets["items"] if not item.get("readable")]
    if unreadable:
        checks.append({"status": "fail", "name": "Private assets", "detail": "Unreadable: " + ", ".join(unreadable)})
    elif assets["items"]:
        checks.append({"status": "pass", "name": "Private assets", "detail": f"{len(assets['items'])} local asset(s) are readable."})
    else:
        checks.append({"status": "warn", "name": "Private assets", "detail": "None supplied; asset-dependent analyses will stay unavailable."})

    ready = not any(item["status"] == "fail" for item in checks)
    return {
        "ready": ready,
        "challenge_type": challenge,
        "checks": checks,
        "message": (
            "Configuration is ready to save. The active configuration has not changed."
            if ready else "Current active configuration has NOT been changed."
        ),
    }


def _next_version(challenge: str) -> str:
    highest = 0
    folder = paths.CONFIG_VERSIONS_DIR / challenge
    if folder.exists():
        for metadata in folder.glob("*.json"):
            match = re.search(r"-v(\d+)$", metadata.stem)
            if match:
                highest = max(highest, int(match.group(1)))
    return f"{challenge}-v{highest + 1:03d}"


def _save_full_rules(challenge: str, candidate: dict[str, Any], scoring: dict[str, Any], source: str) -> dict[str, Any]:
    version_id = _next_version(challenge)
    folder = paths.CONFIG_VERSIONS_DIR / challenge
    metadata = {
        "version_id": version_id,
        "challenge_type": challenge,
        "created_at": _now(),
        "source": source,
        "scoring": scoring,
    }
    _atomic_text(folder / f"{version_id}.yaml", yaml.safe_dump(candidate, sort_keys=False))
    _atomic_text(folder / f"{version_id}.json", json.dumps(metadata, indent=2) + "\n")
    return metadata


def save_version(payload: dict[str, Any]) -> dict[str, Any]:
    tested = test_configuration(payload)
    if not tested.get("ready"):
        raise ValueError(tested.get("message") or "Configuration is not ready.")
    challenge, candidate, scoring = candidate_rules(payload)
    version = _save_full_rules(challenge, candidate, scoring, "configuration-manager")
    return {"saved": True, "version": {**version, "active": False}}


def list_versions(challenge_type: str) -> list[dict[str, Any]]:
    challenge = _challenge_id(challenge_type)
    active = (_read_active_versions().get(challenge) or {}).get("version_id")
    folder = paths.CONFIG_VERSIONS_DIR / challenge
    versions: list[dict[str, Any]] = []
    if folder.exists():
        for file in sorted(folder.glob("*.json"), reverse=True):
            try:
                metadata = json.loads(file.read_text(encoding="utf-8"))
                metadata["active"] = metadata.get("version_id") == active
                versions.append(metadata)
            except (OSError, ValueError):
                continue
    return versions


def activate_version(challenge_type: str, version_id: str) -> dict[str, Any]:
    challenge = _challenge_id(challenge_type)
    if not re.fullmatch(rf"{re.escape(challenge)}-v\d{{3,}}", str(version_id)):
        raise ValueError("Invalid configuration version id.")
    folder = paths.CONFIG_VERSIONS_DIR / challenge
    yaml_path = folder / f"{version_id}.yaml"
    meta_path = folder / f"{version_id}.json"
    if not yaml_path.exists() or not meta_path.exists():
        raise ValueError(f"Configuration version {version_id!r} was not found.")
    candidate = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    rules_module.validate_validation_rules_data(candidate, label=str(yaml_path))
    metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    scoring = metadata.get("scoring") or {"mode": "none"}

    # Validate the external scorer before touching either active file.
    editable = _editable_from_rules(challenge, candidate)
    editable["scoring"] = scoring
    tested = test_configuration({"challenge_type": challenge, "configuration": editable})
    if not tested.get("ready"):
        raise ValueError(tested.get("message") or "Configuration is not ready.")

    current_path = rules_module.VALIDATION_RULES_PATH
    previous_yaml = current_path.read_text(encoding="utf-8")
    scoring_file_existed = paths.SCORING_ACTIVE_CONFIG.exists()
    previous_scoring_text = (
        paths.SCORING_ACTIVE_CONFIG.read_text(encoding="utf-8")
        if scoring_file_existed else ""
    )
    previous_active_versions = _read_active_versions()
    active_file_existed = paths.CONFIG_ACTIVE_VERSION.exists()
    try:
        _atomic_text(current_path, yaml.safe_dump(candidate, sort_keys=False))
        rules_module.validate_config_files()
        set_active_entry(challenge, str(scoring.get("mode") or "none"), scoring.get("package_id"))
        active = copy.deepcopy(previous_active_versions)
        active[challenge] = {"version_id": version_id, "activated_at": _now()}
        _write_active_versions(active)
    except Exception:
        _atomic_text(current_path, previous_yaml)
        rules_module.validate_config_files()
        try:
            if scoring_file_existed:
                _atomic_text(paths.SCORING_ACTIVE_CONFIG, previous_scoring_text)
            else:
                paths.SCORING_ACTIVE_CONFIG.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if active_file_existed:
                _write_active_versions(previous_active_versions)
            else:
                paths.CONFIG_ACTIVE_VERSION.unlink(missing_ok=True)
        except Exception:
            pass
        raise
    return {
        "activated": True,
        "active": True,
        "challenge_type": challenge,
        "version_id": version_id,
        "test": tested,
    }


def export_configuration(challenge_type: str, version_id: str | None = None) -> tuple[bytes, str]:
    challenge = _challenge_id(challenge_type)
    metadata: dict[str, Any]
    if version_id:
        if not re.fullmatch(rf"{re.escape(challenge)}-v\d{{3,}}", str(version_id)):
            raise ValueError("Invalid configuration version id.")
        folder = paths.CONFIG_VERSIONS_DIR / challenge
        rules_text = (folder / f"{version_id}.yaml").read_text(encoding="utf-8")
        metadata = json.loads((folder / f"{version_id}.json").read_text(encoding="utf-8"))
    else:
        rules_text = rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8")
        active = _read_active_versions().get(challenge) or {}
        metadata = {
            "challenge_type": challenge,
            "version_id": active.get("version_id") or f"rules-v{rules_module.validation_rules().get('version')}",
            "exported_at": _now(),
            "scoring": get_active_entry(challenge),
        }
    scoring = metadata.get("scoring") or {}
    manifest = get_package_manifest(str(scoring.get("package_id") or "")) if scoring.get("mode") == "custom" else None
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("validation_rules.yaml", rules_text)
        archive.writestr("metadata.json", json.dumps(metadata, indent=2))
        archive.writestr("scoring-selection.json", json.dumps(scoring, indent=2))
        if manifest:
            public_manifest = {key: value for key, value in manifest.items() if key not in {"installed_path", "status"}}
            archive.writestr("scoring-manifest.json", json.dumps(public_manifest, indent=2))
        archive.writestr(
            "README.txt",
            "OSIPI challenge configuration export. Private reference maps, masks, measured signals, and scoring code are excluded.\n",
        )
    return output.getvalue(), f"osipi-{challenge}-configuration.zip"


def import_configuration(zip_path: Path) -> dict[str, Any]:
    if not zipfile.is_zipfile(zip_path):
        raise ValueError("Configuration import must be a ZIP archive.")
    with zipfile.ZipFile(zip_path) as archive:
        names = set(archive.namelist())
        if "validation_rules.yaml" not in names or "metadata.json" not in names:
            raise ValueError("ZIP must contain validation_rules.yaml and metadata.json.")
        candidate = yaml.safe_load(archive.read("validation_rules.yaml").decode("utf-8"))
        metadata = json.loads(archive.read("metadata.json").decode("utf-8"))
        scoring = (
            json.loads(archive.read("scoring-selection.json").decode("utf-8"))
            if "scoring-selection.json" in names else {"mode": "none"}
        )
    rules_module.validate_validation_rules_data(candidate, label="imported validation_rules.yaml")
    challenge = str(metadata.get("challenge_type") or "").lower()
    if challenge not in candidate.get("challenges", {}):
        raise ValueError("Imported metadata names a challenge not present in the rules.")
    # Import is save-only. The organiser must test and explicitly activate it.
    saved = _save_full_rules(challenge, candidate, scoring, "import")
    return {"imported": True, "activated": False, "version": {**saved, "active": False}}


def capability_matrix() -> list[dict[str, Any]]:
    rows = []
    analysis_rules = rules_module.analysis_by_challenge()
    map_specs = rules_module.map_type_specs()
    artifact_specs = rules_module.artifact_type_specs()
    for challenge in rules_module.challenge_types():
        assets = asset_status(challenge)
        counts = assets["counts"]
        active = get_active_entry(challenge)
        analysis = analysis_rules.get(challenge, {})
        roi = analysis.get("roi_descriptive") or {}
        roi_maps = [
            str((map_specs.get(str(map_id).lower()) or {}).get("display") or map_id)
            for map_id in roi.get("map_types") or []
        ] if roi.get("enabled", False) else []
        rss = analysis.get("signal_rss") or {}
        modelled_id = str(rss.get("modelled_artifact") or "")
        measured_id = str(rss.get("measured_artifact") or "")
        modelled_label = str(
            (artifact_specs.get(modelled_id) or {}).get("label") or modelled_id
        )
        measured_label = str(
            (artifact_specs.get(measured_id) or {}).get("label") or measured_id
        )
        rows.append({
            "challenge_type": challenge,
            "label": rules_module.challenge_labels().get(challenge, challenge.upper()),
            "map_qc": "Available for readable maps",
            "previews": "Available for readable maps",
            "roi_statistics": (
                f"Descriptive statistics for {', '.join(roi_maps)} when compatible masks are available"
                if roi_maps else "Not configured"
            ),
            "reference_comparison": "Available when compatible reference maps are available",
            "difference_maps": "Available with compatible reference comparisons",
            "rss": (
                f"Available for compatible paired {measured_label} and {modelled_label}"
                if rss.get("enabled", False) else "Not configured"
            ),
            "provider_analysis": (
                f"Configured: {active.get('package_name') or active.get('package_id') or active.get('mode')}"
                if active.get("mode") != "none" else "Not configured"
            ),
            "icc": "Not configured",
            "official_ranking": "Not configured",
            "local_assets": counts,
        })
    return rows


def active_configuration_version(challenge_type: str) -> str:
    challenge = str(challenge_type or "").lower()
    active = _read_active_versions().get(challenge) or {}
    return str(active.get("version_id") or f"rules-v{rules_module.validation_rules().get('version', 'unknown')}")
