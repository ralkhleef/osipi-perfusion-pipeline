from __future__ import annotations

import io
import re
import zipfile
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from osipi_pipeline.config import rules as rules_module
from services import configuration_manager_service as manager
from services import path_config as paths
from services import scoring_package_service as packages


@pytest.fixture()
def isolated_manager(tmp_path: Path, monkeypatch):
    rules_path = tmp_path / "config" / "validation_rules.yaml"
    rules_path.parent.mkdir(parents=True)
    rules_path.write_text(rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(rules_module, "VALIDATION_RULES_PATH", rules_path)
    monkeypatch.setattr(paths, "REFERENCE_DATA_DIR", tmp_path / "reference_data")
    monkeypatch.setattr(paths, "CONFIG_MANAGER_DIR", tmp_path / "configuration_manager")
    monkeypatch.setattr(paths, "CONFIG_VERSIONS_DIR", tmp_path / "configuration_manager" / "versions")
    monkeypatch.setattr(paths, "CONFIG_ACTIVE_VERSION", tmp_path / "configuration_manager" / "active.json")
    monkeypatch.setattr(paths, "SCORING_PACKAGES_DIR", tmp_path / "scoring" / "packages")
    monkeypatch.setattr(paths, "SCORING_ACTIVE_CONFIG", tmp_path / "scoring" / "active.json")
    monkeypatch.setattr(paths, "OSIPI_TF62_DIR", tmp_path / "scoring" / "providers" / "tf62")
    monkeypatch.setattr(packages, "SCORING_PACKAGES_DIR", paths.SCORING_PACKAGES_DIR)
    monkeypatch.setattr(packages, "SCORING_ACTIVE_CONFIG", paths.SCORING_ACTIVE_CONFIG)
    rules_module.clear_config_cache()
    yield tmp_path
    rules_module.clear_config_cache()


def _payload(challenge: str = "dce") -> dict:
    state = manager.manager_state(challenge)
    return {"challenge_type": challenge, "configuration": state["editable"]}


def test_manager_exposes_handoff_fields(isolated_manager: Path) -> None:
    state = manager.manager_state("dce")
    editable = state["editable"]
    assert editable["challenge_type"] == "dce"
    assert {item["id"] for item in editable["maps"]} >= {"ktrans", "ve", "vp", "kep"}
    assert "code_execution_required" in editable
    assert "reference_dataset_version" in editable
    assert state["private_data_notice"].startswith("Private organiser data")
    assert all(row["official_ranking"] == "Not configured" for row in state["capabilities"])


@pytest.mark.parametrize("challenge", ["dce", "asl", "dsc"])
def test_every_configured_challenge_round_trips_through_the_manager(
    isolated_manager: Path,
    challenge: str,
) -> None:
    payload = _payload(challenge)
    tested = manager.test_configuration(payload)
    assert tested["ready"] is True
    preview = manager.preview_configuration(payload)
    assert preview["valid"] is True
    assert preview["change_count"] == 0


def test_preview_is_side_effect_free_and_readable(isolated_manager: Path) -> None:
    payload = _payload()
    original = rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8")
    ve = next(item for item in payload["configuration"]["maps"] if item["id"] == "ve")
    ve["state"] = "required"
    preview = manager.preview_configuration(payload)
    assert preview["valid"] is True
    assert any(row["field"].endswith("maps") or "state" in row["field"] for row in preview["changes"])
    assert rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8") == original


def test_invalid_draft_never_changes_active_rules(isolated_manager: Path) -> None:
    payload = _payload()
    original = rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8")
    for item in payload["configuration"]["maps"]:
        item["state"] = "unused"
    tested = manager.test_configuration(payload)
    assert tested["ready"] is False
    assert "NOT been changed" in tested["message"]
    with pytest.raises(ValueError):
        manager.save_version(payload)
    assert rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8") == original


def test_save_then_activate_and_restore_version(isolated_manager: Path) -> None:
    first = manager.save_version(_payload())["version"]
    changed = _payload()
    next(item for item in changed["configuration"]["maps"] if item["id"] == "ve")["state"] = "required"
    second = manager.save_version(changed)["version"]
    assert not any(item["active"] for item in manager.list_versions("dce"))
    manager.activate_version("dce", second["version_id"])
    assert "ve" in rules_module.validation_rules()["challenges"]["dce"]["required_maps"]
    manager.activate_version("dce", first["version_id"])
    assert "ve" not in rules_module.validation_rules()["challenges"]["dce"]["required_maps"]
    assert next(item for item in manager.list_versions("dce") if item["active"])["version_id"] == first["version_id"]


def test_activation_rolls_back_yaml_when_external_activation_fails(isolated_manager: Path, monkeypatch) -> None:
    version = manager.save_version(_payload())["version"]
    original = rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8")

    def fail(*_args, **_kwargs):
        raise ValueError("simulated scorer activation failure")

    monkeypatch.setattr(manager, "set_active_entry", fail)
    with pytest.raises(ValueError, match="simulated"):
        manager.activate_version("dce", version["version_id"])
    assert rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8") == original


def test_activation_rolls_back_when_active_version_marker_fails(
    isolated_manager: Path,
    monkeypatch,
) -> None:
    changed = _payload()
    next(item for item in changed["configuration"]["maps"] if item["id"] == "ve")["state"] = "required"
    version = manager.save_version(changed)["version"]
    original_yaml = rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8")
    original_scoring = packages.get_active_entry("dce")
    real_write = manager._write_active_versions
    calls = 0

    def fail_once(data):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated active marker failure")
        return real_write(data)

    monkeypatch.setattr(manager, "_write_active_versions", fail_once)
    with pytest.raises(OSError, match="active marker"):
        manager.activate_version("dce", version["version_id"])
    assert rules_module.VALIDATION_RULES_PATH.read_text(encoding="utf-8") == original_yaml
    assert packages.get_active_entry("dce") == original_scoring
    assert not paths.CONFIG_ACTIVE_VERSION.exists()


def test_private_asset_is_local_and_excluded_from_export(isolated_manager: Path, tmp_path: Path) -> None:
    source = tmp_path / "secret-reference.nii.gz"
    nib.save(nib.Nifti1Image(np.ones((2, 2, 2), dtype=np.float32), np.eye(4)), source)
    manager.store_private_asset("dce", "reference", source.name, source.read_bytes())
    status = manager.asset_status("dce")
    assert status["items"][0]["readable"] is True

    payload, _filename = manager.export_configuration("dce")
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        assert "validation_rules.yaml" in archive.namelist()
        assert all("secret-reference" not in name for name in archive.namelist())
        assert source.read_bytes() not in payload


def test_import_saves_inactive_version(isolated_manager: Path, tmp_path: Path) -> None:
    exported, _ = manager.export_configuration("dce")
    archive = tmp_path / "configuration.zip"
    archive.write_bytes(exported)
    result = manager.import_configuration(archive)
    assert result["imported"] is True
    assert result["activated"] is False
    assert manager.list_versions("dce")[0]["active"] is False


def test_provenance_names_configuration_package_reference_and_date(isolated_manager: Path) -> None:
    from services.provenance_service import analysis_provenance

    provenance = analysis_provenance("dce")
    assert set(provenance) == {
        "challenge", "challenge_configuration", "scoring_package",
        "pipeline_version", "reference_dataset", "analysis_date",
    }
    assert provenance["challenge"] == "DCE"
    assert provenance["challenge_configuration"].startswith("rules-v")
    assert provenance["scoring_package"] == "not configured"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", provenance["analysis_date"])
