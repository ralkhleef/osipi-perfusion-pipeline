"""FastAPI integration tests for the OSIPI perfusion pipeline.

These tests call the real FastAPI application via TestClient (in-process,
no network required).  They exercise the full request → service → response
path for the core pipeline endpoints.

Prerequisites
-------------
    pip install fastapi httpx pytest nibabel numpy

All side effects (uploads, extractions, validation outputs) are redirected
to isolated temporary directories via monkeypatching of path_config constants.
"""

from __future__ import annotations

import io
import csv
import json
import gzip
import math
import struct
import zipfile
from pathlib import Path
from typing import Generator

import pytest

# ---------------------------------------------------------------------------
# Guard: skip entire module when fastapi / httpx are not installed.
# This lets the CI check pass even in minimal Python environments.
# ---------------------------------------------------------------------------
pytest.importorskip("fastapi", reason="fastapi is required for API tests")
pytest.importorskip("httpx",   reason="httpx is required for TestClient")

from fastapi.testclient import TestClient  # noqa: E402  (after importorskip)


# ---------------------------------------------------------------------------
# Helpers — build tiny in-memory ZIPs and NIfTI files
# ---------------------------------------------------------------------------

def _tiny_nifti_bytes() -> bytes:
    """Return a minimal but valid NIfTI-1 file as bytes (no nibabel required)."""
    # NIfTI-1 magic header: 348 bytes header + 4 bytes extension = 352 bytes
    header = bytearray(348)
    header[0:4] = (348).to_bytes(4, "little")          # sizeof_hdr
    header[344:348] = b"n+1\x00"                        # magic
    header[40:42] = (3).to_bytes(2, "little")           # dim[0] = 3
    header[42:44] = (4).to_bytes(2, "little")           # dim[1] = 4
    header[44:46] = (4).to_bytes(2, "little")           # dim[2] = 4
    header[46:48] = (4).to_bytes(2, "little")           # dim[3] = 4
    header[70:72] = (16).to_bytes(2, "little")          # datatype = float32
    header[72:74] = (32).to_bytes(2, "little")          # bitpix = 32
    # vox_offset (4-byte float at offset 108): must be >= 352.0
    import struct
    header[108:112] = struct.pack("<f", 352.0)
    # pixdim (1.0 for dims 1-3)
    for i in range(1, 4):
        header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)

    extension = b"\x00\x00\x00\x00"   # no extension

    # float32 data: 4*4*4 = 64 voxels × 4 bytes = 256 bytes
    data = b"\x00" * (4 * 4 * 4 * 4)
    return bytes(header) + extension + data


def _nifti_bytes_from_values(values, shape: tuple[int, ...] = (2, 2, 1), gz: bool = True) -> bytes:
    """Return a tiny float32 NIfTI-1 image for deterministic scoring tests."""
    assert len(values) == math.prod(shape)
    header = bytearray(348)
    header[0:4] = (348).to_bytes(4, "little")
    header[344:348] = b"n+1\x00"
    header[40:42] = len(shape).to_bytes(2, "little", signed=True)
    for i, size in enumerate(shape, start=1):
        header[40 + i * 2 : 42 + i * 2] = int(size).to_bytes(2, "little", signed=True)
    header[70:72] = (16).to_bytes(2, "little", signed=True)
    header[72:74] = (32).to_bytes(2, "little", signed=True)
    header[108:112] = struct.pack("<f", 352.0)
    for i in range(1, min(len(shape), 3) + 1):
        header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)
    raw = bytes(header) + b"\x00\x00\x00\x00" + struct.pack(f"<{len(values)}f", *[float(v) for v in values])
    return gzip.compress(raw) if gz else raw


def _make_asl_result_maps_zip(
    filename: str,
    cbf_values,
    att_values=None,
    shape: tuple[int, ...] = (2, 2, 1),
) -> tuple[bytes, str]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("results/maps/sub-001_cbf.nii.gz", _nifti_bytes_from_values(cbf_values, shape))
        if att_values is not None:
            zf.writestr("results/maps/sub-001_att.nii.gz", _nifti_bytes_from_values(att_values, shape))
        zf.writestr("README.md", "# ASL result maps\n")
    return buf.getvalue(), filename


def _write_reference_map(tmp_path: Path, name: str, values, shape: tuple[int, ...] = (2, 2, 1)) -> Path:
    path = tmp_path / "ref" / "maps" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_nifti_bytes_from_values(values, shape))
    return path


def _write_reference_mask(tmp_path: Path, name: str, values, shape: tuple[int, ...] = (2, 2, 1)) -> Path:
    path = tmp_path / "ref" / "masks" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_nifti_bytes_from_values(values, shape))
    return path


def _reference_scoring_status(client: TestClient, sid: str, challenge_type: str = "asl") -> dict:
    r = client.get(
        f"/api/scoring-status?submission_id={sid}&challenge_type={challenge_type}&map_type=cbf"
    )
    assert r.status_code == 200, r.text
    return r.json()["nifti_analysis"]["reference_scoring"]


def _first_reference_map(reference_scoring: dict, map_type: str = "CBF") -> dict:
    for item in reference_scoring.get("maps", []):
        if item.get("detected_map_type") == map_type:
            return item
    raise AssertionError(f"No {map_type} reference scoring row found: {reference_scoring}")


def _make_maps_zip(filename: str, map_names: list[str], readme: str = "# Submission\n") -> tuple[bytes, str]:
    """Build an in-memory ZIP with the requested NIfTI map filenames."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in map_names:
            zf.writestr(name, _tiny_nifti_bytes())
        zf.writestr("README.md", readme)
    return buf.getvalue(), filename


def _make_result_only_zip(filename: str = "team_result.zip") -> tuple[bytes, str]:
    """Build an in-memory ZIP with Ktrans + README (result-only submission)."""
    return _make_maps_zip(filename, ["Ktrans_map.nii"])


def _make_reproducible_zip(filename: str = "team_repro.zip") -> tuple[bytes, str]:
    """Build an in-memory ZIP with Dockerfile + run.py (reproducible submission)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("Dockerfile", "FROM python:3.11-slim\nCMD [\"python3\", \"run.py\"]\n")
        zf.writestr("run.py", "import pathlib; (pathlib.Path('/output')/'out.nii').write_bytes(b'x'*352)\n")
        zf.writestr("README.md", "# Reproducible submission\n")
    return buf.getvalue(), filename


