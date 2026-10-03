"""Pure helpers of the Mero blocker census.

Card 8fa7d8eb moved these verbatim from the single-module layout of card
2516480b. Timestamp parsing, id normalization, verdict-head classification,
canonical digests, and the serializer/parser pair for recommendation lines.
Nothing here reads the board or holds state.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from ._constants import _BLOCKED_RE, _PROVISIONAL_PASS_RE

__all__ = [
    "_now",
    "_parse_ts",
    "_norm_id",
    "_verdict_head",
    "_is_block",
    "_is_pass_token",
    "_canonical_digest",
    "_generation_key",
    "_event_ref",
    "recommendation_event_to_json",
    "parse_recommendation_line",
    "_SHA256_RE",
    "_GENERATION_VERSION",
]

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

#: Deterministic findings digest under this prefix; bump to re-key findings.
_GENERATION_VERSION = "mrc1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(value: object) -> datetime | None:
    """Parse the store's ISO-8601 stamps; None when absent or malformed."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp


def _norm_id(value: object) -> str:
    """Normalise a card id reference to the store's lowercase hex shape."""
    text = re.sub(r"[^0-9a-fA-F]", "", str(value or ""))
    return text.lower()


def _verdict_head(value: str) -> str:
    """The leading token of a verdict, uppercased.

    A PASS routinely explains what it supersedes, so it can contain the word
    BLOCKED in prose. Only the leading token is the verdict.
    """
    if _PROVISIONAL_PASS_RE.match(str(value or "").strip().upper()):
        return str(value or "").strip().split()[0].upper()
    head = str(value or "").strip()
    for sep in ("|", ";", ":", ",", "."):
        head = head.split(sep)[0]
    return head.strip().split()[0].upper() if head.strip() else ""


def _is_block(value: str) -> bool:
    return bool(_BLOCKED_RE.match(str(value or "")))


def _is_pass_token(value: str) -> bool:
    head = _verdict_head(value)
    if not head.startswith("PASS"):
        return False
    # A PASS_FOR_REVIEW has not cleared its own independent review yet, so it
    # cannot contradict or discharge a block. Only a completed pass can.
    return not _PROVISIONAL_PASS_RE.match(head)


def _canonical_digest(payload: object) -> str:
    """SHA-256 over canonical JSON of ``payload``."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str).encode()
    return hashlib.sha256(raw).hexdigest()


def _generation_key(*parts: object) -> str:
    """A short deterministic key over the authoritative inputs of a finding."""
    return _canonical_digest([_GENERATION_VERSION, *(str(p) for p in parts)])[:32]


def _event_ref(event: dict) -> dict:
    """The identifying projection of one source event."""
    return {
        "event_id": str(event.get("event_id") or ""),
        "ts": str(event.get("ts") or ""),
        "action": str(event.get("action") or ""),
        "writer": str(event.get("writer") or ""),
        "seq": event.get("seq"),
    }


def recommendation_event_to_json(event: dict) -> str:
    """Serialize one recommendation event line. Never concatenate JSON."""
    return json.dumps(event, sort_keys=True, separators=(",", ":"), default=str)


def parse_recommendation_line(line: str) -> dict:
    """Parse one recommendation event line, rejecting non-JSON input."""
    event = json.loads(line)
    if not isinstance(event, dict):
        raise ValueError("recommendation line is not a JSON object")
    return event
