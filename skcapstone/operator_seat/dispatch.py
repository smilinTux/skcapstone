"""The dispatcher: the ONLY code on the estate that turns an approval into an
actuation (coord card cf12b21d / SKW-AUTONOMY-E3, AUTONOMY_ARCHITECTURE.md
section 3.2).

Assigns each existing store the role it already claims for itself
(section 3.1): the ITIL change fold is the AUTHORIZATION object (its approval
authenticates the approver -- CAB votes are provenance-bound, no-self-approval
is enforced inside the fold, and the AI structurally cannot satisfy a
human-approval requirement); the action ledger is EVIDENCE and this module's
QUEUE (an intent in PROPOSED state, bound to an ITIL change, is exactly what
this dispatcher iterates); the decisions store stays the human inbox, a
PROJECTION, whose resolution becomes write-through (``resolve_decision``
below): approving submits a provenance-bound CAB vote on the linked change,
and it is THAT vote, folded by ITIL, that authorizes. A decisions record with
no linked change authorizes nothing.

Per pass (``run_dispatch_pass``, called from ``loop._run_once`` for the fast/
auto lane and standalone by ``skoperator honor-pending`` for the slow/human
lane -- one code path, two speeds):

1. Refuse unless the fleet is actuation-ready and not frozen
   (``store.check_actuation_gate``), freeze first.
2. Enumerate ledger intents currently PROPOSED that carry an ``itil_change_id``.
   Intents with neither are invisible to this dispatcher forever, by design.
3. Independently re-read the ITIL change fold for each. Trust the fold, never
   the proposal's claim about itself, never the decisions store.
4. Re-classify at dispatch time against the CURRENT ratified catalog
   (``policy.classify_change``); a hardened classification or a mismatched
   ``catalog_generation`` refuses and escalates rather than honoring a stale
   approval.
5. Append AUTHORIZED with the change id and the fold's approval provenance,
   then EXECUTING, then route through the caller's ``apply_fn`` (requiring its
   ``performed=True`` proof), then the postcondition re-observation, then
   VERIFIED or FAILED with rollback/escalation.

CLOSED INPUT SET (section 3.2, load bearing): this module may read the ledger
intent core and its event stream, the ITIL change fold, freeze/readiness
state, the ratified action catalog, and the live observation needed for
postcondition verification. It must NEVER import the brain, the proposer, or
any other propose-side module -- see ``tests/operator_seat/test_dispatch.py``
for the import-boundary test that enforces this at import time, not just by
convention.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from ..fleet import store
from ..fleet.paths import FleetPaths
from . import action_ledger, decisions, policy, safety

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_iso(now_iso: str) -> datetime:
    return datetime.fromisoformat(now_iso.replace("Z", "+00:00"))


class UnauthorizedDecisionError(ValueError):
    """Raised when a decision cannot be write-through-approved.

    Exactly the case AUTONOMY_ARCHITECTURE.md section 3.1 names: "a decisions
    record with no linked change authorizes nothing, and the CLI says so
    instead of printing success." Callers (the ``skoperator decide`` CLI) are
    expected to catch this and print the reason, not attempt a fallback
    write.
    """


class _PreflightRefusalError(RuntimeError):
    """A dispatch refused before ever calling ``apply_fn`` (stale/hardened).

    Internal to ``dispatch_intent``: distinguishes "never attempted" from an
    actuation that WAS attempted and failed, so the except handler never
    tries to roll back something that never physically happened.
    """


# ---------------------------------------------------------------------------
# Postcondition re-observation (moved from loop.py: dispatch.py is now the
# canonical owner of AUTHORIZED..VERIFIED/FAILED, and loop.py imports these
# two helpers back rather than keeping a second copy -- see loop.py's own
# inline auto-lane comment for why that path still exists unmodified for
# callers that do not (yet) wire an ITIL manager into ``loop.run_once``).
# ---------------------------------------------------------------------------


def condition_firing(condition: dict, problem_types: set[str]) -> bool:
    """Return whether one observed condition is firing under its polarity."""
    from . import adapter

    status = condition.get("status")
    if status == "Unknown":
        return True
    polarity = condition.get("polarity")
    problem_when_true = (
        polarity == "problem_when_true"
        if polarity in adapter.POLARITIES
        else condition.get("type") in problem_types
    )
    return (problem_when_true and status == "True") or (
        not problem_when_true and status == "False"
    )


def verify_postcondition(
    observers: dict[str, Callable[..., dict]],
    proposal: dict,
    paths: FleetPaths,
    now_iso: str,
    problem_types: set[str],
) -> tuple[bool, str]:
    """Re-observe the owning app and require the bound condition to clear."""
    app = proposal.get("app")
    observer = observers.get(app)
    if observer is None:
        return False, "owning observer unavailable"
    conditions = observer(paths, now_iso).get("conditions", [])
    matches = [
        item
        for item in conditions
        if item.get("type") == proposal.get("condition")
        and (proposal.get("object") is None or item.get("object") == proposal.get("object"))
    ]
    if not matches:
        return False, "bound condition missing after action"
    if any(condition_firing(item, problem_types) for item in matches):
        return False, "bound condition still firing after action"
    return True, "postcondition verified"


# ---------------------------------------------------------------------------
# Decisions write-through: approving a decision submits a provenance-bound
# CAB vote on the linked ITIL change; it is that vote, folded by ITIL, that
# authorizes. by="human" dies here -- every caller supplies its own
# authenticated ``subject`` (capauth.resolve_agent_identity's canonical
# subject; see ``fleet/store.py::resolved_writer_identity``), never a
# caller-typed literal.
# ---------------------------------------------------------------------------


def resolve_decision(
    decisions_dir: str,
    itil: Any,
    decision_id: str,
    *,
    approve: bool,
    choice: int | None,
    subject: str,
    resolved_iso: str,
    subject_role: str = "approver",
) -> dict:
    """Resolve a parked decision, write-through to a CAB vote when approving.

    Rejection needs no ITIL involvement (there is nothing to authorize) and
    resolves the projection directly. Approval REQUIRES the chosen option to
    carry an ``itil_change_id`` (or ``change_id``): that is the only thing
    this dispatcher, or any future one, will ever look at to decide whether
    the corresponding ledger intent may be dispatched. A decision parked
    without one cannot be write-through approved -- raises
    ``UnauthorizedDecisionError`` BEFORE any write, so a caller that fails to
    catch it never silently prints success either.

    Args:
        decisions_dir: The pending-decision store directory.
        itil: An ``ITILManager``-shaped object (``submit_cab_vote``).
        decision_id: The decision to resolve.
        approve: True to approve, False to reject.
        choice: Option index for a multi-option decision.
        subject: The caller's authenticated identity (never a magic string
            like ``"human"``): recorded as the CAB vote's authenticated
            voter and as the decision record's ``resolved_by``.
        resolved_iso: ISO timestamp of resolution.
        subject_role: The CAB subject role bound to the vote. Must be one
            ITIL's fold treats as a qualifying human role ("owner" or
            "approver"); the default matches what a human resolving a
            decision through this seat is doing.

    Returns:
        The updated decision record.

    Raises:
        UnauthorizedDecisionError: approving a decision whose chosen option
            has no linked ITIL change.
        ValueError: unknown decision, already resolved, or an out-of-range
            choice (same failures ``decisions.resolve`` itself raises).
    """
    if not approve:
        return decisions.resolve(
            decisions_dir,
            decision_id,
            approve=False,
            choice=choice,
            by=subject,
            resolved_iso=resolved_iso,
        )

    pending = {record["id"]: record for record in decisions.list_pending(decisions_dir)}
    record = pending.get(decision_id)
    if record is None:
        raise ValueError(f"unknown or already-resolved decision: {decision_id}")
    options = record.get("options") or []
    index = choice if (choice is not None and len(options) > 1) else 0
    if not options or not 0 <= index < len(options):
        raise ValueError(f"choice out of range for {len(options)} option(s)")
    change_id = options[index].get("itil_change_id") or options[index].get("change_id")
    if not change_id:
        raise UnauthorizedDecisionError(
            f"decision {decision_id!r} has no ITIL change linked to option {index}; "
            "approving it here would authorize nothing (no store on this estate "
            "can turn a bare decisions-store approval into an actuation) -- "
            "the proposal must be re-parked with a bound change before it can "
            "be approved"
        )
    itil.submit_cab_vote(
        change_id,
        agent=subject,
        decision="approved",
        subject=subject,
        subject_role=subject_role,
    )
    return decisions.resolve(
        decisions_dir,
        decision_id,
        approve=True,
        choice=choice,
        by=subject,
        resolved_iso=resolved_iso,
    )


# ---------------------------------------------------------------------------
# The dispatcher itself
# ---------------------------------------------------------------------------


def _read_change(itil: Any, change_id: str) -> Any | None:
    """Independently re-read one change's fold. Never trusts a cached claim."""
    for change in itil.list_changes():
        if change.id == change_id:
            return change
    return None


