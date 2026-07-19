"""Lightweight local performance helpers.

The pipeline is a local reviewer tool, so these helpers intentionally stay
in-process and dependency-free: timing samples for measurement, bounded worker
limits from config, and a small job registry for long synchronous operations.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from osipi_pipeline.config.rules import performance_settings

_TIMING_LOCK = threading.Lock()
_TIMINGS: list[dict[str, Any]] = []
_JOBS_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def configured_worker_limit(name: str, default: int, *, ceiling: int = 8) -> int:
    """Return a safe worker limit from YAML, CPU count, and a hard ceiling."""

    cpus = os.cpu_count() or 1
    configured = performance_settings().get(name)
    try:
        value = int(configured) if configured is not None else min(default, max(1, cpus))
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, max(1, cpus), ceiling))


def record_timing(stage: str, elapsed_seconds: float, **meta: Any) -> dict[str, Any]:
    sample = {
        "stage": stage,
        "elapsed_seconds": round(float(elapsed_seconds), 6),
        "recorded_at": _now(),
        **meta,
    }
    with _TIMING_LOCK:
        _TIMINGS.append(sample)
        del _TIMINGS[:-200]
    return sample


@contextmanager
def timed(stage: str, **meta: Any) -> Iterator[None]:
    start = time.perf_counter()
    try:
        yield
    finally:
        record_timing(stage, time.perf_counter() - start, **meta)


def recent_timings(limit: int = 50) -> list[dict[str, Any]]:
    with _TIMING_LOCK:
        return list(_TIMINGS[-max(1, limit):])


def start_job(kind: str, *, total: int = 0, stage: str = "queued", key: str | None = None) -> str:
    job_id = uuid.uuid4().hex
    job = {
        "job_id": job_id,
        "kind": kind,
        "key": key or "",
        "status": "running",
        "stage": stage,
        "completed": 0,
        "total": int(total or 0),
        "percent": 0.0,
        "started_at": _now(),
        "updated_at": _now(),
        "finished_at": "",
        "elapsed_seconds": 0.0,
        "error": "",
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job
        del_keys = list(_JOBS.keys())[:-200]
        for old in del_keys:
            if _JOBS.get(old, {}).get("status") != "running":
                _JOBS.pop(old, None)
    return job_id


def update_job(job_id: str | None, *, stage: str | None = None, completed: int | None = None,
               total: int | None = None, status: str | None = None, error: str | None = None) -> None:
    if not job_id:
        return
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return
        if stage is not None:
            job["stage"] = stage
        if total is not None:
            job["total"] = int(total)
        if completed is not None:
            job["completed"] = int(completed)
        if status is not None:
            job["status"] = status
        if error is not None:
            job["error"] = error
        total_value = int(job.get("total") or 0)
        completed_value = int(job.get("completed") or 0)
        job["percent"] = round((completed_value / total_value) * 100.0, 2) if total_value else 0.0
        job["updated_at"] = _now()
        started = datetime.fromisoformat(str(job["started_at"]))
        job["elapsed_seconds"] = round((datetime.now(timezone.utc) - started).total_seconds(), 3)


def finish_job(job_id: str | None, *, error: str | None = None) -> None:
    if not job_id:
        return
    status = "failed" if error else "completed"
    update_job(job_id, status=status, error=error or "")
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if job:
            job["finished_at"] = _now()
            if job.get("total"):
                job["completed"] = job["total"]
                job["percent"] = 100.0 if not error else job.get("percent", 0.0)


def job_status(job_id: str) -> dict[str, Any] | None:
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None