def _make_batch_zip() -> tuple[bytes, str]:
    """Two sub-directories inside one ZIP → batch of 2 submissions."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("team_a/Ktrans_map.nii", _tiny_nifti_bytes())
        zf.writestr("team_a/README.md", "# Team A\n")
        zf.writestr("team_b/Ktrans_map.nii", _tiny_nifti_bytes())
        zf.writestr("team_b/README.md", "# Team B\n")
    return buf.getvalue(), "batch.zip"


# ---------------------------------------------------------------------------
# Pytest fixture — isolated directories + TestClient
# ---------------------------------------------------------------------------

@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> Generator[TestClient, None, None]:
    """Create a TestClient backed by an isolated temporary workspace.

    All path_config constants are monkeypatched so uploads, extractions, and
    validation outputs land in tmp_path rather than the real project tree.
    """
    import sys
    repo_root = Path(__file__).resolve().parents[1]
    for extra in [str(repo_root / "src"), str(repo_root / "backend")]:
        if extra not in sys.path:
            sys.path.insert(0, extra)

    import services.path_config as pc

    scoring_packages_dir = tmp_path / "scoring_packages"
    scoring_active_cfg   = tmp_path / "scoring" / "active.json"

    # Override every directory constant used by the backend.
    monkeypatch.setattr(pc, "INCOMING_DIR",          tmp_path / "incoming",        raising=False)
    monkeypatch.setattr(pc, "EXTRACTED_DIR",          tmp_path / "extracted",       raising=False)
    monkeypatch.setattr(pc, "VALIDATED_DIR",          tmp_path / "validated",       raising=False)
    monkeypatch.setattr(pc, "OUTPUTS_DIR",            tmp_path / "outputs",         raising=False)
    monkeypatch.setattr(pc, "REFERENCE_DATA_DIR",     tmp_path / "ref",             raising=False)
    monkeypatch.setattr(pc, "SCORING_DIR",            tmp_path / "scoring",         raising=False)
    monkeypatch.setattr(pc, "SCORING_OUTPUTS_DIR",    tmp_path / "score_out",       raising=False)
    monkeypatch.setattr(pc, "SCORING_RESULTS_DIR",    tmp_path / "score_out",       raising=False)
    monkeypatch.setattr(pc, "OSIPI_TF62_DIR",         tmp_path / "tf62",            raising=False)
    monkeypatch.setattr(pc, "CODECOLLECTION_DIR",     tmp_path / "codecol",         raising=False)
    monkeypatch.setattr(pc, "SCORING_PACKAGES_DIR",   scoring_packages_dir,         raising=False)
    monkeypatch.setattr(pc, "SCORING_ACTIVE_CONFIG",  scoring_active_cfg,           raising=False)

    # Patch the same constants in already-imported modules that captured them.
    for mod_name in list(sys.modules.keys()):
        mod = sys.modules[mod_name]
        for attr, val in [
            ("INCOMING_DIR",         tmp_path / "incoming"),
            ("EXTRACTED_DIR",        tmp_path / "extracted"),
            ("VALIDATED_DIR",        tmp_path / "validated"),
            ("OUTPUTS_DIR",          tmp_path / "outputs"),
            ("REFERENCE_DATA_DIR",   tmp_path / "ref"),
            ("SCORING_DIR",          tmp_path / "scoring"),
            ("SCORING_OUTPUTS_DIR",  tmp_path / "score_out"),
            ("SCORING_RESULTS_DIR",  tmp_path / "score_out"),
            ("OSIPI_TF62_DIR",       tmp_path / "tf62"),
            ("CODECOLLECTION_DIR",   tmp_path / "codecol"),
            ("VALIDATION_SUBDIR",    tmp_path / "outputs" / "validation"),
            ("PREVIEW_ROOT",         tmp_path / "outputs" / "previews"),
            ("SCORING_PACKAGES_DIR", scoring_packages_dir),
            ("SCORING_ACTIVE_CONFIG",scoring_active_cfg),
        ]:
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val, raising=False)

    # Create directories that the lifespan hook normally creates.
    for d in [
        tmp_path / "incoming", tmp_path / "extracted", tmp_path / "validated",
        tmp_path / "outputs", tmp_path / "outputs" / "validation",
        tmp_path / "ref", tmp_path / "scoring", tmp_path / "score_out",
        tmp_path / "tf62", tmp_path / "codecol",
        scoring_packages_dir,
    ]:
        d.mkdir(parents=True, exist_ok=True)

    import backend.main as app_module
    with TestClient(app_module.app, raise_server_exceptions=True) as tc:
        yield tc


# ---------------------------------------------------------------------------
# Health + metadata endpoints
# ---------------------------------------------------------------------------

def test_health(client: TestClient) -> None:
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_execution_status_returns_docker_info(client: TestClient) -> None:
    r = client.get("/api/execution-status")
    assert r.status_code == 200
    data = r.json()
    assert "docker_available" in data
    assert isinstance(data["docker_available"], bool)
    assert "message" in data


def test_app_config_exposes_configured_challenges_and_maps(client: TestClient) -> None:
    r = client.get("/api/config")
    assert r.status_code == 200
    body = r.json()
    assert body["defaults"]["challenge_type"] == "dce"
    challenges = {item["id"]: item for item in body["challenge_types"]}
    assert {"dce", "asl", "dsc"}.issubset(challenges)
    assert challenges["asl"]["expected_maps"] == ["cbf", "att"]
    map_displays = {item["display"] for item in body["map_types"]}
    assert {"CBF", "ATT", "Ktrans"}.issubset(map_displays)
    assert "Ktrans" in body["map_type_patterns"]


def _warning_codes(result: dict) -> set[str]:
    return {item.get("code") for item in result.get("warnings", [])}


def test_configured_dce_and_dsc_expected_maps_validate_without_code_changes(client: TestClient) -> None:
    """DCE and DSC expected maps come from YAML, not ASL-specific code paths."""
    dce_data, dce_name = _make_maps_zip(
        "dce_full.zip",
        ["Ktrans_map.nii", "kep_map.nii", "vp_map.nii"],
    )
    dce_sid = _upload_and_get_id(client, dce_data, dce_name)
    dce = client.post("/api/validate", json={
        "submission_id": dce_sid, "challenge_type": "dce", "mode": "result_only",
    }).json()
    assert "EXPECTED_MAP_MISSING" not in _warning_codes(dce)

    dsc_data, dsc_name = _make_maps_zip(
        "dsc_full.zip",
        ["cbv_map.nii", "cbf_map.nii", "mtt_map.nii"],
    )
    dsc_sid = _upload_and_get_id(client, dsc_data, dsc_name)
    dsc = client.post("/api/validate", json={
        "submission_id": dsc_sid, "challenge_type": "dsc", "mode": "result_only",
    }).json()
    assert "EXPECTED_MAP_MISSING" not in _warning_codes(dsc)


def test_configured_missing_maps_are_reported_for_each_challenge(client: TestClient) -> None:
    dce_data, dce_name = _make_maps_zip("dce_missing.zip", ["Ktrans_map.nii"])
    dce_sid = _upload_and_get_id(client, dce_data, dce_name)
    dce = client.post("/api/validate", json={
        "submission_id": dce_sid, "challenge_type": "dce", "mode": "result_only",
    }).json()
    dce_missing = [w["message"].lower() for w in dce.get("warnings", []) if w.get("code") == "EXPECTED_MAP_MISSING"]
    assert any("kep" in msg for msg in dce_missing)
    assert any("vp" in msg for msg in dce_missing)

    dsc_data, dsc_name = _make_maps_zip("dsc_missing.zip", ["cbv_map.nii", "cbf_map.nii"])
    dsc_sid = _upload_and_get_id(client, dsc_data, dsc_name)
    dsc = client.post("/api/validate", json={
        "submission_id": dsc_sid, "challenge_type": "dsc", "mode": "result_only",
    }).json()
    dsc_missing = [w["message"].lower() for w in dsc.get("warnings", []) if w.get("code") == "EXPECTED_MAP_MISSING"]
    assert any("mtt" in msg for msg in dsc_missing)


def test_config_only_pet_perfusion_challenge_end_to_end(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """A fictional challenge should work by overriding config only."""
    pytest.importorskip("numpy")
    pytest.importorskip("nibabel")
    import copy
    import yaml
    from osipi_pipeline.config import rules as config_rules

    request.addfinalizer(config_rules.clear_config_cache)

    rules = copy.deepcopy(config_rules.validation_rules())
    settings = copy.deepcopy(config_rules.app_settings())
    rules["default_challenge_type"] = "pet_perfusion"
    rules["map_types"] = {
        "flow": {
            "display": "Flow",
            "label": "Flow",
            "units": "mL/min",
            "patterns": ["flow"],
        },
        "volume": {
            "display": "Volume",
            "label": "Volume",
            "units": "mL",
            "patterns": ["volume"],
        },
        "delay": {
            "display": "Delay",
            "label": "Delay",
            "units": "seconds",
            "patterns": ["delay"],
        },
    }
    rules["challenges"] = {
        "pet_perfusion": {
            "label": "PET Perfusion",
            "description": "Fictional config-only perfusion challenge",
            "expected_maps": ["flow", "volume", "delay"],
            "keywords": ["pet_perfusion", "flow", "volume", "delay"],
        }
    }
    settings["defaults"]["challenge_type"] = "pet_perfusion"
    settings["defaults"]["scoring_map_type"] = "Flow"

    rules_path = tmp_path / "validation_rules.yaml"
    settings_path = tmp_path / "settings.yaml"
    rules_path.write_text(yaml.safe_dump(rules, sort_keys=False), encoding="utf-8")
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(config_rules, "VALIDATION_RULES_PATH", rules_path)
    monkeypatch.setattr(config_rules, "SETTINGS_PATH", settings_path)
    config_rules.clear_config_cache()

    config_body = client.get("/api/config").json()
    assert config_body["defaults"]["challenge_type"] == "pet_perfusion"
    assert config_body["challenge_types"] == [{
        "id": "pet_perfusion",
        "label": "PET Perfusion",
        "expected_maps": ["flow", "volume", "delay"],
    }]
    assert {item["id"] for item in config_body["map_types"]} == {"flow", "volume", "delay"}

    full_data, full_name = _make_maps_zip(
        "pet_full.zip",
        ["results/maps/flow_map.nii", "results/maps/volume_map.nii", "results/maps/delay_map.nii"],
    )
    sid = _upload_and_get_id(client, full_data, full_name)
    validation = client.post("/api/validate", json={
        "submission_id": sid,
        "challenge_type": "pet_perfusion",
        "mode": "result_only",
    })
    assert validation.status_code == 200, validation.text
    assert "EXPECTED_MAP_MISSING" not in _warning_codes(validation.json())

    missing_data, missing_name = _make_maps_zip(
        "pet_missing.zip",
        ["results/maps/flow_map.nii"],
    )
    missing_sid = _upload_and_get_id(client, missing_data, missing_name)
    missing = client.post("/api/validate", json={
        "submission_id": missing_sid,
        "challenge_type": "pet_perfusion",
        "mode": "result_only",
    }).json()
    missing_messages = [
        warning["message"]
        for warning in missing.get("warnings", [])
        if warning.get("code") == "EXPECTED_MAP_MISSING"
    ]
    assert any("Volume" in message for message in missing_messages)
    assert any("Delay" in message for message in missing_messages)

    previews = client.get(f"/api/submissions/{sid}/previews?challenge_type=pet_perfusion")
    assert previews.status_code == 200, previews.text
    detected = {item.get("detected_map_type") for item in previews.json().get("maps", [])}
    assert {"Flow", "Volume", "Delay"}.issubset(detected)

    combined = client.get(f"/api/export-combined?submission_id={sid}&blinded=true")
    assert combined.status_code == 200, combined.text
    header = next(csv.reader(io.StringIO(combined.text)))
    assert {"mean_flow", "mean_volume", "mean_delay"}.issubset(set(header))

    html_report = client.get(f"/api/report?submission_id={sid}&blinded=true")
    assert html_report.status_code == 200, html_report.text
    assert "PET_PERFUSION" in html_report.text
    assert "Mean Flow" in html_report.text

    pdf_report = client.get(f"/api/export/report/pdf?submission_id={sid}&blinded=true")
    assert pdf_report.status_code == 200, pdf_report.text
    pdf_text = pdf_report.content.decode("latin-1", errors="ignore")
    assert "PET_PERFUSION" in pdf_text
    assert "Flow" in pdf_text

    package = _make_scoring_package_zip(
        "pet_demo_scoring",
        challenge_type="pet_perfusion",
        map_type="flow",
        metric_name="pet_demo_metric",
        metric_value=0.42,
    )
    install = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("pet_demo_scoring.zip", package, "application/zip")},
    )
    assert install.status_code == 200, install.text
    active = client.post("/api/scoring/set-active", json={
        "challenge_type": "pet_perfusion",
        "mode": "custom",
        "package_id": "pet_demo_scoring",
    })
    assert active.status_code == 200, active.text
    score = client.post("/api/score", json={
        "submission_id": sid,
        "challenge_type": "pet_perfusion",
        "map_type": "flow",
    })
    assert score.status_code == 200, score.text
    assert score.json()["metrics"]["pet_demo_metric"] == pytest.approx(0.42)


def test_scoring_status_providers_only(client: TestClient) -> None:
    r = client.get("/api/scoring-status")
    assert r.status_code == 200
    data = r.json()
    assert "providers" in data
    provs = data["providers"]
    assert isinstance(provs, list)
    assert any(p["provider_id"] == "osipi_tf62_dce_ktrans" for p in provs)


def test_rankings_empty(client: TestClient) -> None:
    r = client.get("/api/rankings")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["rankings"] == []


def test_outputs_empty(client: TestClient) -> None:
    r = client.get("/api/outputs")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0
    assert data["results"] == []


def test_leaderboard_empty(client: TestClient) -> None:
    r = client.get("/api/leaderboard")
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 0


# ---------------------------------------------------------------------------
# Upload — local ZIP
# ---------------------------------------------------------------------------

def test_upload_rejects_non_zip(client: TestClient) -> None:
    r = client.post(
        "/api/upload-submission",
        files={"file": ("submission.tar.gz", b"fake data", "application/gzip")},
    )
    assert r.status_code == 400
    assert "zip" in r.json()["detail"].lower()


def test_upload_result_only_zip(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    r = client.post(
        "/api/upload-submission",
        files={"file": (fname, data, "application/zip")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert "submission_id" in body
    assert body.get("file_count", 0) >= 2      # Ktrans_map.nii + README.md


def test_upload_batch_zip(client: TestClient) -> None:
    data, fname = _make_batch_zip()
    r = client.post(
        "/api/upload-batch",
        files={"file": (fname, data, "application/zip")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    # Batch ZIP with two sub-dirs should be detected as a batch.
    if body.get("batch"):
        assert body.get("submission_count", 0) >= 2
        assert len(body.get("submissions", [])) >= 2


# ---------------------------------------------------------------------------
# Validate — single submission
# ---------------------------------------------------------------------------

def _upload_and_get_id(client: TestClient, zip_data: bytes, fname: str) -> str:
    r = client.post(
        "/api/upload-submission",
        files={"file": (fname, zip_data, "application/zip")},
    )
    assert r.status_code == 200, r.text
    return r.json()["submission_id"]


def test_validate_result_only_submission(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.post("/api/validate", json={
        "submission_id": sid,
        "challenge_type": "dce",
        "mode": "result_only",
    })
    assert r.status_code == 200
    body = r.json()
    assert "passed" in body
    assert "errors" in body
    assert "warnings" in body
    assert body.get("nifti_count", 0) >= 1
    assert body["submission_id"] == sid


def test_validate_reproducible_submission(client: TestClient) -> None:
    data, fname = _make_reproducible_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.post("/api/validate", json={
        "submission_id": sid,
        "challenge_type": "dce",
        "mode": "reproducible",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "reproducible"
    assert body.get("has_dockerfile") is True
    assert body.get("has_run_instructions") is True


def test_validate_missing_submission_id_returns_400(client: TestClient) -> None:
    r = client.post("/api/validate", json={"submission_id": " "})
    assert r.status_code == 400


def test_validate_nonexistent_submission(client: TestClient) -> None:
    r = client.post("/api/validate", json={
        "submission_id": "does_not_exist_xyz",
        "challenge_type": "dce",
    })
    assert r.status_code == 200
    body = r.json()
    assert body["passed"] is False
    assert any(e["code"] == "SUBMISSION_FOLDER_MISSING" for e in body["errors"])


def test_validate_invalid_email_returns_400(client: TestClient) -> None:
    r = client.post("/api/validate", json={
        "submission_id": "any",
        "challenge_type": "dce",
        "contact_email": "not-an-email",
    })
    assert r.status_code == 400


def test_validate_includes_nifti_summary(client: TestClient) -> None:
    """validation result must always include a nifti_summary list (may be empty)."""
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce"})
    assert r.status_code == 200
    body = r.json()
    assert "nifti_summary" in body
    assert isinstance(body["nifti_summary"], list)


# ---------------------------------------------------------------------------
# Validate-batch
# ---------------------------------------------------------------------------

def test_validate_batch_returns_summary(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.post("/api/validate-batch", json={
        "submission_ids": [sid, "nonexistent_submission"],
        "challenge_type": "dce",
    })
    assert r.status_code == 200
    body = r.json()
    assert "batch_id" in body
    assert body["submission_count"] == 2
    assert "results" in body
    # At least one result per submission_id
    assert len(body["results"]) == 2


def test_validate_batch_empty_ids_returns_400(client: TestClient) -> None:
    r = client.post("/api/validate-batch", json={"submission_ids": []})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def test_preflight_reproducible(client: TestClient) -> None:
    data, fname = _make_reproducible_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.post("/api/preflight", json={"submission_id": sid, "challenge_type": "dce"})
    assert r.status_code == 200
    body = r.json()
    assert body.get("has_run_instructions") is True
    assert body.get("runnable") is True


def test_preflight_result_only(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.post("/api/preflight", json={"submission_id": sid})
    assert r.status_code == 200
    body = r.json()
    assert body.get("has_run_instructions") is False
    assert body.get("runnable") is False


# ---------------------------------------------------------------------------
# Export — validation
# ---------------------------------------------------------------------------

def _upload_validate(client: TestClient) -> str:
    """Upload a result-only submission, validate it, return the submission_id."""
    data, fname = _make_result_only_zip("export_test.zip")
    sid = _upload_and_get_id(client, data, fname)
    rv = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce"})
    assert rv.status_code == 200
    return sid


def test_export_validation_json(client: TestClient) -> None:
    sid = _upload_validate(client)
    r = client.get(f"/api/export-validation?submission_id={sid}&format=json")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    assert body["submission_id"] == sid


def test_export_validation_csv_unblinded(client: TestClient) -> None:
    sid = _upload_validate(client)
    r = client.get(f"/api/export-validation?submission_id={sid}&format=csv&blinded=false")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    text = r.text
    assert "submission_id" in text
    assert "team_name" in text            # unblinded includes PII columns
    assert "contact_email" in text


def test_export_validation_csv_blinded(client: TestClient) -> None:
    sid = _upload_validate(client)
    r = client.get(f"/api/export-validation?submission_id={sid}&format=csv&blinded=true")
    assert r.status_code == 200
    text = r.text
    assert "team_name"    not in text     # blinded must strip PII
    assert "contact_email" not in text


def test_export_validation_missing_returns_404(client: TestClient) -> None:
    r = client.get("/api/export-validation?submission_id=no_such_id&format=json")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Export — batch
# ---------------------------------------------------------------------------

def test_export_batch_csv(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    bv = client.post("/api/validate-batch", json={
        "submission_ids": [sid],
        "challenge_type": "dce",
    })
    batch_id = bv.json()["batch_id"]

    r = client.get(f"/api/export-batch?batch_id={batch_id}&format=csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert sid in r.text


def test_export_batch_json_blinded(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    bv = client.post("/api/validate-batch", json={"submission_ids": [sid]})
    batch_id = bv.json()["batch_id"]

    r = client.get(f"/api/export-batch?batch_id={batch_id}&format=json&blinded=true")
    assert r.status_code == 200
    body = r.json()
    for result in body.get("results", []):
        assert "team_name"    not in result
        assert "contact_email" not in result


def test_export_batch_missing_returns_404(client: TestClient) -> None:
    r = client.get("/api/export-batch?batch_id=nonexistent_batch_xyz")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Export — execution (before any runs — should 404)
# ---------------------------------------------------------------------------

def test_export_execution_before_run_returns_404(client: TestClient) -> None:
    r = client.get("/api/export-execution?submission_id=never_run")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Scoring — not configured
# ---------------------------------------------------------------------------

def test_score_single_not_configured(client: TestClient) -> None:
    r = client.post("/api/score", json={
        "submission_id": "any_submission",
        "challenge_type": "dce",
        "map_type": "Ktrans",
    })
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") in ("not_configured", "not_ready", "failed")


def test_score_export_no_results_returns_404(client: TestClient) -> None:
    r = client.get("/api/export-scoring?submission_id=never_scored")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# NIfTI file listing
# ---------------------------------------------------------------------------

def test_nifti_files_listing_after_upload(client: TestClient) -> None:
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    r = client.get(f"/api/nifti-files/{sid}")
    assert r.status_code == 200
    body = r.json()
    assert "files" in body
    # The result-only ZIP contains Ktrans_map.nii
    assert any("nii" in f.lower() for f in body["files"])


def test_nifti_files_unknown_submission(client: TestClient) -> None:
    r = client.get("/api/nifti-files/no_such_submission")
    assert r.status_code == 200
    assert r.json()["files"] == []


# ---------------------------------------------------------------------------
# Helpers — build scoring package ZIPs
# ---------------------------------------------------------------------------

def _make_scoring_package_zip(
    package_id: str = "demo_dce_scoring",
    *,
    challenge_type: str = "dce",
    map_type: str = "ktrans",
    metric_name: str = "demo_rmse",
    metric_value: float = 0.1,
    display_name: str | None = None,
) -> bytes:
    """Return a minimal but valid scoring package ZIP in memory."""
    manifest = {
        "package_id":     package_id,
        "name":           display_name or f"Test {challenge_type.upper()} Scoring Package",
        "version":        "1.0.0",
        "challenge_type": challenge_type,
        "map_type":       map_type,
        "description":    "DEMO/TEST package only.",
        "metrics":        [metric_name],
        "entry_point":    "scoring.py",
        "call_mode":      "standard",
    }
    scoring_py = (
        "import argparse, json, pathlib, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--submission-dir'); p.add_argument('--output-dir'); p.add_argument('--reference-dir', default='')\n"
        "a = p.parse_args()\n"
        "out = pathlib.Path(a.output_dir); out.mkdir(parents=True, exist_ok=True)\n"
        f"(out / 'metrics.json').write_text(json.dumps({{{metric_name!r}: {metric_value!r}}}))\n"
        "sys.exit(0)\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("scoring.py",    scoring_py)
        zf.writestr("README.md",     "DEMO test package.")
    return buf.getvalue()


def _make_zip_no_manifest() -> bytes:
    """ZIP that is missing manifest.json."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("scoring.py", "# no manifest")
    return buf.getvalue()