def _reconstruct_proposal(intent: action_ledger.ActionIntent) -> dict:
    """Build the minimal actuation payload purely from the frozen intent core.

    This is what keeps the dispatcher's input set closed: no proposal, brief,
    or brain output is ever read here, only fields already durable on the
    intent (AUTONOMY_ARCHITECTURE.md section 3.2's exhaustive input list).
    """
    prop: dict[str, Any] = {
        "app": intent.application,
        "condition": intent.condition_type,
        "object": intent.target_id,
        "action": intent.action,
        "change_id": intent.itil_change_id,
        "itil_change_id": intent.itil_change_id,
    }
    if intent.rollback:
        prop["rollback"] = dict(intent.rollback)
    return prop


def _reclassify(paths: FleetPaths, intent: action_ledger.ActionIntent) -> dict:
    """Re-run policy.classify_change against the CURRENT ratified catalog.

    Two independent things can make an approval stale, and this checks both:

    - ``generation_matches``: did the live, per-app signed OperatorApp
      catalog move since this intent was proposed? Read directly (unsigned):
      staleness here is "did the generation counter move", not "is the
      catalog authentic" -- the same trust boundary ``fleet/store.py``'s own
      freeze-provisioning predicate already accepts for a filesystem-local
      read (closing it fully needs the signing plane, per that function's
      own comment). No live record for this application (e.g. the ``fleet``
      app, whose actions are governed by ``policy.RATIFIED_STANDARD_CATALOG``
      in code, not a live per-app record) means there is nothing to compare
      a generation against, so this axis reads as unchanged -- exactly
      ``loop.py``'s existing degrade-when-unsigned convention
      (``_bind_signed_catalog_generation``, ``_operatorapp_allows``).
    - ``classification``: independent of any live record, re-running
      ``policy.classify_change`` also catches a catalog that hardened in
      CODE (``policy.RATIFIED_STANDARD_CATALOG`` itself changed between a
      deploy at proposal time and one at dispatch time) -- exactly the case
      a generation counter cannot see, since that catalog carries no
      generation at all.
    """
    record = store.read_spec(paths, "operatorapp", intent.application)
    if record is not None and record.get("generation") is not None:
        current_generation = str(record["generation"])
        generation_known = True
        generation_matches = current_generation == intent.catalog_generation
        app_ratified = set(((record.get("spec") or {}).get("ratifiedStandardActions") or []))
    else:
        current_generation = None
        generation_known = False
        generation_matches = True
        app_ratified = set()
    proxy_action = {
        "name": intent.action,
        "standard": (
            intent.action in app_ratified or intent.action in policy.RATIFIED_STANDARD_CATALOG
        ),
        "rollback_plan": bool(intent.rollback),
        "author": "operator",
    }
    return {
        "generation_known": generation_known,
        "generation_matches": generation_matches,
        "current_generation": current_generation,
        "classification": policy.classify_change(proxy_action),
    }


