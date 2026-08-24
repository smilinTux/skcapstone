import json
import re
from pathlib import Path

from starlette.testclient import TestClient

from skdashboard.control_plane_adapters import SPECS
from skdashboard.control_plane_metric_registry import REGISTRY
from skdashboard.dashboard import create_app

ROOT = Path(__file__).parents[1]


def test_now_alias_serves_existing_overview_without_a_write_route(tmp_path: Path) -> None:
    client = TestClient(create_app(tmp_path, control_plane_authorizer=lambda *_: False))
    response = client.get("/control-plane/now")
    assert response.status_code == 200
    assert "<h2>Now</h2>" in response.text
    assert 'id="estate-table"' in response.text
    assert client.post("/control-plane/now").status_code == 405


def test_now_workspace_declares_exact_breadth_and_fail_closed_ai_boundary() -> None:
    html = (ROOT / "src/skdashboard/static/overview.html").read_text(encoding="utf-8")
    js = (ROOT / "src/skdashboard/static/js/overview.js").read_text(encoding="utf-8")
    css = (ROOT / "src/skdashboard/static/css/overview.css").read_text(encoding="utf-8")

    assert js.count(' id: "') == 12
    assert {spec.adapter_id for spec in SPECS} <= {
        quoted.split('"')[1]
        for quoted in js.splitlines()
        if "adapters: [" in quoted
        for quoted in quoted.split("[")[1].split("]")[0].split(", ")
    }
    for marker in (
        "No decision projection available",
        "AI abstained",
        "No recommendation to preview",
        "No comparable baseline",
        "does not refresh, remediate, queue, authorize, or dispatch",
        'id="estate-evidence"',
        'id="now-context"',
    ):
        assert marker in html
    for field in (
        "Evidence",
        "Best practice",
        "Confidence",
        "Uncertainty",
        "Counter-indicators",
        "Alternatives",
        "Impact, risk, preconditions",
    ):
        assert field in html
    assert 'url.pathname = "/control-plane/now"' in js
    assert 'scope: "estate", window: "latest", baseline: "none", service: "all"' in js
    assert "Expected 16 bounded adapter observations" in js
    assert "No silo is assumed healthy" in js
    assert "item.population" in js
    assert "coverage.reporting, 0" not in js
    assert "coverage.expected, 0" not in js
    assert "estateEvidence = new Map()" in js
    assert "dialog.open) dialog.close()" in js
    assert 'document.getElementById("estate-evidence-body").replaceChildren()' in js
    assert 'clearLegacyOverview("Protected estate evidence unavailable")' in js
    assert "fetch(" not in js
    assert "prefers-reduced-motion:reduce" in css
    assert "@media(max-width:560px)" in css
    for token in ("--now-action:#00667a", "--now-current:#126344", "--now-muted:#48546b"):
        assert token in css


def test_now_metric_context_sources_match_the_registry() -> None:
    js = (ROOT / "src/skdashboard/static/js/overview.js").read_text(encoding="utf-8")
    configs = [line for line in js.splitlines() if " metric: " in line]
    assert len(configs) == 12
    for config in configs:
        metric = re.search(r'metric: "([^"]+)@([^"]+)"', config)
        adapters = re.search(r"adapters: (\[[^]]+\])", config)
        explicit_source = re.search(r'metricSource: "([^"]+)"', config)
        assert metric and adapters
        definition = REGISTRY[(metric.group(1), metric.group(2))]
        declared_adapters = json.loads(adapters.group(1))
        shown_source = explicit_source.group(1) if explicit_source else declared_adapters[0]
        assert shown_source == definition.adapter_id

    economy = next(line for line in configs if 'id: "economy"' in line)
    assert 'metricSource: "skcounter.harness"' in economy