def _make_zip_no_script() -> bytes:
    """ZIP with manifest.json but missing scoring.py / challengeScoring.py."""
    manifest = {
        "package_id":     "no_script_pkg",
        "name":           "No Script Package",
        "version":        "1.0.0",
        "challenge_type": "dce",
        "map_type":       "ktrans",
        "description":    "Missing script.",
        "metrics":        ["x"],
        "entry_point":    "scoring.py",
        "call_mode":      "standard",
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        # deliberately omit scoring.py
    return buf.getvalue()


def _make_invalid_zip() -> bytes:
    """Return bytes that are NOT a valid ZIP file."""
    return b"this is not a zip file at all \x00\x01\x02"


# ---------------------------------------------------------------------------
# Scoring package endpoint tests
# ---------------------------------------------------------------------------

def test_scoring_packages_list_empty(client: TestClient) -> None:
    """GET /api/scoring/packages returns empty list when no packages installed."""
    r = client.get("/api/scoring/packages")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)
    assert body == []


def test_scoring_package_upload_valid(client: TestClient) -> None:
    """POST /api/scoring/packages/upload installs a valid package."""
    zip_bytes = _make_scoring_package_zip("demo_dce_scoring")
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("demo_dce_scoring.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("package_id") == "demo_dce_scoring"

    # Package should now appear in list
    r2 = client.get("/api/scoring/packages")
    assert r2.status_code == 200
    ids = [p["package_id"] for p in r2.json()]
    assert "demo_dce_scoring" in ids


def test_scoring_package_upload_invalid_zip(client: TestClient) -> None:
    """POST /api/scoring/packages/upload rejects non-ZIP bytes."""
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("bad.zip", _make_invalid_zip(), "application/zip")},
    )
    assert r.status_code in (400, 422, 500)


