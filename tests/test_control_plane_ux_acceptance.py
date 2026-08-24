from __future__ import annotations

import html
import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WIRE = ROOT / "docs" / "wireframes" / "control-plane-estate-pulse-v2.html"


def _wireframe() -> str:
    return WIRE.read_text(encoding="utf-8")


def test_v2_covers_all_twelve_silos_with_owner_and_truth_state() -> None:
    text = _wireframe()
    expected = {
        "portfolio",
        "flow",
        "itil",
        "engineering",
        "architecture",
        "fleet",
        "ai",
        "economy",
        "governance",
        "legal",
        "corpus",
        "operator",
    }
    silos = set(re.findall(r'<tr data-silo="([^"]+)"', text))
    assert silos == expected
    rows = re.findall(r'<tr data-silo="[^"]+">(.*?)</tr>', text, flags=re.S)
    assert len(rows) == 12
    for row in rows:
        assert "Owner:" in row
        assert re.search(r'class="state (?:current|stale|partial|unavailable|unknown|not_applicable)"', row)
    for state in ("current", "stale", "partial", "unavailable", "unknown", "not_applicable"):
        assert f'class="state {state}"' in text


def test_visible_truth_summary_matches_the_twelve_rows() -> None:
    text = _wireframe()
    rows = re.findall(r'<tr data-silo="[^"]+">(.*?)</tr>', text, flags=re.S)
    states = Counter(re.findall(r'class="state ([^"]+)"', "".join(rows)))
    assert states == Counter(
        {
            "current": 5,
            "partial": 3,
            "unavailable": 1,
            "stale": 1,
            "unknown": 1,
            "not_applicable": 1,
        }
    )
    assert 'aria-label="Truth summary for 12 visible silos"' in text
    for label in ("5 current", "3 partial", "1 unavailable", "1 stale", "1 unknown", "1 policy filtered", "12 visible silos"):
        assert label in text


def test_role_paths_are_direct_and_preserve_scope() -> None:
    text = _wireframe()
    assert 'data-role="project-manager"' in text
    assert 'data-role="architect"' in text
    assert 'task=blocked-value' in text
    assert 'task=blast-radius' in text
    role_links = re.findall(r'<a class="btn primary" href="([^"]+)"', text)
    assert any("role=project-manager" in link and "scope=estate" in link for link in role_links)
    assert any("role=architect" in link and "scope=estate" in link for link in role_links)


def test_primary_navigation_and_evidence_targets_are_real_deep_links() -> None:
    text = _wireframe()
    links = re.findall(r'<a[^>]+href="([^"]+)"', text)
    assert links
    deep_links = [link for link in links if link != "#main-content"]
    assert all(link.startswith("/control-plane/") for link in deep_links)
    assert all("scope=" in link and "window=" in link and "baseline=" in link for link in deep_links)
    assert 'href="#"' not in text
    assert 'data-evidence=' in text
    assert 'id="evidence-drawer"' in text
    assert "metric references" in text.lower()
    assert "watermark" in text.lower()


def test_each_silo_has_own_evidence_data_and_no_fallback() -> None:
    text = _wireframe()
    expected = {
        "portfolio",
        "flow",
        "itil",
        "engineering",
        "architecture",
        "fleet",
        "ai",
        "economy",
        "governance",
        "legal",
        "corpus",
        "operator",
    }
    evidence_keys = set(re.findall(r'<button[^>]+data-evidence="([^"]+)"', text))
    assert evidence_keys == expected
    for key in expected:
        assert re.search(rf"^\s+{key}: \[", text, flags=re.M)
    assert "data[key] || data.flow" not in text
    assert "Synthetic evidence key is not registered" in text


def test_ai_transparency_and_two_interaction_budgets_are_visible() -> None:
    text = _wireframe()
    for label in (
        "Metric references",
        "Uncertainty",
        "Counter-indicators",
        "Alternatives",
        "Preconditions",
        "Impact horizon",
        "Abstention example",
    ):
        assert label in text
    assert re.findall(r'data-interactions="1"', text)
    assert len(re.findall(r'data-interactions="2"', text)) >= 2
    assert "KPI to evidence" in text
    assert "Role lens to evidence" in text


