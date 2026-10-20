#!/usr/bin/env python3
"""Find out why Run Analysis appears to do nothing."""

from __future__ import annotations

import argparse
import json
import urllib.error
import urllib.request

FRONTEND_MARKERS = {
    "/": ["run-result-modal", "score-run-outcome", "grouping-note", "auto-advance"],
    "/static/app.js": [
        "_openRunResult",
        "if (btnAll && !isConfigured)",
        "_autoAdvanceToQc",       # the wizard carrying itself to QC
        "_mapCountLabel",         # maps counted by role, not by file
        "_groupingModel",         # correcting how an upload was grouped
        "fromRestore",            # a reload must not re-run the wizard
    ],
}


def _assets_differing_from_disk(base: str):
    """Which frontend assets the server serves from a different build than this."""
    import hashlib
    import re as _re
    from pathlib import Path as _Path

    frontend = _Path(__file__).resolve().parent.parent / "frontend"
    if not frontend.is_dir():
        return None
    status, html = get(f"{base}/")
    if status != 200:
        return None

    differing = []
    for name in ("app.js", "styles.css"):
        match = _re.search(rf"/static/{_re.escape(name)}\?v=([a-f0-9]+)", html)
        asset = frontend / name
        if not match or not asset.is_file():
            return None
        local = hashlib.sha256(asset.read_bytes()).hexdigest()[:12]
        if match.group(1) != local:
            differing.append((name, match.group(1), local))
    return differing


def get(url: str, timeout: int = 20) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:
        return 0, str(exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--url", default="http://localhost:8000")
    args = parser.parse_args(argv)
    base = args.url.rstrip("/")
    print(f"\n  Asking {base}\n")

    # ── 1. Is anything there? ────────────────────────────────────────────
    status, body = get(f"{base}/")
    if status != 200:
        print("  [FAIL] Nothing is listening.")
        print(f"         {body[:120]}\n")
        print("  FIX:   docker compose up -d --build\n")
        return 1
    print("  [ ok ] The application is running")

    # ── 2. Is the browser being served the current frontend? ─────────────
    stale: list[str] = []
    for path, markers in FRONTEND_MARKERS.items():
        code, text = get(f"{base}{path}")
        if code != 200:
            print(f"  [FAIL] {path} returned HTTP {code}")
            return 1
        missing = [m for m in markers if m not in text]
        if missing:
            stale.append(f"{path} is missing {missing}")
    if stale:
        print("  [FAIL] The server is serving an OLD frontend:")
        for line in stale:
            print(f"         {line}")
        print("\n  FIX:   docker compose up -d --build")
        print("         Run it from the folder you actually updated. This is")
        print("         a server-side problem, so no amount of reloading the")
        print("         browser will change it.\n")
        return 1
    print("  [ ok ] The served frontend is current")

    mismatched = _assets_differing_from_disk(base)
    if mismatched is None:
        print("  [ -- ] Could not compare the served assets with this folder")
    elif mismatched:
        print("  [FAIL] The running app was NOT built from this folder:")
        for name, served, local in mismatched:
            print(f"         {name}: serving {served}, this folder has {local}")
        print()
        print("  FIX:   You are probably in a different copy of the project.")
        print("         Rebuild from here, or cd to the folder you updated:")
        print("           docker compose up -d --build")
        print("         After a ZIP update, the old folder is still on disk and")
        print("         still runnable, which is what makes this easy to miss.\n")
        return 1
    else:
        print("  [ ok ] The running app matches the files in this folder")

    # ── 3. What does the interface think is configured? ──────────────────
    code, text = get(f"{base}/api/scoring/active-config")
    if code != 200:
        print(f"  [FAIL] active-config returned HTTP {code}\n")
        return 1
    config = (json.loads(text).get("active_config") or {})

    print()
    print("  Per challenge:")
    print(f"    {'challenge':<12} {'mode':<10} {'card shows?':<12} button does")
    verdicts = []
    for challenge, entry in sorted(config.items()):
        mode = str((entry or {}).get("mode") or "none")
        configured = mode != "none"
        shows = "yes" if configured else "no, hidden"
        does = "runs the provider" if configured else "explains, then nothing to run"
        print(f"    {challenge:<12} {mode:<10} {shows:<12} {does}")
        verdicts.append((challenge, configured))

    print()
    if not any(ok for _c, ok in verdicts):
        print("  VERDICT: no provider is configured for any challenge.")
        print()
        print("  This is NORMAL and is not what you are demonstrating.")
        print("  The card should be hidden. If your browser still shows")
        print("  'Analysis is ready', reload the page; the checks above have")
        print("  already confirmed the server is serving the current copy.")
        print()
        print("  QC, ROI statistics, the comparison against ground truth,")
        print("  previews and every export all work without a provider.")
    else:
        print("  VERDICT: a provider is configured, so the button should run it.")
        print("  If it still does nothing, open DevTools, press it, and read")
        print("  the Console and Network tabs.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
