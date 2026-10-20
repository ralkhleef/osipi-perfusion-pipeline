"""The browser must never serve a stale script."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "backend"), str(ROOT / "src")]


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def _versions(html: str) -> dict[str, str]:
    import re
    return {m.group(1): m.group(2)
            for m in re.finditer(r"/static/(app\.js|styles\.css)\?v=([^\"']+)", html)}


def test_both_assets_carry_a_version(client) -> None:
    versions = _versions(client.get("/").text)
    assert set(versions) == {"app.js", "styles.css"}, versions


def test_the_version_is_not_the_placeholder_from_the_source(client) -> None:
    """index.html ships ``?v=dev``; the served page must not."""
    for asset, version in _versions(client.get("/").text).items():
        assert version != "dev", f"{asset} was served with the placeholder"


def test_the_version_is_a_content_hash(client) -> None:
    import hashlib
    import main
    html = client.get("/").text
    for asset, version in _versions(html).items():
        digest = hashlib.sha256((main.FRONTEND_DIR / asset).read_bytes()).hexdigest()
        assert digest.startswith(version), f"{asset}: {version} is not its hash"


def test_the_same_file_keeps_the_same_url(client) -> None:
    """Otherwise nothing is ever cached and every load re-downloads."""
    assert _versions(client.get("/").text) == _versions(client.get("/").text)


def test_changing_a_file_changes_its_url(client, tmp_path, monkeypatch) -> None:
    """The property the hand-written numbers were supposed to provide."""
    import main
    original = _versions(client.get("/").text)["app.js"]

    source = main.FRONTEND_DIR / "app.js"
    backup = source.read_bytes()
    try:
        source.write_bytes(backup + b"\n// a change\n")
        changed = _versions(client.get("/").text)["app.js"]
    finally:
        source.write_bytes(backup)

    assert changed != original, "editing app.js did not change its URL"
    assert _versions(client.get("/").text)["app.js"] == original, (
        "restoring the file did not restore its URL")


def test_the_html_itself_is_not_cached(client) -> None:
    """It names the versioned assets, so a cached copy undoes all of this."""
    assert "no-store" in client.get("/").headers.get("cache-control", "")


def test_an_unreadable_asset_still_serves_a_page(monkeypatch) -> None:
    """A missing file is the static mount's problem to report, not a 500 here."""
    import main
    monkeypatch.setattr(main, "FRONTEND_DIR", Path("/nonexistent"), raising=False)
    first = main._asset_fingerprint("app.js")
    assert first and first != "dev"
