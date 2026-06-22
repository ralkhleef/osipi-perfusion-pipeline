"""FastAPI backend for the OSIPI perfusion pipeline web interface."""

import csv
import io
import json
import os
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from services.github_service import import_github_repo
from services.ingest_service import (
    EXTRACT_MAX_BYTES,
    EXTRACT_MAX_FILES,
    ZIP_MAX_BYTES,
    detect_submission_metadata,
    finalize_imported_dir,
    make_safe_id,
    save_and_extract_batch_from_path,
    save_folder_as_batch,
    save_uploaded_folder,
)
from services.path_config import (
    EXTRACTED_DIR,
    FRONTEND_DIR,
    INCOMING_DIR,
    OUTPUTS_DIR,
    REFERENCE_DATA_DIR,
    VALIDATED_DIR,
)
from services.execution_service import run_submission
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
    """Accept a ZIP, save it, extract it, and return a submission_id.

    Streams the upload to disk in 64 KB chunks to avoid loading the entire
    file into RAM.  The size limit is enforced while streaming.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted.")

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(INCOMING_DIR), suffix=".tmp")
    tmp_path = Path(tmp_name)

    try:
        total_bytes = 0
        with os.fdopen(tmp_fd, "wb") as fout:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > ZIP_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"ZIP file is too large (limit: {ZIP_MAX_BYTES // (1024 * 1024)} MB).",
                    )
                fout.write(chunk)

        safe_filename = Path(file.filename).name
        final_path = INCOMING_DIR / safe_filename
        tmp_path.replace(final_path)
        tmp_path = Path(tmp_name)  # keep reference for finally; replace() makes it gone

        result = save_and_extract_batch_from_path(final_path, file.filename)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Upload failed."))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if Path(tmp_name).exists():
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


@app.post("/api/upload-folder-submission")
async def upload_folder_submission(files: List[UploadFile] = File(...)):
    """Accept browser folder-upload files and return a submission_id.

    Enforces file-count and cumulative-size limits before staging.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")
    if len(files) > EXTRACT_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files in folder upload (limit: {EXTRACT_MAX_FILES:,}).",
        )

    materialized = []
    total_bytes = 0
    for f in files:
        contents = await f.read()
        total_bytes += len(contents)
        if total_bytes > EXTRACT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Folder upload exceeds size limit ({EXTRACT_MAX_BYTES // (1024 ** 3)} GB).",
            )
        materialized.append((f.filename, contents))

    result = save_uploaded_folder(materialized)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Folder upload failed."))

    return result


@app.post("/api/upload-folder-batch")
async def upload_folder_batch(files: List[UploadFile] = File(...)):
    """Accept browser folder-upload and auto-detect single vs. batch submissions.

    Preserves ``webkitRelativePath`` relative paths so the backend can detect
    nested submission folders.  Enforces file-count and cumulative-size limits
    before staging.  Returns a batch result if multiple top-level directories
    each contain NIfTI files; otherwise identical to ``/api/upload-folder-submission``.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")
    if len(files) > EXTRACT_MAX_FILES:
        raise HTTPException(
            status_code=400,
            detail=f"Too many files in folder upload (limit: {EXTRACT_MAX_FILES:,}).",
        )

    materialized = []
    total_bytes = 0
    for f in files:
        contents = await f.read()
        total_bytes += len(contents)
        if total_bytes > EXTRACT_MAX_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Folder upload exceeds size limit ({EXTRACT_MAX_BYTES // (1024 ** 3)} GB).",
            )
        materialized.append((f.filename, contents))

    result = save_folder_as_batch(materialized)
    if not result.get("success"):
        raise HTTPException(status_code=400, detail=result.get("error", "Folder upload failed."))

    return result


class SubmissionZenodoRequest(BaseModel):
    zenodo_input: str


@app.post("/api/import-submission-zenodo")
def import_submission_zenodo(req: SubmissionZenodoRequest):
    """Import participant/team submission files from Zenodo.

    After download, runs the same batch-boundary detection as local ZIP uploads.
    Returns ``batch: true`` if multiple submission folders are detected.
    """
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

    record_id    = result["record_id"]
    title        = result.get("title") or f"Zenodo {record_id}"
    submission_id = f"zenodo_{record_id}"
    zenodo_dir   = EXTRACTED_DIR / submission_id
    display_name = f"{title} (Zenodo)"

    batch_result = finalize_imported_dir(zenodo_dir, submission_id, display_name, "zenodo")
    if not batch_result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=batch_result.get("error", "Zenodo import failed."),
        )
    return batch_result


class GitHubSubmissionRequest(BaseModel):
    repo_url: str
    branch: Optional[str] = None


@app.post("/api/import-submission-github")
def import_submission_github(req: GitHubSubmissionRequest):
    """Import a public GitHub repository ZIP archive as a submission.

    After download + extraction, runs batch-boundary detection.
    Returns ``batch: true`` if multiple submission folders are found in the repo.
    """
    if not req.repo_url.strip():
        raise HTTPException(status_code=400, detail="GitHub repository URL cannot be empty.")

    result = import_github_repo(req.repo_url, req.branch)
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("errors", [result.get("message", "GitHub import failed.")])[0],
        )

    return result


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

    # CSV format — one summary row per submission
    errors   = data.get("errors") or []
    warnings = data.get("warnings") or []
    passed   = data.get("passed", False)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "submission_id",
        "validation_timestamp",
        "team_name",
        "contact_email",
        "challenge_type",
        "parameter_map_type",
        "validation_status",
        "ready_for_scoring",
        "nifti_file_count",
        "total_file_count",
        "error_count",
        "warning_count",
        "errors",
        "warnings",
    ])
    writer.writerow([
        data.get("submission_id", ""),
        data.get("validated_at") or data.get("checked_at", ""),
        data.get("team_name", ""),
        data.get("contact_email", ""),
        data.get("challenge_type", ""),
        data.get("map_type", ""),
        "PASSED" if passed else "FAILED",
        "yes" if passed else "no",
        data.get("nifti_count", ""),
        data.get("total_files", ""),
        len(errors),
        len(warnings),
        " | ".join(_msg(e) for e in errors),
        " | ".join(_msg(w) for w in warnings),
    ])
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="osipi-validation-{safe_id}.csv"'},
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
# Batch upload — auto-detects single vs. multi-submission ZIP
# ---------------------------------------------------------------------------


@app.post("/api/upload-batch")
async def upload_batch(file: UploadFile = File(...)):
    """Accept a ZIP that may contain multiple team submissions.

    Streams the upload to disk in 64 KB chunks — the full file is never held
    in RAM.  The size limit is enforced while streaming.  If the ZIP's top-level
    structure contains several directories each holding NIfTI files, each
    directory is treated as an independent submission and the response includes
    a ``submissions`` list.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted.")

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(INCOMING_DIR), suffix=".tmp")
    tmp_path = Path(tmp_name)

    try:
        total_bytes = 0
        with os.fdopen(tmp_fd, "wb") as fout:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > ZIP_MAX_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail=f"ZIP file is too large (limit: {ZIP_MAX_BYTES // (1024 * 1024)} MB).",
                    )
                fout.write(chunk)

        safe_filename = Path(file.filename).name
        final_path = INCOMING_DIR / safe_filename
        tmp_path.replace(final_path)

        result = save_and_extract_batch_from_path(final_path, file.filename)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Upload failed."))
        return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        if Path(tmp_name).exists():
            try:
                Path(tmp_name).unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Batch validation — validate multiple submission IDs in one request
