"""Persisted single-pass inventories for ingested submissions."""

from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from osipi_pipeline.config.rules import (
    app_settings,
    map_type_patterns,
    mask_name_patterns,
    private_path_parts,
    tuple_setting,
    validation_rules,
)
from osipi_pipeline.ingestion.artifact_classifier import classify, detect_map_type
from osipi_pipeline.ingestion.identity_parser import resolve_identity
from osipi_pipeline.ingestion.models import (
    IdentityConflict,
    Manifest,
    SubmissionArtifact,
)
from osipi_pipeline.ingestion.nifti_header import read_ndim
from osipi_pipeline.performance import timed

MANIFEST_FILENAME = ".osipi_manifest.json"


def config_fingerprint() -> str:
    payload = {
        "rules": validation_rules(),
        "settings": app_settings(),
    }
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()


def build_manifest(
    *,
    submission_id: str,
    challenge_type: str,
    original_path: str | Path,
    extracted_path: Path,
) -> Manifest:
    """Create a manifest for an extracted submission folder using one scan."""

    with timed("manifest.build", submission_id=submission_id):
        root = extracted_path.resolve()
        files = sorted(path for path in root.rglob("*") if path.is_file() and path.name != MANIFEST_FILENAME)
        directories = _directory_entries(root, files)
        entries = [_file_entry(path, root) for path in files]
        # Reuses the entries above rather than walking the tree again, so a
        # large synthetic submission is still traversed once.
        artifacts, conflicts = _build_artifacts(entries, root, challenge_type)

    return Manifest(
        submission_id=submission_id,
        challenge_type=challenge_type,
        original_path=_original_path_value(original_path),
        extracted_path=str(root),
        file_count=len(entries),
        nifti_files=[str(item["relative_path"]) for item in entries if item.get("is_nifti")],
        metadata_files=[str(item["relative_path"]) for item in entries if item.get("is_metadata")],
        code_files=[str(item["relative_path"]) for item in entries if item.get("is_code")],
        docker_files=[str(item["relative_path"]) for item in entries if item.get("is_docker")],
        readme_files=[str(item["relative_path"]) for item in entries if item.get("is_readme")],
        timestamp=datetime.now(timezone.utc).isoformat(),
        files=entries,
        directories=directories,
        config_fingerprint=config_fingerprint(),
        artifacts=artifacts,
        identity_conflicts=conflicts,
    )


def is_reference_path(relative_path: str) -> bool:
    """True when a path sits inside reference / mask data, not team output.

    Reference maps and ROI masks are commonly staged *inside* the extracted
    submission, ``submissions/extracted/<sid>/reference`` is the first
    location production searches, but they are scoring inputs, not something
    the team submitted. Counting them as artifacts inflates the map count and
    makes reference files fail participant identity resolution, since they
    carry no participant or repeat. Directory names come from
    ``paths.private_path_parts`` rather than being hardcoded.
    """
    return any(
        part.strip().lower() in private_path_parts()
        for part in relative_path.split("/")[:-1]
    )


def _build_artifacts(
    entries: list[dict[str, Any]], root: Path, challenge_type: str
) -> tuple[tuple[SubmissionArtifact, ...], tuple[IdentityConflict, ...]]:
    """Normalize every submitted file entry into a :class:`SubmissionArtifact`.

    Every submitted file gets a record, duplicates included. Two copies of the
    same map under one identity produce two artifacts; deciding whether that is
    valid belongs to a later phase, and dropping one here would hide it.

    Reference and mask directories are excluded: they are scoring inputs rather
    than submission content. See :func:`is_reference_path`.
    """
    challenge = (challenge_type or "").strip().lower() or None
    artifacts: list[SubmissionArtifact] = []
    conflicts: list[IdentityConflict] = []
    for entry in entries:
        rel = str(entry.get("relative_path") or "")
        if not rel or is_reference_path(rel):
            continue
        filename = rel.rsplit("/", 1)[-1]
        is_nifti = bool(entry.get("is_nifti"))
        role, map_type, artifact_type = classify(
            filename,
            is_nifti=is_nifti,
            is_readme=bool(entry.get("is_readme")),
            is_metadata=bool(entry.get("is_metadata")),
            is_code=bool(entry.get("is_code")),
        )
        identity, file_conflicts = resolve_identity(rel, challenge=challenge)
        conflicts.extend(file_conflicts)
        # Header only: never the voxel array. None when unreadable, which
        # the validation layer reports separately.
        dimensions = read_ndim(root / rel) if is_nifti else None
        artifacts.append(SubmissionArtifact(
            path=rel,
            role=role,
            challenge=challenge,
            dataset=identity.get("dataset"),
            participant=identity.get("participant"),
            repeat=identity.get("repeat"),
            site=identity.get("site"),
            map_type=map_type,
            artifact_type=artifact_type,
            dimensions=dimensions,
        ))
    return tuple(artifacts), tuple(conflicts)


