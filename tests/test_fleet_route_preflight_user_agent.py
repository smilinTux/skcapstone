"""The preflight probe must not be rejected for how it identifies itself.

The gateway forwards the caller's User-Agent upstream. Measured on chiap01
2026-09-18: an identical request body returns HTTP 403 with python-urllib's
default agent and does not with a conventional one, so every kimi preflight
failed and the dispatcher logged ROUTE_PREFLIGHT_BLOCKED and launched nothing
on any host whose owned card routed to kimi.

The same request also must not carry temperature. The kimi backends reject
temperature 0, which fails the probe on exactly the backends most likely to
need probing. scripts/gateway/skgw-warm already omits it for that reason.
"""

from __future__ import annotations

import json
import pathlib

SRC = (
    pathlib.Path(__file__).resolve().parents[1] / "src" / "skcapstone" / "fleet_route_preflight.py"
)


def _source() -> str:
    return SRC.read_text(encoding="utf-8")


def test_preflight_sends_an_explicit_user_agent() -> None:
    src = _source()
    assert "PREFLIGHT_USER_AGENT" in src
    assert '"User-Agent": PREFLIGHT_USER_AGENT' in src


def test_preflight_user_agent_is_not_the_python_default() -> None:
    from skcapstone.fleet_route_preflight import PREFLIGHT_USER_AGENT

    assert PREFLIGHT_USER_AGENT
    assert "python" not in PREFLIGHT_USER_AGENT.lower()
    assert "urllib" not in PREFLIGHT_USER_AGENT.lower()


def test_preflight_body_omits_temperature() -> None:
    """kimi rejects temperature 0, so the probe must not send one at all."""
    src = _source()
    start = src.index('"messages": [{"role": "user", "content": "Reply OK."}]')
    window = src[start : start + 400]
    assert '"temperature"' not in window


def test_preflight_budget_allows_one_visible_token() -> None:
    """A probe too small to produce output reports a healthy route as unhealthy.

    Measured on the live chi gateway 2026-09-18, identical body varying only
    max_tokens: 1 and 8 return HTTP 502 (empty upstream response), while 32, 64
    and 128 return 200 with content "OK". kimi is a reasoning model and spends a
    tiny budget entirely on reasoning.
    """
    from skcapstone.fleet_route_preflight import PREFLIGHT_MAX_TOKENS

    assert PREFLIGHT_MAX_TOKENS >= 32
    assert '"max_tokens": PREFLIGHT_MAX_TOKENS' in _source()
