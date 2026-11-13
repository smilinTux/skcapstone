"""Tests for operator_seat/dispatch.py (coord card cf12b21d / SKW-AUTONOMY-E3).

The dispatcher is the only code on the estate that turns an approval into an
actuation (AUTONOMY_ARCHITECTURE.md section 3.2). These tests exercise it
directly, against a real (throwaway) ActionLedger and a real ITILManager, so
the CAB fold's own approval semantics decide whether a dispatch is honored --
never a stub standing in for that judgment.
"""

from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.cab_grants_testing import cast_signed_vote, envelope
from skcapstone.fleet import store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import action_ledger, dispatch, safety

_NOW = "2026-08-25T00:00:00Z"
#: The injected REAL clock the dispatcher measures evidence age against.
_CLOCK = datetime(2026, 8, 25, 0, 0, 30, tzinfo=timezone.utc)
_EVIDENCE = {"source": "obs:widget", "observed_at": "2026-08-25T00:00:00Z"}


@pytest.fixture(autouse=True)
def _votes_decided_before_the_test_clock(monkeypatch):
    """CAB votes record the REAL time they were cast. These tests run on a fixed clock in
    the past, and an approval dated after the clock is (rightly) refused as future-dated,
    so each vote is re-dated to just before the clock under test."""
    import json as _json

    original = ITILManager.submit_cab_vote

    def patched(self, change_id, *args, **kwargs):
        vote = original(self, change_id, *args, **kwargs)
        path = self.cab_dir / f"{change_id}-{vote.agent}.json"
        data = _json.loads(path.read_text())
        data["decided_at"] = "2026-08-24T23:00:00+00:00"
        path.write_text(_json.dumps(data))
        return vote

    monkeypatch.setattr(ITILManager, "submit_cab_vote", patched)


def _paths(tmp_path) -> FleetPaths:
    return FleetPaths(root=tmp_path / "fleet")


def _human_writer() -> store.Writer:
    return store.Writer(role="operator", node="cli", identity="capauth:test@scratch")


def _provision(paths: FleetPaths) -> None:
    store.set_frozen(paths, False, writer=_human_writer(), reason="test provisioning")


def _ledger(tmp_path) -> action_ledger.ActionLedger:
    return action_ledger.ActionLedger(tmp_path / "ledger")


def _make_proposed_intent(
    ledger: action_ledger.ActionLedger,
    *,
    application: str = "widget",
    action: str = "fix-it",
    target_id: str = "widget-1",
    condition_type: str | None = "Ready",
    catalog_generation: str = "gen-0",
    itil_change_id: str | None,
    rollback: dict | None = None,
    evidence: dict | None = _EVIDENCE,
    lane: str = "auto",
) -> str:
    """Build a real, PROPOSED ledger intent (OBSERVED -> DIAGNOSED -> PROPOSED),
    bound to itil_change_id at BIRTH the same way loop.py now does it -- the
    field is frozen identity and can never be rewritten after creation."""
    created_at = datetime(2026, 8, 25, tzinfo=timezone.utc)
    intent = action_ledger.ActionIntent(
        condition_fingerprint=f"fp-{application}-{action}-{target_id}",
        application=application,
        target_kind="CI",
        target_id=target_id,
        action=action,
        catalog_generation=catalog_generation,
        created_at=created_at,
        itil_change_id=itil_change_id,
        rollback=rollback or {},
        condition_type=condition_type,
    )
    ledger.create(intent, actor="test")
    ledger.append(
        intent.intent_id, action_ledger.ActionState.DIAGNOSED, occurred_at=created_at, actor="test"
    )
    ledger.append(
        intent.intent_id,
        action_ledger.ActionState.PROPOSED,
        occurred_at=created_at,
        actor="test",
        detail={"disposition": lane, **({"evidence": evidence} if evidence else {})},
    )
    return intent.intent_id


def _propose(itil, *, bind=("widget", "fix-it", "widget-1"), **kwargs):
    """Draft a change bound to the intent the way the loop does (app, action, target)."""
    tags = list(kwargs.pop("tags", [])) + [f"op-bind:{bind[0]}|{bind[1]}|{bind[2]}"]
    return itil.propose_change(tags=tags, **kwargs)


