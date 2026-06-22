"""Import public GitHub repositories as submissions via ZIP archives.

Downloads the repository ZIP with streaming (no full-body buffering), enforces
the configured size limit, and runs the same batch-boundary detection as local
ZIP uploads.  Returns the same response shape as ``save_and_extract_batch``.
"""

import os
import re
import tempfile
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import urlparse

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from services.ingest_service import (
    INCOMING_DIR,
    ZIP_MAX_BYTES,
    save_and_extract_batch_from_path,
)


def import_github_repo(repo_url: str, branch: Optional[str] = None) -> Dict:
    """Stream-download a public GitHub repository ZIP and ingest it as a submission.

    Tries ``main`` then ``master`` when no branch is specified.  Enforces the
    global ZIP size limit while streaming so the full archive is never held in
    RAM.  Runs batch-boundary detection after extraction, returning the same
    response shape as ``/api/upload-batch``.
    """
    if not _REQUESTS_AVAILABLE:
        return _err("The 'requests' library is not installed. Run: pip install requests")

    parsed = _parse_github_url(repo_url)
    if parsed is None:
        return _err(
            "Enter a valid GitHub repository URL, for example https://github.com/org/repo."
        )

    owner, repo = parsed
    branches_to_try = [branch.strip()] if branch and branch.strip() else ["main", "master"]

    INCOMING_DIR.mkdir(parents=True, exist_ok=True)

    for attempt_branch in branches_to_try:
        archive_url = (
            f"https://github.com/{owner}/{repo}/archive/refs/heads/{attempt_branch}.zip"
        )

        try:
            resp = requests.get(archive_url, timeout=120, stream=True)
        except requests.ConnectionError:
            return _err("Could not connect to GitHub. Check your internet connection.")
        except requests.Timeout:
            return _err("GitHub repository download timed out.")

        if resp.status_code == 404:
            continue  # try next branch

        if not resp.ok:
            return _err(f"GitHub returned HTTP {resp.status_code}.")

        # Stream to a temp file to avoid loading the entire ZIP into RAM
        safe_filename = f"github_{owner}_{repo}_{attempt_branch}.zip"
        tmp_fd, tmp_name = tempfile.mkstemp(dir=str(INCOMING_DIR), suffix=".tmp")
        tmp_path = Path(tmp_name)

        try:
            total_bytes = 0
            with os.fdopen(tmp_fd, "wb") as fout:
                for chunk in resp.iter_content(chunk_size=65536):
                    total_bytes += len(chunk)
                    if total_bytes > ZIP_MAX_BYTES:
                        resp.close()  # release the streaming connection before returning
                        return _err(
                            f"GitHub repository ZIP is too large "
                            f"(limit: {ZIP_MAX_BYTES // (1024 * 1024)} MB)."
                        )
                    fout.write(chunk)

            final_path = INCOMING_DIR / safe_filename
            tmp_path.replace(final_path)
            tmp_path = None  # file has been renamed; don't delete in finally

            result = save_and_extract_batch_from_path(final_path, safe_filename)
            if result.get("success"):
                result.setdefault("source_type", "github")
                fc = result.get("file_count") or result.get("submission_count", "?")
                result["message"] = f"Imported from GitHub ({owner}/{repo}, {fc} file(s))."
            return result

        except Exception as exc:
            return _err(f"Failed to process GitHub download: {exc}")

        finally:
            # Clean up only if the rename didn't happen (error path)
            if tmp_path is not None and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    tried = ", ".join(branches_to_try)
    return _err(
        f"Repository branch not found on GitHub (tried: {tried}). "
        "Specify the branch name explicitly."
    )


def _parse_github_url(repo_url: str) -> Optional[tuple]:
    parsed = urlparse(repo_url.strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None

    owner = parts[0]
    repo  = re.sub(r"\.git$", "", parts[1])
    if not owner or not repo:
        return None
    return owner, repo


def _err(message: str) -> Dict:
    return {
        "success": False,
        "submission_id": None,
        "file_count": 0,
        "message": message,
        "errors": [message],
    }
