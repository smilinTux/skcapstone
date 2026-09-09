"""Fail-closed long-runner decisions and their bounded runtime application."""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable, Mapping


@dataclass(frozen=True)
class LivenessObservation:
    host: str
    observer_host: str
    observed_at: datetime
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
    unit: str | None = None
    pid: int | None = None
    process_tree: tuple[int, ...] = ()
    cgroup: str | None = None
    process_observed_at: datetime | None = None
    cgroup_observed_at: datetime | None = None
    beat_id: str | None = None
    workspace_path: str | None = None
    workspace_repository: str | None = None
    workspace_head: str | None = None
    workspace_custody_at: datetime | None = None
    workspace_custody_sha256: str | None = None


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


@dataclass(frozen=True)
class RetirementReceipt:
    """Exact immutable fence passed to the only retirement callback."""

    host: str
    observer_host: str
    observed_at: datetime
    unit: str
    pid: int
    process_tree: tuple[int, ...]
    cgroup: str
    process_observed_at: datetime
    cgroup_observed_at: datetime
    owner: str
    card_id: str
    claim_generation: str
    beat_id: str
    heartbeat_at: datetime
    workspace_custody: str
    workspace_path: str
    workspace_repository: str
    workspace_head: str
    workspace_custody_at: datetime
    workspace_custody_sha256: str
    terminal_marker: str
    terminal_at: datetime


@dataclass(frozen=True)
class RuntimeActions:
    request_assistance: Callable[[AssistanceRequest], None]
    reconcile_projection: Callable[[WorkerProjection], None]
    publish_metrics: Callable[[Mapping[str, float | int]], None]
    retire: Callable[[RetirementReceipt], None]


@dataclass(frozen=True)
class CycleResult:
    decisions: tuple[LivenessDecision, ...]
    projections: tuple[WorkerProjection, ...]
    receipts: tuple[RetirementReceipt, ...]
    metric_values: Mapping[str, float | int]


TERMINAL_MARKERS = frozenset({"PASS", "PASS_FOR_REVIEW", "BLOCKED"})
TERMINAL_STATES = frozenset({"terminal", "retirement-ready"})
EVIDENCE_FRESHNESS = timedelta(minutes=2)


def _attributable(o: LivenessObservation) -> bool:
    return bool(
        o.host
        and o.observer_host == o.host
        and o.observed_at
        and o.owner
        and o.card_id
        and o.claim_generation
        and o.process_identity
        and o.session_id
        and o.managed_session
    )


def _retirement_receipt(o: LivenessObservation) -> RetirementReceipt | None:
    """Return a complete exact fence, or None when any proof is absent."""
    if not (
        o.host
        and o.observer_host == o.host
        and o.observed_at
        and o.unit
        and o.pid
        and o.process_tree
        and o.pid in o.process_tree
        and str(o.pid) in (o.process_identity or "")
        and o.cgroup
        and o.unit in o.cgroup
        and o.process_observed_at
        and o.cgroup_observed_at
        and o.beat_id
        and o.heartbeat_at
        and o.workspace_custody
        and o.workspace_path
        and Path(o.workspace_path).is_absolute()
        and o.workspace_repository
        and o.workspace_head
        and o.workspace_custody_at
        and o.workspace_custody_sha256
        and len(o.workspace_custody_sha256) == 64
        and all(character in "0123456789abcdef" for character in o.workspace_custody_sha256)
        and o.terminal_marker
        and o.terminal_at
    ):
        return None
    return RetirementReceipt(
        o.host,
        o.observer_host,
        o.observed_at,
        o.unit,
        o.pid,
        o.process_tree,
        o.cgroup,
        o.process_observed_at,
        o.cgroup_observed_at,
        o.owner,
        o.card_id,
        o.claim_generation,
        o.beat_id,
        o.heartbeat_at,
        o.workspace_custody,
        o.workspace_path,
        o.workspace_repository,
        o.workspace_head,
        o.workspace_custody_at,
        o.workspace_custody_sha256,
        o.terminal_marker,
        o.terminal_at,
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
    evidence_times = (
        o.observed_at,
        o.process_observed_at,
        o.cgroup_observed_at,
        o.workspace_custody_at,
    )
    if any(
        value is None or value > now or now - value > EVIDENCE_FRESHNESS
        for value in evidence_times
    ):
        return _decision(
            o,
            "ambiguous",
            True,
            quarantine=True,
            preserve_workspace=True,
            reason="host-local-evidence-missing-stale-or-future",
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
        if (
            o.live_children == 0
            and o.cgroup_processes == 0
            and custody_safe
            and commits_safe
            and _retirement_receipt(o) is not None
        ):
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


def run_cycle(
    observations: Iterable[LivenessObservation],
    projections: Iterable[WorkerProjection],
    *,
    now: datetime,
    actions: RuntimeActions,
) -> CycleResult:
    """Apply one decision set to every real path through explicit adapters.

    Retirement is last and receives the same exact observation that produced
    the decision. Any incomplete provenance is already quarantined by
    ``classify`` and can never reach the callback.
    """
    observed = tuple(observations)
    decisions = tuple(classify(row, now=now) for row in observed)
    keyed = {(d.owner, d.card_id, d.claim_generation): d for d in decisions}
    projected = reconcile(projections, keyed)
    for projection in projected:
        actions.reconcile_projection(projection)
    for decision in decisions:
        if decision.assistance_request and decision.state == "assistance-due":
            actions.request_assistance(decision.assistance_request)
    assistance_latencies = tuple(
        max(0.0, (now - activity).total_seconds())
        for observation, decision in zip(observed, decisions, strict=True)
        if decision.assistance_request and (activity := _latest_activity(observation)) is not None
    )
    retirement_latencies = tuple(
        max(0.0, (now - observation.terminal_at).total_seconds())
        for observation, decision in zip(observed, decisions, strict=True)
        if decision.retire and observation.terminal_at is not None
    )
    values = metrics(
        decisions,
        assistance_latencies=assistance_latencies,
        retirement_latencies=retirement_latencies,
        preserved_work=(decision.preserve_workspace for decision in decisions),
    )
    actions.publish_metrics(values)
    receipts = []
    for observation, decision in zip(observed, decisions, strict=True):
        if not decision.retire:
            continue
        receipt = _retirement_receipt(observation)
        if receipt is None:
            raise RuntimeError("retirement decision lost its exact evidence fence")
        actions.retire(receipt)
        receipts.append(receipt)
    return CycleResult(decisions, projected, tuple(receipts), values)


class SQLiteReceiptJournal:
    """Small concurrent-writer-safe append-only retirement receipt journal."""

    def __init__(self, path: Path, *, timeout: float = 10.0) -> None:
        self.path = path
        self.timeout = timeout

    def append(self, receipt: RetirementReceipt) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                with sqlite3.connect(self.path, timeout=self.timeout) as db:
                    db.execute(f"PRAGMA busy_timeout={int(self.timeout * 1000)}")
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute(
                        "CREATE TABLE IF NOT EXISTS retirement_receipts "
                        "(receipt_key TEXT PRIMARY KEY, payload TEXT NOT NULL)"
                    )
                    key = ":".join(
                        (receipt.host, receipt.unit, str(receipt.pid), receipt.claim_generation)
                    )
                    payload = json.dumps(asdict(receipt), default=str, sort_keys=True)
                    db.execute(
                        "INSERT OR IGNORE INTO retirement_receipts VALUES (?, ?)",
                        (key, payload),
                    )
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
