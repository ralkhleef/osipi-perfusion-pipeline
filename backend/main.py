"""FastAPI backend for the OSIPI perfusion pipeline web interface."""

import csv
import io
import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.github_service import import_github_repo
from services.ingest_service import detect_submission_metadata, save_and_extract, save_uploaded_folder
from services.path_config import (
    EXTRACTED_DIR,
    FRONTEND_DIR,
    INCOMING_DIR,
    OUTPUTS_DIR,
    REFERENCE_DATA_DIR,
    VALIDATED_DIR,
)
from services.validation_service import validate_submission
from services.zenodo_service import download_zenodo_record


# ---------------------------------------------------------------------------
# App startup — ensure all required directories exist
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app):
    for directory in [
        REFERENCE_DATA_DIR,
        OUTPUTS_DIR,
        INCOMING_DIR,
        EXTRACTED_DIR,
        VALIDATED_DIR,
    ]:
        directory.mkdir(parents=True, exist_ok=True)
    yield


app = FastAPI(title="OSIPI Pipeline", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory=str(FRONTEND_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(FRONTEND_DIR / "index.html"))


@app.get("/api/health")
def health():
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Submission intake — upload and extract a ZIP
# ---------------------------------------------------------------------------


@app.post("/api/upload-submission")
async def upload_submission(file: UploadFile = File(...)):
    """Accept a ZIP, save it, extract it, and return a submission_id."""
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted.")

    contents = await file.read()
    result = save_and_extract(contents, file.filename)

    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Upload failed."))

    return result


@app.post("/api/upload-folder-submission")
async def upload_folder_submission(files: List[UploadFile] = File(...)):
    """Accept browser folder-upload files and return a submission_id."""
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    materialized = []
    for file in files:
        materialized.append((file.filename, await file.read()))

    result = save_uploaded_folder(materialized)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Folder upload failed."))

    return result


class SubmissionZenodoRequest(BaseModel):
    zenodo_input: str


@app.post("/api/import-submission-zenodo")
def import_submission_zenodo(req: SubmissionZenodoRequest):
    """Import participant/team submission files from Zenodo."""
    if not req.zenodo_input.strip():
        raise HTTPException(status_code=400, detail="Zenodo input cannot be empty.")

    result = download_zenodo_record(
        req.zenodo_input,
        target_root=EXTRACTED_DIR,
        folder_prefix="zenodo",
        reset_existing=True,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result["errors"][0] if result.get("errors") else "Zenodo import failed.",
        )

    submission_id = f"zenodo_{result['record_id']}"
    file_count = len(result.get("downloaded_files", []))
    return {
        "success": True,
        "submission_id": submission_id,
        "file_count": file_count,
        **detect_submission_metadata(submission_id),
        "message": f"Imported {file_count} file(s) from Zenodo.",
    }


class GitHubSubmissionRequest(BaseModel):
    repo_url: str
    branch: Optional[str] = None


@app.post("/api/import-submission-github")
def import_submission_github(req: GitHubSubmissionRequest):
    """Import a public GitHub repository ZIP archive as a submission."""
    if not req.repo_url.strip():
        raise HTTPException(status_code=400, detail="GitHub repository URL cannot be empty.")

    result = import_github_repo(req.repo_url, req.branch)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result["errors"][0] if result.get("errors") else "GitHub import failed.",
        )

    submission_id = result["submission_id"]
    return {
        "success": True,
        "submission_id": submission_id,
        "file_count": result["file_count"],
        **detect_submission_metadata(submission_id),
        "message": result.get("message", "Imported GitHub repository."),
    }


# ---------------------------------------------------------------------------
# Validation — accepts submission_id, resolves folder internally
# ---------------------------------------------------------------------------


class ValidateRequest(BaseModel):
    submission_id: str
    challenge_type: str = "dce"
    expected_nifti_count: Optional[int] = None
    expected_nifti_count_mode: Optional[str] = None
    include_code: Optional[str] = None    # "yes" or "no"
    include_readme: Optional[str] = None  # "yes" or "no"
    team_name: Optional[str] = None
    contact_email: Optional[str] = None
    map_type: Optional[str] = None
    map_type_mode: Optional[str] = None
    notes: Optional[str] = None


