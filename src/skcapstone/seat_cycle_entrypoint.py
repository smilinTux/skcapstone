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
import socket
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from skcoord.card_store import CardStore

from .link_cycle import recommend_one_reviewer
from .link_observation_feed import ObservationFeedError, load_observation_feed
from .link_review_work import load_review_work, reconcile_review_work_batch
from .mero_census import run_blocker_census
from .seat_boundaries import BoundaryError
from .seat_cycle_guard import CycleResult, SeatCycleGuard
from .seat_mail import poll_mail, startup_hello

_SEATS = frozenset({"link", "mero", "seraph"})
_LAUNCH = re.compile(
    r"^(?P<outcome>LAUNCHED|LAUNCH_FAILED)\|(?P<host>[^|]+)\|(?P<session>[^|]+)\|(?P<card>[^|]+)"
    r"\|lane=(?P<lane>[^|]+)\|model=(?P<model>[^|]+)"
    r"\|owner=(?P<owner>[^|]+)\|claim_revision=(?P<revision>[^|]+)$"
)
_MAX_SERAPH_BATCH = 8
_NOOP = re.compile(
    r"^NOOP_RECEIPT\|(?P<host>[^|]+)\|reason=(?P<reason>[^|]+)" r"\|seat=(?P<seat>[^|]+)$"
)


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
    reason: str | None = None
    source_revision: str | None = None
    evidence_sha256: str | None = None
    mailbox_poll_at: str | None = None
    mailbox_ok: bool = False
    mailbox_new_messages: int = 0
    mailbox_help_or_handoff: int = 0
    mailbox_digest: str | None = None
    mailbox_error: str | None = None


def load_control_plane(path: Path) -> dict[str, Any]:
    """Read and validate the small public active-host control record."""

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
    return value


def _health_path(home: Path, seat: str) -> Path:
    return home / "coordination" / "seat-cycles" / f"{seat}.health.jsonl"


def _append_health(home: Path, summary: CycleSummary) -> None:
    path = _health_path(home, summary.seat)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"at": _now(), **asdict(summary)}
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
    """Run one fenced Link or Mero cycle, or record a bounded no-op."""

    seat = seat.strip().lower()
    if seat not in _SEATS:
        raise ValueError(f"unsupported recurring seat: {seat}")
    host = (local_host or socket.gethostname()).strip().lower()
    control = load_control_plane(control_plane)
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
            reason=str(reason) if reason is not None else None,
            source_revision=(
                str(values["source_revision"]) if values.get("source_revision") else None
            ),
            evidence_sha256=(
                str(values["evidence_sha256"]) if values.get("evidence_sha256") else None
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


def verify_seraph_dispatch(
    home: Path,
    completed: subprocess.CompletedProcess[str],
) -> dict[str, int | str]:
    """Verify every selector result independently and report partial outcomes."""

    launches = [
        match.groupdict()
        for line in completed.stdout.splitlines()
        if (match := _LAUNCH.fullmatch(line.strip()))
    ]
    noops = [
        match.groupdict()
        for line in completed.stdout.splitlines()
        if (match := _NOOP.fullmatch(line.strip()))
    ]
    if completed.returncode != 0:
        return {
            "cards_examined": 0,
            "recommendations": 0,
            "suppressed": 1,
            "reason": "seraph_dispatch_failed",
        }
    if not launches and len(noops) == 1 and noops[0]["seat"] == "seraph":
        reason = noops[0]["reason"]
        if reason in {"no_eligible_work", "no_available_capacity"}:
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
    succeeded = failed = invalid = 0
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
        model_allowed = (launch["lane"] == "codex" and launch["model"] == "sk-codex-mid") or (
            launch["lane"] == "escalate"
            and launch["model"] == os.environ.get("SKFLEET_ESC_MODEL", "gpt-5.6-sol")
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
            and producer != launch["owner"]
            and producer != recommendations[0].get("reviewer")
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
        elif card.owner is None:
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
        "reason": reason,
    }


def seraph_operation(home: Path) -> dict[str, int | str]:
    """Launch one configurable, bounded Seraph review batch."""

    dispatcher = Path.home() / ".local/bin/skfleet-rotate.py"
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
            "SKFLEET_SEAT_TARGET": str(batch_size),
            "SKFLEET_CODEX_MODEL_S": "sk-codex-mid",
            "SKFLEET_QWEN_TARGET": "0",
            "SKFLEET_GLM_TARGET": "0",
            "SKFLEET_KIMI_TARGET": "0",
            "SKFLEET_MAX_LAUNCH": str(batch_size),
        }
    )
    completed = subprocess.run(
        [str(dispatcher), "--go"], env=env, capture_output=True, text=True, timeout=240
    )
    return verify_seraph_dispatch(home, completed)


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
    print(json.dumps(asdict(summary), sort_keys=True))
    return 0 if summary.result not in {"inactive_host_refused"} else 75


if __name__ == "__main__":
    raise SystemExit(main())