def _approval_provenance(change: Any) -> dict:
    """The fold's own evidence for why this change reads as approved."""
    tail = change.timeline[-1] if change.timeline else None
    return {
        "change_status": change.status.value,
        "change_type": change.change_type.value,
        "timeline_tail": tail,
    }


@dataclass
class DispatchOutcome:
    intent_id: str
    outcome: str
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"intent_id": self.intent_id, "outcome": self.outcome, "detail": self.detail}


def dispatch_intent(
    paths: FleetPaths,
    ledger: action_ledger.ActionLedger,
    itil: Any,
    intent_id: str,
    *,
    adapters: dict[str, Callable[..., dict]],
    problem_types: set[str],
    apply_fn: Callable[[dict, dict], Any],
    rollback_fn: Callable[[dict, dict, Any], Any] | None = None,
    execution_state: safety.ExecutionState | None = None,
    decisions_dir: str | None = None,
    now_iso: str,
    actor: str = "dispatcher",
    emit: Callable[[str], Any] = lambda _m: None,
) -> DispatchOutcome:
    """Dispatch one PROPOSED intent: refuse, verify, authorize, actuate, verify.

    Safe to call on an intent that turns out not to be ready: returns a
    ``"pending: ..."`` outcome and appends nothing when the intent is not
    currently PROPOSED, carries no ``itil_change_id``, or its change has not
    yet folded to approved -- an unapproved change is simply left alone for a
    later pass, exactly the "never actuates" the negative test requires,
    without manufacturing a false escalation for the ordinary case of "still
    waiting on a human".

    A STALE ``catalog_generation`` or a HARDENED classification is different:
    the fold really did approve this change, so AUTHORIZED/EXECUTING are
    appended (the ledger's lifecycle state machine only reaches ESCALATED via
    FAILED, and the design deliberately adds no new transition -- section
    3.4), but the pre-flight recheck refuses BEFORE ``apply_fn`` is ever
    called, so no rollback is attempted for an actuation that never
    happened, and the pass lands on FAILED -> ESCALATED with the reason on
    both events' detail.
    """
    intent = ledger.read_intent(intent_id)
    current = ledger.current_state(intent_id)
    if current is not action_ledger.ActionState.PROPOSED:
        return DispatchOutcome(intent_id, f"skipped: not proposed (state={current.value})")
    if not intent.itil_change_id:
        return DispatchOutcome(intent_id, "skipped: no linked ITIL change")

    gate = store.check_actuation_gate(paths)
    if not gate.allowed:
        emit(f"dispatch: refused for {intent_id}, {gate.reason}")
        return DispatchOutcome(intent_id, f"refused: {gate.reason}")

    change = _read_change(itil, intent.itil_change_id)
    if change is None:
        return DispatchOutcome(intent_id, "refused: linked ITIL change not found")
    if change.status.value != "approved":
        return DispatchOutcome(intent_id, f"pending: ITIL change status={change.status.value}")

    event_at = _parse_iso(now_iso)
    recheck = _reclassify(paths, intent)
    stale = recheck["generation_known"] and not recheck["generation_matches"]
    hardened = (
        change.change_type.value == "standard"
        and recheck["classification"]["change_class"] != "standard"
    )
    stale_reason: str | None = None
    if stale:
        stale_reason = (
            "stale catalog_generation: proposed against "
            f"{intent.catalog_generation!r}, current is {recheck.get('current_generation')!r}"
        )
    elif hardened:
        stale_reason = (
            "classification hardened since proposal: "
            f"{intent.action!r} is no longer a ratified standard action"
        )

    prop = _reconstruct_proposal(intent)
    classification = recheck["classification"]

    # AUTHORIZED/EXECUTING are appended for BOTH outcomes below: the ledger's
    # lifecycle state machine (action_ledger.py's _TRANSITIONS, deliberately
    # untouched -- section 3.4, "the design is a consumer, three refusals,
    # and a demotion") only reaches ESCALATED via FAILED, and FAILED is only
    # reachable from EXECUTING. AUTHORIZED genuinely did happen here: the
    # fold's own vote really did approve this change; what a stale/hardened
    # recheck refuses is the ACTUATION step, which the try block below never
    # lets run for that case (raises before ever calling apply_fn).
    ledger.append(
        intent_id,
        action_ledger.ActionState.AUTHORIZED,
        occurred_at=event_at,
        actor=actor,
        detail={
            "itil_change_id": change.id,
            "approval_provenance": _approval_provenance(change),
            "reclassification": classification,
            **({"stale_reason": stale_reason} if stale_reason else {}),
        },
    )
    ledger.append(
        intent_id, action_ledger.ActionState.EXECUTING, occurred_at=event_at, actor=actor
    )

    fingerprint = safety.action_fingerprint(prop)
    result: Any = None
    try:
        if stale_reason is not None:
            # Refuse before ever calling apply_fn -- never attempted, so the
            # except block below must not attempt a rollback for it either
            # (see the `isinstance(exc, _PreflightRefusalError)` guard).
            raise _PreflightRefusalError(stale_reason)
        if execution_state is not None:
            eligible, reason = execution_state.eligibility(fingerprint, time.time())
            if not eligible:
                raise RuntimeError(f"execution suppressed: {reason}")
        result = apply_fn(prop, classification)
        performed = result.get("performed") if isinstance(result, dict) else None
        if isinstance(result, dict) and "actuation" in result:
            performed = (result.get("actuation") or {}).get("performed")
        if performed is not True:
            reason = (
                result.get("reason", "actuator omitted performed=True proof")
                if isinstance(result, dict)
                else "invalid actuator response"
            )
            raise RuntimeError(str(reason))
        verified, reason = verify_postcondition(adapters, prop, paths, now_iso, problem_types)
        if not verified:
            raise RuntimeError(reason)
        if execution_state is not None:
            execution_state.record(fingerprint, time.time(), success=True)
        auto_change_id = result.get("change_id") if isinstance(result, dict) else None
        ledger.append(
            intent_id,
            action_ledger.ActionState.VERIFIED,
            occurred_at=_parse_iso(now_iso),
            actor=actor,
            detail={"itil_change_id": auto_change_id} if auto_change_id else {},
        )
        emit(f"dispatch: verified {intent_id}")
        return DispatchOutcome(intent_id, "verified", {"result": result})
    except Exception as exc:  # noqa: BLE001 - contain and record, never crash the pass
        # A preflight refusal (stale/hardened) never called apply_fn: it must
        # never count against the execution-state circuit breaker (nothing
        # was actually attempted) and must never trigger a rollback (there is
        # nothing to roll back -- rollback_fn reverses a real actuation).
        preflight = isinstance(exc, _PreflightRefusalError)
        if execution_state is not None and not preflight:
            try:
                execution_state.record(fingerprint, time.time(), success=False, reason=str(exc))
            except Exception as state_exc:  # noqa: BLE001 - see loop.py's identical rationale
                exc = RuntimeError(f"{exc}; state persistence failed: {state_exc}")
        rollback_result: Any = None
        rollback_error: Exception | None = None
        if not preflight and rollback_fn is not None and prop.get("rollback"):
            try:
                rollback_result = rollback_fn(prop, classification, result)
                rollback_performed = (
                    rollback_result.get("performed") if isinstance(rollback_result, dict) else None
                )
                if rollback_performed is not True:
                    raise RuntimeError("rollback omitted performed=True proof")
            except Exception as rb_exc:  # noqa: BLE001 - contain rollback failure
                rollback_error = rb_exc
        auto_change_id = getattr(exc, "change_id", None)
        if auto_change_id is None and isinstance(result, dict):
            auto_change_id = result.get("change_id")
        fail_detail = {"reason": str(exc)}
        if auto_change_id:
            fail_detail["itil_change_id"] = auto_change_id
        if preflight:
            fail_detail["preflight_refusal"] = True
        ledger.append(
            intent_id,
            action_ledger.ActionState.FAILED,
            occurred_at=_parse_iso(now_iso),
            actor=actor,
            detail=fail_detail,
        )
        if rollback_result is not None and rollback_error is None:
            ledger.append(
                intent_id,
                action_ledger.ActionState.ROLLED_BACK,
                occurred_at=_parse_iso(now_iso),
                actor=actor,
                detail={"result": rollback_result},
            )
            outcome = f"failed then rolled back: {exc}"
        else:
            ledger.append(
                intent_id,
                action_ledger.ActionState.ESCALATED,
                occurred_at=_parse_iso(now_iso),
                actor=actor,
                detail={
                    "decision_parked": decisions_dir is not None,
                    "rollback_error": str(rollback_error) if rollback_error else None,
                },
            )
            if decisions_dir is not None:
                decisions.park(
                    decisions_dir,
                    [{**prop, "rationale": str(exc)}],
                    decision_id=f"failed-{intent_id[3:15]}",
                    created_iso=now_iso,
                )
            outcome = f"escalated: {exc}" if preflight else f"failed: {exc}"
        emit(f"dispatch: {outcome} ({intent_id})")
        return DispatchOutcome(intent_id, outcome)


