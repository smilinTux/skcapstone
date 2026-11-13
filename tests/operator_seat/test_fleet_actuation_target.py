"""Fleet actuation target, unit mapping, postcondition and source time (card 21b01d01).

Found by the independent review of #1357 (gate 5 hardening), then tightened by the review
of #1359:

* ``act_dispatch`` restarted ``systemctl --user <fleet object name>`` on the LOCAL node.
  It ignored placement, and the object name is not always the unit (``capauth-keystore``
  runs as ``capauth-authz.service``).
* ``verify_postcondition`` re-read the store right after the restart, so it saw the
  pre-action status and a real restart came out FAILED and escalated.
* ``fleet_observe`` stamped ``source_observed_at`` with the pass clock.
* (#1359 review) a brain-supplied rollback could name another object; ``spec.unit`` was
  trusted unsigned and could be a target or timer; a refusal fed the inline breaker.

These tests pin the fixed behaviour: the unit comes from a SIGNED fleet spec, must be a
``.service`` allowed by the node's profile, the object must be placed on this node, a
rollback is bound to the approved object, the postcondition is judged only on evidence
written strictly after the action (polled for a bounded settle window, never past the pass
deadline), and every fleet condition carries the time its node wrote the status file.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
    loop,
    policy,
)

from .test_loop import _EV_0820, _clock_for, _enroll
from .test_loop import _provision as _provision_loop

LOCAL = "node-local-a"
REMOTE = "node-remote-b"
OBJECT = "capauth-keystore"
UNIT = "capauth-authz.service"

HUMAN = store.Writer(role="operator", node=LOCAL, identity="capauth:test@scratch")
OPERATOR = store.Writer(role="operator", node=LOCAL, identity="operator")
SCHEDULER = store.Writer(role="scheduler", node=LOCAL, identity="scheduler")


def _sign(data: bytes) -> str:
    return "sig-" + hashlib.sha256(data).hexdigest()


def _verify_sig(data: bytes, signature: str) -> bool:
    return signature == _sign(data)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_NODE", LOCAL)
    # Store writes sign only through the explicit test signer; the host's own signing
    # mode must not leak a real signature into these fixtures.
    monkeypatch.setenv("SKFLEET_SIGNING", "off")
    monkeypatch.setenv("SKFLEET_PROFILE_MANIFESTS", str(tmp_path / "no-manifests"))
    p = FleetPaths(root=tmp_path / "fleet")
    store.set_frozen(p, False, writer=HUMAN, reason="test provisioning")
    return p


def _service(paths, *, name=OBJECT, unit=UNIT, node=LOCAL, runtime=None, signed=True, **extra):
    spec = {"unit": unit, **extra} if unit is not None else dict(extra)
    if runtime is not None:
        spec["runtime"] = runtime
    signer = _sign if signed else None
    payload = store.write_spec(paths, "service", name, spec, writer=OPERATOR, signer=signer)
    if node is not None:
        store.write_placement(
            paths, "service", name, node=node, reason="test", writer=SCHEDULER, signer=signer
        )
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


def test_fleet_source_time_is_read_from_the_placed_node(paths):
    # Another node's status sorts first on disk; the placed node's is the one that counts.
    _service(paths, node="zz-placed")
    _status(paths, ready=False, node="aa-other", updated_at="2026-11-13T09:00:00Z")
    _status(paths, ready=False, node="zz-placed", updated_at="2026-11-13T11:00:00Z")
    out = fleet_adapter.fleet_observe(paths, "2030-01-01T00:00:00Z")
    assert _ready_row(out)["source_observed_at"] == "2026-11-13T11:00:00Z"


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
        paths, "2026-11-13T00:00:00Z", runner=_runner(calls), itil=itil, verifier=_verify_sig
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


def test_a_bare_unit_name_is_normalized_to_a_service(paths):
    _service(paths, unit="capauth-authz")
    calls = []
    _apply(paths, calls)({"action": "restart_service", "object": OBJECT, "ts": "t"}, _STD)
    assert calls == [["systemctl", "--user", "restart", "capauth-authz.service"]]


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
        (dict(unit="default.target"), {}, "is a .target unit"),
        (dict(unit="capauth-backup.timer"), {}, "is a .timer unit"),
        (dict(paused=True), {}, "is paused"),
        (dict(deleted=True), {}, "is deleted"),
        (dict(signed=False), {}, "not verified"),
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
    assert fleet_adapter.fleet_target_known(paths, prop, verifier=_verify_sig) is False


def test_no_trust_roster_refuses(paths):
    _service(paths)
    with pytest.raises(actuator.NotAttemptedError, match="no verifier"):
        fleet_adapter.resolve_restart_target(
            paths, {"action": "restart_service", "object": OBJECT}, verifier=None
        )


def test_an_unsigned_placement_refuses(paths):
    store.write_spec(paths, "service", OBJECT, {"unit": UNIT}, writer=OPERATOR, signer=_sign)
    store.write_placement(paths, "service", OBJECT, node=LOCAL, reason="t", writer=SCHEDULER)
    with pytest.raises(actuator.NotAttemptedError, match="placement unsigned"):
        fleet_adapter.resolve_restart_target(
            paths, {"action": "restart_service", "object": OBJECT}, verifier=_verify_sig
        )


def test_a_unit_outside_the_node_profile_is_refused(paths, tmp_path, monkeypatch):
    shipped = Path(__file__).resolve().parents[2] / "deploy/fleet-objects/profile/control.json"
    manifest = json.loads(shipped.read_text())
    units = manifest["spec"]["units"]
    units["allowed"] = [u for u in units.get("allowed", []) if u != UNIT]
    units["required"] = [u for u in units.get("required", []) if u != UNIT]
    units["mustNot"] = sorted(set(units.get("mustNot", [])) | {UNIT})
    mdir = tmp_path / "manifests"
    mdir.mkdir()
    (mdir / "control.json").write_text(json.dumps(manifest))
    monkeypatch.setenv("SKFLEET_PROFILE_MANIFESTS", str(mdir))
    store.write_spec(paths, "node", LOCAL, {"role": "control"}, writer=OPERATOR)
    _service(paths)
    calls = []
    with pytest.raises(actuator.NotAttemptedError, match="forbidden for node"):
        _apply(paths, calls)({"action": "restart_service", "object": OBJECT, "ts": "t"}, _STD)
    assert calls == []


def test_target_known_accepts_a_resolvable_target(paths):
    _service(paths)
    prop = {"action": "restart_service", "object": OBJECT}
    assert fleet_adapter.fleet_target_known(paths, prop, verifier=_verify_sig) is True


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


def test_pause_and_placement_are_rechecked_immediately_before_the_restart(paths):
    _service(paths)
    target = fleet_adapter.resolve_restart_target(
        paths, {"action": "restart_service", "object": OBJECT}, verifier=_verify_sig
    )
    store.write_spec(paths, "service", OBJECT, {"unit": UNIT, "paused": True}, writer=OPERATOR)
    calls = []
    with pytest.raises(actuator.NotAttemptedError, match="paused"):
        act_dispatch._honor_fleet(paths, {"ts": "t"}, target, runner=_runner(calls))
    store.write_spec(paths, "service", OBJECT, {"unit": UNIT}, writer=OPERATOR)
    store.write_placement(paths, "service", OBJECT, node=REMOTE, reason="moved", writer=SCHEDULER)
    with pytest.raises(actuator.NotAttemptedError, match="placed on"):
        act_dispatch._honor_fleet(paths, {"ts": "t"}, target, runner=_runner(calls))
    assert calls == []


# --- rollback is bound to the approved object ---------------------------------


def _rollback(paths, calls):
    return act_dispatch.build_rollback_fn(paths, runner=_runner(calls), verifier=_verify_sig)


def test_rollback_restarts_the_approved_unit(paths):
    _service(paths)
    calls = []
    prop = {
        "action": "restart_service",
        "object": OBJECT,
        "rollback": {"action": "restart_service"},
    }
    out = _rollback(paths, calls)(prop, _STD, None)
    assert out["performed"] is True
    assert calls == [["systemctl", "--user", "restart", UNIT]]


@pytest.mark.parametrize(
    "rollback",
    [
        {"action": "restart_service", "object": "other"},  # restarts default.target
        {"action": "restart_service", "unit": "default.target"},
        {"action": "restart-daemon", "object": "skchat-daemon"},  # leaves the fleet adapter
    ],
)
def test_rollback_naming_another_object_or_unit_is_refused(paths, rollback):
    _service(paths)
    _service(paths, name="other", unit="default.target")
    calls = []
    prop = {"action": "restart_service", "object": OBJECT, "rollback": rollback}
    with pytest.raises(actuator.NotAttemptedError, match="refused|proposal names unit"):
        _rollback(paths, calls)(prop, _STD, None)
    assert calls == []


def test_rollback_of_a_remote_object_is_refused(paths):
    _service(paths, node=REMOTE)
    calls = []
    prop = {
        "action": "restart_service",
        "object": OBJECT,
        "rollback": {"action": "restart_service"},
    }
    with pytest.raises(actuator.NotAttemptedError):
        _rollback(paths, calls)(prop, _STD, None)
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
    item = {"type": "Ready", "status": status, "object": OBJECT}
    if stamp is not None:
        item["source_observed_at"] = stamp
    return item


_ACTED = "2026-11-13T12:00:10.400000+00:00"
_RESULT = {"actuation": {"performed": True, "acted_at": _ACTED, "settle_s": 20, "poll_s": 5}}
_PROP = {"app": "fleet", "condition": "Ready", "object": OBJECT}


def _verify(obs, result=_RESULT, **kw):
    kw.setdefault("sleep", lambda _s: None)
    return dispatch.verify_postcondition(
        {"fleet": obs}, _PROP, None, "x", set(), result=result, **kw
    )


def test_postcondition_waits_for_evidence_newer_than_the_action():
    obs = _Observer(
        [
            _row("True", "2026-11-13T12:00:00Z"),  # healthy, but written BEFORE the action
            _row("True", "2026-11-13T12:00:00Z"),
            _row("True", "2026-11-13T12:00:20Z"),  # the node re-observed after the restart
        ]
    )
    sleeps = []
    ok, reason = _verify(obs, sleep=sleeps.append)
    assert ok is True, reason
    assert sleeps == [5, 5]


def test_postcondition_never_accepts_pre_action_evidence():
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    sleeps = []
    ok, reason = _verify(obs, sleep=sleeps.append)
    assert ok is False
    assert "no evidence newer than the action" in reason
    # Bounded: settle 20 s polled every 5 s is 5 reads and 4 sleeps, never more.
    assert obs.calls == 5
    assert sleeps == [5, 5, 5, 5]


@pytest.mark.parametrize(
    ("stamp", "fresh"),
    [
        (None, False),  # a missing stamp is never fresh
        ("not a time", False),
        ("2026-11-13T12:00:10Z", False),  # same whole second as the action: may predate it
        ("2026-11-13T12:00:11Z", True),
        ("2026-11-13T12:00:10.300000+00:00", False),  # sub-second, before the action
        ("2026-11-13T12:00:10.400000+00:00", False),  # equal is not strictly after
        ("2026-11-13T12:00:10.500000+00:00", True),
    ],
)
def test_postcondition_needs_evidence_strictly_after_the_action(stamp, fresh):
    ok, _ = _verify(_Observer([_row("True", stamp)]))
    assert ok is fresh


def test_postcondition_fails_on_fresh_evidence_that_is_still_firing():
    ok, reason = _verify(_Observer([_row("False", "2026-11-13T12:00:30Z")]))
    assert ok is False
    assert "still firing" in reason


def test_postcondition_settle_window_is_capped():
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    huge = {"actuation": {**_RESULT["actuation"], "settle_s": 10**9, "poll_s": 1}}
    ok, _ = _verify(obs, result=huge)
    assert ok is False
    assert obs.calls <= dispatch.MAX_POSTCONDITION_SETTLE_S + 1


def test_postcondition_settle_stops_at_the_pass_deadline():
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    clock = {"t": 100.0}

    def sleep(seconds):
        clock["t"] += seconds

    ok, reason = _verify(obs, sleep=sleep, deadline=112.0, monotonic=lambda: clock["t"])
    assert ok is False
    assert "pass deadline" in reason
    assert obs.calls == 3  # reads at t=100, 105, 110; sleeping to 115 would pass 112
    assert clock["t"] == 110.0


def test_postcondition_without_an_action_time_is_unchanged():
    # An actuation that does not say when it acted (skchat's live probes) keeps the
    # single immediate re-read.
    obs = _Observer([_row("True", "2026-11-13T12:00:00Z")])
    ok, _ = dispatch.verify_postcondition({"fleet": obs}, _PROP, None, "x", set())
    assert ok is True
    assert obs.calls == 1


def test_fleet_actuation_reports_when_it_acted(paths):
    _service(paths)
    before = datetime.now(timezone.utc)
    out = _apply(paths, [])({"action": "restart_service", "object": OBJECT, "ts": "t"}, _STD)
    acted = datetime.fromisoformat(out["actuation"]["acted_at"])
    assert before <= acted <= datetime.now(timezone.utc)
    assert 0 < out["actuation"]["settle_s"] <= dispatch.MAX_POSTCONDITION_SETTLE_S


# ---------------------------------------------------------------------------
# The inline loop lane (no ITIL) agrees with the dispatch lane
# ---------------------------------------------------------------------------


class _SpyState:
    def __init__(self):
        self.recorded = []

    def eligibility(self, _fp, _now):
        return True, ""

    def record(self, *args, **kwargs):
        self.recorded.append((args, kwargs))


def _ratify_demo(paths):
    human = store.Writer(role="operator", node="node-158", identity="chef")
    store.write_spec(
        paths,
        "operatorapp",
        "demo",
        {
            "name": "demo",
            "conditions": ["Ready"],
            "proposedStandardActions": ["restart"],
            "ratifiedStandardActions": ["restart"],
        },
        writer=human,
    )


def _inline(paths, tmp_path, monkeypatch, observer, apply_fn, **kwargs):
    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observer})
    return loop.run_once(
        paths,
        now_iso="2026-08-20T00:00:00Z",
        clock=_clock_for("2026-08-20T00:00:00Z"),
        propose=lambda _b, _r: [
            {
                "app": "demo",
                "condition": "Ready",
                "action": "restart",
                "object": "svc",
                "evidence": _EV_0820,
                "rollback": {"action": "restart"},
            }
        ],
        explain={"actions": [{"name": "restart", "standard": True, "reversible": True}]},
        apply_fn=apply_fn,
        execute=True,
        require_verified_actions=True,
        lifecycle_ledger=action_ledger.ActionLedger(tmp_path / "ledger"),
        emit=lambda _s: None,
        **kwargs,
    )


def test_inline_lane_judges_the_postcondition_on_the_actuation_time(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision_loop(paths)
    _ratify_demo(paths)
    reads = {"n": 0}

    def observer(_p, _n):
        reads["n"] += 1
        status = "False" if reads["n"] == 1 else "True"
        return {
            "conditions": [
                {
                    "type": "Ready",
                    "status": status,
                    "object": "svc",
                    "source_observed_at": "2026-08-19T23:59:30Z",
                }
            ]
        }

    result = {"performed": True, "acted_at": "2026-08-20T00:00:05.000000+00:00", "settle_s": 0}
    res = _inline(paths, tmp_path, monkeypatch, observer, lambda _p, _c: result)
    # Healthy, but only on evidence written before the action: not verified.
    assert res["outcomes"][0]["outcome"].startswith("failed:"), res["outcomes"][0]
    assert "no evidence newer than the action" in res["outcomes"][0]["outcome"]


def test_inline_lane_refusal_is_not_an_attempt(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    _provision_loop(paths)
    _ratify_demo(paths)

    def observer(_p, _n):
        return {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False",
                    "object": "svc",
                    "source_observed_at": "2026-08-19T23:59:30Z",
                }
            ]
        }

    def refuse(_p, _c):
        raise actuator.NotAttemptedError("service 'svc' is placed on 'elsewhere'")

    state = loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)
    rollbacks = []
    res = _inline(
        paths,
        tmp_path,
        monkeypatch,
        observer,
        refuse,
        execution_state=state,
        rollback_fn=lambda p, c, r: rollbacks.append(p) or {"performed": True},
    )
    assert "placed on" in res["outcomes"][0]["outcome"]
    assert state._load()["actions"] == {}, "a refusal never feeds the circuit breaker"
    assert rollbacks == [], "nothing ran, so nothing is rolled back"


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
    return intent.intent_id, change_id


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
    intent_id, change_id = _intent(ledger, chg.id, stamp)
    return itil, ledger, intent_id, change_id


def _dispatch(paths, itil, ledger, intent_id, calls, **kw):
    return dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"fleet": fleet_adapter.fleet_observe},
        problem_types=set(fleet_adapter.PROBLEM_WHEN_TRUE),
        apply_fn=act_dispatch.build_apply_fn(
            paths,
            _iso(datetime.now(timezone.utc)),
            runner=_runner(calls),
            itil=itil,
            verifier=_verify_sig,
        ),
        now_iso=_iso(datetime.now(timezone.utc)),
        **kw,
    )


def test_e2e_fleet_restart_verifies_once_the_node_reports_after_the_action(tmp_path, paths):
    itil, ledger, intent_id, _ = _world(tmp_path, paths)
    calls = []
    sleeps = []

    def sleep(seconds):
        # The node's sknoded converge pass notices the restarted unit (new pid) on its
        # second poll and writes a fresh status against the current spec generation, in
        # a later second than the restart (status files are stamped to the second).
        sleeps.append(seconds)
        if len(sleeps) == 2:
            time.sleep(1.05)
            _status(paths, ready=True, pid=200)

    outcome = _dispatch(
        paths,
        itil,
        ledger,
        intent_id,
        calls,
        postcondition_sleep=sleep,
        target_known=lambda p: fleet_adapter.fleet_target_known(paths, p, verifier=_verify_sig),
    )
    assert outcome.outcome == "verified", outcome.outcome
    assert calls == [["systemctl", "--user", "restart", UNIT]]
    assert len(sleeps) == 2
    lineage = [e.state.value for e in ledger.events(intent_id)]
    assert lineage[-3:] == ["authorized", "executing", "verified"]


def test_e2e_fleet_restart_fails_when_no_fresh_status_arrives(tmp_path, paths):
    itil, ledger, intent_id, _ = _world(tmp_path, paths)
    calls = []
    sleeps = []
    outcome = _dispatch(paths, itil, ledger, intent_id, calls, postcondition_sleep=sleeps.append)
    assert outcome.outcome.startswith("failed:"), outcome.outcome
    assert calls == [["systemctl", "--user", "restart", UNIT]]
    assert sleeps, "the postcondition must poll before giving up"
    assert "failed" in [e.state.value for e in ledger.events(intent_id)]


def test_e2e_remote_placement_is_refused_in_preflight_and_spends_nothing(tmp_path, paths):
    itil, ledger, intent_id, change_id = _world(tmp_path, paths, node=REMOTE)
    calls = []
    outcome = _dispatch(
        paths,
        itil,
        ledger,
        intent_id,
        calls,
        execution_state=_SpyState(),
        postcondition_sleep=lambda _s: None,
        target_known=lambda p: fleet_adapter.fleet_target_known(paths, p, verifier=_verify_sig),
    )
    assert calls == [], "an object placed on another node must not be restarted here"
    assert outcome.outcome.startswith("refused:"), outcome.outcome
    states = [e.state.value for e in ledger.events(intent_id)]
    assert "authorized" not in states and "executing" not in states
    change = next(c for c in itil.list_changes() if c.id == change_id)
    assert change.status.value == "approved", "the approved change is not spent"


def test_e2e_remote_placement_without_preflight_still_never_actuates(tmp_path, paths):
    # Defence in depth: a caller that wires no target_known still refuses in apply_fn,
    # as a preflight refusal that never feeds the breaker.
    itil, ledger, intent_id, _ = _world(tmp_path, paths, node=REMOTE)
    calls = []
    state = _SpyState()
    outcome = _dispatch(
        paths,
        itil,
        ledger,
        intent_id,
        calls,
        execution_state=state,
        postcondition_sleep=lambda _s: None,
    )
    assert calls == []
    assert outcome.outcome.startswith("escalated:"), outcome.outcome
    assert f"placed on {REMOTE!r}" in outcome.outcome
    failed = next(e for e in ledger.events(intent_id) if e.state.value == "failed")
    assert failed.detail.get("preflight_refusal") is True
    assert state.recorded == [], "a refusal before actuation never feeds the circuit breaker"
