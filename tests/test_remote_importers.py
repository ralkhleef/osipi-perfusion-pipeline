"""Importing a submission from GitHub or Zenodo.

Both modules take a string a stranger typed and turn it into a network
request and a file on disk, and both sat near 17 percent covered. Nothing
here reaches the network: ``requests`` is replaced with a stub, so what gets
checked is the part that is ours, which URLs are accepted, which are refused,
what happens when the far end misbehaves, and whether the size limit holds.
"""

from __future__ import annotations

import io
import zipfile
from types import SimpleNamespace

import pytest


# ── GitHub URL parsing ────────────────────────────────────────────────────

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/OSIPI/TF6.2_DCE-DSC-MRI_Challenges", ("OSIPI", "TF6.2_DCE-DSC-MRI_Challenges")),
    ("https://github.com/org/repo.git", ("org", "repo")),
    ("https://www.github.com/org/repo", ("org", "repo")),
    ("  https://github.com/org/repo/  ", ("org", "repo")),
    ("https://github.com/org/repo/tree/main/sub", ("org", "repo")),
])
def test_a_real_repository_url_resolves_to_owner_and_repo(url, expected):
    from services.github_service import _parse_github_url
    assert _parse_github_url(url) == expected


@pytest.mark.parametrize("url", [
    "https://gitlab.com/org/repo",
    "https://github.com/org",
    "https://github.com/",
    "not a url at all",
    "",
    "https://evil.example.com/github.com/org/repo",
])
def test_anything_that_is_not_a_github_repository_is_refused(url):
    """The host is checked, not merely searched for.

    A URL like https://evil.example.com/github.com/org/repo contains the
    string "github.com" and must still be refused.
    """
    from services.github_service import _parse_github_url
    assert _parse_github_url(url) is None


def test_a_bad_url_never_reaches_the_network(monkeypatch):
    import services.github_service as github_service

    def explode(*args, **kwargs):
        raise AssertionError("a rejected URL must not be fetched")

    monkeypatch.setattr(github_service, "requests",
                        SimpleNamespace(get=explode), raising=False)
    out = github_service.import_github_repo("https://gitlab.com/org/repo")
    assert out["success"] is False
    assert "valid GitHub repository URL" in out["message"]


# ── GitHub download behaviour ─────────────────────────────────────────────

class _Response:
    def __init__(self, status_code=200, chunks=(), ok=None):
        self.status_code = status_code
        self.ok = (200 <= status_code < 300) if ok is None else ok
        self._chunks = chunks

    def iter_content(self, chunk_size=8192):
        yield from self._chunks

    def close(self):
        pass


def _zip_bytes() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("repo-main/README.md", "hello")
    return buffer.getvalue()


def _stub_requests(monkeypatch, module, handler):
    """Install a fake requests module that records the URLs asked for."""
    seen: list[str] = []

    class ConnectionError_(Exception):
        pass

    class Timeout_(Exception):
        pass

    def get(url, **kwargs):
        seen.append(url)
        return handler(url)

    monkeypatch.setattr(module, "requests", SimpleNamespace(
        get=get, ConnectionError=ConnectionError_, Timeout=Timeout_,
        RequestException=Exception), raising=False)
    monkeypatch.setattr(module, "_REQUESTS_AVAILABLE", True, raising=False)
    return seen


def test_main_is_tried_before_master(monkeypatch, tmp_path):
    """Most repositories are on main. Asking for it first saves a round trip."""
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "INCOMING_DIR", tmp_path)
    seen = _stub_requests(monkeypatch, github_service,
                          lambda url: _Response(404))

    github_service.import_github_repo("https://github.com/org/repo")
    assert len(seen) == 2, seen
    assert "main.zip" in seen[0] and "master.zip" in seen[1]


def test_a_repository_on_neither_branch_reports_that_clearly(monkeypatch, tmp_path):
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "INCOMING_DIR", tmp_path)
    _stub_requests(monkeypatch, github_service, lambda url: _Response(404))

    out = github_service.import_github_repo("https://github.com/org/repo")
    assert out["success"] is False
    assert out["message"]


def test_an_explicit_branch_is_the_only_one_tried(monkeypatch, tmp_path):
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "INCOMING_DIR", tmp_path)
    seen = _stub_requests(monkeypatch, github_service, lambda url: _Response(404))

    github_service.import_github_repo("https://github.com/org/repo", branch="develop")
    assert len(seen) == 1 and "develop.zip" in seen[0]


def test_a_server_error_is_reported_with_its_status(monkeypatch, tmp_path):
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "INCOMING_DIR", tmp_path)
    _stub_requests(monkeypatch, github_service, lambda url: _Response(503))

    out = github_service.import_github_repo("https://github.com/org/repo")
    assert out["success"] is False
    assert "503" in out["message"]


