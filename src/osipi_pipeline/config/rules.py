"""Shared, validated configuration for challenge and pipeline rules."""

from __future__ import annotations

import copy
import re
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import Any

try:
    import yaml
    from yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode
except ImportError:  # pragma: no cover - dependency is declared.
    yaml = None  # type: ignore[assignment]
    MappingNode = Node = ScalarNode = SequenceNode = object  # type: ignore[misc,assignment]


REPO_ROOT = Path(__file__).resolve().parents[3]
CONFIG_DIR = REPO_ROOT / "config"
VALIDATION_RULES_PATH = CONFIG_DIR / "validation_rules.yaml"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"

_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")

_RULE_TOP_LEVEL_KEYS = {
    "version",
    "default_challenge_type",
    "nifti_suffixes",
    "metadata_suffixes",
    "readme_names",
    "code_file_names",
    "code_extensions",
    "code_folder_names",
    "map_types",
    "challenges",
    # Optional. Describes submitted files that are not parameter maps — a 4-D
    # fitted signal, a methods document. Absent in older configs.
    "artifact_types",
}
_MAP_TYPE_KEYS = {"display", "label", "units", "patterns", "dimensions"}
# `expected_maps` is retained unchanged: it means "maps that should be present",
# and a missing one is a warning. `required_maps` / `optional_maps` are the
# newer, explicit split. A challenge that declares neither keeps exactly its
# current behaviour, which is what leaves ASL and DSC untouched.
_CHALLENGE_KEYS = {
    "label",
    "description",
    "expected_maps",
    "keywords",
    "required_maps",
    "optional_maps",
    "required_artifacts",
    "datasets",
    "filename_identity_patterns",
}
# Named groups a filename identity pattern may capture. Anything else is a
# typo or an attempt to smuggle logic into configuration, so it is rejected
# rather than silently ignored.
_IDENTITY_GROUPS = {"dataset", "participant", "repeat", "site"}
_ARTIFACT_TYPE_KEYS = {"role", "dimensions", "suffixes", "patterns", "label"}
_DATASET_KEYS = {"participants", "repeats", "sites"}

_SETTINGS_TOP_LEVEL_KEYS = {"version", "defaults", "limits", "reporting", "performance", "paths", "ingestion"}
_SETTINGS_SECTION_KEYS = {
    "defaults": {"challenge_type", "scoring_map_type", "validation_mode"},
    "limits": {"zip_max_bytes", "extract_max_bytes", "extract_max_files"},
    "reporting": {"default_blinded", "percent_aggregation", "include_pdf_export"},
    "performance": {
        "nifti_validation_workers",
        "batch_validation_workers",
        "validation_cache_enabled",
        "manifest_cache_enabled",
        "preview_cache_enabled",
    },
    "paths": {
        "output_map_subdirs",
        "private_path_parts",
        "mask_name_patterns",
        "mask_label_rules",
    },
    "ingestion": {"skip_prefixes", "skip_names", "structural_subdirs"},
}
_MASK_LABEL_RULE_KEYS = {"label", "patterns"}


class ConfigValidationError(RuntimeError):
    """Raised when repository configuration is malformed or unsafe."""


def clear_config_cache() -> None:
    """Clear cached config after tests or tools change config paths."""

    validation_rules.cache_clear()
    app_settings.cache_clear()


