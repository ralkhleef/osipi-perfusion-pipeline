"""Resolve and prepare ingestion sources.

A source is where the submission comes from: a local folder, local zip file, or
GitHub repository URL. This module turns that source into a local working copy
that the rest of ingestion can scan.
"""

# TODO: This file decides what kind of input the user gave: folder, zip, or GitHub URL.
# TODO: Later, add source handlers for OSF, Google Drive, and direct HTTP links.
# TODO: Keep source handling lightweight so large MRI data is not pulled into the repo.

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import zipfile

CLONE_TIMEOUT_SECONDS = 120


@dataclass(frozen=True)
class SubmissionSource:
    """A normalized description of where a submission came from.

    ``kind`` is the source type, such as ``folder``, ``zip``, or ``github``.
    ``original`` keeps the user-provided path or URL for the manifest.
    ``submission_id`` is a filesystem-safe name used for output folders.
    """

    kind: str
    original: str
    submission_id: str


def resolve_source(input_value: str | Path) -> SubmissionSource:
    """Identify whether the input is a folder, zip file, or GitHub URL."""

    input_text = str(input_value)
    # Check GitHub URLs before local paths because URLs are not filesystem paths.
    github_slug = _github_slug(input_text)
    if github_slug:
        owner, repo = github_slug
        return SubmissionSource(kind="github", original=input_text, submission_id=f"{owner}_{repo}")

    local_path = Path(input_value).expanduser()
    if not local_path.exists():
        raise FileNotFoundError(f"Submission input does not exist or is not a supported URL: {input_text}")
    if local_path.is_dir():
        return SubmissionSource(kind="folder", original=str(local_path), submission_id=_safe_submission_id(local_path.name))
    if local_path.suffix.lower() == ".zip":
        return SubmissionSource(kind="zip", original=str(local_path), submission_id=_safe_submission_id(local_path.stem))

    raise ValueError(f"Submission input must be a directory, .zip file, or GitHub repository URL: {input_text}")


def materialize_source(source: SubmissionSource, destination: Path) -> None:
    """Create a local working copy of the source at ``destination``."""

    _prepare_destination(destination)
    if source.kind == "folder":
        shutil.copytree(Path(source.original), destination)
        return
    if source.kind == "zip":
        destination.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(Path(source.original), "r") as zip_file:
            safe_extract_zip(zip_file, destination)
        return
    if source.kind == "github":
        _clone_github_repository(source.original, destination)
        return

    raise ValueError(f"Unsupported submission source type: {source.kind}")


def safe_extract_zip(zip_file: zipfile.ZipFile, destination: Path) -> None:
    """Extract a zip file while blocking unsafe paths."""

    destination_root = destination.resolve()
    for member in zip_file.infolist():
        target = (destination / member.filename).resolve()
        # This prevents a zip entry like "../../file" from escaping the folder.
        if destination_root not in target.parents and target != destination_root:
            raise ValueError(f"Unsafe zip entry would extract outside destination: {member.filename}")
    zip_file.extractall(destination)


def _clone_github_repository(repo_url: str, destination: Path) -> None:
    """Clone a GitHub repository into a local working folder."""

    if shutil.which("git") is None:
        raise ValueError("GitHub ingestion requires git to be installed and available on PATH.")

    destination.parent.mkdir(parents=True, exist_ok=True)
    # A shallow clone is enough for ingestion and keeps downloads small.
    command = ["git", "clone", "--depth", "1", repo_url, str(destination)]
    env = os.environ.copy()
    # Do not automatically download Git LFS files, which may be large MRI data.
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    print("Cloning GitHub repo...")
    try:
        subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
            env=env,
            timeout=CLONE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        if destination.exists():
            shutil.rmtree(destination)
        raise ValueError(f"GitHub clone timed out after {CLONE_TIMEOUT_SECONDS} seconds: {repo_url}") from exc
    except subprocess.CalledProcessError as exc:
        details = (exc.stderr or exc.stdout or "").strip()
        message = f"Could not clone GitHub repository: {repo_url}"
        if details:
            message = f"{message}\n{details}"
        raise ValueError(message) from exc
    print("GitHub repo cloned successfully.")

    git_metadata = destination / ".git"
    if git_metadata.exists():
        # The manifest should describe the submission files, not Git internals.
        shutil.rmtree(git_metadata)


def _github_slug(input_text: str) -> tuple[str, str] | None:
    """Return a safe owner/repo pair for supported GitHub URL styles."""

    patterns = (
        r"^https?://github\.com/([^/\s]+)/([^/\s#?]+?)(?:\.git)?/?(?:[?#].*)?$",
        r"^git@github\.com:([^/\s]+)/([^/\s#?]+?)(?:\.git)?$",
    )
    for pattern in patterns:
        match = re.match(pattern, input_text)
        if match:
            owner = _safe_submission_id(match.group(1))
            repo = _safe_submission_id(match.group(2))
            return owner, repo
    return None


def _prepare_destination(destination: Path) -> None:
    """Create a clean destination folder."""

    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)


def _safe_submission_id(raw_id: str) -> str:
    """Convert a folder, zip, or repo name into a safe output folder name."""

    safe_id = "".join(character if character.isalnum() or character in "._-" else "_" for character in raw_id)
    return safe_id.strip("._-") or "submission"
