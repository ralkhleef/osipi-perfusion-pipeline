"""FastAPI backend for the OSIPI perfusion pipeline web interface."""

import csv
import io
import json
import os
import shutil
import subprocess
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
    CODECOLLECTION_DIR,
    EXTRACTED_DIR,
    FRONTEND_DIR,
    INCOMING_DIR,
    OSIPI_TF62_DIR,
    OUTPUTS_DIR,
    REFERENCE_DATA_DIR,
    SCORING_DIR,
    SCORING_OUTPUTS_DIR,
    SCORING_RESULTS_DIR,  # backward-compat alias
    VALIDATED_DIR,
)
from services.execution_service import run_submission
from services.validation_service import (
    find_batch_result,
    preflight_check,
    validate_batch,
    validate_submission,
)
from services.zenodo_service import download_zenodo_record
from scoring import (
    all_providers_status,
    batch_scoring_status,
    load_scoring_result,
    score_batch,
    score_submission,
    scoring_status,
)


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
        SCORING_DIR,
        SCORING_OUTPUTS_DIR,
        OSIPI_TF62_DIR,
        CODECOLLECTION_DIR,
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


@app.get("/api/execution-status")
def execution_status():
    """Return Docker availability and version for the current backend environment."""
    docker_path = shutil.which("docker")
    if not docker_path:
        return {
            "docker_available": False,
            "docker_version": "",
            "message": "Docker is not installed or not available on PATH.",
        }
    try:
        result = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            return {
                "docker_available": True,
                "docker_version": version,
                "message": f"Docker {version} is available.",
            }
        # Docker binary found but daemon not reachable
        return {
            "docker_available": False,
            "docker_version": "",
            "message": "Docker daemon is not running or the socket is not accessible.",
        }
    except subprocess.TimeoutExpired:
        return {
            "docker_available": False,
            "docker_version": "",
            "message": "Docker availability check timed out.",
        }
    except Exception as exc:
        return {
            "docker_available": False,
            "docker_version": "",
            "message": str(exc),
        }


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
    mode: str = "auto"  # "auto" | "result_only" | "result_validation" | "reproducible" | "reproducible_execution"


class PreflightRequest(BaseModel):
    submission_id: str
    challenge_type: str = "dce"
    team_name: Optional[str] = None
    contact_email: Optional[str] = None


_VALID_MODES = {"auto", "result_only", "result_validation", "reproducible", "reproducible_execution"}

def _normalise_mode(raw: Optional[str]) -> str:
    """Normalise mode string to one of the values validate_submission() accepts."""
    m = (raw or "auto").strip().lower()
    if m == "reproducible_execution":
        return "reproducible"
    if m not in _VALID_MODES:
        return "auto"
    return m


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

    mode = _normalise_mode(req.mode)

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
        mode=mode,
    )


