"""Fail-closed pi lane adapter for SKRSI.

Two measurement families, each from its own authority:

1. Per-turn token usage from a ``pi --mode json`` byte stream. The
   authoritative final usage is the completed assistant message carried by
   ``turn_end.message`` (identical to ``message_end.message``); cumulative
   ``message_update`` records are never summed.
2. Lane report verdict and rounds-to-accept, read back from the authoritative
   skcapstone fold, never synthesized from the pi stream itself.

Deliberately NOT wired into ``SKRSIRuntime.collect``: this is a standalone
adapter, so a lane report is ingested exactly once (RV finding 5). The
``append_metric_event`` primitive from the retired ``skrsi_estate_adapters``
seat code is replaced inline so this module stays self-contained.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping

from .skrsi_registry import AppendOnlyOutbox, SKRSIError, make_record

ACTOR = "pi-lane-adapter"

# (usage key, event type, metric name, unit)
TOKEN_METRICS = (
    ("input", "pi.turn.input", "skrsi.pi.turn.input_tokens", "token"),
    ("output", "pi.turn.output", "skrsi.pi.turn.output_tokens", "token"),
    ("cacheRead", "pi.turn.cache_read", "skrsi.pi.turn.cache_read_tokens", "token"),
    ("cacheWrite", "pi.turn.cache_write", "skrsi.pi.turn.cache_write_tokens", "token"),
)
VERDICT_METRIC = "skrsi.pi.lane.report.accept"
ROUNDS_METRIC = "skrsi.pi.lane.rounds_to_accept"
VERDICT_EVENT = "lane.report.verdict"
ROUNDS_EVENT = "lane.report.rounds"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")

# The governed review-card fold vocabulary is PASS / FAIL / BLOCKED. The
# independent reviewer's ACCEPT/REJECT verdict folds to PASS/FAIL, so ACCEPT
# maps to PASS (value 1) and REJECT maps to FAIL or BLOCKED (value 0). Round 1
# tested "ACCEPT" against the fold, which never contains that literal, so every
# terminal PASS was recorded as 0 (RV finding 2). This mapping is the fix.
_FOLD_ACCEPT = frozenset({"PASS"})
_FOLD_REJECT = frozenset({"FAIL", "BLOCKED"})


class PiAdapterError(SKRSIError):
    """A malformed or unusable pi stream or fold input."""


@dataclass(frozen=True)
class PiUsage:
    input: int
    output: int
    cache_read: int
    cache_write: int


@dataclass(frozen=True)
class PiTurn:
    session_id: str
    index: int
    occurred_at: str
    usage: PiUsage


@dataclass(frozen=True)
class RejectedRecord:
    line: int
    reason: str


def _unix_ms_to_iso(value: object) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise PiAdapterError("message timestamp must be a non-negative unix-ms number")
    return (
        datetime.fromtimestamp(value / 1000, tz=timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def _usage_from(value: object, *, line: int) -> PiUsage | RejectedRecord:
    if not isinstance(value, Mapping):
        return RejectedRecord(line, "turn usage is not an object")
    fields: dict[str, int] = {}
    for source_key, field in (
        ("input", "input"),
        ("output", "output"),
        ("cacheRead", "cache_read"),
        ("cacheWrite", "cache_write"),
    ):
        raw = value.get(source_key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
            return RejectedRecord(line, f"usage.{source_key} must be a non-negative integer")
        fields[field] = raw
    return PiUsage(**fields)


def _turn_from_record(
    record: Mapping[str, Any], *, session_id: str, index: int, line: int
) -> PiTurn | RejectedRecord:
    message = record.get("message")
    if not isinstance(message, Mapping):
        return RejectedRecord(line, "turn_end has no message")
    try:
        occurred_at = _unix_ms_to_iso(message.get("timestamp"))
    except PiAdapterError as exc:
        return RejectedRecord(line, str(exc))
    usage = _usage_from(message.get("usage"), line=line)
    if isinstance(usage, RejectedRecord):
        return usage
    return PiTurn(session_id=session_id, index=index, occurred_at=occurred_at, usage=usage)


def parse_turns(
    jsonl: bytes | str,
) -> tuple[tuple[PiTurn, ...], tuple[RejectedRecord, ...]]:
    """Split a pi JSONL byte stream on LF and return completed turns.

    Returns ``(turns, rejected)``. A truncated line, a non-object record, or a
    turn with a missing or negative usage field is rejected rather than zeroed
    or guessed. The session header supplies the session id; every ``turn_end``
    record is one turn and its ``message.usage`` is the authoritative final
    usage (never the sum of ``message_update`` deltas).
    """
    if isinstance(jsonl, str):
        jsonl = jsonl.encode("utf-8")
    turns: list[PiTurn] = []
    rejected: list[RejectedRecord] = []
    session_id = ""
    turn_index = 1
    for line_number, raw_line in enumerate(jsonl.split(b"\n"), start=1):
        line = raw_line.rstrip(b"\r")
        if not line.strip():
            continue
        try:
            record = json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            rejected.append(RejectedRecord(line_number, "line is not JSON"))
            continue
        if not isinstance(record, dict):
            rejected.append(RejectedRecord(line_number, "record is not an object"))
            continue
        record_type = record.get("type")
        if record_type == "session":
            session_id = str(record.get("id", ""))
            continue
        if record_type == "turn_end":
            turn = _turn_from_record(
                record, session_id=session_id, index=turn_index, line=line_number
            )
            if isinstance(turn, PiTurn):
                turns.append(turn)
                turn_index += 1
            else:
                rejected.append(turn)
    return tuple(turns), tuple(rejected)


def _now_iso(now: datetime | None) -> str:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("now must include a timezone")
    return now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _append_observation(
    outbox: AppendOnlyOutbox,
    *,
    event_id: str,
    event_type: str,
    occurred_at: str,
    recorded_at: str,
    value: int,
    unit: str,
    source: str,
    target_ref: str,
    revision: str,
    cohort: str,
    metric: str,
) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SKRSIError("metric value must be numeric")
    record = make_record(
        "Observation",
        actor=ACTOR,
        target_ref=target_ref,
        event_id=event_id,
        event_type=event_type,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        payload={
            "value": value,
            "unit": unit,
            "source": source,
            "cohort": cohort,
            "sample_id": event_id,
            "collection_quality": "complete",
            "metric": metric,
            "target_revision": revision,
        },
    )
    if not any(entry.idempotency_key == record.idempotency_key for entry in outbox.entries()):
        outbox.append(record)
    return record.idempotency_key


def collect_turns(
    outbox: AppendOnlyOutbox,
    turns: tuple[PiTurn, ...] | list[PiTurn],
    *,
    target_ref: str,
    target_revision: str = "1",
    now: datetime | None = None,
) -> int:
    """Emit four token measurements per turn and return the observation count."""
    recorded_at = _now_iso(now)
    emitted = 0
    for turn in turns:
        values = {
            "input": turn.usage.input,
            "output": turn.usage.output,
            "cacheRead": turn.usage.cache_read,
            "cacheWrite": turn.usage.cache_write,
        }
        for key, event_type, metric, unit in TOKEN_METRICS:
            _append_observation(
                outbox,
                event_id=f"pi:turn:{turn.session_id}:{turn.index}:{key}",
                event_type=event_type,
                occurred_at=turn.occurred_at,
                recorded_at=recorded_at,
                value=values[key],
                unit=unit,
                source="pi",
                target_ref=target_ref,
                revision=target_revision,
                cohort=f"pi-lane:{turn.session_id}",
                metric=metric,
            )
            emitted += 1
    return emitted


def _fold_value(folded_verdict: str) -> int:
    if folded_verdict in _FOLD_ACCEPT:
        return 1
    if folded_verdict in _FOLD_REJECT:
        return 0
    raise PiAdapterError(f"unknown fold verdict: {folded_verdict!r}")


def record_lane_verdict(
    outbox: AppendOnlyOutbox,
    *,
    lane_id: str,
    evidence_sha256: str,
    folded_verdict: str,
    review_rounds: int,
    occurred_at: str,
    recorded_at: str,
    target_ref: str,
    target_revision: str = "1",
) -> tuple[str, str]:
    """Emit the verdict and rounds observations for one governed lane report.

    ``folded_verdict`` is the terminal governed review-card outcome (PASS, FAIL,
    or BLOCKED), never a self-typed value from the pi stream. PASS maps to
    accept value 1; FAIL and BLOCKED map to 0. ``source`` is the review
    authority ``seraph``, not the lane itself.
    """
    value = _fold_value(folded_verdict)
    if isinstance(review_rounds, bool) or not isinstance(review_rounds, int) or review_rounds < 1:
        raise PiAdapterError("review_rounds must be a positive integer")
    if not _SHA256.fullmatch(evidence_sha256):
        raise PiAdapterError("evidence_sha256 must be lowercase 64-hex")
    verdict_key = _append_observation(
        outbox,
        event_id=f"pi:lane:{lane_id}:verdict:{evidence_sha256}",
        event_type=VERDICT_EVENT,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        value=value,
        unit="1",
        source="seraph",
        target_ref=target_ref,
        revision=target_revision,
        cohort=f"pi-lane:{lane_id}",
        metric=VERDICT_METRIC,
    )
    rounds_key = _append_observation(
        outbox,
        event_id=f"pi:lane:{lane_id}:rounds:{evidence_sha256}",
        event_type=ROUNDS_EVENT,
        occurred_at=occurred_at,
        recorded_at=recorded_at,
        value=review_rounds,
        unit="{round}",
        source="seraph",
        target_ref=target_ref,
        revision=target_revision,
        cohort=f"pi-lane:{lane_id}",
        metric=ROUNDS_METRIC,
    )
    return verdict_key, rounds_key
