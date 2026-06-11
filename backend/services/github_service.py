"""Import public GitHub repositories as submissions via ZIP archives."""

import re
from typing import Dict, Optional
from urllib.parse import urlparse

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from services.ingest_service import save_and_extract


def import_github_repo(repo_url: str, branch: Optional[str] = None) -> Dict:
    """Download a public GitHub repository ZIP and ingest it as a submission."""
    if not _REQUESTS_AVAILABLE:
        return _err("The 'requests' library is not installed. Run: pip install requests")

    parsed = _parse_github_url(repo_url)
    if parsed is None:
        return _err("Enter a valid GitHub repository URL, for example https://github.com/org/repo.")

    owner, repo = parsed

    # If the caller specified a branch, use it directly; otherwise try main then master.
    branches_to_try = [branch.strip()] if branch and branch.strip() else ["main", "master"]
    resp = None
    selected_branch = branches_to_try[0]

    for attempt_branch in branches_to_try:
        archive_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{attempt_branch}.zip"
        try:
            r = requests.get(archive_url, timeout=120)
        except requests.ConnectionError:
            return _err("Could not connect to GitHub. Check your internet connection.")
        except requests.Timeout:
            return _err("GitHub repository download timed out.")
        if r.status_code == 404:
            continue
        if not r.ok:
            return _err(f"GitHub returned HTTP {r.status_code}.")
        resp = r
        selected_branch = attempt_branch
        break

    if resp is None:
        return _err(f"Repository branch not found on GitHub (tried: {', '.join(branches_to_try)}).")

    filename = f"github_{owner}_{repo}_{selected_branch}.zip"
    result = save_and_extract(resp.content, filename)
    if result.get("success"):
        result["message"] = f"Imported {result['file_count']} file(s) from GitHub."
    return result


def _parse_github_url(repo_url: str) -> Optional[tuple]:
    parsed = urlparse(repo_url.strip())
    if parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        return None

    parts = [part for part in parsed.path.strip("/").split("/") if part]
    if len(parts) < 2:
        return None

    owner = parts[0]
    repo = re.sub(r"\.git$", "", parts[1])
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
