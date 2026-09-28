"""Bounded recurring entrypoints for the Link and Mero control-plane seats.

The entrypoint performs the cheap, revision-pinned host fence before opening
CardStore or mediated PR input. A host-local :class:`SeatCycleGuard` then
prevents a second invocation from waiting or doing duplicate work. A missing
or invalid Link feed is recorded as an honest bounded no-op.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import socket
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from skcoord.card import Column
from skcoord.card_store import CardStore

from .estate import host_lifecycle_claim
from .fleet.deployment_manifest import DISPATCHER_RELATIVE_PATH, deployed_artifact_path
from .fleet.rotation_lock import SERAPH_LOCK_WAIT_SECONDS
from .lifecycle_seats import LIFECYCLE_SEATS
from .link_cycle import recommend_one_reviewer
from .link_observation_feed import ObservationFeedError, load_observation_feed
from .link_review_work import load_review_work, reconcile_review_work_batch
from .mero_census import run_blocker_census
from .receipt_output import condense_dispatcher_output
from .seat_boundaries import BoundaryError, canonical_principal
from .seat_cycle_guard import CycleResult, SeatCycleGuard
from .seat_cycle_overlap import RecurringCycleSchedule, seat_cycle_runs_overlap
from .seat_mail import poll_mail, startup_hello

_SEATS = LIFECYCLE_SEATS
_LAUNCH = re.compile(
    r"^(?P<outcome>LAUNCHED|LAUNCH_FAILED)\|(?P<host>[^|]+)\|(?P<session>[^|]+)\|(?P<card>[^|]+)"
    r"\|lane=(?P<lane>[^|]+)\|model=(?P<model>[^|]+)"
    r"\|owner=(?P<owner>[^|]+)\|claim_revision=(?P<revision>[^|]+)$"
)
_MAX_SERAPH_BATCH = 8
_MAX_ROLE_BATCH = 8
# Seraph's total wall-clock budget for one service invocation is NOT free to
# raise on its own. It is fenced by two production constants this module
# does not own:
#   - src/skcapstone/data/systemd/skfleet-seraph.service sets
#     TimeoutStartSec=540, so systemd itself SIGKILLs the whole unit at 540s,
#     uncontrolled, if our own bounded reap has not already finished.
#   - src/skcapstone/data/systemd/skfleet-seraph.timer fires the service
#     every ten minutes (600s) on the clock (see its OnCalendar). A cycle
#     that runs past 600s overlaps its own next scheduled firing.
# test_dispatcher_routes_niobe_and_seraph_through_safe_bounded_waits (in
# tests/test_rotation_lock_fairness.py) enforces the resulting invariant:
#   SERAPH_LOCK_WAIT_SECONDS + _SERAPH_DISPATCH_TIMEOUT_SECONDS + 30 < 300
# which, with SERAPH_LOCK_WAIT_SECONDS == 75, caps this constant at 434.
# Do NOT weaken or delete that test to make room for a bigger number here;
# it is the thing that caught this constant being raised past what the
# service timeout and timer cadence can actually absorb.
#
# Default Seraph dispatcher wall-clock timeout, in seconds. Overridable via
# SKFLEET_SERAPH_DISPATCH_TIMEOUT_SECONDS (bounded by
# _SERAPH_DISPATCH_TIMEOUT_MAX_SECONDS below); see
# _resolve_seraph_dispatch_timeout_seconds for how the override is read.
#
# WHY 420: the budget was raised as a whole on 2026-09-20, which is what the
# previous note (kept below) said the real fix had to be.
#
# 190 was never enough. Measured over 3h on chiap08 against a 7304-card store,
# fourteen consecutive cycles ran 150s to 188s wall clock, so a normal run sat
# permanently within 2s to 40s of its own deadline. Over that window
# seraph_dispatch_timeout was the DOMINANT cycle outcome (7 of 11 classified
# cycles), ahead of seraph_no_available_capacity (2). Profiling showed no
# single hotspot to optimise away: the full CardStore fold is 1.2s, the event
# log read 0.3s, the overlay fold 0.1s, and `skmail read seraph` under 1s. The
# time is spread across the dispatcher's own per-card subprocess work, which
# grows with the card store.
#
# So the ceiling moved instead, together, exactly as the old note required:
#   TimeoutStartSec 300 -> 540 (skfleet-seraph.service)
#   timer cadence   5min -> 10min (skfleet-seraph.timer)
# The cadence half was already proven in production: chiap08 had been running
# a 10-minute OnCalendar drop-in over the 5-minute template for some time, so
# the template had drifted from the live estate and this aligns it.
#
# The invariant still holds with room to spare: 75 + 420 + 30 = 525 < 540,
# and 540 < 600 so a cycle cannot overlap its next firing. Against measured
# runs of 150s to 188s that is a margin of roughly 2.2x rather than 1.01x.
#
# The ORIGINAL note, which remains the standing rule for anyone tempted to
# raise this constant alone: 190 was the largest round value the module could
# hold against the old 300s ceiling (75 + 190 + 30 = 295, a 5s margin). It was
# only a modest improvement over 180, and a run near the measured 180s point
# was still at real risk of being killed. The fix for that is making the
# dispatcher run faster against a growing card store, or deliberately raising
# the whole budget (TimeoutStartSec and the timer cadence together, with the
# cadence proven to tolerate it) rather than this constant in isolation.
#
# _run_seraph_dispatcher reaps a timeout with
# os.killpg(process.pid, signal.SIGTERM) across the whole process group. A
# dispatcher killed mid-run can be interrupted between claiming a card and
# launching its worker, leaving a claimed card with no worker behind it.
# That orphaned claim then occupies the card until a reaper clears it, which
# is why an unbounded dispatcher is worse than one that occasionally gets
# killed -- do not remove the timeout to dodge this ceiling.
_SERAPH_DISPATCH_TIMEOUT_SECONDS = 420
_SERAPH_DISPATCH_TIMEOUT_ENV = "SKFLEET_SERAPH_DISPATCH_TIMEOUT_SECONDS"
# Mirrors skfleet-seraph.service's TimeoutStartSec and the 30s cleanup_margin
# test_dispatcher_routes_niobe_and_seraph_through_safe_bounded_waits budgets
# alongside SERAPH_LOCK_WAIT_SECONDS. Kept here, not imported, because the
# test asserts against its OWN literal 540/30 to catch either side drifting;
# importing would let both drift together silently.
_SERAPH_SERVICE_DEADLINE_SECONDS = 540
_SERAPH_CLEANUP_MARGIN_SECONDS = 30
# An env override at or above this is rejected (falls back to the default)
# rather than accepted, because it would violate the invariant above outright.
_SERAPH_DISPATCH_TIMEOUT_MAX_SECONDS = (
    _SERAPH_SERVICE_DEADLINE_SECONDS - SERAPH_LOCK_WAIT_SECONDS - _SERAPH_CLEANUP_MARGIN_SECONDS
)
_DISPATCH_TERMINATE_GRACE_SECONDS = 5
_SUBPROCESS_RUN = subprocess.run
_NOOP = re.compile(
    r"^NOOP_RECEIPT\|(?P<host>[^|]+)\|reason=(?P<reason>[^|]+)" r"\|seat=(?P<seat>[^|]+)$"
)
_CYCLE_SCHEDULES = {
    "link": RecurringCycleSchedule("*/5 * * * *", timedelta(minutes=2)),
    "mero": RecurringCycleSchedule("2-57/5 * * * *", timedelta(minutes=3)),
}

# A seat asks SKGateway for a SIZE, never for a provider. The gateway resolves
# sk-s, sk-m, sk-l, or sk-xl to a member that meets the capability floor and the
# trust zone of the request, so one estate can run Claude, an OpenRouter free
# tier, NIM, or a local backend while another runs a subscription lane, with no
# code change on either. Provider-pinned ids (sk-codex-mid, sk-glm-l, sk-kimi-*)
# stay valid operator overrides; they are no longer the built-in default.
#
# A size with no qualifying member fails closed at the gateway. That is correct
# and deliberate: nothing here substitutes a smaller bucket, because a silent
# downgrade hides a real capability gap behind work that quietly got weaker.
_SIZE_CLASSES: tuple[str, ...] = ("S", "M", "L", "XL")
_SIZE_MODEL_DEFAULTS: dict[str, str] = {"S": "sk-s", "M": "sk-m", "L": "sk-l", "XL": "sk-xl"}
_SIZE_MODEL_ENV = "SKFLEET_MODEL_{size}"
# Deprecated provider-named spelling, still read so an estate configured before
# the rename keeps its exact behaviour across the upgrade.
_LEGACY_SIZE_MODEL_ENV = "SKFLEET_CODEX_MODEL_{size}"


def resolve_size_class_models(
    environ: Mapping[str, str] | None = None,
    sizes: Iterable[str] = _SIZE_CLASSES,
) -> dict[str, str]:
    """Resolve the SKGateway bucket each card size class dispatches to.

    Operator configuration wins over the built-in default. This function is the
    fix for a defect where the dispatch path copied the environment and then
    overwrote these variables, so a systemd drop-in setting them was silently
    defeated and every estate was pinned to one subscription provider.

    Precedence, highest first: ``SKFLEET_MODEL_<size>``, the deprecated
    ``SKFLEET_CODEX_MODEL_<size>``, then the provider-neutral default. A value
    that is empty or whitespace counts as unset, so a blank ``Environment=``
    line cannot blank a bucket.

    Args:
        environ: Environment mapping to read. Defaults to ``os.environ``.
        sizes: Size classes to resolve. Unknown size names are ignored.

    Returns:
        An environment overlay mapping both the current and the deprecated
        variable name of every resolved size class to its chosen bucket, ready
        to pass to :func:`subprocess.run` as part of a child environment.
    """

    values = os.environ if environ is None else environ
    overlay: dict[str, str] = {}
    for size in sizes:
        default = _SIZE_MODEL_DEFAULTS.get(size)
        if default is None:
            continue
        configured = values.get(_SIZE_MODEL_ENV.format(size=size)) or values.get(
            _LEGACY_SIZE_MODEL_ENV.format(size=size)
        )
        model = str(configured or "").strip() or default
        overlay[_SIZE_MODEL_ENV.format(size=size)] = model
        overlay[_LEGACY_SIZE_MODEL_ENV.format(size=size)] = model
    return overlay


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CycleSummary:
    seat: str
    host: str
    control_revision: str
    cycle_id: str
    result: str
    cards_examined: int = 0
    recommendations: int = 0
    suppressed: int = 0
    dispatch_succeeded: int = 0
    dispatch_failed: int = 0
    dispatch_retryable: int = 0
    reason: str | None = None
    source_revision: str | None = None
    evidence_sha256: str | None = None
    exception_type: str | None = None
    cleanup: str | None = None
    dispatcher_stdout: str | None = None
    dispatcher_stderr: str | None = None
    mailbox_poll_at: str | None = None
    mailbox_ok: bool = False
    mailbox_new_messages: int = 0
    mailbox_help_or_handoff: int = 0
    mailbox_digest: str | None = None
    mailbox_error: str | None = None


def load_control_plane(path: Path, *, host: str | None = None) -> dict[str, Any]:
    """Read and validate the small public active-host control record.

    This is the point where a record from the SYNCED coordination tree is
    read and then trusted, so it is also where a host-local refusal belongs.
    The election itself stays estate-wide (see :mod:`skcapstone.estate` for
    why exactly one host per estate must be elected there). What is added on
    top is one-directional: when this machine carries a host-local lifecycle
    claim, the synced record has to agree with it. The claim can only ever
    refuse, never grant, so a record that reached this machine by mis-sync or
    tampering cannot activate a host whose own operator never declared it.

    Args:
        path: The estate's ``coordination/seat-control-plane.json``.
        host: Running host override, for tests.

    Returns:
        The validated control record.

    Raises:
        ValueError: On a bad schema, a missing active host or revision, a seat
            not provisioned on the active host, or a record that contradicts
            this machine's own host-local claim.
    """

    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("seat control plane schema_version must be 1")
    active_host = str(value.get("active_host") or "")
    revision = str(value.get("revision") or "")
    if not active_host or not revision:
        raise ValueError("seat control plane requires active_host and revision")
    seats = value.get("seats")
    if not isinstance(seats, dict):
        raise ValueError("seat control plane seats are required")
    for seat in _SEATS:
        hosts = seats.get(seat)
        if not isinstance(hosts, list) or active_host not in hosts:
            raise ValueError(f"seat {seat} is not provisioned on active host")
    claim = host_lifecycle_claim(host=host)
    if claim is not None and claim != active_host.strip().lower():
        raise ValueError(f"seat control plane names {active_host} but this machine claims {claim}")
    return value


def _health_path(home: Path, seat: str) -> Path:
    return home / "coordination" / "seat-cycles" / f"{seat}.health.jsonl"


def _condensed_receipt_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return payload with dispatcher stdout/stderr bounded for embedding.

    This is the single place both receipt outputs (the health JSONL file
    and the CLI's own stdout summary, see main() below) go through, so
    every current and future producer of dispatcher_stdout/stderr on
    CycleSummary is bounded, not just the seraph timeout path that
    happens to set them today.
    """

    payload = dict(payload)
    payload["dispatcher_stdout"] = condense_dispatcher_output(payload.get("dispatcher_stdout"))
    payload["dispatcher_stderr"] = condense_dispatcher_output(payload.get("dispatcher_stderr"))
    return payload