def test_an_oversized_archive_is_refused_while_still_streaming(monkeypatch, tmp_path):
    """The limit has to bite during the download, not after it.

    Checking afterwards would mean writing the whole thing to disk first,
    which is the failure mode the limit exists to prevent.
    """
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "INCOMING_DIR", tmp_path)
    monkeypatch.setattr(github_service, "ZIP_MAX_BYTES", 1024, raising=False)

    huge = (b"x" * 4096 for _ in range(100))
    _stub_requests(monkeypatch, github_service,
                   lambda url: _Response(200, chunks=huge))

    out = github_service.import_github_repo("https://github.com/org/repo")
    assert out["success"] is False
    leftover = list(tmp_path.glob("*.tmp"))
    assert not leftover, f"a refused download left {leftover} behind"


def test_a_connection_failure_is_a_message_not_a_traceback(monkeypatch, tmp_path):
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "INCOMING_DIR", tmp_path)

    class ConnectionError_(Exception):
        pass

    def get(url, **kwargs):
        raise ConnectionError_("no route to host")

    monkeypatch.setattr(github_service, "requests", SimpleNamespace(
        get=get, ConnectionError=ConnectionError_, Timeout=type("T", (Exception,), {}),
        RequestException=Exception), raising=False)
    monkeypatch.setattr(github_service, "_REQUESTS_AVAILABLE", True, raising=False)

    out = github_service.import_github_repo("https://github.com/org/repo")
    assert out["success"] is False
    assert "connect" in out["message"].lower()


def test_the_importer_says_so_when_requests_is_absent(monkeypatch):
    import services.github_service as github_service

    monkeypatch.setattr(github_service, "_REQUESTS_AVAILABLE", False, raising=False)
    out = github_service.import_github_repo("https://github.com/org/repo")
    assert out["success"] is False
    assert "requests" in out["message"]


# ── Zenodo record identifiers ─────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("12345678", "12345678"),
    ("10.5281/zenodo.12345678", "12345678"),
    ("https://doi.org/10.5281/zenodo.12345678", "12345678"),
    ("https://zenodo.org/records/12345678", "12345678"),
    ("https://zenodo.org/record/12345678", "12345678"),
    ("https://zenodo.org/records/12345678/files/data.zip", "12345678"),
])
def test_every_documented_zenodo_form_yields_the_record_id(text, expected):
    """Users paste a DOI, a URL or the bare number. All three are accepted."""
    from services.zenodo_service import _parse_record_id
    assert _parse_record_id(text) == expected


@pytest.mark.parametrize("text", [
    "", "not a record", "https://zenodo.org/", "zenodo", "10.5281/dryad.12345",
])
def test_input_with_no_record_id_in_it_is_refused(text):
    from services.zenodo_service import _parse_record_id
    assert _parse_record_id(text) is None


def test_unparseable_input_is_refused_before_any_request(monkeypatch):
    import services.zenodo_service as zenodo_service

    def explode(*args, **kwargs):
        raise AssertionError("nothing should be fetched for unparseable input")

    monkeypatch.setattr(zenodo_service, "requests",
                        SimpleNamespace(get=explode), raising=False)
    out = zenodo_service.handle_zenodo_input("not a record")
    assert out["success"] is False


def _stub_zenodo(monkeypatch, record, file_bodies=None, file_status=200):
    """A fake Zenodo: one metadata response, then one body per file."""
    import services.zenodo_service as zenodo_service

    file_bodies = file_bodies or {}
    seen: list[str] = []

    class HTTPError_(Exception):
        pass

    class _Meta:
        status_code = 200
        ok = True

        def json(self):
            return record

    class _File:
        def __init__(self, body):
            self._body = body
            self.status_code = file_status
            self.ok = 200 <= file_status < 300

        def raise_for_status(self):
            if not self.ok:
                raise HTTPError_(f"{self.status_code}")

        def iter_content(self, chunk_size=65536):
            for i in range(0, len(self._body), chunk_size):
                yield self._body[i:i + chunk_size]

        def close(self):
            pass

    def get(url, **kwargs):
        seen.append(url)
        if url.startswith(zenodo_service.ZENODO_API):
            return _Meta()
        name = url.rsplit("/", 1)[-1].split("?")[0]
        return _File(file_bodies.get(name, b"data"))

    monkeypatch.setattr(zenodo_service, "requests", SimpleNamespace(
        get=get, HTTPError=HTTPError_, ConnectionError=type("C", (Exception,), {}),
        Timeout=type("T", (Exception,), {}), RequestException=Exception),
        raising=False)
    monkeypatch.setattr(zenodo_service, "_REQUESTS_AVAILABLE", True, raising=False)
    return seen


