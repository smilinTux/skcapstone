"""Tests for the Fleet Drift panel: GET /api/fleet/drift and its sk-alert gate.

Epic 3bbf39ea, card d1c6d605. Builds a throwaway fleet tree (SKFLEET_ROOT)
holding one node in every state the panel has to render differently: clean,
error, warn, info, no role, no profile and no published inventory. Nothing
here touches the live ~/.skcapstone/fleet.

Two properties matter more than the output shape.

The three grades must stay three grades. A test that only asserted "node-info
is drifted" would still pass if someone escalated info to error, which is the
exact change that turns this panel into wallpaper.

And a node that cannot be graded must render as SKIPPED, not as clean. The
absent-vs-empty inventory distinction is covered from both sides here: an
absent inventory is a skip, while an empty one is a real observation that can
legitimately grade WARN.

No em/en dashes anywhere (SKWorld hard rule).
"""

from __future__ import annotations

import asyncio
import json

import pytest
from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths

from skdashboard import dashboard_fleet as df

WORKER_PROFILE = {
    "description": "gpu worker",
    "units": {
        "required": ["skai-beellama.service"],
        "allowed": ["skai-beellama.service"],
        "mustNot": ["skchat-daemon.service"],
    },
    "packages": {"required": [], "allowed": ["skcapstone"], "mustNot": ["skmemory"]},
    "unitsIgnore": ["gpg-agent*.socket"],
    "stateTier": "none",
    "capauthIdentityClass": "worker",
    "syncFolders": ["skfleet-control"],
}

REQUIRED_UNIT = "skai-beellama.service"
FORBIDDEN_UNIT = "skchat-daemon.service"


def _inventory(units: dict | None = None, packages: dict | None = None) -> dict:
    return {
        "units": {"user": units or {}},
        "packages": packages or {},
        "collectedAt": "2026-08-15T00:00:00Z",
    }


@pytest.fixture
def paths(tmp_path) -> FleetPaths:
    return FleetPaths(root=tmp_path / "fleet")


@pytest.fixture
def operator():
    return store.Writer(role="operator", node="node-158", identity="capauth:chef@skworld.io")


def _bind(paths, operator, name: str, role: str) -> None:
    store.write_spec(paths, "node", name, {"role": role, "cordoned": False}, writer=operator)


def _publish(paths, name: str, status: dict) -> None:
    """Publish a node.json as that node's own sknoded seat would."""
    writer = store.Writer(role="sknoded", node=name, identity="")
    store.write_node_file(paths, writer, "node.json", {"status": status})


@pytest.fixture
def fleet_tree(paths, operator):
    """One node per renderable state, so no state is covered by accident."""
    store.write_spec(paths, "profile", "worker-gpu", WORKER_PROFILE, writer=operator)

    _bind(paths, operator, "node-clean", "worker-gpu")
    _publish(
        paths,
        "node-clean",
        {"inventory": _inventory({REQUIRED_UNIT: "enabled"}, {"skcapstone": "1.0"})},
    )

    _bind(paths, operator, "node-error", "worker-gpu")
    _publish(
        paths,
        "node-error",
        {
            "inventory": _inventory(
                {REQUIRED_UNIT: "enabled", FORBIDDEN_UNIT: "enabled"}, {"skcapstone": "1.0"}
            )
        },
    )

    # Nothing enabled yet: a real observation of a node mid-install, which is
    # a warn, not an error.
    _bind(paths, operator, "node-warn", "worker-gpu")
    _publish(paths, "node-warn", {"inventory": _inventory()})

    _bind(paths, operator, "node-info", "worker-gpu")
    _publish(
        paths,
        "node-info",
        {
            "inventory": _inventory(
                {REQUIRED_UNIT: "enabled", "extra.service": "enabled"}, {"skcapstone": "1.0"}
            )
        },
    )

    _bind(paths, operator, "node-norole", "")
    _publish(paths, "node-norole", {"inventory": _inventory()})

    _bind(paths, operator, "node-noprofile", "ghost-role")
    _publish(paths, "node-noprofile", {"inventory": _inventory()})

    # Reported in, but on a build that publishes no inventory block.
    _bind(paths, operator, "node-noinv", "worker-gpu")
    _publish(paths, "node-noinv", {"capacity": {"cpu": 4}})

    return paths


def _by_node(rows: list[dict]) -> dict:
    return {row["node"]: row for row in rows}


# ---------------------------------------------------------------- report ---


def test_every_node_lands_in_exactly_one_bucket(fleet_tree) -> None:
    payload = df.collect_drift(fleet_tree)

    assert payload["errors"] == []
    graded = _by_node(payload["nodes"])
    skipped = _by_node(payload["skipped"])
    assert set(graded) == {"node-clean", "node-error", "node-warn", "node-info"}
    assert set(skipped) == {"node-norole", "node-noprofile", "node-noinv"}
    assert not set(graded) & set(skipped)
    assert payload["summary"] == {
        "graded": 4,
        "skipped": 3,
        "error": 1,
        "warn": 1,
        "info": 1,
        "ok": 1,
    }