@app.post("/api/preflight")
def preflight(req: PreflightRequest):
    """Check whether a submission is ready to execute (reproducible mode).

    Does not require NIfTI output maps — they will be generated by execution.
    Returns ``runnable``, ``has_run_instructions``, ``has_run_config``,
    ``has_existing_maps``, ``errors``, and ``warnings``.
    """
    if not req.submission_id.strip():
        raise HTTPException(status_code=400, detail="submission_id is required.")
    return preflight_check(
        req.submission_id.strip(),
        challenge_type=req.challenge_type.strip() or "dce",
        team_name=req.team_name,
        contact_email=req.contact_email,
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
    blinded: bool = Query(False, description="True to strip team_name and contact_email"),
):
    """Export the saved validation result for a submission as JSON or CSV.

    When ``blinded=True`` the CSV omits ``team_name`` and ``contact_email`` so
    the file is safe for peer review or public release.
    """
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

    if blinded:
        writer.writerow([
            "submission_id",
            "validation_timestamp",
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
    else:
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

    suffix = "blinded" if blinded else "unblinded"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="osipi-validation-{suffix}-{safe_id}.csv"'},
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
    team_names: Optional[Dict[str, str]] = None
    contact_emails: Optional[Dict[str, str]] = None
    mode: str = "auto"  # "auto" | "result_only" | "result_validation" | "reproducible" | "reproducible_execution"


@app.post("/api/validate-batch")
def validate_batch_endpoint(req: BatchValidateRequest):
    """Validate multiple submission IDs and return an aggregate batch result."""

    if not req.submission_ids:
        raise HTTPException(status_code=400, detail="submission_ids must not be empty.")
    if len(req.submission_ids) > 500:
        raise HTTPException(status_code=400, detail="At most 500 submission IDs per batch.")

    mode = _normalise_mode(req.mode)

    return validate_batch(
        submission_ids=req.submission_ids,
        challenge_type=req.challenge_type,
        map_type=req.map_type,
        map_type_mode=req.map_type_mode,
        notes=req.notes,
        team_names=req.team_names,
        contact_emails=req.contact_emails,
        mode=mode,
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
    timeout_seconds: Optional[int] = None  # None → let run_config.json or default decide
    map_type: Optional[str] = None         # forwarded to post-execution output validation


@app.post("/api/execute")
def execute_submission_endpoint(req: ExecuteRequest):
    """Build a Docker image for the submission and run it inside a sandboxed container.

    Resolves ``submission_id`` to the extracted folder path, checks that the
    submission contains a ``Dockerfile``, then delegates to :func:`run_submission`.

    Returns the full :class:`ExecutionResult` dict plus ``stdout_preview``,
    ``stderr_preview`` (first 8 KB each), and ``output_file_count``.

    **``success`` vs ``passed``**:

    - ``success: false`` — pre-flight error (no Dockerfile, bad submission_id,
      Docker not installed).  Returns HTTP 400.
    - ``success: true, passed: false`` — execution ran but failed (build error,
      non-zero exit, timeout).  Returns HTTP 200 with full result + logs so the
      UI can display what went wrong.

    Resource constraints applied to every container:

    - ``--network none`` — no outbound internet access.
    - ``--security-opt no-new-privileges`` — no privilege escalation.
    - ``--memory 4g`` and ``--cpus 2.0`` — resource limits.
    - Submission mounted at ``/submission:ro``.
    - Output directory mounted at ``/output:rw``.
    """
    if not req.submission_id.strip():
        raise HTTPException(status_code=400, detail="submission_id is required.")
    if req.timeout_seconds is not None and (
        req.timeout_seconds < 10 or req.timeout_seconds > 3600
    ):
        raise HTTPException(
            status_code=400,
            detail="timeout_seconds must be between 10 and 3600.",
        )

    result = run_submission(
        req.submission_id.strip(),
        challenge_type=req.challenge_type.strip() or "dce",
        timeout_seconds=req.timeout_seconds,
        map_type=req.map_type,
    )

    # Pre-flight failures (no Dockerfile, invalid path, Docker missing) → 400
    if not result.get("success"):
        raise HTTPException(
            status_code=400,
            detail=result.get("message", "Docker execution failed."),
        )

    # Save execution result to disk so /api/export-execution can read it later.
    _save_execution_result(req.submission_id.strip(), result)

    # Build/run failures → 200 with full result so the UI can show logs
    return result


# ---------------------------------------------------------------------------
# Execution result persistence helpers
# ---------------------------------------------------------------------------

def _exec_result_path(submission_id: str) -> Path:
    """Return the path where an execution result JSON is stored."""
    safe_id = make_safe_id(submission_id)
    exec_dir = OUTPUTS_DIR / "execution_results"
    exec_dir.mkdir(parents=True, exist_ok=True)
    return exec_dir / f"{safe_id}_exec.json"


def _save_execution_result(submission_id: str, result: dict) -> None:
    """Persist the execution result dict to disk for later export."""
    try:
        path = _exec_result_path(submission_id)
        path.write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
    except Exception:
        pass  # best-effort; do not break the execution response


def _load_execution_result(submission_id: str) -> Optional[dict]:
    """Load a previously saved execution result, or None if not found."""
    path = _exec_result_path(submission_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Execution exports — blinded and unblinded CSV
# ---------------------------------------------------------------------------

def _exec_result_to_row(r: dict, blinded: bool) -> list:
    """Convert a single execution result dict to a CSV row list."""
    ov      = r.get("output_validation") or {}
    ov_errs = ov.get("errors") or []
    ov_warn = ov.get("warnings") or []

    if r.get("_run_status_override"):
        run_status = r["_run_status_override"]
    elif r.get("passed"):
        run_status = "PASSED"
    elif r.get("timed_out"):
        run_status = "TIMED_OUT"
    elif r.get("build_failed"):
        run_status = "BUILD_FAILED"
    elif r.get("passed") is None:
        run_status = "NOT_RUN"
    else:
        run_status = "FAILED"

    row: list = []
    if not blinded:
        row += [r.get("team_name", ""), r.get("contact_email", "")]
    row += [
        r.get("submission_id", ""),
        r.get("source_folder", ""),
        r.get("challenge_type", ""),
        r.get("mode", "reproducible"),
        run_status,
        r.get("exit_code", ""),
        "yes" if r.get("timed_out") else "no",
        "yes" if r.get("build_failed") else "no",
        "yes" if r.get("container_start_failed") else "no",
        r.get("command", ""),
        r.get("output_file_count", len(r.get("output_files") or [])),
        "; ".join(r.get("output_files") or []),
        ov.get("nifti_count", 0),
        "PASSED" if ov.get("passed") else ("N/A" if not ov else "FAILED"),
        " | ".join(_msg(e) for e in ov_errs),
        " | ".join(_msg(w) for w in ov_warn),
        r.get("executed_at") or r.get("finished_at", ""),
    ]
    return row


_EXEC_CSV_HEADER_BLINDED = [
    "submission_id", "source_folder", "challenge_type", "mode",
    "run_status", "exit_code", "timed_out", "build_failed", "container_start_failed",
    "command",
    "generated_file_count", "generated_files",
    "generated_nifti_count", "output_validation_status",
    "output_validation_errors", "output_validation_warnings",
    "executed_at",
]
_EXEC_CSV_HEADER_UNBLINDED = [
    "team_name", "contact_email",
] + _EXEC_CSV_HEADER_BLINDED


@app.get("/api/export-execution")
def export_execution(
    submission_id: str = Query(..., description="submission_id returned by /api/execute"),
    blinded: bool = Query(False, description="True to strip team_name and contact_email"),
):
    """Export the execution result for a single submission as CSV.

    Requires that /api/execute has been called first for the same submission_id.
    Blinded CSV strips team_name and contact_email.
    """
    result = _load_execution_result(submission_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No execution result found for this submission. Run /api/execute first.",
        )

    # Merge in validation metadata if available (for team_name, contact_email)
    val_files = _find_validation_files(submission_id)
    if val_files:
        val = json.loads(val_files[0].read_text(encoding="utf-8"))
        result.setdefault("team_name",     val.get("team_name", ""))
        result.setdefault("contact_email", val.get("contact_email", ""))
        result.setdefault("source_folder", val.get("source_folder", ""))
        result.setdefault("mode",          val.get("mode", "reproducible"))
    result.setdefault("submission_id", submission_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_EXEC_CSV_HEADER_UNBLINDED if not blinded else _EXEC_CSV_HEADER_BLINDED)
    writer.writerow(_exec_result_to_row(result, blinded))

    safe_id = make_safe_id(submission_id)
    suffix  = "blinded" if blinded else "unblinded"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="osipi_execution_{safe_id}_{suffix}.csv"'},
    )


@app.get("/api/export-batch-execution")
def export_batch_execution(
    batch_id: str = Query(..., description="batch_id returned by /api/validate-batch"),
    blinded: bool = Query(False, description="True to strip team_name and contact_email"),
):
    """Export execution results for all submissions in a batch as a single CSV.

    Includes only submissions that have been run via /api/execute.
    Submissions not yet run appear as rows with run_status=NOT_RUN.
    """
    batch = find_batch_result(batch_id)
    if batch is None:
        raise HTTPException(
            status_code=404,
            detail="Batch not found. Run /api/validate-batch first.",
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_EXEC_CSV_HEADER_UNBLINDED if not blinded else _EXEC_CSV_HEADER_BLINDED)

    for r in batch.get("results", []):
        sub_id = r.get("submission_id", "")
        exec_r = _load_execution_result(sub_id)
        if exec_r is None:
            # Submission has not been run yet — include a placeholder row
            placeholder: dict = {
                "submission_id": sub_id,
                "team_name":     r.get("team_name", ""),
                "contact_email": r.get("contact_email", ""),
                "source_folder": r.get("source_folder", ""),
                "challenge_type": r.get("challenge_type", ""),
                "mode":          r.get("mode", "reproducible"),
                "passed": None, "timed_out": False, "build_failed": False,
                "container_start_failed": False,
                "exit_code": "", "command": "", "output_files": [],
                "output_file_count": 0, "output_validation": None,
                "finished_at": "",
            }
            placeholder["_run_status_override"] = "NOT_RUN"
            row = _exec_result_to_row(placeholder, blinded)
            writer.writerow(row)
            continue

        # Merge validation metadata (team/contact info)
        exec_r.setdefault("team_name",     r.get("team_name", ""))
        exec_r.setdefault("contact_email", r.get("contact_email", ""))
        exec_r.setdefault("source_folder", r.get("source_folder", ""))
        exec_r.setdefault("challenge_type", r.get("challenge_type", ""))
        exec_r.setdefault("mode",           r.get("mode", "reproducible"))
        exec_r.setdefault("submission_id",  sub_id)
        writer.writerow(_exec_result_to_row(exec_r, blinded))

    safe_bid = batch_id.replace("/", "_").replace("\\", "_")
    suffix   = "blinded" if blinded else "unblinded"
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="osipi_batch_execution_{safe_bid}_{suffix}.csv"'},
    )


# ===========================================================================
# ── Scoring endpoints ────────────────────────────────────────────────────────
# ===========================================================================

class ScoreRequest(BaseModel):
    submission_id:  str
    provider_id:    Optional[str] = None   # preferred; if set, challenge_type/map_type are ignored
    challenge_type: str = "dce"
    map_type:       str = "Ktrans"


class ScoreBatchRequest(BaseModel):
    submission_ids: List[str]
    provider_id:    Optional[str] = None   # preferred; if set, challenge_type/map_type are ignored
    challenge_type: str = "dce"
    map_type:       str = "Ktrans"
    batch_id:       Optional[str] = None


@app.get("/api/scoring-status")
def get_scoring_status(
    submission_id:  Optional[str] = Query(None),
    provider_id:    Optional[str] = Query(None),
    challenge_type: str           = Query("dce"),
    map_type:       str           = Query("Ktrans"),
    batch_id:       Optional[str] = Query(None),
):
    """Return scoring status for a single submission, a batch, or all providers.

    Calling with no parameters returns the ``providers`` infrastructure snapshot
    for all registered scoring providers — useful for the Score step UI to show
    provider cards without needing a specific submission.

    If ``batch_id`` is provided, returns aggregated status for all submissions
    in that batch.  If ``submission_id`` is provided, returns single-submission
    status.  Both responses also include a ``providers`` key.

    This endpoint NEVER returns fake scores. Missing prerequisites are reported
    via ``status="not_configured"`` and a ``missing`` list.
    """
    if batch_id:
        batch_result = find_batch_result(batch_id)
        if not batch_result:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id!r} not found.")
        submission_ids = [r["submission_id"] for r in (batch_result.get("results") or [])]
        return batch_scoring_status(submission_ids, challenge_type, map_type, provider_id=provider_id)

    if not submission_id:
        # Providers-only request — no submission needed
        return {"providers": all_providers_status()}

    return scoring_status(
        submission_id.strip(),
        challenge_type.strip(),
        map_type.strip(),
        provider_id=provider_id,
    )


@app.post("/api/score")
def score_single(req: ScoreRequest):
    """Run scoring for a single submission.

    Prerequisites: execution must have already produced outputs, the OSIPI
    scoring script must be present, and reference data + mask files must exist.
    Returns status="not_configured" (HTTP 200) if any prerequisite is missing.
    """
    result = score_submission(
        req.submission_id.strip(),
        req.challenge_type.strip() or "dce",
        req.map_type.strip() or "Ktrans",
        provider_id=req.provider_id,
    )
    return result


@app.post("/api/score-batch")
def score_batch_endpoint(req: ScoreBatchRequest):
    """Run scoring for a list of submissions sequentially.

    Each submission is scored independently. Submissions that fail their
    prerequisite check are returned with status="not_configured".
    """
    if not req.submission_ids:
        raise HTTPException(status_code=400, detail="submission_ids is required.")
    results = score_batch(req.submission_ids, req.challenge_type, req.map_type, provider_id=req.provider_id)
    scored  = sum(1 for r in results if r.get("status") == "scored")
    return {
        "batch_id":   req.batch_id,
        "total":      len(results),
        "scored":     scored,
        "results":    results,
    }


@app.get("/api/export-scoring")
def export_scoring(
    submission_id:  Optional[str] = Query(None),
    batch_id:       Optional[str] = Query(None),
    blinded:        bool          = Query(False),
):
    """Export scoring results as CSV.

    Fields (blinded):
        submission_id, provider_id, challenge_type, map_type,
        scoring_status, score_available, metrics_json,
        artifact_count, artifacts, errors, warnings, scored_at

    Unblinded adds: team_name, contact_email

    Returns HTTP 404 if no scoring results are found.
    """
    rows: list[list] = []

    _SCORING_HEADER_BLINDED = [
        "submission_id", "provider_id", "challenge_type", "map_type",
        "scoring_status", "score_available", "metrics_json",
        "artifact_count", "artifacts", "errors", "warnings", "scored_at",
    ]
    _SCORING_HEADER_UNBLINDED = ["team_name", "contact_email"] + _SCORING_HEADER_BLINDED

    def _make_scoring_row(sid: str, r: dict, blind: bool) -> list:
        metrics   = r.get("metrics") or {}
        artifacts = r.get("artifacts") or []
        status    = r.get("status", "")
        errors    = r.get("stderr") or r.get("errors") or []
        warnings  = r.get("warnings") or []

        # Normalize error/warning to pipe-delimited strings
        if isinstance(errors, list):
            errors_str = " | ".join(str(e) for e in errors)
        elif isinstance(errors, str):
            errors_str = errors[:2000]  # truncate raw stderr
        else:
            errors_str = str(errors)

        warnings_str = " | ".join(str(w) for w in warnings) if isinstance(warnings, list) else str(warnings)

        row: list = []
        if not blind:
            val_files = _find_validation_files(sid)
            if val_files:
                try:
                    vd = json.loads(val_files[0].read_text(encoding="utf-8"))
                    row += [vd.get("team_name", ""), vd.get("contact_email", "")]
                except Exception:
                    row += ["", ""]
            else:
                row += ["", ""]

        row += [
            sid,
            r.get("provider_id", ""),
            r.get("challenge_type", ""),
            r.get("map_type", ""),
            status,
            "yes" if (status == "scored" and metrics) else ("partial" if status == "scored" else "no"),
            json.dumps(metrics) if metrics else "",
            len(artifacts),
            " | ".join(str(a) for a in artifacts),
            errors_str,
            warnings_str,
            r.get("scored_at", ""),
        ]
        return row

    def _collect_ids() -> list[str]:
        if batch_id:
            batch_result = find_batch_result(batch_id)
            if not batch_result:
                raise HTTPException(status_code=404, detail=f"Batch {batch_id!r} not found.")
            return [r["submission_id"] for r in (batch_result.get("results") or [])]
        if submission_id:
            return [submission_id.strip()]
        raise HTTPException(status_code=400, detail="submission_id or batch_id is required.")

    sids = _collect_ids()
    for sid in sids:
        r = load_scoring_result(sid)
        if r:
            rows.append(_make_scoring_row(sid, r, blinded))

    if not rows:
        raise HTTPException(
            status_code=404,
            detail="No scoring results found. Run /api/score or /api/score-batch first.",
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_SCORING_HEADER_UNBLINDED if not blinded else _SCORING_HEADER_BLINDED)
    writer.writerows(rows)

    suffix = "blinded" if blinded else "unblinded"
    tag    = batch_id or (submission_id or "export")
    fname  = f"osipi_scoring_{tag}_{suffix}.csv".replace("/", "_")
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )
