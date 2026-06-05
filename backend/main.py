"""FastAPI backend for the OSIPI perfusion pipeline web interface."""

import json
from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
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
from services.zenodo_service import download_zenodo_record, handle_zenodo_input


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

    return {
        "success": True,
        "submission_id": result["submission_id"],
        "file_count": result["file_count"],
        "nifti_count": result.get("nifti_count", 0),
        "detected_parameter_map_type": result.get("detected_parameter_map_type", "Unknown"),
        "detected_map_type_confidence": result.get("detected_map_type_confidence", "none"),
        "detection_warning": result.get("detection_warning"),
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
# Outputs — list saved validation results
# ---------------------------------------------------------------------------


@app.get("/api/outputs")
def list_outputs():
    """Return all saved validation results, newest first."""
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    files = sorted(
        OUTPUTS_DIR.glob("*_validation.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    for f in files:
        try:
            results.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            pass
    return {"results": results, "count": len(results)}


# ---------------------------------------------------------------------------
# Challenge data — Zenodo download
# ---------------------------------------------------------------------------


class ZenodoRequest(BaseModel):
    zenodo_input: str


@app.post("/api/import-challenge-data")
def import_challenge_data(req: ZenodoRequest):
    """Parse a Zenodo URL/DOI/ID, fetch metadata, and download files."""
    if not req.zenodo_input.strip():
        raise HTTPException(status_code=400, detail="Zenodo input cannot be empty.")

    result = handle_zenodo_input(req.zenodo_input)

    if not result.get("success") and not result.get("record_id"):
        raise HTTPException(
            status_code=400,
            detail=result["errors"][0] if result["errors"] else "Invalid Zenodo input.",
        )

    return result