def _append_health(home: Path, summary: CycleSummary) -> None:
    path = _health_path(home, summary.seat)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _condensed_receipt_payload({"at": _now(), **asdict(summary)})
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")


def run_cycle(
    *,
    seat: str,
    home: Path,
    control_plane: Path,
    local_host: str | None = None,
    dry_run: bool = False,
    operation: Callable[[], dict[str, int]] | None = None,
    schedules: Mapping[str, RecurringCycleSchedule] = _CYCLE_SCHEDULES,
    schedule_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> CycleSummary:
    """Run one fenced lifecycle-seat cycle, or record a bounded no-op."""

    seat = seat.strip().lower()
    if seat not in _SEATS:
        raise ValueError(f"unsupported recurring seat: {seat}")
    host = (local_host or socket.gethostname()).strip().lower()
    control = load_control_plane(control_plane, host=host)
    revision = str(control["revision"])
    if host != str(control["active_host"]):
        summary = CycleSummary(
            seat=seat,
            host=host,
            control_revision=revision,
            cycle_id="inactive-host",
            result="inactive_host_refused",
            reason=f"active_host={control['active_host']}",
        )
        _append_health(home, summary)
        return summary

    if seat == "mero" and seat_cycle_runs_overlap(
        "link",
        schedules["link"],
        "mero",
        schedules["mero"],
        after=schedule_clock(),
    ):
        summary = CycleSummary(
            seat=seat,
            host=host,
            control_revision=revision,
            cycle_id="schedule-overlap",
            result="schedule_overlap_deferred",
            reason="higher_priority_cycle=link",
        )
        _append_health(home, summary)
        return summary

    # Mail is presence and coordination only. Read before operation, but do
    # not acknowledge automatically and never allow mail failure to widen or
    # replace the seat's typed authority boundary.
    startup_hello(home, seat, host=host)
    mailbox = poll_mail(seat)

    guard = SeatCycleGuard(home / "coordination" / "seat-cycles", seat)

    def guarded_operation(_cycle_id: str) -> dict[str, int | str]:
        if dry_run:
            return {"cards_examined": 0, "recommendations": 0, "suppressed": 0}
        if operation is None:
            return {
                "cards_examined": 0,
                "recommendations": 0,
                "suppressed": 0,
                "reason": "operation_not_configured",
            }
        return operation()

    result: CycleResult[dict[str, int | str]] = guard.run(guarded_operation)
    if not result.ran:
        summary = CycleSummary(
            seat=seat,
            host=host,
            control_revision=revision,
            cycle_id=result.cycle_id,
            result="overlap_noop",
            reason=result.live_cycle_id,
            **mailbox.as_dict(),
        )
    else:
        values = result.value or {}
        reason = values.get("reason")
        summary = CycleSummary(
            seat=seat,
            host=host,
            control_revision=revision,
            cycle_id=result.cycle_id,
            result=("dry_run" if dry_run else str(reason or "complete")),
            cards_examined=int(values.get("cards_examined", 0)),
            recommendations=int(values.get("recommendations", 0)),
            suppressed=int(values.get("suppressed", 0)),
            dispatch_succeeded=int(values.get("dispatch_succeeded", 0)),
            dispatch_failed=int(values.get("dispatch_failed", 0)),
            dispatch_retryable=int(values.get("dispatch_retryable", 0)),
            reason=str(reason) if reason is not None else None,
            source_revision=(
                str(values["source_revision"]) if values.get("source_revision") else None
            ),
            evidence_sha256=(
                str(values["evidence_sha256"]) if values.get("evidence_sha256") else None
            ),
            exception_type=(
                str(values["exception_type"]) if values.get("exception_type") else None
            ),
            cleanup=str(values["cleanup"]) if values.get("cleanup") else None,
            dispatcher_stdout=(
                str(values["dispatcher_stdout"])
                if values.get("dispatcher_stdout") is not None
                else None
            ),
            dispatcher_stderr=(
                str(values["dispatcher_stderr"])
                if values.get("dispatcher_stderr") is not None
                else None
            ),
            **mailbox.as_dict(),
        )
    _append_health(home, summary)
    return summary


def mero_operation(home: Path) -> dict[str, int]:
    report = run_blocker_census(home, emit=True)
    return {
        "cards_examined": report.cards_examined,
        "recommendations": len(report.findings),
        "suppressed": report.suppressed_unchanged,
    }


def presence_operation() -> dict[str, int | str]:
    """Record a bounded presence cycle without acquiring card authority."""

    return {
        "cards_examined": 0,
        "recommendations": 0,
        "suppressed": 0,
        "reason": "presence_complete",
    }


def verify_seraph_dispatch(
    home: Path,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, int | str]:
    """Verify every selector result independently and report partial outcomes."""

    # The launcher writes its action log to stdout in normal operation, but
    # worker/bootstrap failures can move the same typed lines to stderr.  A
    # receipt is an output contract, not a stream contract, so inspect both
    # streams without treating diagnostics as receipts.
    output = "\n".join(
        value
        for value in (getattr(completed, "stdout", ""), getattr(completed, "stderr", ""))
        if value
    )
    launches = [
        match.groupdict()
        for line in output.splitlines()
        if (match := _LAUNCH.fullmatch(line.strip()))
    ]
    noops = [
        match.groupdict()
        for line in output.splitlines()
        if (match := _NOOP.fullmatch(line.strip()))
    ]
    if completed.returncode != 0 and not launches:
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "reason": "seraph_dispatch_failed",
        }
    if not launches and len(noops) == 1 and noops[0]["seat"] == "seraph":
        reason = noops[0]["reason"]
        if reason in {
            "no_eligible_work",
            "no_available_capacity",
            "all_candidates_suppressed",
            "rotation_overlap",
        }:
            return {
                "cards_examined": 0,
                "recommendations": 0,
                "suppressed": 0,
                "reason": f"seraph_{reason}",
            }
    if not launches or noops:
        return {
            "cards_examined": len(launches),
            "recommendations": 0,
            "suppressed": 1,
            "reason": "seraph_launch_receipt_missing",
        }
    store = CardStore(home)
    succeeded = failed = 0
    invalid = int(completed.returncode != 0)
    source_heads: set[tuple[str, str]] = set()
    seen_cards: set[str] = set()
    for launch in launches:
        card = store.fold(launch["card"])
        events = store._read_events(launch["card"])
        recommendations = [
            event
            for event in events
            if event.get("action") == "review_assignment_recommendation"
            and event.get("writer") == "link"
            and event.get("reviewer") == launch["owner"]
        ]
        receipts = [
            event
            for event in events
            if event.get("action") == "review_assignment_launch"
            and event.get("reviewer") == launch["owner"]
            and event.get("claim_revision") == launch["revision"]
        ]
        producer = str((getattr(card, "links", {}) or {}).get("producer_identity") or "")
        source_head = (
            str(getattr(card, "meta", {}).get("link_source_card") or ""),
            str(getattr(card, "meta", {}).get("link_head_revision") or ""),
        )
        route_identity = receipts[0].get("route_identity") if len(receipts) == 1 else None
        model_allowed = (
            isinstance(route_identity, dict)
            and receipts[0].get("schema") == "skfleet.review-assignment-launch/v2"
            and route_identity.get("model_or_bucket") == launch["model"]
            and isinstance(route_identity.get("logical_route"), str)
            and bool(route_identity.get("logical_route"))
            and isinstance(route_identity.get("provider"), str)
            and bool(route_identity.get("provider"))
            and isinstance(route_identity.get("capacity_domains"), list)
            and bool(route_identity.get("capacity_domains"))
            and all(
                isinstance(domain, str) and domain
                for domain in route_identity.get("capacity_domains", [])
            )
        )
        common_valid = (
            card is not None
            and launch["card"] not in seen_cards
            and all(source_head)
            and source_head not in source_heads
            and "review" in card.labels
            and "seat-seraph" in card.labels
            and model_allowed
            and launch["owner"].startswith("pi-seraph-")
            and len(recommendations) == 1
            and len(receipts) == 1
            and recommendations[0].get("author") == producer
            and canonical_principal(producer) != canonical_principal(launch["owner"])
            and canonical_principal(producer)
            != canonical_principal(str(recommendations[0].get("reviewer") or ""))
            and receipts[0].get("recommendation_id") == recommendations[0].get("recommendation_id")
        )
        seen_cards.add(launch["card"])
        source_heads.add(source_head)
        if not common_valid:
            invalid += 1
            continue
        launched = launch["outcome"] == "LAUNCHED"
        if receipts[0].get("launched") is not launched:
            invalid += 1
            continue
        if launched:
            status = getattr(getattr(card, "status", None), "value", getattr(card, "status", None))
            if (
                status != "doing"
                or card.owner != launch["owner"]
                or card.meta.get("_claim_revision") != launch["revision"]
            ):
                invalid += 1
                continue
            unit = f"skfleet-worker-{launch['lane']}-{launch['card']}.service"
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                capture_output=True,
                timeout=10,
            )
            if active.returncode != 0:
                invalid += 1
                continue
            succeeded += 1
        elif _failed_launch_is_retryable(store, launch, card, events, receipts[0]):
            failed += 1
        else:
            invalid += 1
    suppressed = failed + invalid
    if succeeded and suppressed:
        reason = "seraph_dispatch_partial"
    elif succeeded:
        reason = "seraph_dispatch_complete"
    else:
        reason = "seraph_dispatch_failed"
    return {
        "cards_examined": len(launches),
        "recommendations": succeeded,
        "suppressed": suppressed,
        "dispatch_succeeded": succeeded,
        "dispatch_failed": suppressed,
        "dispatch_retryable": failed,
        "reason": reason,
    }