def run_dispatch_pass(
    paths: FleetPaths,
    ledger: action_ledger.ActionLedger,
    itil: Any,
    *,
    adapters: dict[str, Callable[..., dict]],
    problem_types: set[str],
    apply_fn: Callable[[dict, dict], Any],
    rollback_fn: Callable[[dict, dict, Any], Any] | None = None,
    execution_state: safety.ExecutionState | None = None,
    decisions_dir: str | None = None,
    now_iso: str,
    actor: str = "dispatcher",
    emit: Callable[[str], Any] = lambda _m: None,
) -> list[DispatchOutcome]:
    """Enumerate every PROPOSED, ITIL-bound ledger intent and dispatch each.

    The entry point ``skoperator honor-pending`` uses, and what
    ``loop._run_once`` re-points its own auto lane through when an ITIL
    manager is wired: "one code path, two speeds"
    (AUTONOMY_ARCHITECTURE.md section 3.2).
    """
    gate = store.check_actuation_gate(paths)
    if not gate.allowed:
        emit(f"dispatch: pass refused, {gate.reason}")
        return []
    outcomes = []
    for intent_id in ledger.list_intents(state=action_ledger.ActionState.PROPOSED):
        intent = ledger.read_intent(intent_id)
        if not intent.itil_change_id:
            continue  # invisible to the dispatcher forever, by design
        outcomes.append(
            dispatch_intent(
                paths,
                ledger,
                itil,
                intent_id,
                adapters=adapters,
                problem_types=problem_types,
                apply_fn=apply_fn,
                rollback_fn=rollback_fn,
                execution_state=execution_state,
                decisions_dir=decisions_dir,
                now_iso=now_iso,
                actor=actor,
                emit=emit,
            )
        )
    return outcomes


__all__ = [
    "UnauthorizedDecisionError",
    "DispatchOutcome",
    "condition_firing",
    "verify_postcondition",
    "resolve_decision",
    "dispatch_intent",
    "run_dispatch_pass",
]
