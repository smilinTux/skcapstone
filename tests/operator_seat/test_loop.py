"""Report-only operator loop: it observes and reports, and never writes."""

from __future__ import annotations

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.operator_seat import action_ledger, loop


def _enroll(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None},
    )
    paths = FleetPaths(root=tmp_path / "fleet")
    op = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=op)
    sknoded.run_once(paths, "node-158")
    return paths, op


def test_loop_reports_and_returns_a_brief(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    out = []
    res = loop.run_once(paths, now_iso="2026-07-29T00:00:00Z", emit=out.append)
    assert res["frozen"] is False
    assert res["brief"] is not None
    assert res["route"] in ("ornith", "claude")
    assert res["report"] == out[0]


def test_loop_stands_down_when_frozen(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    human = store.Writer(role="operator", node="node-158", identity="chef")
    store.set_frozen(paths, True, writer=human, reason="drill")
    out = []
    res = loop.run_once(paths, now_iso="2026-07-29T00:00:00Z", emit=out.append)
    assert res["frozen"] is True
    assert res["proposals"] == []
    assert "standing down" in out[0]


def test_loop_writes_nothing_to_the_fleet(tmp_path, monkeypatch):
    # Report-only guarantee: a pass must not create or change any fleet file.
    paths, _ = _enroll(tmp_path, monkeypatch)

    def _snapshot():
        return {p: p.read_bytes() for p in (tmp_path / "fleet").rglob("*.json")}

    before = _snapshot()
    loop.run_once(paths, now_iso="2026-07-29T00:00:00Z", emit=lambda _s: None)
    assert _snapshot() == before  # no file created or modified


def test_loop_carries_injected_proposals_into_the_report(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    proposal = {
        "change_class": "normal",
        "action": "restart_service",
        "rationale": "service Ready went False",
    }
    out = []
    res = loop.run_once(
        paths, now_iso="2026-07-29T00:00:00Z", propose=lambda b, r: [proposal], emit=out.append
    )
    assert res["proposals"] == [proposal]
    assert "restart_service" in out[0]


def test_loop_auto_proposal_not_applied_when_execution_off(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    applied = []
    prop = {"action": "restart_service", "object": "web"}
    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [prop],
        apply_fn=lambda p, c: applied.append(p),
        execute=False,
        emit=lambda _s: None,
    )
    assert applied == []  # execution off: nothing is applied
    assert res["outcomes"][0]["outcome"].startswith("auto-ready")


def test_loop_auto_proposal_applied_when_execution_on(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    applied = []
    prop = {"action": "restart_service", "object": "web"}
    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [prop],
        apply_fn=lambda p, c: applied.append(p),
        execute=True,
        emit=lambda _s: None,
    )
    assert applied == [prop]
    assert res["outcomes"][0]["outcome"] == "applied"


def test_loop_escalation_parks_for_approval(tmp_path, monkeypatch):
    from skcapstone.operator_seat import decisions

    paths, _ = _enroll(tmp_path, monkeypatch)
    ddir = str(tmp_path / "decisions")
    prop = {"action": "delete_object", "object": "web"}  # irreversible -> escalate
    loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [prop],
        decisions_dir=ddir,
        execute=True,
        apply_fn=lambda p, c: None,
        emit=lambda _s: None,
    )
    assert len(decisions.list_pending(ddir)) == 1  # parked, not applied


def test_loop_merges_extra_observers_builtins_win(tmp_path, monkeypatch):
    # A manifest-discovered observer widens what Atlas observes; a built-in id
    # always wins a clash (fleet stays the real fleet adapter).
    paths, _ = _enroll(tmp_path, monkeypatch)
    seen = {}

    def _skbrain_observe(paths_, now_iso_):
        return {"conditions": [{"type": "OpsSchemaPresent", "status": "Unknown"}]}

    def _fake_fleet(paths_, now_iso_):
        seen["fleet_called"] = True
        return {"conditions": []}

    res = loop.run_once(
        paths,
        now_iso="2026-07-31T00:00:00Z",
        extra_observers={"skbrain": _skbrain_observe, "fleet": _fake_fleet},
        emit=lambda _s: None,
    )
    # The discovered observer was included; the built-in fleet observer overrode
    # the same-named extra (the fake fleet was never called).
    assert res["brief"] is not None
    assert "fleet_called" not in seen


def test_loop_extra_observers_none_is_byte_identical(tmp_path, monkeypatch):
    # Default (no extra observers) reasons exactly as before: report-only, no write.
    paths, _ = _enroll(tmp_path, monkeypatch)

    def _snapshot():
        return {p: p.read_bytes() for p in (tmp_path / "fleet").rglob("*.json")}

    before = _snapshot()
    loop.run_once(
        paths, now_iso="2026-07-31T00:00:00Z", extra_observers=None, emit=lambda _s: None
    )
    assert _snapshot() == before


def test_loop_frozen_never_applies_even_with_execute(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    human = store.Writer(role="operator", node="node-158", identity="chef")
    store.set_frozen(paths, True, writer=human)
    applied = []
    loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [{"action": "restart_service", "object": "x"}],
        apply_fn=lambda p, c: applied.append(p),
        execute=True,
        emit=lambda _s: None,
    )
    assert applied == []  # freeze wins even with execute=True


def test_loop_failing_apply_does_not_abort_the_pass(tmp_path, monkeypatch):
    # Blast radius: one bad proposal must not kill the proposals behind it.
    paths, _ = _enroll(tmp_path, monkeypatch)
    bad = {"action": "restart_service", "object": "nonexistent"}
    good = {"action": "restart_service", "object": "web"}
    applied = []

    def apply_fn(p, c):
        if p["object"] == "nonexistent":
            raise ValueError("unknown service object 'nonexistent'")
        applied.append(p)

    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [bad, good],
        apply_fn=apply_fn,
        execute=True,
        emit=lambda _s: None,
    )
    assert applied == [good]  # the proposal behind the failure still ran
    assert res["outcomes"][0]["outcome"].startswith("failed")
    assert "unknown service object" in res["outcomes"][0]["outcome"]
    assert res["outcomes"][1]["outcome"] == "applied"


def test_loop_failing_apply_still_parks_later_escalations(tmp_path, monkeypatch):
    # The silent-loss bug: escalations queued behind a failure were never parked,
    # so a human was never asked about them.
    from skcapstone.operator_seat import decisions

    paths, _ = _enroll(tmp_path, monkeypatch)
    ddir = tmp_path / "decisions"
    bad = {"action": "restart_service", "object": "nonexistent"}
    escalation = {"action": "delete_object", "object": "web"}  # irreversible -> escalate

    def apply_fn(p, c):
        raise ValueError("unknown service object 'nonexistent'")

    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [bad, escalation],
        apply_fn=apply_fn,
        execute=True,
        decisions_dir=str(ddir),
        emit=lambda _s: None,
    )
    assert res["outcomes"][1]["outcome"].startswith("escalated")
    assert decisions.list_pending(str(ddir))  # the human actually got asked


