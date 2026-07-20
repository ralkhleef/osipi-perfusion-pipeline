"""FastAPI backend for the OSIPI perfusion pipeline web interface."""

import csv
import base64
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
from services.pdf_report_service import generate_pdf_report
from osipi_pipeline.config.rules import (
    app_settings,
    challenge_labels,
    challenge_types,
    default_challenge_type,
    default_scoring_map_type,
    expected_maps_by_challenge,
    map_type_patterns,
    map_type_specs,
    tuple_setting,
)
from osipi_pipeline.performance import job_status, recent_timings, timed
from scoring import (
    all_providers_status,
    analyze_submission_niftis,
    batch_scoring_status,
    load_scoring_result,
    score_batch,
    score_submission,
    scoring_status,
)


DEFAULT_CHALLENGE_TYPE = default_challenge_type()
DEFAULT_SCORING_MAP_TYPE = default_scoring_map_type()
KNOWN_CHALLENGE_TYPES = tuple(challenge_types())


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


@app.get("/api/config")
def app_config():
    """Return user-facing pipeline defaults and configured validation rules."""

    settings = app_settings()
    configured_challenges = tuple(challenge_types())
    return {
        "defaults": {
            "challenge_type": default_challenge_type(),
            "scoring_map_type": default_scoring_map_type(),
            "validation_mode": settings.get("defaults", {}).get("validation_mode", "auto"),
        },
        "challenge_types": [
            {
                "id": challenge,
                "label": challenge_labels().get(challenge, challenge.upper()),
                "expected_maps": list(expected_maps_by_challenge().get(challenge, ())),
            }
            for challenge in configured_challenges
        ],
        "map_type_patterns": {
            key: list(value)
            for key, value in map_type_patterns(display_keys=True).items()
        },
        "map_types": [
            {
                "id": key,
                "display": str(spec.get("display") or key),
                "label": str(spec.get("label") or key),
                "units": spec.get("units"),
            }
            for key, spec in map_type_specs().items()
        ],
        "limits": settings.get("limits", {}),
        "reporting": settings.get("reporting", {}),
    }


@app.get("/api/performance/timings")
def performance_timings(limit: int = Query(50, ge=1, le=200)):
    """Return recent in-process timing samples for local performance review."""

    return {"timings": recent_timings(limit)}


@app.get("/api/jobs/{job_id}")
def get_job_status(job_id: str):
    """Return local in-process job progress for long-running operations."""

    status = job_status(job_id)
    if not status:
        raise HTTPException(status_code=404, detail="Job not found.")
    return status


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

        with timed("ingestion.upload_zip", filename=file.filename, bytes=total_bytes):
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


def _flatten_upload_result(result: dict, fallback_filename: str) -> List[dict]:
    """Normalise a single-ZIP extraction result into a list of submission records.

    A single-submission result is turned into one record; a batch result returns
    its per-submission records unchanged. Each record already carries
    ``detected_challenge_type`` (per-submission challenge) from ingestion.
    """
    if result.get("batch"):
        return list(result.get("submissions") or [])
    return [{
        "submission_id": result.get("submission_id"),
        "source_folder": result.get("original_filename") or fallback_filename,
        "file_count": result.get("file_count"),
        "nifti_count": result.get("nifti_count"),
        "detected_parameter_map_type": result.get("detected_parameter_map_type"),
        "detected_map_type_confidence": result.get("detected_map_type_confidence"),
        "detection_warning": result.get("detection_warning"),
        "detected_challenge_type": result.get("detected_challenge_type"),
    }]


@app.post("/api/upload-submissions")
async def upload_submissions(files: List[UploadFile] = File(...)):
    """Accept several ZIPs at once and merge them into one batch.

    Each ZIP is extracted independently (single- or multi-submission), then all
    resulting submissions are merged into a single batch list. Every submission
    keeps its own ``detected_challenge_type`` so a mixed upload (e.g. ASL + DCE)
    stays correctly scoped downstream — the pipeline never merges challenges.

    A per-file failure is reported in ``failed`` and does not abort the others.
    """
    if not files:
        raise HTTPException(status_code=400, detail="No files were uploaded.")

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    submissions: List[dict] = []
    failed: List[dict] = []
    seen_ids: set[str] = set()

    for upload in files:
        name = Path(upload.filename or "").name
        if not name.lower().endswith(".zip"):
            failed.append({"filename": upload.filename, "error": "Only .zip files are accepted."})
            continue

        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(INCOMING_DIR), suffix=".tmp")
        tmp_path = Path(tmp_name)
        try:
            total_bytes = 0
            oversize = False
            with os.fdopen(tmp_fd, "wb") as fout:
                while True:
                    chunk = await upload.read(65536)
                    if not chunk:
                        break
                    total_bytes += len(chunk)
                    if total_bytes > ZIP_MAX_BYTES:
                        oversize = True
                        break
                    fout.write(chunk)
            if oversize:
                failed.append({
                    "filename": name,
                    "error": f"ZIP file is too large (limit: {ZIP_MAX_BYTES // (1024 * 1024)} MB).",
                })
                continue

            final_path = INCOMING_DIR / name
            tmp_path.replace(final_path)
            with timed("ingestion.upload_zip", filename=name, bytes=total_bytes):
                result = save_and_extract_batch_from_path(final_path, name)
            if not result.get("success"):
                failed.append({"filename": name, "error": result.get("error", "Upload failed.")})
                continue
            for record in _flatten_upload_result(result, name):
                sid = record.get("submission_id")
                if sid and sid in seen_ids:
                    # Two archives produced the same submission id; keep the first,
                    # flag the duplicate so nothing is silently overwritten.
                    failed.append({"filename": name, "error": f"Duplicate submission id '{sid}' skipped."})
                    continue
                if sid:
                    seen_ids.add(sid)
                record["source_archive"] = name
                submissions.append(record)
        except HTTPException:
            raise
        except Exception as exc:
            failed.append({"filename": name, "error": str(exc)})
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    if not submissions:
        detail = "No valid submissions were extracted from the uploaded files."
        if failed:
            detail += " " + "; ".join(f"{f.get('filename')}: {f.get('error')}" for f in failed[:5])
        raise HTTPException(status_code=400, detail=detail)

    return {
        "success": True,
        "batch": True,
        "source_type": "local",
        "submission_count": len(submissions),
        "submissions": submissions,
        "failed": failed,
        "message": f"Extracted {len(submissions)} submission(s) from {len(files)} archive(s).",
    }


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
    challenge_type: str = DEFAULT_CHALLENGE_TYPE
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
    qc_mode: str = "deep"  # "deep" preserves current behavior; "quick" skips voxel loading
    force_validation_refresh: bool = False


class PreflightRequest(BaseModel):
    submission_id: str
    challenge_type: str = DEFAULT_CHALLENGE_TYPE
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
        challenge_type=req.challenge_type.strip() or DEFAULT_CHALLENGE_TYPE,
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
        qc_mode=req.qc_mode,
        force_validation_refresh=req.force_validation_refresh,
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
        challenge_type=req.challenge_type.strip() or DEFAULT_CHALLENGE_TYPE,
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

        with timed("ingestion.upload_batch_zip", filename=file.filename, bytes=total_bytes):
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
    challenge_type: str = DEFAULT_CHALLENGE_TYPE
    # Optional per-submission challenge overrides ({submission_id: challenge}).
    # Lets a mixed batch (e.g. ASL + DCE) validate each submission under its own
    # challenge. Falls back to challenge_type for any submission not listed.
    challenge_types: Optional[Dict[str, str]] = None
    map_type: Optional[str] = None
    map_type_mode: Optional[str] = None
    notes: Optional[str] = None
    team_names: Optional[Dict[str, str]] = None
    contact_emails: Optional[Dict[str, str]] = None
    mode: str = "auto"  # "auto" | "result_only" | "result_validation" | "reproducible" | "reproducible_execution"
    qc_mode: str = "deep"


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
        challenge_types=req.challenge_types,
        map_type=req.map_type,
        map_type_mode=req.map_type_mode,
        notes=req.notes,
        team_names=req.team_names,
        contact_emails=req.contact_emails,
        mode=mode,
        qc_mode=req.qc_mode,
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

    # Group rows by challenge (stable) so a mixed batch export is challenge-ordered.
    batch_results = sorted(
        batch.get("results", []),
        key=lambda r: str(r.get("challenge_type") or "").strip().upper(),
    )
    for r in batch_results:
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

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")


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
    with timed("preview.list", submission_id=submission_id, challenge_type=challenge_type or ""):
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
            <a class="button secondary" href="/static/index.html#score">Back to app</a>
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
    challenge_type: str = DEFAULT_CHALLENGE_TYPE
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
        challenge_type=req.challenge_type.strip() or DEFAULT_CHALLENGE_TYPE,
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
    challenge_type: str = DEFAULT_CHALLENGE_TYPE
    map_type:       str = DEFAULT_SCORING_MAP_TYPE