@app.post("/api/validate")
def validate(req: ValidateRequest):
    """Run file-level validation for the given submission_id."""
    if not req.submission_id.strip():
        raise HTTPException(status_code=400, detail="submission_id is required.")
    if req.team_name is not None and len(req.team_name.strip()) > 120:
        raise HTTPException(status_code=400, detail="Team name must be 120 characters or fewer.")
    if req.contact_email is not None and req.contact_email.strip():
        email = req.contact_email.strip()
        if "@" not in email or "." not in email.split("@")[-1]:
            raise HTTPException(status_code=400, detail="Contact email is not a valid email address.")

    return validate_submission(
        req.submission_id,
        challenge_type=req.challenge_type.strip() or "dce",
        expected_nifti_count=req.expected_nifti_count,
        expected_nifti_count_mode=req.expected_nifti_count_mode,
        include_code=req.include_code,
        include_readme=req.include_readme,
        team_name=req.team_name,
        contact_email=req.contact_email,
        map_type=req.map_type,
        map_type_mode=req.map_type_mode,
        notes=req.notes,
    )


# ---------------------------------------------------------------------------
# Shared helper — find validation JSON files in both storage locations
# ---------------------------------------------------------------------------

def _find_validation_files(submission_id: Optional[str] = None):
    """Return validation JSON files from data/outputs/validation/ and data/outputs/ (newest first)."""
    val_subdir = OUTPUTS_DIR / "validation"
    val_subdir.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)

    all_files = list(val_subdir.glob("*_validation.json")) + list(OUTPUTS_DIR.glob("*_validation.json"))
    # Deduplicate by resolved path in case of symlinks
    seen = set()
    unique = []
    for f in all_files:
        key = f.resolve()
        if key not in seen:
            seen.add(key)
            unique.append(f)

    unique.sort(key=lambda f: f.stat().st_mtime, reverse=True)

    if submission_id:
        safe = submission_id.replace("/", "_").replace("\\", "_")
        unique = [f for f in unique if submission_id in f.stem or safe in f.stem]

    return unique


def _msg(item) -> str:
    """Extract a plain string message from either a string or a {message: ...} dict."""
    if isinstance(item, dict):
        return item.get("message", str(item))
    return str(item or "")


# ---------------------------------------------------------------------------
# Outputs — list saved validation results
# ---------------------------------------------------------------------------


@app.get("/api/outputs")
def list_outputs():
    """Return all saved validation results, newest first."""
    results = []
    for f in _find_validation_files():
        try:
            results.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Export — validation results and manifests
# ---------------------------------------------------------------------------


