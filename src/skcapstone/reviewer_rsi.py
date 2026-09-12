"""Deterministic, read-only reviewer quality projection."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median
from typing import Any, Callable, Iterable

from skcoord.card_store import CardStore

SCHEMA = "skfleet.reviewer-rsi/v1"
LOGICAL_BUCKETS = ("sk-s", "sk-m", "sk-l", "sk-xl")
DEFAULT_EXCLUSIONS = frozenset({"273845dc", "72c101a1", "89852b23"})
PASS = frozenset({"PASS", "PASS_FOR_REVIEW", "APPROVED"})
FAIL_CLOSED = frozenset({"FAIL", "FAIL_CLOSED", "BLOCKED"})


def _event_hash(event: dict[str, Any]) -> str:
    """Return a stable reference to one parsed immutable event."""
    payload = json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _iso_seconds(start: str, end: str) -> float | None:
    """Return elapsed seconds or unavailable for malformed timestamps."""
    try:
        return (
            datetime.fromisoformat(end.replace("Z", "+00:00"))
            - datetime.fromisoformat(start.replace("Z", "+00:00"))
        ).total_seconds()
    except (TypeError, ValueError):
        return None


def _valid_iso(value: Any) -> bool:
    """Return whether a value is an ISO-8601 timestamp."""
    if not isinstance(value, str) or not value:
        return False
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return True


def _metric(numerator: int, denominator: int) -> dict[str, Any]:
    """Build a ratio without turning a missing denominator into zero."""
    if denominator == 0:
        return {"state": "unavailable", "reason": "no_adjudicated_observations"}
    return {
        "state": "available",
        "numerator": numerator,
        "denominator": denominator,
        "value": numerator / denominator,
    }


def _bucket(labels: Iterable[str]) -> str | None:
    """Resolve exactly one ordinary logical bucket."""
    found = sorted(set(labels).intersection(LOGICAL_BUCKETS))
    return found[0] if len(found) == 1 else None


def _verdict(event: dict[str, Any]) -> str:
    """Normalize an explicitly recorded outcome."""
    return str(event.get("verdict") or event.get("value") or "").strip().upper()


def _is_hashed(event: dict[str, Any]) -> bool:
    """Require an exact lowercase SHA-256 evidence reference."""
    digest = event.get("evidence_sha256")
    return (
        isinstance(digest, str)
        and len(digest) == 64
        and all(c in "0123456789abcdef" for c in digest)
    )


def project_reviewer_rsi(
    cards: Iterable[Any],
    event_reader: Callable[[str], list[dict[str, Any]]],
    *,
    excluded_card_ids: Iterable[str] = DEFAULT_EXCLUSIONS,
) -> dict[str, Any]:
    """Project reviewer quality from immutable card events and evidence hashes.

    The projection emits no writer, node, host, provider, route, or endpoint
    values. Reviewers are grouped only by the card's single logical size bucket.
    """
    excluded = frozenset(excluded_card_ids)
    rows: list[dict[str, Any]] = []
    findings: dict[str, list[dict[str, Any]]] = defaultdict(list)
    all_timestamps: list[str] = []

    materialized = sorted(cards, key=lambda card: card.id)
    known_card_ids = {card.id for card in materialized} - excluded
    events_by_card = {
        card.id: event_reader(card.id) for card in materialized if card.id not in excluded
    }
    for card in materialized:
        if card.id in excluded:
            continue
        events = events_by_card[card.id]
        for event in events:
            ts = event.get("ts")
            if _valid_iso(ts):
                all_timestamps.append(ts)
            if event.get("action") == "review_finding" and _is_hashed(event):
                source_review = str(event.get("source_review_card") or "")
                if source_review and source_review not in excluded:
                    findings[source_review].append(event)

    for card in materialized:
        if card.id in excluded:
            continue
        events = events_by_card[card.id]
        labels = list(card.labels)
        logical_bucket = _bucket(labels)
        is_review = "review" in labels or "independent-review" in labels
        if not is_review:
            continue

        claims = [event for event in events if event.get("action") in {"claim", "assign"}]
        verdicts = [
            event
            for event in events
            if event.get("action") in {"verdict", "blocked"} and _verdict(event)
        ]
        superseded = any(event.get("action") == "supersede" for event in events)
        hashed_verdicts = [event for event in verdicts if _is_hashed(event)]
        unique = {_verdict(event) for event in hashed_verdicts}
        row: dict[str, Any] = {
            "card_id": card.id,
            "bucket": logical_bucket,
            "state": "available",
            "event_refs": [_event_hash(event) for event in hashed_verdicts],
            "evidence_sha256": sorted({event["evidence_sha256"] for event in hashed_verdicts}),
        }
        if logical_bucket is None:
            row.update(state="unavailable", reason="missing_or_ambiguous_logical_bucket")
        elif superseded:
            row.update(state="unavailable", reason="superseded_review")
        elif not verdicts:
            row.update(state="unavailable", reason="missing_verdict")
        elif len(hashed_verdicts) != len(verdicts):
            row.update(state="unavailable", reason="unhashed_evidence")
        elif len(unique) != 1:
            row.update(state="unavailable", reason="conflicting_verdicts")
        else:
            first = hashed_verdicts[0]
            row["first_verdict"] = _verdict(first)
            row["review_key"] = str(first.get("review_key") or card.id)
            row["stale_feed_block"] = (
                row["first_verdict"] == "BLOCKED"
                and "stale" in str(first.get("reason") or "").lower()
                and "feed" in str(first.get("reason") or "").lower()
            )
            row["reproduced"] = (
                first.get("reproduced") if isinstance(first.get("reproduced"), bool) else None
            )
            if claims:
                row["latency_seconds"] = _iso_seconds(
                    str(claims[0].get("ts") or ""), str(first.get("ts") or "")
                )
            else:
                row["latency_seconds"] = None

            adjudications = sorted(
                findings.get(card.id, []),
                key=lambda event: (event.get("ts", ""), event.get("event_id", "")),
            )
            if adjudications:
                final = adjudications[-1]
                row["confirmed_verdict"] = _verdict(final)
                row["finding_event_ref"] = _event_hash(final)
                row["finding_evidence_sha256"] = final["evidence_sha256"]
                row["failure_class"] = str(final.get("failure_class") or "")
                owner_card = str(final.get("owner_card_id") or "")
                row["owner_card_id"] = owner_card if owner_card in known_card_ids else ""
        rows.append(row)

    by_bucket: dict[str, dict[str, Any]] = {}
    for bucket in LOGICAL_BUCKETS:
        bucket_rows = [row for row in rows if row.get("bucket") == bucket]
        usable = [row for row in bucket_rows if row["state"] == "available"]
        adjudicated = [row for row in usable if row.get("confirmed_verdict")]
        correct = sum(row["first_verdict"] == row["confirmed_verdict"] for row in adjudicated)
        false_pass = sum(
            row["first_verdict"] in PASS and row["confirmed_verdict"] in FAIL_CLOSED
            for row in adjudicated
        )
        false_closed = sum(
            row["first_verdict"] in FAIL_CLOSED and row["confirmed_verdict"] in PASS
            for row in adjudicated
        )
        reruns = [row for row in usable if row.get("reproduced") is not None]
        latency = [
            row["latency_seconds"] for row in usable if row.get("latency_seconds") is not None
        ]
        keys = Counter(row["review_key"] for row in usable)
        failure_counts = Counter(
            row.get("failure_class") for row in adjudicated if row.get("failure_class")
        )

        proposals = []
        for failure_class, count in sorted(failure_counts.items()):
            if count < 2:
                continue
            owners = sorted(
                {
                    row.get("owner_card_id")
                    for row in adjudicated
                    if row.get("failure_class") == failure_class
                }
                - {"", None}
            )
            proposals.append(
                {
                    "failure_class": failure_class,
                    "occurrences": count,
                    "state": "proposed" if owners else "unavailable",
                    "existing_owner_cards": owners,
                    "reason": None if owners else "existing_owner_card_not_found",
                }
            )

        by_bucket[bucket] = {
            "attempts": len(bucket_rows),
            "available_attempts": len(usable),
            "unavailable": Counter(
                row.get("reason") for row in bucket_rows if row["state"] == "unavailable"
            ),
            "reproducibility": _metric(
                sum(row["reproduced"] is True for row in reruns), len(reruns)
            ),
            "first_pass_accuracy": _metric(correct, len(adjudicated)),
            "false_pass": _metric(false_pass, len(adjudicated)),
            "false_fail_closed": _metric(false_closed, len(adjudicated)),
            "latency_seconds": (
                {"state": "available", "count": len(latency), "median": median(latency)}
                if latency
                else {"state": "unavailable", "reason": "missing_claim_or_verdict_time"}
            ),
            "stale_feed_blocks": sum(row.get("stale_feed_block") is True for row in usable),
            "duplicate_assignments": sum(count - 1 for count in keys.values() if count > 1),
            "repair_recurrence": {
                key: value for key, value in sorted(failure_counts.items()) if value > 1
            },
            "improvement_proposals": proposals,
        }

    return {
        "schema": SCHEMA,
        "observation_window": {
            "state": "available" if all_timestamps else "unavailable",
            "start": min(all_timestamps) if all_timestamps else None,
            "end": max(all_timestamps) if all_timestamps else None,
        },
        "excluded_card_ids": sorted(excluded),
        "buckets": by_bucket,
        "source": {"card_count": len(materialized), "review_attempt_count": len(rows)},
    }


def project_from_card_store(home: Path) -> dict[str, Any]:
    """Read the authoritative CardStore projection without mutating it."""
    store = CardStore(home)
    cards = store.list_cards(include_archived=True)
    return project_reviewer_rsi(cards, store._read_events)  # noqa: SLF001