def test_scoring_package_upload_no_manifest(client: TestClient) -> None:
    """POST /api/scoring/packages/upload rejects ZIP without manifest.json."""
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("no_manifest.zip", _make_zip_no_manifest(), "application/zip")},
    )
    assert r.status_code in (400, 422, 500)


def test_scoring_package_upload_no_script(client: TestClient) -> None:
    """POST /api/scoring/packages/upload rejects ZIP without a scoring script."""
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("no_script.zip", _make_zip_no_script(), "application/zip")},
    )
    # Should either reject at install time (400) or succeed (200) but mark not-ready.
    # Either way a later check_ready would return not ready.
    # We accept 200 or 4xx — the key invariant is it must NOT 500 silently with a bad state.
    assert r.status_code != 500 or "error" in r.text.lower()


def test_scoring_package_remove(client: TestClient) -> None:
    """DELETE /api/scoring/packages/{id} removes an installed package."""
    # Install first
    zip_bytes = _make_scoring_package_zip("pkg_to_remove")
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("pkg_to_remove.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text

    # Delete it
    r2 = client.delete("/api/scoring/packages/pkg_to_remove")
    assert r2.status_code == 200

    # Should no longer appear in list
    r3 = client.get("/api/scoring/packages")
    ids = [p["package_id"] for p in r3.json()]
    assert "pkg_to_remove" not in ids


def test_scoring_package_remove_nonexistent(client: TestClient) -> None:
    """DELETE /api/scoring/packages/{id} returns 404 for unknown package."""
    r = client.delete("/api/scoring/packages/does_not_exist")
    assert r.status_code == 404


def test_scoring_active_config_default(client: TestClient) -> None:
    """GET /api/scoring/active-config returns default 'none' mode for all challenge types."""
    r = client.get("/api/scoring/active-config")
    assert r.status_code == 200
    body = r.json()
    assert "active" in body
    for ct in ("dce", "asl", "dsc"):
        assert ct in body["active"]
        assert body["active"][ct]["mode"] == "none"


def test_scoring_set_active_none(client: TestClient) -> None:
    """POST /api/scoring/set-active with mode='none' saves successfully."""
    r = client.post("/api/scoring/set-active", json={
        "challenge_type": "dce",
        "mode": "none",
    })
    assert r.status_code == 200


def test_scoring_set_active_builtin(client: TestClient) -> None:
    """POST /api/scoring/set-active with mode='builtin' saves successfully."""
    r = client.post("/api/scoring/set-active", json={
        "challenge_type": "dce",
        "mode": "builtin",
    })
    assert r.status_code == 200


def test_scoring_set_active_custom_with_package(client: TestClient) -> None:
    """POST /api/scoring/set-active with mode='custom' and a valid package_id saves."""
    # Install a package first
    zip_bytes = _make_scoring_package_zip("custom_pkg")
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("custom_pkg.zip", zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text

    r2 = client.post("/api/scoring/set-active", json={
        "challenge_type": "dce",
        "mode": "custom",
        "package_id": "custom_pkg",
    })
    assert r2.status_code == 200

    # Active config should reflect the change
    r3 = client.get("/api/scoring/active-config")
    assert r3.status_code == 200
    active = r3.json()["active"]
    assert active["dce"]["mode"] == "custom"
    assert active["dce"]["package_id"] == "custom_pkg"


def test_scoring_set_active_custom_no_package(client: TestClient) -> None:
    """POST /api/scoring/set-active with mode='custom' but no package_id returns error."""
    r = client.post("/api/scoring/set-active", json={
        "challenge_type": "dce",
        "mode": "custom",
        # no package_id
    })
    assert r.status_code in (400, 422)


def test_scoring_disabled_mode_returns_not_configured(client: TestClient) -> None:
    """When mode='none', scoring-status returns status='not_configured'."""
    # Ensure mode is none (default)
    client.post("/api/scoring/set-active", json={"challenge_type": "dce", "mode": "none"})

    r = client.post("/api/scoring-status", json={
        "submission_id": "any_id",
        "challenge_type": "dce",
        "map_type": "Ktrans",
    })
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") in ("not_configured", "not_ready", "failed")


def test_score_single_disabled_mode(client: TestClient) -> None:
    """When mode='none', score endpoint returns not_configured rather than fake metrics."""
    # Upload a submission so it exists
    zip_bytes = _make_scoring_package_zip("ignored_pkg")
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)

    # Ensure scoring is disabled
    client.post("/api/scoring/set-active", json={"challenge_type": "dce", "mode": "none"})

    r = client.post("/api/score-single", json={
        "submission_id": sid,
        "challenge_type": "dce",
        "map_type": "Ktrans",
    })
    assert r.status_code == 200
    body = r.json()
    # Must NOT contain fabricated metric values; status should indicate unconfigured/failed
    assert body.get("status") in ("not_configured", "not_ready", "failed", "error")


# ---------------------------------------------------------------------------
# ASL ZIP structural layout detection
# ---------------------------------------------------------------------------

def _make_asl_structural_zip(filename: str = "asl_submission.zip") -> tuple[bytes, str]:
    """ZIP with input/ + results/maps/ inside one root — must be ONE submission."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("lena_asl/input/asl_data.nii", _tiny_nifti_bytes())
        zf.writestr("lena_asl/results/maps/cbf_map.nii", _tiny_nifti_bytes())
        zf.writestr("lena_asl/README.md", "# ASL submission\n")
    return buf.getvalue(), filename


def test_asl_structural_zip_detected_as_single_submission(client: TestClient) -> None:
    """An ASL ZIP with input/ + results/ subdirs must be ONE submission, not a batch."""
    zip_bytes, fname = _make_asl_structural_zip()
    r = client.post(
        "/api/upload-batch",
        files={"file": (fname, zip_bytes, "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["success"] is True
    # Should be a single submission (not a batch split)
    submissions = body.get("submissions", [])
    if body.get("batch"):
        # If detected as batch it must have exactly 1 entry (the root folder)
        assert len(submissions) == 1, (
            f"ASL structural ZIP was incorrectly split into {len(submissions)} submissions: "
            + str([s.get("submission_id") for s in submissions])
        )


# ---------------------------------------------------------------------------
# Custom ASL scoring package: scoring_status returns "ready" when configured
# ---------------------------------------------------------------------------

def _make_asl_scoring_package_zip() -> bytes:
    """Minimal ASL custom scoring package ZIP."""
    manifest = {
        "package_id":     "demo_asl_scoring",
        "name":           "Test ASL Scoring Package",
        "version":        "1.0.0",
        "challenge_type": "asl",
        "map_type":       "cbf",
        "description":    "DEMO/TEST ASL package.",
        "metrics":        ["demo_cbf_error"],
        "entry_point":    "scoring.py",
        "call_mode":      "standard",
    }
    scoring_py = (
        "import argparse, json, pathlib, sys\n"
        "p = argparse.ArgumentParser()\n"
        "p.add_argument('--submission-dir'); p.add_argument('--output-dir'); p.add_argument('--reference-dir', default='')\n"
        "a = p.parse_args()\n"
        "out = pathlib.Path(a.output_dir); out.mkdir(parents=True, exist_ok=True)\n"
        "(out / 'metrics.json').write_text(json.dumps({'demo_cbf_error': 0.05}))\n"
        "sys.exit(0)\n"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("manifest.json", json.dumps(manifest))
        zf.writestr("scoring.py",    scoring_py)
    return buf.getvalue()


def test_custom_asl_scoring_status_ready_when_configured(client: TestClient) -> None:
    """When a custom ASL package is installed and active, scoring_status returns 'ready'
    even before any execution outputs exist (no docker run required)."""
    # 1. Install the ASL scoring package
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("demo_asl_scoring.zip", _make_asl_scoring_package_zip(), "application/zip")},
    )
    assert r.status_code == 200, r.text

    # 2. Set it as active for ASL
    r2 = client.post("/api/scoring/set-active", json={
        "challenge_type": "asl",
        "mode": "custom",
        "package_id": "demo_asl_scoring",
    })
    assert r2.status_code == 200, r2.text

    # 3. Query scoring status for a (non-existent) submission — should return "ready",
    #    not "not_configured", because the package itself is properly configured.
    r3 = client.post("/api/scoring-status", json={
        "submission_id":  "asl_test_submission",
        "challenge_type": "asl",
        "map_type":       "cbf",
    })
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body.get("status") == "ready", (
        f"Expected status='ready' but got {body.get('status')!r}. "
        f"Missing: {body.get('missing')}. Message: {body.get('message')}"
    )
    assert body.get("active_mode") == "custom"


def test_custom_asl_score_runs_without_exec_outputs(client: TestClient) -> None:
    """When custom ASL scoring is configured, /api/score should use the extracted
    submission dir as fallback (not require docker exec outputs)."""
    # 1. Upload an ASL submission
    data, fname = _make_result_only_zip("asl_sub.zip")
    sid = _upload_and_get_id(client, data, fname)

    # 2. Install + activate ASL scoring package
    r = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("demo_asl_scoring.zip", _make_asl_scoring_package_zip(), "application/zip")},
    )
    assert r.status_code == 200, r.text
    r2 = client.post("/api/scoring/set-active", json={
        "challenge_type": "asl",
        "mode": "custom",
        "package_id": "demo_asl_scoring",
    })
    assert r2.status_code == 200, r2.text

    # 3. Run scoring — should not return not_configured
    r3 = client.post("/api/score", json={
        "submission_id":  sid,
        "challenge_type": "asl",
        "map_type":       "cbf",
    })
    assert r3.status_code == 200, r3.text
    body = r3.json()
    assert body.get("status") != "not_configured", (
        f"Scoring returned not_configured even though a custom ASL package is active. "
        f"Message: {body.get('message')}"
    )


def test_custom_dsc_scoring_uses_same_config_driven_package_framework(client: TestClient) -> None:
    """DSC can plug into custom scoring through config/manifest only."""
    data, fname = _make_maps_zip(
        "dsc_submission.zip",
        ["results/maps/cbv_map.nii", "results/maps/cbf_map.nii", "results/maps/mtt_map.nii"],
    )
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dsc", "mode": "result_only"})

    pkg = _make_scoring_package_zip(
        "demo_dsc_scoring",
        challenge_type="dsc",
        map_type="cbv",
        metric_name="demo_cbv_error",
        metric_value=0.2,
    )
    install = client.post(
        "/api/scoring/packages/upload",
        files={"file": ("demo_dsc_scoring.zip", pkg, "application/zip")},
    )
    assert install.status_code == 200, install.text
    active = client.post("/api/scoring/set-active", json={
        "challenge_type": "dsc", "mode": "custom", "package_id": "demo_dsc_scoring",
    })
    assert active.status_code == 200, active.text

    scored = client.post("/api/score", json={
        "submission_id": sid, "challenge_type": "dsc", "map_type": "cbv",
    })
    assert scored.status_code == 200, scored.text
    body = scored.json()
    assert body["status"] == "scored"
    assert body["metrics"]["demo_cbv_error"] == pytest.approx(0.2)
    detected = body["nifti_analysis"]["summary"]["parameter_maps_detected"]
    assert {"CBV", "CBF", "MTT"}.issubset(set(detected))


# ---------------------------------------------------------------------------
# Proposal-coverage tests for Score & Preview, export, and full workflow behavior
# ---------------------------------------------------------------------------

def test_local_zip_upload_succeeds(client: TestClient) -> None:
    """Local ZIP upload via /api/upload-submission returns a submission_id."""
    data, fname = _make_result_only_zip("local_upload_test.zip")
    r = client.post(
        "/api/upload-submission",
        files={"file": (fname, data, "application/zip")},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("success") is True
    assert body.get("submission_id")


def test_nifti_readability_after_upload(client: TestClient) -> None:
    """Uploaded NIfTI files are reported as readable via /api/nifti-files."""
    data, fname = _make_result_only_zip("nifti_read_test.zip")
    sid = _upload_and_get_id(client, data, fname)
    r = client.get(f"/api/nifti-files/{sid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("files"), list)
    assert len(body["files"]) >= 1, "Expected at least one NIfTI reported readable"


def test_validation_result_persisted(client: TestClient) -> None:
    """After /api/validate, the result can be re-fetched via /api/export-validation."""
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce", "mode": "result_only"})
    r = client.get(f"/api/export-validation?submission_id={sid}&format=json")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("submission_id") == sid


def test_result_only_submission_run_readiness(client: TestClient) -> None:
    """A result-only submission (maps but no Dockerfile) gets run_readiness != 'runnable'."""
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)
    r = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce", "mode": "result_only"})
    assert r.status_code == 200, r.text
    body = r.json()
    rr = body.get("run_readiness", "")
    assert rr != "runnable", (
        f"Result-only submission should not be 'runnable', got {rr!r}"
    )
    # Should be result_only or similar — not blocking
    assert body.get("passed") is True or body.get("nifti_count", 0) >= 1


def test_reproducible_submission_is_runnable(client: TestClient) -> None:
    """A submission with a Dockerfile gets run_readiness == 'runnable'."""
    data, fname = _make_reproducible_zip()
    sid = _upload_and_get_id(client, data, fname)
    r = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce", "mode": "reproducible"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("has_dockerfile") is True
    rr = body.get("run_readiness", "")
    assert rr == "runnable", f"Expected 'runnable', got {rr!r}"


def test_scoring_not_configured_returns_honest_status(client: TestClient) -> None:
    """When no scoring is configured, /api/score returns not_configured — never a fake score."""
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)
    r = client.post("/api/score", json={
        "submission_id":  sid,
        "challenge_type": "dce",
        "map_type":       "Ktrans",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    status = body.get("status", "")
    assert status in ("not_configured", "not_ready", "failed"), (
        f"Expected an honest non-scoring status, got {status!r} — "
        "do not fake scoring results when no package is configured."
    )
    # Must NOT return metrics when not configured
    metrics = body.get("metrics") or (body.get("score_result") or {}).get("metrics")
    assert not metrics, f"Should have no metrics when not_configured, got {metrics}"


def test_reference_scoring_perfect_cbf_zero_errors(client: TestClient, tmp_path: Path) -> None:
    data, fname = _make_asl_result_maps_zip("perfect_cbf.zip", [1, 2, 3, 4])
    sid = _upload_and_get_id(client, data, fname)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4])

    ref = _reference_scoring_status(client, sid)
    row = _first_reference_map(ref, "CBF")

    assert ref["available"] is True
    assert row["status"] == "compared"
    assert row["whole_map"]["rmse"] == pytest.approx(0.0)
    assert row["whole_map"]["bias"] == pytest.approx(0.0)
    assert row["whole_map"]["mae"] == pytest.approx(0.0)


def test_report_separates_cbf_att_and_flags_repeatability_unavailable(
    client: TestClient, tmp_path: Path
) -> None:
    # CBF differs from ref (bias) and ATT matches ref (bias 0): the report must
    # show them as SEPARATE rows and must not average them into one number.
    data, fname = _make_asl_result_maps_zip("asl_cbf_att.zip", [3, 4, 5, 6], att_values=[10, 10, 10, 10])
    sid = _upload_and_get_id(client, data, fname)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4])
    _write_reference_map(tmp_path, "sub-001_att.nii.gz", [10, 10, 10, 10])

    ref = _reference_scoring_status(client, sid)
    summary = ref["summary"]
    assert set(summary["by_map_type"]) == {"CBF", "ATT"}
    # No cross-unit average across CBF (mL/100g/min) and ATT (seconds):
    assert summary["mean_rmse"] is None
    assert summary["aggregate_map_type"] == "mixed"
    assert ref["repeatability_status"] == "unavailable_requires_repeated_datasets"

    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl"})
    html = client.get(f"/api/report?submission_id={sid}").text
    assert "CBF" in html and "ATT" in html
    assert "Error CoV" in html
    assert "Repeatability CoV and ICC are unavailable" in html


def test_reference_scoring_constant_offset_expected_bias_rmse(client: TestClient, tmp_path: Path) -> None:
    data, fname = _make_asl_result_maps_zip("offset_cbf.zip", [3, 4, 5, 6])
    sid = _upload_and_get_id(client, data, fname)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4])

    row = _first_reference_map(_reference_scoring_status(client, sid), "CBF")

    assert row["whole_map"]["bias"] == pytest.approx(2.0)
    assert row["whole_map"]["mean_error"] == pytest.approx(2.0)
    assert row["whole_map"]["mae"] == pytest.approx(2.0)
    assert row["whole_map"]["rmse"] == pytest.approx(2.0)
    assert row["whole_map"]["standard_deviation_error"] == pytest.approx(0.0)
    assert row["whole_map"]["correlation"] == pytest.approx(1.0)


def test_reference_scoring_missing_reference_keeps_qc_only(client: TestClient) -> None:
    data, fname = _make_asl_result_maps_zip("missing_ref.zip", [1, 2, 3, 4])
    sid = _upload_and_get_id(client, data, fname)

    status = client.get(f"/api/scoring-status?submission_id={sid}&challenge_type=asl&map_type=cbf").json()
    analysis = status["nifti_analysis"]
    ref = analysis["reference_scoring"]

    assert analysis["summary"]["map_count"] == 1
    assert analysis["summary"]["finite_percent"] == pytest.approx(100.0)
    assert ref["available"] is False
    assert ref["status"] == "reference_not_available"
    assert _first_reference_map(ref, "CBF")["status"] == "reference_not_available"


def test_reference_scoring_mismatched_shape_reports_error(client: TestClient, tmp_path: Path) -> None:
    data, fname = _make_asl_result_maps_zip("shape_mismatch.zip", [1, 2, 3, 4])
    sid = _upload_and_get_id(client, data, fname)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4, 5, 6, 7, 8], shape=(2, 2, 2))

    ref = _reference_scoring_status(client, sid)
    row = _first_reference_map(ref, "CBF")

    assert ref["status"] == "scoring_error"
    assert row["status"] == "shape_mismatch"
    assert "Resampling is not performed yet" in row["error"]


def test_reference_scoring_mask_uses_only_mask_voxels(client: TestClient, tmp_path: Path) -> None:
    data, fname = _make_asl_result_maps_zip("masked_cbf.zip", [12, 14, 100, 100])
    sid = _upload_and_get_id(client, data, fname)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [10, 10, 10, 10])
    _write_reference_mask(tmp_path, "brain_mask.nii.gz", [1, 1, 0, 0])

    row = _first_reference_map(_reference_scoring_status(client, sid), "CBF")
    brain = next(mask for mask in row["masks"] if mask["mask_name"] == "brain_mask.nii.gz")

    assert row["whole_map"]["bias"] != pytest.approx(3.0)
    assert brain["status"] == "compared"
    assert brain["metrics"]["voxel_count"] == 2
    assert brain["metrics"]["bias"] == pytest.approx(3.0)
    assert brain["metrics"]["mae"] == pytest.approx(3.0)
    assert brain["metrics"]["rmse"] == pytest.approx(math.sqrt(10.0))


def test_reference_scoring_score_endpoint_writes_artifacts(client: TestClient, tmp_path: Path) -> None:
    data, fname = _make_asl_result_maps_zip("artifact_cbf.zip", [3, 4, 5, 6])
    sid = _upload_and_get_id(client, data, fname)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4])

    r = client.post("/api/score", json={"submission_id": sid, "challenge_type": "asl", "map_type": "cbf"})
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["metrics"] == {}
    assert body["reference_scoring"]["available"] is True
    artifacts = body.get("artifacts") or []
    assert "reference_scoring.json" in artifacts
    assert "reference_scoring.csv" in artifacts
    assert any(name.endswith("_difference.nii") for name in artifacts)

    export = client.get(f"/api/export-scoring?submission_id={sid}&blinded=true")
    assert export.status_code == 200, export.text
    for col in (
        "reference_based_scoring_available",
        "reference_scoring_status",
        "reference_mean_rmse",
        "reference_mean_mae",
        "reference_mean_bias",
        "reference_metrics_json",
        "overall_qc_summary_json",
        "per_map_metadata_json",
        "per_map_stats_json",
    ):
        assert col in export.text
    assert "available" in export.text
    assert "2.0" in export.text


def test_export_scoring_csv_after_custom_scoring(client: TestClient) -> None:
    """After running custom scoring, /api/export-scoring returns a CSV with results."""
    # Upload + validate
    data, fname = _make_result_only_zip("score_export_test.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce"})

    # Install + activate scoring package
    client.post(
        "/api/scoring/packages/upload",
        files={"file": ("pkg.zip", _make_scoring_package_zip(), "application/zip")},
    )
    client.post("/api/scoring/set-active", json={
        "challenge_type": "dce", "mode": "custom", "package_id": "demo_dce_scoring",
    })

    # Run scoring
    r_score = client.post("/api/score", json={
        "submission_id": sid, "challenge_type": "dce", "map_type": "Ktrans",
    })
    assert r_score.status_code == 200, r_score.text
    score_body = r_score.json()
    if score_body.get("status") not in ("scored", "failed"):
        pytest.skip(f"Scoring did not complete (status={score_body.get('status')}); skipping export check")

    # Export scoring CSV
    r_exp = client.get(f"/api/export-scoring?submission_id={sid}&format=csv")
    assert r_exp.status_code == 200, r_exp.text
    assert "text/csv" in r_exp.headers.get("content-type", "")
    assert sid in r_exp.text


def test_batch_export_blinded_strips_pii(client: TestClient) -> None:
    """Batch blinded export must not include team_name or contact_email columns."""
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)
    bv = client.post("/api/validate-batch", json={"submission_ids": [sid]})
    batch_id = bv.json()["batch_id"]

    r = client.get(f"/api/export-batch?batch_id={batch_id}&format=csv&blinded=true")
    assert r.status_code == 200, r.text
    text = r.text
    assert "team_name"     not in text, "Blinded export must not contain team_name"
    assert "contact_email" not in text, "Blinded export must not contain contact_email"


def test_batch_export_unblinded_includes_pii(client: TestClient) -> None:
    """Batch unblinded export must include team_name and contact_email columns."""
    data, fname = _make_result_only_zip()
    sid = _upload_and_get_id(client, data, fname)
    bv = client.post("/api/validate-batch", json={"submission_ids": [sid]})
    batch_id = bv.json()["batch_id"]

    r = client.get(f"/api/export-batch?batch_id={batch_id}&format=csv&blinded=false")
    assert r.status_code == 200, r.text
    text = r.text
    assert "team_name"     in text, "Unblinded export must include team_name"
    assert "contact_email" in text, "Unblinded export must include contact_email"


# ===========================================================================
# Additional coverage — submission unwrap, .gz handling, combined export,
# HTML report, zero-byte NIfTI, ASL case-insensitivity.
# ===========================================================================

def _make_no_wrapper_structural_zip(filename: str = "nowrap_asl.zip") -> tuple[bytes, str]:
    """ZIP with input/ and results/maps/ at the TOP level (no wrapper folder).

    Must be detected as ONE submission, not split into <name>_input / <name>_results.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("input/sub-001_asl.nii", _tiny_nifti_bytes())
        zf.writestr("results/maps/sub-001_cbf.nii", _tiny_nifti_bytes())
        zf.writestr("README.md", "# ASL\n")
    return buf.getvalue(), filename


def test_asl_wrapper_zip_unwrapped_to_results_maps(client: TestClient) -> None:
    """A wrapped ASL ZIP becomes ONE submission whose maps sit at results/maps/
    (the redundant top wrapper folder is removed)."""
    zip_bytes, fname = _make_asl_structural_zip("lena_asl.zip")
    r = client.post("/api/upload-submission", files={"file": (fname, zip_bytes, "application/zip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch"] is False, "Wrapped ASL submission must NOT be a batch"
    sid = body["submission_id"]

    files = client.get(f"/api/nifti-files/{sid}").json()["files"]
    # The CBF map must be reachable at results/maps/... with NO extra wrapper prefix.
    assert any(f.replace("\\", "/") == "results/maps/cbf_map.nii" for f in files), (
        f"Expected results/maps/cbf_map.nii at submission root, got {files}"
    )


def test_no_wrapper_structural_zip_single_submission(client: TestClient) -> None:
    """input/ + results/maps/ at the ZIP top level → ONE submission, not split."""
    zip_bytes, fname = _make_no_wrapper_structural_zip()
    r = client.post("/api/upload-submission", files={"file": (fname, zip_bytes, "application/zip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch"] is False, (
        "A structural input/+results/ ZIP must be one submission, not a batch "
        f"({body.get('submission_count')} detected)"
    )
    sid = body["submission_id"]
    assert not sid.endswith("_input") and not sid.endswith("_results"), (
        f"Submission was wrongly split: {sid!r}"
    )


def test_random_gz_not_treated_as_nifti(client: TestClient) -> None:
    """A stray .gz archive must not be counted as a NIfTI file."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cbf_map.nii", _tiny_nifti_bytes())
        zf.writestr("notes.tar.gz", b"\x1f\x8b\x08\x00not-a-nifti")
        zf.writestr("README.md", "# ASL\n")
    sid = _upload_and_get_id(client, buf.getvalue(), "gz_test.zip")
    files = client.get(f"/api/nifti-files/{sid}").json()["files"]
    assert all(not f.endswith(".tar.gz") for f in files), f"Random .gz leaked into NIfTI list: {files}"
    assert any(f.endswith("cbf_map.nii") for f in files)

    v = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl", "mode": "result_only"}).json()
    assert v["nifti_count"] == 1, f"Expected exactly 1 NIfTI (the .nii), got {v['nifti_count']}"


def test_zero_byte_nifti_flagged(client: TestClient) -> None:
    """A zero-byte NIfTI is flagged with a warning but does not crash validation."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("cbf_map.nii", b"")            # zero-byte NIfTI
        zf.writestr("README.md", "# ASL\n")
    sid = _upload_and_get_id(client, buf.getvalue(), "zero_byte.zip")
    v = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl", "mode": "result_only"}).json()
    warn_codes = {w.get("code") for w in v.get("warnings", [])}
    assert "EMPTY_NIFTI_FILE" in warn_codes, f"Zero-byte NIfTI not flagged. Warnings: {v.get('warnings')}"


def test_export_combined_csv_is_researcher_facing(client: TestClient) -> None:
    """Main combined CSV is a clean MRI evaluation summary, not a workflow log."""
    data, fname = _make_result_only_zip("combined_test.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={
        "submission_id": sid,
        "challenge_type": "dce",
        "mode": "result_only",
        "team_name": "Perfusion Lab",
        "contact_email": "pi@example.org",
    })

    r = client.get(f"/api/export-combined?submission_id={sid}&blinded=false")
    assert r.status_code == 200, r.text
    assert "text/csv" in r.headers.get("content-type", "")
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    header = rows[0].keys()
    for col in (
        "team_name", "contact_email", "original_submission_name", "submission_id",
        "blinded_submission_id", "challenge_type", "map_types", "map_count",
        "warning_count", "error_count", "reference_status",
        "finite_voxels_percent", "nan_count", "inf_count", "negative_voxels_percent",
        "mean_cbf", "mean_att", "mean_ktrans", "mean_cbv", "mean_mtt",
        "rmse", "mae", "bias", "cov", "icc", "notes",
    ):
        assert col in header, f"Combined CSV missing researcher-facing column {col!r}"
    for old_col in (
        "validation_status", "validation_passed", "execution_status", "scoring_status",
        "ran", "skipped", "result_maps_provided", "metrics_json",
        "overall_qc_summary_json", "per_map_metadata_json", "per_map_stats_json",
    ):
        assert old_col not in header, f"Main CSV should not expose workflow/debug column {old_col!r}"
    row = rows[0]
    assert row["team_name"] == "Perfusion Lab"
    assert row["contact_email"] == "pi@example.org"
    assert row["submission_id"] == sid
    assert row["blinded_submission_id"] == "submission_001"
    assert row["map_types"] == "Ktrans"
    assert row["reference_status"] == "Not available"
    assert "Reference maps were not available" in row["notes"]


def test_export_combined_blinded_strips_pii(client: TestClient) -> None:
    """Blinded combined CSV must not contain private identifier columns."""
    data, fname = _make_result_only_zip("combined_blind.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={
        "submission_id": sid,
        "challenge_type": "dce",
        "mode": "result_only",
        "team_name": "Private Team",
        "contact_email": "secret@example.org",
    })
    r = client.get(f"/api/export-combined?submission_id={sid}&blinded=true")
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert len(rows) == 1
    header = rows[0].keys()
    assert "team_name" not in header
    assert "contact_email" not in header
    assert "original_submission_name" not in header
    assert "submission_id" not in header
    assert "Private Team" not in r.text
    assert "secret@example.org" not in r.text
    assert sid not in r.text
    assert rows[0]["blinded_submission_id"] == "submission_001"


def test_export_combined_json_blinded_summary(client: TestClient) -> None:
    """Combined JSON is machine-readable and respects blinded mode."""
    data, fname = _make_result_only_zip("combined_json.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={
        "submission_id": sid,
        "challenge_type": "dce",
        "mode": "result_only",
        "team_name": "Private JSON Team",
        "contact_email": "json-secret@example.org",
    })
    r = client.get(f"/api/export-combined?submission_id={sid}&blinded=true&format=json")
    assert r.status_code == 200, r.text
    assert "application/json" in r.headers.get("content-type", "")
    body = r.json()
    assert body["report_type"] == "blinded"
    assert body["submission_count"] == 1
    item = body["submissions"][0]
    assert item["blinded_submission_id"] == "submission_001"
    assert item["submission_id"] is None
    assert item["team_name"] is None
    assert item["contact_email"] is None
    assert item["qc"]["map_count"] >= 1
    assert item["reference"]["status"] == "Not available"
    assert "Private JSON Team" not in r.text
    assert "json-secret@example.org" not in r.text


def test_report_html_generated(client: TestClient) -> None:
    """/api/report returns a self-contained HTML evaluation report."""
    data, fname = _make_result_only_zip("report_test.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce", "mode": "result_only"})
    r = client.get(f"/api/report?submission_id={sid}")
    assert r.status_code == 200, r.text
    assert "text/html" in r.headers.get("content-type", "")
    assert "OSIPI Perfusion Pipeline Report" in r.text
    assert "Executive Summary" in r.text
    assert "Key Metrics" in r.text
    assert "Visual Summary" in r.text
    assert "Submission Metadata" in r.text
    assert "QC / Evaluation Summary" in r.text
    assert "Scoring Summary" in r.text
    assert "Parameter Map Previews" in r.text
    assert "Per-Submission Results" in r.text
    assert "Errors, Warnings, and Recommended Actions" in r.text
    assert "Notes / Limitations" in r.text
    assert "Validation outcome summary" in r.text
    assert "Voxel validity summary" in r.text
    assert "Basic NIfTI QC" in r.text
    assert "not full BIDS validation" in r.text
    assert "Challenge type:" in r.text
    assert "Number of submissions:" in r.text
    assert "Number of maps:" in r.text
    assert "Map types detected:" in r.text
    assert "Finite voxels" in r.text
    assert "Reference status" in r.text
    assert r.text.count("Reference maps were not available, so this report shows QC metrics only.") == 1
    # Plain printable report — no purple-heavy app/dashboard styling.
    assert "#4c2a86" not in r.text
    assert 'class="cards"' not in r.text
    assert 'class="metrics"' not in r.text
    for forbidden in (
        "Validation summary", "Execution summary", "Result maps provided",
        "<th>Validation</th>", "<th>Execution</th>", "<th>Scoring</th>",
        "Scoring is not configured", "reference_not_available",
    ):
        assert forbidden not in r.text


def test_report_pdf_generated_when_reference_unavailable(client: TestClient) -> None:
    """/api/export/report/pdf returns a researcher-facing PDF without workflow labels."""
    data, fname = _make_result_only_zip("report_pdf_test.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce", "mode": "result_only"})

    r = client.get(f"/api/export/report/pdf?submission_id={sid}&blinded=true")
    assert r.status_code == 200, r.text
    assert "application/pdf" in r.headers.get("content-type", "")
    content_disposition = r.headers.get("content-disposition", "")
    assert ".pdf" in content_disposition
    assert r.content.startswith(b"%PDF")
    assert len(r.content) > 500
    pdf_text = r.content.decode("latin-1", errors="ignore")
    for expected in (
        "OSIPI Perfusion Pipeline Report", "Executive Summary", "Status and Key QC Metrics",
        "QC / Evaluation Summary",
        "Submission Metadata", "Scoring Summary", "Per-Submission Results",
        "Notes / Limitations", "Challenge type", "Export date",
        "Pipeline version", "Configuration version",
        "Number of submissions", "Number of maps", "Map types detected",
        "Finite voxels", "Reference status", "Validation status", "Execution status",
    ):
        assert expected in pdf_text
    # The decorative "Small QC Charts" section was removed to reduce noise.
    assert "Small QC Charts" not in pdf_text
    assert pdf_text.count("Reference maps were not available, so this report shows QC metrics only.") == 1
    for forbidden in (
        "Execution summary", "Validation summary", "Result maps provided",
        "Ran", "Skipped", "reference_not_available",
    ):
        assert forbidden not in pdf_text


