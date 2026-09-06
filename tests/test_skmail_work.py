from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path

PATH = Path(__file__).parents[1] / "scripts/fleet/skmail_work.py"
SPEC = importlib.util.spec_from_file_location("skmail_work", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MODULE)


def test_envelope_is_work_scoped_and_hashed() -> None:
    msg = MODULE.envelope("work.help.request", "worker", "jarvis", "abc12345", "rev1", "need help")
    assert msg["schema"] == "skmail.work.v1"
    assert msg["card_id"] == "abc12345"
    assert msg["claim_revision"] == "rev1"
    assert len(msg["message_id"]) == 36
    assert len(msg["body_hash"]) == 64


def test_envelope_body_is_data_not_executable() -> None:
    msg = MODULE.envelope(
        "work.progress", "worker", "jarvis", "abc12345", "rev1", "$(touch pwned)"
    )
    assert json.dumps(msg, sort_keys=True).find("touch pwned") >= 0


def test_validate_accepts_canonical_hash_and_rejects_tamper() -> None:
    msg = MODULE.envelope("work.progress", "worker", "jarvis", "abc12345", "rev1", "phase 1")
    assert MODULE.validate_envelope(msg) == msg
    msg["body"] = "phase 2"
    try:
        MODULE.validate_envelope(msg)
    except ValueError as exc:
        assert "hash" in str(exc)
    else:
        raise AssertionError("tampered envelope accepted")


def test_reader_fences_expired_stale_and_duplicate_messages() -> None:
    now = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
    first = MODULE.envelope("work.progress", "worker", "jarvis", "card-1", "rev-1", "one")
    first["created_at"] = "2025-12-31T23:00:00+00:00"
    first["expires_at"] = "2026-01-01T01:00:00+00:00"
    unsigned = dict(first)
    unsigned.pop("body_hash")
    first["body_hash"] = hashlib.sha256(MODULE._canonical(unsigned)).hexdigest()
    stale = MODULE.envelope("work.progress", "worker", "jarvis", "card-1", "old", "stale")
    wrong_card = MODULE.envelope("work.progress", "worker", "jarvis", "other", "rev-1", "other")
    result = MODULE.read_work_envelopes(
        [first, first, stale, wrong_card],
        recipient="jarvis",
        card_id="card-1",
        claim_revision="rev-1",
        now=now,
    )
    assert [item["body"] for item in result] == ["one"]


def test_reader_never_executes_body_text() -> None:
    message = MODULE.envelope(
        "work.help.request",
        "worker",
        "jarvis",
        "card-1",
        "rev-1",
        "__import__('os').system('touch /tmp/nope')",
    )
    result = MODULE.read_work_envelopes([message], recipient="jarvis")
    assert result[0]["body"].startswith("__import__")