def manifest_path(root: Path) -> Path:
    return root / MANIFEST_FILENAME


def refresh_manifest(
    root: Path,
    *,
    submission_id: str = "",
    challenge_type: str = "",
    original_path: str | Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    manifest = build_manifest(
        submission_id=submission_id or root.name,
        challenge_type=(challenge_type or "").lower(),
        original_path=original_path or root,
        extracted_path=root,
    )
    path = manifest_path(root)
    data = manifest.to_dict()
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    _refresh_directory_mtimes(root, data)
    path.write_text(json.dumps(data, indent=2, default=str) + "\n", encoding="utf-8")
    return data


def load_manifest(root: Path, *, refresh_if_stale: bool = True, **refresh_kwargs: Any) -> dict[str, Any] | None:
    path = manifest_path(root)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if isinstance(data, dict) and not refresh_if_stale:
            return data
        if isinstance(data, dict) and _manifest_is_current(root, data):
            return data
    if not refresh_if_stale or not root.exists():
        return None
    return refresh_manifest(root, **refresh_kwargs)


def manifest_files(root: Path, *, refresh_if_stale: bool = True, **refresh_kwargs: Any) -> list[Path]:
    manifest = load_manifest(root, refresh_if_stale=refresh_if_stale, **refresh_kwargs)
    if not manifest:
        return []
    result: list[Path] = []
    for item in manifest.get("files", []):
        if isinstance(item, dict) and item.get("relative_path"):
            result.append(root / str(item["relative_path"]))
    return result


def save_manifest(manifest: Manifest, manifests_dir: Path) -> tuple[Path, Path]:
    """Save one manifest in both JSON and CSV formats."""

    manifests_dir.mkdir(parents=True, exist_ok=True)
    base_name = f"{manifest.challenge_type}_{manifest.submission_id}_manifest"
    json_path = manifests_dir / f"{base_name}.json"
    csv_path = manifests_dir / f"{base_name}.csv"

    manifest_data = manifest.to_dict()
    json_path.write_text(json.dumps(manifest_data, indent=2, default=str) + "\n", encoding="utf-8")

    with csv_path.open("w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(manifest_data.keys()))
        writer.writeheader()
        writer.writerow({key: _csv_value(value) for key, value in manifest_data.items()})

    return json_path, csv_path


def _file_entry(path: Path, root: Path) -> dict[str, Any]:
    stat = path.stat()
    rel = _as_relative_posix(path, root)
    name = path.name.lower()
    parts = tuple(part.lower() for part in Path(rel).parts)
    is_nifti = _is_nifti(path)
    is_metadata = path.suffix.lower() in _metadata_suffixes()
    is_readme = _is_readme(path)
    is_code = _is_code(path, parts)
    is_docker = _is_docker_file(path)
    is_reference = bool(set(parts).intersection({"reference", "references", "ref"}))
    is_mask = _is_mask(path, parts)
    is_preview = bool(set(parts).intersection({"preview", "previews"})) or name.endswith(".png")
    is_output = bool(set(parts).intersection({"result", "results", "output", "outputs", "maps", "map"}))
    detected_id = _detect_parameter_map_id(path) if is_nifti else ""
    return {
        "relative_path": rel,
        "size": int(stat.st_size),
        "mtime": float(stat.st_mtime),
        "mtime_ns": int(stat.st_mtime_ns),
        "suffix": _suffix_type(path),
        "is_nifti": is_nifti,
        "nifti_classification": "nifti" if is_nifti else "",
        "detected_parameter_map_id": detected_id,
        "is_code": is_code,
        "is_readme": is_readme,
        "is_metadata": is_metadata,
        "is_docker": is_docker,
        "is_output": is_output,
        "is_mask": is_mask,
        "is_reference": is_reference,
        "is_preview": is_preview,
    }


def _directory_entries(root: Path, files: Iterable[Path]) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    directories: set[Path] = {root}
    for file_path in files:
        current = file_path.parent
        while True:
            directories.add(current)
            if current == root:
                break
            current = current.parent
    for directory in sorted(directories):
        try:
            stat = directory.stat()
        except OSError:
            continue
        entries.append({
            "relative_path": _as_relative_posix(directory, root),
            "mtime_ns": int(stat.st_mtime_ns),
        })
    return entries


def _manifest_is_current(root: Path, manifest: dict[str, Any]) -> bool:
    if manifest.get("config_fingerprint") != config_fingerprint():
        return False
    for item in manifest.get("files", []):
        if not isinstance(item, dict) or not item.get("relative_path"):
            return False
        path = root / str(item["relative_path"])
        try:
            stat = path.stat()
        except OSError:
            return False
        if int(item.get("size", -1)) != stat.st_size:
            return False
        if int(item.get("mtime_ns", -1)) != stat.st_mtime_ns:
            return False
    for item in manifest.get("directories", []):
        if not isinstance(item, dict):
            return False
        path = root / str(item.get("relative_path") or "")
        try:
            stat = path.stat()
        except OSError:
            return False
        if int(item.get("mtime_ns", -1)) != stat.st_mtime_ns:
            return False
    return True


def _refresh_directory_mtimes(root: Path, manifest: dict[str, Any]) -> None:
    for item in manifest.get("directories", []):
        if not isinstance(item, dict):
            continue
        path = root / str(item.get("relative_path") or "")
        try:
            item["mtime_ns"] = int(path.stat().st_mtime_ns)
        except OSError:
            pass


def _is_nifti(path: Path) -> bool:
    return path.name.lower().endswith(tuple_setting("nifti_suffixes"))


def _is_readme(path: Path) -> bool:
    name = path.name.lower()
    for configured in tuple_setting("readme_names"):
        configured = configured.lower()
        if name == configured or (("." not in configured) and name.startswith(configured)):
            return True
    return False


def _is_docker_file(path: Path) -> bool:
    name = path.name.lower()
    return name == "dockerfile" or name == ".dockerignore" or name.startswith("docker-compose")


def _is_code(path: Path, parts: Iterable[str]) -> bool:
    name = path.name.lower()
    if name in set(tuple_setting("code_file_names")):
        return True
    if path.suffix.lower() in set(tuple_setting("code_extensions")):
        return True
    return bool(set(parts).intersection(set(tuple_setting("code_folder_names"))))


def _is_mask(path: Path, parts: Iterable[str]) -> bool:
    part_set = set(parts)
    if part_set.intersection({"mask", "masks"}):
        return True
    name = path.name.lower()
    return any(pattern in name for pattern in mask_name_patterns())


def _detect_parameter_map_id(path: Path) -> str:
    """Legacy manifest field, now backed by the boundary-safe classifier.

    This previously asked whether a configured pattern appeared anywhere in
    the filename, so ``curve.nii.gz`` matched ``ve`` and ``developer.nii.gz``
    matched it twice (yielding "mixed"). Required-map enforcement cannot rest
    on that, and keeping two different matchers would guarantee they drift,
    so both this field and ``SubmissionArtifact.map_type`` now come from
    :func:`detect_map_type`.
    """
    return detect_map_type(path.name) or ""


def _metadata_suffixes() -> set[str]:
    return set(tuple_setting("metadata_suffixes"))


def _suffix_type(path: Path) -> str:
    name = path.name.lower()
    if name.endswith(".nii.gz"):
        return ".nii.gz"
    return path.suffix.lower()


def _as_relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _csv_value(value: object) -> object:
    if isinstance(value, list):
        if value and isinstance(value[0], dict):
            return json.dumps(value, default=str)
        return ";".join(str(item) for item in value)
    return value


def _original_path_value(original_path: str | Path) -> str:
    path_text = str(original_path)
    if "://" in path_text or path_text.startswith("git@"):
        return path_text
    return str(Path(original_path).resolve())
