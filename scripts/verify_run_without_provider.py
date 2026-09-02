"""Run Analysis with no scoring provider configured.

A scoring provider and a comparison against the organisers' ground truth are
two different things, and only the first one needs configuring. Every challenge
starts life with reference maps and masks in place and no provider at all --
which is exactly the state the DCE challenge is in -- and in that state the
comparison still produces bias, RMSE, error CoV and the ROI descriptive tables.

The Score step used to treat "no provider" as "nothing to run": the card was
hidden, the table was hidden, and the button reported that there was nothing
for it to do. This script proves the opposite by driving the real routes the
browser calls with the active configuration set to mode="none", on data small
enough to run in seconds.

Run:  python3 scripts/verify_run_without_provider.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path

import nibabel as nib
import numpy as np

REPO = Path(__file__).resolve().parents[1]
for extra in ("src", "backend", "."):
    p = str(REPO / extra)
    if p not in sys.path:
        sys.path.insert(0, p)

WORK = Path(tempfile.mkdtemp(prefix="osipi_noprovider_"))
SHAPE = (12, 12, 6)
AFFINE = np.diag([2.0, 2.0, 4.0, 1.0])

rng = np.random.default_rng(20260901)
truth = rng.uniform(0.02, 0.20, SHAPE).astype(np.float32)

gm = np.zeros(SHAPE, dtype=np.float32)
gm[2:7, 2:7, 1:4] = 1.0
wm = np.zeros(SHAPE, dtype=np.float32)
wm[8:11, 3:8, 1:4] = 1.0

# Correct everywhere except inside grey matter, where Ktrans is 0.01 too high.
# A comparison that is actually running has to show that; one that is being
# skipped cannot.
GM_BIAS = 0.01
submitted = truth + gm * GM_BIAS


def save(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    nib.save(nib.Nifti1Image(np.asarray(data, np.float32), AFFINE), str(path))
    return path


ref_root = WORK / "reference_data"
save(ref_root / "masks" / "GM_mask.nii.gz", gm)
save(ref_root / "masks" / "WM_mask.nii.gz", wm)
save(ref_root / "dce" / "maps" / "Ktrans.nii.gz", truth)

staging = WORK / "staging"
save(staging / "Ktrans.nii.gz", submitted)
(staging / "methods.txt").write_text("Placeholder methods document.\n", encoding="utf-8")

buf = BytesIO()
with zipfile.ZipFile(buf, "w") as zf:
    for f in sorted(staging.rglob("*")):
        if f.is_file():
            zf.write(f, f"team_delta/{f.relative_to(staging)}")
zip_bytes = buf.getvalue()

import services.path_config as pc  # noqa: E402

mapping = {
    "INCOMING_DIR": WORK / "incoming",
    "EXTRACTED_DIR": WORK / "extracted",
    "OUTPUTS_DIR": WORK / "outputs",
    "VALIDATION_SUBDIR": WORK / "outputs" / "validation",
    "PREVIEW_ROOT": WORK / "outputs" / "previews",
    "REFERENCE_DATA_DIR": ref_root,
    "SCORING_DIR": WORK / "scoring",
    "SCORING_OUTPUTS_DIR": WORK / "score_out",
    "SCORING_RESULTS_DIR": WORK / "score_out",
    "OSIPI_TF62_DIR": WORK / "tf62",
    "CODECOLLECTION_DIR": WORK / "codecol",
    "SCORING_PACKAGES_DIR": WORK / "packages",
    "SCORING_ACTIVE_CONFIG": WORK / "scoring" / "active.json",
    "CONFIG_MANAGER_DIR": WORK / "cfg",
    "CONFIG_VERSIONS_DIR": WORK / "cfg" / "versions",
    "CONFIG_ACTIVE_VERSION": WORK / "cfg" / "active.json",
}
for name, value in mapping.items():
    setattr(pc, name, value)
for mod in list(sys.modules.values()):
    for name, value in mapping.items():
        if hasattr(mod, name):
            setattr(mod, name, value)
for d in mapping.values():
    if d.suffix != ".json":
        d.mkdir(parents=True, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402
import backend.main as app_module  # noqa: E402
import scoring as scoring_mod  # noqa: E402

for target in (app_module, scoring_mod):
    for name, value in mapping.items():
        if hasattr(target, name):
            setattr(target, name, value)

COMPARED = {"available", "partial_reference_scoring"}
failures: list[str] = []


def check(desc: str, condition: bool, extra: str = "") -> None:
    if condition:
        print(f"  OK    {desc}")
    else:
        print(f"  FAIL  {desc}" + (f" -- {extra}" if extra else ""))
        failures.append(desc)


def rule(title: str) -> None:
    print(f"\n{'=' * 74}\n{title}\n{'=' * 74}")


with TestClient(app_module.app, raise_server_exceptions=True) as client:
    rule("No provider is configured")
    active = scoring_mod.get_active_entry("dce")
    check("the active scoring mode is none", active.get("mode", "none") == "none",
          json.dumps(active))

    rule("Upload and validate")
    r = client.post(
        "/api/upload-submission",
        files={"file": ("team_delta.zip", zip_bytes, "application/zip")},
        data={"challenge_type": "dce"},
    )
    check("upload accepted", r.status_code == 200, str(r.status_code))
    body = r.json()
    sid = body.get("submission_id") or (body.get("submissions") or [{}])[0].get("submission_id")
    check("a submission id came back", bool(sid), json.dumps(body)[:200])

    r = client.post("/api/validate", json={"submission_id": sid, "challenge_type": "dce"})
    check("validation ran", r.status_code == 200, str(r.status_code))

    rule("What the Score step reads before anything is pressed")
    r = client.get("/api/scoring-status", params={
        "submission_id": sid, "challenge_type": "dce", "map_type": "ktrans"})
    check("scoring-status answered", r.status_code == 200, str(r.status_code))
    status_payload = r.json()
    ref = status_payload.get("reference_scoring") or {}
    print(f"    status={status_payload.get('status')!r} "
          f"reference={ref.get('status')!r} masks={ref.get('mask_count')}")
    check("the provider is reported as unconfigured",
          status_payload.get("status") == "not_configured", str(status_payload.get("status")))
    check("the comparison against ground truth ran anyway",
          str(ref.get("status")) in COMPARED, str(ref.get("status")))
    check("the masks were found", (ref.get("mask_count") or 0) >= 2, str(ref.get("mask_count")))
    check("ROI descriptive rows exist",
          len(ref.get("roi_descriptive_statistics") or []) > 0)

    rule("Pressing Run Analysis")
    r = client.post("/api/score", json={
        "submission_id": sid, "challenge_type": "dce", "map_type": "ktrans"})
    check("the run answered 200", r.status_code == 200, str(r.status_code))
    score_payload = r.json()
    ref2 = score_payload.get("reference_scoring") or {}
    check("the run still reports no provider",
          score_payload.get("status") == "not_configured", str(score_payload.get("status")))
    check("and still produced a comparison",
          str(ref2.get("status")) in COMPARED, str(ref2.get("status")))

    rule("The numbers are real, not placeholders")
    rows = []
    for row in ref2.get("maps") or []:
        for mask in row.get("masks") or []:
            metrics = mask.get("metrics") or {}
            if mask.get("status") == "compared":
                rows.append((mask.get("mask_label"), metrics.get("bias"), metrics.get("rmse")))
    for label, bias, rmse in rows:
        print(f"    {label:<14} bias={bias:+.5f}  rmse={rmse:.5f}")
    gm_row = next((r_ for r_ in rows if "gray" in str(r_[0]).lower()), None)
    wm_row = next((r_ for r_ in rows if "white" in str(r_[0]).lower()), None)
    check("grey matter was compared", gm_row is not None)
    check("white matter was compared", wm_row is not None)
    if gm_row:
        check(f"grey matter recovers the {GM_BIAS} bias that was injected",
              abs(gm_row[1] - GM_BIAS) < 1e-5, f"bias={gm_row[1]}")
    if wm_row:
        check("white matter, which was left alone, shows no bias",
              abs(wm_row[1]) < 1e-6, f"bias={wm_row[1]}")

    rule("The results can leave the page")
    r = client.get("/api/export-roi-descriptive", params={"submission_id": sid})
    check("the ROI CSV exports", r.status_code == 200, str(r.status_code))
    check("it has data rows", len(r.text.strip().splitlines()) > 1)
    r = client.get("/api/report", params={"submission_id": sid, "blinded": "false"})
    check("the HTML report renders", r.status_code == 200, str(r.status_code))
    check("the report names the compared regions",
          "gray matter" in r.text and "white matter" in r.text)

    dump = Path("/tmp/noprovider_status_payload.json")
    dump.write_text(json.dumps(status_payload, default=str), encoding="utf-8")
    print(f"\nstatus payload written to {dump}")

print(f"\n=== {len(failures)} failed ===\n" if failures else "\n=== all checks passed ===\n")
sys.exit(1 if failures else 0)