def _failed_launch_is_retryable(
    store: CardStore,
    launch: dict[str, str],
    card: object,
    events: list[dict[str, object]],
    receipt: dict[str, object],
) -> bool:
    """Bind a failed launch to its exact released, currently claimable generation."""

    owner = launch["owner"]
    revision = launch["revision"]
    claims = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "claim"
        and event.get("owner") == owner
        and (event.get("claim_revision") or event.get("event_id")) == revision
    ]
    releases = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "release_claim"
        and event.get("released_owner") == owner
        and event.get("expected_claim_revision") == revision
    ]
    try:
        receipt_index = next(index for index, event in enumerate(events) if event is receipt)
    except StopIteration:
        return False
    if (
        len(claims) != 1
        or len(releases) != 1
        or not claims[0] < receipt_index < releases[0]
        or getattr(card, "owner", None) is not None
        or getattr(card, "status", None) != Column.BACKLOG
        or bool(getattr(card, "archived", False))
    ):
        return False
    labels = {str(label).lower() for label in getattr(card, "labels", ())}
    if (
        labels & {"do-not-claim", "human-gate", "not-claimable", "superseded"}
        or any(label.startswith("superseded-") or "do-not-claim" in label for label in labels)
        or "[human]" in str(getattr(card, "title", "")).lower()
    ):
        return False
    return all(
        (dependency := store.fold(dependency_id)) is not None and dependency.status == Column.DONE
        for dependency_id in getattr(card, "dependencies", ())
    )


