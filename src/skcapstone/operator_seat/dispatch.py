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

import fcntl
import math
import os
import re
import time
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterator
from urllib.parse import quote

from ..fleet import store
from ..fleet.paths import FleetPaths
from ..itil import CABDecisionValue, _is_human_approval
from . import action_ledger, decisions, evidence, node_local, policy, safety

#: Prefix of the tag the loop puts on every change it drafts, naming the ONE intent
#: (app, action, target) that change authorizes.
BINDING_TAG_PREFIX = "op-bind:"


#: How long a human CAB approval stays usable, measured from the vote's ``decided_at``
#: against the real clock. A human approves a CHANGE for an EPISODE; a day is generous for
#: the slow lane and short enough that a recurrence next week cannot ride an old vote.
#: Configurable (``honor-pending --max-approval-age``) up to the cap below; Chef can relax it.
DEFAULT_MAX_APPROVAL_AGE_SECONDS = 86400.0
MAX_ALLOWED_APPROVAL_AGE_SECONDS = 604800.0  # seven days

#: How long an intent may sit in AUTHORIZED/EXECUTING before it is treated as stranded by a
#: crash. A pass has a five minute runtime budget, so twice that is not a live dispatch.
DEFAULT_EXECUTING_TIMEOUT_SECONDS = 600.0

_CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def change_binding_tag(app: str, action: str, target: str) -> str:
    """The tag that binds a drafted ITIL change to the (app, action, target) it is for.

    Each part is percent-escaped, so no part can inject the ``|`` separator and two
    different triples can never produce the same tag; a raw ``|`` in a part is rejected
    outright (``ValueError``) rather than relied on to be escaped.
    """
    parts = (app, action, target)
    if any("|" in part for part in parts):
        raise ValueError("an app, action or target containing '|' cannot be bound")
    return BINDING_TAG_PREFIX + "|".join(quote(part, safe="") for part in parts)


_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _parse_iso(now_iso: str) -> datetime:
    return datetime.fromisoformat(now_iso.replace("Z", "+00:00"))


def _event_dt(clock: Callable[[], datetime] | None) -> datetime:
    """The time to stamp a lifecycle event with: the clock AT WRITE TIME, never the pass start.

    Stamping events from the pass-start ``now_iso`` made the second intent of a long pass look
    as old as the pass the moment it was written, which the stranded sweep then read as a crash.
    A naive clock falls back to the real one rather than crashing the write.
    """
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    if moment.tzinfo is None:
        return datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc)


def _clock_iso(clock: Callable[[], datetime] | None) -> str:
    """The REAL clock now (or the injected one), as an aware ISO 8601 string."""
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    if moment.tzinfo is None:
        # Never read a naive clock as local time: leave it naive so evidence.refusal
        # refuses it ("the clock is not a timezone-aware timestamp").
        return moment.isoformat()
    return moment.astimezone(timezone.utc).isoformat()


def is_freeze_refusal(exc: BaseException) -> bool:
    """Whether a failure IS the kill switch refusing, not a fault of the action.

    Every actuation surface refuses with ``fleet is frozen: ...`` (or the actuator
    gate's bare ``frozen`` reason). A genuine failure that merely COINCIDES with a
    freeze is not matched, so it still counts against the retry budget.
    """
    text = str(exc).strip().lower()
    return "fleet is frozen" in text or text == f"actuation failed: {store.REASON_FROZEN}"


def void_proposed(
    ledger: action_ledger.ActionLedger,
    intent_id: str,
    *,
    occurred_at: datetime,
    actor: str,
    reason: str,
) -> bool:
    """Close a PROPOSED intent that will not be actuated, so nothing acts on it later.

    The dispatcher enumerates PROPOSED, ITIL-bound intents; an intent written but not
    actuated in the pass that wrote it would otherwise sit there until a later pass
    (or ``skoperator honor-pending``) acted on it, unattended and on old evidence.
    The void is the existing terminal ESCALATED state reached over the PROPOSED edge,
    with ``voided`` on its detail. Returns whether anything was closed.
    """
    if ledger.current_state(intent_id) is not action_ledger.ActionState.PROPOSED:
        return False
    ledger.append(
        intent_id,
        action_ledger.ActionState.ESCALATED,
        occurred_at=occurred_at,
        actor=actor,
        detail={"voided": True, "reason": reason, "decision_parked": False},
    )
    return True


def _proposed_detail(ledger: action_ledger.ActionLedger, intent_id: str) -> dict:
    for event in ledger.events(intent_id):
        if event.state is action_ledger.ActionState.PROPOSED:
            return dict(event.detail)
    return {}


def stored_evidence(ledger: action_ledger.ActionLedger, intent_id: str) -> dict | None:
    """The structured evidence stamp the loop recorded on the intent's PROPOSED event."""
    stamp = _proposed_detail(ledger, intent_id).get("evidence")
    return stamp if isinstance(stamp, dict) else None


def stored_lane(ledger: action_ledger.ActionLedger, intent_id: str) -> str | None:
    """The lane the loop recorded on the PROPOSED event: ``auto`` or ``escalate``.

    None for an intent that predates the field or was written by hand: it cannot be
    told apart from a pending human decision, so it is never acted on as auto and
    never voided.
    """
    lane = _proposed_detail(ledger, intent_id).get("disposition")
    return lane if lane in ("auto", "escalate") else None


