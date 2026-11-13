"""Fleet actuation target, unit mapping, postcondition and source time (card 21b01d01).

Found by the independent review of #1357 (gate 5 hardening):

* ``act_dispatch`` restarted ``systemctl --user <fleet object name>`` on the LOCAL node.
  It ignored placement, and the object name is not always the unit (``capauth-keystore``
  runs as ``capauth-authz.service``).
* ``verify_postcondition`` re-read the store right after the restart, so it saw the
  pre-action status and a real restart came out FAILED and escalated.
* ``fleet_observe`` stamped ``source_observed_at`` with the pass clock.

These tests pin the fixed behaviour: the unit comes from the fleet spec and nothing else,
an object placed on another node never actuates here, the postcondition is judged only on
evidence written after the action (polled for a bounded settle window), and every fleet
condition carries the time its node actually wrote the status it was derived from.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import (
    act_dispatch,
    action_ledger,
    actuator,
    dispatch,
    fleet_adapter,
    policy,
)

LOCAL = "node-local-a"
REMOTE = "node-remote-b"
OBJECT = "capauth-keystore"
UNIT = "capauth-authz.service"

HUMAN = store.Writer(role="operator", node=LOCAL, identity="capauth:test@scratch")
OPERATOR = store.Writer(role="operator", node=LOCAL, identity="operator")
SCHEDULER = store.Writer(role="scheduler", node=LOCAL, identity="scheduler")


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_NODE", LOCAL)
    p = FleetPaths(root=tmp_path / "fleet")
    store.set_frozen(p, False, writer=HUMAN, reason="test provisioning")
    return p


def _service(paths, *, name=OBJECT, unit=UNIT, node=LOCAL, runtime=None, **extra):
    spec = {"unit": unit, **extra} if unit is not None else dict(extra)
    if runtime is not None:
        spec["runtime"] = runtime
    payload = store.write_spec(paths, "service", name, spec, writer=OPERATOR)
    if node is not None:
        store.write_placement(paths, "service", name, node=node, reason="test", writer=SCHEDULER)
    return payload


def _status(paths, *, ready: bool, name=OBJECT, node=LOCAL, pid=100, updated_at=None):
    """Write a node status the way converge does (sknoded role, write-on-change)."""
    spec = store.read_spec(paths, "service", name)
    writer = store.Writer(role="sknoded", node=node, identity="sknoded")
    store.write_status(
        paths,
        "service",
        name,
        node=node,
        status={"state": "active" if ready else "failed", "pid": pid, "since": str(pid)},
        conditions=[{"type": "Ready", "status": "True" if ready else "False"}],
        observed_generation=int(spec["generation"]),
        writer=writer,
    )
    if updated_at is not None:
        path = paths.status_path(node, "service", name)
        data = json.loads(path.read_text())
        data["updatedAt"] = updated_at
        path.write_text(json.dumps(data))


def _runner(calls):
    def runner(cmd, **_kw):
        calls.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    return runner


def _ready_row(observation, name=OBJECT):
    return next(
        c for c in observation["conditions"] if c["type"] == "Ready" and c["object"] == name
    )


# ---------------------------------------------------------------------------
# 4. A real source time
# ---------------------------------------------------------------------------


def test_fleet_observe_stamps_each_condition_with_its_status_write_time(paths):
    _service(paths)
    _status(paths, ready=False, updated_at="2026-11-13T10:00:00Z")
    out = fleet_adapter.fleet_observe(paths, "2030-01-01T00:00:00Z")
    row = _ready_row(out)
    assert row["source_observed_at"] == "2026-11-13T10:00:00Z"
    # The pass clock is not a source time and is never reported as one.
    assert "source_observed_at" not in out
    assert all(c.get("source_observed_at") != "2030-01-01T00:00:00Z" for c in out["conditions"])


def test_fleet_condition_without_a_status_has_no_source_time(paths):
    _service(paths)
    out = fleet_adapter.fleet_observe(paths, "2030-01-01T00:00:00Z")
    assert "source_observed_at" not in _ready_row(out)


def test_fleet_node_conditions_carry_the_node_report_write_time(paths):
    writer = store.Writer(role="sknoded", node=LOCAL, identity="sknoded")
    store.write_node_file(
        paths,
        writer,
        "node.json",
        {"name": LOCAL, "conditions": [{"type": "DiskPressure", "status": "False"}]},
    )
    path = paths.node_status_dir(LOCAL) / "node.json"
    data = json.loads(path.read_text())
    data["updatedAt"] = "2026-11-13T09:00:00Z"
    path.write_text(json.dumps(data))
    store.write_spec(paths, "node", LOCAL, {}, writer=OPERATOR)
    out = fleet_adapter.fleet_observe(paths, "2030-01-01T00:00:00Z")
    disk = next(c for c in out["conditions"] if c["type"] == "DiskPressure")
    assert disk["source_observed_at"] == "2026-11-13T09:00:00Z"


# ---------------------------------------------------------------------------
# 1 + 2. Placement-aware target and object-to-unit mapping
# ---------------------------------------------------------------------------


def _apply(paths, calls, itil=None):
    return act_dispatch.build_apply_fn(
        paths, "2026-11-13T00:00:00Z", runner=_runner(calls), itil=itil
    )


_STD = {"change_class": "standard", "risk": "low", "auto_approvable": True}


def test_restart_uses_the_unit_from_the_spec_not_the_object_name(paths):
    _service(paths)
    calls = []
    out = _apply(paths, calls)({"action": "restart_service", "object": OBJECT, "ts": "t"}, _STD)
    assert out["actuation"]["performed"] is True
    assert calls == [["systemctl", "--user", "restart", UNIT]]
    assert out["actuation"]["unit"] == UNIT
    assert out["actuation"]["node"] == LOCAL


class _ITIL:
    def __init__(self):
        self.changes = []

    def propose_change(self, **kwargs):
        self.changes.append(kwargs)
        return type("Chg", (), {"id": "chg-1"})()


@pytest.mark.parametrize(
    ("setup", "proposal_extra", "needle"),
    [
        (dict(node=REMOTE), {}, f"placed on {REMOTE!r}"),
        (dict(node=None), {}, "no placement"),
        (dict(runtime="docker"), {}, "runtime 'docker'"),
        (dict(unit=None), {}, "no unit mapping"),
        (dict(unit="-bad unit"), {}, "not a plain systemd unit name"),
        (dict(paused=True), {}, "paused"),
        (dict(), {"unit": "something-else.service"}, "proposal names unit"),
    ],
)
def test_unresolvable_targets_are_refused_before_anything_runs(
    paths, setup, proposal_extra, needle
):
    _service(paths, **setup)
    calls = []
    itil = _ITIL()
    prop = {"action": "restart_service", "object": OBJECT, "ts": "t", **proposal_extra}
    with pytest.raises(actuator.NotAttemptedError) as err:
        _apply(paths, calls, itil)(prop, _STD)
    assert needle in str(err.value)
    assert calls == [], "nothing may reach systemctl for a refused target"
    assert itil.changes == [], "a refused target opens no ITIL change"
    spec = store.read_spec(paths, "service", OBJECT)["spec"]
    assert "operatorActions" not in spec, "a refused target is not annotated as acted on"


def test_unknown_object_is_refused(paths):
    calls = []
    with pytest.raises(actuator.NotAttemptedError, match="unknown service"):
        _apply(paths, calls)({"action": "restart_service", "object": "ghost", "ts": "t"}, _STD)
    assert calls == []


def test_fleet_actions_without_physical_muscle_are_refused_not_restarted(paths):
    store.write_spec(
        paths, "cronjob", "nightly", {"schedule": "@daily", "command": "echo"}, writer=OPERATOR
    )
    calls = []
    with pytest.raises(actuator.NotAttemptedError, match="no physical muscle"):
        _apply(paths, calls)({"action": "rerun_cronjob", "object": "nightly", "ts": "t"}, _STD)
    assert calls == [], "a cronjob must never be 'restarted' as if it were a service unit"


def test_rollback_resolves_the_same_way(paths):
    _service(paths, node=REMOTE)
    calls = []
    rollback = act_dispatch.build_rollback_fn(paths, runner=_runner(calls))
    prop = {
        "action": "restart_service",
        "object": OBJECT,
        "rollback": {"action": "restart_service"},
    }
    with pytest.raises(actuator.NotAttemptedError):
        rollback(prop, _STD, None)
    assert calls == []


def test_no_fleet_action_is_newly_ratified():
    # The card's rule: ratification is Chef's. This fix must not widen the catalog.
    assert policy.RATIFIED_STANDARD_CATALOG == frozenset(
        {"restart_service", "rotate_credential", "rotate_log", "update_label"}
    )


# ---------------------------------------------------------------------------
# 3. A postcondition that can pass
# ---------------------------------------------------------------------------


class _Observer:
    def __init__(self, items):
        self.items = list(items)
        self.calls = 0

    def __call__(self, _paths, _now):
        self.calls += 1
        current = self.items[min(self.calls - 1, len(self.items) - 1)]
        return {"conditions": [current]}


def _row(status, stamp):
    return {"type": "Ready", "status": status, "object": OBJECT, "source_observed_at": stamp}


_ACTED = "2026-11-13T12:00:10Z"
_RESULT = {"actuation": {"performed": True, "acted_at": _ACTED, "settle_s": 20, "poll_s": 5}}
_PROP = {"app": "fleet", "condition": "Ready", "object": OBJECT}


def test_postcondition_waits_for_evidence_newer_than_the_action(paths):
    obs = _Observer(
        [
            _row("True", "2026-11-13T12:00:00Z"),  # healthy, but written BEFORE the action
            _row("True", "2026-11-13T12:00:00Z"),
            _row("True", "2026-11-13T12:00:20Z"),  # the node re-observed after the restart
        ]
    )
    sleeps = []
    ok, reason = dispatch.verify_postcondition(
        {"fleet": obs}, _PROP, paths, "x", set(), result=_RESULT, sleep=sleeps.append
    )
    assert ok is True, reason
    assert sleeps == [5, 5]


def test_postcondition_never_accepts_pre_action_evidence(paths):
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    sleeps = []
    ok, reason = dispatch.verify_postcondition(
        {"fleet": obs}, _PROP, paths, "x", set(), result=_RESULT, sleep=sleeps.append
    )
    assert ok is False
    assert "no evidence newer than the action" in reason
    # Bounded: settle 20 s polled every 5 s is 5 reads and 4 sleeps, never more.
    assert obs.calls == 5
    assert sleeps == [5, 5, 5, 5]


def test_postcondition_fails_on_fresh_evidence_that_is_still_firing(paths):
    obs = _Observer([_row("False", "2026-11-13T12:00:30Z")])
    ok, reason = dispatch.verify_postcondition(
        {"fleet": obs}, _PROP, paths, "x", set(), result=_RESULT, sleep=lambda _s: None
    )
    assert ok is False
    assert "still firing" in reason


def test_postcondition_settle_window_is_capped(paths):
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    huge = {"actuation": {**_RESULT["actuation"], "settle_s": 10**9, "poll_s": 1}}
    ok, _ = dispatch.verify_postcondition(
        {"fleet": obs}, _PROP, paths, "x", set(), result=huge, sleep=lambda _s: None
    )
    assert ok is False
    assert obs.calls <= dispatch.MAX_POSTCONDITION_SETTLE_S + 1


def test_postcondition_without_an_action_time_is_unchanged(paths):
    # An actuation that does not say when it acted (skchat's live probes) keeps the
    # single immediate re-read.
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    ok, _ = dispatch.verify_postcondition({"fleet": obs}, _PROP, paths, "x", set())
    assert ok is True
    assert obs.calls == 1


def test_fleet_actuation_reports_when_it_acted(paths):
    _service(paths)
    before = datetime.now(timezone.utc).replace(microsecond=0)
    out = _apply(paths, [])({"action": "restart_service", "object": OBJECT, "ts": "t"}, _STD)
    acted = datetime.fromisoformat(out["actuation"]["acted_at"].replace("Z", "+00:00"))
    assert before <= acted <= datetime.now(timezone.utc)
    assert 0 < out["actuation"]["settle_s"] <= dispatch.MAX_POSTCONDITION_SETTLE_S


# ---------------------------------------------------------------------------
# 5. End to end through dispatch_intent with the REAL build_apply_fn
# ---------------------------------------------------------------------------


def _intent(ledger, change_id, evidence_at):
    created = datetime.now(timezone.utc) - timedelta(seconds=5)
    intent = action_ledger.ActionIntent(
        condition_fingerprint=f"fp-fleet-restart-{OBJECT}",
        application="fleet",
        target_kind="CI",
        target_id=OBJECT,
        action="restart_service",
        catalog_generation="gen-0",
        created_at=created,
        itil_change_id=change_id,
        rollback={},
        condition_type="Ready",
    )
    ledger.create(intent, actor="test")
    ledger.append(
        intent.intent_id, action_ledger.ActionState.DIAGNOSED, occurred_at=created, actor="test"
    )
    ledger.append(
        intent.intent_id,
        action_ledger.ActionState.PROPOSED,
        occurred_at=created,
        actor="test",
        detail={
            "disposition": "auto",
            "evidence": {
                "source": f"observation:fleet/Ready/{OBJECT}",
                "observed_at": evidence_at,
            },
        },
    )
    return intent.intent_id


def _world(tmp_path, paths, *, node=LOCAL):
    _service(paths, node=node)
    _status(paths, ready=False, node=node)
    stamp = store.read_status(paths, "service", OBJECT, node)["updatedAt"]
    itil = ITILManager(tmp_path / "itil")
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    chg = itil.propose_change(
        title=f"restart {OBJECT}",
        change_type="standard",
        risk="low",
        rollback_plan="restart again",
        created_by="atlas",
        tags=["operator", f"op-bind:fleet|restart_service|{OBJECT}"],
    )
    return itil, ledger, _intent(ledger, chg.id, stamp)


def test_e2e_fleet_restart_verifies_once_the_node_reports_after_the_action(tmp_path, paths):
    itil, ledger, intent_id = _world(tmp_path, paths)
    calls = []
    sleeps = []

    def sleep(seconds):
        # The node's sknoded converge pass notices the restarted unit (new pid) on its
        # second poll and writes a fresh status against the current spec generation.
        sleeps.append(seconds)
        if len(sleeps) == 2:
            _status(paths, ready=True, pid=200)

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"fleet": fleet_adapter.fleet_observe},
        problem_types=set(fleet_adapter.PROBLEM_WHEN_TRUE),
        apply_fn=act_dispatch.build_apply_fn(
            paths, _iso(datetime.now(timezone.utc)), runner=_runner(calls), itil=itil
        ),
        now_iso=_iso(datetime.now(timezone.utc)),
        postcondition_sleep=sleep,
    )
    assert outcome.outcome == "verified", outcome.outcome
    assert calls == [["systemctl", "--user", "restart", UNIT]]
    assert len(sleeps) == 2
    lineage = [e.state.value for e in ledger.events(intent_id)]
    assert lineage[-3:] == ["authorized", "executing", "verified"]


def test_e2e_fleet_restart_fails_when_no_fresh_status_arrives(tmp_path, paths):
    itil, ledger, intent_id = _world(tmp_path, paths)
    calls = []
    sleeps = []
    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"fleet": fleet_adapter.fleet_observe},
        problem_types=set(fleet_adapter.PROBLEM_WHEN_TRUE),
        apply_fn=act_dispatch.build_apply_fn(
            paths, _iso(datetime.now(timezone.utc)), runner=_runner(calls), itil=itil
        ),
        now_iso=_iso(datetime.now(timezone.utc)),
        postcondition_sleep=sleeps.append,
    )
    assert outcome.outcome.startswith("failed:"), outcome.outcome
    assert calls == [["systemctl", "--user", "restart", UNIT]]
    assert sleeps, "the postcondition must poll before giving up"
    assert "failed" in [e.state.value for e in ledger.events(intent_id)]


def test_e2e_remote_placement_never_actuates_locally(tmp_path, paths):
    itil, ledger, intent_id = _world(tmp_path, paths, node=REMOTE)
    calls = []
    state = _SpyState()
    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"fleet": fleet_adapter.fleet_observe},
        problem_types=set(fleet_adapter.PROBLEM_WHEN_TRUE),
        apply_fn=act_dispatch.build_apply_fn(
            paths, _iso(datetime.now(timezone.utc)), runner=_runner(calls), itil=itil
        ),
        execution_state=state,
        now_iso=_iso(datetime.now(timezone.utc)),
        postcondition_sleep=lambda _s: None,
    )
    assert calls == [], "an object placed on another node must not be restarted here"
    assert outcome.outcome.startswith("escalated:"), outcome.outcome
    assert f"placed on {REMOTE!r}" in outcome.outcome
    failed = next(e for e in ledger.events(intent_id) if e.state.value == "failed")
    assert failed.detail.get("preflight_refusal") is True
    assert state.recorded == [], "a refusal before actuation never feeds the circuit breaker"


class _SpyState:
    def __init__(self):
        self.recorded = []

    def eligibility(self, _fp, _now):
        return True, ""

    def record(self, *args, **kwargs):
        self.recorded.append((args, kwargs))