@app.get("/api/export-validation")
def export_validation(
    submission_id: str = Query(...),
    format: str = Query("json"),
):
    """Export the saved validation result for a submission as JSON or CSV."""
    safe_id = submission_id.replace("/", "_").replace("\\", "_")
    candidates = _find_validation_files(submission_id)

    if not candidates:
        raise HTTPException(status_code=404, detail="No validation result found for this submission. Run validation first.")

    data = json.loads(candidates[0].read_text(encoding="utf-8"))

    if format == "json":
        return Response(
            content=json.dumps(data, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="osipi_validation_{safe_id}.json"'},
        )

    # CSV format
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["submission_id", "challenge_type", "passed", "nifti_count",
                     "error_count", "warning_count", "errors", "warnings", "validated_at"])
    writer.writerow([
        data.get("submission_id", ""),
        data.get("challenge_type", ""),
        data.get("passed", ""),
        data.get("nifti_count", ""),
        len(data.get("errors", [])),
        len(data.get("warnings", [])),
        "; ".join(_msg(e) for e in data.get("errors", [])),
        "; ".join(_msg(w) for w in data.get("warnings", [])),
        data.get("validated_at") or data.get("checked_at", ""),
    ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="osipi_validation_{safe_id}.csv"'},
    )


@app.get("/api/export-manifest")
def export_manifest(submission_id: str = Query(...)):
    """Export the ingestion manifest for a submission as CSV."""
    import glob as _glob

    safe_id = submission_id.replace("/", "_").replace("\\", "_")

    # Look for manifest CSV in extracted or outputs
    patterns = [
        str(EXTRACTED_DIR / "**" / f"*{safe_id}*manifest*.csv"),
        str(EXTRACTED_DIR / "**" / "manifest.csv"),
        str(OUTPUTS_DIR / f"*{safe_id}*manifest*.csv"),
    ]
    found = []
    for p in patterns:
        found.extend(_glob.glob(p, recursive=True))

    if not found:
        # Fallback: build a minimal CSV from the validation result
        match_list = _find_validation_files(submission_id)
        match = match_list[0] if match_list else None
        if match:
            data = json.loads(match.read_text(encoding="utf-8"))
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["submission_id", "challenge_type", "passed", "nifti_count",
                              "error_count", "warning_count", "errors", "warnings", "validated_at"])
            writer.writerow([
                data.get("submission_id", ""),
                data.get("challenge_type", ""),
                data.get("passed", ""),
                data.get("nifti_count", ""),
                len(data.get("errors", [])),
                len(data.get("warnings", [])),
                "; ".join(_msg(e) for e in data.get("errors", [])),
                "; ".join(_msg(w) for w in data.get("warnings", [])),
                data.get("validated_at") or data.get("checked_at", ""),
            ])
            return Response(
                content=output.getvalue(),
                media_type="text/csv",
                headers={"Content-Disposition": f'attachment; filename="osipi_manifest_{safe_id}.csv"'},
            )
        raise HTTPException(status_code=404, detail="No manifest found for this submission.")

    return FileResponse(
        found[0],
        media_type="text/csv",
        filename=f"osipi_manifest_{safe_id}.csv",
    )


# ---------------------------------------------------------------------------
# Rankings — all validation results sorted by score
# ---------------------------------------------------------------------------


@app.get("/api/rankings")
def get_rankings():
    """Return all validation results ranked: passed first, then fewest errors, fewest warnings."""
    results = []
    for f in _find_validation_files():
        try:
            results.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass

    def rank_key(r):
        passed = 0 if r.get("passed") else 1
        errors = len(r.get("errors") or [])
        warnings = len(r.get("warnings") or [])
        return (passed, errors, warnings)

    results.sort(key=rank_key)

    ranked = []
    for i, r in enumerate(results, 1):
        ranked.append({
            "rank": i,
            "submission_id": r.get("submission_id", ""),
            "team_name": r.get("team_name", ""),
            "challenge_type": r.get("challenge_type", ""),
            "passed": r.get("passed", False),
            "nifti_count": r.get("nifti_count", 0),
            "error_count": len(r.get("errors") or []),
            "warning_count": len(r.get("warnings") or []),
            "validated_at": r.get("validated_at") or r.get("checked_at", ""),
        })

    return {"rankings": ranked, "count": len(ranked)}


# ---------------------------------------------------------------------------
# Export — HTML report
# ---------------------------------------------------------------------------