def human_approval_times(itil: Any, change: Any) -> list[str]:
    """``decided_at`` of every qualifying human approval; ``[]`` means NOT human-approved.

    Derived from the CAB votes, never inferred from a missing ``auto-approved`` marker: the
    fold only writes that marker while a change is still PROPOSED, so a standard change that
    goes approved, scheduled, unscheduled folds ``approved`` with no marker and used to read
    as human. A change is human-approved only when there is a qualifying human approval vote
    (``_is_human_approval``, the drafter's own excluded, exactly as the fold does) and no
    rejection. A ``standard`` change, or one that meets the fold's auto-normal derivation, is
    auto-approved whatever its timeline says, and a human vote on it does not change that.
    Fails closed: a vote store that cannot be read means no human approval.
    """
    if change.change_type.value == "standard":
        return []
    try:
        votes = list(itil.get_cab_votes(change.id))
    except Exception:  # noqa: BLE001 - unreadable votes are no approval
        return []
    rejected = any(v.decision == CABDecisionValue.REJECTED for v in votes)
    if (
        change.change_type.value == "normal"
        and "auto-normal" in (change.tags or [])
        and change.created_by == "operator"
        and change.risk.value != "high"
        and (change.rollback_plan or "").strip()
        and not rejected
    ):
        return []
    if rejected:
        return []
    return [
        str(v.decided_at)
        for v in votes
        if v.decision == CABDecisionValue.APPROVED
        and v.agent != change.prepared_by
        and _is_human_approval(v)
    ]


def change_human_approved(itil: Any, change: Any) -> bool:
    """Whether a qualifying human approved this change (see ``human_approval_times``)."""
    return bool(human_approval_times(itil, change))


def _valid_approval_age(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value) and 0 < value <= MAX_ALLOWED_APPROVAL_AGE_SECONDS
    except OverflowError:
        return False


def approval_refusal(times: list[str], clock_iso: str, max_age_s: Any) -> tuple[str, str] | None:
    """Why a human approval may not be used, as ``(kind, reason)``, or None when usable.

    ``kind`` is ``"invalid"`` (bad configuration, clock or unreadable time: refuse, close
    nothing), ``"future"`` (dated more than the skew after the clock) or ``"expired"`` (older
    than the limit). The caller CLOSES the intent and retires its change for both ``"future"``
    and ``"expired"``: refusing a future-dated vote alone would let it buy time. The newest
    qualifying vote counts.
    """
    if not _valid_approval_age(max_age_s):
        return "invalid", f"max approval age invalid: {max_age_s!r}"
    now = evidence.parse_aware(clock_iso)
    if now is None:
        return "invalid", "human approval unverifiable: the clock is not timezone-aware"
    decided = [d for d in (evidence.parse_aware(t) for t in times) if d is not None]
    if not decided:
        return "invalid", "human approval time unreadable"
    newest = max(decided)
    age = (now - newest).total_seconds()
    if age < -evidence.MAX_FUTURE_SKEW_SECONDS:
        return "future", f"human approval is dated in the future: {-age:.0f}s ahead"
    if age > max_age_s:
        return "expired", f"human approval expired: decided {age:.0f}s ago, max {max_age_s:g}s"
    return None


def change_bound_to(change: Any, intent: action_ledger.ActionIntent) -> bool:
    """Whether the change was drafted for exactly this intent's (app, action, target).

    An approval authorizes ONE thing. A change with no binding tag (a legacy or
    hand-made one, or one a proposal pointed at), or one bound to something else, or
    with more than one binding tag, authorizes nothing here.
    """
    try:
        expected = change_binding_tag(intent.application, intent.action, intent.target_id)
    except ValueError:
        return False
    tags = [t for t in (change.tags or []) if str(t).startswith(BINDING_TAG_PREFIX)]
    return tags == [expected]


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


def reobserve(
    observers: dict[str, Callable[..., dict]],
    proposal: dict,
    paths: FleetPaths,
    now_iso: str,
    problem_types: set[str],
    *,
    clock_iso: str,
    max_evidence_age_s: float,
) -> tuple[str, str]:
    """Re-observe the owning app BEFORE acting. Returns ``(verdict, reason)`` where the
    verdict is ``"firing"`` (act), ``"cleared"`` (a definite not-firing reading) or
    ``"indeterminate"`` (do not act, and do not conclude it cleared).

    Only a DEFINITE ``True``/``False`` status counts. ``Unknown`` is not "still
    firing" (``condition_firing`` treats it as firing so that a dead probe raises an
    alarm; that is right for alerting and wrong for permission to act). No observer,
    an observer that raises, no matching condition, an ``Unknown`` one, or a reading
    with no fresh source time of its own (the re-observation must itself be fresh on
    the real clock) are all indeterminate.
    """
    observer = observers.get(proposal.get("app"))
    if observer is None:
        return "indeterminate", "owning observer unavailable"
    try:
        payload = observer(paths, now_iso)
        conditions = payload.get("conditions", [])
    except Exception as exc:  # noqa: BLE001 - an unreadable probe is never "still firing"
        return "indeterminate", f"re-observation failed: {type(exc).__name__}"
    matches = [
        item
        for item in conditions
        if item.get("type") == proposal.get("condition")
        and (proposal.get("object") is None or item.get("object") == proposal.get("object"))
    ]
    if not matches:
        return "indeterminate", "bound condition not observed before action"
    if any(item.get("status") not in ("True", "False") for item in matches):
        return "indeterminate", "bound condition status Unknown at re-observation"
    for item in matches:
        stamp = item.get("source_observed_at", payload.get("source_observed_at"))
        if evidence.parse_aware(stamp) is None:
            return "indeterminate", "re-observation carries no usable source time"
        stale = evidence.refusal(
            {"evidence": {"source": "re-observation", "observed_at": stamp}},
            clock_iso,
            max_evidence_age_s,
        )
        if stale is not None:
            return "indeterminate", f"re-observation not usable: {stale}"
    if any(condition_firing(item, problem_types) for item in matches):
        return "firing", "condition still firing"
    return "cleared", "bound condition no longer firing (re-observed before action)"


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
        "catalog_generation": intent.catalog_generation,
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


