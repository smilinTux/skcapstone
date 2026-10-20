"""Bounded recurring entrypoints for the Link and Mero control-plane seats.

The entrypoint performs the cheap, revision-pinned host fence before opening
CardStore or mediated PR input. A host-local :class:`SeatCycleGuard` then
prevents a second invocation from waiting or doing duplicate work. A missing
or invalid Link feed is recorded as an honest bounded no-op.

The paused Atlas, Seraph and Niobe dispatcher seats were retired in the
dispatcher cut; only the two seats whose units still ship are runnable here.
"""

from __future__ import annotations

import argparse
import json
import socket
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .estate import host_lifecycle_claim
from .link_cycle import recommend_one_reviewer
from .link_observation_feed import ObservationFeedError, load_observation_feed
from .link_review_work import load_review_work, reconcile_review_work_batch
from .mero_census import run_blocker_census
from .receipt_output import condense_dispatcher_output
from .seat_boundaries import BoundaryError
from .seat_cycle_guard import CycleResult, SeatCycleGuard
from .seat_mail import poll_mail, startup_hello

#: The seats this entrypoint runs. A narrower set than
#: ``lifecycle_seats.LIFECYCLE_SEATS`` (the packaged five-seat profile
#: contract) on purpose: the other three have no unit and no code path here.
_SEATS = frozenset({"link", "mero"})


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

    # Replay fence: a feed whose evidence hash was already consumed by the
    # previous cycle is refused (observation_feed_replayed), so an unchanged
    # feed cannot append the same handoffs twice. The marker is only written
    # after a feed has been accepted, and dry runs never reach this function.
    marker = home / "coordination" / "seat-cycles" / "link.last-feed-sha256"
    try:
        prior = marker.read_text(encoding="utf-8").strip() or None
    except OSError:
        prior = None
    try:
        feed = load_observation_feed(feed_path, prior_evidence_sha256=prior)
    except ObservationFeedError as exc:
        lineage = lineage_path or home / "coordination" / "link-lineage.json"
        return _emit_review_work(home, lineage, str(exc))
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(feed.evidence_sha256 + "\n", encoding="utf-8")

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

        def operation() -> dict[str, int | str]:
            return mero_operation(args.home)

    else:

        def operation() -> dict[str, int | str]:
            return link_operation(args.home, feed_path)

    summary = run_cycle(
        seat=args.seat,
        home=args.home,
        control_plane=args.control_plane,
        dry_run=args.dry_run,
        operation=operation,
    )
    print(json.dumps(_condensed_receipt_payload(asdict(summary)), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