# ---------------------------------------------------------------------------


class BatchValidateRequest(BaseModel):
    submission_ids: List[str]
    challenge_type: str = "dce"
    map_type: Optional[str] = None
    map_type_mode: Optional[str] = None
    notes: Optional[str] = None
    # Optional per-submission metadata; key = submission_id
    team_names: Optional[Dict[str, str]] = None
    contact_emails: Optional[Dict[str, str]] = None


@app.post("/api/validate-batch")
def validate_batch_endpoint(req: BatchValidateRequest):
    """Validate multiple submission IDs and return an aggregate batch result.

    Each submission is validated independently; one failure does not stop the
    rest.  The response contains a ``batch_id`` that can be used with
    ``/api/export-batch`` to download the results as CSV.
    """
    from services.validation_service import validate_batch

    if not req.submission_ids:
        raise HTTPException(status_code=400, detail="submission_ids must not be empty.")
    if len(req.submission_ids) > 500:
        raise HTTPException(status_code=400, detail="At most 500 submission IDs per batch.")

    return validate_batch(
        submission_ids=req.submission_ids,
        challenge_type=req.challenge_type,
        map_type=req.map_type,
        map_type_mode=req.map_type_mode,
        notes=req.notes,
        team_names=req.team_names,
        contact_emails=req.contact_emails,
    )


# ---------------------------------------------------------------------------
# Batch export — blinded and unblinded CSV
# ---------------------------------------------------------------------------