class _LockBusyError(RuntimeError):
    """The per-change lock is held by a live dispatch (or sweep) on this node."""


@contextmanager
def _change_lock(change_id: str, *, blocking: bool = False) -> Iterator[None]:
    """An exclusive per-change-id flock in the NODE-LOCAL state directory.

    Held by a dispatch from the moment it commits to a change until it has closed the change
    out, so a sweep (or a second dispatch) that cannot take it knows the change is live and
    leaves it alone. It deliberately lives outside the synced fleet tree (an flock orders
    processes on one kernel only; cross-node safety is the single-actuator-node rule in
    ``node_local``), in a directory verified private (0700, ours, not a symlink). Raises
    ``_LockBusyError`` when held and ``blocking`` is False, and ``node_local.UnsafeDirectoryError``
    when the directory is not private.
    """
    if not _CHANGE_ID_RE.fullmatch(change_id):
        raise ValueError(f"invalid change id for locking: {change_id!r}")
    directory = node_local.private_chain(node_local.state_dir(), "atlas", "change-locks")
    fd = os.open(directory / f"{change_id}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError as exc:
            raise _LockBusyError(change_id) from exc
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _spend_change(itil: Any, change_id: str, intent_id: str, actor: str) -> str | None:
    """Take the approval for ONE dispatch: a compare-and-swap, or the reason it was lost.

    The caller holds the per-change lock. Re-read the change (it must still be ``approved``),
    move it to ``implementing`` with a note naming this intent, and require that the FIRST
    unconflicted ``approved->implementing`` timeline entry is ours. The re-read matters because
    the lock only orders writers on this node; the timeline check matters because a conflicted
    second ``implementing`` event still folds ``implementing``, so the status alone cannot tell
    the winner from the loser. Returns None when this dispatch owns the approval.
    """
    mine = f"dispatch of {intent_id}"
    try:
        current = _read_change(itil, change_id)
        if current is None or current.status.value != "approved":
            status = current.status.value if current is not None else "missing"
            return f"the change is no longer approved (status={status}); already taken"
        taken = itil.update_change(change_id, actor, new_status="implementing", note=mine)
    except Exception as exc:  # noqa: BLE001 - an approval that cannot be spent is not usable
        return f"could not take the approval: {exc}"
    entries = [
        e
        for e in (getattr(taken, "timeline", None) or [])
        if isinstance(e, dict)
        and e.get("action") in ("status:approved->implementing", "status:scheduled->implementing")
        and not e.get("conflicted")
    ]
    if not entries or entries[0].get("note") != mine:
        return "the approval was taken by another dispatch"
    return None


def _retire_change(itil: Any, change_id: str, actor: str, note: str) -> None:
    """Move an approval that will not be used to a terminal state (rejected, then closed),
    so a later recurrence needs a new proposal and a new human vote. Best effort."""
    for status in ("rejected", "closed"):
        try:
            itil.update_change(change_id, actor, new_status=status, note=note[:200])
        except Exception:  # noqa: BLE001 - the intent is already closed; this is bookkeeping
            return


def _close_intent(
    ledger: action_ledger.ActionLedger,
    itil: Any,
    change: Any,
    intent_id: str,
    *,
    occurred_at: datetime,
    actor: str,
    reason: str,
) -> None:
    """Close a PROPOSED intent that can never be actuated and retire its change."""
    void_proposed(ledger, intent_id, occurred_at=occurred_at, actor=actor, reason=reason)
    _retire_change(itil, change.id, actor, reason)


def escalate_stranded(
    ledger: action_ledger.ActionLedger,
    itil: Any,
    *,
    now_iso: str,
    clock: Callable[[], datetime] | None = None,
    actor: str = "dispatcher",
    decisions_dir: str | None = None,
    emit: Callable[[str], Any] = lambda _m: None,
    executing_timeout_s: float = DEFAULT_EXECUTING_TIMEOUT_SECONDS,
) -> list[DispatchOutcome]:
    """Escalate intents a crash stranded in AUTHORIZED or EXECUTING. NEVER re-runs them.

    The actuator may or may not have run, so the only safe move is a human: the intent goes
    FAILED then ESCALATED, its change is closed out (``failed`` if it was already taken,
    retired if it was still only approved), a decision is parked for a human and an alert is
    emitted. Two guards keep it from touching a LIVE dispatch: lifecycle events are stamped
    from the clock at write time, so an intent is only "old" if it has really sat that long;
    and a live dispatch holds its per-change lock from commit to close-out, so a change whose
    lock cannot be taken (non-blocking) is skipped. An intent with no change id (the inline
    lane) has no lock to test and relies on the age alone.
    """
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    if moment.tzinfo is None:
        raise ValueError("clock must be timezone-aware")
    out: list[DispatchOutcome] = []
    for state in (action_ledger.ActionState.EXECUTING, action_ledger.ActionState.AUTHORIZED):
        for intent_id in ledger.list_intents(state=state):
            events = ledger.events(intent_id)
            if not events or (moment - events[-1].occurred_at).total_seconds() < (
                executing_timeout_s
            ):
                continue
            intent = ledger.read_intent(intent_id)
            with ExitStack() as stack:
                if intent.itil_change_id:
                    try:
                        stack.enter_context(_change_lock(intent.itil_change_id))
                    except _LockBusyError:
                        continue  # a live dispatch owns it
                    except (node_local.UnsafeDirectoryError, ValueError) as exc:
                        emit(f"dispatch: cannot test the lock for {intent_id}: {exc}")
                        continue
                    if ledger.current_state(intent_id) is not state:
                        continue  # it moved on while we were deciding
                reason = (
                    f"stranded in {state.value} (a dispatcher crashed or was killed mid-run); "
                    "the actuator may or may not have run, so it is not re-run"
                )
                at = _event_dt(clock)
                if state is action_ledger.ActionState.AUTHORIZED:
                    ledger.append(
                        intent_id, action_ledger.ActionState.EXECUTING, occurred_at=at, actor=actor
                    )
                ledger.append(
                    intent_id,
                    action_ledger.ActionState.FAILED,
                    occurred_at=at,
                    actor=actor,
                    detail={"reason": reason, "stranded": True},
                )
                ledger.append(
                    intent_id,
                    action_ledger.ActionState.ESCALATED,
                    occurred_at=at,
                    actor=actor,
                    detail={"decision_parked": decisions_dir is not None, "stranded": True},
                )
                if intent.itil_change_id:
                    current = _read_change(itil, intent.itil_change_id)
                    if current is not None and current.status.value == "implementing":
                        _close_out_change(itil, current.id, actor, "failed", reason, emit)
                    elif current is not None and current.status.value in ("approved", "scheduled"):
                        _retire_change(itil, current.id, actor, reason)
                if decisions_dir is not None:
                    decisions.park(
                        decisions_dir,
                        [{**_reconstruct_proposal(intent), "rationale": reason}],
                        decision_id=f"stranded-{intent_id[3:15]}",
                        created_iso=now_iso,
                    )
                emit(f"dispatch: ALERT {intent_id} {reason}")
                out.append(DispatchOutcome(intent_id, f"escalated: {reason}"))
    return out


def close_episode(
    ledger: action_ledger.ActionLedger,
    itil: Any,
    intent_id: str,
    *,
    clock: Callable[[], datetime] | None = None,
    actor: str = "dispatcher",
    reason: str,
) -> bool:
    """Close an open intent whose EPISODE is over and retire its change.

    Used by the loop when it observes the bound condition definitely clear, so a vote (or an
    approval) from one episode can never ride the next. Skips an intent whose change is locked
    by a live dispatch. Returns whether anything was closed.
    """
    intent = ledger.read_intent(intent_id)
    with ExitStack() as stack:
        if intent.itil_change_id:
            try:
                stack.enter_context(_change_lock(intent.itil_change_id))
            except (_LockBusyError, node_local.UnsafeDirectoryError, ValueError):
                return False
        if not void_proposed(
            ledger, intent_id, occurred_at=_event_dt(clock), actor=actor, reason=reason
        ):
            return False
        if intent.itil_change_id:
            _retire_change(itil, intent.itil_change_id, actor, reason)
        return True


def void_stale_auto_intents(
    ledger: action_ledger.ActionLedger,
    itil: Any,
    *,
    clock: Callable[[], datetime] | None = None,
    actor: str = "dispatcher",
    emit: Callable[[str], Any] = lambda _m: None,
    max_age_s: float = DEFAULT_EXECUTING_TIMEOUT_SECONDS,
) -> list[str]:
    """Void auto-lane PROPOSED intents older than one pass: leftovers of a killed loop.

    The loop writes an auto-lane intent and dispatches it within the same pass, or voids it
    (``loop``). If the process was killed in between, the intent is left PROPOSED with no one
    waiting on it, and a later pass could act on it unattended. An auto-lane intent older than
    a pass (twice the runtime budget) is closed, and its change retired, before anything else
    runs. Escalate-lane intents are never touched: a human decision may be waiting on them.
    """
    moment = (clock or (lambda: datetime.now(timezone.utc)))()
    if moment.tzinfo is None:
        raise ValueError("clock must be timezone-aware")
    closed: list[str] = []
    for intent_id in ledger.list_intents(state=action_ledger.ActionState.PROPOSED):
        events = ledger.events(intent_id)
        if not events or (moment - events[-1].occurred_at).total_seconds() < max_age_s:
            continue
        if stored_lane(ledger, intent_id) != "auto":
            continue
        intent = ledger.read_intent(intent_id)
        reason = "auto-lane intent older than one pass (left by a killed loop); voided unattended"
        with ExitStack() as stack:
            if intent.itil_change_id:
                try:
                    stack.enter_context(_change_lock(intent.itil_change_id))
                except _LockBusyError:
                    continue
                except (node_local.UnsafeDirectoryError, ValueError):
                    continue
            if void_proposed(
                ledger, intent_id, occurred_at=_event_dt(clock), actor=actor, reason=reason
            ):
                if intent.itil_change_id:
                    _retire_change(itil, intent.itil_change_id, actor, reason)
                emit(f"dispatch: voided stale auto-lane intent {intent_id}")
                closed.append(intent_id)
    return closed


def _close_out_change(
    itil: Any, change_id: str, actor: str, status: str, note: str, emit: Callable[[str], Any]
) -> None:
    """Move a spent change on (deployed or failed). Best effort: the change already left
    ``approved`` when it was taken, so it cannot be reused even if this write fails."""
    try:
        itil.update_change(change_id, actor, new_status=status, note=note)
    except Exception as exc:  # noqa: BLE001 - the spent change is not reusable regardless
        emit(f"dispatch: could not mark change {change_id} {status}: {exc}")


def _preaction_refusal(
    paths: FleetPaths,
    ledger: action_ledger.ActionLedger,
    itil: Any,
    intent: action_ledger.ActionIntent,
    change: Any,
    prop: dict,
    *,
    stale_reason: str | None,
    adapters: dict[str, Callable[..., dict]],
    problem_types: set[str],
    now_iso: str,
    event_at: datetime,
    actor: str,
    clock: Callable[[], datetime] | None,
    max_evidence_age_s: float,
    target_known: Callable[[dict], bool] | None,
    action_allowed: Callable[[dict], bool] | None,
    execution_state: safety.ExecutionState | None,
    max_approval_age_s: float,
) -> DispatchOutcome | None:
    """Everything that must hold before AUTHORIZED, or the refusal to return.

    Nothing is written to the ledger for a refusal except the void of an auto-lane
    intent that can never become actionable (stale stored evidence, a definitely
    cleared condition). In particular a refusal never appends AUTHORIZED: nothing was
    authorized. The order matters: what the change IS (bound to this intent, who
    approved it, which lane the loop recorded) is settled before any other judgement,
    so a stale catalog can never close an intent that has no human approval behind it.
    """
    intent_id = intent.intent_id
    if not change_bound_to(change, intent):
        return DispatchOutcome(
            intent_id,
            "refused: the ITIL change is not bound to this intent (an approval authorizes "
            "one app, action and target)",
        )
    approval_times = human_approval_times(itil, change)
    human_approved = bool(approval_times)
    auto_approved = not human_approved
    lane = stored_lane(ledger, intent_id)
    if auto_approved and lane == "escalate":
        return DispatchOutcome(
            intent_id,
            "pending: no human approval (the proposal was escalated for a human and the "
            "change carries no qualifying human vote); left PROPOSED for its pending decision",
        )
    if auto_approved and lane != "auto":
        return DispatchOutcome(
            intent_id,
            "refused: no qualifying human approval and no recorded auto lane (cannot tell "
            "it from a pending human decision)",
        )
    if human_approved:
        # A human approves a change for an EPISODE, and the approval expires: past the
        # limit it is refused and the intent closed (a new vote needs a new proposal).
        # A bad configuration or a future-dated vote is refused and closes nothing.
        stale_vote = approval_refusal(approval_times, _clock_iso(clock), max_approval_age_s)
        if stale_vote is not None:
            kind, why = stale_vote
            if kind in ("expired", "future"):
                # A vote dated in the future is closed too: merely refusing it would let it
                # buy time (a vote dated five days ahead would be refused now and honored
                # when its date arrives, long past the age limit it was meant to meet).
                _close_intent(
                    ledger,
                    itil,
                    change,
                    intent_id,
                    occurred_at=_event_dt(clock),
                    actor=actor,
                    reason=why,
                )
            return DispatchOutcome(intent_id, f"refused: {why}")
    if stale_reason is not None:
        return DispatchOutcome(intent_id, f"refused: {stale_reason}")
    if target_known is not None and not target_known(prop):
        return DispatchOutcome(intent_id, "refused: target not resolvable at dispatch")
    if action_allowed is not None and not action_allowed(prop):
        return DispatchOutcome(intent_id, "refused: action not allowed for this app at dispatch")
    clock_iso = _clock_iso(clock)
    if auto_approved:
        stale = evidence.refusal(
            {"evidence": stored_evidence(ledger, intent_id)}, clock_iso, max_evidence_age_s
        )
        if stale is not None:
            _close_intent(
                ledger,
                itil,
                change,
                intent_id,
                occurred_at=_event_dt(clock),
                actor=actor,
                reason=stale,
            )
            return DispatchOutcome(intent_id, f"refused: {stale}")
    if execution_state is not None:
        # Cooldown and an open circuit are SUPPRESSION: nothing is attempted, so it is
        # not a failure, not recorded against the breaker, and never rolled back. The
        # intent simply stays PROPOSED (the loop voids it if it is an auto-lane one).
        eligible, reason = execution_state.eligibility(
            safety.action_fingerprint(prop), time.time()
        )
        if not eligible:
            return DispatchOutcome(intent_id, f"refused: execution suppressed: {reason}")
    verdict, why = reobserve(
        adapters,
        prop,
        paths,
        now_iso,
        problem_types,
        clock_iso=clock_iso,
        max_evidence_age_s=max_evidence_age_s,
    )
    if verdict == "firing":
        return None
    if verdict == "cleared":
        # The episode this intent (and its approval, human or not) was for is over. Close the
        # intent and retire the change: a recurrence is a new episode and needs a new
        # proposal and, for the human lane, a new human vote.
        reason = f"condition cleared before dispatch ({why})"
        _close_intent(
            ledger,
            itil,
            change,
            intent_id,
            occurred_at=_event_dt(clock),
            actor=actor,
            reason=reason,
        )
        return DispatchOutcome(intent_id, f"refused: {reason}")
    return DispatchOutcome(intent_id, f"refused: {why}")


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
    clock: Callable[[], datetime] | None = None,
    max_evidence_age_s: float = evidence.DEFAULT_MAX_AGE_SECONDS,
    target_known: Callable[[dict], bool] | None = None,
    action_allowed: Callable[[dict], bool] | None = None,
    max_approval_age_s: float = DEFAULT_MAX_APPROVAL_AGE_SECONDS,
) -> DispatchOutcome:
    """Dispatch one PROPOSED intent: refuse, verify, authorize, actuate, verify.

    Before anything is authorized the dispatcher checks, for ANY intent however it
    was left, that acting is still justified:

    * it re-observes the owning app and requires the bound condition to be DEFINITELY
      firing now (``reobserve``; Unknown is not firing, and the reading must itself
      be fresh). A human-approved change may wait hours or days; the approval is for
      the condition, not for whatever the estate looked like then;
    * it re-checks the target (``target_known``) and the ratified action binding
      (``action_allowed``) when the caller supplies them, because the planner's
      verdict at proposal time is not a permission that lasts;
    * the change must be bound to THIS intent (``change_bound_to``: app, action and
      target), and is used up by the run it authorizes. Taking it is a compare-and-swap
      (``_spend_change``) under an exclusive per-change lock on this host: the change is
      re-read, moved to ``implementing`` with a note naming this intent, and this dispatch
      must be the FIRST unconflicted ``approved->implementing`` entry on its timeline, or
      nothing is attempted. It then goes ``deployed`` or ``failed``;
    * a human approval covers one EPISODE and expires (``approval_refusal``): older than
      ``max_approval_age_s`` (default a day, capped at a week) it is refused and the intent
      closed, and when re-observation sees the condition definitely cleared the intent is
      closed and the change retired, so a recurrence needs a new proposal and a new vote;
    * "human-approved" is derived from the VOTES (``change_human_approved``), never from
      a missing ``auto-approved`` marker. An intent the loop recorded as ESCALATED with
      no qualifying human vote stays pending and is NEVER voided, because a human
      decision is waiting on it; one with no recorded lane is refused and left alone;
    * a change with no human behind it (standard, or ``auto-normal``) must carry fresh
      evidence stored on the intent's PROPOSED event, against the REAL clock. Stored
      evidence only ages, so an auto-lane intent that fails this, or whose condition
      definitely cleared, is voided (``void_proposed``) rather than retried forever;
    * cooldown and an open circuit are suppression, not failure: a refusal before
      AUTHORIZED with nothing recorded and nothing rolled back.

    A human-approved intent is exempt from the EVIDENCE age limit (the approval legitimately
    arrives long after the observation) but not from its own approval-age limit, and never
    from the re-observation.

    Safe to call on an intent that turns out not to be ready: returns a
    ``"pending: ..."`` outcome and appends nothing when the intent is not
    currently PROPOSED, carries no ``itil_change_id``, or its change has not
    yet folded to approved -- an unapproved change is simply left alone for a
    later pass, exactly the "never actuates" the negative test requires,
    without manufacturing a false escalation for the ordinary case of "still
    waiting on a human".

    A STALE ``catalog_generation`` or a HARDENED classification refuses too, AFTER the
    change, human-approval and lane checks, and leaves the intent PROPOSED with no
    AUTHORIZED written: nothing was authorized, and closing the intent here would let a
    stale catalog destroy a pending human decision. A proposal under the new catalog is
    a new intent (``catalog_generation`` is part of the identity).
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
    not_actuator = node_local.actuator_refusal()
    if not_actuator is not None:
        emit(f"dispatch: refused for {intent_id}, {not_actuator}")
        return DispatchOutcome(intent_id, f"refused: {not_actuator}")

    change = _read_change(itil, intent.itil_change_id)
    if change is None:
        return DispatchOutcome(intent_id, "refused: linked ITIL change not found")
    if change.status.value != "approved":
        return DispatchOutcome(intent_id, f"pending: ITIL change status={change.status.value}")

    # Commit to this change: take its per-change lock (non-blocking) and hold it through
    # close-out. A live dispatch is therefore visible to the stranded sweep and to a second
    # dispatch, which both back off instead of racing it.
    stack = ExitStack()
    try:
        stack.enter_context(_change_lock(change.id))
    except _LockBusyError:
        return DispatchOutcome(
            intent_id, "pending: the change is being dispatched by another process"
        )
    except node_local.UnsafeDirectoryError as exc:
        return DispatchOutcome(intent_id, f"refused: {exc}")
    except OSError as exc:
        emit(f"dispatch: error taking the change lock for {intent_id}: {exc}")
        return DispatchOutcome(intent_id, f"error: could not take the change lock: {exc}")
    with stack:
        event_at = _parse_iso(now_iso)
        prop = _reconstruct_proposal(intent)
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

        refusal = _preaction_refusal(
            paths,
            ledger,
            itil,
            intent,
            change,
            prop,
            stale_reason=stale_reason,
            adapters=adapters,
            problem_types=problem_types,
            now_iso=now_iso,
            event_at=event_at,
            actor=actor,
            clock=clock,
            max_evidence_age_s=max_evidence_age_s,
            target_known=target_known,
            action_allowed=action_allowed,
            execution_state=execution_state,
            max_approval_age_s=max_approval_age_s,
        )
        if refusal is not None:
            emit(f"dispatch: {refusal.outcome} ({intent_id})")
            return refusal

        classification = recheck["classification"]

        # Every refusal has been taken by now: what follows is a real authorization. A ledger
        # error here (an invalid transition, an unreadable stream) is this intent's problem, not
        # the pass's: record it and let the caller carry on with the rest.
        try:
            ledger.append(
                intent_id,
                action_ledger.ActionState.AUTHORIZED,
                occurred_at=_event_dt(clock),
                actor=actor,
                detail={
                    "itil_change_id": change.id,
                    "approval_provenance": _approval_provenance(change),
                    "reclassification": classification,
                },
            )
            ledger.append(
                intent_id,
                action_ledger.ActionState.EXECUTING,
                occurred_at=_event_dt(clock),
                actor=actor,
            )
        except Exception as exc:  # noqa: BLE001 - contained per intent
            emit(f"dispatch: error recording the authorization of {intent_id}: {exc}")
            return DispatchOutcome(intent_id, f"error: could not record the authorization: {exc}")

        fingerprint = safety.action_fingerprint(prop)
        result: Any = None
        change_taken = False
        try:
            # Use the approval up BEFORE the actuator runs, as a compare-and-swap. If this
            # dispatch does not own it, nothing is attempted.
            lost = _spend_change(itil, change.id, intent_id, actor)
            if lost is not None:
                raise _PreflightRefusalError(lost)
            change_taken = True
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
        except Exception as exc:  # noqa: BLE001 - contain and record, never crash the pass
            # A preflight refusal (stale/hardened) never called apply_fn: it must
            # never count against the execution-state circuit breaker (nothing
            # was actually attempted) and must never trigger a rollback (there is
            # nothing to roll back -- rollback_fn reverses a real actuation).
            preflight = isinstance(exc, _PreflightRefusalError)
            if change_taken:
                _close_out_change(itil, change.id, actor, "failed", str(exc)[:200], emit)
            # The kill switch REFUSING is not a fault of the action and must not feed the
            # circuit breaker. A genuine failure that merely coincides with a freeze is
            # still a failure and still counts.
            if execution_state is not None and not preflight and not is_freeze_refusal(exc):
                try:
                    execution_state.record(
                        fingerprint, time.time(), success=False, reason=str(exc)
                    )
                except Exception as state_exc:  # noqa: BLE001 - see loop.py's identical rationale
                    exc = RuntimeError(f"{exc}; state persistence failed: {state_exc}")
            rollback_result: Any = None
            rollback_error: Exception | None = None
            if not preflight and rollback_fn is not None and prop.get("rollback"):
                try:
                    rollback_result = rollback_fn(prop, classification, result)
                    rollback_performed = (
                        rollback_result.get("performed")
                        if isinstance(rollback_result, dict)
                        else None
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
                occurred_at=_event_dt(clock),
                actor=actor,
                detail=fail_detail,
            )
            if rollback_result is not None and rollback_error is None:
                ledger.append(
                    intent_id,
                    action_ledger.ActionState.ROLLED_BACK,
                    occurred_at=_event_dt(clock),
                    actor=actor,
                    detail={"result": rollback_result},
                )
                outcome = f"failed then rolled back: {exc}"
            else:
                ledger.append(
                    intent_id,
                    action_ledger.ActionState.ESCALATED,
                    occurred_at=_event_dt(clock),
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

        # The action ran AND its postcondition verified. From here on nothing is rolled back and
        # nothing is treated as a failed action: a bookkeeping error (the ledger, the ITIL record)
        # is recorded and alerted, because undoing a fix that worked would be worse than the error.
        try:
            if execution_state is not None:
                execution_state.record(fingerprint, time.time(), success=True)
            _close_out_change(itil, change.id, actor, "deployed", f"{intent_id} verified", emit)
            auto_change_id = result.get("change_id") if isinstance(result, dict) else None
            ledger.append(
                intent_id,
                action_ledger.ActionState.VERIFIED,
                occurred_at=_event_dt(clock),
                actor=actor,
                detail={"itil_change_id": auto_change_id} if auto_change_id else {},
            )
        except Exception as exc:  # noqa: BLE001 - alert, never roll back a fix that worked
            message = (
                f"the action ran and verified but could not be recorded: {exc}; it is NOT rolled "
                "back, a human must reconcile the ledger"
            )
            emit(f"dispatch: ALERT {intent_id} {message}")
            return DispatchOutcome(intent_id, f"error: {message}", {"result": result})
        emit(f"dispatch: verified {intent_id}")
        return DispatchOutcome(intent_id, "verified", {"result": result})


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
    clock: Callable[[], datetime] | None = None,
    max_evidence_age_s: float = evidence.DEFAULT_MAX_AGE_SECONDS,
    target_known: Callable[[dict], bool] | None = None,
    action_allowed: Callable[[dict], bool] | None = None,
    max_approval_age_s: float = DEFAULT_MAX_APPROVAL_AGE_SECONDS,
    executing_timeout_s: float = DEFAULT_EXECUTING_TIMEOUT_SECONDS,
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
    not_actuator = node_local.actuator_refusal()
    if not_actuator is not None:
        emit(f"dispatch: pass refused, {not_actuator}")
        return []

    def body() -> list[DispatchOutcome]:
        return _dispatch_pass_body(
            paths,
            ledger,
            itil,
            adapters=adapters,
            problem_types=problem_types,
            apply_fn=apply_fn,
            rollback_fn=rollback_fn,
            execution_state=execution_state,
            decisions_dir=decisions_dir,
            now_iso=now_iso,
            actor=actor,
            emit=emit,
            clock=clock,
            max_evidence_age_s=max_evidence_age_s,
            target_known=target_known,
            action_allowed=action_allowed,
            max_approval_age_s=max_approval_age_s,
            executing_timeout_s=executing_timeout_s,
        )

    # Single-flight, shared with ``loop.run_once``: a dispatch pass (``skoperator
    # honor-pending``) must not overlap a loop pass or another dispatch pass. Raises
    # ``RuntimeError("another Atlas operator pass is active")`` when one is running.
    if execution_state is not None:
        with execution_state.single_flight():
            return body()
    return body()


def _dispatch_pass_body(
    paths: FleetPaths,
    ledger: action_ledger.ActionLedger,
    itil: Any,
    *,
    adapters: dict[str, Callable[..., dict]],
    problem_types: set[str],
    apply_fn: Callable[[dict, dict], Any],
    rollback_fn: Callable[[dict, dict, Any], Any] | None,
    execution_state: safety.ExecutionState | None,
    decisions_dir: str | None,
    now_iso: str,
    actor: str,
    emit: Callable[[str], Any],
    clock: Callable[[], datetime] | None,
    max_evidence_age_s: float,
    target_known: Callable[[dict], bool] | None,
    action_allowed: Callable[[dict], bool] | None,
    max_approval_age_s: float,
    executing_timeout_s: float,
) -> list[DispatchOutcome]:
    outcomes: list[DispatchOutcome] = []
    # First, leftovers of a killed loop, then anything a crash stranded mid-run: voided,
    # escalated with an alert, never re-run.
    try:
        void_stale_auto_intents(ledger, itil, clock=clock, actor=actor, emit=emit)
    except Exception as exc:  # noqa: BLE001 - never let the sweep stop the dispatch pass
        emit(f"dispatch: stale auto-lane sweep failed: {exc}")
    try:
        outcomes.extend(
            escalate_stranded(
                ledger,
                itil,
                now_iso=now_iso,
                clock=clock,
                actor=actor,
                decisions_dir=decisions_dir,
                emit=emit,
                executing_timeout_s=executing_timeout_s,
            )
        )
    except Exception as exc:  # noqa: BLE001 - never let the sweep stop the dispatch pass
        emit(f"dispatch: stranded-intent sweep failed: {exc}")
    for intent_id in ledger.list_intents(state=action_ledger.ActionState.PROPOSED):
        try:
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
                    clock=clock,
                    max_evidence_age_s=max_evidence_age_s,
                    target_known=target_known,
                    action_allowed=action_allowed,
                    max_approval_age_s=max_approval_age_s,
                )
            )
        except Exception as exc:  # noqa: BLE001 - one bad intent never crashes the pass
            emit(f"dispatch: error dispatching {intent_id}: {exc}")
            outcomes.append(DispatchOutcome(intent_id, f"error: {exc}"))
    return outcomes


__all__ = [
    "UnauthorizedDecisionError",
    "DispatchOutcome",
    "condition_firing",
    "verify_postcondition",
    "resolve_decision",
    "dispatch_intent",
    "run_dispatch_pass",
    "is_freeze_refusal",
    "escalate_stranded",
    "void_stale_auto_intents",
    "close_episode",
    "approval_refusal",
    "human_approval_times",
    "reobserve",
    "stored_lane",
    "change_human_approved",
    "change_bound_to",
    "change_binding_tag",
    "stored_evidence",
    "void_proposed",
]
