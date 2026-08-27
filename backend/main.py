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

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
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
    CONFIG_MANAGER_DIR,
    CONFIG_VERSIONS_DIR,
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
from services.configuration_manager_service import (
    activate_version,
    export_configuration,
    import_configuration,
    manager_state,
    preview_configuration,
    save_version,
    store_private_asset,
    test_configuration,
)
from services.provenance_service import analysis_provenance
from services.nifti_preview_service import (
    get_preview_download_path,
    get_preview_item,
    get_preview_png_path,
    list_submission_previews,
    public_preview_item,
    public_preview_manifest,
)
from services.pdf_report_service import (
    ROI_METHOD_TEXT,
    _build_report_model,
    affected_display,
    build_limitations,
    generate_pdf_report,
    export_filename,
    report_filename_tag,
)
from services.report_branding import (
    BRAND,
    SANS_STACK,
    lockup_data_uri,
    logo_data_uri,
    status_tone as report_status_tone,
)
from osipi_pipeline.scoring.descriptive_statistics import (
    CSV_COLUMNS as ROI_CSV_COLUMNS,
)
from services.report_figures import (
    bland_altman_figure,
    to_svg as figure_to_svg,
)
from osipi_pipeline.config.rules import (
    ConfigValidationError,
    app_settings,
    challenge_labels,
    challenge_types,
    default_challenge_type,
    default_scoring_map_type,
    expected_maps_by_challenge,
    mask_name_patterns,
    map_type_patterns,
    map_type_specs,
    private_path_parts,
    tuple_setting,
    validate_config_files,
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
# App startup: ensure all required directories exist
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app):
    for directory in [
        REFERENCE_DATA_DIR,
        OUTPUTS_DIR,
        INCOMING_DIR,
        EXTRACTED_DIR,
        SCORING_DIR,
        SCORING_OUTPUTS_DIR,
        SCORING_PACKAGES_DIR,
        OSIPI_TF62_DIR,
        CODECOLLECTION_DIR,
        CONFIG_MANAGER_DIR,
        CONFIG_VERSIONS_DIR,
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


@app.post("/api/config/reload")
def reload_config():
    """Validate config files and replace the cached rules without a restart."""
    try:
        rules, _settings = validate_config_files()
    except ConfigValidationError as exc:
        return {
            "reloaded": False,
            "error": str(exc),
            "detail": "The previous configuration is still in use.",
        }

    challenges = sorted(rules.get("challenges") or {})
    return {
        "reloaded": True,
        "challenges": challenges,
        "map_types": sorted(rules.get("map_types") or {}),
        "reloaded_at": datetime.now(timezone.utc).isoformat(),
    }


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


# ---------------------------------------------------------------------------
# Reviewer Configuration Manager
# ---------------------------------------------------------------------------


@app.get("/api/configuration-manager")
def configuration_manager(challenge_type: str = Query(...)):
    """Return an editable view, local versions, private assets and capabilities."""
    try:
        return manager_state(challenge_type)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/configuration-manager/test")
def configuration_manager_test(payload: Dict):
    """Test a draft without changing active rules or scoring configuration."""
    return test_configuration(payload)


@app.post("/api/configuration-manager/preview")
def configuration_manager_preview(payload: Dict):
    try:
        return preview_configuration(payload)
    except (ValueError, ConfigValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/api/configuration-manager/versions")
def configuration_manager_save(payload: Dict):
    try:
        return save_version(payload)
    except (ValueError, ConfigValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class ConfigurationActivateRequest(BaseModel):
    challenge_type: str
    version_id: str


@app.post("/api/configuration-manager/activate")
def configuration_manager_activate(req: ConfigurationActivateRequest):
    try:
        return activate_version(req.challenge_type, req.version_id)
    except (OSError, ValueError, ConfigValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/configuration-manager/export")
def configuration_manager_export(
    challenge_type: str = Query(...),
    version_id: Optional[str] = Query(None),
):
    try:
        payload, filename = export_configuration(challenge_type, version_id)
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return Response(
        content=payload,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/configuration-manager/import")
async def configuration_manager_import(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Configuration import must be a ZIP archive.")
    tmp_fd, tmp_name = tempfile.mkstemp(suffix=".zip")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(tmp_fd, "wb") as handle:
            while chunk := await file.read(65536):
                handle.write(chunk)
        return import_configuration(tmp_path)
    except (OSError, ValueError, ConfigValidationError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/configuration-manager/assets/upload")
async def configuration_manager_asset_upload(
    challenge_type: str = Form(...),
    asset_kind: str = Form(...),
    file: UploadFile = File(...),
):
    content = await file.read()
    try:
        return store_private_asset(challenge_type, asset_kind, file.filename or "", content)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
# Submission intake: upload and extract a ZIP
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
    stays correctly scoped downstream, the pipeline never merges challenges.

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
# Validation: accepts submission_id, resolves folder internally
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

    Does not require NIfTI output maps, they will be generated by execution.
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
# Shared helper: find validation JSON files in both storage locations
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
        # Use an exact stem because one batch id can be a prefix of another.
        wanted = {
            f"{submission_id}_validation",
            f"{submission_id.replace('/', '_').replace(chr(92), '_')}_validation",
        }
        unique = [f for f in unique if f.stem in wanted]

    return unique


def _msg(item) -> str:
    """Extract a plain string message from either a string or a {message: ...} dict."""
    if isinstance(item, dict):
        return item.get("message", str(item))
    return str(item or "")


# ---------------------------------------------------------------------------
# Outputs: list saved validation results
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
# Export: validation results and manifests
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

    # CSV format: one summary row per submission
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
# Legacy validation-review ordering (not scientific scoring or ranking)
# ---------------------------------------------------------------------------


@app.get("/api/rankings")
def get_rankings():
    """Return the legacy validation review order.

    The compatibility route name is retained for existing clients. Its order
    is based only on validation state and must never be presented as an OSIPI
    scientific score or challenge ranking.
    """
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

    return {
        "rankings": ranked,
        "count": len(ranked),
        "ordering_basis": "validation review priority",
        "official_ranking": False,
        "deprecation_note": (
            "Compatibility endpoint only; official OSIPI challenge ranking "
            "is not configured."
        ),
    }


# ---------------------------------------------------------------------------
# Batch upload: auto-detects single vs. multi-submission ZIP
# ---------------------------------------------------------------------------


@app.post("/api/upload-batch")
async def upload_batch(file: UploadFile = File(...)):
    """Accept a ZIP that may contain multiple team submissions.

    Streams the upload to disk in 64 KB chunks, the full file is never held
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
# Batch validation: validate multiple submission IDs in one request
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
# Batch export: blinded and unblinded CSV
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
# NIfTI viewer: list and serve NIfTI files for browser-side rendering
# ---------------------------------------------------------------------------

NIFTI_SUFFIXES = tuple_setting("nifti_suffixes")
PRIVATE_NIFTI_PATH_PARTS = private_path_parts()
PRIVATE_NIFTI_NAME_PATTERNS = mask_name_patterns()


def _is_private_nifti(path: Path, folder: Path) -> bool:
    """Reject reference assets and masks at every browser-facing NIfTI route."""
    try:
        relative = path.relative_to(folder)
    except ValueError:
        return True
    if {part.lower() for part in relative.parts}.intersection(PRIVATE_NIFTI_PATH_PARTS):
        return True
    name = path.name.lower()
    return any(pattern in name for pattern in PRIVATE_NIFTI_NAME_PATTERNS)


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
        if p.is_file()
        and p.name.lower().endswith(NIFTI_SUFFIXES)
        and not _is_private_nifti(p, folder)
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

    try:
        target.relative_to(folder.resolve())
    except ValueError:
        raise HTTPException(status_code=403, detail="Access denied.")
    if not target.exists() or not target.is_file():
        raise HTTPException(status_code=404, detail="File not found.")
    if not target.name.lower().endswith(NIFTI_SUFFIXES):
        raise HTTPException(status_code=400, detail="Only NIfTI files can be served.")
    if _is_private_nifti(target, folder.resolve()):
        raise HTTPException(status_code=403, detail="Private reference or mask files cannot be served.")

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
# Docker execution: build image and run submission
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

    - ``success: false``, pre-flight error (no Dockerfile, bad submission_id,
      Docker not installed).  Returns HTTP 400.
    - ``success: true, passed: false``, execution ran but failed (build error,
      non-zero exit, timeout).  Returns HTTP 200 with full result + logs so the
      UI can display what went wrong.

    Resource constraints applied to every container:

    - ``--network none``, no outbound internet access.
    - ``--security-opt no-new-privileges``, no privilege escalation.
    - ``--memory 4g`` and ``--cpus 2.0``, resource limits.
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
# Execution exports: blinded and unblinded CSV
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
            # Submission has not been run yet, include a placeholder row
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


_PRIVATE_SCORING_FIELDS = {
    "path", "reference_root", "reference_path", "mask_path", "submitted_path", "source_path",
}


def _public_scoring_result(value):
    """Recursively remove server filesystem paths from public API payloads."""
    if isinstance(value, dict):
        return {
            key: _public_scoring_result(item)
            for key, item in value.items()
            if key not in _PRIVATE_SCORING_FIELDS
        }
    if isinstance(value, list):
        return [_public_scoring_result(item) for item in value]
    if isinstance(value, tuple):
        return [_public_scoring_result(item) for item in value]
    return value


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
    for all registered scoring providers, useful for the Score step UI to show
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
        return _public_scoring_result(
            batch_scoring_status(submission_ids, challenge_type, map_type, provider_id=provider_id)
        )

    if not submission_id:
        # Providers-only request: no submission needed
        return {"providers": all_providers_status()}

    return _public_scoring_result(scoring_status(
        submission_id.strip(),
        challenge_type.strip(),
        map_type.strip(),
        provider_id=provider_id,
    ))


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
    return _public_scoring_result(result)


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
    return _public_scoring_result({
        "batch_id":   req.batch_id,
        "total":      len(results),
        "scored":     scored,
        "results":    results,
    })


@app.get("/api/leaderboard")
def get_leaderboard():
    """Return submissions with stored analysis results (summary, no ranking).

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
# Scoring Package Management: admin/reviewer endpoints
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
    providers = all_providers_status()
    enriched: dict[str, dict] = {}
    for challenge, raw_entry in cfg.items():
        entry = dict(raw_entry or {})
        mode = str(entry.get("mode") or "none")
        provider = None
        if mode == "builtin":
            provider = next((
                item for item in providers
                if item.get("source") == "builtin"
                and not item.get("not_for_scoring")
                and str(item.get("challenge_type") or "").lower() == str(challenge).lower()
            ), None)
        elif mode == "custom" and entry.get("package_id"):
            provider = next((
                item for item in providers
                if item.get("provider_id") == entry.get("package_id")
            ), None)
        entry.update({
            "provider_id": provider.get("provider_id") if provider else entry.get("package_id"),
            "provider_name": provider.get("provider_name") if provider else entry.get("package_name"),
            "official": bool(provider and provider.get("official")),
            "provider_ready": bool(
                provider and provider.get("status") in {"ready", "dev_data_available"}
            ),
        })
        enriched[str(challenge)] = entry
    return {
        "active": enriched,
        "active_config": enriched,
        "packages": list_packages(),
        "providers": providers,
    }


class ScoringSetActiveRequest(BaseModel):
    challenge_type: str
    mode: str                    # "none" | "builtin" | "custom"
    package_id: Optional[str] = None  # required when mode="custom"


@app.post("/api/scoring/set-active")
def scoring_set_active(req: ScoringSetActiveRequest):
    """Set the active scoring mode for a challenge type.

    mode="none"   , scoring disabled; app shows "Scoring not configured"
    mode="builtin", use the single compatible registered built-in provider
    mode="custom" , use an uploaded package (package_id required)
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


@app.get("/api/export-roi-descriptive")
def export_roi_descriptive(
    submission_id: Optional[str] = Query(None),
    batch_id:      Optional[str] = Query(None),
    blinded:       bool          = Query(False, description="True to use a neutral download filename"),
):
    """Export within-ROI parameter-map descriptive statistics as CSV.

    One row per scan and ROI. Values are the canonical records computed once
    during scoring, this endpoint reads them, it does not recompute.

    Kept separate from the reference-error CSV: within-scan spatial spread
    and error-against-reference are different quantities, and merging them
    into one file would invite them being read as the same thing.
    """
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(list(ROI_CSV_COLUMNS))

    sids = _collect_export_ids(batch_id, submission_id)
    for sid in sids:
        for record in _roi_descriptive_records(sid):
            writer.writerow([
                "" if record.get(column) is None else record.get(column)
                for column in ROI_CSV_COLUMNS
            ])

    # The ROI columns carry scan identity only (dataset/participant/repeat/site),
    # never team identity, so the rows are safe in either mode; ``blinded`` only
    # governs the download filename.
    tag = report_filename_tag(
        (batch_id or submission_id or "export").replace("/", "_"), blinded=blinded)
    # A header-only CSV is returned when there are no records, rather than a
    # 404: "no ROI statistics" is a valid outcome, not a missing resource.
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="roi_descriptive_statistics_{tag}.csv"'},
    )


def _roi_descriptive_records(submission_id: str) -> list[dict]:
    """Canonical ROI records for one submission. Never recomputes."""
    try:
        summary = _gather_summary(submission_id)
    except Exception:
        return []
    analysis = summary.get("nifti_analysis")
    analysis = analysis if isinstance(analysis, dict) else {}
    scoring = analysis.get("reference_scoring")
    scoring = scoring if isinstance(scoring, dict) else {}
    records = scoring.get("roi_descriptive_statistics")
    return [r for r in (records or []) if isinstance(r, dict)]


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

def submission_exists(submission_id: str) -> bool:
    """True when anything on disk belongs to this exact submission id.

    A submission counts as present if it has been extracted, validated,
    executed, or scored. Any one of those is enough, an export may legitimately
    run before some stages have.
    """
    sid = (submission_id or "").strip()
    if not sid:
        return False
    if (EXTRACTED_DIR / sid).is_dir():
        return True
    if _find_validation_files(sid):
        return True
    if _exec_result_path(sid).exists():
        return True
    # No bare try/except here: swallowing the lookup would silently downgrade
    # every scored-but-unvalidated submission to "not found".
    return load_scoring_result(sid) is not None


def _collect_export_ids(batch_id: Optional[str], submission_id: Optional[str]) -> List[str]:
    """Resolve known batch or submission ids, returning 404 for unknown ids."""
    if batch_id:
        batch_result = find_batch_result(batch_id)
        if not batch_result:
            raise HTTPException(status_code=404, detail=f"Batch {batch_id!r} not found.")
        return [r["submission_id"] for r in (batch_result.get("results") or [])]
    if submission_id:
        sid = submission_id.strip()
        if not submission_exists(sid):
            raise HTTPException(
                status_code=404, detail=f"Submission {sid!r} not found.")
        return [sid]
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
    # No execution result on disk: infer from validation run-readiness
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
    "Compatible reference maps were not available, so reference-comparison "
    "metrics were not calculated."
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
        # Canonical role-based counts, computed once during validation.
        "counts":          (val or {}).get("counts") or {},
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
# Long-format (tidy) researcher CSV, one row per
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
            "",  # contact_name, not captured in current submission metadata
            s.get("contact_email", ""),
            "",  # institution, not captured
            (s.get("mode") or "local"),  # submission_source (best available)
            s.get("source_folder", ""),  # original_archive_name
            "",  # repository_url, not captured for local uploads
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
                        blinded_id, challenge, subject_id, "",  # session_or_repeat_id, no repeats yet
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
    map × ROI × metric, CBF and ATT stay in separate rows and no cross-map or
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
        tag = report_filename_tag(
            (batch_id or submission_id or "export").replace("/", "_"), blinded=blinded)
        return Response(
            content=json.dumps({
                "report_type": "blinded" if blinded else "unblinded",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "submission_count": len(summaries),
                "analysis_provenance": analysis_provenance(
                    [item.get("challenge_type") for item in summaries]
                ),
                "submissions": summaries,
                "limitations": [
                    "BIDS checking covers layout and naming only: the dataset description, subject and session directories, filename entities and their order. It is not the full BIDS specification.",
                    "Generic QC/reference metrics are not official OSIPI scores unless an official provider is configured.",
                ],
            }, indent=2),
            media_type="application/json",
            headers={"Content-Disposition":
                     f'attachment; filename="{export_filename("osipi_combined", tag, blinded=blinded, extension="json")}"'},
        )

    # ── Long (tidy) CSV: one row per submission × subject × session × map × ROI × metric ──
    if csv_shape == "long":
        header, long_rows = _long_csv_rows(gathered_by_sid, sids, blinded)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(header)
        for r in long_rows:
            w.writerow(r)
        tag = report_filename_tag(
            (batch_id or submission_id or "export").replace("/", "_"), blinded=blinded)
        return Response(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition":
                     f'attachment; filename="{export_filename("osipi_results_long", tag, blinded=blinded, extension="csv")}"'},
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
            # Nothing recorded for this submission yet, still emit a row so the
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

    tag = report_filename_tag(
        (batch_id or submission_id or "export").replace("/", "_"), blinded=blinded)
    return Response(
        content=output.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition":
                 f'attachment; filename="{export_filename("osipi_combined", tag, blinded=blinded, extension="csv")}"'},
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


def _status_chip_html(label: str, tone: str | None = None) -> str:
    """Render a status as a coloured dot and plain text."""
    tone = tone or report_status_tone(label)
    return (
        f'<span class="stat stat-{_esc(tone)}">'
        f'<span class="dot" aria-hidden="true"></span>{_esc(label)}</span>'
    )




def _issue_rows_html(issue_rows: list[list[str]]) -> str:
    rows: list[str] = []
    for severity, label, text, affected, action in issue_rows:
        rows.append(
            "<tr>"
            f"<td>{_status_chip_html(severity)}</td><td>{_esc(label)}</td><td>{_esc(text)}</td><td>{_esc(affected)}</td><td>{_esc(action)}</td>"
            "</tr>"
        )
    if not rows:
        rows.append(
            '<tr><td colspan="5">'
            + _status_chip_html("None recorded", "ok")
            + "</td></tr>"
        )
    return (
        '<div class="table-wrap"><table>'
        "<caption>Errors and warnings raised during validation, with the "
        "action required before the submission can be shared.</caption>"
        '<thead><tr><th>Severity</th><th>Submission</th><th>Message</th>'
        "<th>Affected file</th><th>Recommended action</th></tr></thead>"
        "<tbody>" + "".join(rows) + "</tbody></table></div>"
    )


CONTENTS_CAPTION = (
    "What the submission contains, grouped by dataset and type. "
    "Parameter maps, fitted signals and documents are counted separately; "
    "organiser reference data is not counted as submitted content."
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
        (f"Batch ({len(summaries)} submissions)" if blinded else f"Batch {batch_id}") if batch_id
        else (_submission_display_name(summaries[0], 1, blinded=blinded) if len(summaries) == 1 else "Export session")
    )

    # Build the shared model once so HTML and PDF use the same scoped results.
    report_model = _build_report_model(
        summaries, tag=(batch_id or submission_id or "report"), blinded=blinded
    )


    # No QC bar charts and no map thumbnails: the researchers asked for a
    # table-focused printable report. Previews stay in the interactive app.
    issues_html = _issue_rows_html(report_model.get("issues") or [])

    reference_comparison_rows_html = []
    for idx, summary in enumerate(summaries, start=1):
        analysis_fields = summary["analysis_fields"]
        if not _reference_available(analysis_fields):
            continue
        submission_label = _submission_display_name(
            summary, idx, blinded=blinded
        )
        for reference_row in analysis_fields["reference_metric_rows"]:
            reference_comparison_rows_html.append(
                "<tr>"
                f"<td>{_esc(submission_label)}</td>"
                f"<td>{_esc(reference_row.get('detected_map_type', ''))}</td>"
                f"<td>{_esc(reference_row.get('scope', ''))}</td>"
                f"<td>{_esc(_fmt_report_cell(reference_row.get('rmse')))}</td>"
                f"<td>{_esc(_fmt_report_cell(reference_row.get('mae')))}</td>"
                f"<td>{_esc(_fmt_report_cell(reference_row.get('bias')))}</td>"
                f"<td>{_esc(_fmt_report_cell(reference_row.get('coefficient_of_variation')))}</td>"
                f"<td>{_esc(_fmt_report_cell(reference_row.get('correlation')))}</td>"
                f"<td>{_esc(_fmt_report_cell(reference_row.get('voxel_count'), 0))}</td>"
                "</tr>"
            )
    _main_map_headers = report_model.get("main_map_metric_headers") or []
    _main_map_rows = report_model.get("main_map_metric_rows") or []
    main_map_results_html = (
        '<div class="table-wrap"><table><thead><tr>'
        + "".join(f"<th>{_esc(value)}</th>" for value in _main_map_headers)
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
            for row in _main_map_rows
        )
        + "</tbody></table></div>"
        if _main_map_rows else
        '<p class="report-note">No readable map metrics were available.</p>'
    )
    reference_comparison_html = (
        '<div class="table-wrap"><table><thead><tr>'
        '<th>Submission</th><th>Map</th><th>ROI</th><th>RMSE</th>'
        '<th>MAE</th><th>Bias</th><th>Error CoV</th><th>Corr.</th><th>Valid voxels</th>'
        '</tr></thead><tbody>'
        + "".join(reference_comparison_rows_html)
        + "</tbody></table></div>"
        if reference_comparison_rows_html else
        '<p class="report-note">No compatible reference comparison was available.</p>'
    )
    challenge_reference_rows = []
    for challenge, metrics in (
        report_model.get("reference_metrics_by_challenge") or {}
    ).items():
        for metric, value in metrics.items():
            challenge_reference_rows.append(
                f"<tr><th>{_esc(challenge)} {_esc(metric)}</th>"
                f"<td>{_esc(_fmt_report_cell(value))}</td></tr>"
            )
    challenge_reference_summary_html = (
        '<div class="table-wrap compact-kv"><table class="kv"><tbody>'
        + "".join(challenge_reference_rows)
        + "</tbody></table></div>"
        if len(challenges) > 1 and challenge_reference_rows else ""
    )
    # Only the caveats that apply to this run, shared with the PDF so both
    # reports carry identical wording.
    limitations_html = "".join(
        f"<li>{_esc(item)}</li>"
        for item in report_model.get("limitations", [])
    )

    blind_label = "Blinded report" if blinded else "Unblinded report"

    # Embedded as a data URI, not linked to /static, because the report is
    # downloaded and emailed and a link would 404 once it leaves the server.
    # Full lockup first, then the mark, then a text masthead.
    lockup_uri = lockup_data_uri(760)
    logo_uri = None if lockup_uri else logo_data_uri(300)
    if lockup_uri:
        logo_html = (
            f'<img class="lockup" src="{lockup_uri}" '
            f'alt="OSIPI, Open Science Initiative for Perfusion Imaging">'
        )
    elif logo_uri:
        logo_html = (
            f'<img class="mark" src="{logo_uri}" alt="OSIPI logo">'
            '<div class="wordmark">OSIPI<span>Perfusion pipeline</span></div>'
        )
    else:
        logo_html = '<div class="wordmark">OSIPI<span>Perfusion pipeline</span></div>'

    # Rendered from the same pre-formatted records summarized by the PDF.
    # Nothing is recomputed or refiltered here; HTML keeps the complete table
    # while the printable report carries a compact availability summary.
    # Display rows, not the full rows: columns whose value never varies are
    # lifted into a caption instead of repeating identically down the table.
    # The CSV export is built separately from the records and keeps everything.
    _roi_rows = report_model.get("roi_descriptive_display_rows") or []
    _roi_headers = report_model.get("roi_descriptive_display_headers") or []
    _roi_scope = report_model.get("roi_descriptive_scope") or {}
    _roi_summary = report_model.get("roi_descriptive_summary") or {}
    _roi_caption = (
        f"Within-ROI parameter-map statistics: "
        f"{_roi_summary.get('available_rows', 0)} of "
        f"{_roi_summary.get('total_rows', 0)} scan-ROI combinations available. "
        if _roi_rows else
        "Within-ROI parameter-map statistics. None were available for this "
        "submission. "
    ) + ROI_METHOD_TEXT
    # Resolved by name. Fixed offsets were wrong the moment a column could be
    # dropped, and the configurable metric list already changes both the count
    # and the order of these columns.
    _ROI_TEXT_COLUMNS = {"Dataset", "Participant", "Repeat", "Site",
                         "Map", "ROI", "Units", "Status"}
    _roi_numeric = {
        index for index, header in enumerate(_roi_headers)
        if str(header) not in _ROI_TEXT_COLUMNS
    }
    _num_attr = ' class="num"'

    def _roi_cell(tag: str, index: int, value: object) -> str:
        attr = _num_attr if index in _roi_numeric else ""
        return f"<{tag}{attr}>{_esc(value)}</{tag}>"

    # Whatever was identical on every row, said once instead of repeated.
    _roi_scope_html = (
        '<p class="roi-scope">'
        + " · ".join(f"{_esc(label)}: <strong>{_esc(value)}</strong>"
                     for label, value in _roi_scope.items())
        + "</p>"
    ) if _roi_scope else ""

    roi_table_html = (
        _roi_scope_html
        + '<div class="table-wrap"><table>'
        + f"<caption>{_esc(_roi_caption)}</caption><thead><tr>"
        + "".join(_roi_cell("th", i, h) for i, h in enumerate(_roi_headers))
        + "</tr></thead><tbody>"
        + "".join(
            "<tr>"
            + "".join(_roi_cell("td", i, cell) for i, cell in enumerate(row))
            + "</tr>"
            for row in (_roi_rows or [["—"] * max(1, len(_roi_headers))])
        )
        + "</tbody></table></div>"
    )

    def _prototype_table(headers, rows, caption):
        if not rows:
            return ""
        return (
            '<div class="table-wrap"><table><caption>' + _esc(caption)
            + '</caption><thead><tr>'
            + "".join(f"<th>{_esc(value)}</th>" for value in headers)
            + "</tr></thead><tbody>"
            + "".join(
                "<tr>" + "".join(f"<td>{_esc(cell)}</td>" for cell in row) + "</tr>"
                for row in rows
            )
            + "</tbody></table></div>"
        )

    grouped_table_html = _prototype_table(
        report_model.get("grouped_roi_headers") or [],
        report_model.get("grouped_roi_rows") or [],
        "Descriptive grouping of scan-level ROI medians. Pair differences are shown only for two clearly matched repeats or sites; these are not ICC, formal repeatability, pass/fail, or ranking.",
    )
    rss_table_html = _prototype_table(
        report_model.get("dce_rss_headers") or [],
        report_model.get("dce_rss_rows") or [],
        "Residual Sum of Squares (RSS): raw voxelwise sum across time of (measured − modelled)², summarized by region. This is not deviance or official scoring.",
    )
    # The per-region comparison, from the same model rows the PDF renders.
    _region_rows = report_model.get("reference_region_rows") or []
    _region_headers = list(report_model.get("reference_region_headers") or [])
    if _region_rows and len({r[0] for r in _region_rows}) == 1:
        _region_headers, _region_rows = _region_headers[1:], [r[1:] for r in _region_rows]
    region_table_html = _prototype_table(
        _region_headers, _region_rows,
        "Comparison against ground truth within each region. A whole-image "
        "figure can hide opposite regional errors that cancel, so the whole "
        "image is shown as its own row.",
    )

    prototype_analysis_html = (
        grouped_table_html + rss_table_html
        if grouped_table_html or rss_table_html else ""
    )

    # Same rows the PDF renders, from the same model key, so the two formats
    # cannot drift apart.
    _header_check_rows = report_model.get("header_check_rows") or []
    header_check_html = _prototype_table(
        report_model.get("header_check_headers") or [],
        _header_check_rows,
        "Submitted map headers compared against the reference: shape, voxel "
        "size, orientation and data type. A map can be the right shape and "
        "score plausibly while being flipped, which no comparison metric "
        "reveals. Fields that neither file declares read as not verified.",
    )
    if any(row and row[-1] == "Geometry differs" for row in _header_check_rows):
        header_check_html += (
            '<p class="report-note">One or more maps differ from the reference '
            "in shape, voxel size or orientation. Comparison metrics for those "
            "maps are not reliable until the difference is explained.</p>"
        )

    # The masthead carries only immediate report context. Versions, packages,
    # and reference identifiers live once in the collapsed Provenance section.
    meta_items = [
        ("Report type", blind_label),
        ("Generated", generated),
    ]
    if not blinded and len(summaries) == 1:
        # A single-submission report does not render the batch overview table,
        # so organiser identity belongs in the metadata block.  Blinded output
        # never enters this branch.
        meta_items[1:1] = [
            ("Team", summaries[0].get("team_name") or "Not provided"),
            ("Contact", summaries[0].get("contact_email") or "Not provided"),
        ]
    meta_html = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_esc(value)}</dd>"
        for label, value in meta_items
    )
    provenance_labels = [
        ("Challenge", "challenge"),
        ("Configuration version", "challenge_configuration"),
        ("Scoring package", "scoring_package"),
        ("Pipeline version", "pipeline_version"),
        ("Reference dataset", "reference_dataset"),
        ("Analysis date", "analysis_date"),
    ]
    provenance_html = "".join(
        f"<dt>{_esc(label)}</dt><dd>{_esc(report_model['analysis_provenance'].get(key, 'not available'))}</dd>"
        for label, key in provenance_labels
    )
    review_status_html = "".join(
        f"<div class=\"review-item\"><span>{_esc(label)}</span>"
        f"<strong>{_esc(value)}</strong></div>"
        for label, value in report_model.get("review_statuses", {}).items()
    )
    executive_metrics_html = "".join(
        f"<tr><td>{_esc(label)}</td><td>{_esc(value)}</td></tr>"
        for label, value in report_model.get("executive_metrics", {}).items()
    )
    reference_note_html = (
        "" if reference_available else
        '<p class="report-note">No compatible reference was provided. '
        "Reference comparison is not available for this report.</p>"
    )

    # ── Figures ───────────────────────────────────────────────────────────
    _figure_blocks = []
    # One figure per parameter: Bland-Altman. The RMSE/MAE dot plot, the
    # identity plot, and the finite-voxel plot were cut, the first two
    # restate what this figure and the results table already show, and a
    # three-point plot of values between 98% and 99% earns nothing.
    # Reuse the model's points and units rather than recomputing them.
    _units_by_map = report_model.get("map_units") or {}
    for _map_type, _pts in (report_model.get("agreement_points") or {}).items():
        _units = _units_by_map.get(_map_type, "map units")
        _ba = bland_altman_figure(_pts, units=_units, width=430)
        if not _ba:
            continue
        _limits = _ba.get("limits")
        _interval = (
            f" Limits of agreement: {_fmt_report_num(_limits[0], 2)} to "
            f"{_fmt_report_num(_limits[1], 2)} {_units}." if _limits else ""
        )
        _figure_blocks.append((
            f"Figure {len(_figure_blocks) + 1}. Agreement between submitted and "
            f"reference {_map_type}. Each point is one region of one "
            "submission; the solid line is zero bias and the dashed lines are "
            "the pooled 95% limits of agreement." + _interval,
            figure_to_svg(_ba),
        ))
    figures_html = ""
    if _figure_blocks:
        figures_html = (
            '<div class="figure-grid">'
            + "".join(
                f'<figure class="fig-block">{svg}'
                f"<figcaption>{_esc(cap)}</figcaption></figure>"
                for cap, svg in _figure_blocks
            )
            + "</div>"
        )

    html = f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OSIPI Perfusion Pipeline &mdash; Submission Review Report &mdash; {_esc(session_name)}</title>
<style>
  :root {{
    --ink:{BRAND['ink']}; --ink-soft:{BRAND['ink_soft']}; --muted:{BRAND['muted']};
    --subtle:{BRAND['subtle']}; --rule:{BRAND['rule']}; --hairline:{BRAND['hairline']};
    --faint:{BRAND['faint']};
    --ok:{BRAND['ok']}; --warn:{BRAND['warn']}; --bad:{BRAND['bad']};
    --neutral:{BRAND['neutral']};
    --sans:"Inter", "Montserrat", {SANS_STACK};
    --serif:var(--sans);
    --mono:var(--sans);
  }}
  *, *::before, *::after {{ box-sizing:border-box; }}
  body {{
    margin:0; padding:32px 20px 64px; background:#f4f4f2; color:var(--ink);
    font-family:var(--sans); font-size:14px; line-height:1.6;
    -webkit-font-smoothing:antialiased;
  }}
  .sheet {{ max-width:940px; margin:0 auto; background:#fff; padding:44px 56px 52px; }}

  /* ── Masthead ─────────────────────────────────────────────────────────
     Running head, then the thick/thin rule pair that signals a journal
     page, then the title block. No colour, no fills. */
  .runhead {{
    display:flex; align-items:center; gap:19px;
    padding-bottom:15px; border-bottom:1px solid var(--rule);
  }}
  .lockup {{ display:block; width:340px; max-width:60%; height:auto; flex:none; }}
  .mark {{ display:block; width:78px; height:78px; object-fit:contain; flex:none; }}
  .wordmark {{
    font-family:var(--sans); font-size:15px; font-weight:700; letter-spacing:.08em; text-transform:uppercase;
    color:var(--ink); line-height:1.45;
  }}
  .wordmark span {{ display:block; letter-spacing:.04em; color:var(--muted); font-size:11.5px; font-weight:500; }}
  .runhead .issue {{
    margin-left:auto; text-align:right; font-size:11px; color:var(--muted);
    font-variant-numeric:tabular-nums; letter-spacing:.03em; line-height:1.5;
  }}
  .titleblock {{ padding-top:22px; }}
  h1 {{
    font-family:var(--sans); font-size:32px; font-weight:700;
    letter-spacing:-.02em; line-height:1.18; margin:0;
  }}
  .deck {{
    font-family:var(--sans); font-size:14px;
    color:var(--muted); margin:7px 0 0;
  }}
  .lead {{
    font-family:var(--sans); font-size:15px; line-height:1.55;
    color:var(--ink-soft); margin:17px 0 0; max-width:62ch;
  }}

  /* ── Status line, dots, not pills ────────────────────────────────── */
  .statusline {{
    display:flex; flex-wrap:wrap; gap:8px 26px; margin:20px 0 0;
    padding:11px 0; border-top:1px solid var(--hairline);
    border-bottom:1px solid var(--hairline);
  }}
  .stat-item {{ display:inline-flex; align-items:baseline; gap:7px; font-size:13px; }}
  .stat-key {{
    font-family:var(--sans); font-size:11px; font-weight:600; letter-spacing:.02em; color:var(--subtle);
  }}
  .stat {{ display:inline-flex; align-items:baseline; gap:5px; color:var(--ink); }}
  .stat .dot {{
    width:7px; height:7px; border-radius:50%; background:var(--neutral);
    display:inline-block; flex:none; transform:translateY(-1px);
  }}
  .stat-ok .dot {{ background:var(--ok); }}
  .stat-warn .dot {{ background:var(--warn); }}
  .stat-bad .dot {{ background:var(--bad); }}
  .stat-ok {{ color:var(--ok); }}
  .stat-warn {{ color:var(--warn); }}
  .stat-bad {{ color:var(--bad); }}

  /* ── Report metadata: a run-in definition list, not a grid of boxes ── */
  .meta {{
    display:grid; grid-template-columns:auto 1fr auto 1fr; gap:5px 14px;
    margin:16px 0 0; font-size:12.5px;
  }}
  .meta dt {{
    font-family:var(--sans); font-size:11px; font-weight:600; letter-spacing:.01em;
    color:var(--subtle); white-space:nowrap; padding-top:1px;
  }}
  .meta dd {{ margin:0; color:var(--ink); word-break:break-word; }}

  /* ── Key figures: a rule band, no cards ───────────────────────────── */
  .figures {{
    display:flex; flex-wrap:wrap; margin:26px 0 0;
    border-top:0.75px solid var(--rule); border-bottom:1px solid var(--faint);
  }}
  .fig {{ flex:1 1 110px; padding:11px 16px 11px 0; }}
  .fig + .fig {{ padding-left:16px; border-left:1px solid var(--faint); }}
  .fig-label {{
    display:block; font-family:var(--sans); font-size:11px; font-weight:600; letter-spacing:.01em;
    color:var(--subtle);
  }}
  .fig-value {{
    display:block; font-size:21px; line-height:1.25; margin-top:3px;
    font-variant-numeric:tabular-nums; letter-spacing:-.01em;
  }}
  .fig-note {{ display:block; font-size:11px; color:var(--muted); margin-top:1px; }}
  .review-grid {{
    display:grid; grid-template-columns:repeat(4,minmax(0,1fr));
    margin:2px 0 18px; border-top:1px solid var(--rule);
    border-bottom:1px solid var(--hairline); overflow:hidden;
  }}
  .review-item {{ padding:11px 14px 11px 0; min-width:0; }}
  .review-item + .review-item {{ padding-left:14px; border-left:1px solid var(--faint); }}
  .review-item strong {{ display:block; font-size:14px; font-weight:600; margin-top:2px; overflow-wrap:anywhere; }}
  .review-item span {{ font-family:var(--sans); font-size:10.5px; font-weight:600; letter-spacing:.01em; color:var(--subtle); }}

  /* ── Section headings: small caps over a hairline ─────────────────── */
  h2 {{
    font-family:var(--sans); font-size:13px; font-weight:700;
    letter-spacing:0; color:var(--ink);
    margin:36px 0 0; padding-bottom:5px; border-bottom:1px solid var(--hairline);
  }}
  h3 {{
    font-family:var(--sans); font-size:15px; font-weight:600;
    margin:22px 0 2px; color:var(--ink);
  }}
  h3 .report-muted {{ font-style:italic; font-size:13px; color:var(--muted); }}

  /* ── Figures ──────────────────────────────────────────────────────── */
  .figure-grid {{
    display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr));
    gap:26px 32px; margin:18px 0 0;
  }}
  .fig-block {{ margin:0; min-width:0; }}
  .fig-svg {{ display:block; overflow:visible; }}
  .fig-block figcaption {{
    font-family:var(--sans); font-size:12px;
    color:var(--muted); line-height:1.5; margin-top:9px;
  }}

  /* ── Tables: booktabs. Horizontal rules only. ─────────────────────── */
  .table-wrap {{ overflow-x:auto; margin:14px 0 0; }}
  table {{
    width:100%; border-collapse:collapse; font-size:12.5px;
    font-variant-numeric:tabular-nums;
  }}
  thead th {{
    text-align:left; font-weight:500; font-size:11px; letter-spacing:.06em;
    text-transform:uppercase; color:var(--subtle); vertical-align:bottom;
    padding:0 10px 6px 0; white-space:nowrap;
    border-top:1px solid var(--rule); border-bottom:0.75px solid var(--rule);
    padding-top:7px;
  }}
  tbody td {{
    padding:7px 10px 7px 0; vertical-align:top; color:var(--ink-soft);
    border-bottom:1px solid var(--faint);
  }}
  tbody tr:last-child td {{ border-bottom:1px solid var(--rule); }}
  tbody td:first-child {{ color:var(--ink); }}
  th:last-child, td:last-child {{ padding-right:0; }}
  /* Numerals right-align so magnitudes compare down the column. */
  td.num, th.num {{ text-align:right; }}
  caption {{
    caption-side:bottom; text-align:left; font-family:var(--sans);
    font-size:12px; color:var(--muted);
    padding-top:8px; line-height:1.5;
  }}
  table.kv thead th {{ border-top:1px solid var(--rule); }}
  table.kv td:first-child {{ width:46%; color:var(--muted); }}
  table.kv td:last-child {{ color:var(--ink); }}

  /* ── Per-submission detail ────────────────────────────────────────── */
  .report-details {{ margin:18px 0 0; }}
  .report-details > summary {{
    cursor:pointer; font-family:var(--sans); font-size:14px; font-weight:600; color:var(--ink);
    padding:7px 0; border-bottom:1px solid var(--hairline); list-style:none;
  }}
  .report-details > summary::-webkit-details-marker {{ display:none; }}
  .report-details > summary::before {{
    content:"+"; display:inline-block; width:15px; color:var(--muted);
    font-family:var(--sans);
  }}
  .report-details[open] > summary::before {{ content:"\\2212"; }}
  .report-details > summary:hover {{ color:var(--muted); }}
  .data-details {{ margin-top:14px; }}
  .data-details > summary {{ font-family:var(--sans); font-weight:600; font-size:13px; }}

  /* ── Reviewer-oriented disclosure sections ───────────────────────── */
  .report-section {{ margin:20px 0 0; border-top:1px solid var(--rule); }}
  .report-section > summary {{
    cursor:pointer; list-style:none; padding:12px 0;
    font-family:var(--sans); font-size:13px; font-weight:700;
    letter-spacing:0; color:var(--ink);
    display:flex; align-items:center; gap:8px;
  }}
  .report-section > summary::-webkit-details-marker {{ display:none; }}
  .report-section > summary::before {{
    content:"+"; width:14px; font-family:var(--sans); font-size:15px;
    font-weight:400; line-height:1; color:var(--muted);
  }}
  .report-section[open] > summary::before {{ content:"\\2212"; }}
  .report-section-body {{ padding:2px 0 14px; overflow:hidden; }}
  .section-count {{
    min-width:21px; padding:1px 6px; border:1px solid var(--hairline);
    border-radius:999px; text-align:center; letter-spacing:0;
    font-family:var(--sans); font-size:10px; color:var(--muted);
  }}
  .compact-kv {{ max-width:520px; margin-bottom:20px; }}

  /* ── Notes ────────────────────────────────────────────────────────── */
  .notes {{ font-size:13.5px; color:var(--ink-soft); margin-top:12px; }}
  .notes p {{ margin:0 0 6px; }}
  .report-note {{
    font-family:var(--sans); font-size:12px;
    color:var(--muted); margin:8px 0 0;
  }}
  .report-muted {{ color:var(--muted); }}
  .limitations-list {{
    margin:12px 0 0; padding-left:17px; font-size:12.5px; color:var(--ink-soft);
    line-height:1.6;
  }}
  .limitations-list li {{ margin-bottom:4px; padding-left:3px; }}
  .limitations-list li::marker {{ color:var(--subtle); }}

  .colophon {{
    margin-top:40px; padding-top:11px; border-top:1px solid var(--faint);
    color:var(--muted); font-size:11px; letter-spacing:.04em;
    display:flex; justify-content:space-between; gap:14px; flex-wrap:wrap;
  }}

  @media (max-width:760px) {{
    body {{ padding:0; background:#fff; }}
    .sheet {{ padding:24px 20px 36px; }}
    .meta {{ grid-template-columns:auto 1fr; }}
    .review-grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }}
    .report-section-body {{ padding-left:0; }}
    h1 {{ font-size:24px; }}
  }}

  /* ── Print ─────────────────────────────────────────────────────────
     The palette is already ink-on-paper, so printing needs no colour
     coercion beyond the status dots. */
  @media print {{
    body {{ padding:0; background:#fff; font-size:9.5pt; }}
    .sheet {{ max-width:none; padding:0; }}
    h2 {{ page-break-after:avoid; break-after:avoid; margin-top:20pt; }}
    h3 {{ page-break-after:avoid; break-after:avoid; }}
    thead {{ display:table-header-group; }}
    tr {{ page-break-inside:avoid; break-inside:avoid; }}
    caption {{ page-break-before:avoid; }}
    .table-wrap {{ overflow:visible; }}
    .report-details[open] > summary::before,
    .report-details > summary::before {{ content:""; }}
    .stat .dot {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
  }}
</style></head>
<body>
<article class="sheet">
  <header>
    <div class="runhead">
      {logo_html}
      <div class="issue">{_esc(export_date)}<br>{_esc(blind_label)}</div>
    </div>
    <div class="titleblock">
      <h1>Submission review report</h1>
      <p class="deck">{_esc(session_name)}{_esc(' · ' + ', '.join(challenges) if challenges else '')}</p>
    </div>
    <dl class="meta">{meta_html}</dl>
  </header>

  <details class="report-section" open>
    <summary>Key Results</summary>
    <div class="report-section-body">
      <div class="review-grid">{review_status_html}</div>
      {reference_note_html}
      <div class="table-wrap compact-kv"><table class="kv"><tbody>{executive_metrics_html}</tbody></table></div>
      {main_map_results_html}
    </div>
  </details>

  {f'''<details class="report-section" open>
    <summary>Comparison by Region <span class="section-count">{len(_region_rows)}</span></summary>
    <div class="report-section-body">{region_table_html}</div>
  </details>''' if _region_rows else ''}

  {f'''<details class="report-section" open>
    <summary>Header and Orientation Check <span class="section-count">{len(_header_check_rows)}</span></summary>
    <div class="report-section-body">{header_check_html}</div>
  </details>''' if _header_check_rows else ''}

  {f'''<details class="report-section">
    <summary>ROI Results <span class="section-count">{len(_roi_rows)}</span></summary>
    <div class="report-section-body">{roi_table_html}</div>
  </details>''' if _roi_rows else ''}

  {f'''<details class="report-section">
    <summary>Reference Comparison</summary>
    <div class="report-section-body">{challenge_reference_summary_html}{reference_comparison_html}{figures_html}</div>
  </details>''' if reference_available else ''}

  {f'''<details class="report-section">
    <summary>Additional Analysis</summary>
    <div class="report-section-body">{prototype_analysis_html}</div>
  </details>''' if prototype_analysis_html else ''}

  <details class="report-section">
    <summary>Issues &amp; Limitations{f''' <span class="section-count">{len(report_model['issues'])} review item{'s' if len(report_model['issues']) != 1 else ''}</span>''' if report_model['issues'] else ''}</summary>
    <div class="report-section-body">
      {issues_html if report_model['issues'] else '<p class="report-note">No review items were recorded.</p>'}
      <ul class="limitations-list">{limitations_html}</ul>
    </div>
  </details>

  <details class="report-section">
    <summary>Provenance</summary>
    <div class="report-section-body"><dl class="meta">{provenance_html}</dl></div>
  </details>

  <div class="colophon">
    <span>OSIPI Perfusion Pipeline &middot; automated report</span>
    <span>Pipeline {_esc(report_model['analysis_provenance']['pipeline_version'])} &middot; configuration {_esc(report_model['analysis_provenance']['challenge_configuration'])} &middot; scoring package {_esc(report_model['analysis_provenance']['scoring_package'])} &middot; reference {_esc(report_model['analysis_provenance']['reference_dataset'])} &middot; {_esc(generated)}</span>
  </div>
</article>
</body></html>"""

    # Blinded downloads must not carry the team name in the filename either.
    tag = report_filename_tag(
        (batch_id or submission_id or "report").replace("/", "_"), blinded=blinded)
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
    raw_tag = (batch_id or submission_id or "report").replace("/", "_")
    tag = report_filename_tag(raw_tag, blinded=blinded)
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