def _config_label(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def _format_errors(path: Path, errors: list[str]) -> ConfigValidationError:
    label = _config_label(path)
    body = "\n".join(f"- {error}" for error in errors)
    return ConfigValidationError(f"{label} failed validation:\n{body}")


def _node_key(key_node: Node) -> str:
    if isinstance(key_node, ScalarNode):
        return str(key_node.value)
    return "<non-scalar-key>"


def _detect_duplicate_keys(node: Node, path: str = "") -> list[str]:
    errors: list[str] = []
    if isinstance(node, MappingNode):
        seen: set[str] = set()
        for key_node, value_node in node.value:
            key = _node_key(key_node)
            child_path = f"{path}.{key}" if path else key
            if key in seen:
                errors.append(f"{child_path}: duplicate identifier")
            seen.add(key)
            errors.extend(_detect_duplicate_keys(value_node, child_path))
    elif isinstance(node, SequenceNode):
        for index, child in enumerate(node.value):
            child_path = f"{path}[{index}]" if path else f"[{index}]"
            errors.extend(_detect_duplicate_keys(child, child_path))
    return errors


def _read_yaml(path: Path) -> dict[str, Any]:
    if yaml is None:
        raise ConfigValidationError("PyYAML is required to load configuration.")
    if not path.exists():
        raise ConfigValidationError(f"{_config_label(path)} is missing.")
    text = path.read_text(encoding="utf-8")
    try:
        node = yaml.compose(text)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"{_config_label(path)}: malformed YAML: {exc}") from exc
    if node is None:
        raise ConfigValidationError(f"{_config_label(path)}: configuration file is empty.")
    duplicate_errors = _detect_duplicate_keys(node)
    if duplicate_errors:
        raise _format_errors(path, duplicate_errors)
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigValidationError(f"{_config_label(path)}: malformed YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigValidationError(f"{_config_label(path)}: root must be a mapping.")
    return data


def _path(path: str, key: Any) -> str:
    return f"{path}.{key}" if path else str(key)


def _reject_unknown_keys(
    mapping: dict[Any, Any],
    allowed: set[str],
    path: str,
    errors: list[str],
) -> None:
    for key in mapping:
        key_path = _path(path, key)
        if not isinstance(key, str):
            errors.append(f"{key_path}: key must be a string")
        elif key not in allowed:
            errors.append(f"{key_path}: unknown key")


def _require_mapping(value: Any, path: str, errors: list[str]) -> dict[Any, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{path}: must be a mapping")
        return None
    return value


def _require_string(value: Any, path: str, errors: list[str], *, allow_blank: bool = False) -> str | None:
    if not isinstance(value, str):
        errors.append(f"{path}: must be a string")
        return None
    if not allow_blank and not value.strip():
        errors.append(f"{path}: must not be blank")
    return value


def _require_string_list(
    value: Any,
    path: str,
    errors: list[str],
    *,
    allow_empty: bool = False,
    allow_blank_items: bool = False,
) -> list[str]:
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return []
    if not value and not allow_empty:
        errors.append(f"{path}: must not be empty")
    result: list[str] = []
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        if not isinstance(item, str):
            errors.append(f"{item_path}: must be a string")
            continue
        if not allow_blank_items and not item.strip():
            errors.append(f"{item_path}: must not be blank")
        result.append(item)
    return result


def _require_positive_int(value: Any, path: str, errors: list[str]) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        errors.append(f"{path}: must be an integer")
        return
    if value <= 0:
        errors.append(f"{path}: must be greater than 0")


def _require_bool(value: Any, path: str, errors: list[str]) -> None:
    if not isinstance(value, bool):
        errors.append(f"{path}: must be a boolean")


def _validate_identifier(raw: Any, path: str, errors: list[str]) -> str | None:
    if not isinstance(raw, str):
        errors.append(f"{path}: identifier must be a string")
        return None
    if not _ID_RE.match(raw):
        errors.append(f"{path}: identifier must use letters, digits, underscores, or hyphens")
        return None
    return raw.lower()


def _check_duplicate_normalized_ids(keys: list[Any], path: str, errors: list[str]) -> None:
    seen: dict[str, Any] = {}
    for key in keys:
        norm = _validate_identifier(key, _path(path, key), errors)
        if not norm:
            continue
        if norm in seen:
            errors.append(f"{_path(path, key)}: duplicate identifier also defined as {seen[norm]!r}")
        else:
            seen[norm] = key


def _is_safe_relative(value: str, *, allow_empty: bool = False) -> bool:
    if value == "" and allow_empty:
        return True
    if not value.strip():
        return False
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    return not path.is_absolute() and ".." not in path.parts


def _validate_relative_path_list(
    value: Any,
    path: str,
    errors: list[str],
    *,
    allow_empty_items: bool = False,
) -> list[str]:
    items = _require_string_list(
        value,
        path,
        errors,
        allow_empty=True,
        allow_blank_items=allow_empty_items,
    )
    for index, item in enumerate(items):
        if not _is_safe_relative(item, allow_empty=allow_empty_items):
            errors.append(f"{path}[{index}]: must be a relative path that does not escape the project root")
    return items


def _validate_validation_rules(rules: dict[str, Any], path: Path) -> dict[str, Any]:
    errors: list[str] = []
    _reject_unknown_keys(rules, _RULE_TOP_LEVEL_KEYS, "", errors)

    for key in ("version", "default_challenge_type", "map_types", "challenges"):
        if key not in rules:
            errors.append(f"{key}: required section is missing")
    for key in (
        "nifti_suffixes",
        "metadata_suffixes",
        "readme_names",
        "code_file_names",
        "code_extensions",
        "code_folder_names",
    ):
        if key not in rules:
            errors.append(f"{key}: required section is missing")
        else:
            _require_string_list(rules.get(key), key, errors)

    if "version" in rules:
        _require_positive_int(rules.get("version"), "version", errors)

    default_challenge = None
    if "default_challenge_type" in rules:
        default_challenge = _require_string(rules.get("default_challenge_type"), "default_challenge_type", errors)

    map_types = _require_mapping(rules.get("map_types"), "map_types", errors) if "map_types" in rules else None
    challenge_config = _require_mapping(rules.get("challenges"), "challenges", errors) if "challenges" in rules else None

    map_ids: set[str] = set()
    if map_types is not None:
        _check_duplicate_normalized_ids(list(map_types.keys()), "map_types", errors)
        for raw_id, spec in map_types.items():
            map_id = _validate_identifier(raw_id, f"map_types.{raw_id}", errors)
            if map_id:
                map_ids.add(map_id)
            spec_path = f"map_types.{raw_id}"
            spec_map = _require_mapping(spec, spec_path, errors)
            if spec_map is None:
                continue
            _reject_unknown_keys(spec_map, _MAP_TYPE_KEYS, spec_path, errors)
            for field in ("display", "label", "patterns"):
                if field not in spec_map:
                    errors.append(f"{spec_path}.{field}: required field is missing")
            if "display" in spec_map:
                _require_string(spec_map.get("display"), f"{spec_path}.display", errors)
            if "label" in spec_map:
                _require_string(spec_map.get("label"), f"{spec_path}.label", errors)
            if "units" in spec_map and spec_map.get("units") is not None:
                _require_string(spec_map.get("units"), f"{spec_path}.units", errors, allow_blank=True)
            if "patterns" in spec_map:
                _require_string_list(spec_map.get("patterns"), f"{spec_path}.patterns", errors)
            if "dimensions" in spec_map and spec_map.get("dimensions") is not None:
                dims = spec_map.get("dimensions")
                if not isinstance(dims, int) or isinstance(dims, bool) or dims < 2 or dims > 7:
                    errors.append(f"{spec_path}.dimensions: must be an integer between 2 and 7")

    # ── artifact_types (optional) ─────────────────────────────────────────
    # Validated before challenges so required_artifacts can be checked
    # against the resolved set of artifact ids.
    artifact_ids: set[str] = set()
    if "artifact_types" in rules:
        artifact_types = _require_mapping(rules.get("artifact_types"), "artifact_types", errors)
        if artifact_types is not None:
            _check_duplicate_normalized_ids(list(artifact_types.keys()), "artifact_types", errors)
            for raw_id, spec in artifact_types.items():
                artifact_id = _validate_identifier(raw_id, f"artifact_types.{raw_id}", errors)
                if artifact_id:
                    artifact_ids.add(artifact_id)
                spec_path = f"artifact_types.{raw_id}"
                spec_map = _require_mapping(spec, spec_path, errors)
                if spec_map is None:
                    continue
                _reject_unknown_keys(spec_map, _ARTIFACT_TYPE_KEYS, spec_path, errors)
                for field in ("role", "suffixes", "patterns"):
                    if field not in spec_map:
                        errors.append(f"{spec_path}.{field}: required field is missing")
                if "role" in spec_map:
                    _require_string(spec_map.get("role"), f"{spec_path}.role", errors)
                if "label" in spec_map and spec_map.get("label") is not None:
                    _require_string(spec_map.get("label"), f"{spec_path}.label", errors)
                if "suffixes" in spec_map:
                    _require_string_list(spec_map.get("suffixes"), f"{spec_path}.suffixes", errors)
                if "patterns" in spec_map:
                    _require_string_list(spec_map.get("patterns"), f"{spec_path}.patterns", errors)
                # Same bounds as map_types.dimensions, and equally optional:
                # a methods document has no dimensionality at all.
                if "dimensions" in spec_map and spec_map.get("dimensions") is not None:
                    dims = spec_map.get("dimensions")
                    if not isinstance(dims, int) or isinstance(dims, bool) or dims < 2 or dims > 7:
                        errors.append(f"{spec_path}.dimensions: must be an integer between 2 and 7")

    challenge_ids: set[str] = set()
    if challenge_config is not None:
        _check_duplicate_normalized_ids(list(challenge_config.keys()), "challenges", errors)
        for raw_id, spec in challenge_config.items():
            challenge_id = _validate_identifier(raw_id, f"challenges.{raw_id}", errors)
            if challenge_id:
                challenge_ids.add(challenge_id)
            spec_path = f"challenges.{raw_id}"
            spec_map = _require_mapping(spec, spec_path, errors)
            if spec_map is None:
                continue
            _reject_unknown_keys(spec_map, _CHALLENGE_KEYS, spec_path, errors)
            for field in ("label", "expected_maps", "keywords"):
                if field not in spec_map:
                    errors.append(f"{spec_path}.{field}: required field is missing")
            if "label" in spec_map:
                _require_string(spec_map.get("label"), f"{spec_path}.label", errors)
            if "description" in spec_map and spec_map.get("description") is not None:
                _require_string(spec_map.get("description"), f"{spec_path}.description", errors)
            expected = _require_string_list(
                spec_map.get("expected_maps"),
                f"{spec_path}.expected_maps",
                errors,
            ) if "expected_maps" in spec_map else []
            for index, map_id in enumerate(expected):
                if map_id.lower() not in map_ids:
                    errors.append(
                        f"{spec_path}.expected_maps[{index}]: unknown map id {map_id!r}"
                    )
            if "keywords" in spec_map:
                _require_string_list(spec_map.get("keywords"), f"{spec_path}.keywords", errors)

            # ── optional DCE-2026 fields ──────────────────────────────────
            # Each is independently optional. Absent means "not declared",
            # which preserves the existing expected_maps-only behaviour.
            for field in ("required_maps", "optional_maps"):
                if field not in spec_map:
                    continue
                declared = _require_string_list(
                    spec_map.get(field), f"{spec_path}.{field}", errors
                )
                for index, map_id in enumerate(declared):
                    if map_id.lower() not in map_ids:
                        errors.append(
                            f"{spec_path}.{field}[{index}]: unknown map id {map_id!r}"
                        )

            if "required_artifacts" in spec_map:
                declared = _require_string_list(
                    spec_map.get("required_artifacts"),
                    f"{spec_path}.required_artifacts",
                    errors,
                )
                for index, artifact_id in enumerate(declared):
                    if artifact_id.lower() not in artifact_ids:
                        errors.append(
                            f"{spec_path}.required_artifacts[{index}]: "
                            f"unknown artifact id {artifact_id!r}"
                        )

            if "filename_identity_patterns" in spec_map:
                patterns = _require_string_list(
                    spec_map.get("filename_identity_patterns"),
                    f"{spec_path}.filename_identity_patterns",
                    errors,
                )
                for index, pattern in enumerate(patterns):
                    item_path = f"{spec_path}.filename_identity_patterns[{index}]"
                    # Compile at load time so a broken pattern fails startup
                    # rather than silently matching nothing on every upload.
                    try:
                        compiled = re.compile(pattern)
                    except re.error as exc:
                        errors.append(f"{item_path}: invalid regular expression ({exc})")
                        continue
                    groups = set(compiled.groupindex)
                    unknown = sorted(groups - _IDENTITY_GROUPS)
                    if unknown:
                        errors.append(
                            f"{item_path}: unknown named group(s) "
                            f"{', '.join(repr(g) for g in unknown)}; allowed: "
                            f"{', '.join(sorted(_IDENTITY_GROUPS))}"
                        )
                    if not groups & _IDENTITY_GROUPS:
                        errors.append(
                            f"{item_path}: must capture at least one of "
                            f"{', '.join(sorted(_IDENTITY_GROUPS))}"
                        )

            if "datasets" in spec_map:
                datasets = _require_mapping(
                    spec_map.get("datasets"), f"{spec_path}.datasets", errors
                )
                if datasets is not None:
                    _check_duplicate_normalized_ids(
                        list(datasets.keys()), f"{spec_path}.datasets", errors
                    )
                    for raw_name, dataset in datasets.items():
                        ds_path = f"{spec_path}.datasets.{raw_name}"
                        _validate_identifier(raw_name, ds_path, errors)
                        ds_map = _require_mapping(dataset, ds_path, errors)
                        if ds_map is None:
                            continue
                        _reject_unknown_keys(ds_map, _DATASET_KEYS, ds_path, errors)
                        for field in ("repeats", "sites"):
                            if field not in ds_map:
                                errors.append(f"{ds_path}.{field}: required field is missing")
                            else:
                                _require_positive_int(
                                    ds_map.get(field), f"{ds_path}.{field}", errors
                                )
                        # participants may be null: OSIPI has not finalised the
                        # synthetic cohort size, and a placeholder integer would
                        # read as a decision that has not been made.
                        if "participants" not in ds_map:
                            errors.append(f"{ds_path}.participants: required field is missing")
                        elif ds_map.get("participants") is not None:
                            _require_positive_int(
                                ds_map.get("participants"), f"{ds_path}.participants", errors
                            )

    if default_challenge and default_challenge.lower() not in challenge_ids:
        errors.append(f"default_challenge_type: unknown challenge id {default_challenge!r}")

    if errors:
        raise _format_errors(path, errors)
    return copy.deepcopy(rules)


def _display_to_map_ids(rules: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for map_id, spec in rules.get("map_types", {}).items():
        norm_id = str(map_id).lower()
        result[norm_id] = norm_id
        display = str((spec or {}).get("display") or map_id).lower()
        result[display] = norm_id
    return result


def _validate_mask_label_rules(value: Any, path: str, errors: list[str]) -> None:
    if value is None:
        return
    if not isinstance(value, list):
        errors.append(f"{path}: must be a list")
        return
    for index, item in enumerate(value):
        item_path = f"{path}[{index}]"
        item_map = _require_mapping(item, item_path, errors)
        if item_map is None:
            continue
        _reject_unknown_keys(item_map, _MASK_LABEL_RULE_KEYS, item_path, errors)
        if "label" not in item_map:
            errors.append(f"{item_path}.label: required field is missing")
        else:
            _require_string(item_map.get("label"), f"{item_path}.label", errors)
        if "patterns" not in item_map:
            errors.append(f"{item_path}.patterns: required field is missing")
        else:
            _require_string_list(item_map.get("patterns"), f"{item_path}.patterns", errors)


def _validate_settings(settings: dict[str, Any], path: Path, rules: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    _reject_unknown_keys(settings, _SETTINGS_TOP_LEVEL_KEYS, "", errors)

    for key in ("version", "defaults", "limits", "reporting", "paths", "ingestion"):
        if key not in settings:
            errors.append(f"{key}: required section is missing")

    if "version" in settings:
        _require_positive_int(settings.get("version"), "version", errors)

    sections: dict[str, dict[Any, Any]] = {}
    for section, allowed in _SETTINGS_SECTION_KEYS.items():
        if section not in settings:
            continue
        section_map = _require_mapping(settings.get(section), section, errors)
        if section_map is None:
            continue
        _reject_unknown_keys(section_map, allowed, section, errors)
        sections[section] = section_map

    defaults = sections.get("defaults", {})
    for key in ("challenge_type", "scoring_map_type", "validation_mode"):
        if key not in defaults:
            errors.append(f"defaults.{key}: required field is missing")
    default_challenge = None
    if "challenge_type" in defaults:
        default_challenge = _require_string(defaults.get("challenge_type"), "defaults.challenge_type", errors)
    default_map = None
    if "scoring_map_type" in defaults:
        default_map = _require_string(defaults.get("scoring_map_type"), "defaults.scoring_map_type", errors)
    if "validation_mode" in defaults:
        mode = _require_string(defaults.get("validation_mode"), "defaults.validation_mode", errors)
        if mode and mode not in {"auto", "result_only", "result_validation", "reproducible"}:
            errors.append("defaults.validation_mode: must be one of auto, result_only, result_validation, reproducible")

    challenge_by_norm = {str(key).lower(): value for key, value in rules.get("challenges", {}).items()}
    challenge_ids = set(challenge_by_norm)
    if default_challenge and default_challenge.lower() not in challenge_ids:
        errors.append(f"defaults.challenge_type: unknown challenge id {default_challenge!r}")

    map_lookup = _display_to_map_ids(rules)
    if default_map:
        resolved_map = map_lookup.get(default_map.lower())
        if not resolved_map:
            errors.append(f"defaults.scoring_map_type: unknown map id/display {default_map!r}")
        elif default_challenge and default_challenge.lower() in challenge_ids:
            expected = {
                str(item).lower()
                for item in challenge_by_norm[default_challenge.lower()].get("expected_maps", [])
            }
            if resolved_map not in expected:
                errors.append(
                    "defaults.scoring_map_type: default map must belong to "
                    f"configured default challenge {default_challenge!r}"
                )

    limits = sections.get("limits", {})
    for key in ("zip_max_bytes", "extract_max_bytes", "extract_max_files"):
        if key not in limits:
            errors.append(f"limits.{key}: required field is missing")
        else:
            _require_positive_int(limits.get(key), f"limits.{key}", errors)
    if (
        isinstance(limits.get("zip_max_bytes"), int)
        and isinstance(limits.get("extract_max_bytes"), int)
        and not isinstance(limits.get("zip_max_bytes"), bool)
        and not isinstance(limits.get("extract_max_bytes"), bool)
        and limits["extract_max_bytes"] < limits["zip_max_bytes"]
    ):
        errors.append("limits.extract_max_bytes: must be greater than or equal to limits.zip_max_bytes")

    reporting = sections.get("reporting", {})
    for key in ("default_blinded", "include_pdf_export"):
        if key not in reporting:
            errors.append(f"reporting.{key}: required field is missing")
        else:
            _require_bool(reporting.get(key), f"reporting.{key}", errors)
    if "percent_aggregation" not in reporting:
        errors.append("reporting.percent_aggregation: required field is missing")
    else:
        aggregation = _require_string(reporting.get("percent_aggregation"), "reporting.percent_aggregation", errors)
        if aggregation and aggregation not in {"voxel_weighted", "mean"}:
            errors.append("reporting.percent_aggregation: must be voxel_weighted or mean")

    performance = sections.get("performance", {})
    for key in ("nifti_validation_workers", "batch_validation_workers"):
        if key in performance:
            _require_positive_int(performance.get(key), f"performance.{key}", errors)
    for key in ("validation_cache_enabled", "manifest_cache_enabled", "preview_cache_enabled"):
        if key in performance:
            _require_bool(performance.get(key), f"performance.{key}", errors)

    paths = sections.get("paths", {})
    for key in ("output_map_subdirs", "private_path_parts", "mask_name_patterns"):
        if key not in paths:
            errors.append(f"paths.{key}: required field is missing")
    if "output_map_subdirs" in paths:
        _validate_relative_path_list(
            paths.get("output_map_subdirs"),
            "paths.output_map_subdirs",
            errors,
            allow_empty_items=True,
        )
    if "private_path_parts" in paths:
        _validate_relative_path_list(paths.get("private_path_parts"), "paths.private_path_parts", errors)
    if "mask_name_patterns" in paths:
        _require_string_list(paths.get("mask_name_patterns"), "paths.mask_name_patterns", errors)
    _validate_mask_label_rules(paths.get("mask_label_rules"), "paths.mask_label_rules", errors)

    ingestion = sections.get("ingestion", {})
    for key in ("skip_prefixes", "skip_names", "structural_subdirs"):
        if key not in ingestion:
            errors.append(f"ingestion.{key}: required field is missing")
    for key in ("skip_prefixes", "skip_names", "structural_subdirs"):
        if key in ingestion:
            _validate_relative_path_list(ingestion.get(key), f"ingestion.{key}", errors)

    if errors:
        raise _format_errors(path, errors)
    return copy.deepcopy(settings)


@lru_cache(maxsize=1)
def validation_rules() -> dict[str, Any]:
    """Return validated rules from ``config/validation_rules.yaml``."""

    return _validate_validation_rules(_read_yaml(VALIDATION_RULES_PATH), VALIDATION_RULES_PATH)


@lru_cache(maxsize=1)
def app_settings() -> dict[str, Any]:
    """Return validated settings from ``config/settings.yaml``."""

    rules = validation_rules()
    return _validate_settings(_read_yaml(SETTINGS_PATH), SETTINGS_PATH, rules)


def validate_config_files() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load and validate both repository config files."""

    clear_config_cache()
    return validation_rules(), app_settings()


def default_challenge_type() -> str:
    rules_default = str(validation_rules().get("default_challenge_type") or "").strip().lower()
    settings_default = str(app_settings().get("defaults", {}).get("challenge_type") or "").strip().lower()
    return rules_default or settings_default


def default_scoring_map_type() -> str:
    configured = str(app_settings().get("defaults", {}).get("scoring_map_type") or "").strip()
    if configured:
        return configured
    first = next(iter(map_type_specs().values()), {})
    return str(first.get("display") or "")


def challenge_types() -> tuple[str, ...]:
    return tuple(str(key).lower() for key in validation_rules().get("challenges", {}).keys())


def challenge_labels() -> dict[str, str]:
    return {
        str(key).lower(): str(value.get("label") or key).strip()
        for key, value in validation_rules().get("challenges", {}).items()
        if isinstance(value, dict)
    }


def expected_maps_by_challenge() -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for challenge, config in validation_rules().get("challenges", {}).items():
        maps = config.get("expected_maps") or []
        result[str(challenge).lower()] = tuple(str(item).lower() for item in maps)
    return result


def _challenge_id_list(field: str) -> dict[str, tuple[str, ...]]:
    """Shared reader for the per-challenge id-list fields.

    Challenges that do not declare ``field`` map to an empty tuple rather
    than being omitted, so callers can index by challenge without guarding.
    """
    result: dict[str, tuple[str, ...]] = {}
    for challenge, config in validation_rules().get("challenges", {}).items():
        values = config.get(field) or []
        result[str(challenge).lower()] = tuple(str(item).lower() for item in values)
    return result


def required_maps_by_challenge() -> dict[str, tuple[str, ...]]:
    """Map ids a submission must provide, per challenge.

    Empty for any challenge that has not declared ``required_maps``. Nothing
    enforces this yet — Phase 1 only describes the requirement. Existing
    ``expected_maps`` behaviour is untouched, which is what keeps ASL and DSC
    validation identical.
    """
    return _challenge_id_list("required_maps")


def optional_maps_by_challenge() -> dict[str, tuple[str, ...]]:
    """Map ids that are accepted but not required, per challenge."""
    return _challenge_id_list("optional_maps")


def required_artifacts_by_challenge() -> dict[str, tuple[str, ...]]:
    """Artifact ids a submission must provide, per challenge.

    Ids are guaranteed by schema validation to exist in ``artifact_types``.
    """
    return _challenge_id_list("required_artifacts")


def filename_identity_patterns_by_challenge() -> dict[str, tuple[str, ...]]:
    """Ordered filename identity regexes per challenge.

    Returns validated pattern *strings*, not compiled objects: the rules
    mapping is deep-copied and JSON-fingerprinted by the manifest layer, and
    a compiled pattern is neither copyable nor serialisable. The parser
    compiles them once behind its own cache.

    Order is significant — the first pattern that matches wins.
    """
    return _challenge_id_list_raw("filename_identity_patterns")


def _challenge_id_list_raw(field: str) -> dict[str, tuple[str, ...]]:
    """Like :func:`_challenge_id_list` but preserves original case.

    Regexes are case-sensitive by construction, so lowercasing them would
    silently change what they match.
    """
    result: dict[str, tuple[str, ...]] = {}
    for challenge, config in validation_rules().get("challenges", {}).items():
        values = config.get(field) or []
        result[str(challenge).lower()] = tuple(str(item) for item in values)
    return result


def artifact_type_specs() -> dict[str, dict[str, Any]]:
    """Non-parameter-map submission artifacts, keyed by lowercase id.

    Covers roles such as a 4-D fitted signal or a methods document. Returns
    a deep copy so callers cannot mutate the cached configuration, matching
    :func:`map_type_specs`. Empty when the optional section is absent.
    """
    return {
        str(key).lower(): copy.deepcopy(value)
        for key, value in (validation_rules().get("artifact_types") or {}).items()
    }


def datasets_by_challenge() -> dict[str, dict[str, dict[str, int | None]]]:
    """Expected dataset structure per challenge, keyed by dataset name.

    Each dataset yields ``participants``, ``repeats`` and ``sites``.
    ``participants`` is ``None`` when the organiser has not finalised the
    cohort size; the schema permits this deliberately so an unknown count is
    not misrepresented as a decided one. Dataset names are not restricted to
    synthetic/clinical — a future challenge may define its own.
    """
    result: dict[str, dict[str, dict[str, int | None]]] = {}
    for challenge, config in validation_rules().get("challenges", {}).items():
        datasets = config.get("datasets") or {}
        result[str(challenge).lower()] = {
            str(name).lower(): {
                "participants": spec.get("participants"),
                "repeats": spec.get("repeats"),
                "sites": spec.get("sites"),
            }
            for name, spec in datasets.items()
        }
    return result


def map_type_patterns(*, display_keys: bool = False) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    for key, config in validation_rules().get("map_types", {}).items():
        result_key = str(config.get("display") or key) if display_keys else str(key).lower()
        patterns = config.get("patterns") or [key]
        result[result_key] = tuple(str(item).lower() for item in patterns)
    return result


def known_auto_detected_labels() -> frozenset[str]:
    labels = {
        str(config.get("display") or key)
        for key, config in validation_rules().get("map_types", {}).items()
    }
    labels.add("Mixed/Other")
    return frozenset(labels)


def challenge_keyword_config() -> dict[str, dict[str, tuple[str, ...]]]:
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for challenge, config in validation_rules().get("challenges", {}).items():
        result[str(challenge).lower()] = {
            "keywords": tuple(str(item).lower() for item in (config.get("keywords") or []))
        }
    return result


def map_type_specs() -> dict[str, dict[str, Any]]:
    return {
        str(key).lower(): copy.deepcopy(value)
        for key, value in validation_rules().get("map_types", {}).items()
    }


def tuple_setting(name: str) -> tuple[str, ...]:
    value = validation_rules().get(name) or []
    return tuple(str(item).lower() for item in value)


def settings_tuple(section: str, name: str) -> tuple[str, ...]:
    section_data = app_settings().get(section, {})
    if not isinstance(section_data, dict):
        return ()
    value = section_data.get(name) or []
    return tuple(str(item) for item in value)


def output_map_subpaths() -> tuple[str, ...]:
    return settings_tuple("paths", "output_map_subdirs")


def private_path_parts() -> frozenset[str]:
    return frozenset(str(item).lower() for item in settings_tuple("paths", "private_path_parts"))


def mask_name_patterns() -> tuple[str, ...]:
    return tuple(str(item).lower() for item in settings_tuple("paths", "mask_name_patterns"))


def mask_label_rules() -> tuple[dict[str, tuple[str, ...] | str], ...]:
    rules = app_settings().get("paths", {}).get("mask_label_rules") or []
    result: list[dict[str, tuple[str, ...] | str]] = []
    for item in rules:
        if not isinstance(item, dict):
            continue
        result.append({
            "label": str(item.get("label") or "").strip(),
            "patterns": tuple(str(pattern).lower() for pattern in (item.get("patterns") or [])),
        })
    return tuple(result)


def performance_settings() -> dict[str, Any]:
    settings = app_settings().get("performance") or {}
    return copy.deepcopy(settings) if isinstance(settings, dict) else {}
