#!/usr/bin/env python3
"""Check the application is ready to demonstrate, before you need it to be.

Run this half an hour before a meeting. It walks a real submission through
all six steps against the running application and prints one line per step,
so a failure is found while there is still time to do something about it
rather than in front of an audience.

    python3 scripts/preflight.py                    # against localhost:8000
    python3 scripts/preflight.py --url http://...   # somewhere else
    python3 scripts/preflight.py --keep             # leave the submission

Everything it uploads is generated here and thrown away afterwards. No real
or private data is involved.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

OK, BAD, WARN = "  ok  ", " FAIL ", " warn "


class Check:
    """One step, its outcome, and what to do when it goes wrong."""

    def __init__(self, name: str, hint: str = "") -> None:
        self.name, self.hint = name, hint
        self.status, self.detail = WARN, ""

    def line(self) -> str:
        return f"[{self.status}] {self.name:34} {self.detail}"


def request(url: str, *, data=None, headers=None, timeout=120):
    """A plain request, returning (status, body). Never raises for HTTP."""
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except Exception as exc:  # connection refused, DNS, timeout
        return 0, str(exc).encode()


def multipart(filename: str, payload: bytes) -> tuple[bytes, str]:
    """A file upload body, without pulling in a dependency to build it."""
    boundary = uuid.uuid4().hex
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/zip\r\n\r\n",
        payload, b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    return body, f"multipart/form-data; boundary={boundary}"


def build_submission(destination: Path) -> bool:
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "make_minimal_submission.py"),
         "--challenge", "asl", "--out", str(destination)],
        cwd=REPO_ROOT, capture_output=True, text=True,
    )
    return result.returncode == 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000")
    parser.add_argument("--keep", action="store_true",
                        help="Leave the demo submission in place afterwards")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")

    print(f"\n  Checking {base}\n")
    checks: list[Check] = []

    def record(name: str, status: str, detail: str = "", hint: str = "") -> Check:
        check = Check(name, hint)
        check.status, check.detail = status, detail
        checks.append(check)
        print(check.line())
        return check

    # ── Is it even up? ────────────────────────────────────────────────────
    started = time.perf_counter()
    status, body = request(f"{base}/", timeout=15)
    if status != 200:
        record("Application responds", BAD, str(body[:90], errors="replace"),
               "Start it with: docker compose up --build")
        print("\n  Nothing else can be checked until it is running.\n"
              "  Start it with:  docker compose up --build\n")
        return 1
    record("Application responds", OK, f"{time.perf_counter() - started:.2f}s")

    for label, path in (("Interface loads", "/"),
                        ("Scripts served", "/static/app.js"),
                        ("Styles served", "/static/styles.css"),
                        ("Challenge config", "/api/config"),
                        ("Scoring status", "/api/scoring-status")):
        status, body = request(f"{base}{path}")
        record(label, OK if status == 200 else BAD,
               f"{status}, {len(body)} bytes" if status == 200 else f"HTTP {status}")

    # ── A real submission through all six steps ───────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "preflight_asl.zip"
        if not build_submission(archive):
            record("Build a demo submission", BAD, "",
                   "Check scripts/make_minimal_submission.py runs")
            return 1
        record("Build a demo submission", OK, f"{archive.stat().st_size} bytes")

        body, content_type = multipart("preflight_asl.zip", archive.read_bytes())
        status, raw = request(f"{base}/api/upload-submission", data=body,
                              headers={"Content-Type": content_type})
        if status != 200:
            record("1 Upload", BAD, f"HTTP {status}")
            return 1
        import json
        sid = json.loads(raw).get("submission_id")
        record("1 Upload", OK, f"id {sid}")

        payload = json.dumps({
            "submission_id": sid, "challenge_type": "asl",
            "team_name": "preflight check", "contact_email": "preflight@example.org",
        }).encode()
        status, raw = request(f"{base}/api/validate", data=payload,
                              headers={"Content-Type": "application/json"})
        if status == 200:
            result = json.loads(raw)
            record("2-3 Review and validate", OK,
                   f"{result.get('error_count')} errors, "
                   f"{result.get('warning_count')} warnings")
        else:
            record("2-3 Review and validate", BAD, f"HTTP {status}")

        payload = json.dumps({"submission_id": sid, "challenge_type": "asl",
                              "map_type": "cbf"}).encode()
        status, raw = request(f"{base}/api/score", data=payload,
                              headers={"Content-Type": "application/json"})
        record("4-5 Run and QC", OK if status == 200 else BAD,
               json.loads(raw).get("status", "") if status == 200 else f"HTTP {status}")

        for label, path in (
            ("6 HTML report", f"/api/report?submission_id={sid}"),
            ("6 PDF report", f"/api/export/report/pdf?submission_id={sid}"),
            ("6 CSV results", f"/api/export-combined?submission_id={sid}"),
            ("6 ROI statistics", f"/api/export-roi-descriptive?submission_id={sid}"),
        ):
            status, raw = request(f"{base}{path}")
            record(label, OK if status == 200 else BAD,
                   f"{len(raw)} bytes" if status == 200 else f"HTTP {status}")

        if not args.keep:
            request(f"{base}/api/submission/{sid}", data=b"", headers={})

    failed = [c for c in checks if c.status == BAD]
    print()
    if failed:
        print(f"  {len(failed)} of {len(checks)} checks failed:")
        for check in failed:
            print(f"    {check.name}{' -> ' + check.hint if check.hint else ''}")
        print()
        return 1
    print(f"  All {len(checks)} checks passed. The walkthrough works end to end.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
