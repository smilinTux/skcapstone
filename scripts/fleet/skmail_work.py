#!/usr/bin/env python3
"""Structured, work-scoped SKMail envelopes.

This keeps ordinary SKMail append-only while giving workers a stable,
deduplicable protocol for hello, progress, dependency waits, help, and status.
Messages are data only; this module never executes message bodies.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import uuid
import re
from collections.abc import Iterable
from pathlib import Path

WRITER = Path(__file__).with_name("skmail_writer.py")
KINDS = {"agent.hello", "agent.status", "work.progress", "work.help.request",
         "work.help.response", "dependency.wait", "dependency.changed",
         "work.complete", "work.blocked"}
IDENTITY = re.compile(r"^[A-Za-z0-9_.:@-]{1,160}$")
CARD = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def validate_envelope(payload: dict, now: dt.datetime | None = None) -> dict:
    """Validate a work envelope and its canonical hash without executing body text."""
    required = {"schema", "message_id", "thread_id", "type", "sender", "recipient",
                "card_id", "claim_revision", "created_at", "expires_at", "body", "body_hash"}
    if not isinstance(payload, dict) or not required.issubset(payload):
        raise ValueError("incomplete SKMail work envelope")
    if payload["schema"] != "skmail.work.v1" or payload["type"] not in KINDS:
        raise ValueError("unsupported SKMail work envelope")
    for key in ("message_id", "thread_id", "sender", "recipient", "claim_revision"):
        if not isinstance(payload[key], str) or not IDENTITY.fullmatch(payload[key]):
            raise ValueError(f"invalid {key}")
    if not isinstance(payload["card_id"], str) or not CARD.fullmatch(payload["card_id"]):
        raise ValueError("invalid card_id")
    if not isinstance(payload["body"], str):
        raise ValueError("body must be text")
    unsigned = dict(payload)
    supplied = unsigned.pop("body_hash")
    if not isinstance(supplied, str) or hashlib.sha256(_canonical(unsigned)).hexdigest() != supplied:
        raise ValueError("body_hash mismatch")
    current = now or dt.datetime.now(dt.timezone.utc)
    expires = dt.datetime.fromisoformat(payload["expires_at"])
    if expires <= current:
        raise ValueError("expired SKMail work envelope")
    return payload


def read_work_envelopes(records: Iterable[dict], recipient: str | None = None,
                        card_id: str | None = None, claim_revision: str | None = None,
                        now: dt.datetime | None = None) -> list[dict]:
    """Return valid, unexpired, deduplicated work messages for one claim.

    Ordinary SKMail and malformed legacy records are ignored.  A caller may
    provide the current recipient/card/revision to fence replies from stale
    workers.  Messages are sorted by creation time and message id, making a
    replay deterministic without mutating the append-only mailbox.
    """
    accepted: dict[str, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        candidate = record.get("work_envelope", record)
        if not isinstance(candidate, dict):
            continue
        try:
            validate_envelope(candidate, now=now)
        except (TypeError, ValueError, KeyError):
            continue
        if recipient and candidate["recipient"].lower() not in {recipient.lower(), "all"}:
            continue
        if card_id and candidate["card_id"] != card_id:
            continue
        if claim_revision and candidate["claim_revision"] != claim_revision:
            continue
        accepted[candidate["message_id"]] = candidate
    return sorted(accepted.values(), key=lambda item: (item["created_at"], item["message_id"]))


def envelope(kind: str, sender: str, recipient: str, card_id: str,
             claim_revision: str, body: str, thread_id: str = "") -> dict:
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    message_id = str(uuid.uuid4())
    payload = {
        "schema": "skmail.work.v1",
        "message_id": message_id,
        "thread_id": thread_id or message_id,
        "type": kind,
        "sender": sender,
        "recipient": recipient,
        "card_id": card_id,
        "claim_revision": claim_revision,
        "created_at": now,
        "expires_at": (dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=24)).isoformat(),
        "body": body,
    }
    payload["body_hash"] = hashlib.sha256(_canonical(payload)).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description="Send a structured SKMail work envelope.")
    parser.add_argument("kind", choices=("agent.hello", "agent.status", "work.progress",
                                          "work.help.request", "work.help.response",
                                          "dependency.wait", "dependency.changed",
                                          "work.complete", "work.blocked"))
    parser.add_argument("sender")
    parser.add_argument("recipient")
    parser.add_argument("card_id")
    parser.add_argument("claim_revision")
    parser.add_argument("body")
    parser.add_argument("--thread", default="")
    parser.add_argument("--boxdir", type=Path, default=Path.home() / ".skcapstone/coordination/skmail.d")
    parser.add_argument("--host", default=__import__("socket").gethostname())
    args = parser.parse_args()
    payload = envelope(args.kind, args.sender, args.recipient, args.card_id,
                       args.claim_revision, args.body, args.thread)
    import importlib.util
    spec = importlib.util.spec_from_file_location("skmail_writer", WRITER)
    writer = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(writer)
    subject = f"SKMAIL-WORK {args.kind} {args.card_id}"
    line = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = writer.append(args.boxdir, args.sender, args.recipient, "normal", subject, line, args.host)
    print(json.dumps({"message_id": payload["message_id"], "body_hash": payload["body_hash"], "line_hash": digest}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
