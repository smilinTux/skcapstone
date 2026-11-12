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
DEFAULT_PROGRESS_TIMEOUT_S = 900.0


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


@dataclass(frozen=True)
class ProgressObservation:
    """Bounded progress proof for one exact worker generation."""

    owner: str
    card_id: str
    session_id: str
    claim_revision: str
    expected_claim_revision: str
    progress_at: str | None
    terminal_evidence_seen: bool = False
    process_alive: bool | None = None
    session_alive: bool | None = None
    #: Bytes in this worker's largest agent transcript, or None when unknown.
    #: None is NOT zero: an unmeasured transcript must never actuate.
    transcript_bytes: int | None = None


def classify_progress(
    observation: ProgressObservation,
    *,
    now: datetime,
    progress_timeout_s: float = DEFAULT_PROGRESS_TIMEOUT_S,
) -> str:
    """Classify executable progress without performing any actuation."""
    if not all(
        (
            observation.owner,
            observation.card_id,
            observation.session_id,
            observation.claim_revision,
            observation.expected_claim_revision,
        )
    ):
        return "progress-invalid-identity"
    if observation.claim_revision != observation.expected_claim_revision:
        return "progress-claim-mismatch"
    if observation.process_alive is False and observation.session_alive is False:
        return "progress-exited"
    if observation.terminal_evidence_seen:
        return "progress-terminal-evidence"
    if not observation.progress_at:
        return "progress-missing"
    progress = _parse_time(observation.progress_at)
    if progress is None:
        return "progress-malformed"
    age = (now.astimezone(timezone.utc) - progress).total_seconds()
    if age < 0:
        return "progress-clock-skew"
    if age > progress_timeout_s:
        return "progress-stale"
    return "progress-fresh"


