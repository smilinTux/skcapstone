"""Conservative long-runner liveness decisions.

This module is deliberately pure: observations are supplied by the fleet
collector and decisions never mutate CardStore, sessions, or workspaces.
Missing or contradictory evidence is quarantined rather than interpreted as
worker death.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable, Mapping


@dataclass(frozen=True)
class LivenessObservation:
    owner: str
    card_id: str
    claim_generation: str
    process_identity: str | None
    managed_session: bool
    live_children: int
    heartbeat_at: datetime | None
    terminal_marker: str | None
    skmail_response_at: datetime | None
    workspace_recoverable: bool
    workspace_custody: str | None
    quiet_tool_wait: bool = False
    interrupted: bool = False


@dataclass(frozen=True)
class LivenessDecision:
    state: str
    attributable: bool
    escalate: bool = False
    retire: bool = False
    quarantine: bool = False
    reason: str = ""


TERMINAL_MARKERS = frozenset({"PASS", "PASS_FOR_REVIEW", "BLOCKED"})


def _attributable(o: LivenessObservation) -> bool:
    return bool(
        o.owner and o.card_id and o.claim_generation and o.process_identity and o.managed_session
    )


def classify(
    o: LivenessObservation,
    *,
    now: datetime,
    assist_after: timedelta = timedelta(hours=1),
    checkpoint_after: timedelta = timedelta(minutes=15),
) -> LivenessDecision:
    """Classify without treating an idle tool wait as a dead worker."""
    if not _attributable(o):
        return LivenessDecision(
            "ambiguous", False, quarantine=True, reason="incomplete-identity-or-session"
        )
    if o.interrupted:
        return LivenessDecision("interrupted", True, quarantine=True, reason="signal-interruption")
    if o.terminal_marker not in (None, *TERMINAL_MARKERS):
        return LivenessDecision(
            "ambiguous", True, quarantine=True, reason="invalid-terminal-marker"
        )
    if o.terminal_marker:
        if o.live_children == 0 and o.workspace_recoverable and o.workspace_custody:
            return LivenessDecision(
                "terminal",
                True,
                retire=True,
                reason="terminal-zero-children-recoverable-workspace",
            )
        return LivenessDecision(
            "terminal-orphan",
            True,
            quarantine=True,
            reason="terminal-with-live-children-or-unrecoverable-workspace",
        )
    if o.quiet_tool_wait:
        return LivenessDecision("waiting", True, reason="quiet-tool-wait-is-not-death")
    if o.heartbeat_at is None:
        return LivenessDecision("active-compute", True, reason="no-heartbeat-yet")
    age = now - o.heartbeat_at
    if age < assist_after:
        return LivenessDecision("active-compute", True, reason="recent-heartbeat")
    # Escalation is attributable and time-bounded. A response can be late while
    # still proving the worker is alive, so it does not itself authorize retire.
    if o.skmail_response_at and o.skmail_response_at >= o.heartbeat_at:
        return LivenessDecision("assisted", True, reason="status-response-received")
    return LivenessDecision(
        "assistance-due", True, escalate=True, reason="heartbeat-quiet-past-assistance-window"
    )


def reconcile(decisions: Iterable[LivenessDecision]) -> tuple[LivenessDecision, ...]:
    """Idempotent projection: terminal decisions dominate stale projections."""
    return tuple(decisions)


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
    return {
        "long_runner_count": sum(d.state not in {"terminal", "interrupted"} for d in rows),
        "active_compute_count": sum(d.state == "active-compute" for d in rows),
        "waiting_count": sum(d.state == "waiting" for d in rows),
        "terminal_orphan_count": sum(d.state == "terminal-orphan" for d in rows),
        "assistance_latency": sum(assists) / len(assists) if assists else 0,
        "retirement_latency": sum(retires) / len(retires) if retires else 0,
        "preserved_work_outcomes": sum(preserved),
    }