def test_loop_failing_apply_still_emits_a_report(tmp_path, monkeypatch):
    # A pass that hits a failure must still tell someone what happened.
    paths, _ = _enroll(tmp_path, monkeypatch)
    out = []
    loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [{"action": "restart_service", "object": "nonexistent"}],
        apply_fn=lambda p, c: (_ for _ in ()).throw(ValueError("boom")),
        execute=True,
        emit=out.append,
    )
    assert out and "restart_service" in out[0]


def test_loop_unresolvable_target_parks_instead_of_applying(tmp_path, monkeypatch):
    # The skoperator incident shape: the proposer named an object that does not
    # exist. Rather than hand it to the act verb and fail at actuation, it must
    # park for a human.
    from skcapstone.operator_seat import decisions

    paths, _ = _enroll(tmp_path, monkeypatch)
    ddir = tmp_path / "decisions"
    applied = []
    res = loop.run_once(
        paths,
        now_iso="2026-07-29T00:00:00Z",
        propose=lambda b, r: [{"action": "restart_service", "object": "ghost"}],
        apply_fn=lambda p, c: applied.append(p),
        execute=True,
        decisions_dir=str(ddir),
        target_known=lambda p: False,
        emit=lambda _s: None,
    )
    assert applied == []  # never handed to the act verb
    assert res["outcomes"][0]["outcome"].startswith("escalated")
    assert decisions.list_pending(str(ddir))


