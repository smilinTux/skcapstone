"""Effective lanes: allowed AND route-health-ok, with in-cycle fall-through."""

from __future__ import annotations

import sys
from pathlib import Path

from skcapstone.fleet.lane_health import (
    effective_lanes_line,
    fallthrough_log_lines,
    preflight_through_ledger,
    select_lane_with_fallthrough,
)

sys.path.insert(0, str(Path(__file__).parent))
from test_skfleet_lane_affinity import _load_lane_helpers  # noqa: E402

LANES = ["qwen", "glm", "deepseek", "codex"]
MODELS = {
    "glm": "sk-glm-s",
    "deepseek": "sk-deepseek-s",
    "codex": "sk-codex-s",
    "qwen": "sk-qwen-s",
}


class Harness:
    def __init__(self, dead=(), disabled=(), now=1000.0):
        ns = _load_lane_helpers()
        ns["PRODUCTION_POLICY"] = {"lanes": {n: {"enabled": n not in disabled} for n in LANES}}
        self.select = ns["select_compatible_lane"]
        self.dead = set(dead)
        self.disabled = set(disabled)
        self.ledger: dict = {}
        self.probes: list[str] = []
        self.now = now
        # Highest free first, so the dead family is preferred and must be skipped.
        self.remaining = {
            n: (0 if n in disabled else {"glm": 9, "deepseek": 8, "codex": 7, "qwen": 0}[n])
            for n in LANES
        }

    def _probe(self, model):
        self.probes.append(model)
        if model in {MODELS[n] for n in self.dead}:
            raise ValueError("gateway rejected preflight with HTTP 429")
        return object()

    def route(self, labels):
        def preflight(model):
            result, why, _ = preflight_through_ledger(
                self.ledger, (model, "rev"), lambda: self._probe(model), now=self.now
            )
            return result is not None, why

        health = {n: (True, "healthy") for n in LANES}
        return select_lane_with_fallthrough(
            lambda h: self.select(
                labels, False, [{"name": n} for n in LANES], self.remaining, True, False, h
            ),
            lambda lane: MODELS[lane],
            preflight,
            health,
        )


def test_dead_family_falls_through_to_healthy_allowed_family():
    h = Harness(dead={"glm", "deepseek"})
    lane, _, hops = h.route([])
    assert lane == "codex"
    assert [x["from"] for x in hops] == ["glm", "deepseek"]
    assert [x["to"] for x in hops] == ["deepseek", "codex"]
    lines = fallthrough_log_lines("chiap08", "card1", hops)
    assert lines[0].startswith("ROUTE_FALLTHROUGH|chiap08|card1|from=glm|to=deepseek|reason=")


def test_pin_to_unhealthy_allowed_family_waits():
    h = Harness(dead={"glm"})
    lane, reason, hops = h.route(["glm-only"])
    assert lane is None
    assert reason.startswith("no-compatible-healthy-lane")
    assert hops[0]["to"] == "none"
    assert h.probes == ["sk-glm-s"]


def test_disabled_family_never_chosen_or_probed():
    h = Harness(dead={"glm"}, disabled={"deepseek"})
    lane, _, _ = h.route([])
    assert lane == "codex"
    assert "sk-deepseek-s" not in h.probes


def test_pin_to_disabled_family_follows_fallback_then_health():
    h = Harness(dead={"glm"}, disabled={"codex"})
    lane, _, hops = h.route(["codex-only"])
    assert lane == "deepseek"
    assert [x["from"] for x in hops] == ["glm"]


def test_all_allowed_dead_waits_without_disabled_family():
    h = Harness(dead={"glm", "deepseek", "codex"}, disabled={"qwen"})
    lane, reason, hops = h.route([])
    assert lane is None and len(hops) == 3


def test_recovery_reenables_after_failure_cache_expires():
    h = Harness(dead={"glm"})
    assert h.route([])[0] == "deepseek"
    h.dead.clear()
    h.now += 5  # failure still cached: stays withdrawn, no new traffic
    assert h.route([])[0] == "deepseek"
    assert h.probes.count("sk-glm-s") == 1
    h.now += 120  # failure TTL elapsed, probe passes
    assert h.route([])[0] == "glm"
    assert h.probes.count("sk-glm-s") == 2


def test_no_extra_probes_per_cycle():
    h = Harness(dead={"glm"})
    for _ in range(5):
        h.route([])
    assert sorted(h.probes) == ["sk-deepseek-s", "sk-glm-s"]


def test_effective_lanes_line_reports_allowed_healthy_unhealthy():
    lanes = [
        {"name": "glm", "target": 4},
        {"name": "codex", "target": 4},
        {"name": "deepseek", "target": 0},
    ]
    line = effective_lanes_line("chiap08", lanes, {"glm": "HTTP 429", "deepseek": "x"})
    assert line == (
        "EFFECTIVE_LANES|chiap08|allowed=glm,codex|healthy=codex|unhealthy=glm:HTTP 429"
    )