def test_authorization_preview_exposes_exact_identity_and_fail_closed_states() -> None:
    text = _wireframe()
    assert 'id="auth-drawer" role="dialog" aria-modal="true"' in text
    for label in (
        "Actor",
        "Capability",
        "Exact target and target version",
        "Policy decision reference",
        "Exact preview hash",
        "Expiry",
        "Stale target:",
        "Denied:",
    ):
        assert label in text
    assert re.search(r"sha256:[0-9a-f]{64}", text)
    assert "never dispatches" in text
    assert "Changed target version invalidates this hash" in text
    for state in ("ready", "stale-target", "denied-policy", "expired", "changed-parameters"):
        assert f'value="{state}"' in text
    assert "previewState.addEventListener" in text
    assert "authorizeButton.disabled = state[3]" in text
    assert "aria-disabled" in text


def test_scope_and_role_controls_update_deep_link_context() -> None:
    text = _wireframe()
    assert 'id="service-scope"' in text
    assert 'id="role-context"' in text
    assert "serviceLabels" in text
    assert "function applyContextToLinks" in text
    assert "function initializeContext" in text
    assert "window.addEventListener(\"popstate\"" in text
    assert "history.pushState" in text
    assert "url.searchParams.set(\"scope\", context.scope)" in text
    assert "url.searchParams.set(\"service\", context.service)" in text
    assert "url.searchParams.set(\"window\", context.window)" in text
    assert "url.searchParams.set(\"baseline\", context.baseline)" in text
    assert "url.searchParams.set(\"role\", context.role)" in text


def _dump_dom(query: str) -> str:
    chrome = shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    if not chrome:
        import pytest

        pytest.skip("Chrome is unavailable for the synthetic interaction lane")
    result = subprocess.run(
        [
            chrome,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=1000",
            "--dump-dom",
            f"file://{WIRE}?{query}",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return html.unescape(result.stdout)


def test_browser_scope_role_and_preview_states_are_executable() -> None:
    scoped = _dump_dom("scope=skdashboard&service=skgateway&window=7d&baseline=target&role=architect")
    assert "/control-plane/now?role=architect&scope=skdashboard&window=7d&baseline=target&service=skgateway" in scoped
    assert 'id="role-context" class="scope"' in scoped
    assert 'option value="architect">Architect</option>' in scoped
    assert 'id="service-scope" class="scope"' in scoped
    assert "option>SKGateway</option>" in scoped
    evidence = _dump_dom("scope=skdashboard&service=skgateway&window=7d&baseline=target&role=architect&evidence=legal")
    assert "/control-plane/governance?role=architect&scope=skdashboard&window=7d&baseline=target&service=skgateway" in evidence
    states = {
        "ready": ("Ready for human authorization", "Authorization is available", False),
        "stale-target": ("Stale target", "target version changed", True),
        "denied-policy": ("Denied by policy", "capability or policy decision is unavailable", True),
        "expired": ("Expired preview", "preview expiry passed", True),
        "changed-parameters": ("Changed parameters", "parameters changed after preview creation", True),
    }
    for state, (status, reason, disabled) in states.items():
        dom = _dump_dom(f"preview=1&state={state}")
        assert status in dom
        assert reason in dom
        authorize = re.search(r'<button class="btn primary" id="authorize"[^>]*>', dom)
        assert authorize
        assert ("disabled=\"\"" in authorize.group(0)) is disabled


def test_keyboard_dialog_table_and_responsive_acceptance_contract() -> None:
    text = _wireframe()
    assert 'class="skip" href="#main-content"' in text
    assert 'aria-describedby="evidence-description"' in text
    assert 'aria-describedby="auth-description"' in text
    assert "lastTrigger" in text
    assert "event.key === \"Tab\"" in text
    assert "event.key === \"Escape\"" in text
    assert '<caption>' in text
    assert 'scope="col"' in text
    assert "@media (max-width: 680px)" in text
    assert "@media (prefers-reduced-motion: reduce)" in text
    assert "overflow-x: auto" in text


def test_v2_wireframe_is_synthetic_local_and_ascii_dash_clean() -> None:
    text = _wireframe()
    assert "synthetic data" in text
    assert "No live source is queried" in text
    assert not re.search(r'(?:src|href)="https?://', text)
    assert "\u2013" not in text
    assert "\u2014" not in text