class _FakeObserver:
    """A single-app observer whose reported condition flips when `heal()` is
    called -- the "live observation needed for postcondition verification"
    input the dispatcher is explicitly allowed to read."""

    def __init__(self, condition_type: str, object_id: str):
        self.condition_type = condition_type
        self.object_id = object_id
        self.healthy = False

    def heal(self) -> None:
        self.healthy = True

    def __call__(self, paths, now_iso) -> dict:
        status = "False" if self.healthy else "True"
        return {
            "source_observed_at": _EVIDENCE["observed_at"],
            "conditions": [
                {"type": self.condition_type, "status": status, "object": self.object_id}
            ],
        }


def _apply_fn_that_heals(observer: _FakeObserver):
    def apply_fn(prop, classification):
        observer.heal()
        return {"performed": True}

    return apply_fn


def _apply_fn_that_fails(reason="simulated actuation failure"):
    def apply_fn(prop, classification):
        return {"performed": False, "reason": reason}

    return apply_fn


# ---------------------------------------------------------------------------
# Full arc: PROPOSED -> (CAB approve) -> AUTHORIZED -> EXECUTING -> VERIFIED
# ---------------------------------------------------------------------------


def test_dispatch_intent_actuates_and_verifies_an_approved_change(tmp_path):
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    observer = _FakeObserver("Ready", "widget-1")

    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    # A simulated human CAB-approves via a provenance-bound vote (never a bare
    # "human" string), exactly what dispatch.resolve_decision submits.
    cast_signed_vote(itil, chg.id, subject="capauth:approver@scratch", role="approver")

    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=_apply_fn_that_heals(observer),
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )

    assert outcome.outcome == "verified"
    events = ledger.events(intent_id)
    lineage = [e.state.value for e in events]
    assert lineage == [
        "observed",
        "diagnosed",
        "proposed",
        "authorized",
        "executing",
        "verified",
    ]
    authorized_event = events[lineage.index("authorized")]
    assert authorized_event.detail["itil_change_id"] == chg.id
    assert authorized_event.detail["approval_provenance"]["change_status"] == "approved"


# ---------------------------------------------------------------------------
# Negative: an unapproved change never actuates
# ---------------------------------------------------------------------------


def test_unapproved_change_never_actuates(tmp_path):
    """Negative test: fails against pre-change behaviour, where nothing ever
    re-read a resolved (or unresolved) decision at all -- loop.py never
    looked at one, so there was no gate here to fail. Now: a change with no
    qualifying CAB approval is left PROPOSED, never dispatched."""
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    observer = _FakeObserver("Ready", "widget-1")

    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    # No CAB vote submitted: the change stays "proposed" at fold time.

    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called for an unapproved change")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )

    assert outcome.outcome.startswith("pending:")
    assert ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert observer.healthy is False


def test_rejected_change_never_actuates(tmp_path):
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    observer = _FakeObserver("Ready", "widget-1")

    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    cast_signed_vote(
        itil, chg.id, subject="capauth:approver@scratch", role="approver", decision="rejected"
    )
    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called for a rejected change")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    assert outcome.outcome == "pending: ITIL change status=rejected"
    assert ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED


# ---------------------------------------------------------------------------
# Negative: a decisions-store-only approval with no linked change
# ---------------------------------------------------------------------------