# The ACTUATING deadline, which is deliberately not the classifying one.
#
# DEFAULT_PROGRESS_TIMEOUT_S (900s) is where a worker stops looking fresh. It
# is a reporting threshold and always was. This is the deadline past which
# silence stops being consistent with working.
#
# Measured: 158 WORKER_PROGRESS records, 12 distinct owners, 5 chi hosts,
# 20260918T193007Z to 20260919T052500Z (the whole life of the report-only
# pass). Full write-up in docs/fleet/wedged-worker-actuation.md.
#
#   state              n    min       p50      p95      max
#   progress-fresh    38      2s       14s     132s     225s
#   progress-stale    10  53,545s  55,795s  57,445s  57,445s
#   progress-missing 110       no workspace write at all
#
# The two populations are cleanly BIMODAL: not one observation landed
# anywhere in 225s..53,545s. That gap, not a percentile, is what makes a
# threshold defensible here, and 14400s sits inside it with margin on both
# sides:
#
#   worst gap on a worker that was genuinely working   628s   (2026-09-18 s18)
#   worst progress-fresh observation                   225s   (this window)
#   -> the threshold is 23x the former, 64x the latter
#   lowest progress-stale observation ever seen     53,545s
#   -> the threshold is 3.7x BELOW it
#
# Genuinely-working workers this threshold would have killed in the measured
# window: ZERO. Exactly one owner was ever past it
# (pi-qwen-chiap01-34115541, 10 observations, 53,545s to 57,445s) and it was
# not working: its unit was crashlooping with 0-byte logs, and `scanned=1587`
# never changed across the whole stale run, so not one file was added or
# touched. Its later return to progress-fresh was a NEW generation's launch
# touching the workspace, not the old process resuming.
#
# RE-DERIVED 2026-09-19 against the agent transcript, and lowered 14400 ->
# 7200, because the signal underneath it changed.
#
# The bimodality above was partly an artifact of the source. Workspace mtime
# counted DIRECTORY mtimes, and an ordinary `git status` bumps `.git` without
# writing any file, so the "progress-fresh" population was measuring git
# reads. On 2026-09-19 six chi workers that had produced zero edit/write tool
# calls, zero commits and zero dirty files across 2.5h to 6h all reported
# progress_age_s=11..26 and classified wedge-progressing. No value of this
# constant could have fired, because the oracle never went stale.
#
# The reporter now reads the agent's own session transcript instead
# (_session_progress_at), and that signal IS sharply bimodal. Measured over
# 2,754 fleet sessions with >=20 events, all four chi worker hosts, the
# longest silence inside a session whose worker went on to keep working:
#
#   p50   p90   p95    p99    p99.9    max
#    45s  184s  300s   513s   1,350s   2,558s
#
#   sessions with a max gap above 3,600s:      0 / 2,754
#   sessions with a max gap above 1 hour:      0 / 2,754
#   the 139ec63d incident sat silent for  22,680s (6h18m)
#
# The empty band is 2,558s .. 22,680s. 7200 sits inside it, and two
# independent bounds put it there rather than a percentile:
#
#   worst observed healthy silence                 2,558s -> 2.8x below
#   largest bash tool timeout ever issued by any
#     worker (a full `pytest` run, and the longest
#     a single tool call can hold the transcript
#     silent by construction)                      3,600s -> 2.0x below
#   the one known wedge                           22,680s -> 3.2x above
#
# Genuinely-working workers this threshold would have killed, replayed over
# all 2,754 sessions: ZERO. At 3,600s it is also zero, but that equals the
# maximum legal single tool call, so it is not left as the margin. Halving
# 14400 to 7200 also halves the seat loss on the incident it was built for.
#
# This deadline is only applied to source=session-mtime. A workspace-mtime
# reading is reported and never actuated, for two independent reasons.
#
# First, its gap distribution overlaps the healthy one outright: productive
# sessions were measured going up to 29,181s between workspace writes.
#
# Second, and decisively, an entire card class is REQUIRED to write nothing.
# 2,601 of 6,901 folded cards (37.7%), and 20 of the 41 currently in DOING,
# carry the `source-only` label, and their acceptance criteria say so
# outright: "Read-only audit. No edits, commit, push". Their deliverable is
# card evidence and mail, not a diff. Under a workspace-mtime deadline every
# one of them is a kill candidate from the moment it starts, so arming that
# signal would have reaped about half of all active work for complying with
# its own card. The transcript has no such blind spot: an auditor reading the
# tree writes a record per turn like any other worker.
DEFAULT_WEDGE_TIMEOUT_S = 7200.0

# A runaway transcript is the one progress signal that separates working from
# looping, because a looping worker keeps its mtime perfectly fresh.
#
# Measured 2026-09-21, card a81000a2: 9.5 hours holding a codex slot at
# state=progress-fresh with a 165MB transcript containing 4,459 `read`, 3,609
# `bash` and 2,555 `grep` calls against FOUR `edit` calls totalling 520 bytes.
# Not wedged and not idle: looping on exploration, producing nothing, and
# exempt from every existing deadline because it never stopped writing.
#
# 100MB is chosen against the measured distribution, not picked round: across
# chiap02/03/04, 33 of 1,253 worker sessions exceeded 50MB. A healthy worker
# measured 2.5MB at 29 minutes. 100MB sits far above normal work and far below
# the 165MB/282MB/338MB runaways, so it separates them without clipping the
# long tail of legitimately large jobs.
DEFAULT_TRANSCRIPT_LIMIT_BYTES = 100 * 1024 * 1024
TRANSCRIPT_LIMIT_ENV = "SKFLEET_TRANSCRIPT_LIMIT_BYTES"


def transcript_limit_bytes(env=None) -> int:
    """Resolve the runaway-transcript ceiling. A bad override falls back."""
    import os as _os

    source = _os.environ if env is None else env
    raw = str(source.get(TRANSCRIPT_LIMIT_ENV, "")).strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return DEFAULT_TRANSCRIPT_LIMIT_BYTES
        if parsed > 0:
            return parsed
    return DEFAULT_TRANSCRIPT_LIMIT_BYTES


