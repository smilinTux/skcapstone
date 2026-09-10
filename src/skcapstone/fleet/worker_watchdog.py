"""Pure worker lease and attribution checks for fleet monitoring.

No lease state is derived from beat evidence alone; beats only corroborate
preconditioned claim events.

This module observes and classifies only.  It never releases a claim, reaps a
worker, or terminates a process.  Beat absence or cross-host beat age never
sets ``releasable``; a caller needs separate host-local negative proof plus an
exact owner and claim-revision fence before any actuation.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping

WORKER_STATES = frozenset(
    {
        "running",
        "stalled",
        "timed-out",
        "exited",
        "claim-mismatch",
        "transport-stale",
        "worker-stale",
        "telemetry-fault",
    }
)
MEASURED_CROSS_HOST_P95_S = 292.336
# Host-local notice only.  Cross-host evidence past this is transport-stale,
# never dead (measured p95 is 292.336s).
HOST_LOCAL_BEAT_NOTICE_S = 120.0
# Default above measured cross-host p95 with margin.
DEFAULT_HEARTBEAT_TIMEOUT_S = 600.0
DEFAULT_TRANSPORT_TIMEOUT_S = 600.0


@dataclass(frozen=True)
class WorkerObservation:
    """One read-only worker and claim snapshot."""

    owner: str
    claim_owner: str | None
    claim_revision: str | None
    expected_claim_revision: str | None
    process_alive: bool
    session_alive: bool
    heartbeat_at: str | None
    lease_expires_at: str | None
    session_id: str | None = None
    card_id: str | None = None
    host: str | None = None
    lane: str | None = None
    model: str | None = None
    heartbeat_source_host: str | None = None
    heartbeat_received_at: str | None = None
    observer_host: str | None = None
    unit_active: bool | None = None


@dataclass(frozen=True)
class WorkerClassification:
    """A bounded, auditable worker state."""

    owner: str
    state: str
    reason: str
    claim_revision: str | None = None
    expected_claim_revision: str | None = None
    session_id: str | None = None
    card_id: str | None = None
    host: str | None = None
    lane: str | None = None
    model: str | None = None
    releasable: bool = False


@dataclass(frozen=True)
class WorkerGeneration:
    """A duplicate execution identity that must not be selected implicitly."""

    owner: str
    host: str
    card_id: str
    claim_revisions: tuple[str, ...]
    count: int


@dataclass(frozen=True)
class StartupObservation:
    """Bounded startup proof for one exact worker generation.

    A process existing is not evidence that it started useful work.  The
    scheduler may use this result for diagnostics, but any actuation still
    requires the exact claim fence checked by :func:`classify_worker`.
    """

    owner: str
    card_id: str
    session_id: str
    claim_revision: str
    expected_claim_revision: str
    heartbeat_seen: bool
    executable_evidence_seen: bool
    heartbeat_at: str | None = None
    executable_evidence: Mapping[str, object] | None = None
    process_alive: bool | None = None
    session_alive: bool | None = None


def classify_startup(
    observation: StartupObservation,
    *,
    now: datetime | None = None,
    heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
) -> str:
    """Classify startup without treating a live process as useful work.

    Both an attributable heartbeat and executable evidence are required.  A
    missing or mismatched identity is fail-closed and never a release signal.
    """
    if not all(
        (
            observation.owner,
            observation.card_id,
            observation.session_id,
            observation.claim_revision,
            observation.expected_claim_revision,
        )
    ):
        return "startup-invalid-identity"
    if observation.claim_revision != observation.expected_claim_revision:
        return "startup-claim-mismatch"
    if not observation.heartbeat_seen or not observation.heartbeat_at:
        return "startup-heartbeat-missing"
    heartbeat = _parse_time(observation.heartbeat_at)
    if heartbeat is None:
        return "startup-heartbeat-malformed"
    if now is not None:
        age = (now.astimezone(timezone.utc) - heartbeat).total_seconds()
        if age < 0:
            return "startup-heartbeat-clock-skew"
        if age > heartbeat_timeout_s:
            return "startup-heartbeat-stale"
    if not observation.executable_evidence_seen:
        return "startup-evidence-missing"
    evidence = observation.executable_evidence
    if not isinstance(evidence, Mapping):
        return "startup-evidence-malformed"
    expected = {
        "owner": observation.owner,
        "card_id": observation.card_id,
        "session_id": observation.session_id,
        "claim_revision": observation.claim_revision,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        return "startup-evidence-mismatch"
    if evidence.get("kind") != "executable-work":
        return "startup-evidence-malformed"
    return "startup-ready"


def startup_actuation_fenced(
    observation: StartupObservation,
    *,
    owner: str,
    claim_revision: str,
    now: datetime,
    heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
) -> bool:
    """Return whether a selector may offer an exact startup release.

    This is deliberately an interface, not an actuator.  It requires a
    second, host-local observation that both process and session are absent,
    plus exact owner and claim-revision fencing.  Missing observations never
    become release permission.
    """
    if owner != observation.owner or claim_revision != observation.claim_revision:
        return False
    if observation.process_alive is not False or observation.session_alive is not False:
        return False
    state = classify_startup(observation, now=now, heartbeat_timeout_s=heartbeat_timeout_s)
    return state in {
        "startup-heartbeat-stale",
        "startup-heartbeat-missing",
        "startup-heartbeat-malformed",
        "startup-evidence-missing",
        "startup-evidence-malformed",
        "startup-evidence-mismatch",
    }


@dataclass(frozen=True)
class GatewayRequest:
    """Value-free gateway request attribution fields."""

    request_id: str
    agent_id: str | None
    session_id: str | None
    card_id: str | None
    claim_revision: str | None
    host: str | None
    lane: str | None
    model: str | None


@dataclass(frozen=True)
class RequestAttribution:
    """Gateway request joined to a worker, without request content."""

    request_id: str
    state: str
    worker_owner: str | None
    missing: tuple[str, ...]
    reason: str = ""


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _identity(observation: WorkerObservation) -> tuple:
    return (
        observation.claim_revision,
        observation.expected_claim_revision,
        observation.session_id,
        observation.card_id,
        observation.host,
        observation.lane,
        observation.model,
    )


def _result(
    observation: WorkerObservation,
    state: str,
    reason: str,
    *,
    releasable: bool = False,
) -> WorkerClassification:
    return WorkerClassification(
        observation.owner,
        state,
        reason,
        *_identity(observation),
        releasable=releasable,
    )


def _is_cross_host(observation: WorkerObservation) -> bool:
    """True when beat origin differs from the classifying observer host."""
    if not observation.observer_host or not observation.heartbeat_source_host:
        return False
    return observation.observer_host != observation.heartbeat_source_host


def classify_worker(
    observation: WorkerObservation,
    *,
    now: datetime,
    heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
    transport_timeout_s: float = DEFAULT_TRANSPORT_TIMEOUT_S,
) -> WorkerClassification:
    """Classify one worker using exact claim fencing and bounded freshness.

    Claim identity is checked before liveness.  A live process with a missing
    or changed claim is therefore ``claim-mismatch`` and is never silently
    treated as healthy.  Cross-host beat age is ``transport-stale``, never
    dead.  Host-local beat age past timeout is ``worker-stale``.  Beat absence
    while a host-local unit is active is ``telemetry-fault`` and never
    releasable.  This function has no mutation side effects.
    """
    del transport_timeout_s  # kept for call-site compatibility; age uses source
    if not observation.owner or not observation.claim_owner:
        return _result(observation, "claim-mismatch", "owner-missing")
    if observation.claim_owner != observation.owner:
        return _result(observation, "claim-mismatch", "owner-mismatch")
    if not observation.claim_revision or not observation.expected_claim_revision:
        return _result(observation, "claim-mismatch", "claim-revision-missing")
    if observation.claim_revision != observation.expected_claim_revision:
        return _result(observation, "claim-mismatch", "claim-revision-mismatch")
    if not observation.process_alive and not observation.session_alive:
        return _result(observation, "exited", "no-process-or-session")
    expiry = _parse_time(observation.lease_expires_at)
    if expiry is not None and now.astimezone(timezone.utc) >= expiry:
        return _result(observation, "timed-out", "lease-expired")

    # Beat absence alone never actuates.  Active unit => telemetry fault.
    if observation.heartbeat_at is None:
        if observation.unit_active:
            return _result(
                observation,
                "telemetry-fault",
                "beat-absent-unit-active",
                releasable=False,
            )
        return _result(
            observation,
            "stalled",
            "heartbeat-absent",
            releasable=False,
        )

    heartbeat = _parse_time(observation.heartbeat_at)
    if heartbeat is None:
        return _result(observation, "stalled", "heartbeat-unparseable", releasable=False)
    current = now.astimezone(timezone.utc)
    source_age = (current - heartbeat).total_seconds()
    if source_age < 0:
        return _result(observation, "stalled", "heartbeat-clock-skew", releasable=False)

    if _is_cross_host(observation):
        # Refuse worker-death evaluation on cross-host evidence.
        if source_age > HOST_LOCAL_BEAT_NOTICE_S:
            return _result(
                observation,
                "transport-stale",
                "transport-stale",
                releasable=False,
            )
        return _result(observation, "running", "heartbeat-fresh")

    if source_age > heartbeat_timeout_s:
        return _result(observation, "worker-stale", "worker-stale", releasable=False)
    return _result(observation, "running", "heartbeat-fresh")


def duplicate_worker_owners(
    observations: Iterable[WorkerObservation],
) -> tuple[WorkerGeneration, ...]:
    """Return duplicate execution generations, retaining the legacy name.

    Missing generation fields are omitted because an ambiguous record cannot
    safely identify a duplicate.  This helper never chooses an authority.
    """
    groups: dict[tuple[str, str, str], list[str]] = {}
    for observation in observations:
        fields = (observation.owner, observation.host, observation.card_id)
        if all(fields) and observation.claim_revision:
            groups.setdefault(fields, []).append(observation.claim_revision)
    return tuple(
        WorkerGeneration(owner, host, card, tuple(sorted(revisions)), len(revisions))
        for (owner, host, card), revisions in sorted(groups.items())
        if len(revisions) > 1
    )


def summarize_workers(
    observations: Iterable[WorkerObservation],
    *,
    now: datetime,
    heartbeat_timeout_s: float = DEFAULT_HEARTBEAT_TIMEOUT_S,
    transport_timeout_s: float = DEFAULT_TRANSPORT_TIMEOUT_S,
) -> dict[str, int]:
    """Return stable counts for every worker state, including zeroes."""
    counts = {state: 0 for state in sorted(WORKER_STATES)}
    for observation in observations:
        state = classify_worker(
            observation,
            now=now,
            heartbeat_timeout_s=heartbeat_timeout_s,
            transport_timeout_s=transport_timeout_s,
        ).state
        counts[state] += 1
    return counts


def correlate_request(
    request: GatewayRequest,
    workers: Mapping[str, WorkerClassification],
) -> RequestAttribution:
    """Join a gateway request to its worker identity, reporting gaps.

    ``workers`` is keyed by agent ID.  Missing attribution is explicit and
    never inferred from model, host, or request activity alone.
    """
    fields = {
        "agent_id": request.agent_id,
        "session_id": request.session_id,
        "card_id": request.card_id,
        "claim_revision": request.claim_revision,
        "host": request.host,
        "lane": request.lane,
        "model": request.model,
    }
    missing = tuple(name for name, value in fields.items() if not value)
    worker = workers.get(request.agent_id or "")
    if worker is None:
        return RequestAttribution(
            request.request_id, "unmatched", None, missing, "worker-unmatched"
        )
    if missing:
        return RequestAttribution(
            request.request_id,
            "incomplete",
            worker.owner,
            missing,
            "attribution-incomplete",
        )
    if (
        not worker.owner
        or not worker.claim_revision
        or not worker.expected_claim_revision
        or worker.claim_revision != worker.expected_claim_revision
    ):
        return RequestAttribution(
            request.request_id,
            "claim-mismatch",
            worker.owner or None,
            (),
            "claim-revision-mismatch",
        )
    if request.claim_revision != worker.claim_revision:
        return RequestAttribution(
            request.request_id,
            "claim-mismatch",
            worker.owner,
            (),
            "claim-revision-mismatch",
        )
    expected = {
        "agent_id": worker.owner,
        "session_id": worker.session_id,
        "card_id": worker.card_id,
        "claim_revision": worker.claim_revision,
        "host": worker.host,
        "lane": worker.lane,
        "model": worker.model,
    }
    for field, value in expected.items():
        if not value:
            return RequestAttribution(
                request.request_id,
                "incomplete",
                worker.owner,
                (field,),
                "worker-identity-incomplete",
            )
        if fields[field] != value:
            return RequestAttribution(
                request.request_id,
                "claim-mismatch" if field == "claim_revision" else "unmatched",
                worker.owner,
                (),
                "identity-mismatch:%s" % field,
            )
    return RequestAttribution(
        request.request_id, worker.state, worker.owner, (), "attribution-valid"
    )