def test_intent_with_no_linked_change_is_invisible_to_the_dispatcher(tmp_path):
    """A ledger intent with no itil_change_id is invisible to run_dispatch_pass
    forever, by design (AUTONOMY_ARCHITECTURE.md section 3.2 step 2)."""
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    observer = _FakeObserver("Ready", "widget-1")

    intent_id = _make_proposed_intent(ledger, itil_change_id=None)

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called")

    outcomes = dispatch.run_dispatch_pass(
        paths,
        ledger,
        itil,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    assert outcomes == []
    assert ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED

    # dispatch_intent called directly also refuses, rather than crashing.
    direct = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    assert direct.outcome == "skipped: no linked ITIL change"


def test_resolve_decision_with_no_linked_change_refuses_before_any_write(tmp_path):
    from skcapstone.operator_seat import decisions

    itil = ITILManager(tmp_path / "itil")
    ddir = str(tmp_path / "decisions")
    decisions.park(
        ddir,
        [{"action": "restart_service", "object": "x"}],  # no itil_change_id
        decision_id="d1",
        created_iso=_NOW,
    )
    with pytest.raises(dispatch.UnauthorizedDecisionError):
        dispatch.resolve_decision(
            ddir,
            itil,
            "d1",
            approve=True,
            choice=0,
            subject="capauth:approver@scratch",
            resolved_iso=_NOW,
            authorization=envelope("chg-none", issuer="capauth:approver@scratch"),
        )
    # Never actuates: the decision is left pending, not silently resolved.
    assert len(decisions.list_pending(ddir)) == 1
    assert itil.list_changes() == []  # no CAB vote / change ever touched


# ---------------------------------------------------------------------------
# Negative: a stale catalog_generation refuses and escalates
# ---------------------------------------------------------------------------


def test_stale_catalog_generation_refuses_and_escalates(tmp_path):
    """Negative test: fails against pre-change behaviour (no dispatcher
    existed to re-check anything). The intent was proposed against
    catalog_generation "1"; the OperatorApp record has since moved to "2"
    (a human ratified/reratified the app's standard-action catalog). The
    dispatcher must refuse to honor the stale approval and escalate rather
    than actuate."""
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    observer = _FakeObserver("Ready", "widget-1")
    human = _human_writer()

    # generation 1: nothing ratified yet.
    store.write_spec(paths, "operatorapp", "widget", {"ratifiedStandardActions": []}, writer=human)
    # generation 2: still nothing ratified (bumping generation is enough to
    # prove staleness; a human re-touching the app's catalog record at all
    # is the observable event this test is standing in for).
    store.write_spec(paths, "operatorapp", "widget", {"ratifiedStandardActions": []}, writer=human)
    record = store.read_spec(paths, "operatorapp", "widget")
    assert str(record["generation"]) == "2"

    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    cast_signed_vote(itil, chg.id, subject="capauth:approver@scratch", role="approver")
    # Proposed against generation "1" (now stale: the live record is "2").
    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id, catalog_generation="1")

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called for a stale approval")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": observer},
        problem_types={"Ready"},
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )

    # A stale catalog REFUSES and leaves the intent PROPOSED: no AUTHORIZED is written,
    # because nothing was authorized (round 4, finding 4).
    assert outcome.outcome.startswith("refused:")
    assert "stale catalog_generation" in outcome.outcome
    assert ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert "authorized" not in [e.state.value for e in ledger.events(intent_id)]
    assert observer.healthy is False


def test_hardened_classification_refuses_and_escalates(tmp_path, monkeypatch):
    """A standard change whose action has since fallen out of the RATIFIED
    catalog (hardened classification, e.g. a code deploy narrowed
    ``policy.RATIFIED_STANDARD_CATALOG`` between proposal and dispatch) must
    not be honored even though the ITIL fold already auto-approved it as a
    standard change at proposal time -- the fold's status is trusted for
    APPROVAL, but the dispatcher independently re-checks eligibility against
    the CURRENT catalog before ever calling apply_fn.

    Uses the ``fleet``-style case (no live per-app OperatorApp record, so
    ``catalog_generation`` staleness cannot be evaluated at all) to isolate
    this from the generation-mismatch path -- see
    ``test_stale_catalog_generation_refuses_and_escalates`` for that one."""
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    observer = _FakeObserver("Ready", "widget-1")

    chg = _propose(
        itil,
        bind=("fleet", "restart_service", "widget-1"),
        title="restart widget-1",
        change_type="standard",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    # No CAB vote needed: a standard change auto-approves at the fold.
    # "restart_service" is in the real policy.RATIFIED_STANDARD_CATALOG.
    intent_id = _make_proposed_intent(
        ledger, itil_change_id=chg.id, action="restart_service", application="fleet"
    )

    # A code deploy narrows the ratified catalog between proposal and dispatch.
    monkeypatch.setattr(dispatch.policy, "RATIFIED_STANDARD_CATALOG", frozenset())

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called for a hardened classification")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"fleet": observer},
        problem_types={"Ready"},
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    assert outcome.outcome.startswith("refused:")
    assert "classification hardened" in outcome.outcome
    assert ledger.current_state(intent_id) is action_ledger.ActionState.PROPOSED
    assert "authorized" not in [e.state.value for e in ledger.events(intent_id)]


# ---------------------------------------------------------------------------
# Freeze / readiness gate wins first
# ---------------------------------------------------------------------------


def test_dispatch_refuses_when_frozen(tmp_path):
    paths = _paths(tmp_path)
    _provision(paths)
    store.set_frozen(paths, True, writer=_human_writer(), reason="test freeze")
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)

    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="standard",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called while frozen")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={},
        problem_types=set(),
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    assert outcome.outcome == f"refused: {store.REASON_FROZEN}"


def test_dispatch_refuses_when_unprovisioned(tmp_path):
    paths = _paths(tmp_path)  # never provisioned
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)

    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="standard",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    def boom(prop, classification):
        raise AssertionError("apply_fn must never be called while unprovisioned")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={},
        problem_types=set(),
        apply_fn=boom,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    assert outcome.outcome == f"refused: {store.REASON_UNPROVISIONED}"