def _resolve_seraph_dispatch_timeout_seconds() -> int:
    """Resolve the Seraph dispatcher wall-clock timeout, in seconds.

    Reads SKFLEET_SERAPH_DISPATCH_TIMEOUT_SECONDS, following the same
    env-reading shape as the SKFLEET_SERAPH_BATCH_SIZE read in
    seraph_operation. Unlike the batch size, an invalid override here does
    NOT suppress the dispatch: a non-integer, zero, or negative value falls
    back to the default rather than crashing or disabling the timeout
    outright. Disabling the timeout is not an acceptable failure mode - see
    the comment on _SERAPH_DISPATCH_TIMEOUT_SECONDS for why an unbounded
    dispatcher is worse than one that occasionally gets killed.

    A value that is otherwise well-formed but at or above
    _SERAPH_DISPATCH_TIMEOUT_MAX_SECONDS is ALSO rejected in favour of the
    default, for a different reason: that ceiling is load-bearing, not
    advisory. It is what keeps SERAPH_LOCK_WAIT_SECONDS +
    _SERAPH_DISPATCH_TIMEOUT_SECONDS + _SERAPH_CLEANUP_MARGIN_SECONDS under
    the 540s systemd TimeoutStartSec and the 600s timer cadence on
    skfleet-seraph.service/.timer. An env var that could push this module
    past that ceiling would let a single operator override silently break
    an invariant a dedicated test exists to protect
    (test_dispatcher_routes_niobe_and_seraph_through_safe_bounded_waits in
    tests/test_rotation_lock_fairness.py). Accepting no override at all is
    strictly better than accepting one that can do that.
    """

    raw = os.environ.get(_SERAPH_DISPATCH_TIMEOUT_ENV)
    if raw is None:
        return _SERAPH_DISPATCH_TIMEOUT_SECONDS
    try:
        timeout = int(raw)
    except ValueError:
        return _SERAPH_DISPATCH_TIMEOUT_SECONDS
    if timeout <= 0:
        return _SERAPH_DISPATCH_TIMEOUT_SECONDS
    if timeout >= _SERAPH_DISPATCH_TIMEOUT_MAX_SECONDS:
        return _SERAPH_DISPATCH_TIMEOUT_SECONDS
    return timeout