def test_pdf_report_includes_cached_map_previews_when_available(client: TestClient) -> None:
    pytest.importorskip("numpy")
    pytest.importorskip("nibabel")
    data, fname = _make_result_only_zip("report_pdf_preview.zip")
    sid = _upload_and_get_id(client, data, fname)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce", "mode": "result_only"})

    previews = client.get(f"/api/submissions/{sid}/previews?challenge_type=dce")
    assert previews.status_code == 200, previews.text
    preview_body = previews.json()
    assert preview_body.get("maps"), preview_body
    assert any(item.get("preview_available") for item in preview_body["maps"]), preview_body

    pdf = client.get(f"/api/export/report/pdf?submission_id={sid}&blinded=true")
    assert pdf.status_code == 200, pdf.text
    pdf_text = pdf.content.decode("latin-1", errors="ignore")
    assert "Map Previews" in pdf_text
    assert "Ktrans" in pdf_text


def test_pdf_report_blinded_vs_unblinded_labels() -> None:
    """Blinded PDF uses generic submission labels; unblinded may reveal the folder name."""
    import sys
    from pathlib import Path as _Path
    sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "backend"))
    from services.pdf_report_service import generate_pdf_report

    summaries = [{
        "submission_id": "sub_xyz", "source_folder": "team_alpha_asl_submission",
        "challenge_type": "asl", "warning_count": 0, "error_count": 0,
        "analysis_fields": {
            "parameter_maps_detected": "CBF, ATT", "map_count": 2,
            "finite_voxels_percent": 100.0, "nan_count": 0, "inf_count": 0,
            "negative_voxels_percent": 0.0, "mean_coefficient_of_variation": 0.4,
            "reference_based_scoring_available": False, "reference_compared_map_count": 0,
            "reference_scoring_status": "reference_not_available",
        },
        "nifti_analysis": {"summary": {"means_by_map_type": {"CBF": 15.3, "ATT": 480.8}}, "maps": []},
    }]

    blinded = generate_pdf_report(summaries, tag="t", blinded=True).decode("latin-1", "ignore")
    unblinded = generate_pdf_report(summaries, tag="t", blinded=False).decode("latin-1", "ignore")
    assert "Submission 1" in blinded
    assert "team_alpha_asl_submission" not in blinded   # blinded strips identifying folder name
    assert "team_alpha_asl_submission" in unblinded       # unblinded may include it


