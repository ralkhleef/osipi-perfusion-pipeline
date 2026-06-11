"""Download Zenodo record files into a configured local folder.

Accepts a Zenodo URL, DOI, or bare record ID.  Calls the Zenodo REST API to
read the record title and file list, then downloads each file.
"""

import re
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote

try:
    import requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False

from services.path_config import REFERENCE_DATA_DIR

ZENODO_API = "https://zenodo.org/api/records"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def handle_zenodo_input(zenodo_input: str) -> Dict:
    """Import a Zenodo record as official benchmark/reference data."""
    return download_zenodo_record(
        zenodo_input,
        target_root=REFERENCE_DATA_DIR,
        folder_prefix="zenodo",
        reset_existing=False,
    )


def download_zenodo_record(
    zenodo_input: str,
    target_root: Path,
    folder_prefix: str,
    reset_existing: bool = False,
) -> Dict:
    """Parse the input, fetch record metadata, and download files.

    Args:
        zenodo_input: A Zenodo URL, DOI link, or plain numeric record ID.
        target_root: Parent folder for the downloaded record.
        folder_prefix: Prefix used for the record folder name.
        reset_existing: Whether to replace an existing target folder first.

    Returns a dict with: success, record_id, title, downloaded_files, errors.
    """
    if not _REQUESTS_AVAILABLE:
        return _err("The 'requests' library is not installed. Run: pip install requests")

    record_id = _parse_record_id(zenodo_input.strip())
    if record_id is None:
        return _err(
            "Could not find a Zenodo record ID in the input. "
            "Accepted formats: https://zenodo.org/records/12345678  "
            "or  https://doi.org/10.5281/zenodo.12345678  or  12345678"
        )

    # Fetch record metadata from the Zenodo API.
    try:
        resp = requests.get(f"{ZENODO_API}/{record_id}", timeout=15)
    except requests.ConnectionError:
        return _err("Could not connect to Zenodo. Check your internet connection.")
    except requests.Timeout:
        return _err("Zenodo API request timed out.")

    if resp.status_code == 404:
        return _err(f"Record {record_id} was not found on Zenodo.")
    if not resp.ok:
        return _err(f"Zenodo API returned HTTP {resp.status_code}.")

    record = resp.json()
    title = record.get("metadata", {}).get("title", "Unknown title")
    files = record.get("files", [])

    target_dir = target_root / f"{folder_prefix}_{record_id}"
    if reset_existing and target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    if not files:
        return {
            "success": True,
            "record_id": record_id,
            "title": title,
            "downloaded_files": [],
            "errors": ["No files are listed for this Zenodo record."],
        }

    downloaded, errors = _download_files(files, record_id, target_dir)

    return {
        "success": len(downloaded) > 0,
        "record_id": record_id,
        "title": title,
        "downloaded_files": downloaded,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _parse_record_id(text: str) -> Optional[str]:
    """Extract a numeric Zenodo record ID from any supported input format."""

    # Bare numeric ID
    if text.isdigit():
        return text

    # DOI-style: 10.5281/zenodo.12345678
    m = re.search(r"zenodo\.(\d+)", text)
    if m:
        return m.group(1)

    # URL-style: /records/12345678 or /record/12345678
    m = re.search(r"/records?/(\d+)", text)
    if m:
        return m.group(1)

    return None


def _download_files(files: List, record_id: str, target_dir: Path) -> Tuple[List[str], List[str]]:
    """Download each file in the record's file list in parallel.

    Returns (downloaded_filenames, error_messages).
    """
    def _fetch_one(file_info: dict) -> Tuple[Optional[str], Optional[str]]:
        filename = file_info.get("key") or file_info.get("filename")
        if not filename:
            return None, f"Skipped a file entry with no filename: {file_info}"

        download_url = (
            file_info.get("links", {}).get("self")
            or f"https://zenodo.org/records/{record_id}/files/{quote(filename)}?download=1"
        )
        try:
            file_resp = requests.get(download_url, timeout=120, stream=True)
            file_resp.raise_for_status()
            rel_path = _safe_relative_path(filename)
            dest = target_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open(dest, "wb") as fh:
                for chunk in file_resp.iter_content(chunk_size=65536):
                    fh.write(chunk)
            return str(rel_path), None
        except requests.HTTPError as exc:
            return None, f"HTTP error downloading {filename}: {exc}"
        except requests.Timeout:
            return None, f"Download timed out for {filename}."
        except Exception as exc:
            return None, f"Failed to download {filename}: {exc}"

    downloaded: List[str] = []
    errors: List[str] = []
    max_workers = min(6, len(files)) if files else 1

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_fetch_one, f): f for f in files}
        for future in as_completed(futures):
            result, error = future.result()
            if result:
                downloaded.append(result)
            if error:
                errors.append(error)

    return downloaded, errors


def _err(message: str) -> Dict:
    return {
        "success": False,
        "record_id": None,
        "title": None,
        "downloaded_files": [],
        "errors": [message],
    }


def _safe_relative_path(raw_path: str) -> Path:
    path = Path((raw_path or "").replace("\\", "/"))
    parts = [
        part for part in path.parts
        if part not in ("", ".", "/") and part != path.anchor
    ]
    if not parts or any(part == ".." for part in parts):
        raise ValueError("Record contains an unsafe file path.")
    return Path(*parts)