# Widening this set is a deliberate act. "wedge-transcript-runaway" was added
# 2026-09-21 by operator decision after two workers held slots for 9.5 hours
# each while every existing signal reported them healthy.
WEDGE_ACTUATING_STATES = frozenset(
    {"wedge-stale-confirmed", "wedge-absent-confirmed", "wedge-transcript-runaway"}
)


def classify_wedge(
    observation: ProgressObservation,
    *,
    now: datetime,
    claim_age_s: float | None,
    receipt_local: bool,
    progress_timeout_s: float = DEFAULT_PROGRESS_TIMEOUT_S,
    wedge_timeout_s: float = DEFAULT_WEDGE_TIMEOUT_S,
) -> str:
    """Classify whether a live-but-silent worker has stopped producing work.

    Observes and classifies only; like everything else in this module it never
    releases, kills or reaps.  Every branch that is not positive proof of
    silence returns a refusal, because the failure this guards against is
    ending a worker that was going to finish.

    A stale workspace and an absent workspace are NOT the same evidence and
    are not treated as such:

    * a stale mtime is a fact about this worker whatever path it was read
      from, so it needs no receipt;
    * an absent workspace is only a fact when the path came from the exact
      generation's admission receipt.  Otherwise the caller inferred the path
      from the owner name and a miss is a measurement failure, which must
      never read as a kill signal.
    """
    state = classify_progress(observation, now=now, progress_timeout_s=progress_timeout_s)
    if state == "progress-fresh":
        # A runaway is fresh BY DEFINITION: it never stops writing, so every
        # deadline in this module exempts it. Size is the evidence elapsed time
        # cannot be, which is why this sits inside the exemption rather than
        # contradicting the rule below.
        #
        # It must stay INSIDE this branch. Checked earlier it would fire on a
        # claim-mismatched or exited observation, actuating against a
        # superseded generation, which is the one thing this module exists to
        # prevent. classify_progress has already cleared identity by here.
        measured = observation.transcript_bytes
        if isinstance(measured, int) and measured > transcript_limit_bytes():
            return "wedge-transcript-runaway"
        # Long is not the same as wedged.  Elapsed time is never evidence.
        return "wedge-progressing"
    if state not in {"progress-stale", "progress-missing"}:
        # progress-exited belongs to the absence reaper, which carries a
        # cross-host quorum gate this path deliberately does not have.
        return "wedge-refused-" + state
    if claim_age_s is None or claim_age_s < 0:
        return "wedge-unmeasured"
    if state == "progress-missing":
        if not receipt_local:
            return "wedge-unmeasured"
        if claim_age_s <= wedge_timeout_s:
            return "wedge-within-margin"
        return "wedge-absent-confirmed"
    progress = _parse_time(observation.progress_at)
    if progress is None:
        return "wedge-refused-progress-malformed"
    age = (now.astimezone(timezone.utc) - progress).total_seconds()
    if age <= wedge_timeout_s:
        return "wedge-within-margin"
    return "wedge-stale-confirmed"


def wedge_actuation_fenced(
    observation: ProgressObservation,
    *,
    owner: str,
    claim_revision: str,
    now: datetime,
    claim_age_s: float | None,
    receipt_local: bool,
    progress_timeout_s: float = DEFAULT_PROGRESS_TIMEOUT_S,
    wedge_timeout_s: float = DEFAULT_WEDGE_TIMEOUT_S,
) -> bool:
    """Return whether a caller may offer an exact wedged-worker release.

    An interface, not an actuator, exactly like
    :func:`startup_actuation_fenced`.  The caller still has to stop the unit,
    release under a CAS on this claim revision, and confirm the release
    against a fresh fold.  A newer generation fails the fence here and is
    never released on an older generation's observation.
    """
    if owner != observation.owner or claim_revision != observation.claim_revision:
        return False
    return (
        classify_wedge(
            observation,
            now=now,
            claim_age_s=claim_age_s,
            receipt_local=receipt_local,
            progress_timeout_s=progress_timeout_s,
            wedge_timeout_s=wedge_timeout_s,
        )
        in WEDGE_ACTUATING_STATES
    )


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