def test_grades_are_not_flattened_into_one_drift_badge(fleet_tree) -> None:
    """forbidden is error, missing_required is warn, unexpected is info."""
    graded = _by_node(df.collect_drift(fleet_tree)["nodes"])

    assert graded["node-error"]["severity"] == "error"
    assert graded["node-error"]["forbidden_units"] == [FORBIDDEN_UNIT]
    assert graded["node-error"]["counts"] == {"error": 1, "warn": 0, "info": 0}

    assert graded["node-warn"]["severity"] == "warn"
    assert graded["node-warn"]["missing_required_units"] == [REQUIRED_UNIT]
    assert graded["node-warn"]["counts"] == {"error": 0, "warn": 1, "info": 0}

    assert graded["node-info"]["severity"] == "info"
    assert graded["node-info"]["unexpected_units"] == ["extra.service"]
    assert graded["node-info"]["counts"] == {"error": 0, "warn": 0, "info": 1}

    assert graded["node-clean"]["severity"] == "ok"
    assert graded["node-clean"]["findings"] == []


def test_findings_carry_their_grade_and_category(fleet_tree) -> None:
    findings = _by_node(df.collect_drift(fleet_tree)["nodes"])["node-error"]["findings"]
    assert findings == [
        {"grade": "error", "category": "forbidden_units", "name": FORBIDDEN_UNIT}
    ]


def test_worst_node_sorts_first(fleet_tree) -> None:
    names = [node["node"] for node in df.collect_drift(fleet_tree)["nodes"]]
    assert names == ["node-error", "node-warn", "node-info", "node-clean"]


def test_skipped_nodes_say_why_and_are_never_graded(fleet_tree) -> None:
    skipped = _by_node(df.collect_drift(fleet_tree)["skipped"])

    assert skipped["node-norole"]["reason_code"] == "no_role"
    assert skipped["node-noprofile"]["reason_code"] == "no_profile"
    assert skipped["node-noinv"]["reason_code"] == "no_inventory"
    for row in skipped.values():
        assert row["reason"], "a skip with no reason is indistinguishable from a pass"
        # No severity key at all: a skipped node must not be summable as clean.
        assert "severity" not in row


def test_absent_inventory_skips_but_an_empty_one_is_graded(fleet_tree) -> None:
    """The distinction the whole path exists to preserve.

    An absent inventory means the node has not reported; grading it would say
    "everything required is missing" about a machine nobody has looked at. An
    EMPTY inventory is a genuine observation and does grade.
    """
    payload = df.collect_drift(fleet_tree)
    skipped = _by_node(payload["skipped"])
    graded = _by_node(payload["nodes"])

    assert skipped["node-noinv"]["reason_code"] == "no_inventory"
    assert "node-noinv" not in graded
    assert graded["node-warn"]["missing_required_units"] == [REQUIRED_UNIT]


def test_missing_fleet_tree_is_an_empty_report_not_a_crash(tmp_path) -> None:
    payload = df.collect_drift(FleetPaths(root=tmp_path / "nope"))
    assert payload["nodes"] == []
    assert payload["skipped"] == []
    assert payload["summary"]["graded"] == 0


# ----------------------------------------------------------------- alert ---


class _Recorder:
    """A stand-in sk-alert sender that records instead of sending."""

    def __init__(self, ok: bool = True) -> None:
        self.messages: list[str] = []
        self.ok = ok

    def __call__(self, message: str) -> bool:
        self.messages.append(message)
        return self.ok


@pytest.fixture
def state_path(tmp_path):
    return tmp_path / "alert-state" / "fleet-drift-alert.json"


def test_alert_fires_on_an_error_grade_finding(fleet_tree, state_path) -> None:
    send = _Recorder()
    result = df.maybe_alert(
        df.collect_drift(fleet_tree), state_path=state_path, now=1000.0, send=send
    )

    assert result["fired"] is True
    assert result["reason"] == "fired"
    assert len(send.messages) == 1
    assert "node-error" in send.messages[0]
    assert FORBIDDEN_UNIT in send.messages[0]


def test_alert_does_not_fire_on_info_or_warn_grade(paths, operator, state_path) -> None:
    """Negative control. Noise must not be able to reach Chef's phone."""
    store.write_spec(paths, "profile", "worker-gpu", WORKER_PROFILE, writer=operator)
    _bind(paths, operator, "node-info", "worker-gpu")
    _publish(
        paths,
        "node-info",
        {"inventory": _inventory({REQUIRED_UNIT: "enabled", "extra.service": "enabled"})},
    )
    _bind(paths, operator, "node-warn", "worker-gpu")
    _publish(paths, "node-warn", {"inventory": _inventory()})

    payload = df.collect_drift(paths)
    assert {node["severity"] for node in payload["nodes"]} == {"info", "warn"}

    send = _Recorder()
    result = df.maybe_alert(payload, state_path=state_path, now=1000.0, send=send)

    assert send.messages == []
    assert result["fired"] is False
    assert result["fingerprint"] == []


