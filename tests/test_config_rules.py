from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from osipi_pipeline.config import rules as config_rules


def _base_rules() -> dict:
    return {
        "version": 1,
        "default_challenge_type": "dce",
        "nifti_suffixes": [".nii", ".nii.gz"],
        "metadata_suffixes": [".json", ".yaml"],
        "readme_names": ["readme.md"],
        "code_file_names": ["dockerfile", "run.py"],
        "code_extensions": [".py"],
        "code_folder_names": ["src"],
        "map_types": {
            "ktrans": {
                "display": "Ktrans",
                "label": "Volume transfer constant",
                "units": "min^-1",
                "patterns": ["ktrans"],
            },
            "kep": {
                "display": "Kep",
                "label": "Rate constant",
                "units": None,
                "patterns": ["kep"],
            },
            "vp": {
                "display": "Vp",
                "label": "Plasma volume fraction",
                "units": None,
                "patterns": ["vp"],
            },
        },
        "challenges": {
            "dce": {
                "label": "DCE",
                "description": "Dynamic contrast enhanced MRI",
                "expected_maps": ["ktrans", "kep", "vp"],
                "keywords": ["dce", "ktrans"],
            },
        },
    }


def _base_settings() -> dict:
    return {
        "version": 1,
        "defaults": {
            "challenge_type": "dce",
            "scoring_map_type": "Ktrans",
            "validation_mode": "auto",
        },
        "limits": {
            "zip_max_bytes": 1024,
            "extract_max_bytes": 2048,
            "extract_max_files": 10,
        },
        "reporting": {
            "default_blinded": True,
            "percent_aggregation": "voxel_weighted",
            "include_pdf_export": True,
        },
        "paths": {
            "output_map_subdirs": ["results/maps", "results", ""],
            "private_path_parts": ["reference", "masks"],
            "mask_name_patterns": ["mask", "roi"],
            "mask_label_rules": [
                {"label": "brain mask", "patterns": ["brain"]},
            ],
        },
        "ingestion": {
            "skip_prefixes": ["__MACOSX"],
            "skip_names": [".DS_Store"],
            "structural_subdirs": ["input", "results"],
        },
    }


def _write_yaml(path: Path, data: dict) -> None:
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _load_temp_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rules_data: dict | str,
    settings_data: dict | str | None = None,
) -> tuple[dict, dict]:
    rules_path = tmp_path / "validation_rules.yaml"
    settings_path = tmp_path / "settings.yaml"
    if isinstance(rules_data, str):
        rules_path.write_text(rules_data, encoding="utf-8")
    else:
        _write_yaml(rules_path, rules_data)
    if settings_data is None:
        settings_data = _base_settings()
    if isinstance(settings_data, str):
        settings_path.write_text(settings_data, encoding="utf-8")
    else:
        _write_yaml(settings_path, settings_data)
    monkeypatch.setattr(config_rules, "VALIDATION_RULES_PATH", rules_path)
    monkeypatch.setattr(config_rules, "SETTINGS_PATH", settings_path)
    config_rules.clear_config_cache()
    try:
        return config_rules.validate_config_files()
    finally:
        config_rules.clear_config_cache()


def _assert_config_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rules_data: dict | str,
    expected: str,
    settings_data: dict | str | None = None,
) -> None:
    with pytest.raises(config_rules.ConfigValidationError) as excinfo:
        _load_temp_config(tmp_path, monkeypatch, rules_data, settings_data)
    assert expected in str(excinfo.value)


def test_current_repository_config_is_valid() -> None:
    config_rules.clear_config_cache()
    try:
        rules, settings = config_rules.validate_config_files()
    finally:
        config_rules.clear_config_cache()
    assert "challenges" in rules
    assert settings["defaults"]["challenge_type"] in rules["challenges"]


def test_missing_required_section_reports_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _base_rules()
    del data["map_types"]
    _assert_config_error(tmp_path, monkeypatch, data, "map_types: required section is missing")


def test_unknown_expected_map_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _base_rules()
    data["challenges"]["dce"]["expected_maps"][2] = "unknown_map"
    _assert_config_error(
        tmp_path,
        monkeypatch,
        data,
        "challenges.dce.expected_maps[2]: unknown map id 'unknown_map'",
    )


def test_duplicate_challenge_id_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = """
version: 1
default_challenge_type: dce
nifti_suffixes: [.nii]
metadata_suffixes: [.json]
readme_names: [readme.md]
code_file_names: [dockerfile]
code_extensions: [.py]
code_folder_names: [src]
map_types:
  ktrans:
    display: Ktrans
    label: Volume transfer constant
    units: min^-1
    patterns: [ktrans]
challenges:
  dce:
    label: DCE
    expected_maps: [ktrans]
    keywords: [dce]
  dce:
    label: Duplicate
    expected_maps: [ktrans]
    keywords: [duplicate]
"""
    _assert_config_error(tmp_path, monkeypatch, text, "challenges.dce: duplicate identifier")


def test_duplicate_map_id_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    text = """
version: 1
default_challenge_type: dce
nifti_suffixes: [.nii]
metadata_suffixes: [.json]
readme_names: [readme.md]
code_file_names: [dockerfile]
code_extensions: [.py]
code_folder_names: [src]
map_types:
  ktrans:
    display: Ktrans
    label: Volume transfer constant
    units: min^-1
    patterns: [ktrans]
  ktrans:
    display: Duplicate
    label: Duplicate
    units:
    patterns: [duplicate]
challenges:
  dce:
    label: DCE
    expected_maps: [ktrans]
    keywords: [dce]
"""
    _assert_config_error(tmp_path, monkeypatch, text, "map_types.ktrans: duplicate identifier")


def test_invalid_default_challenge_reports_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _base_settings()
    settings["defaults"]["challenge_type"] = "missing"
    _assert_config_error(
        tmp_path,
        monkeypatch,
        _base_rules(),
        "defaults.challenge_type: unknown challenge id 'missing'",
        settings,
    )


def test_default_map_must_belong_to_default_challenge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rules = _base_rules()
    rules["map_types"]["extra"] = {
        "display": "Extra",
        "label": "Extra configured map",
        "units": None,
        "patterns": ["extra"],
    }
    settings = _base_settings()
    settings["defaults"]["scoring_map_type"] = "Extra"
    _assert_config_error(
        tmp_path,
        monkeypatch,
        rules,
        "defaults.scoring_map_type: default map must belong",
        settings,
    )


def test_invalid_pattern_type_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = _base_rules()
    data["map_types"]["ktrans"]["patterns"][0] = 123
    _assert_config_error(tmp_path, monkeypatch, data, "map_types.ktrans.patterns[0]: must be a string")


def test_unsafe_configured_path_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _base_settings()
    settings["paths"]["output_map_subdirs"][0] = "../outside"
    _assert_config_error(
        tmp_path,
        monkeypatch,
        _base_rules(),
        "paths.output_map_subdirs[0]: must be a relative path",
        settings,
    )


def test_invalid_numeric_limit_reports_exact_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _base_settings()
    settings["limits"]["extract_max_files"] = 0
    _assert_config_error(
        tmp_path,
        monkeypatch,
        _base_rules(),
        "limits.extract_max_files: must be greater than 0",
        settings,
    )


def test_malformed_yaml_reports_clear_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _assert_config_error(tmp_path, monkeypatch, "version: [", "malformed YAML")