@app.get("/api/export-batch")
def export_batch(
    batch_id: str = Query(..., description="batch_id returned by /api/validate-batch"),
    format: str = Query("csv", description="'csv' or 'json'"),
    blinded: bool = Query(False, description="True to strip team_name and contact_email"),
):
    """Export a previously validated batch as CSV (blinded or unblinded) or JSON.

    Unblinded CSV includes ``team_name`` and ``contact_email``.
    Blinded CSV replaces them with the anonymous ``submission_id`` only.
    """
    from services.validation_service import find_batch_result

    batch = find_batch_result(batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail="Batch not found. Run /api/validate-batch first and use the returned batch_id.",
        )

    safe_id = batch_id.replace("/", "_").replace("\\", "_")

    if format == "json":
        if blinded:
            # Strip PII before returning
            batch = _blind_batch(batch)
        return Response(
            content=json.dumps(batch, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{safe_id}.json"'},
        )

    # ── CSV ───────────────────────────────────────────────────────────────────
    output = io.StringIO()
    writer = csv.writer(output)

    if blinded:
        writer.writerow([
            "submission_id", "challenge_type", "detected_map_types",
            "validation_status", "ready_for_scoring",
            "nifti_count", "total_files",
            "error_count", "warning_count",
            "errors", "warnings",
            "validation_timestamp",
        ])
    else:
        writer.writerow([
            "submission_id", "team_name", "contact_email",
            "challenge_type", "detected_map_types",
            "validation_status", "ready_for_scoring",
            "nifti_count", "total_files",
            "error_count", "warning_count",
            "errors", "warnings",
            "validation_timestamp",
        ])

    for r in batch.get("results", []):
        errors   = r.get("errors")   or []
        warnings = r.get("warnings") or []
        passed   = r.get("passed", False)

        row: list = [r.get("submission_id", "")]
        if not blinded:
            row.append(r.get("team_name", ""))
            row.append(r.get("contact_email", ""))
        row.extend([
            r.get("challenge_type", ""),
            r.get("map_type", ""),
            "PASSED" if passed else "FAILED",
            "yes" if passed else "no",
            r.get("nifti_count", ""),
            r.get("total_files", ""),
            len(errors),
            len(warnings),
            " | ".join(_msg(e) for e in errors),
            " | ".join(_msg(w) for w in warnings),
            r.get("validated_at") or r.get("checked_at", ""),
        ])
        writer.writerow(row)

    suffix   = "blinded" if blinded else "unblinded"
    csv_name = f"{safe_id}_{suffix}.csv"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{csv_name}"'},
    )


def _blind_batch(batch: dict) -> dict:
    """Return a copy of a batch result with PII fields removed."""
    import copy
    b = copy.deepcopy(batch)
    for r in b.get("results", []):
        r.pop("team_name", None)
        r.pop("contact_email", None)
    return b


# ---------------------------------------------------------------------------
# NIfTI viewer — list and serve NIfTI files for browser-side rendering
# ---------------------------------------------------------------------------

NIFTI_SUFFIXES = (".nii", ".nii.gz")


@app.get("/api/nifti-files/{submission_id}")
def list_nifti_files(submission_id: str):
    """Return the NIfTI filenames found for a given submission."""
    safe_id = make_safe_id(submission_id)
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
    safe_id = make_safe_id(submission_id)
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


# ---------------------------------------------------------------------------
# Docker execution — build image and run submission
# ---------------------------------------------------------------------------


class ExecuteRequest(BaseModel):
    submission_id: str
    challenge_type: str = "dce"
    timeout_seconds: int = 300


@app.post("/api/execute")
def execute_submission_endpoint(req: ExecuteRequest):
    """Build a Docker image for the submission and run it inside a sandboxed container.

    Resolves ``submission_id`` to the extracted folder path, then delegates to
    :func:`run_submission` which calls :func:`execute_submission` from the pipeline
    package.  Returns the full :class:`ExecutionResult` dict plus ``stdout_preview``
    and ``stderr_preview`` (first 8 KB of each log file).

    The container is run with:

    - ``--network none`` — no outbound internet access.
    - ``--security-opt no-new-privileges`` — no privilege escalation.
    - ``--memory 4g`` and ``--cpus 2.0`` — resource limits.
    - Submission mounted at ``/submission:ro``.
    - Output directory mounted at ``/output:rw``.
    """
    if not req.submission_id.strip():
        raise HTTPException(status_code=400, detail="submission_id is required.")
    if req.timeout_seconds < 10 or req.timeout_seconds > 3600:
        raise HTTPException(
            status_code=400,
            detail="timeout_seconds must be between 10 and 3600.",
        )

    result = run_submission(
        req.submission_id.strip(),
        challenge_type=req.challenge_type.strip() or "dce",
        timeout_seconds=req.timeout_seconds,
    )

    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("message", "Docker execution failed."),
        )

    return result
