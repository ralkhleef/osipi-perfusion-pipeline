"""Multi-ZIP upload + per-submission challenge tagging/validation.

Verifies that several ZIPs can be uploaded at once, that each submission keeps
its own detected challenge, and that a mixed batch validates each submission
under its own challenge without merging challenges.
"""
from __future__ import annotations

import io
import struct
import sys
import zipfile
from pathlib import Path
from typing import Generator

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402


def _tiny_nifti() -> bytes:
    header = bytearray(348)
    header[0:4] = (348).to_bytes(4, "little")
    header[344:348] = b"n+1\x00"
    header[40:42] = (3).to_bytes(2, "little")
    for off in (42, 44, 46):
        header[off:off + 2] = (4).to_bytes(2, "little")
    header[70:72] = (16).to_bytes(2, "little")
    header[72:74] = (32).to_bytes(2, "little")
    header[108:112] = struct.pack("<f", 352.0)
    for i in range(1, 4):
        header[76 + i * 4 : 76 + i * 4 + 4] = struct.pack("<f", 1.0)
    return bytes(header) + b"\x00\x00\x00\x00" + b"\x00" * (4 * 4 * 4 * 4)


def _asl_zip() -> tuple[str, bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("asl_submission/results/maps/sub-001_Perfmap.nii.gz", _tiny_nifti())
        zf.writestr("asl_submission/results/maps/sub-001_ATTmap.nii.gz", _tiny_nifti())
        zf.writestr("asl_submission/README.md", "# ASL submission\n")
    return "asl_team.zip", buf.getvalue()


def _dce_zip() -> tuple[str, bytes]:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("dce_submission/results/maps/sub-001_Ktrans.nii.gz", _tiny_nifti())
        zf.writestr("dce_submission/README.md", "# DCE ktrans submission\n")
    return "dce_team.zip", buf.getvalue()


@pytest.fixture()
def client(tmp_path: Path, monkeypatch) -> Generator[TestClient, None, None]:
    repo_root = Path(__file__).resolve().parents[1]
    for extra in [str(repo_root / "src"), str(repo_root / "backend")]:
        if extra not in sys.path:
            sys.path.insert(0, extra)
    import services.path_config as pc  # noqa: E402

    mapping = {
        "INCOMING_DIR": tmp_path / "incoming",
        "EXTRACTED_DIR": tmp_path / "extracted",
        "VALIDATED_DIR": tmp_path / "validated",
        "OUTPUTS_DIR": tmp_path / "outputs",
        "REFERENCE_DATA_DIR": tmp_path / "ref",
        "SCORING_DIR": tmp_path / "scoring",
        "SCORING_OUTPUTS_DIR": tmp_path / "score_out",
        "SCORING_RESULTS_DIR": tmp_path / "score_out",
        "OSIPI_TF62_DIR": tmp_path / "tf62",
        "CODECOLLECTION_DIR": tmp_path / "codecol",
        "VALIDATION_SUBDIR": tmp_path / "outputs" / "validation",
        "PREVIEW_ROOT": tmp_path / "outputs" / "previews",
        "SCORING_PACKAGES_DIR": tmp_path / "scoring_packages",
        "SCORING_ACTIVE_CONFIG": tmp_path / "scoring" / "active.json",
    }
    for attr, val in mapping.items():
        monkeypatch.setattr(pc, attr, val, raising=False)
    for mod in list(sys.modules.values()):
        for attr, val in mapping.items():
            if hasattr(mod, attr):
                monkeypatch.setattr(mod, attr, val, raising=False)
    for d in (mapping["INCOMING_DIR"], mapping["EXTRACTED_DIR"], mapping["VALIDATED_DIR"],
              mapping["OUTPUTS_DIR"], mapping["VALIDATION_SUBDIR"], mapping["REFERENCE_DATA_DIR"],
              mapping["SCORING_DIR"], mapping["SCORING_OUTPUTS_DIR"], mapping["OSIPI_TF62_DIR"],
              mapping["CODECOLLECTION_DIR"], mapping["SCORING_PACKAGES_DIR"]):
        d.mkdir(parents=True, exist_ok=True)

    import backend.main as app_module  # noqa: E402
    with TestClient(app_module.app, raise_server_exceptions=True) as tc:
        yield tc


def _upload_many(client, zips):
    files = [("files", (name, data, "application/zip")) for name, data in zips]
    return client.post("/api/upload-submissions", files=files)


def test_multi_zip_merges_into_one_batch(client):
    r = _upload_many(client, [_asl_zip(), _dce_zip()])
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch"] is True
    assert body["submission_count"] == 2
    assert not body["failed"]
    ids = {s["submission_id"] for s in body["submissions"]}
    assert ids == {"asl_team", "dce_team"}


def test_each_submission_keeps_its_own_challenge(client):
    body = _upload_many(client, [_asl_zip(), _dce_zip()]).json()
    by_id = {s["submission_id"]: s for s in body["submissions"]}
    assert by_id["asl_team"]["detected_challenge_type"] == "asl"
    assert by_id["dce_team"]["detected_challenge_type"] == "dce"
    # each also carries which archive it came from
    assert by_id["asl_team"]["source_archive"] == "asl_team.zip"


def test_mixed_batch_validates_each_under_its_own_challenge(client):
    body = _upload_many(client, [_asl_zip(), _dce_zip()]).json()
    challenge_types = {s["submission_id"]: s["detected_challenge_type"] for s in body["submissions"]}
    r = client.post("/api/validate-batch", json={
        "submission_ids": list(challenge_types),
        "challenge_type": "asl",              # global fallback
        "challenge_types": challenge_types,   # per-submission overrides
    })
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["mixed_challenges"] is True
    assert set(summary["challenges_present"]) == {"ASL", "DCE"}
    per = {res["submission_id"]: res["challenge_type"] for res in summary["results"]}
    assert per["asl_team"] == "ASL"
    assert per["dce_team"] == "DCE"   # validated under DCE, not the ASL fallback


def test_non_zip_is_reported_not_fatal(client):
    files = [
        ("files", ("asl_team.zip", _asl_zip()[1], "application/zip")),
        ("files", ("notes.txt", b"hello", "text/plain")),
    ]
    r = client.post("/api/upload-submissions", files=files)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["submission_count"] == 1
    assert any(f["filename"] == "notes.txt" for f in body["failed"])


def test_single_zip_endpoint_unchanged(client):
    name, data = _asl_zip()
    r = client.post("/api/upload-submission", files={"file": (name, data, "application/zip")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["batch"] is False
    assert body["submission_id"] == "asl_team"
    assert body["detected_challenge_type"] == "asl"


# ── Slice 2: challenge-grouped reports/exports, no cross-challenge totals ────
def _validate_mixed(client):
    body = _upload_many(client, [_asl_zip(), _dce_zip()]).json()
    ct = {s["submission_id"]: s["detected_challenge_type"] for s in body["submissions"]}
    r = client.post("/api/validate-batch", json={
        "submission_ids": list(ct), "challenge_type": "asl", "challenge_types": ct,
    })
    return r.json()["batch_id"]


def test_mixed_report_grouped_by_challenge_no_cross_total(client):
    batch_id = _validate_mixed(client)
    html = client.get(f"/api/report?batch_id={batch_id}").text
    assert "no cross-challenge totals are computed" in html
    # The caveat moved from a table row into the leader paragraph when the
    # report was restyled; assert the meaning, not the old phrasing.
    assert "spans more than one challenge" in html
    assert "reported per challenge" in html
    assert "ASL RMSE" in html and "DCE RMSE" in html   # per-challenge rows (in QC summary)
    # No single global RMSE row that would pool challenges:
    assert "<td>RMSE</td>" not in html


def test_mixed_combined_export_grouped_one_row_per_submission(client):
    import csv as _csv
    batch_id = _validate_mixed(client)
    text = client.get(f"/api/export-combined?batch_id={batch_id}&format=csv").text
    rows = list(_csv.reader(io.StringIO(text)))
    header, body = rows[0], rows[1:]
    ci = header.index("challenge_type")
    challenges = [r[ci] for r in body]
    assert len(body) == 2                       # one row per submission, no total row
    assert challenges == sorted(challenges)     # grouped/sorted by challenge
    assert set(challenges) == {"ASL", "DCE"}


def test_single_challenge_report_has_no_mixed_note(client):
    name, data = _asl_zip()
    sid = client.post("/api/upload-submission",
                      files={"file": (name, data, "application/zip")}).json()["submission_id"]
    client.post("/api/validate", json={"submission_id": sid, "challenge_type": "asl"})
    html = client.get(f"/api/report?submission_id={sid}").text
    assert "no cross-challenge totals" not in html
    assert "Scoring Summary (per challenge)" not in html