@app.get("/api/export-report")
def export_report(submission_id: str = Query(...)):
    """Generate and return a self-contained HTML validation report."""
    safe_id = submission_id.replace("/", "_").replace("\\", "_")
    candidates = _find_validation_files(submission_id)

    if not candidates:
        raise HTTPException(status_code=404, detail="No validation result found. Run validation first.")

    d = json.loads(candidates[0].read_text(encoding="utf-8"))

    passed = d.get("passed", False)
    status_color = "#2D6A4F" if passed else "#A83232"
    status_text = "PASSED" if passed else "FAILED"
    errors = d.get("errors") or []
    warnings = d.get("warnings") or []
    checked = d.get("validated_at") or d.get("checked_at", "")
    team = d.get("team_name", "") or d.get("submission_id", submission_id)

    def issue_rows(items, row_class):
        if not items:
            return "<p style='color:#6B6278;margin:0'>None</p>"
        rows = ""
        for item in items:
            msg = item.get("message", str(item)) if isinstance(item, dict) else str(item)
            rows += f'<div class="{row_class}" style="padding:6px 10px;margin:4px 0;border-radius:6px;font-size:13px">{msg}</div>'
        return rows

    error_html = issue_rows(errors, "error-row")
    warn_html = issue_rows(warnings, "warn-row")

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8"/>
<title>OSIPI Validation Report — {team}</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
    background:#F2EEF6;margin:0;padding:2rem;color:#1E1A2E}}
  .card{{background:#fff;border:1px solid #E0D9EA;border-radius:12px;padding:1.5rem 2rem;
    max-width:720px;margin:0 auto 1.5rem}}
  h1{{font-size:1.25rem;margin:0 0 0.25rem;color:#5B4678}}
  .sub{{color:#6B6278;font-size:0.85rem;margin:0 0 1.25rem}}
  .meta-grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:0.75rem;
    margin-bottom:1.25rem}}
  .meta-item span{{display:block;font-size:0.75rem;color:#6B6278;text-transform:uppercase;
    letter-spacing:.05em;margin-bottom:2px}}
  .meta-item strong{{font-size:0.9rem}}
  .status-banner{{border-radius:8px;padding:0.6rem 1rem;font-weight:600;font-size:1rem;
    color:#fff;background:{status_color};margin-bottom:1.25rem;display:inline-block}}
  .section-title{{font-size:0.8rem;font-weight:600;text-transform:uppercase;
    letter-spacing:.06em;color:#6B6278;margin:1rem 0 0.4rem}}
  .error-row{{background:#FCF1F1;color:#A83232;border-left:3px solid #A83232}}
  .warn-row{{background:#FBF5EA;color:#8A5A1A;border-left:3px solid #D4A017}}
</style>
</head>
<body>
<div class="card">
  <h1>OSIPI Perfusion Pipeline — Validation Report</h1>
  <p class="sub">Generated {checked}</p>
  <div class="status-banner">{status_text}</div>
  <div class="meta-grid">
    <div class="meta-item"><span>Team / Submission</span><strong>{team}</strong></div>
    <div class="meta-item"><span>Challenge</span><strong>{d.get("challenge_type","—").upper()}</strong></div>
    <div class="meta-item"><span>NIfTI files</span><strong>{d.get("nifti_count","—")}</strong></div>
    <div class="meta-item"><span>Errors</span><strong>{len(errors)}</strong></div>
    <div class="meta-item"><span>Warnings</span><strong>{len(warnings)}</strong></div>
  </div>
  <div class="section-title">Errors ({len(errors)})</div>
  {error_html}
  <div class="section-title">Warnings ({len(warnings)})</div>
  {warn_html}
</div>
</body>
</html>"""

    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="osipi_report_{safe_id}.html"'},
    )


# ---------------------------------------------------------------------------
# NIfTI viewer — list and serve NIfTI files for browser-side rendering
# ---------------------------------------------------------------------------

NIFTI_SUFFIXES = (".nii", ".nii.gz")


@app.get("/api/nifti-files/{submission_id}")
def list_nifti_files(submission_id: str):
    """Return the NIfTI filenames found for a given submission."""
    safe_id = submission_id.replace("/", "_").replace("\\", "_")
    folder = EXTRACTED_DIR / safe_id
    if not folder.exists() or not folder.is_dir():
        return {"files": []}
    files = [
        str(p.relative_to(folder))
        for p in sorted(folder.rglob("*"))
        if p.is_file() and p.name.lower().endswith(NIFTI_SUFFIXES)
    ]
    return {"files": files, "submission_id": safe_id}


@app.get("/api/nifti/{submission_id}/{filepath:path}")
def serve_nifti(submission_id: str, filepath: str):
    """Serve a NIfTI file from the submission folder for the browser viewer."""
    safe_id = submission_id.replace("/", "_").replace("\\", "_")
    folder = EXTRACTED_DIR / safe_id
    # Resolve and verify the file is inside the submission folder (no path traversal)
    try:
        target = (folder / filepath).resolve()
        folder.resolve()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid file path.")

    if not str(target).startswith(str(folder.resolve())):
        raise HTTPException(status_code=403, detail="Access denied.")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if not target.name.lower().endswith(NIFTI_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only NIfTI files can be served.")

    media_type = "application/gzip" if target.name.lower().endswith(".gz") else "application/octet-stream"
    return FileResponse(
        str(target),
        media_type=media_type,
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )
