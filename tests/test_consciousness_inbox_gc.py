"""Tests for C1 — consciousness_loop inbox GC (delete-on-consume + deadletter).

Presence-on-disk == unconsumed. A successfully-submitted envelope MUST be
removed from the inbox; a malformed/oversized envelope MUST be routed out to a
``deadletter/`` sibling so it is neither re-scanned nor left to pile up.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from skcapstone.consciousness_loop import (
    ConsciousnessConfig,
    ConsciousnessLoop,
    _DEADLETTER_DIR,
    _INBOX_DIR,
)


def _make_loop(tmp_path: Path) -> ConsciousnessLoop:
    """Build a loop rooted at *tmp_path* with a fake executor."""
    config = ConsciousnessConfig(fallback_chain=["passthrough"])
    loop = ConsciousnessLoop(
        config,
        home=tmp_path / ".skcapstone",
        shared_root=tmp_path,
    )
    # Do not run real work — a truthy MagicMock stands in for a Future.
    loop._executor = MagicMock()
    return loop


def _inbox_file(tmp_path: Path, name: str, payload) -> Path:
    inbox = tmp_path / _INBOX_DIR
    inbox.mkdir(parents=True, exist_ok=True)
    msg = inbox / name
    if isinstance(payload, (dict, list)):
        msg.write_text(json.dumps(payload), encoding="utf-8")
    else:
        msg.write_text(str(payload), encoding="utf-8")
    return msg


def _deadletter_dir(tmp_path: Path) -> Path:
    return tmp_path / _DEADLETTER_DIR


class TestDeleteOnConsume:
    """A submitted envelope is removed from the inbox."""

    def test_consumed_file_is_removed_after_submit(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(
            tmp_path,
            "good.skc.json",
            {"sender": "jarvis", "payload": {"content": "hi", "content_type": "text"}},
        )

        loop._on_inbox_file(msg)

        assert loop._executor.submit.called, "expected process_envelope submit"
        assert not msg.exists(), "consumed envelope must be deleted from inbox"

    def test_not_deadlettered_on_success(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(
            tmp_path,
            "good2.skc.json",
            {"sender": "jarvis", "payload": {"content": "hi"}},
        )

        loop._on_inbox_file(msg)

        # A successful consume must not leave a deadletter copy behind.
        dead = _deadletter_dir(tmp_path)
        assert not dead.exists() or not any(dead.iterdir())

    def test_file_survives_when_submit_fails(self, tmp_path):
        """If submit raises, the file is left on disk (not consumed, not lost)."""
        loop = _make_loop(tmp_path)
        loop._executor.submit.side_effect = RuntimeError("boom")
        msg = _inbox_file(
            tmp_path,
            "boom.skc.json",
            {"sender": "jarvis", "payload": {"content": "hi"}},
        )

        loop._on_inbox_file(msg)

        assert msg.exists(), "file must survive a failed submit for retry/TTL"


class TestDeadletter:
    """Malformed / oversized envelopes are routed to deadletter/, not left."""

    def test_malformed_json_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "bad.skc.json", "{not valid json")

        loop._on_inbox_file(msg)

        assert not msg.exists(), "malformed file removed from inbox"
        dead = _deadletter_dir(tmp_path)
        assert dead.is_dir()
        assert (dead / "bad.skc.json").exists()
        assert not loop._executor.submit.called

    def test_non_dict_envelope_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "list.skc.json", ["not", "a", "dict"])

        loop._on_inbox_file(msg)

        assert not msg.exists()
        assert (_deadletter_dir(tmp_path) / "list.skc.json").exists()
        assert not loop._executor.submit.called

    def test_oversized_envelope_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        big = {"sender": "x", "payload": {"content": "A" * 1_100_000}}
        msg = _inbox_file(tmp_path, "big.skc.json", big)
        assert msg.stat().st_size > 1_000_000

        loop._on_inbox_file(msg)

        assert not msg.exists(), "oversized file removed from inbox"
        assert (_deadletter_dir(tmp_path) / "big.skc.json").exists()
        assert not loop._executor.submit.called

    def test_missing_sender_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "nosender.skc.json", {"payload": {"content": "hi"}})

        loop._on_inbox_file(msg)

        assert not msg.exists()
        assert (_deadletter_dir(tmp_path) / "nosender.skc.json").exists()

    def test_deadletter_name_collision_preserved(self, tmp_path):
        """Two malformed files with the same name both survive in deadletter."""
        loop = _make_loop(tmp_path)
        m1 = _inbox_file(tmp_path, "dup.skc.json", "{bad")
        loop._on_inbox_file(m1)
        m2 = _inbox_file(tmp_path, "dup.skc.json", "{bad again")
        loop._on_inbox_file(m2)

        dead = _deadletter_dir(tmp_path)
        files = list(dead.iterdir())
        assert len(files) == 2, f"both malformed copies kept, got {files}"


class TestDuplicateConsumed:
    """A duplicate (already-processed message_id) is removed, not left to pile up."""

    def test_duplicate_removed(self, tmp_path):
        loop = _make_loop(tmp_path)
        env = {"message_id": "abc123", "sender": "jarvis", "payload": {"content": "hi"}}
        first = _inbox_file(tmp_path, "first.skc.json", env)
        loop._on_inbox_file(first)
        assert not first.exists()

        dup = _inbox_file(tmp_path, "dup.skc.json", env)
        loop._on_inbox_file(dup)

        assert not dup.exists(), "duplicate envelope must be removed"