# ---------------------------------------------------------------------------
# ActionLedger.list_intents
# ---------------------------------------------------------------------------


def test_list_intents_filters_by_folded_state(tmp_path):
    ledger = _ledger(tmp_path)
    proposed_id = _make_proposed_intent(ledger, itil_change_id="chg-a", target_id="w-1")
    other_id = _make_proposed_intent(ledger, itil_change_id="chg-b", target_id="w-2")
    ledger.append(
        other_id,
        action_ledger.ActionState.AUTHORIZED,
        occurred_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
        actor="test",
    )

    all_ids = ledger.list_intents()
    assert set(all_ids) == {proposed_id, other_id}
    proposed_only = ledger.list_intents(state=action_ledger.ActionState.PROPOSED)
    assert proposed_only == [proposed_id]
    authorized_only = ledger.list_intents(state=action_ledger.ActionState.AUTHORIZED)
    assert authorized_only == [other_id]


# ---------------------------------------------------------------------------
# Import boundary: dispatch.py must never import the brain or any propose path
# ---------------------------------------------------------------------------

_DENYLISTED_MODULES = {"brain", "proposer", "brief"}


def test_dispatch_module_never_imports_the_brain_or_a_propose_path():
    """The actuator's input set is closed and this is load bearing
    (AUTONOMY_ARCHITECTURE.md section 3.2): dispatch.py may read the ledger,
    the ITIL fold, freeze/readiness state, and the ratified catalog, but must
    NEVER read proposals, briefs, or brain/model output. This parses the
    ACTUAL source of dispatch.py (not merely a convention) and fails if any
    import statement anywhere in the file, top-level or nested, names one of
    the denylisted propose-path modules."""
    source_path = Path(dispatch.__file__)
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    offenders: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                leaf = alias.name.rsplit(".", 1)[-1]
                if leaf in _DENYLISTED_MODULES:
                    offenders.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            leaf = module.rsplit(".", 1)[-1]
            if leaf in _DENYLISTED_MODULES:
                offenders.append(module)
            for alias in node.names:
                if alias.name in _DENYLISTED_MODULES:
                    offenders.append(f"{module}.{alias.name}")

    assert offenders == [], f"dispatch.py must never import a propose-path module: {offenders}"


def test_freeze_landing_during_dispatch_does_not_feed_the_circuit_breaker(tmp_path):
    """A kill switch that lands while the actuator runs is not a fault of the action."""
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    state = safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)
    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    cast_signed_vote(itil, chg.id, subject="capauth:approver@scratch", role="approver")
    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    def freeze_then_refuse(prop, classification):
        store.set_frozen(paths, True, writer=_human_writer(), reason="lands inside the actuator")
        raise RuntimeError("fleet is frozen: the operator does not actuate")

    outcome = dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": _FakeObserver("Ready", "widget-1")},
        problem_types={"Ready"},
        apply_fn=freeze_then_refuse,
        execution_state=state,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )

    assert outcome.outcome.startswith("failed:")
    assert state._load()["actions"] == {}


def test_a_real_failure_that_coincides_with_a_freeze_still_feeds_the_circuit_breaker(tmp_path):
    paths = _paths(tmp_path)
    _provision(paths)
    itil = ITILManager(tmp_path / "itil")
    ledger = _ledger(tmp_path)
    state = safety.ExecutionState(tmp_path / "state", cooldown_seconds=0)
    chg = _propose(
        itil,
        title="fix widget-1",
        change_type="normal",
        risk="low",
        rollback_plan="revert",
        created_by="atlas",
        tags=["operator"],
    )
    cast_signed_vote(itil, chg.id, subject="capauth:approver@scratch", role="approver")
    intent_id = _make_proposed_intent(ledger, itil_change_id=chg.id)

    def freeze_then_fail_for_real(prop, classification):
        store.set_frozen(paths, True, writer=_human_writer(), reason="coincides with a real fault")
        raise RuntimeError("disk exploded")

    dispatch.dispatch_intent(
        paths,
        ledger,
        itil,
        intent_id,
        adapters={"widget": _FakeObserver("Ready", "widget-1")},
        problem_types={"Ready"},
        apply_fn=freeze_then_fail_for_real,
        execution_state=state,
        now_iso=_NOW,
        clock=lambda: _CLOCK,
    )
    entries = list(state._load()["actions"].values())
    assert [e["consecutive_failures"] for e in entries] == [1]