def test_alert_is_edge_triggered_not_once_per_poll(fleet_tree, state_path) -> None:
    send = _Recorder()
    payload = df.collect_drift(fleet_tree)
    df.maybe_alert(payload, state_path=state_path, now=1000.0, send=send)

    # Same condition, many polls, hours apart: still one alert.
    for tick in (1001.0, 2000.0, 90000.0):
        result = df.maybe_alert(payload, state_path=state_path, now=tick, send=send)
        assert result["fired"] is False
        assert result["reason"] == "unchanged"
    assert len(send.messages) == 1


def test_a_new_finding_inside_the_cooldown_is_deferred_not_swallowed(
    fleet_tree, operator, state_path
) -> None:
    send = _Recorder()
    df.maybe_alert(df.collect_drift(fleet_tree), state_path=state_path, now=1000.0, send=send)

    _bind(fleet_tree, operator, "node-second", "worker-gpu")
    _publish(fleet_tree, "node-second", {"inventory": _inventory({FORBIDDEN_UNIT: "enabled"})})
    worse = df.collect_drift(fleet_tree)

    held = df.maybe_alert(worse, state_path=state_path, now=1100.0, send=send)
    assert held["fired"] is False
    assert held["reason"] == "rate_limited"
    assert len(send.messages) == 1

    after = df.maybe_alert(worse, state_path=state_path, now=1000.0 + 301, send=send)
    assert after["fired"] is True
    assert len(send.messages) == 2
    assert "node-second" in send.messages[1]


def test_a_failed_send_is_retried_after_the_cooldown(fleet_tree, state_path) -> None:
    payload = df.collect_drift(fleet_tree)
    broken = _Recorder(ok=False)
    result = df.maybe_alert(payload, state_path=state_path, now=1000.0, send=broken)
    assert result["fired"] is False
    assert result["reason"] == "send_failed"

    # Not recorded as delivered, so the same condition is still owed.
    working = _Recorder()
    again = df.maybe_alert(payload, state_path=state_path, now=1000.0 + 301, send=working)
    assert again["fired"] is True
    assert len(working.messages) == 1


def test_a_cleared_then_recurring_condition_alerts_again(fleet_tree, state_path) -> None:
    send = _Recorder()
    payload = df.collect_drift(fleet_tree)
    df.maybe_alert(payload, state_path=state_path, now=1000.0, send=send)

    cleared = df.maybe_alert(
        {"nodes": [], "skipped": [], "summary": {}}, state_path=state_path, now=1400.0, send=send
    )
    assert cleared["fired"] is False
    assert cleared["reason"] == "cleared"

    back = df.maybe_alert(payload, state_path=state_path, now=1500.0, send=send)
    assert back["fired"] is True
    assert len(send.messages) == 2


def test_alert_state_is_json_on_disk(fleet_tree, state_path) -> None:
    """Survives a dashboard restart, so a restart is not an alert."""
    df.maybe_alert(
        df.collect_drift(fleet_tree), state_path=state_path, now=1000.0, send=_Recorder()
    )
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_fire"] == 1000.0
    assert state["fingerprint"] == [f"node-error/forbidden_units/{FORBIDDEN_UNIT}"]


# ----------------------------------------------------------------- route ---


class _FakeHeaders(dict):
    def get(self, key, default=None):  # noqa: D102
        return super().get(key.lower(), default)


class FakeRequest:
    """Minimal stand-in for a Starlette ``Request``, as in the economy tests."""

    def __init__(self):
        self.headers = _FakeHeaders()
        self.path_params = {}
        self.query_params = {}


def _route_endpoint(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no route registered for path {path!r}")


def test_api_route_serves_the_report_and_runs_the_alert_gate(
    fleet_tree, tmp_path, monkeypatch
) -> None:
    from skdashboard.dashboard import create_app

    send = _Recorder()
    monkeypatch.setattr(df, "_default_send", send)
    monkeypatch.setenv("SKFLEET_ROOT", str(fleet_tree.root))

    app = create_app(tmp_path / "home")
    response = asyncio.run(_route_endpoint(app, "/api/fleet/drift")(FakeRequest()))

    assert response.status_code == 200
    payload = json.loads(response.body)
    assert _by_node(payload["nodes"])["node-error"]["severity"] == "error"
    assert payload["alert"]["fired"] is True
    assert len(send.messages) == 1


def test_fleet_page_is_routed_and_shipped() -> None:
    from pathlib import Path

    import skdashboard
    from skdashboard.dashboard import create_app

    app = create_app(Path("/nonexistent-home"))
    assert {getattr(r, "path", None) for r in app.routes} >= {"/fleet", "/api/fleet/drift"}
    static = Path(skdashboard.__file__).parent / "static"
    assert (static / "fleet.html").is_file()
    assert (static / "js" / "fleet.js").is_file()
    assert (static / "css" / "fleet.css").is_file()
