#!/usr/bin/env python3
"""Find out why Run Analysis appears to do nothing.

Run Analysis can look broken for several unrelated reasons, and they need
different fixes, so guessing wastes time. This asks the running application
directly and prints one verdict.

    python3 scripts/diagnose_run_analysis.py

It reads only. It uploads nothing and changes no configuration.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

# Markers that only exist in the current frontend. If the server is handing out
# an older copy, the browser cannot possibly show the new behaviour, and that is
# by far the most common cause.
FRONTEND_MARKERS = {
    "/": ["run-result-modal", "score-run-outcome"],
    "/static/app.js": ["_openRunResult", "if (btnAll && !isConfigured)"],
}


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
        print("         then in Chrome: DevTools open, right click reload,")
        print("         'Empty Cache and Hard Reload'\n")
        return 1
    print("  [ ok ] The served frontend is current")

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
        print("  'Analysis is ready', the page is stale: hard reload it.")
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