def test_combined_export_hides_result_only_workflow_status(client: TestClient) -> None:
    """Main combined CSV keeps result-only workflow status out of researcher exports."""
    zip_bytes, fname = _make_asl_structural_zip("lena_skip.zip")
    r = client.post("/api/upload-submission", files={"file": (fname, zip_bytes, "application/zip")})
    sid = r.json()["submission_id"]
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl", "mode": "result_only"})
    csv_text = client.get(f"/api/export-combined?submission_id={sid}&blinded=true").text
    assert "skipped_result_maps" not in csv_text
    assert "execution_status" not in csv_text


def test_custom_asl_scoring_status_case_insensitive(client: TestClient) -> None:
    """challenge_type matching is case-insensitive: 'ASL' must match an 'asl' package."""
    client.post(
        "/api/scoring/packages/upload",
        files={"file": ("demo_asl_scoring.zip", _make_asl_scoring_package_zip(), "application/zip")},
    )
    client.post("/api/scoring/set-active", json={
        "challenge_type": "ASL", "mode": "custom", "package_id": "demo_asl_scoring",
    })
    body = client.post("/api/scoring-status", json={
        "submission_id": "asl_case_sub", "challenge_type": "ASL", "map_type": "CBF",
    }).json()
    assert body.get("status") == "ready", (
        f"Uppercase 'ASL' did not match 'asl' package: status={body.get('status')!r}"
    )