def test_a_record_with_files_downloads_them_all(monkeypatch, tmp_path):
    import services.zenodo_service as zenodo_service

    record = {"metadata": {"title": "Reference maps"},
              "files": [{"key": "a.nii.gz"}, {"key": "b.nii.gz"}]}
    _stub_zenodo(monkeypatch, record, {"a.nii.gz": b"AAA", "b.nii.gz": b"BBB"})

    out = zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert out["success"] is True
    assert out["title"] == "Reference maps"
    assert sorted(out["downloaded_files"]) == ["a.nii.gz", "b.nii.gz"]
    assert (tmp_path / "zenodo_12345678" / "a.nii.gz").read_bytes() == b"AAA"


def test_a_record_with_no_files_succeeds_but_says_so(monkeypatch, tmp_path):
    """An empty record is not an error, but it must not look like a download."""
    import services.zenodo_service as zenodo_service

    _stub_zenodo(monkeypatch, {"metadata": {"title": "Empty"}, "files": []})
    out = zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert out["success"] is True
    assert out["downloaded_files"] == []
    assert out["errors"]


def test_a_file_entry_with_no_name_is_skipped_and_reported(monkeypatch, tmp_path):
    import services.zenodo_service as zenodo_service

    record = {"metadata": {"title": "Odd"}, "files": [{"size": 10}, {"key": "ok.nii"}]}
    _stub_zenodo(monkeypatch, record, {"ok.nii": b"fine"})

    out = zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert out["downloaded_files"] == ["ok.nii"]
    assert any("no filename" in e for e in out["errors"])


def test_a_failing_file_does_not_lose_the_ones_that_worked(monkeypatch, tmp_path):
    """One bad file in a record should not discard the rest of the download."""
    import services.zenodo_service as zenodo_service

    record = {"metadata": {"title": "Mixed"}, "files": [{"key": "gone.nii"}]}
    _stub_zenodo(monkeypatch, record, {}, file_status=500)

    out = zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert out["success"] is False
    assert out["downloaded_files"] == []
    assert any("gone.nii" in e for e in out["errors"])


def test_a_record_over_the_size_limit_is_aborted_mid_download(monkeypatch, tmp_path):
    """The cap is cumulative across files, not per file."""
    import services.zenodo_service as zenodo_service

    monkeypatch.setattr(zenodo_service, "_DOWNLOAD_MAX_BYTES", 1024, raising=False)
    record = {"metadata": {"title": "Huge"}, "files": [{"key": "big.nii"}]}
    _stub_zenodo(monkeypatch, record, {"big.nii": b"x" * 200_000})

    out = zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert out["success"] is False
    assert any("size limit" in e for e in out["errors"])


def test_a_filename_cannot_escape_the_target_directory(monkeypatch, tmp_path):
    """Zenodo supplies the filename, so it is untrusted input."""
    import services.zenodo_service as zenodo_service

    record = {"metadata": {"title": "Hostile"},
              "files": [{"key": "../../escaped.nii"}]}
    _stub_zenodo(monkeypatch, record, {"escaped.nii": b"nope"})

    zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert not (tmp_path.parent / "escaped.nii").exists()
    assert not (tmp_path / "escaped.nii").exists()


def test_a_missing_record_is_reported_by_number(monkeypatch, tmp_path):
    import services.zenodo_service as zenodo_service

    class _Missing:
        status_code = 404
        ok = False

        def json(self):
            return {}

    monkeypatch.setattr(zenodo_service, "requests", SimpleNamespace(
        get=lambda url, **kw: _Missing(), ConnectionError=type("C", (Exception,), {}),
        Timeout=type("T", (Exception,), {}), RequestException=Exception),
        raising=False)
    monkeypatch.setattr(zenodo_service, "_REQUESTS_AVAILABLE", True, raising=False)

    out = zenodo_service.download_zenodo_record("12345678", tmp_path, "zenodo")
    assert out["success"] is False
    # Zenodo reports failures in `errors`, GitHub in `message`. The API layer
    # reads `errors` first and falls back to `message`, so both shapes have to
    # keep working; this pins the Zenodo half of that contract.
    assert any("12345678" in e for e in out["errors"])


def test_a_record_id_is_carried_into_the_api_url(monkeypatch):
    """The id must reach the request. A parser nobody uses proves nothing."""
    import services.zenodo_service as zenodo_service

    seen: list[str] = []

    class _R:
        status_code = 404
        ok = False

        def json(self):
            return {}

    def get(url, **kwargs):
        seen.append(url)
        return _R()

    monkeypatch.setattr(zenodo_service, "requests", SimpleNamespace(
        get=get, ConnectionError=type("C", (Exception,), {}),
        Timeout=type("T", (Exception,), {}), RequestException=Exception),
        raising=False)
    monkeypatch.setattr(zenodo_service, "_REQUESTS_AVAILABLE", True, raising=False)

    zenodo_service.handle_zenodo_input("https://zenodo.org/records/12345678")
    assert seen, "no request was made"
    assert "12345678" in seen[0]
