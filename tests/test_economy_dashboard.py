"""Tests for GET /api/economy: the fleet-wide Economy view (autopilot cost
ledger + joule wallet wealth) that :mod:`skdashboard.dashboard_economy`
assembles for the dashboard.

Drives the real route handler (extracted from
:func:`skdashboard.dashboard.create_app`) with a fake Starlette request,
mirroring the pattern in test_queue_gate_enforcement.py. Seeds a tmp
SKAI_COST_DIR (autopilot_cost ledger) and a tmp agent home (skjoule wallets)
so neither test touches the live ~/.skcapstone/autopilot-cost/ or
~/.skcapstone/agents/*/wallet/.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import tempfile
from pathlib import Path

import pytest

from skdashboard.dashboard import create_app

# Both tests here assume skharness is importable: one calls
# skharness.autocode.autopilot_cost.record_run directly, and the other asserts
# `errors == []` and a full 30-point cost series, which only hold when the cost
# ledger backend is present. skharness is a private sibling monorepo package
# that is NOT on PyPI, so CI cannot install it. Skip loudly rather than assert
# against a degraded shape: a skipped test says "not covered here", a relaxed
# assertion would say "covered" while checking nothing.
pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("skharness") is None,
    reason="optional private sibling skharness is not installed (not published to PyPI)",
)


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)


class FakeRequest:
    """Minimal stand-in for a Starlette ``Request`` -- only what a bare GET
    handler touches: ``.headers``, ``.path_params``, ``.query_params``."""

    def __init__(self):
        self.headers = _FakeHeaders()
        self.path_params = {}
        self.query_params = {}


def _route_endpoint(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no route registered for path {path!r}")


def _call(handler, request):
    return asyncio.run(handler(request))


@pytest.fixture
def home():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def app(home):
    return create_app(home)


@pytest.fixture(autouse=True)
def _isolate_cost_dir(monkeypatch, tmp_path):
    """Never touch the live ~/.skcapstone/autopilot-cost/."""
    monkeypatch.setenv("SKAI_COST_DIR", str(tmp_path / "autopilot-cost"))


def _get_economy(app):
    handler = _route_endpoint(app, "/api/economy")
    response = _call(handler, FakeRequest())
    assert response.status_code == 200
    return json.loads(response.body)


# --------------------------------------------------------------------------- #
# route registration + shape on an empty install                              #
# --------------------------------------------------------------------------- #


def test_economy_route_and_page_are_registered(app):
    assert _route_endpoint(app, "/api/economy") is not None
    assert any(getattr(r, "path", None) == "/economy" for r in app.routes)


def test_api_economy_shape_with_no_data(app):
    """No ledger, no settlements, no wallets: every top-level key must still
    be present and well-formed -- never a 500, never a missing section."""
    d = _get_economy(app)

    for key in ("autopilot_cost", "cost_series", "settlements", "joule_economy", "errors"):
        assert key in d

    assert isinstance(d["cost_series"], list)
    assert len(d["cost_series"]) == 30

    assert isinstance(d["settlements"], list)
    assert d["settlements"] == []

    ac = d["autopilot_cost"]
    assert ac["today"] == {"cost_usd": 0.0, "joules": 0, "tokens": 0, "runs": 0}
    assert ac["by_repo"] == {}

    je = d["joule_economy"]
    assert isinstance(je["agents"], list)
    assert je["agents"] == []          # no wallets on disk -> degrades cleanly
    assert je["active_agents"] == 0
    assert je["total_supply"] == 0

    assert d["errors"] == []           # skharness + skcapstone are both installed here


# --------------------------------------------------------------------------- #
# seeded ledger + settlements + wallets                                      #
# --------------------------------------------------------------------------- #


def test_api_economy_reflects_seeded_ledger_and_wallets(app, home):
    from skharness.autocode import autopilot_cost

    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    today_iso = today.date().isoformat()

    autopilot_cost.record_run(
        card_id="task-1", repo="skrender", tokens=1000, cost_usd=2.0,
        passed=True, pr="", ts=f"{today_iso}T00:00:00+00:00",
    )
    autopilot_cost.record_settlement(
        card_id="task-1", commit_sha="deadbeef", agent="lumina",
        minted=100, spent=25, net=75, balance_after=575,
        ts=f"{today_iso}T00:00:01+00:00",
    )

    from skcapstone.skjoule import JouleWallet

    wallet = JouleWallet("lumina", home=home)
    wallet.mint(575, description="seed for test")

    d = _get_economy(app)

    ac = d["autopilot_cost"]
    assert ac["today"]["runs"] == 1
    assert ac["today"]["cost_usd"] == pytest.approx(2.0)
    assert ac["by_repo"]["skrender"]["runs"] == 1

    series_today = [row for row in d["cost_series"] if row["date"] == today_iso]
    assert len(series_today) == 1
    assert series_today[0]["runs"] == 1

    assert len(d["settlements"]) == 1
    assert d["settlements"][0]["card_id"] == "task-1"
    assert d["settlements"][0]["agent"] == "lumina"

    je = d["joule_economy"]
    assert je["active_agents"] == 1
    assert je["total_supply"] == 575
    assert je["agents"] == [{"agent": "lumina", "balance": 575, "level": "Practitioner"}]

    assert d["errors"] == []


def test_api_economy_wallets_sorted_balance_descending(app, home):
    from skcapstone.skjoule import JouleWallet

    JouleWallet("lumina", home=home).mint(500, description="seed")
    JouleWallet("opus", home=home).mint(2000, description="seed")
    JouleWallet("jarvis", home=home).mint(10, description="seed")

    d = _get_economy(app)
    balances = [a["balance"] for a in d["joule_economy"]["agents"]]
    assert balances == sorted(balances, reverse=True)
    assert d["joule_economy"]["agents"][0]["agent"] == "opus"