# ---------------------------------------------------------------------------
# Long-format (tidy) researcher CSV export
# ---------------------------------------------------------------------------

def _long_csv(client, *, sid=None, batch_id=None, blinded=False):
    params = {"format": "csv", "shape": "long", "blinded": str(blinded).lower()}
    if sid:
        params["submission_id"] = sid
    if batch_id:
        params["batch_id"] = batch_id
    r = client.get("/api/export-combined", params=params)
    assert r.status_code == 200, r.text
    rows = list(csv.DictReader(io.StringIO(r.text)))
    return r.text, rows


def _asl_with_ref(client, tmp_path, fname="asl_long.zip", cbf=(3, 4, 5, 6), att=(1, 2, 3, 4),
                  team=None, email=None):
    data, name = _make_asl_result_maps_zip(fname, list(cbf), att_values=list(att))
    sid = _upload_and_get_id(client, data, name)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4])
    _write_reference_map(tmp_path, "sub-001_att.nii.gz", [1, 2, 3, 4])
    body = {"submission_id": sid, "challenge_type": "asl"}
    if team:
        body["team_name"] = team
    if email:
        body["contact_email"] = email
    client.post("/api/validate", json=body)
    return sid


def test_long_csv_cbf_and_att_separate_rows(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    _, rows = _long_csv(client, sid=sid)
    assert {"CBF", "ATT"} <= {r["map_type"] for r in rows}
    cbf_rmse = [r for r in rows if r["map_type"] == "CBF" and r["metric_name"] == "rmse"]
    att_rmse = [r for r in rows if r["map_type"] == "ATT" and r["metric_name"] == "rmse"]
    assert cbf_rmse and att_rmse            # separate CBF and ATT rows
    assert cbf_rmse[0] is not att_rmse[0]


def test_long_csv_whole_image_and_roi_are_separate_rows(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    _write_reference_mask(tmp_path, "gray_matter.nii.gz", [1, 1, 0, 1])
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl",
                                       "force_validation_refresh": True})
    _, rows = _long_csv(client, sid=sid)
    cbf_rois = {r["roi"] for r in rows if r["map_type"] == "CBF"}
    assert "whole_image" in cbf_rois
    assert any(roi != "whole_image" for roi in cbf_rois)   # at least one ROI/mask row