def _run_seraph_dispatcher(
    command: list[str], *, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run Seraph in an isolated process group and reap it on timeout."""

    timeout = _resolve_seraph_dispatch_timeout_seconds()
    if subprocess.run is not _SUBPROCESS_RUN:
        return subprocess.run(
            command,
            env=environment,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    process = subprocess.Popen(
        command,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = process.communicate(timeout=_DISPATCH_TERMINATE_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(
            command,
            timeout,
            output=stdout if stdout is not None else exc.stdout,
            stderr=stderr if stderr is not None else exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def seraph_operation(home: Path) -> dict[str, int | str]:
    """Launch one configurable, bounded Seraph review batch.

    ``home`` is the ESTATE home, and it is used for exactly one thing here:
    locating the card store that ``verify_seraph_dispatch`` reads. It is
    deliberately NOT passed to ``deployed_artifact_path``, whose base is a
    USER home.

    That distinction was lost once and cost a 19-hour silent outage. This
    call read ``deployed_artifact_path(name, home)``, so on chi it resolved
    the dispatcher to ``~/.skcapstone/.local/bin/skfleet-rotate.py`` while
    the rollout deploys to ``~/.local/bin/skfleet-rotate.py``. The guard
    below found no file, returned ``seraph_dispatcher_missing`` with
    ``suppressed: 1``, and the independent-review lane dispatched nothing
    while ``awaiting_review`` climbed past 400. Every batch was skipped and
    the only record was a reason string inside a receipt nobody was
    reading.
    """

    dispatcher = deployed_artifact_path(DISPATCHER_RELATIVE_PATH.name)
    if not dispatcher.is_file() or not os.access(dispatcher, os.X_OK):
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "dispatch_succeeded": 0,
            "dispatch_failed": 1,
            "reason": "seraph_dispatcher_missing",
        }
    try:
        batch_size = int(os.environ.get("SKFLEET_SERAPH_BATCH_SIZE", "2"))
    except ValueError:
        batch_size = 0
    if not 1 <= batch_size <= _MAX_SERAPH_BATCH:
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "dispatch_succeeded": 0,
            "dispatch_failed": 1,
            "reason": "seraph_batch_size_invalid",
        }
    env = os.environ.copy()
    env.update(
        {
            "SKFLEET_ONLY_SEAT": "seraph",
            "SKFLEET_TARGET": str(batch_size),
            "SKFLEET_SEAT_TARGET": str(batch_size),
            "SKFLEET_QWEN_TARGET": "0",
            "SKFLEET_GLM_TARGET": "0",
            "SKFLEET_KIMI_TARGET": "0",
            "SKFLEET_MAX_LAUNCH": str(batch_size),
        }
    )
    # Seraph reviews only [S] work, so only that bucket is resolved here.
    env.update(resolve_size_class_models(env, sizes=("S",)))
    try:
        completed = _run_seraph_dispatcher([str(dispatcher), "--go"], environment=env)
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode(errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout
        )
        stderr = (
            exc.stderr.decode(errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr
        )
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "dispatch_failed": 1,
            "reason": "seraph_dispatch_timeout",
            "exception_type": type(exc).__name__,
            "cleanup": "process_group_reaped",
            "dispatcher_stdout": stdout or "",
            "dispatcher_stderr": stderr or "",
        }
    return verify_seraph_dispatch(home, completed)


def verify_role_dispatch(
    home: Path,
    completed: subprocess.CompletedProcess[str],
    seat: str,
    accepted_models: Iterable[str] | None = None,
) -> dict[str, int | str]:
    """Verify a bounded ATLAS selector result.

    Args:
        home: Estate home holding the card store.
        completed: Finished dispatcher process whose receipts are verified.
        seat: ``atlas``.
        accepted_models: Models a receipt may name. Defaults to the resolved
            size class buckets, which is what the dispatch path asked for. This
            check used to require the literal ``sk-codex-mid``, which no launch
            receipt has ever carried since the launcher started emitting the
            job's logical route, so every real launch was counted invalid.

    Returns:
        Bounded counters and a reason string describing the verified outcome.
    """

    output = "\n".join(
        value
        for value in (getattr(completed, "stdout", ""), getattr(completed, "stderr", ""))
        if value
    )
    launches = [
        match.groupdict()
        for line in output.splitlines()
        if (match := _LAUNCH.fullmatch(line.strip()))
    ]
    noops = [
        match.groupdict()
        for line in output.splitlines()
        if (match := _NOOP.fullmatch(line.strip()))
    ]
    if not launches and len(noops) == 1 and noops[0]["seat"] == seat:
        reason = noops[0]["reason"]
        if completed.returncode == 0 and reason in {
            "no_eligible_work",
            "no_available_capacity",
            "rotation_overlap",
        }:
            return {
                "cards_examined": 0,
                "recommendations": 0,
                "suppressed": 0,
                "reason": f"{seat}_{reason}",
            }
    if not launches or noops:
        return {
            "cards_examined": len(launches),
            "recommendations": 0,
            "suppressed": 1,
            "dispatch_succeeded": 0,
            "dispatch_failed": 1,
            "reason": f"{seat}_dispatch_failed",
        }

    store = CardStore(home)
    accepted = (
        set(accepted_models)
        if accepted_models is not None
        else set(resolve_size_class_models().values())
    )
    succeeded = failed = 0
    invalid = int(completed.returncode != 0)
    seen_cards: set[str] = set()
    for launch in launches:
        card = store.fold(launch["card"])
        events = store._read_events(launch["card"])
        labels = {str(label).strip().lower() for label in getattr(card, "labels", ())}
        seat_labels = {label for label in labels if label.startswith("seat-")}
        valid = (
            card is not None
            and launch["card"] not in seen_cards
            and launch["owner"].startswith(f"pi-{seat}-")
            and seat_labels == {f"seat-{seat}"}
            and "dispatch-approved" in labels
            and launch["lane"] == "codex"
            and launch["model"] in accepted
        )
        seen_cards.add(launch["card"])
        if not valid:
            invalid += 1
            continue
        if launch["outcome"] == "LAUNCHED":
            status = getattr(getattr(card, "status", None), "value", getattr(card, "status", None))
            if (
                status != "doing"
                or card.owner != launch["owner"]
                or card.meta.get("_claim_revision") != launch["revision"]
            ):
                invalid += 1
                continue
            unit = f"skfleet-worker-{launch['lane']}-{launch['card']}.service"
            active = subprocess.run(
                ["systemctl", "--user", "is-active", "--quiet", unit],
                capture_output=True,
                timeout=10,
            )
            if active.returncode != 0:
                invalid += 1
                continue
            succeeded += 1
        elif _failed_claim_is_retryable(store, launch, card, events):
            failed += 1
        else:
            invalid += 1
    suppressed = failed + invalid
    reason = (
        f"{seat}_dispatch_partial"
        if succeeded and suppressed
        else f"{seat}_dispatch_complete" if succeeded else f"{seat}_dispatch_failed"
    )
    return {
        "cards_examined": len(launches),
        "recommendations": succeeded,
        "suppressed": suppressed,
        "dispatch_succeeded": succeeded,
        "dispatch_failed": suppressed,
        "dispatch_retryable": failed,
        "reason": reason,
    }


def _failed_claim_is_retryable(
    store: CardStore,
    launch: dict[str, str],
    card: object,
    events: list[dict[str, object]],
) -> bool:
    """Require exact-generation claim release after a failed role launch."""

    owner, revision = launch["owner"], launch["revision"]
    claims = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "claim"
        and event.get("owner") == owner
        and (event.get("claim_revision") or event.get("event_id")) == revision
    ]
    releases = [
        index
        for index, event in enumerate(events)
        if event.get("action") == "release_claim"
        and event.get("released_owner") == owner
        and event.get("expected_claim_revision") == revision
    ]
    if (
        len(claims) != 1
        or len(releases) != 1
        or claims[0] >= releases[0]
        or getattr(card, "owner", None) is not None
        or getattr(card, "status", None) != Column.BACKLOG
        or bool(getattr(card, "archived", False))
    ):
        return False
    return all(
        (dependency := store.fold(dependency_id)) is not None and dependency.status == Column.DONE
        for dependency_id in getattr(card, "dependencies", ())
    )


def role_dispatch_operation(home: Path, seat: str) -> dict[str, int | str]:
    """Launch one configurable, bounded ATLAS batch.

    ``home`` is the ESTATE home and is passed on to ``verify_role_dispatch``
    for the card store only. As in ``seraph_operation``, it must not reach
    ``deployed_artifact_path``: this call site carried the identical defect.
    """

    dispatcher = deployed_artifact_path(DISPATCHER_RELATIVE_PATH.name)
    if not dispatcher.is_file() or not os.access(dispatcher, os.X_OK):
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "dispatch_succeeded": 0,
            "dispatch_failed": 1,
            "reason": f"{seat}_dispatcher_missing",
        }
    env_name = f"SKFLEET_{seat.upper()}_BATCH_SIZE"
    try:
        batch_size = int(os.environ.get(env_name, "2"))
    except ValueError:
        batch_size = 0
    if seat != "atlas" or not 1 <= batch_size <= _MAX_ROLE_BATCH:
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "dispatch_succeeded": 0,
            "dispatch_failed": 1,
            "reason": f"{seat}_batch_size_invalid",
        }
    env = os.environ.copy()
    env.update(
        {
            "SKFLEET_ONLY_SEAT": seat,
            "SKFLEET_SEAT_TARGET": str(batch_size),
            "SKFLEET_QWEN_TARGET": "0",
            "SKFLEET_GLM_TARGET": "0",
            "SKFLEET_KIMI_TARGET": "0",
            "SKFLEET_MAX_LAUNCH": str(batch_size),
        }
    )
    # Resolved last, and from `env` rather than from a literal, so operator
    # configuration wins instead of being overwritten by a hardcoded default.
    size_models = resolve_size_class_models(env)
    env.update(size_models)
    completed = subprocess.run(
        [str(dispatcher), "--go"],
        env=env,
        capture_output=True,
        text=True,
        timeout=240,
    )
    # Verification accepts exactly the buckets this dispatch asked for.
    return verify_role_dispatch(home, completed, seat, set(size_models.values()))


def _emit_review_work(home: Path, lineage_path: Path, feed_reason: str) -> dict[str, int | str]:
    try:
        source_revision, evidence_sha256, recommendations = load_review_work(lineage_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"reason": feed_reason, "suppressed": 1}
    output = home / "coordination" / "seat-cycles" / "link.review-work.jsonl"
    if recommendations:
        results = reconcile_review_work_batch(
            home, recommendations, evidence_sha256=evidence_sha256
        )
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as stream:
            for recommendation, result in zip(recommendations, results, strict=True):
                event = {
                    "source_revision": source_revision,
                    "evidence_sha256": evidence_sha256,
                    **recommendation,
                    "reconciliation": result.as_dict(),
                }
                stream.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
    return {
        "cards_examined": len(recommendations),
        "recommendations": len(recommendations),
        "suppressed": 1,
        "reason": f"review_work_only:{feed_reason}",
        "source_revision": source_revision,
        "evidence_sha256": evidence_sha256,
    }


def link_operation(
    home: Path, feed_path: Path, lineage_path: Path | None = None
) -> dict[str, int | str]:
    """Consume mediated observations and append advisory handoffs only."""

    try:
        feed = load_observation_feed(feed_path)
    except ObservationFeedError as exc:
        lineage = lineage_path or home / "coordination" / "link-lineage.json"
        return _emit_review_work(home, lineage, str(exc))

    handoffs: list[dict[str, object]] = []
    suppressed = 0
    for record in feed.records:
        observation = record.observation
        if not record.review_card_id or not record.review_card_revision:
            suppressed += 1
            continue
        try:
            handoff = recommend_one_reviewer(
                observation,
                producer=feed.producer,
                candidates=feed.reviewer_candidates,
                review_card_id=record.review_card_id,
                review_card_revision=record.review_card_revision,
            )
        except BoundaryError:
            suppressed += 1
            continue
        if handoff is not None:
            handoffs.append(
                {
                    "source_revision": feed.source_revision,
                    "evidence_sha256": feed.evidence_sha256,
                    **handoff.as_event(),
                }
            )

    output = home / "coordination" / "seat-cycles" / "link.handoffs.jsonl"
    if handoffs:
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("a", encoding="utf-8") as stream:
            for handoff in handoffs:
                stream.write(json.dumps(handoff, sort_keys=True, separators=(",", ":")) + "\n")
    return {
        "cards_examined": len(feed.records),
        "recommendations": len(handoffs),
        "suppressed": suppressed,
        "source_revision": feed.source_revision,
        "evidence_sha256": feed.evidence_sha256,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seat", choices=sorted(_SEATS), required=True)
    parser.add_argument("--home", type=Path, default=Path.home() / ".skcapstone")
    parser.add_argument("--control-plane", type=Path, required=True)
    parser.add_argument(
        "--observation-feed",
        type=Path,
        default=None,
        help="mediated Link observation feed; defaults to coordination/link-observations.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    feed_path = args.observation_feed or args.home / "coordination" / "link-observations.json"
    if args.seat == "mero":

        def operation() -> dict[str, int]:
            return mero_operation(args.home)

    elif args.seat == "seraph":

        def operation() -> dict[str, int | str]:
            return seraph_operation(args.home)

    elif args.seat == "link":

        def operation() -> dict[str, int | str]:
            return link_operation(args.home, feed_path)

    elif args.seat == "atlas":

        def operation() -> dict[str, int | str]:
            return role_dispatch_operation(args.home, args.seat)

    else:

        def operation() -> dict[str, int | str]:
            return presence_operation()

    summary = run_cycle(
        seat=args.seat,
        home=args.home,
        control_plane=args.control_plane,
        dry_run=args.dry_run,
        operation=operation,
    )
    print(json.dumps(_condensed_receipt_payload(asdict(summary)), sort_keys=True))
    return 0 if summary.result not in {"inactive_host_refused"} else 75


if __name__ == "__main__":
    raise SystemExit(main())
