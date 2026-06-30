"""FastAPI backend for the OSIPI perfusion pipeline web interface."""

import csv
import html
import io
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime, timezone
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
    SCORING_ACTIVE_CONFIG,
    SCORING_DIR,
    SCORING_OUTPUTS_DIR,
    SCORING_PACKAGES_DIR,
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
from services.scoring_package_service import (
    get_active_entry,
    install_package,
    list_packages,
    load_active_config,
    remove_package,
    set_active_entry,
)
from services.nifti_preview_service import (
    get_preview_download_path,
    get_preview_item,
    get_preview_png_path,
    list_submission_previews,
    public_preview_item,
    public_preview_manifest,
)
from scoring import (
    all_providers_status,
    analyze_submission_niftis,
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
        SCORING_PACKAGES_DIR,
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

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        file_refs: List[tuple] = []
        total_bytes = 0
        for f in files:
            tmp_file = tmp_dir / str(len(file_refs))
            with tmp_file.open("wb") as fp:
                while True:
                    chunk = await f.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > EXTRACT_MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Folder upload exceeds size limit ({EXTRACT_MAX_BYTES // (1024 ** 3)} GB).",
                        )
                    fp.write(chunk)
            file_refs.append((f.filename, tmp_file))

        result = save_uploaded_folder(file_refs)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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

    tmp_dir = Path(tempfile.mkdtemp())
    try:
        file_refs: List[tuple] = []
        total_bytes = 0
        for f in files:
            tmp_file = tmp_dir / str(len(file_refs))
            with tmp_file.open("wb") as fp:
                while True:
                    chunk = await f.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > EXTRACT_MAX_BYTES:
                        raise HTTPException(
                            status_code=413,
                            detail=f"Folder upload exceeds size limit ({EXTRACT_MAX_BYTES // (1024 ** 3)} GB).",
                        )
                    fp.write(chunk)
            file_refs.append((f.filename, tmp_file))

        result = save_folder_as_batch(file_refs)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

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


@app.get("/api/submissions/{submission_id}/previews")
def list_submission_preview_manifest(
    submission_id: str,
    challenge_type: Optional[str] = Query(None),
):
    """Return cached preview metadata for submitted/result NIfTI maps only."""
    manifest = list_submission_previews(submission_id, challenge_type=challenge_type)
    return public_preview_manifest(manifest)


@app.get("/api/submissions/{submission_id}/previews/{map_id}/{plane}.png")
def serve_submission_preview_png(submission_id: str, map_id: str, plane: str):
    """Serve a cached PNG preview slice for a submitted/result map."""
    try:
        path = get_preview_png_path(submission_id, map_id, plane)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return FileResponse(
        str(path),
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@app.get("/api/submissions/{submission_id}/maps/{map_id}/download")
def download_submission_preview_map(submission_id: str, map_id: str):
    """Download the original submitted/result NIfTI map used for preview."""
    try:
        path = get_preview_download_path(submission_id, map_id)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    media_type = "application/gzip" if path.name.lower().endswith(".gz") else "application/octet-stream"
    return FileResponse(
        str(path),
        media_type=media_type,
        filename=path.name,
        headers={"Cache-Control": "no-store"},
    )


def _preview_page_html(submission_id: str, item: dict) -> str:
    public = public_preview_item(item)
    title = html.escape(public.get("file_name") or "NIfTI preview")
    map_type = html.escape(public.get("detected_map_type") or "Unknown")
    shape = html.escape(" x ".join(str(v) for v in public.get("shape") or []) or "not available")
    voxel = html.escape(", ".join(str(v) for v in public.get("voxel_size") or []) or "not available")
    dtype = html.escape(str(public.get("dtype") or "not available"))
    mean = html.escape(str(public.get("mean") if public.get("mean") is not None else "not available"))
    std = html.escape(str(public.get("std") if public.get("std") is not None else "not available"))
    finite = html.escape(str(public.get("finite_percent") if public.get("finite_percent") is not None else "not available"))
    negative = html.escape(str(public.get("negative_percent") if public.get("negative_percent") is not None else "not available"))
    error = html.escape(public.get("preview_error") or "")
    download_url = html.escape(public.get("download_url") or "#")
    planes = [
        ("Axial", public.get("axial_url")),
        ("Coronal", public.get("coronal_url")),
        ("Sagittal", public.get("sagittal_url")),
    ]
    plane_buttons = "".join(
        f'<button type="button" data-plane="{html.escape(url)}" class="{ "is-active" if i == 0 else "" }">{label}</button>'
        for i, (label, url) in enumerate(planes)
        if url
    )
    first_image = next((url for _, url in planes if url), "")
    preview_body = (
        f'<img id="preview-image" src="{html.escape(first_image)}" alt="Preview slice for {title}">'
        if first_image else
        f'<div class="empty-preview">Preview unavailable{": " + error if error else ""}</div>'
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title} preview</title>
  <style>
    :root {{ color-scheme: light; --purple:#533a9d; --border:#d8dde8; --text:#20232d; --muted:#687083; }}
    body {{ margin:0; font-family: Inter, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background:#f4f5f8; color:var(--text); }}
    main {{ max-width:1040px; margin:0 auto; padding:28px 18px 40px; }}
    .page {{ background:#fff; border:1px solid var(--border); border-radius:14px; overflow:hidden; box-shadow:0 12px 34px rgba(20,24,36,.08); }}
    header {{ display:flex; justify-content:space-between; gap:16px; padding:18px 20px; border-top:5px solid var(--purple); border-bottom:1px solid var(--border); }}
    h1 {{ margin:0; font-size:1.1rem; line-height:1.3; overflow-wrap:anywhere; }}
    .sub {{ margin-top:5px; color:var(--muted); font-size:.86rem; }}
    .status {{ align-self:flex-start; border:1px solid var(--border); border-radius:999px; padding:5px 10px; color:var(--purple); font-weight:800; font-size:.74rem; white-space:nowrap; }}
    .content {{ display:grid; grid-template-columns:minmax(0, 1.5fr) minmax(260px, .75fr); gap:0; }}
    .viewer {{ padding:18px; border-right:1px solid var(--border); }}
    .tabs {{ display:flex; flex-wrap:wrap; gap:8px; margin-bottom:12px; }}
    .tabs button {{ border:1px solid var(--border); background:#fff; color:var(--text); border-radius:8px; padding:7px 12px; font-weight:750; cursor:pointer; }}
    .tabs button.is-active {{ border-color:var(--purple); color:var(--purple); background:#f1edff; }}
    .image-wrap {{ min-height:420px; display:grid; place-items:center; background:#10131a; border-radius:10px; overflow:hidden; }}
    img {{ max-width:100%; max-height:74vh; image-rendering:auto; }}
    .empty-preview {{ color:#fff; padding:28px; text-align:center; }}
    aside {{ padding:18px 20px; }}
    dl {{ display:grid; grid-template-columns:110px minmax(0,1fr); gap:9px 12px; margin:0; font-size:.84rem; }}
    dt {{ color:var(--muted); }}
    dd {{ margin:0; font-weight:750; overflow-wrap:anywhere; }}
    .actions {{ display:flex; flex-wrap:wrap; gap:10px; margin-top:18px; }}
    a.button {{ text-decoration:none; border-radius:9px; padding:9px 12px; font-weight:800; font-size:.82rem; }}
    .primary {{ background:var(--purple); color:#fff; }}
    .secondary {{ border:1px solid var(--border); color:var(--text); background:#fff; }}
    .note {{ margin-top:18px; color:var(--muted); font-size:.8rem; line-height:1.5; }}
    @media (max-width: 820px) {{ .content {{ grid-template-columns:1fr; }} .viewer {{ border-right:0; border-bottom:1px solid var(--border); }} header {{ flex-direction:column; }} }}
  </style>
</head>
<body>
  <main>
    <section class="page">
      <header>
        <div>
          <h1>{title}</h1>
          <div class="sub">{map_type} · submission {html.escape(make_safe_id(submission_id))}</div>
        </div>
        <div class="status">{'Preview available' if public.get('preview_available') else 'Preview unavailable'}</div>
      </header>
      <div class="content">
        <div class="viewer">
          <div class="tabs">{plane_buttons}</div>
          <div class="image-wrap">{preview_body}</div>
        </div>
        <aside>
          <dl>
            <dt>Map type</dt><dd>{map_type}</dd>
            <dt>Shape</dt><dd>{shape}</dd>
            <dt>Voxel size</dt><dd>{voxel}</dd>
            <dt>Data type</dt><dd>{dtype}</dd>
            <dt>Mean</dt><dd>{mean}</dd>
            <dt>Std.</dt><dd>{std}</dd>
            <dt>Finite %</dt><dd>{finite}</dd>
            <dt>Negative %</dt><dd>{negative}</dd>
          </dl>
          <div class="actions">
            <a class="button primary" href="{download_url}">Download NIfTI for ITK-SNAP</a>
            <a class="button secondary" href="/static/index.html#summary">Back to app</a>
          </div>
          <p class="note">For full medical image inspection, download the NIfTI file and open it in ITK-SNAP, FSLeyes, 3D Slicer, or another NIfTI viewer.</p>
        </aside>
      </div>
    </section>
  </main>
  <script>
    const buttons = document.querySelectorAll('[data-plane]');
    const img = document.getElementById('preview-image');
    buttons.forEach((button) => button.addEventListener('click', () => {{
      buttons.forEach((b) => b.classList.remove('is-active'));
      button.classList.add('is-active');
      if (img) img.src = button.dataset.plane;
    }}));
  </script>
</body>
</html>"""


@app.get("/preview/{submission_id}/{map_id}")
def full_preview_page(submission_id: str, map_id: str):
    """Open a view-only full preview page for a submitted/result map."""
    item = get_preview_item(submission_id, map_id)
    if item is None:
        list_submission_previews(submission_id, challenge_type=None)
        item = get_preview_item(submission_id, map_id)
    if item is None:
        raise HTTPException(status_code=404, detail="Preview map not found.")
    return Response(content=_preview_page_html(submission_id, item), media_type="text/html")


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


class ScoringStatusRequest(BaseModel):
    submission_id:  Optional[str] = None
    provider_id:    Optional[str] = None
    challenge_type: str           = "dce"
    map_type:       str           = "Ktrans"
    batch_id:       Optional[str] = None


@app.post("/api/scoring-status")
def post_scoring_status(req: ScoringStatusRequest):
    """POST variant of :func:`get_scoring_status` accepting a JSON body.

    Convenience for clients that prefer POST + JSON over query parameters.
    Delegates to the exact same logic so behaviour is identical.
    """
    return get_scoring_status(
        submission_id=req.submission_id,
        provider_id=req.provider_id,
        challenge_type=req.challenge_type,
        map_type=req.map_type,
        batch_id=req.batch_id,
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


@app.post("/api/score-single")
def score_single_legacy(req: ScoreRequest):
    """Backwards-compatible alias for ``POST /api/score``.

    Older clients/tests call ``/api/score-single``; it delegates to the same
    single-submission scoring logic so behaviour is identical.
    """
    return score_single(req)


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


@app.get("/api/leaderboard")
def get_leaderboard():
    """Return a list of all scored submissions (simple summary — no ranking).

    Reads every ``*_score.json`` file from the scoring outputs directory and
    returns them sorted by ``scored_at`` descending (most recent first).
    """
    from services.path_config import SCORING_OUTPUTS_DIR
    entries = []
    if SCORING_OUTPUTS_DIR.exists():
        for p in sorted(SCORING_OUTPUTS_DIR.glob("*_score.json")):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                entries.append({
                    "submission_id": data.get("submission_id", p.stem.replace("_score", "")),
                    "provider_id":   data.get("provider_id"),
                    "status":        data.get("status", "unknown"),
                    "scored_at":     data.get("scored_at"),
                    "metrics":       data.get("metrics") or {},
                    "artifact_count": data.get("artifact_count", 0),
                    "message":       data.get("message", ""),
                })
            except Exception:
                continue
    # Sort most-recent first
    entries.sort(key=lambda e: e.get("scored_at") or "", reverse=True)
    return {"count": len(entries), "entries": entries}


# ---------------------------------------------------------------------------
# Scoring Package Management — admin/reviewer endpoints
# ---------------------------------------------------------------------------


@app.get("/api/scoring/packages")
def scoring_packages_list():
    """List all installed scoring packages with their manifests and readiness.

    Returns a JSON array of package objects (each with ``package_id``). The
    array shape is the stable, backwards-compatible contract clients/tests rely
    on; the frontend handles both an array and a legacy ``{packages: [...]}``.
    """
    return list_packages()


@app.post("/api/scoring/packages/upload")
async def scoring_package_upload(file: UploadFile = File(...)):
    """Upload and install a scoring package ZIP.

    The ZIP must contain manifest.json and a scoring entry-point script.
    See backend/services/scoring_package_service.py for the manifest schema.
    Returns the installed package manifest on success.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Only .zip files are accepted.")

    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    tmp_path = Path(tmp_name)
    try:
        total_bytes = 0
        with os.fdopen(tmp_fd, "wb") as fout:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total_bytes += len(chunk)
                if total_bytes > 500 * 1024 * 1024:  # 500 MB limit
                    raise HTTPException(status_code=413, detail="Scoring package ZIP exceeds 500 MB limit.")
                fout.write(chunk)

        result = install_package(tmp_path)
        if not result.get("success"):
            raise HTTPException(status_code=400, detail=result.get("error", "Package installation failed."))
        return result
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


@app.delete("/api/scoring/packages/{package_id}")
def scoring_package_remove(package_id: str):
    """Remove an installed scoring package.

    Also clears any active-config entries pointing to this package.
    """
    if not package_id or not package_id.replace("-", "").replace("_", "").isalnum():
        raise HTTPException(status_code=400, detail="Invalid package_id.")
    result = remove_package(package_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Package not found."))
    return result


@app.get("/api/scoring/active-config")
def scoring_active_config():
    """Return the current active scoring configuration for all challenge types.

    Exposes the config under both ``active`` (stable/expected key) and
    ``active_config`` (legacy key the frontend reads) so older and newer
    clients keep working.
    """
    cfg = load_active_config()
    return {"active": cfg, "active_config": cfg, "packages": list_packages()}


class ScoringSetActiveRequest(BaseModel):
    challenge_type: str          # "dce" | "asl" | "dsc"
    mode: str                    # "none" | "builtin" | "custom"
    package_id: Optional[str] = None  # required when mode="custom"


@app.post("/api/scoring/set-active")
def scoring_set_active(req: ScoringSetActiveRequest):
    """Set the active scoring mode for a challenge type.

    mode="none"    — scoring disabled; app shows "Scoring not configured"
    mode="builtin" — use the built-in OSIPI TF6.2 provider
    mode="custom"  — use an uploaded package (package_id required)
    """
    ct = req.challenge_type.strip().lower()
    if ct not in ("dce", "asl", "dsc"):
        raise HTTPException(status_code=400, detail="challenge_type must be 'dce', 'asl', or 'dsc'.")
    try:
        entry = set_active_entry(ct, req.mode, req.package_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "challenge_type": ct, "active": entry}


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
        reference_based_scoring_available, reference_scoring_status,
        reference_map_count, reference_compared_map_count,
        reference_mean_rmse, reference_mean_mae, reference_mean_bias,
        reference_mean_coefficient_of_variation, reference_metrics_json,
        overall_qc_summary_json, per_map_metadata_json, per_map_stats_json,
        artifact_count, artifacts, errors, warnings, scored_at

    Unblinded adds: team_name, contact_email

    Returns HTTP 404 if no scoring results are found.
    """
    rows: list[list] = []

    _SCORING_HEADER_BLINDED = [
        "submission_id", "provider_id", "challenge_type", "map_type",
        "scoring_status", "score_available", "metrics_json",
        "reference_based_scoring_available", "reference_scoring_status",
        "reference_map_count", "reference_compared_map_count",
        "reference_mean_rmse", "reference_mean_mae", "reference_mean_bias",
        "reference_mean_coefficient_of_variation", "reference_metrics_json",
        "overall_qc_summary_json", "per_map_metadata_json", "per_map_stats_json",
        "artifact_count", "artifacts", "errors", "warnings", "scored_at",
    ]
    _SCORING_HEADER_UNBLINDED = ["team_name", "contact_email"] + _SCORING_HEADER_BLINDED

    def _make_scoring_row(sid: str, r: dict, blind: bool) -> list:
        metrics   = r.get("metrics") or {}
        artifacts = r.get("artifacts") or []
        status    = r.get("status", "")
        errors    = r.get("stderr") or r.get("errors") or []
        warnings  = r.get("warnings") or []
        analysis = _analysis_for_summary(sid, r.get("challenge_type", ""), r)
        analysis_fields = _analysis_summary_fields(analysis)

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
            "yes" if analysis_fields.get("reference_based_scoring_available") else "no",
            analysis_fields.get("reference_scoring_status", ""),
            analysis_fields.get("reference_map_count", 0),
            analysis_fields.get("reference_compared_map_count", 0),
            analysis_fields.get("reference_mean_rmse"),
            analysis_fields.get("reference_mean_mae"),
            analysis_fields.get("reference_mean_bias"),
            analysis_fields.get("reference_mean_coefficient_of_variation"),
            json.dumps(analysis_fields.get("reference_metric_rows") or []),
            json.dumps(analysis_fields.get("overall_qc_summary") or {}),
            json.dumps(analysis_fields.get("per_map_metadata") or []),
            json.dumps(analysis_fields.get("per_map_stats") or []),
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


# ---------------------------------------------------------------------------
# Combined summary export + HTML report
# ---------------------------------------------------------------------------

def _collect_export_ids(batch_id: Optional[str], submission_id: Optional[str]) -> List[str]:
    """Resolve a batch_id or submission_id to a list of submission IDs."""
    if batch_id:
        batch_result = find_batch_result(batch_id)
        if not batch_result:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id!r} not found.")
        return [r["submission_id"] for r in (batch_result.get("results") or [])]
    if submission_id:
        return [submission_id.strip()]
    raise HTTPException(status_code=400, detail="submission_id or batch_id is required.")


def _load_validation(sid: str) -> Optional[dict]:
    files = _find_validation_files(sid)
    if not files:
        return None
    try:
        return json.loads(files[0].read_text(encoding="utf-8"))
    except Exception:
        return None


def _execution_status_label(val: Optional[dict], execr: Optional[dict]) -> str:
    """Human-friendly execution status that respects the result-only skip rule."""
    if execr is not None:
        if execr.get("passed"):
            return "passed"
        if execr.get("timed_out"):
            return "timed_out"
        if execr.get("build_failed"):
            return "build_failed"
        if execr.get("passed") is None:
            return "not_run"
        return "failed"
    # No execution result on disk — infer from validation run-readiness
    readiness = (val or {}).get("run_readiness", "")
    if readiness == "result_only":
        return "skipped_result_maps"
    if readiness == "runnable":
        return "not_run"
    if readiness == "not_runnable":
        return "cannot_run"
    return "not_run"


_FRIENDLY_METRIC_LABELS = {
    "accuracy": "Accuracy",
    "repeatability": "Repeatability",
    "reproducibility": "Reproducibility",
    "osipi_silver_score": "Silver Score",
    "osipi_gold_score": "Gold Score",
    "mean_finite_percent": "Finite voxels",
    "finite_percent": "Finite voxels",
    "mean_coefficient_of_variation": "Coefficient of variation",
    "coefficient_of_variation": "Coefficient of variation",
    "negative_voxel_percent": "Negative voxels",
    "mean_negative_voxel_percent": "Negative voxels",
    "mean_standard_deviation": "Standard deviation",
    "rmse": "RMSE",
    "mean_rmse": "Mean RMSE",
    "bias": "Bias",
    "mean_bias": "Mean bias",
    "mae": "MAE",
    "mean_mae": "Mean MAE",
}


def _friendly_metric_label(key: str) -> str:
    if key in _FRIENDLY_METRIC_LABELS:
        return _FRIENDLY_METRIC_LABELS[key]
    return str(key).replace("_", " ").title()


def _fmt_report_num(value, digits: int = 3) -> str:
    if value is None:
        return "not available"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float):
            return (f"{value:.{digits}f}").rstrip("0").rstrip(".")
        return str(value)
    return str(value)


def _analysis_for_summary(sid: str, challenge_type: str, score: Optional[dict]) -> dict:
    analysis = (score or {}).get("nifti_analysis")
    if isinstance(analysis, dict):
        return analysis
    return analyze_submission_niftis(sid, challenge_type or (score or {}).get("challenge_type", ""))


def _analysis_summary_fields(analysis: dict) -> dict:
    summary = analysis.get("summary") if isinstance(analysis, dict) else {}
    summary = summary if isinstance(summary, dict) else {}
    reference_scoring = analysis.get("reference_scoring") if isinstance(analysis, dict) else {}
    reference_scoring = reference_scoring if isinstance(reference_scoring, dict) else {}
    reference_summary = reference_scoring.get("summary") if isinstance(reference_scoring, dict) else {}
    reference_summary = reference_summary if isinstance(reference_summary, dict) else {}
    maps = analysis.get("maps") if isinstance(analysis, dict) else []
    maps = maps if isinstance(maps, list) else []
    per_map_metadata = []
    per_map_stats = []
    for item in maps:
        if not isinstance(item, dict):
            continue
        per_map_metadata.append({
            "file_name": item.get("file_name", ""),
            "detected_map_type": item.get("detected_map_type", ""),
            "parameter_label": item.get("parameter_label", ""),
            "units": item.get("units") or "units not provided",
            **(item.get("metadata") or {}),
        })
        per_map_stats.append({
            "file_name": item.get("file_name", ""),
            "detected_map_type": item.get("detected_map_type", ""),
            **(item.get("stats") or {}),
        })
    reference_rows = []
    for item in reference_scoring.get("maps") or []:
        if not isinstance(item, dict):
            continue
        whole = item.get("whole_map") if isinstance(item.get("whole_map"), dict) else {}
        reference_rows.append({
            "submitted_file": item.get("submitted_file", ""),
            "reference_file": item.get("reference_file", ""),
            "detected_map_type": item.get("detected_map_type", ""),
            "scope": "whole map",
            "mask_name": "",
            "status": item.get("status", ""),
            "rmse": whole.get("rmse"),
            "mae": whole.get("mae"),
            "bias": whole.get("bias"),
            "coefficient_of_variation": whole.get("coefficient_of_variation"),
            "correlation": whole.get("correlation"),
            "voxel_count": whole.get("voxel_count"),
            "finite_voxel_percent": whole.get("finite_voxel_percent"),
            "difference_map": item.get("difference_map"),
        })
        for mask in item.get("masks") or []:
            if not isinstance(mask, dict):
                continue
            metrics = mask.get("metrics") if isinstance(mask.get("metrics"), dict) else {}
            reference_rows.append({
                "submitted_file": item.get("submitted_file", ""),
                "reference_file": item.get("reference_file", ""),
                "detected_map_type": item.get("detected_map_type", ""),
                "scope": mask.get("mask_label", "mask"),
                "mask_name": mask.get("mask_name", ""),
                "status": mask.get("status", ""),
                "rmse": metrics.get("rmse"),
                "mae": metrics.get("mae"),
                "bias": metrics.get("bias"),
                "coefficient_of_variation": metrics.get("coefficient_of_variation"),
                "correlation": metrics.get("correlation"),
                "voxel_count": metrics.get("voxel_count"),
                "finite_voxel_percent": metrics.get("finite_voxel_percent"),
                "difference_map": item.get("difference_map"),
            })
    return {
        "map_count": summary.get("map_count", 0),
        "parameter_maps_detected": ", ".join(summary.get("parameter_maps_detected") or []),
        "finite_voxels_percent": summary.get("finite_percent"),
        "negative_voxels_percent": summary.get("negative_voxel_percent"),
        "mean_coefficient_of_variation": summary.get("mean_coefficient_of_variation"),
        "mean_standard_deviation": summary.get("mean_standard_deviation"),
        "total_voxel_count": summary.get("total_voxel_count", 0),
        "finite_voxel_count": summary.get("finite_voxel_count", 0),
        "nan_count": summary.get("nan_count", 0),
        "inf_count": summary.get("inf_count", 0),
        "overall_qc_summary": summary,
        "per_map_metadata": per_map_metadata,
        "per_map_stats": per_map_stats,
        "reference_based_scoring_available": bool(analysis.get("reference_based_scoring_available")),
        "reference_scoring_status": reference_scoring.get("status", "reference_not_available"),
        "reference_map_count": reference_summary.get("reference_map_count", 0),
        "reference_compared_map_count": reference_summary.get("compared_map_count", 0),
        "reference_mean_rmse": reference_summary.get("mean_rmse"),
        "reference_mean_mae": reference_summary.get("mean_mae"),
        "reference_mean_bias": reference_summary.get("mean_bias"),
        "reference_mean_coefficient_of_variation": reference_summary.get("mean_coefficient_of_variation"),
        "reference_scoring": reference_scoring,
        "reference_metric_rows": reference_rows,
    }


def _gather_summary(sid: str) -> dict:
    """Collect validation + execution + scoring info for a single submission."""
    val   = _load_validation(sid)
    execr = _load_execution_result(sid)
    score = load_scoring_result(sid)
    challenge_type = (val or {}).get("challenge_type", "") or (score or {}).get("challenge_type", "")
    analysis = _analysis_for_summary(sid, challenge_type, score)
    analysis_fields = _analysis_summary_fields(analysis)
    metrics = (score or {}).get("metrics") or {}
    numeric_metrics = {
        k: v for k, v in metrics.items()
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    }
    return {
        "submission_id":   sid,
        "team_name":       (val or {}).get("team_name", ""),
        "contact_email":   (val or {}).get("contact_email", ""),
        "source_folder":   (val or {}).get("source_folder", "") or (val or {}).get("submission_id", sid),
        "challenge_type":  challenge_type,
        "mode":            (val or {}).get("mode", ""),
        "val_passed":      bool((val or {}).get("passed")) if val else None,
        "error_count":     (val or {}).get("error_count", 0) if val else 0,
        "warning_count":   (val or {}).get("warning_count", 0) if val else 0,
        "nifti_count":     (val or {}).get("nifti_count", 0) if val else 0,
        "run_readiness":   (val or {}).get("run_readiness", ""),
        "exec_status":     _execution_status_label(val, execr),
        "generated_files": (execr or {}).get("output_file_count",
                            len((execr or {}).get("output_files") or [])) if execr else 0,
        "scoring_status":  (score or {}).get("status", "not_scored"),
        "scoring_official": bool((score or {}).get("official", False)),
        "scored_at":       (score or {}).get("scored_at", ""),
        "numeric_metrics": numeric_metrics,
        "nifti_analysis":  analysis,
        "analysis_fields": analysis_fields,
        "has_validation":  val is not None,
        "has_scoring":     score is not None,
    }


_COMBINED_HEADER_BLINDED = [
    "submission_id", "source_folder", "challenge_type", "mode",
    "validation_passed", "error_count", "warning_count", "nifti_count",
    "run_readiness", "execution_status", "generated_file_count",
    "scoring_status", "official_scoring", "metrics_json", "scored_at",
    "reference_based_scoring_available", "map_count", "parameter_maps_detected",
    "finite_voxels_percent", "negative_voxels_percent",
    "mean_coefficient_of_variation", "mean_standard_deviation",
    "total_voxel_count", "finite_voxel_count", "nan_count", "inf_count",
    "reference_scoring_status", "reference_map_count", "reference_compared_map_count",
    "reference_mean_rmse", "reference_mean_mae", "reference_mean_bias",
    "reference_mean_coefficient_of_variation", "reference_metrics_json",
    "overall_qc_summary_json", "per_map_metadata_json", "per_map_stats_json",
]
_COMBINED_HEADER_UNBLINDED = ["team_name", "contact_email"] + _COMBINED_HEADER_BLINDED


@app.get("/api/export-combined")
def export_combined(
    submission_id: Optional[str] = Query(None),
    batch_id:      Optional[str] = Query(None),
    blinded:       bool          = Query(False, description="True to strip team_name and contact_email"),
):
    """Export a single combined summary CSV: one row per submission containing
    validation, execution, and scoring status together.

    Blinded export omits team_name and contact_email.
    """
    sids = _collect_export_ids(batch_id, submission_id)

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(_COMBINED_HEADER_UNBLINDED if not blinded else _COMBINED_HEADER_BLINDED)

    wrote_any = False
    for sid in sids:
        s = _gather_summary(sid)
        af = s["analysis_fields"]
        if not s["has_validation"] and not s["has_scoring"] and s["exec_status"] == "not_run":
            # Nothing recorded for this submission yet — still emit a row so the
            # combined export is complete, but mark it clearly.
            pass
        wrote_any = True
        row: list = []
        if not blinded:
            row += [s["team_name"], s["contact_email"]]
        row += [
            s["submission_id"], s["source_folder"], s["challenge_type"], s["mode"],
            "" if s["val_passed"] is None else ("yes" if s["val_passed"] else "no"),
            s["error_count"], s["warning_count"], s["nifti_count"],
            s["run_readiness"], s["exec_status"], s["generated_files"],
            s["scoring_status"], "yes" if s["scoring_official"] else "no",
            json.dumps(s["numeric_metrics"]) if s["numeric_metrics"] else "",
            s["scored_at"],
            "yes" if af["reference_based_scoring_available"] else "no",
            af["map_count"], af["parameter_maps_detected"],
            af["finite_voxels_percent"], af["negative_voxels_percent"],
            af["mean_coefficient_of_variation"], af["mean_standard_deviation"],
            af["total_voxel_count"], af["finite_voxel_count"], af["nan_count"], af["inf_count"],
            af["reference_scoring_status"], af["reference_map_count"], af["reference_compared_map_count"],
            af["reference_mean_rmse"], af["reference_mean_mae"], af["reference_mean_bias"],
            af["reference_mean_coefficient_of_variation"],
            json.dumps(af["reference_metric_rows"], default=str),
            json.dumps(af["overall_qc_summary"], default=str),
            json.dumps(af["per_map_metadata"], default=str),
            json.dumps(af["per_map_stats"], default=str),
        ]
        writer.writerow(row)

    if not wrote_any:
        raise HTTPException(status_code=404, detail="No submissions found to export.")

    suffix = "blinded" if blinded else "unblinded"
    tag    = (batch_id or submission_id or "export").replace("/", "_")
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="osipi_combined_{tag}_{suffix}.csv"'},
    )


def _esc(text) -> str:
    """Minimal HTML escaping for report generation."""
    return (
        str(text if text is not None else "")
        .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


@app.get("/api/report")
def export_report(
    submission_id: Optional[str] = Query(None),
    batch_id:      Optional[str] = Query(None),
    blinded:       bool          = Query(True, description="True (default) to strip team/contact info"),
):
    """Generate a self-contained HTML evaluation report.

    Includes validation, execution, and scoring/QC summaries plus a metric
    table.  Clearly states when official OSIPI scoring is not configured.
    Blinded by default so the report is safe to share with reviewers.
    """
    sids = _collect_export_ids(batch_id, submission_id)
    summaries = [_gather_summary(sid) for sid in sids]

    active_cfg = load_active_config()
    summary_challenges = {
        str(s.get("challenge_type", "")).strip().lower()
        for s in summaries
        if s.get("challenge_type")
    }
    any_official = any(
        s["scoring_official"] for s in summaries
    ) or any(
        (active_cfg.get(ct) or {}).get("mode") == "builtin"
        for ct in summary_challenges
    )

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    # ── Aggregate counts ──────────────────────────────────────────────────────
    n          = len(summaries)
    n_passed   = sum(1 for s in summaries if s["val_passed"])
    n_failed   = sum(1 for s in summaries if s["val_passed"] is False)
    n_scored   = sum(1 for s in summaries if s["scoring_status"] == "scored")
    n_skipped  = sum(1 for s in summaries if s["exec_status"] == "skipped_result_maps")

    # ── Scoring note (professional, no demo language) ──────────────────────────
    # The truthful "reference package not installed" clarification is moved to a
    # muted footnote at the bottom of the report, not the prominent body note.
    scoring_footnote = ""
    if any_official:
        scoring_note = ('<div class="note note-ok"><strong>Scoring results.</strong> '
                        'Official OSIPI scoring is configured; reference-based metrics are shown '
                        'where an actual scoring result provides them.</div>')
    elif n_scored > 0:
        scoring_note = ('<div class="note note-ok"><strong>Scoring results.</strong> '
                        'Quality-control metrics and NIfTI map statistics generated successfully.</div>')
        scoring_footnote = ('<p class="footnote">Reference scoring package not installed. '
                            'QC metrics are shown from the active scorer.</p>')
    else:
        scoring_note = ('<div class="note note-gray">Scoring is not configured. '
                        'This report covers validation, execution, and NIfTI map QC/statistics.</div>')

    # ── Per-submission rows ───────────────────────────────────────────────────
    rows_html = []
    technical_html = []
    for s in summaries:
        af = s["analysis_fields"]
        analysis = s["nifti_analysis"] if isinstance(s.get("nifti_analysis"), dict) else {}
        summary = analysis.get("summary") if isinstance(analysis, dict) else {}
        summary = summary if isinstance(summary, dict) else {}
        visible_metrics = summary.get("visible_metrics") or []
        visible_by_label = {
            str(item.get("label")): item.get("value")
            for item in visible_metrics
            if isinstance(item, dict)
        }
        finite = visible_by_label.get("Finite voxels") or _fmt_report_num(af["finite_voxels_percent"])
        negative = visible_by_label.get("Negative voxels") or _fmt_report_num(af["negative_voxels_percent"])
        cov = visible_by_label.get("Coefficient of variation") or _fmt_report_num(af["mean_coefficient_of_variation"])
        mean_values = [
            f"{_esc(item.get('label'))}: {_esc(item.get('value'))}"
            for item in visible_metrics
            if isinstance(item, dict) and str(item.get("label", "")).startswith("Mean ")
        ]
        if af["reference_based_scoring_available"]:
            mean_values = [
                f"RMSE: {_esc(_fmt_report_num(af['reference_mean_rmse']))}",
                f"MAE: {_esc(_fmt_report_num(af['reference_mean_mae']))}",
                f"Bias: {_esc(_fmt_report_num(af['reference_mean_bias']))}",
                f"Ref CoV: {_esc(_fmt_report_num(af['reference_mean_coefficient_of_variation']))}",
            ]
        elif not mean_values:
            mean_values = [f"Std. deviation: {_esc(_fmt_report_num(af['mean_standard_deviation']))}"]
        stats_cell = "<br>".join(mean_values) if mean_values else "—"
        val_badge = ("—" if s["val_passed"] is None
                     else ('<span class="b b-ok">passed</span>' if s["val_passed"]
                           else '<span class="b b-err">failed</span>'))
        team_cell = "" if blinded else f"<td>{_esc(s['team_name'])}</td>"
        detected = af["parameter_maps_detected"] or "None detected"
        rows_html.append(
            "<tr>"
            f"<td>{_esc(s['source_folder'])}</td>"
            f"{team_cell}"
            f"<td>{_esc(s['challenge_type'])}</td>"
            f"<td>{val_badge}</td>"
            f"<td>{_esc(s['exec_status'].replace('_', ' '))}</td>"
            f"<td>{_esc(s['scoring_status'].replace('_', ' '))}</td>"
            f"<td>{_esc(af['map_count'])}</td>"
            f"<td>{_esc(detected)}</td>"
            f"<td>Finite voxels: {_esc(finite)}<br>Negative voxels: {_esc(negative)}</td>"
            f"<td>Coefficient of variation: {_esc(cov)}<br>{stats_cell}</td>"
            "</tr>"
        )

        maps = analysis.get("maps") if isinstance(analysis, dict) else []
        maps = maps if isinstance(maps, list) else []
        map_rows = []
        for item in maps:
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            stats = item.get("stats") or {}
            units = item.get("units") or "units not provided"
            shape = " x ".join(str(v) for v in (meta.get("shape") or [])) or "—"
            voxel = ", ".join(str(v) for v in (meta.get("voxel_size") or [])) or "—"
            voxels = f"{meta.get('finite_voxel_count', 0)} / {meta.get('total_voxel_count', 0)}"
            map_rows.append(
                "<tr>"
                f"<td>{_esc(item.get('file_name', ''))}</td>"
                f"<td>{_esc(item.get('detected_map_type', 'Unknown'))}</td>"
                f"<td>{_esc(item.get('parameter_label', ''))}</td>"
                f"<td>{_esc(units)}</td>"
                f"<td>{_esc(shape)}</td>"
                f"<td>{_esc(voxel)}</td>"
                f"<td>{_esc(meta.get('data_type') or '—')}</td>"
                f"<td>{_esc(meta.get('affine_orientation_summary') or '—')}</td>"
                f"<td>{_esc(voxels)}</td>"
                f"<td>{_esc(meta.get('nan_count', 0))}</td>"
                f"<td>{_esc(meta.get('inf_count', 0))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('mean')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('median')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('standard_deviation')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('min')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('max')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('finite_percent')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('negative_voxel_percent')))}</td>"
                f"<td>{_esc(_fmt_report_num(stats.get('coefficient_of_variation')))}</td>"
                "</tr>"
            )
        map_table = (
            "<table class=\"tech-table\"><thead><tr>"
            "<th>File</th><th>Type</th><th>Label</th><th>Units</th><th>Shape</th><th>Voxel size</th>"
            "<th>Data type</th><th>Affine/orientation</th><th>Finite / total voxels</th>"
            "<th>NaN</th><th>Inf</th><th>Mean</th><th>Median</th><th>Std. dev.</th>"
            "<th>Min</th><th>Max</th><th>Finite %</th><th>Negative %</th><th>CoV</th>"
            "</tr></thead><tbody>"
            + ("".join(map_rows) if map_rows else '<tr><td colspan="19">No readable NIfTI maps found.</td></tr>')
            + "</tbody></table>"
        )
        reference_rows = []
        for ref_row in af["reference_metric_rows"]:
            reference_rows.append(
                "<tr>"
                f"<td>{_esc(ref_row.get('submitted_file', ''))}</td>"
                f"<td>{_esc(ref_row.get('reference_file', ''))}</td>"
                f"<td>{_esc(ref_row.get('detected_map_type', ''))}</td>"
                f"<td>{_esc(ref_row.get('scope', ''))}</td>"
                f"<td>{_esc(ref_row.get('mask_name', ''))}</td>"
                f"<td>{_esc(ref_row.get('status', ''))}</td>"
                f"<td>{_esc(_fmt_report_num(ref_row.get('rmse')))}</td>"
                f"<td>{_esc(_fmt_report_num(ref_row.get('mae')))}</td>"
                f"<td>{_esc(_fmt_report_num(ref_row.get('bias')))}</td>"
                f"<td>{_esc(_fmt_report_num(ref_row.get('coefficient_of_variation')))}</td>"
                f"<td>{_esc(_fmt_report_num(ref_row.get('correlation')))}</td>"
                f"<td>{_esc(_fmt_report_num(ref_row.get('finite_voxel_percent')))}</td>"
                f"<td>{_esc(ref_row.get('voxel_count') if ref_row.get('voxel_count') is not None else 'not available')}</td>"
                f"<td>{_esc(ref_row.get('difference_map') or '')}</td>"
                "</tr>"
            )
        reference_table = (
            "<table class=\"tech-table\"><thead><tr>"
            "<th>Submitted</th><th>Reference</th><th>Map</th><th>Scope</th><th>Mask</th><th>Status</th>"
            "<th>RMSE</th><th>MAE</th><th>Bias</th><th>CoV</th><th>Correlation</th>"
            "<th>Finite %</th><th>Voxels</th><th>Difference map</th>"
            "</tr></thead><tbody>"
            + ("".join(reference_rows) if reference_rows else '<tr><td colspan="14">Reference map not available; QC metrics only.</td></tr>')
            + "</tbody></table>"
        )
        raw_metrics = (
            "<ul class=\"raw-list\">" +
            "".join(f"<li><code>{_esc(k)}</code>: {_esc(v)}</li>" for k, v in s["numeric_metrics"].items()) +
            "</ul>"
        ) if s["numeric_metrics"] else '<p class="muted">No raw scoring metric keys were produced.</p>'
        technical_html.append(
            f"<details class=\"report-details\"><summary>View technical details for {_esc(s['source_folder'])}</summary>"
            f"<p class=\"muted\">Reference-based scoring available: {'yes' if af['reference_based_scoring_available'] else 'no'}</p>"
            f"<h3>Reference scoring metrics</h3>{reference_table}"
            f"{map_table}"
            f"<h3>Raw metric keys</h3>{raw_metrics}"
            f"<h3>Overall QC summary</h3><pre>{_esc(json.dumps(af['overall_qc_summary'], indent=2, default=str))}</pre>"
            "</details>"
        )

    team_th = "" if blinded else "<th>Team</th>"
    table_html = (
        "<table><thead><tr>"
        f"<th>Submission</th>{team_th}<th>Challenge</th><th>Validation</th>"
        "<th>Execution</th><th>Scoring</th><th>Maps</th><th>Parameter maps</th>"
        "<th>Voxel validity</th><th>Map statistics</th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    technical_report_html = "".join(technical_html)

    blind_label = "Blinded (team identities removed)" if blinded else "Unblinded (internal review)"

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>OSIPI Evaluation Report</title>
<style>
  :root {{ --purple:#4c2a86; --lav:#ede8f6; --ok:#1e7d4f; --warn:#9a6a00; --err:#b3261e; --gray:#6b6b76; }}
  body {{ font-family: 'Inter', system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
          color:#23232b; max-width:980px; margin:32px auto; padding:0 20px; line-height:1.5; }}
  h1 {{ color:var(--purple); font-size:1.6rem; margin:0 0 4px; }}
  .sub {{ color:var(--gray); font-size:0.9rem; margin:0 0 18px; }}
  .accent {{ height:4px; background:var(--purple); border-radius:3px; margin-bottom:18px; }}
  .cards {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin:18px 0; }}
  .card {{ background:var(--lav); border-radius:10px; padding:14px; text-align:center; }}
  .card .v {{ font-size:1.7rem; font-weight:700; color:var(--purple); }}
  .card .l {{ font-size:0.78rem; color:var(--gray); }}
  table {{ width:100%; border-collapse:collapse; margin-top:10px; font-size:0.85rem; }}
  th,td {{ text-align:left; padding:8px 10px; border-bottom:1px solid #e7e4ef; }}
  th {{ background:#faf8fd; color:var(--purple); font-weight:600; }}
  .b {{ padding:2px 8px; border-radius:10px; font-size:0.74rem; font-weight:600; }}
  .b-ok {{ background:#e3f4ea; color:var(--ok); }}
  .b-err {{ background:#fbe6e4; color:var(--err); }}
  .note {{ padding:12px 14px; border-radius:8px; font-size:0.86rem; margin:14px 0; }}
  .note-ok {{ background:#e3f4ea; color:var(--ok); }}
  .note-warn {{ background:#fdf3df; color:var(--warn); }}
  .note-gray {{ background:#f0f0f3; color:var(--gray); }}
  .report-details {{ border:1px solid #e7e4ef; border-radius:8px; margin:12px 0; overflow:hidden; }}
  .report-details > summary {{ cursor:pointer; padding:10px 12px; background:#faf8fd; color:var(--purple); font-weight:700; font-size:0.85rem; }}
  .report-details p, .report-details h3, .report-details pre, .report-details .tech-table, .report-details .raw-list {{ margin-left:12px; margin-right:12px; }}
  .report-details h3 {{ font-size:0.9rem; color:var(--purple); margin-top:14px; margin-bottom:6px; }}
  .muted {{ color:var(--gray); font-size:0.82rem; }}
  .raw-list {{ font-size:0.82rem; }}
  code {{ background:#f3f1f8; border-radius:4px; padding:1px 4px; }}
  pre {{ background:#fbfafd; border:1px solid #ece8f4; border-radius:6px; padding:10px; overflow:auto; font-size:0.76rem; }}
  .tech-table {{ display:block; overflow-x:auto; white-space:nowrap; }}
  .footnote {{ margin-top:22px; color:var(--gray); font-size:0.74rem; font-style:italic; }}
  footer {{ margin-top:14px; color:var(--gray); font-size:0.78rem; }}
</style></head>
<body>
  <div class="accent"></div>
  <h1>OSIPI Perfusion Challenge — Evaluation Report</h1>
  <p class="sub">Generated {_esc(generated)} · {n} submission(s) · {_esc(blind_label)}</p>
  <div class="cards">
    <div class="card"><div class="v">{n}</div><div class="l">Submissions</div></div>
    <div class="card"><div class="v">{n_passed}</div><div class="l">Validation passed</div></div>
    <div class="card"><div class="v">{n_skipped}</div><div class="l">Execution skipped<br>(result maps)</div></div>
    <div class="card"><div class="v">{n_scored}</div><div class="l">Scored</div></div>
  </div>
  {scoring_note}
  <h2 style="font-size:1.1rem;color:var(--purple)">Per-submission results</h2>
  {table_html}
  <h2 style="font-size:1.1rem;color:var(--purple);margin-top:22px">NIfTI map technical details</h2>
  {technical_report_html}
  <h2 style="font-size:1.1rem;color:var(--purple);margin-top:22px">What this report contains</h2>
  <p style="font-size:0.86rem;color:#444">The standardized evaluation package includes validation
  results (file structure, NIfTI readability, metadata), execution status (Docker runs, or skipped
  when result maps were already provided), per-map NIfTI metadata/statistics, and scoring/QC metrics
  where a scoring package is configured. Use the CSV exports for machine-readable comparison.</p>
  {scoring_footnote}
  <footer>OSIPI Perfusion Pipeline · {n_failed} submission(s) failed validation · Automated report.</footer>
</body></html>"""

    tag = (batch_id or submission_id or "report").replace("/", "_")
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="osipi_report_{tag}.html"'},
    )