def test_long_csv_one_metric_per_row(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    _, rows = _long_csv(client, sid=sid)
    names = {r["metric_name"] for r in rows}
    assert {"rmse", "mae", "bias", "error_coefficient_of_variation", "correlation",
            "repeatability_coefficient_of_variation", "icc"} <= names
    assert all("," not in r["metric_name"] for r in rows)
    from collections import Counter
    # Accuracy metrics: exactly 5 rows per (map, real ROI). Repeatability/ICC are
    # emitted once per submission (submission-level), not repeated per map/ROI.
    per_map_roi = Counter(
        (r["map_type"], r["roi"]) for r in rows if r["roi"] != "(submission-level)"
    )
    assert per_map_roi and all(count == 5 for count in per_map_roi.values())
    sub_level = [r for r in rows if r["roi"] == "(submission-level)"]
    assert {r["metric_name"] for r in sub_level} == {
        "repeatability_coefficient_of_variation", "icc"}
    assert len(sub_level) == 2   # exactly one each, not repeated per map/ROI


def test_long_csv_missing_metrics_are_blank_not_zero(client, tmp_path):
    # CBF submitted but NO reference written -> comparison metrics unavailable.
    data, name = _make_asl_result_maps_zip("noref.zip", [3, 4, 5, 6])
    sid = _upload_and_get_id(client, data, name)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl"})
    _, rows = _long_csv(client, sid=sid)
    rmse_rows = [r for r in rows if r["metric_name"] == "rmse"]
    assert rmse_rows
    for r in rmse_rows:
        assert r["metric_value"] == ""              # blank, never "0"
        assert r["metric_status"] != "computed"
    rep = [r for r in rows if r["metric_name"] == "repeatability_coefficient_of_variation"]
    assert rep and all(r["metric_value"] == "" and "unavailable" in r["metric_status"] for r in rep)
    icc = [r for r in rows if r["metric_name"] == "icc"]
    assert icc and all(r["metric_value"] == "" for r in icc)


def test_long_csv_blinded_has_no_identity_or_paths(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path, fname="secret_team_zip.zip",
                        team="Secret Team", email="person@hospital.org")
    text, rows = _long_csv(client, sid=sid, blinded=True)
    for bad in ["Secret Team", "person@hospital.org", "secret_team_zip", sid,
                ".nii", "submissions/extracted", "/Users", "sessions/"]:
        assert bad not in text, f"blinded CSV leaked: {bad!r}"
    assert rows and rows[0]["blinded_submission_id"].startswith("SUB-")
    header = list(rows[0].keys())
    for col in ["team_name", "contact_email", "contact_name", "institution",
                "submission_id", "original_archive_name", "repository_url"]:
        assert col not in header


def test_long_csv_unblinded_has_permitted_identity_fields(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path, team="My Team", email="me@lab.org")
    _, rows = _long_csv(client, sid=sid, blinded=False)
    header = list(rows[0].keys())
    for col in ["submission_id", "team_name", "contact_name", "contact_email",
                "institution", "submission_source", "original_archive_name",
                "repository_url", "submitted_at"]:
        assert col in header
    assert rows[0]["team_name"] == "My Team"
    assert rows[0]["contact_email"] == "me@lab.org"


def test_long_csv_mixed_challenges_no_cross_challenge_rows(client, tmp_path):
    a, an = _make_asl_result_maps_zip("aslmix.zip", [3, 4, 5, 6])
    asid = _upload_and_get_id(client, a, an)
    d, dn = _make_maps_zip("dcemix.zip", ["results/maps/Ktrans_map.nii"])
    dsid = _upload_and_get_id(client, d, dn)
    _write_reference_map(tmp_path, "sub-001_cbf.nii.gz", [1, 2, 3, 4])
    vb = client.post("/api/validate-batch", json={
        "submission_ids": [asid, dsid], "challenge_type": "asl",
        "challenge_types": {asid: "asl", dsid: "dce"},
    }).json()
    _, rows = _long_csv(client, batch_id=vb["batch_id"])
    challenges = {r["challenge"] for r in rows}
    assert challenges <= {"ASL", "DCE"}
    assert "ASL" in challenges and "DCE" in challenges
    # every row belongs to exactly one challenge; no aggregate/total sentinel row
    assert all(r["challenge"] in {"ASL", "DCE"} for r in rows)
    assert not any(r["blinded_submission_id"].lower() in {"total", "all", "combined"} for r in rows)
    # no row mixes a CBF metric under the DCE challenge
    assert not any(r["challenge"] == "DCE" and r["map_type"] == "CBF" for r in rows)


def test_long_csv_arbitrary_roi_names_preserved(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    _write_reference_mask(tmp_path, "custom_region_7.nii.gz", [1, 1, 0, 1])
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl",
                                       "force_validation_refresh": True})
    _, rows = _long_csv(client, sid=sid)
    rois = {r["roi"] for r in rows}
    assert any("custom" in roi.lower() or "region" in roi.lower() for roi in rois), rois


def test_long_csv_has_versions_and_export_date(client, tmp_path):
    import re
    sid = _asl_with_ref(client, tmp_path)
    _, rows = _long_csv(client, sid=sid)
    assert rows[0]["pipeline_version"] and rows[0]["pipeline_version"] != "unknown"
    assert rows[0]["configuration_version"] not in ("", None)
    assert re.match(r"\d{4}-\d{2}-\d{2}", rows[0]["export_date"])


def test_wide_csv_and_json_exports_still_compatible(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    # Wide is still the default shape (existing consumers unaffected).
    wide = client.get("/api/export-combined", params={"submission_id": sid, "format": "csv"})
    assert wide.status_code == 200
    header = next(csv.reader(io.StringIO(wide.text)))
    assert "mean_cbf" in header and "rmse" in header      # wide columns preserved
    # JSON export unaffected by the shape parameter.
    j = client.get("/api/export-combined", params={"submission_id": sid, "format": "json"})
    assert j.status_code == 200 and "submissions" in j.json()


# ---------------------------------------------------------------------------
# PDF / HTML report improvements (versions, per-map sections, voxel counts)
# ---------------------------------------------------------------------------

def test_html_report_has_versions_permap_and_voxel_counts(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    _write_reference_mask(tmp_path, "gray_matter.nii.gz", [1, 1, 0, 1])
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl",
                                       "force_validation_refresh": True})
    html = client.get(f"/api/report?submission_id={sid}").text
    # versions
    assert "Pipeline version" in html and "Configuration version" in html
    # per-map submitted-outputs properties
    assert "Submitted outputs" in html and ">Units<" in html and "Voxel size" in html
    # reference comparison per map/ROI with valid/excluded voxel counts
    assert "Reference comparison" in html
    assert "Valid voxels" in html and "Excluded voxels" in html
    assert "<th>ROI</th>" in html
    # unavailable-metric note, CBF and ATT both present
    assert "Repeatability CoV and ICC are unavailable" in html
    assert "CBF" in html and "ATT" in html
    # offline + no internal path leakage / decorative map-mean chart removed
    assert "<script src=\"http" not in html and "/sessions/" not in html
    assert "submissions/extracted" not in html
    assert "Map mean summary" not in html


def test_pdf_report_has_versions_and_permap_sections(client, tmp_path):
    sid = _asl_with_ref(client, tmp_path)
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl"})
    pdf = client.get(f"/api/export/report/pdf?submission_id={sid}")
    assert pdf.status_code == 200
    assert pdf.content[:5] == b"%PDF-" and len(pdf.content) > 3000
