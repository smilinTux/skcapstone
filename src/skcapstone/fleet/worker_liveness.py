"""Pure, fail-closed long-runner liveness and retirement planning.

Callers supply correlated observations. This module emits decisions and
projection updates only. It never sends mail, mutates CardStore, terminates a
session, or changes a workspace.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Iterable, Mapping


@dataclass(frozen=True)
class LivenessObservation:
    owner: str
    card_id: str
    claim_generation: str
    process_identity: str | None
    session_id: str | None
    managed_session: bool
    process_alive: bool | None
    session_alive: bool | None
    live_children: int
    child_activity_at: datetime | None
    heartbeat_at: datetime | None
    terminal_marker: str | None
    terminal_at: datetime | None
    skmail_response_at: datetime | None
    assistance_requested_at: datetime | None
    workspace_recoverable: bool
    workspace_custody: str | None
    unpushed_commits: tuple[str, ...] = ()
    unpushed_commits_preserved: bool = False
    quiet_tool_wait: bool = False
    interrupted: bool = False
    cleanup_failed: bool = False
    claim_active: bool = True
    cpu_percent: float = 0.0
    current_claim_generation: str | None = None
    cgroup_processes: int | None = None


@dataclass(frozen=True)
class AssistanceRequest:
    owner: str
    card_id: str
    claim_generation: str
    session_id: str
    requested_at: datetime
    checkpoint_due_at: datetime


@dataclass(frozen=True)
class LivenessDecision:
    owner: str
    card_id: str
    claim_generation: str
    state: str
    attributable: bool
    assistance_request: AssistanceRequest | None = None
    retire: bool = False
    quarantine: bool = False
    preserve_workspace: bool = False
    reason: str = ""


@dataclass(frozen=True)
class WorkerProjection:
    owner: str
    card_id: str
    claim_generation: str
    state: str


TERMINAL_MARKERS = frozenset({"PASS", "PASS_FOR_REVIEW", "BLOCKED"})
TERMINAL_STATES = frozenset({"terminal", "retirement-ready"})


def _attributable(o: LivenessObservation) -> bool:
    return bool(
        o.owner
        and o.card_id
        and o.claim_generation
        and o.process_identity
        and o.session_id
        and o.managed_session
    )


def _decision(
    o: LivenessObservation, state: str, attributable: bool, **changes: object
) -> LivenessDecision:
    return LivenessDecision(o.owner, o.card_id, o.claim_generation, state, attributable, **changes)


def _latest_activity(o: LivenessObservation) -> datetime | None:
    values = tuple(value for value in (o.heartbeat_at, o.child_activity_at) if value)
    return max(values, default=None)


def classify(
    o: LivenessObservation,
    *,
    now: datetime,
    assist_after: timedelta = timedelta(hours=1),
    checkpoint_after: timedelta = timedelta(minutes=15),
) -> LivenessDecision:
    """Classify one exact worker generation without performing actuation."""
    attributable = _attributable(o)
    if not attributable:
        return _decision(
            o,
            "ambiguous",
            False,
            quarantine=True,
            preserve_workspace=True,
            reason="incomplete-identity-or-session",
        )
    if not o.current_claim_generation or o.current_claim_generation != o.claim_generation:
        return _decision(
            o,
            "stale-claim",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="claim-generation-not-current",
        )
    if o.cgroup_processes is None or o.cgroup_processes < 0:
        return _decision(
            o,
            "ambiguous",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="cgroup-process-state-unknown",
        )
    if o.live_children < 0 or o.cleanup_failed:
        return _decision(
            o,
            "cleanup-failed",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="invalid-child-count" if o.live_children < 0 else "partial-cleanup-failure",
        )
    if o.interrupted:
        return _decision(
            o,
            "interrupted",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="signal-interruption",
        )
    if o.process_alive is None or o.session_alive is None:
        return _decision(
            o,
            "ambiguous",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="process-or-session-state-unknown",
        )
    if o.terminal_marker not in (None, *TERMINAL_MARKERS):
        return _decision(
            o,
            "ambiguous",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="invalid-terminal-marker",
        )
    if o.terminal_marker:
        if o.terminal_at is None:
            return _decision(
                o,
                "terminal-orphan",
                True,
                quarantine=True,
                preserve_workspace=True,
                reason="terminal-timestamp-missing",
            )
        custody_safe = bool(o.workspace_recoverable and o.workspace_custody)
        commits_safe = not o.unpushed_commits or o.unpushed_commits_preserved
        if o.live_children == 0 and o.cgroup_processes == 0 and custody_safe and commits_safe:
            return _decision(
                o,
                "retirement-ready",
                True,
                retire=True,
                preserve_workspace=bool(o.unpushed_commits),
                reason="terminal-zero-children-and-cgroup-recoverable-workspace",
            )
        return _decision(
            o,
            "terminal-orphan",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="terminal-retirement-proof-incomplete",
        )
    if not o.claim_active:
        return _decision(
            o,
            "stale-claim",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="claim-generation-not-active",
        )
    if not o.process_alive:
        return _decision(
            o,
            "dead-process",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="worker-process-absent-without-terminal-evidence",
        )
    if o.session_alive is False:
        return _decision(
            o,
            "dead-pane",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="managed-session-absent-without-terminal-evidence",
        )
    if o.quiet_tool_wait:
        return _decision(o, "waiting", True, reason="quiet-tool-wait-is-not-death")

    activity_at = _latest_activity(o)
    if activity_at is None or now - activity_at < assist_after:
        return _decision(o, "active-compute", True, reason="recent-or-unbounded-activity")
    if o.skmail_response_at and (
        o.assistance_requested_at is None or o.skmail_response_at >= o.assistance_requested_at
    ):
        return _decision(o, "assisted", True, reason="status-response-received")
    requested_at = o.assistance_requested_at or now
    checkpoint_due_at = requested_at + checkpoint_after
    request = AssistanceRequest(
        o.owner,
        o.card_id,
        o.claim_generation,
        o.session_id or "",
        requested_at,
        checkpoint_due_at,
    )
    if o.assistance_requested_at and now >= checkpoint_due_at:
        return _decision(
            o,
            "checkpoint-missed",
            True,
            assistance_request=request,
            quarantine=True,
            preserve_workspace=True,
            reason="structured-status-request-unanswered",
        )
    return _decision(
        o,
        "assistance-due" if o.assistance_requested_at is None else "checkpoint-pending",
        True,
        assistance_request=request,
        reason="heartbeat-and-child-activity-quiet-past-assistance-window",
    )


def reconcile(
    projections: Iterable[WorkerProjection],
    decisions: Mapping[tuple[str, str, str], LivenessDecision],
) -> tuple[WorkerProjection, ...]:
    """Replace stale ready or active projections with exact terminal truth."""
    reconciled = []
    for projection in projections:
        key = (projection.owner, projection.card_id, projection.claim_generation)
        decision = decisions.get(key)
        if decision and decision.state in TERMINAL_STATES:
            projection = replace(projection, state="terminal")
        reconciled.append(projection)
    return tuple(reconciled)


def metrics(
    decisions: Iterable[LivenessDecision],
    *,
    assistance_latencies: Iterable[float] = (),
    retirement_latencies: Iterable[float] = (),
    preserved_work: Iterable[bool] = (),
) -> Mapping[str, float | int]:
    rows = tuple(decisions)
    assists = tuple(assistance_latencies)
    retires = tuple(retirement_latencies)
    preserved = tuple(preserved_work)
    terminal_states = TERMINAL_STATES | {"interrupted"}
    return {
        "long_runner_count": sum(d.state not in terminal_states for d in rows),
        "active_compute_count": sum(d.state == "active-compute" for d in rows),
        "waiting_count": sum(d.state == "waiting" for d in rows),
        "terminal_orphan_count": sum(d.state == "terminal-orphan" for d in rows),
        "assistance_latency": sum(assists) / len(assists) if assists else 0,
        "retirement_latency": sum(retires) / len(retires) if retires else 0,
        "preserved_work_outcomes": sum(preserved),
    }