def test_verified_execution_requires_ratified_app_condition(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    applied = []
    prop = {
        "app": "skchat",
        "condition": "DaemonReady",
        "action": "restart-daemon",
        "object": "daemon",
    }
    res = loop.run_once(
        paths,
        now_iso="2026-08-20T00:00:00Z",
        propose=lambda b, r: [prop],
        explain={"actions": [{"name": "restart-daemon", "standard": True, "reversible": True}]},
        apply_fn=lambda p, c: applied.append(p) or {"performed": True},
        execute=True,
        require_verified_actions=True,
        emit=lambda _s: None,
    )
    assert applied == []
    assert res["planned"][0]["binding_denied"] is True


def test_verified_execution_reobserves_and_requires_condition_clear(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
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
    calls = {"observe": 0, "apply": 0}

    def observe(paths_, now_):
        calls["observe"] += 1
        return {
            "conditions": [
                {
                    "type": "Ready",
                    "status": "False" if calls["observe"] == 1 else "True",
                    "object": "svc",
                }
            ]
        }

    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observe})
    prop = {"app": "demo", "condition": "Ready", "action": "restart", "object": "svc"}
    ledger = action_ledger.ActionLedger(tmp_path / "action-ledger")
    res = loop.run_once(
        paths,
        now_iso="2026-08-20T00:00:00Z",
        propose=lambda b, r: [prop],
        explain={"actions": [{"name": "restart", "standard": True, "reversible": True}]},
        apply_fn=lambda p, c: calls.__setitem__("apply", 1) or {"performed": True},
        execute=True,
        require_verified_actions=True,
        execution_state=loop.safety.ExecutionState(tmp_path / "state", cooldown_seconds=0),
        lifecycle_ledger=ledger,
        emit=lambda _s: None,
    )
    assert calls == {"observe": 2, "apply": 1}
    assert res["outcomes"][0]["outcome"] == "verified"
    intent_id = res["outcomes"][0]["intent_id"]
    assert intent_id is not None
    assert ledger.current_state(intent_id) is action_ledger.ActionState.VERIFIED
    assert [event.state.value for event in ledger.events(intent_id)] == [
        "observed",
        "diagnosed",
        "proposed",
        "authorized",
        "executing",
        "verified",
    ]


def test_performed_false_is_a_failure(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
    res = loop.run_once(
        paths,
        now_iso="2026-08-20T00:00:00Z",
        propose=lambda b, r: [{"action": "restart_service", "object": "svc"}],
        apply_fn=lambda p, c: {"performed": False, "reason": "systemd failed"},
        execute=True,
        emit=lambda _s: None,
    )
    assert res["outcomes"][0]["outcome"].startswith("failed:")


def test_failed_verification_executes_typed_rollback(tmp_path, monkeypatch):
    paths, _ = _enroll(tmp_path, monkeypatch)
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
    observer = lambda _p, _n: {  # noqa: E731
        "conditions": [{"type": "Ready", "status": "False", "object": "svc"}]
    }
    monkeypatch.setattr(loop, "ADAPTERS", {"demo": observer})
    ledger = action_ledger.ActionLedger(tmp_path / "ledger")
    rollbacks = []
    result = loop.run_once(
        paths,
        now_iso="2026-08-20T00:00:00Z",
        propose=lambda _b, _r: [
            {
                "app": "demo",
                "condition": "Ready",
                "action": "restart",
                "object": "svc",
                "rollback": {"action": "restart"},
            }
        ],
        explain={"actions": [{"name": "restart", "standard": True, "reversible": True}]},
        apply_fn=lambda _p, _c: {"performed": True},
        rollback_fn=lambda p, c, r: rollbacks.append((p, c, r)) or {"performed": True},
        execute=True,
        require_verified_actions=True,
        lifecycle_ledger=ledger,
        emit=lambda _s: None,
    )

    intent_id = result["outcomes"][0]["intent_id"]
    assert len(rollbacks) == 1
    assert ledger.current_state(intent_id) is action_ledger.ActionState.ROLLED_BACK
