"""Reloading challenge rules from the interface, instead of restarting.

Editing config/validation_rules.yaml used to require a container restart, so a
mentor could not change what a challenge requires without a terminal. These
cover the endpoint behind the Reload button, and in particular what happens
when the edit is wrong — the case the button exists to make survivable.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


@pytest.fixture()
def api():
    from fastapi.testclient import TestClient
    import main
    return TestClient(main.app)


def test_reload_reports_the_challenges_it_read(api) -> None:
    body = api.post("/api/config/reload").json()
    assert body["reloaded"] is True
    assert "dce" in body["challenges"]
    assert "ktrans" in body["map_types"]


def test_reload_picks_up_an_edit_without_a_restart(api, tmp_path, monkeypatch) -> None:
    """The whole point: change the file, press the button, see the change."""
    from osipi_pipeline.config import rules as config_rules

    # Shaped like the real asl entry, so the reload succeeds or fails on
    # whether the file was re-read, not on whether the probe is valid.
    probe = ("challenges:\n"
             "  zzz_probe:\n"
             "    label: Probe\n"
             "    description: Added by a test\n"
             "    expected_maps:\n"
             "      - cbf\n"
             "    keywords:\n"
             "      - cbf\n")
    original = config_rules.VALIDATION_RULES_PATH.read_text()
    edited = original.replace("challenges:\n", probe, 1)
    assert edited != original

    patched = tmp_path / "validation_rules.yaml"
    patched.write_text(edited)
    monkeypatch.setattr(config_rules, "VALIDATION_RULES_PATH", patched)

    body = api.post("/api/config/reload").json()
    assert body["reloaded"] is True
    assert "zzz_probe" in body["challenges"], "the edit was not picked up"


def test_a_broken_edit_is_reported_and_the_old_config_keeps_running(
        api, tmp_path, monkeypatch) -> None:
    """A typo must not take the pipeline down or show a stack trace.

    Someone who is not a developer has to be able to fix the file and press
    the button again, which means the failure has to be readable and the
    running configuration has to survive it.
    """
    from osipi_pipeline.config import rules as config_rules

    broken = tmp_path / "validation_rules.yaml"
    broken.write_text("challenges:\n  dce:\n    not_a_real_key: 1\n")
    monkeypatch.setattr(config_rules, "VALIDATION_RULES_PATH", broken)

    response = api.post("/api/config/reload")
    assert response.status_code == 200, "a bad edit must not surface as a 500"
    body = response.json()
    assert body["reloaded"] is False
    assert body["error"], "the reader is not told what is wrong"
    assert "still in use" in body["detail"]

    # And the pipeline is still serving the previous configuration.
    monkeypatch.undo()
    config_rules.clear_config_cache()
    assert api.get("/api/config").status_code == 200


def test_the_button_and_its_handler_are_wired_up() -> None:
    """An endpoint nobody can reach does not remove the terminal."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    html = (root / "frontend" / "index.html").read_text()
    app_js = (root / "frontend" / "app.js").read_text()

    assert 'id="config-reload-btn"' in html
    assert 'id="config-reload-msg"' in html
    assert "config-reload-btn" in app_js and "/api/config/reload" in app_js

    # Read the handler's own body by brace matching. Searching the whole file
    # for hydrateAppConfig() passes on any other call site, which is how the
    # first version of this check missed the refresh being deleted.
    start = app_js.index("async function _reloadChallengeRules")
    depth, end = 0, start
    for index in range(app_js.index("{", start), len(app_js)):
        if app_js[index] == "{":
            depth += 1
        elif app_js[index] == "}":
            depth -= 1
            if depth == 0:
                end = index
                break
    body = app_js[start:end]

    assert "/api/config/reload" in body
    assert "hydrateAppConfig()" in body, \
        "the screen still shows the old challenge list after a reload"