class ScoreBatchRequest(BaseModel):
    submission_ids: List[str]
    provider_id:    Optional[str] = None   # preferred; if set, challenge_type/map_type are ignored
    challenge_type: str = DEFAULT_CHALLENGE_TYPE
    map_type:       str = DEFAULT_SCORING_MAP_TYPE
    batch_id:       Optional[str] = None


@app.get("/api/scoring-status")
def get_scoring_status(
    submission_id:  Optional[str] = Query(None),
    provider_id:    Optional[str] = Query(None),
    challenge_type: str           = Query(DEFAULT_CHALLENGE_TYPE),
    map_type:       str           = Query(DEFAULT_SCORING_MAP_TYPE),
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
    challenge_type: str           = DEFAULT_CHALLENGE_TYPE
    map_type:       str           = DEFAULT_SCORING_MAP_TYPE
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
    with timed("scoring.single", submission_id=req.submission_id, challenge_type=req.challenge_type):
        result = score_submission(
            req.submission_id.strip(),
            req.challenge_type.strip() or DEFAULT_CHALLENGE_TYPE,
            req.map_type.strip() or DEFAULT_SCORING_MAP_TYPE,
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
    challenge_type: str
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
    configured_challenges = tuple(challenge_types())
    if ct not in configured_challenges:
        raise HTTPException(
            status_code=400,
            detail=f"challenge_type must be one of: {', '.join(configured_challenges)}.",
        )
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
    "mean_coefficient_of_variation": "Spatial CoV (map variability)",
    "spatial_coefficient_of_variation": "Spatial CoV (map variability)",
    "coefficient_of_variation": "Error CoV (voxel error spread)",
    "error_coefficient_of_variation": "Error CoV (voxel error spread)",
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


REFERENCE_UNAVAILABLE_NOTE = (
    "Reference maps were not available, so this report shows QC metrics only."
)

# Repeatability CoV and ICC require repeated (noise-varied) datasets, which a
# single submitted map cannot provide. Surfaced in every report so the shown
# accuracy CoV is never mistaken for a repeatability measure.
REPEATABILITY_UNAVAILABLE_NOTE = (
    "Repeatability CoV and ICC are unavailable: they require repeated "
    "(noise-varied) datasets, which have not been provided. The coefficient of "
    "variation reported here is an accuracy error-CoV, not a repeatability CoV."
)


def _fmt_export_cell(value, digits: int = 3) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        if isinstance(value, float):
            return (f"{value:.{digits}f}").rstrip("0").rstrip(".")
        return str(value)
    return str(value)


def _fmt_report_cell(value, digits: int = 3) -> str:
    text = _fmt_export_cell(value, digits=digits)
    return text if text != "" else "Not available"


def _mean_numeric(values) -> Optional[float]:
    nums = [
        float(v) for v in values
        if isinstance(v, (int, float)) and not isinstance(v, bool)
    ]
    if not nums:
        return None
    return sum(nums) / len(nums)


def _weighted_percent(numerators, denominators) -> Optional[float]:
    num_total = 0.0
    den_total = 0.0
    for numerator, denominator in zip(numerators, denominators):
        if (
            isinstance(numerator, (int, float))
            and not isinstance(numerator, bool)
            and isinstance(denominator, (int, float))
            and not isinstance(denominator, bool)
        ):
            num_total += float(numerator)
            den_total += float(denominator)
    if den_total <= 0:
        return None
    return (num_total / den_total) * 100.0


def _reference_available(analysis_fields: dict) -> bool:
    return (
        bool(analysis_fields.get("reference_based_scoring_available"))
        or int(analysis_fields.get("reference_compared_map_count") or 0) > 0
    )


def _reference_status_label(analysis_fields: dict) -> str:
    raw = str(analysis_fields.get("reference_scoring_status") or "").strip().lower()
    compared = int(analysis_fields.get("reference_compared_map_count") or 0)
    if raw == "partial_reference_scoring":
        return "Partial"
    if raw == "available" or compared > 0 or bool(analysis_fields.get("reference_based_scoring_available")):
        return "Available"
    if raw in {"shape_mismatch", "submitted_invalid", "reference_invalid", "scoring_error"}:
        return raw.replace("_", " ").title()
    return "Not available"


def _research_notes(summary: dict, *, include_reference_note: bool = True) -> str:
    notes: list[str] = []
    af = summary["analysis_fields"]
    if include_reference_note and not _reference_available(af):
        notes.append(REFERENCE_UNAVAILABLE_NOTE)
    warning_count = int(summary.get("warning_count") or 0)
    error_count = int(summary.get("error_count") or 0)
    if warning_count:
        notes.append(f"{warning_count} warning(s) reported.")
    if error_count:
        notes.append(f"{error_count} error(s) reported.")
    return " ".join(notes)


def _submission_display_name(summary: dict, index: int, *, blinded: bool) -> str:
    if blinded:
        return f"Submission {index}"
    return str(summary.get("source_folder") or summary.get("submission_id") or f"Submission {index}")


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
    means_by_map_type = summary.get("means_by_map_type")
    means_by_map_type = means_by_map_type if isinstance(means_by_map_type, dict) else {}
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
        _wv, _wt = whole.get("voxel_count"), whole.get("total_voxel_count")
        _wex = (_wt - _wv) if isinstance(_wt, (int, float)) and isinstance(_wv, (int, float)) else None
        reference_rows.append({
            "submitted_file": item.get("submitted_file", ""),
            "reference_file": item.get("reference_file", ""),
            "detected_map_type": item.get("detected_map_type", ""),
            "scope": "whole image",
            "mask_name": "",
            "status": item.get("status", ""),
            "rmse": whole.get("rmse"),
            "mae": whole.get("mae"),
            "bias": whole.get("bias"),
            "coefficient_of_variation": whole.get("error_coefficient_of_variation", whole.get("coefficient_of_variation")),
            "correlation": whole.get("correlation"),
            "voxel_count": _wv,
            "excluded_voxel_count": _wex,
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
                "coefficient_of_variation": metrics.get("error_coefficient_of_variation", metrics.get("coefficient_of_variation")),
                "correlation": metrics.get("correlation"),
                "voxel_count": metrics.get("voxel_count"),
                "excluded_voxel_count": (
                    (metrics.get("total_voxel_count") - metrics.get("voxel_count"))
                    if isinstance(metrics.get("total_voxel_count"), (int, float))
                    and isinstance(metrics.get("voxel_count"), (int, float)) else None
                ),
                "finite_voxel_percent": metrics.get("finite_voxel_percent"),
                "difference_map": item.get("difference_map"),
            })
    return {
        "map_count": summary.get("map_count", 0),
        "parameter_maps_detected": ", ".join(summary.get("parameter_maps_detected") or []),
        "finite_voxels_percent": summary.get("finite_percent"),
        "negative_voxels_percent": summary.get("negative_voxel_percent"),
        "means_by_map_type": means_by_map_type,
        "mean_coefficient_of_variation": summary.get("mean_coefficient_of_variation"),
        "mean_standard_deviation": summary.get("mean_standard_deviation"),
        "total_voxel_count": summary.get("total_voxel_count", 0),
        "finite_voxel_count": summary.get("finite_voxel_count", 0),
        "negative_voxel_count": summary.get("negative_voxel_count", 0),
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
        "errors":          (val or {}).get("errors", []) if val else [],
        "warnings":        (val or {}).get("warnings", []) if val else [],
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


def _combined_mean_columns() -> list[tuple[str, str]]:
    return [
        (str(key).lower(), str(spec.get("display") or key))
        for key, spec in map_type_specs().items()
    ]


def _combined_header_blinded() -> list[str]:
    return [
        "blinded_submission_id", "challenge_type", "map_types", "map_count",
        "warning_count", "error_count", "reference_status",
        "finite_voxels_percent", "nan_count", "inf_count", "negative_voxels_percent",
        *[f"mean_{key}" for key, _display in _combined_mean_columns()],
        "rmse", "mae", "bias", "cov", "icc", "notes",
    ]


def _combined_header_unblinded() -> list[str]:
    return [
        "team_name", "contact_email", "original_submission_name", "submission_id",
    ] + _combined_header_blinded()


# ---------------------------------------------------------------------------
# Long-format (tidy) researcher CSV — one row per
#   submission × subject × session/repeat × map × ROI × metric
# ---------------------------------------------------------------------------

# Scientific metrics emitted per (map, ROI). Accuracy metrics carry the computed
# value (or blank when a comparison did not run); repeatability CoV and ICC are
# always emitted as explicitly unavailable (they need repeated noise-varied
# datasets), because that "unavailable" status is useful to the researcher.
_LONG_ACCURACY_METRICS = [
    ("rmse", "rmse"),
    ("mae", "mae"),
    ("bias", "bias"),
    ("error_coefficient_of_variation", "error_coefficient_of_variation"),
    ("correlation", "correlation"),
]
_LONG_UNAVAILABLE_METRICS = [
    "repeatability_coefficient_of_variation",
    "icc",
]

_LONG_SCIENTIFIC_COLUMNS = [
    "blinded_submission_id", "challenge", "subject_id", "session_or_repeat_id",
    "map_type", "map_display_name", "units", "roi",
    "metric_name", "metric_value", "metric_status",
    "valid_voxel_count", "excluded_voxel_count",
    "finite_voxel_percent", "nan_voxel_count", "inf_voxel_count",
    "negative_voxel_percent", "reference_status", "validation_status",
    "warning_codes", "pipeline_version", "configuration_version", "export_date",
]

# Organiser-only identity columns, prepended for the unblinded long CSV.
_LONG_IDENTITY_COLUMNS = [
    "submission_id", "team_name", "contact_name", "contact_email", "institution",
    "submission_source", "original_archive_name", "repository_url", "submitted_at",
]


def _pipeline_version() -> str:
    try:
        text = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        import re as _re
        m = _re.search(r'^version\s*=\s*"([^"]+)"', text, _re.M)
        return m.group(1) if m else "unknown"
    except Exception:
        return "unknown"


def _configuration_version() -> str:
    try:
        from osipi_pipeline.config.rules import validation_rules as _vr
        v = _vr().get("version")
        return str(v) if v is not None else "unknown"
    except Exception:
        return "unknown"


def _subject_from_name(name: str) -> str:
    import re as _re
    m = _re.search(r"sub-([A-Za-z0-9]+)", str(name or ""))
    return m.group(1) if m else ""


def _warning_codes(summary: dict) -> str:
    codes = []
    for w in summary.get("warnings") or []:
        if isinstance(w, dict) and w.get("code"):
            codes.append(str(w.get("code")))
    return ";".join(codes)


def _validation_status_text(summary: dict) -> str:
    passed = summary.get("val_passed")
    if passed is True:
        return "passed"
    if passed is False:
        return "failed"
    return "not_validated"


def _long_metric_status(region_status: str, value) -> str:
    if region_status != "compared":
        return region_status or "reference_not_available"
    return "computed" if value is not None else "not_available"


def _long_csv_rows(gathered_by_sid: dict, sids: list, blinded: bool) -> tuple[list, list]:
    """Return (header, rows) for the tidy long-format researcher CSV.

    One row per submission × subject × session/repeat × map × ROI × metric.
    CBF and ATT stay in separate rows; each ROI is a separate row; each metric is
    a separate row. Missing metric values are left blank (never zero). No
    cross-map or cross-challenge aggregation is performed.
    """
    header = (([] if blinded else list(_LONG_IDENTITY_COLUMNS)) + list(_LONG_SCIENTIFIC_COLUMNS))
    pipeline_version = _pipeline_version()
    config_version = _configuration_version()
    export_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows: list[list] = []
    for idx, sid in enumerate(sids, start=1):
        s = gathered_by_sid[sid]
        blinded_id = f"SUB-{idx:04d}"
        challenge = str(s.get("challenge_type") or "").strip().upper()
        validation_status = _validation_status_text(s)
        warning_codes = _warning_codes(s)
        analysis = s.get("nifti_analysis") if isinstance(s.get("nifti_analysis"), dict) else {}
        ref = analysis.get("reference_scoring") if isinstance(analysis.get("reference_scoring"), dict) else {}
        ref_maps = ref.get("maps") or []
        # QC per-map lookup for nan/inf/finite/negative fallback (keyed by map type).
        qc_by_type: dict[str, dict] = {}
        for qm in analysis.get("maps") or []:
            if isinstance(qm, dict) and qm.get("detected_map_type"):
                qc_by_type.setdefault(str(qm["detected_map_type"]), qm)

        identity_cells = [] if blinded else [
            s.get("submission_id", ""),
            s.get("team_name", ""),
            "",  # contact_name — not captured in current submission metadata
            s.get("contact_email", ""),
            "",  # institution — not captured
            (s.get("mode") or "local"),  # submission_source (best available)
            s.get("source_folder", ""),  # original_archive_name
            "",  # repository_url — not captured for local uploads
            (s.get("scored_at") or ""),  # submitted_at (best available timestamp)
        ]

        for ref_row in ref_maps:
            if not isinstance(ref_row, dict):
                continue
            map_type = str(ref_row.get("detected_map_type") or "Unknown")
            display = str(ref_row.get("parameter_label") or map_type)
            units = ref_row.get("units") or ""
            if units == "units not provided":
                units = ""
            map_ref_status = str(ref_row.get("status") or "reference_not_available")
            subject_id = _subject_from_name(ref_row.get("submitted_file"))
            qc = qc_by_type.get(map_type, {})
            qc_meta = qc.get("metadata") or {}
            qc_stats = qc.get("stats") or {}
            nan_count = qc_meta.get("nan_count")
            inf_count = qc_meta.get("inf_count")

            scopes = [("whole_image", ref_row.get("whole_map") or {})]
            for mask in ref_row.get("masks") or []:
                if isinstance(mask, dict):
                    roi_name = mask.get("mask_label") or mask.get("mask_name") or "roi"
                    scopes.append((str(roi_name), mask.get("metrics") or {}))

            for roi_name, metrics in scopes:
                region_status = str(metrics.get("status") or map_ref_status)
                valid = metrics.get("voxel_count")
                total = metrics.get("total_voxel_count")
                excluded = (total - valid) if isinstance(total, (int, float)) and isinstance(valid, (int, float)) else None
                finite_pct = metrics.get("finite_voxel_percent")
                if finite_pct is None and roi_name == "whole_image":
                    finite_pct = qc_stats.get("finite_percent")
                neg_pct = metrics.get("negative_voxel_percent")
                if neg_pct is None and roi_name == "whole_image":
                    neg_pct = qc_stats.get("negative_voxel_percent")

                def _emit(metric_name: str, value, status: str):
                    sci = [
                        blinded_id, challenge, subject_id, "",  # session_or_repeat_id — no repeats yet
                        map_type, display, units, roi_name,
                        metric_name,
                        _fmt_export_cell(value),
                        status,
                        _fmt_export_cell(valid, digits=0),
                        _fmt_export_cell(excluded, digits=0),
                        _fmt_export_cell(finite_pct),
                        _fmt_export_cell(nan_count, digits=0),
                        _fmt_export_cell(inf_count, digits=0),
                        _fmt_export_cell(neg_pct),
                        region_status,
                        validation_status,
                        warning_codes,
                        pipeline_version,
                        config_version,
                        export_date,
                    ]
                    rows.append(identity_cells + sci)

                for metric_name, key in _LONG_ACCURACY_METRICS:
                    value = metrics.get(key)
                    if key == "error_coefficient_of_variation":
                        value = metrics.get("error_coefficient_of_variation", metrics.get("coefficient_of_variation"))
                    status = _long_metric_status(region_status, value)
                    if region_status != "compared":
                        value = None  # no comparison → blank, never zero
                    _emit(metric_name, value, status)

        # Repeatability CoV and ICC are unavailable for the whole submission
        # (they need repeated noise-varied datasets). Emit ONE submission-level
        # row each instead of repeating an identical unavailable row per map/ROI,
        # keeping the tidy table machine-readable without noise.
        for metric_name in _LONG_UNAVAILABLE_METRICS:
            sci = [
                blinded_id, challenge, "", "",
                "(all maps)", "", "", "(submission-level)",
                metric_name, "", "unavailable_requires_repeated_datasets",
                "", "", "", "", "", "",
                "not_applicable", validation_status, warning_codes,
                pipeline_version, config_version, export_date,
            ]
            rows.append(identity_cells + sci)

    return header, rows


@app.get("/api/export-combined")
def export_combined(
    submission_id: Optional[str] = Query(None),
    batch_id:      Optional[str] = Query(None),
    blinded:       bool          = Query(False, description="True to strip team_name and contact_email"),
    format:        str           = Query("csv", description="'csv' or 'json'"),
    shape:         str           = Query("wide", description="CSV shape: 'wide' (one row per submission) or 'long' (tidy: one row per map/ROI/metric)"),
):
    """Export a researcher-facing summary.

    CSV ``shape=wide`` (default) returns one row per submission. ``shape=long``
    returns a tidy table with one row per submission × subject × session/repeat ×
    map × ROI × metric — CBF and ATT stay in separate rows and no cross-map or
    cross-challenge averages are introduced. Blinded export omits team/contact/
    original submission identifiers. Raw validation, execution, and scoring
    exports remain available from their dedicated backend endpoints.
    """
    sids = _collect_export_ids(batch_id, submission_id)
    export_format = (format or "csv").strip().lower()
    csv_shape = (shape or "wide").strip().lower()
    if export_format not in {"csv", "json"}:
        raise HTTPException(status_code=400, detail="format must be 'csv' or 'json'.")
    if csv_shape not in {"wide", "long"}:
        raise HTTPException(status_code=400, detail="shape must be 'wide' or 'long'.")

    # Gather once and group submissions by challenge (stable within a challenge)
    # so exports are challenge-grouped. Every row/object carries its own
    # challenge_type; there is no cross-challenge aggregate row.
    gathered_by_sid = {sid: _gather_summary(sid) for sid in sids}
    sids = sorted(sids, key=lambda sid: str(gathered_by_sid[sid].get("challenge_type") or "").strip().upper())

    if export_format == "json":
        summaries = []
        for idx, sid in enumerate(sids, start=1):
            s = gathered_by_sid[sid]
            af = s["analysis_fields"]
            item = {
                "blinded_submission_id": f"submission_{idx:03d}",
                "submission_id": None if blinded else s["submission_id"],
                "team_name": None if blinded else s["team_name"],
                "contact_email": None if blinded else s["contact_email"],
                "original_submission_name": None if blinded else s["source_folder"],
                "challenge_type": s["challenge_type"],
                "validation": {
                    "available": s["has_validation"],
                    "passed": s["val_passed"],
                    "warning_count": s["warning_count"],
                    "error_count": s["error_count"],
                    "nifti_count": s["nifti_count"],
                    "run_readiness": s["run_readiness"],
                },
                "execution": {
                    "status": s["exec_status"],
                    "generated_files": s["generated_files"],
                },
                "qc": {
                    "map_types": af["parameter_maps_detected"],
                    "map_count": af["map_count"],
                    "finite_voxels_percent": af["finite_voxels_percent"],
                    "nan_count": af["nan_count"],
                    "inf_count": af["inf_count"],
                    "negative_voxels_percent": af["negative_voxels_percent"],
                    "means_by_map_type": af.get("means_by_map_type") or {},
                    "mean_coefficient_of_variation": af["mean_coefficient_of_variation"],
                },
                "reference": {
                    "status": _reference_status_label(af),
                    "available": _reference_available(af),
                    "rmse": af["reference_mean_rmse"] if _reference_available(af) else None,
                    "mae": af["reference_mean_mae"] if _reference_available(af) else None,
                    "bias": af["reference_mean_bias"] if _reference_available(af) else None,
                    "compared_map_count": af["reference_compared_map_count"],
                    "reference_map_count": af["reference_map_count"],
                },
                "scoring": {
                    "status": s["scoring_status"],
                    "official": s["scoring_official"],
                    "scored_at": s["scored_at"],
                    "numeric_metrics": s["numeric_metrics"],
                },
                "notes": _research_notes(s),
            }
            summaries.append(item)
        if not summaries:
            raise HTTPException(status_code=404, detail="No submissions found to export.")
        tag = (batch_id or submission_id or "export").replace("/", "_")
        suffix = "blinded" if blinded else "unblinded"
        return Response(
            content=json.dumps({
                "report_type": "blinded" if blinded else "unblinded",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "submission_count": len(summaries),
                "submissions": summaries,
                "limitations": [
                    "Basic NIfTI QC is not full BIDS validation.",
                    "Generic QC/reference metrics are not official OSIPI scores unless an official provider is configured.",
                ],
            }, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="osipi_combined_{tag}_{suffix}.json"'},
        )

    # ── Long (tidy) CSV: one row per submission × subject × session × map × ROI × metric ──
    if csv_shape == "long":
        header, long_rows = _long_csv_rows(gathered_by_sid, sids, blinded)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(header)
        for r in long_rows:
            w.writerow(r)
        suffix = "blinded" if blinded else "unblinded"
        tag = (batch_id or submission_id or "export").replace("/", "_")
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="osipi_results_long_{tag}_{suffix}.csv"'},
        )

    output = io.StringIO()
    writer = csv.writer(output)
    combined_mean_columns = _combined_mean_columns()
    writer.writerow(_combined_header_unblinded() if not blinded else _combined_header_blinded())

    wrote_any = False
    for idx, sid in enumerate(sids, start=1):
        s = gathered_by_sid[sid]
        af = s["analysis_fields"]
        if not s["has_validation"] and not s["has_scoring"] and s["exec_status"] == "not_run":
            # Nothing recorded for this submission yet — still emit a row so the
            # combined export is complete, but mark it clearly.
            pass
        wrote_any = True
        row: list = []
        if not blinded:
            row += [s["team_name"], s["contact_email"], s["source_folder"], s["submission_id"]]
        reference_available = _reference_available(af)
        row += [
            f"submission_{idx:03d}",
            s["challenge_type"],
            af["parameter_maps_detected"],
            _fmt_export_cell(af["map_count"], digits=0),
            _fmt_export_cell(s["warning_count"], digits=0),
            _fmt_export_cell(s["error_count"], digits=0),
            _reference_status_label(af),
            _fmt_export_cell(af["finite_voxels_percent"]),
            _fmt_export_cell(af["nan_count"], digits=0),
            _fmt_export_cell(af["inf_count"], digits=0),
            _fmt_export_cell(af["negative_voxels_percent"]),
            *[
                _fmt_export_cell((af.get("means_by_map_type") or {}).get(display))
                for _key, display in combined_mean_columns
            ],
            _fmt_export_cell(af["reference_mean_rmse"] if reference_available else None),
            _fmt_export_cell(af["reference_mean_mae"] if reference_available else None),
            _fmt_export_cell(af["reference_mean_bias"] if reference_available else None),
            _fmt_export_cell(af["mean_coefficient_of_variation"]),
            _fmt_export_cell(s["numeric_metrics"].get("icc") if isinstance(s.get("numeric_metrics"), dict) else None),
            _research_notes(s),
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


def _report_status(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw == "skipped_result_maps":
        return "Execution not required"
    if raw in {"", "none", "not_run", "not_scored", "reference_not_available", "not_available"}:
        return "Not available"
    if raw in {"pass", "passed", "complete", "completed", "scored", "available"}:
        return "Complete"
    if raw in {"warning", "partial_reference_scoring", "needs_review"}:
        return "Needs review"
    if raw in {"fail", "failed", "error", "timed_out", "timed-out", "cannot_run"}:
        return "Unable to continue"
    return raw.replace("_", " ").replace("-", " ").title()


def _status_chip_html(label: str, tone: str) -> str:
    return f'<span class="r-chip r-chip-{_esc(tone)}">{_esc(label)}</span>'


def _metric_card_html(label: str, value: object, note: str = "") -> str:
    note_html = f'<span class="metric-note">{_esc(note)}</span>' if note else ""
    return (
        '<div class="metric-card">'
        f'<span class="metric-label">{_esc(label)}</span>'
        f'<strong>{_esc(_fmt_report_cell(value))}</strong>'
        f'{note_html}</div>'
    )


def _bar_svg(title: str, values: list[tuple[str, float | int | None, str]], *, width: int = 520) -> str:
    numeric = [(label, max(0.0, float(value or 0)), color) for label, value, color in values]
    if not numeric:
        return ""
    max_value = max((value for _label, value, _color in numeric), default=0.0)
    if max_value <= 0:
        return ""
    row_h = 30
    label_w = 155
    chart_w = width - label_w - 58
    height = 34 + row_h * len(numeric)
    rows: list[str] = []
    for idx, (label, value, color) in enumerate(numeric):
        y = 28 + idx * row_h
        bar_w = 0 if max_value <= 0 else max(1, (value / max_value) * chart_w)
        rows.append(
            f'<text x="0" y="{y + 13}" class="svg-label">{_esc(label)}</text>'
            f'<rect x="{label_w}" y="{y}" width="{chart_w}" height="16" rx="3" class="svg-track"/>'
            f'<rect x="{label_w}" y="{y}" width="{bar_w:.1f}" height="16" rx="3" fill="{_esc(color)}"/>'
            f'<text x="{label_w + chart_w + 8}" y="{y + 13}" class="svg-value">{_esc(_fmt_report_cell(value))}</text>'
        )
    return (
        f'<figure class="report-chart" role="img" aria-label="{_esc(title)}">'
        f'<figcaption>{_esc(title)}</figcaption>'
        f'<svg viewBox="0 0 {width} {height}" aria-hidden="true">'
        f'<text x="0" y="14" class="svg-title">{_esc(title)}</text>'
        + "".join(rows)
        + '</svg></figure>'
    )


def _stacked_percent_svg(title: str, values: list[tuple[str, float | int | None, str]]) -> str:
    numeric = [(label, max(0.0, float(value or 0)), color) for label, value, color in values]
    total = sum(value for _label, value, _color in numeric)
    if total <= 0:
        return ""
    x = 0.0
    segments = []
    legend = []
    for label, value, color in numeric:
        pct = value / total * 100.0
        segments.append(f'<rect x="{x:.3f}" y="0" width="{pct:.3f}" height="14" fill="{_esc(color)}"/>')
        legend.append(f'<span><i style="background:{_esc(color)}"></i>{_esc(label)}: {_esc(_fmt_report_cell(value))}</span>')
        x += pct
    return (
        f'<figure class="report-chart compact-chart" role="img" aria-label="{_esc(title)}">'
        f'<figcaption>{_esc(title)}</figcaption>'
        '<svg viewBox="0 0 100 14" preserveAspectRatio="none" aria-hidden="true">'
        + "".join(segments)
        + '</svg>'
        f'<div class="chart-legend">{"".join(legend)}</div>'
        '</figure>'
    )


def _preview_is_parameter_map(item: dict) -> bool:
    """True when a preview item is a 3-D recognized parameter map (CBF/ATT/…).

    Prefers the ``is_parameter_map`` flag set by the preview service; falls back
    to shape + map type for manifests written before that flag existed.
    """
    if isinstance(item.get("is_parameter_map"), bool):
        return item["is_parameter_map"]
    shape = [d for d in (item.get("shape") or []) if d]
    map_type = str(item.get("detected_map_type") or "").strip().lower()
    return len(shape) == 3 and map_type not in {"", "unknown", "mixed/other"}


def _report_preview_gallery(summaries: list[dict], *, blinded: bool) -> str:
    cards: list[str] = []
    for idx, summary in enumerate(summaries, start=1):
        sid = str(summary.get("submission_id") or "")
        manifest_path = OUTPUTS_DIR / "previews" / make_safe_id(sid) / "preview_manifest.json"
        if not sid or not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for item in manifest.get("maps") or []:
            if not item.get("preview_available"):
                continue
            # Only 3-D recognized parameter maps (CBF/ATT/…) belong in the gallery.
            # 4-D ASL/model data and unrecognized files are excluded so no
            # "Unknown" card appears beside CBF and ATT.
            if not _preview_is_parameter_map(item):
                continue
            map_id = str(item.get("map_id") or "")
            image_path = OUTPUTS_DIR / "previews" / make_safe_id(sid) / f"{map_id}_axial.png"
            if not image_path.exists():
                continue
            try:
                encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            except Exception:
                continue
            sub_label = _submission_display_name(summary, idx, blinded=blinded)
            cards.append(
                '<figure class="preview-card">'
                f'<img src="data:image/png;base64,{encoded}" alt="Axial preview for {_esc(item.get("detected_map_type") or "map")}">'
                f'<figcaption><strong>{_esc(item.get("detected_map_type") or "Unknown map")}</strong>'
                f'<span>{_esc(sub_label)}</span></figcaption>'
                '</figure>'
            )
            if len(cards) >= 8:
                break
        if len(cards) >= 8:
            break
    if not cards:
        return '<p class="report-muted">No parameter-map previews are available.</p>'
    return '<div class="preview-gallery">' + "".join(cards) + '</div>'


def _issue_rows_html(summaries: list[dict], *, blinded: bool) -> str:
    rows: list[str] = []
    for idx, summary in enumerate(summaries, start=1):
        label = _submission_display_name(summary, idx, blinded=blinded)
        for severity, source in (("Blocking error", "errors"), ("Needs review", "warnings")):
            messages = summary.get(source) or []
            if not isinstance(messages, list):
                continue
            for msg in messages:
                if isinstance(msg, dict):
                    text = str(msg.get("message") or msg.get("code") or "Issue recorded.")
                    affected = str(msg.get("path") or "")
                else:
                    text = str(msg)
                    affected = ""
                affected_label = Path(affected).name if affected else "Not specified"
                action = (
                    "Fix the blocking issue and validate again."
                    if severity == "Blocking error"
                    else "Review the item; warnings do not block export."
                )
                rows.append(
                    "<tr>"
                    f"<td>{_esc(severity)}</td><td>{_esc(label)}</td><td>{_esc(text)}</td><td>{_esc(affected_label)}</td><td>{_esc(action)}</td>"
                    "</tr>"
                )
    if not rows:
        rows.append('<tr><td colspan="5">No blocking errors or warnings were recorded.</td></tr>')
    return (
        '<table><caption>Errors, warnings, and recommended actions</caption>'
        '<thead><tr><th>Severity</th><th>Submission</th><th>Message</th><th>Affected file</th><th>Recommended action</th></tr></thead>'
        '<tbody>' + "".join(rows) + '</tbody></table>'
    )


@app.get("/api/report")
def export_report(
    submission_id: Optional[str] = Query(None),
    batch_id:      Optional[str] = Query(None),
    blinded:       bool          = Query(True, description="True (default) to strip team/contact info"),
):
    """Generate a self-contained, MRI-researcher-facing HTML report."""
    sids = _collect_export_ids(batch_id, submission_id)
    with timed("report.html.gather", submission_count=len(sids)):
        summaries = [_gather_summary(sid) for sid in sids]

    # Group submissions by challenge so a mixed batch (e.g. ASL + DCE) renders
    # each challenge together. Stable sort → single-challenge order is unchanged.
    summaries.sort(key=lambda s: str(s.get("challenge_type") or "").strip().upper())

    generated_dt = datetime.now(timezone.utc)
    generated = generated_dt.strftime("%Y-%m-%d %H:%M UTC")
    export_date = generated_dt.strftime("%Y-%m-%d")
    fields = [
        s.get("analysis_fields") if isinstance(s.get("analysis_fields"), dict) else {}
        for s in summaries
    ]
    n = len(summaries)
    map_count = sum(int(af.get("map_count") or 0) for af in fields)
    warning_count = sum(int(s.get("warning_count") or 0) for s in summaries)
    error_count = sum(int(s.get("error_count") or 0) for s in summaries)
    map_types = sorted({
        mt.strip()
        for af in fields
        for mt in str(af.get("parameter_maps_detected") or "").split(",")
        if mt.strip()
    })
    challenges = sorted({
        str(s.get("challenge_type") or "").strip().upper()
        for s in summaries
        if str(s.get("challenge_type") or "").strip()
    })
    reference_available = any(_reference_available(af) for af in fields)
    reference_status = "Available" if reference_available else "Not available"
    session_name = (
        f"Batch {batch_id}" if batch_id
        else (_submission_display_name(summaries[0], 1, blinded=blinded) if len(summaries) == 1 else "Export session")
    )

    finite = _weighted_percent(
        (af.get("finite_voxel_count") for af in fields),
        (af.get("total_voxel_count") for af in fields),
    )
    negative = _weighted_percent(
        (af.get("negative_voxel_count") for af in fields),
        (af.get("finite_voxel_count") for af in fields),
    )
    combined_mean_columns = _combined_mean_columns()
    mean_by_map_type = {
        display: _mean_numeric((af.get("means_by_map_type") or {}).get(display) for af in fields)
        for _key, display in combined_mean_columns
    }
    report_mean_types = [
        display for _key, display in combined_mean_columns
        if mean_by_map_type.get(display) is not None
    ]
    cov = _mean_numeric(af.get("mean_coefficient_of_variation") for af in fields)
    rmse = _mean_numeric(af.get("reference_mean_rmse") for af in fields if _reference_available(af))
    mae = _mean_numeric(af.get("reference_mean_mae") for af in fields if _reference_available(af))
    bias = _mean_numeric(af.get("reference_mean_bias") for af in fields if _reference_available(af))

    # ── Challenge scoping ─────────────────────────────────────────────────────
    # Never compute a single RMSE/MAE/Bias/CoV across different challenges. When
    # more than one challenge is present, aggregates are reported PER CHALLENGE.
    is_mixed_challenge = len(challenges) > 1

    def _fields_for_challenge(ch: str) -> list:
        return [
            s["analysis_fields"]
            for s in summaries
            if str(s.get("challenge_type") or "").strip().upper() == ch
            and isinstance(s.get("analysis_fields"), dict)
        ]

    def _reference_agg(subset: list) -> dict:
        ref_sub = [af for af in subset if _reference_available(af)]
        return {
            "available": bool(ref_sub),
            "rmse": _mean_numeric(af.get("reference_mean_rmse") for af in ref_sub),
            "mae": _mean_numeric(af.get("reference_mean_mae") for af in ref_sub),
            "bias": _mean_numeric(af.get("reference_mean_bias") for af in ref_sub),
            "cov": _mean_numeric(af.get("mean_coefficient_of_variation") for af in subset),
        }

    per_challenge_reference = {ch: _reference_agg(_fields_for_challenge(ch)) for ch in challenges}

    def _reference_summary_rows_html() -> str:
        if not is_mixed_challenge:
            return (
                f"<tr><td>RMSE</td><td>{_esc(_fmt_report_cell(rmse) if reference_available else 'Not available')}</td></tr>"
                f"<tr><td>MAE</td><td>{_esc(_fmt_report_cell(mae) if reference_available else 'Not available')}</td></tr>"
                f"<tr><td>Bias</td><td>{_esc(_fmt_report_cell(bias) if reference_available else 'Not available')}</td></tr>"
                f"<tr><td>Spatial CoV</td><td>{_esc(_fmt_report_cell(cov))}</td></tr>"
            )
        parts = ['<tr><td colspan="2"><em>Grouped by challenge — no cross-challenge totals are computed.</em></td></tr>']
        for ch in challenges:
            agg = per_challenge_reference[ch]
            avail = agg["available"]
            parts.append(f'<tr><td colspan="2"><strong>{_esc(ch)}</strong></td></tr>')
            parts.append(f"<tr><td>{_esc(ch)} RMSE</td><td>{_esc(_fmt_report_cell(agg['rmse']) if avail else 'Not available')}</td></tr>")
            parts.append(f"<tr><td>{_esc(ch)} MAE</td><td>{_esc(_fmt_report_cell(agg['mae']) if avail else 'Not available')}</td></tr>")
            parts.append(f"<tr><td>{_esc(ch)} Bias</td><td>{_esc(_fmt_report_cell(agg['bias']) if avail else 'Not available')}</td></tr>")
            parts.append(f"<tr><td>{_esc(ch)} Spatial CoV</td><td>{_esc(_fmt_report_cell(agg['cov']))}</td></tr>")
        return "".join(parts)

    reference_summary_rows_html = _reference_summary_rows_html()
    validation_status = "Unable to continue" if error_count else ("Needs review" if warning_count else "Complete")
    execution_statuses = [_report_status(s.get("exec_status")) for s in summaries]
    execution_status = "Mixed" if len(set(execution_statuses)) > 1 else (execution_statuses[0] if execution_statuses else "Not available")
    qc_status = "Unable to continue" if error_count else ("QC complete" if map_count else "Not available")
    export_readiness = "Ready with limitations" if error_count or warning_count or not reference_available else "Ready"
    report_visibility = "Blinded" if blinded else "Unblinded"
    status_cards_html = "".join([
        _metric_card_html("Validation status", validation_status, f"{warning_count} warnings, {error_count} blocking errors"),
        _metric_card_html("Execution status", execution_status, "Result-only submissions do not require execution"),
        _metric_card_html("QC/reference status", qc_status, reference_status),
        _metric_card_html("Export readiness", export_readiness, report_visibility),
    ])
    key_metric_cards_html = "".join([
        _metric_card_html("Maps available", map_count, ", ".join(map_types) if map_types else "No map types detected"),
        _metric_card_html("Finite voxels", finite, "weighted across included maps"),
        _metric_card_html("NaN / Inf", f"{sum(int(af.get('nan_count') or 0) for af in fields)} / {sum(int(af.get('inf_count') or 0) for af in fields)}"),
        _metric_card_html("Negative voxels", negative),
        _metric_card_html("Reference availability", reference_status),
    ])
    # Generic QC bar charts were removed at researcher request: for single
    # submissions they are trivial, and they are not the submitted-vs-reference
    # agreement plot the challenge team wants (that is a future, mentor-defined
    # deliverable). The report stays table-focused.
    # Map preview thumbnails were removed from the report at researcher request
    # (kept in the interactive app, not the printable report).
    issues_html = _issue_rows_html(summaries, blinded=blinded)

    metadata_rows_html = []
    for idx, s in enumerate(summaries, start=1):
        af = s["analysis_fields"]
        unblinded_cells = "" if blinded else (
            f"<td>{_esc(s.get('team_name', ''))}</td>"
            f"<td>{_esc(s.get('contact_email', ''))}</td>"
        )
        metadata_rows_html.append(
            "<tr>"
            f"<td>{_esc(_submission_display_name(s, idx, blinded=blinded))}</td>"
            f"<td>{_esc(str(s.get('challenge_type') or '').upper() or 'Not available')}</td>"
            f"<td>{_esc(af.get('parameter_maps_detected') or 'Not available')}</td>"
            f"<td>{_esc(af.get('map_count') or 0)}</td>"
            f"{unblinded_cells}"
            "</tr>"
        )
    metadata_html = (
        "<table><thead><tr><th>Submission</th><th>Challenge</th><th>Map types</th><th>Maps</th>"
        + ("" if blinded else "<th>Team</th><th>Contact</th>")
        + "</tr></thead><tbody>"
        + "".join(metadata_rows_html)
        + "</tbody></table>"
    )

    rows_html = []
    map_details_html = []
    for idx, s in enumerate(summaries, start=1):
        af = s["analysis_fields"]
        analysis = s["nifti_analysis"] if isinstance(s.get("nifti_analysis"), dict) else {}
        detected = af["parameter_maps_detected"] or "Not available"
        reference_cell = _reference_status_label(af)
        notes = _research_notes(s, include_reference_note=False) or ""
        submission_label = _submission_display_name(s, idx, blinded=blinded)
        unblinded_cells = "" if blinded else (
            f"<td>{_esc(s.get('team_name', ''))}</td>"
            f"<td>{_esc(s.get('contact_email', ''))}</td>"
        )
        rows_html.append(
            "<tr>"
            f"<td>{_esc(submission_label)}</td>"
            f"{unblinded_cells}"
            f"<td>{_esc(s['challenge_type'])}</td>"
            f"<td>{_esc(detected)}</td>"
            f"<td>{_esc(af['map_count'])}</td>"
            f"<td>{_esc(_fmt_report_cell(af['finite_voxels_percent']))}</td>"
            f"<td>{_esc(af['nan_count'])} / {_esc(af['inf_count'])}</td>"
            f"<td>{_esc(_fmt_report_cell(af['negative_voxels_percent']))}</td>"
            + "".join(
                f"<td>{_esc(_fmt_report_cell((af.get('means_by_map_type') or {}).get(display)))}</td>"
                for display in report_mean_types
            )
            +
            f"<td>{_esc(reference_cell)}</td>"
            f"<td>{_esc(_fmt_report_cell(af['reference_mean_rmse']) if _reference_available(af) else 'Not available')}</td>"
            f"<td>{_esc(_fmt_report_cell(af['reference_mean_mae']) if _reference_available(af) else 'Not available')}</td>"
            f"<td>{_esc(_fmt_report_cell(af['reference_mean_bias']) if _reference_available(af) else 'Not available')}</td>"
            f"<td>{_esc(notes)}</td>"
            "</tr>"
        )

        maps = analysis.get("maps") if isinstance(analysis, dict) else []
        maps = maps if isinstance(maps, list) else []
        map_rows = []
        for map_idx, item in enumerate(maps, start=1):
            if not isinstance(item, dict):
                continue
            meta = item.get("metadata") or {}
            stats = item.get("stats") or {}
            map_label = f"Map {map_idx}" if blinded else item.get("file_name", f"Map {map_idx}")
            _shape = meta.get("shape") or []
            _vox = meta.get("voxel_size") or []
            _units = item.get("units") or "not provided"
            map_rows.append(
                "<tr>"
                f"<td>{_esc(map_label)}</td>"
                f"<td>{_esc(item.get('detected_map_type', 'Unknown'))}</td>"
                f"<td>{_esc(_units)}</td>"
                f"<td>{_esc(str(len(_shape)) + 'D' if _shape else 'n/a')}</td>"
                f"<td>{_esc('×'.join(str(x) for x in _shape) if _shape else 'n/a')}</td>"
                f"<td>{_esc('×'.join(str(x) for x in _vox) if _vox else 'n/a')}</td>"
                f"<td>{_esc(_fmt_report_cell(stats.get('finite_percent')))}</td>"
                f"<td>{_esc(meta.get('nan_count', 0))} / {_esc(meta.get('inf_count', 0))}</td>"
                f"<td>{_esc(_fmt_report_cell(stats.get('negative_voxel_percent')))}</td>"
                f"<td>{_esc(_fmt_report_cell(stats.get('mean')))}</td>"
                f"<td>{_esc(_fmt_report_cell(stats.get('coefficient_of_variation')))}</td>"
                "</tr>"
            )
        reference_rows = []
        if _reference_available(af):
            for ref_idx, ref_row in enumerate(af["reference_metric_rows"], start=1):
                reference_rows.append(
                    "<tr>"
                    f"<td>{_esc(ref_row.get('detected_map_type', ''))}</td>"
                    f"<td>{_esc(ref_row.get('scope', ''))}</td>"
                    f"<td>{_esc(_reference_status_label({'reference_scoring_status': ref_row.get('status'), 'reference_compared_map_count': 1 if ref_row.get('status') == 'compared' else 0}))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('rmse')))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('mae')))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('bias')))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('coefficient_of_variation')))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('correlation')))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('voxel_count'), 0))}</td>"
                    f"<td>{_esc(_fmt_report_cell(ref_row.get('excluded_voxel_count'), 0))}</td>"
                    "</tr>"
                )
        map_table = (
            "<h3>Submitted outputs <span style=\"font-weight:normal\">(per map — CBF and ATT separate)</span></h3>"
            "<table class=\"detail-table\"><thead><tr>"
            "<th>Map</th><th>Type</th><th>Units</th><th>Dims</th><th>Shape</th><th>Voxel size</th>"
            "<th>Finite voxels</th><th>NaN / Inf</th>"
            "<th>Negative voxels</th><th>Mean</th><th>CoV</th>"
            "</tr></thead><tbody>"
            + ("".join(map_rows) if map_rows else '<tr><td colspan="11">No readable NIfTI maps found.</td></tr>')
            + "</tbody></table>"
        )
        reference_table = ""
        if reference_rows:
            reference_table = (
                "<h3>Reference comparison <span style=\"font-weight:normal\">(per map and ROI — CBF and ATT are never combined)</span></h3>"
                "<table class=\"detail-table\"><thead><tr>"
                "<th>Map type</th><th>ROI</th><th>Reference status</th>"
                "<th>RMSE</th><th>MAE</th><th>Bias</th><th>Error CoV</th><th>Correlation</th>"
                "<th>Valid voxels</th><th>Excluded voxels</th>"
                "</tr></thead><tbody>"
                + "".join(reference_rows)
                + "</tbody></table>"
                + f"<p class=\"report-note\">{_esc(REPEATABILITY_UNAVAILABLE_NOTE)}</p>"
            )
        map_details_html.append(
            f"<details class=\"report-details\"><summary>Map-level results for {_esc(submission_label)}</summary>"
            f"{map_table}{reference_table}"
            "</details>"
        )

    table_html = (
        "<table><thead><tr>"
        "<th>Submission</th>"
        + ("" if blinded else "<th>Team</th><th>Contact</th>")
        + "<th>Challenge</th><th>Map types</th><th>Maps</th>"
        "<th>Finite voxels</th><th>NaN / Inf</th><th>Negative voxels</th>"
        + "".join(f"<th>Mean {_esc(display)}</th>" for display in report_mean_types)
        + "<th>Reference status</th>"
        "<th>RMSE</th><th>MAE</th><th>Bias</th><th>Notes</th>"
        "</tr></thead><tbody>" + "".join(rows_html) + "</tbody></table>"
    )
    detail_html = "".join(map_details_html)
    mean_summary_rows = "".join(
        f"<tr><td>Mean {_esc(display)}</td><td>{_esc(_fmt_report_cell(mean_by_map_type.get(display)))}</td></tr>"
        for display in report_mean_types
    )
    summary_items = [
        f"{n} submission{'s' if n != 1 else ''} reviewed.",
        f"{map_count} map{'s' if map_count != 1 else ''} included.",
        f"Detected map types: {', '.join(map_types) if map_types else 'Not available'}.",
    ]
    if is_mixed_challenge:
        summary_items.append(
            "This batch spans multiple challenges (" + ", ".join(challenges) + "). "
            "Results are grouped and aggregated per challenge — no cross-challenge totals are computed."
        )
    # The reference-availability note appears exactly once, in the summary.
    if not reference_available:
        summary_items.append(REFERENCE_UNAVAILABLE_NOTE)
    else:
        summary_items.append("Reference maps were available; reference metrics are included.")
        summary_items.append(REPEATABILITY_UNAVAILABLE_NOTE)
    if warning_count:
        summary_items.append(f"{warning_count} warning{'s' if warning_count != 1 else ''} reported.")
    if error_count:
        summary_items.append(f"{error_count} error{'s' if error_count != 1 else ''} reported.")
    summary_html = "".join(f"<li>{_esc(item)}</li>" for item in summary_items)
    notes = []
    if warning_count:
        notes.append("Warnings indicate files or metadata that may need review but did not prevent QC export.")
    if not notes:
        notes.append("No additional limitations were reported for this export.")
    notes_html = "".join(f"<p>{_esc(note)}</p>" for note in notes)

    blind_label = "Blinded report" if blinded else "Unblinded report"

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<title>OSIPI Perfusion Pipeline Report</title>
<style>
  body {{ font-family: Arial, Helvetica, "Segoe UI", sans-serif;
          color:#1a1a1a; max-width:960px; margin:28px auto; padding:0 26px; line-height:1.5; }}
  h1 {{ color:#5e42a6; font-size:1.4rem; margin:0 0 2px; }}
  h2 {{ color:#5e42a6; font-size:1.02rem; margin:22px 0 8px; border-bottom:1px solid #d9d2ec; padding-bottom:4px; }}
  h3 {{ color:#111; font-size:0.9rem; margin:14px 0 6px; }}
  .sub {{ color:#555; font-size:0.86rem; margin:0 0 14px; }}
  .meta {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:3px 24px; font-size:0.85rem; color:#222; margin:0 0 4px; }}
  .metric-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; margin:10px 0 12px; }}
  .metric-card {{ border:1px solid #d8d8df; border-radius:8px; padding:10px 11px; background:#fbfbfd; }}
  .metric-card strong {{ display:block; margin-top:4px; font-size:1rem; color:#111; }}
  .metric-label {{ display:block; color:#555; font-size:0.75rem; font-weight:700; text-transform:uppercase; letter-spacing:0; }}
  .metric-note {{ display:block; margin-top:3px; color:#666; font-size:0.72rem; line-height:1.35; }}
  .chart-grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:12px; margin-top:10px; }}
  .report-chart {{ margin:0; padding:10px; border:1px solid #d8d8df; border-radius:8px; background:#fff; }}
  .report-chart figcaption {{ font-weight:700; font-size:0.8rem; margin:0 0 7px; }}
  .report-chart svg {{ display:block; width:100%; height:auto; }}
  .svg-title {{ font-size:12px; font-weight:700; fill:#111; }}
  .svg-label,.svg-value {{ font-size:11px; fill:#333; }}
  .svg-track {{ fill:#f0f0f4; }}
  .compact-chart svg {{ height:28px; border-radius:5px; overflow:hidden; border:1px solid #ddd; }}
  .chart-legend {{ display:flex; flex-wrap:wrap; gap:6px 10px; margin-top:8px; color:#444; font-size:0.73rem; }}
  .chart-legend span {{ display:inline-flex; align-items:center; gap:4px; }}
  .chart-legend i {{ width:9px; height:9px; border-radius:2px; display:inline-block; }}
  .preview-gallery {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(130px,1fr)); gap:10px; margin-top:10px; }}
  .preview-card {{ margin:0; border:1px solid #d8d8df; border-radius:8px; overflow:hidden; background:#fff; }}
  .preview-card img {{ display:block; width:100%; height:120px; object-fit:contain; background:#10131a; }}
  .preview-card figcaption {{ padding:7px 8px; font-size:0.73rem; color:#444; }}
  .preview-card figcaption span {{ display:block; color:#666; }}
  .report-muted {{ color:#666; font-size:0.84rem; }}
  caption {{ text-align:left; font-weight:700; color:#111; margin-bottom:5px; }}
  .summary-list {{ margin:6px 0 0; padding-left:20px; font-size:0.9rem; color:#222; }}
  table {{ width:100%; border-collapse:collapse; margin-top:8px; font-size:0.8rem; }}
  th,td {{ text-align:left; padding:6px 8px; border:1px solid #d2d2d2; vertical-align:top; }}
  th {{ background:#f5f3fb; color:#111; font-weight:600; }}
  td {{ color:#222; }}
  table.kv {{ width:auto; min-width:340px; max-width:560px; }}
  table.kv td:first-child {{ color:#555; width:55%; }}
  .report-details {{ margin:10px 0; }}
  .report-details > summary {{ cursor:pointer; padding:6px 0; font-weight:600; font-size:0.85rem; color:#111; }}
  .detail-table {{ margin:6px 0 0; }}
  .notes {{ font-size:0.88rem; color:#222; }}
  .notes p {{ margin:0 0 6px; }}
  .limitations-list {{ margin:6px 0 0; padding-left:18px; font-size:0.86rem; color:#222; }}
  footer {{ margin-top:22px; color:#666; font-size:0.78rem; border-top:1px solid #d2d2d2; padding-top:8px; }}
  @media (max-width:640px) {{
    body {{ padding:0 14px; }}
    .meta {{ grid-template-columns:1fr; }}
    table {{ display:block; overflow-x:auto; }}
  }}
  @media print {{
    body {{ margin:0; max-width:none; padding:10mm 12mm; }}
    h2 {{ page-break-after:avoid; }}
    table {{ page-break-inside:auto; }}
    thead {{ display:table-header-group; }}
    tr {{ page-break-inside:avoid; }}
  }}
</style></head>
<body>
  <h1>OSIPI Perfusion Pipeline Report</h1>
  <p class="sub">{_esc(blind_label)} &middot; generated {_esc(generated)}</p>
  <div class="meta">
    <div><strong>Batch/session name:</strong> {_esc(session_name)}</div>
    <div><strong>Challenge type:</strong> {_esc(', '.join(challenges) if challenges else 'Not available')}</div>
    <div><strong>Number of submissions:</strong> {_esc(n)}</div>
    <div><strong>Number of maps:</strong> {_esc(map_count)}</div>
    <div><strong>Map types detected:</strong> {_esc(', '.join(map_types) if map_types else 'Not available')}</div>
    <div><strong>Reference status:</strong> {_esc(reference_status)}</div>
    <div><strong>Export date:</strong> {_esc(export_date)}</div>
    <div><strong>Pipeline version:</strong> {_esc(_pipeline_version())}</div>
    <div><strong>Configuration version:</strong> {_esc(_configuration_version())}</div>
  </div>
  <h2>Executive Summary</h2>
  <div class="metric-grid">{status_cards_html}</div>
  <ul class="summary-list">{summary_html}</ul>
  <h2>Key Metrics</h2>
  <div class="metric-grid">{key_metric_cards_html}</div>
  <h2>Submission Metadata</h2>
  {metadata_html}
  <h2>QC / Evaluation Summary</h2>
  <table class="kv"><tbody>
    <tr><td>Submissions reviewed</td><td>{_esc(n)}</td></tr>
    <tr><td>Maps included</td><td>{_esc(map_count)}</td></tr>
    <tr><td>Map types</td><td>{_esc(', '.join(map_types) if map_types else 'Not available')}</td></tr>
    <tr><td>Finite voxels</td><td>{_esc(_fmt_report_cell(finite))}</td></tr>
    <tr><td>NaN / Inf</td><td>{_esc(sum(int(af.get('nan_count') or 0) for af in fields))} / {_esc(sum(int(af.get('inf_count') or 0) for af in fields))}</td></tr>
    <tr><td>Negative voxels</td><td>{_esc(_fmt_report_cell(negative))}</td></tr>
    {"" if is_mixed_challenge else mean_summary_rows}
    <tr><td>Reference status</td><td>{_esc(reference_status)}</td></tr>
    {reference_summary_rows_html}
  </tbody></table>
  <h2>Per-Submission Results</h2>
  {table_html}
  {detail_html}
  <h2>Errors, Warnings, and Recommended Actions</h2>
  {issues_html}
  <h2>Notes / Limitations</h2>
  <div class="notes">{notes_html}</div>
  <ul class="limitations-list">
    <li>Basic NIfTI QC checks readability and generic voxel statistics; it is not full BIDS validation.</li>
    <li>Generic reference metrics are shown only when matching reference maps are available.</li>
    <li>Generic QC/reference metrics are not official OSIPI scores unless an official scoring provider is configured.</li>
    <li>Missing values are reported as Not available and are not converted to zero.</li>
  </ul>
  <footer>OSIPI Perfusion Pipeline &mdash; automated report.</footer>
</body></html>"""

    tag = (batch_id or submission_id or "report").replace("/", "_")
    return Response(
        content=html,
        media_type="text/html",
        headers={"Content-Disposition": f'inline; filename="osipi_report_{tag}.html"'},
    )


@app.get("/api/export/report/pdf")
def export_report_pdf(
    submission_id: Optional[str] = Query(None),
    batch_id:      Optional[str] = Query(None),
    blinded:       bool          = Query(True, description="True (default) to strip team/contact info"),
):
    """Generate a compact PDF evaluation report from existing report data."""
    sids = _collect_export_ids(batch_id, submission_id)
    with timed("report.pdf.gather", submission_count=len(sids)):
        summaries = [_gather_summary(sid) for sid in sids]
    tag = (batch_id or submission_id or "report").replace("/", "_")
    try:
        with timed("report.pdf.generate", submission_count=len(summaries)):
            pdf = generate_pdf_report(summaries, tag=tag, blinded=blinded)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="osipi_report_{tag}.pdf"'},
    )
