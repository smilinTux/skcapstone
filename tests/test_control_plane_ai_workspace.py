from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from skdashboard.dashboard import create_app

ROOT = Path(__file__).parents[1]


def test_ai_workspace_is_read_only_and_discoverable(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: False))
    response = client.get("/control-plane/ai")
    assert response.status_code == 200
    assert "Outcomes before activity" in response.text
    assert 'id="ai-lanes"' in response.text
    assert 'id="ai-quality-rows"' in response.text
    assert 'id="ai-drilldown-rows"' in response.text
    assert 'id="ai-provenance-rows"' in response.text
    assert 'id="ai-detail"' in response.text
    assert client.post("/control-plane/ai").status_code == 405


def test_ai_workspace_keeps_units_lanes_unknowns_and_provenance_distinct() -> None:
    html = (ROOT / "src/skdashboard/static/ai.html").read_text(encoding="utf-8")
    js = (ROOT / "src/skdashboard/static/js/ai.js").read_text(encoding="utf-8")
    css = (ROOT / "src/skdashboard/static/css/ai.css").read_text(encoding="utf-8")

    for marker in (
        "Harness-reported usage",
        "gateway-observed usage",
        "never summed",
        "Tokens, USD, Joules, latency, quality, and value",
        "Pricing revision",
        "Cost confidence",
        "Accepted outcome",
        "Verified effect",
        "Evaluation quality",
        "Citation coverage",
        "Rework",
        "Override",
        "Abstention",
        "Denial handling",
        "Budget",
        "Cost per accepted outcome",
        "Scope and provenance",
        "No-write and data-minimization boundary",
    ):
        assert marker in html or marker in js

    for source in ("skcounter.harness", "skgateway.observed", "skjoule.wallet"):
        assert source in js
    for dimension in (
        '"Model"',
        '"Client"',
        '"Provider"',
        '"Node"',
        '"Route"',
        '"Queue"',
        '"Cache"',
        '"Tool error"',
        '"Quality"',
        '"Cost"',
    ):
        assert dimension in js
    assert "apiUrl(context)" in js
    assert "quality.metric_registry" in js
    assert "registry.registry_version" in js
    assert "registry.registry_hash" in js
    assert 'clear("Loading protected AI outcome evidence.", "Loading")' in js
    assert "const current = epoch" in js
    assert "lastTrigger.focus()" in js
    assert 'aria-label="Open ${esc(dimension)} drilldown"' in js
    assert "/api/v1/ai" not in js
    assert "fetch(" not in js
    assert "localStorage" not in js
    assert "sessionStorage" not in js
    assert "IndexedDB" not in js
    assert "workspace_path" not in js
    assert "source_path" not in js
    assert "session_id" not in js
    assert "cost_usd" in js
    assert "total_supply" in js
    assert "pricing revision" not in js.lower() or "Not projected" in js
    assert "@media(max-width:560px)" in css
    assert "@media(prefers-reduced-motion:reduce)" in css


def test_control_plane_navigation_links_the_ai_workspace() -> None:
    for page in (
        "overview.html",
        "projects.html",
        "schedule.html",
        "reliability.html",
        "architecture.html",
        "governance.html",
    ):
        text = (ROOT / "src/skdashboard/static" / page).read_text(encoding="utf-8")
        assert "/control-plane/ai?" in text


def test_ai_workspace_real_chrome_purges_delayed_401_and_403_responses() -> None:
    if not shutil.which("node") or not (
        shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    ):
        pytest.skip("Node or Chrome is unavailable for the AI CDP qualification")
    try:
        result = subprocess.run(
            ["node", "scripts/qualify_control_plane_ai_cdp.mjs"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        pytest.fail(f"AI CDP qualification exited {exc.returncode}: {(exc.stderr or '')[-4000:]}")
    evidence = json.loads(result.stdout.strip())
    assert evidence == {
        "result": "PASS",
        "base": "7299700a",
        "budgetRows": 11,
        "registry": True,
        "keyboard": True,
        "focusReturn": True,
        "axNames": True,
        "lightContrast": evidence["lightContrast"],
        "darkContrast": evidence["darkContrast"],
        "reducedMotion": True,
        "responsive": [390, 320],
        "delayedPurge": True,
        "unauthorizedPurge": True,
        "forbiddenPurge": True,
        "staleResponseBlocked": True,
        "requests": evidence["requests"],
        "writes": 0,
        "external": 0,
        "exceptions": 0,
        "scratchCleaned": True,
    }
    assert evidence["lightContrast"] >= 4.5
    assert evidence["darkContrast"] >= 4.5
