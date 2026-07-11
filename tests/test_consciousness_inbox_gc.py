"""Tests for consciousness_loop inbox GC — staging, retry, rescan, broadcast.

Presence-on-disk == unconsumed. The primary GC is NO LONGER "delete on submit"
(which lost the message if the async worker later failed). Instead a directed
envelope is atomically STAGED into ``sync/comms/processing/`` before submit and
deleted only after the worker SUCCESSFULLY processes it; a transient failure
leaves it in processing/ for a rescan; a poison message is deadlettered.

Broadcasts (empty recipient) are processed but LEFT on disk for the TTL prune so
co-resident agents sharing the inbox still receive them.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

from skcapstone.consciousness_loop import (
    _DEADLETTER_DIR,
    _INBOX_DIR,
    _PROCESSING_DIR,
    ConsciousnessConfig,
    ConsciousnessLoop,
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


def _inbox_file(tmp_path: Path, name: str, payload, peer: str = "jarvis") -> Path:
    inbox = tmp_path / _INBOX_DIR / peer
    inbox.mkdir(parents=True, exist_ok=True)
    msg = inbox / name
    if isinstance(payload, (dict, list)):
        msg.write_text(json.dumps(payload), encoding="utf-8")
    else:
        msg.write_text(str(payload), encoding="utf-8")
    return msg


def _directed(content="hi", **extra):
    env = {"sender": "jarvis", "recipient": "lumina", "payload": {"content": content}}
    env.update(extra)
    return env


def _deadletter_dir(tmp_path: Path) -> Path:
    return tmp_path / _DEADLETTER_DIR


def _processing_dir(tmp_path: Path) -> Path:
    return tmp_path / _PROCESSING_DIR


def _processing_files(tmp_path: Path):
    d = _processing_dir(tmp_path)
    return [p for p in d.rglob("*.skc.json")] if d.is_dir() else []


class TestDirectedStaging:
    """A directed envelope is staged out of the inbox before submit (not deleted)."""

    def test_staged_out_of_inbox_and_submitted(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "good.skc.json", _directed())

        loop._on_inbox_file(msg)

        assert loop._executor.submit.called, "expected a submit"
        assert not msg.exists(), "envelope must be moved out of the inbox (staged)"
        staged = _processing_files(tmp_path)
        assert len(staged) == 1, "envelope must live in processing/ until processed"

    def test_not_deadlettered_on_success_path(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "good2.skc.json", _directed())
        loop._on_inbox_file(msg)
        dead = _deadletter_dir(tmp_path)
        assert not dead.exists() or not any(dead.iterdir())


class TestProcessStaged:
    """The worker deletes the staged file only on SUCCESS; failures are preserved."""

    def _stage(self, loop, tmp_path, env=None):
        msg = _inbox_file(tmp_path, "m.skc.json", env or _directed())
        staged = loop._stage_for_processing(msg)
        assert staged is not None and staged.exists()
        return staged

    def test_success_deletes_staged(self, tmp_path):
        """F2(c): successful processing removes the staged file."""
        loop = _make_loop(tmp_path)
        loop.process_envelope = MagicMock(return_value="reply")  # succeeds
        staged = self._stage(loop, tmp_path)

        loop._process_staged(staged)

        assert not staged.exists(), "staged file must be deleted after success"

    def test_none_return_is_success(self, tmp_path):
        """A legit no-reply (None return, e.g. an ACK) still counts as processed."""
        loop = _make_loop(tmp_path)
        loop.process_envelope = MagicMock(return_value=None)
        staged = self._stage(loop, tmp_path)

        loop._process_staged(staged)

        assert not staged.exists()

    def test_failure_preserves_staged(self, tmp_path):
        """F2(a): a raise during processing must NOT lose the message."""
        loop = _make_loop(tmp_path)
        loop.process_envelope = MagicMock(side_effect=RuntimeError("LLM down"))
        staged = self._stage(loop, tmp_path)

        loop._process_staged(staged)

        assert staged.exists(), "transient failure must leave the file for retry"
        dead = _deadletter_dir(tmp_path)
        assert not dead.exists() or not any(dead.iterdir()), "one failure != deadletter"

    def test_poison_deadlettered_after_max_attempts(self, tmp_path):
        """A message that keeps failing is eventually deadlettered, not retried forever."""
        loop = _make_loop(tmp_path)
        loop.process_envelope = MagicMock(side_effect=RuntimeError("poison"))
        staged = self._stage(loop, tmp_path)

        from skcapstone.consciousness_loop import _MAX_PROCESS_ATTEMPTS

        for _ in range(_MAX_PROCESS_ATTEMPTS):
            if staged.exists():
                loop._process_staged(staged)

        assert not staged.exists(), "poison message removed from processing/"
        dead = _deadletter_dir(tmp_path)
        assert dead.is_dir() and any(dead.iterdir()), "poison message preserved in deadletter"


class TestSubmitFailurePreserves:
    def test_file_survives_when_submit_fails(self, tmp_path):
        """A raising submit must not lose the message (kept in processing/)."""
        loop = _make_loop(tmp_path)
        loop._executor.submit.side_effect = RuntimeError("boom")
        msg = _inbox_file(tmp_path, "boom.skc.json", _directed())

        loop._on_inbox_file(msg)

        # It was staged (moved) before submit; the staged copy survives.
        assert not msg.exists()
        assert len(_processing_files(tmp_path)) == 1, "message preserved for rescan"


class TestRescan:
    """Startup / periodic rescan recovers files the create-only watcher missed."""

    def test_preexisting_inbox_file_picked_up(self, tmp_path):
        """F2(b): a file already present at startup is submitted by the rescan."""
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "pre.skc.json", _directed())

        loop.rescan_inbox()

        assert loop._executor.submit.called, "rescan must submit a pre-existing inbox file"
        assert not msg.exists(), "rescanned inbox file is staged out"
        assert len(_processing_files(tmp_path)) == 1

    def test_processing_leftover_resubmitted(self, tmp_path):
        """A stale file left in processing/ (crash/retry) is resubmitted by rescan."""
        loop = _make_loop(tmp_path)
        proc = _processing_dir(tmp_path)
        proc.mkdir(parents=True, exist_ok=True)
        leftover = proc / "left.skc.json"
        leftover.write_text(json.dumps(_directed()), encoding="utf-8")
        old = time.time() - 3600
        os.utime(leftover, (old, old))  # older than the stale guard

        loop.rescan_inbox()

        assert loop._executor.submit.called, "stale processing file must be resubmitted"


class TestBroadcast:
    """F6: an empty-recipient broadcast is processed but LEFT on disk for the TTL prune."""

    def test_broadcast_not_removed_on_consume(self, tmp_path):
        loop = _make_loop(tmp_path)
        env = {"sender": "jarvis", "payload": {"content": "team announce"}}  # no recipient
        msg = _inbox_file(tmp_path, "bcast.skc.json", env)

        loop._on_inbox_file(msg)

        assert loop._executor.submit.called, "broadcast is still processed"
        assert msg.exists(), "broadcast must stay in the shared inbox for co-resident agents"
        assert not _processing_files(tmp_path), "broadcast is not staged for deletion"

    def test_broadcast_duplicate_left(self, tmp_path):
        loop = _make_loop(tmp_path)
        env = {"message_id": "bx1", "sender": "jarvis", "payload": {"content": "hi"}}
        m1 = _inbox_file(tmp_path, "b1.skc.json", env)
        loop._on_inbox_file(m1)
        assert m1.exists()
        m2 = _inbox_file(tmp_path, "b2.skc.json", env, peer="lumina")
        loop._on_inbox_file(m2)
        # A duplicate broadcast is also left on disk (not consumed) for other agents.
        assert m2.exists()


class TestDeadletterValidation:
    """Malformed / oversized envelopes are routed to deadletter/, not left."""

    def test_malformed_json_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "bad.skc.json", "{not valid json")

        loop._on_inbox_file(msg)

        assert not msg.exists()
        dead = _deadletter_dir(tmp_path)
        assert dead.is_dir() and (dead / "bad.skc.json").exists()
        assert not loop._executor.submit.called

    def test_oversized_envelope_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        big = {"sender": "x", "recipient": "lumina", "payload": {"content": "A" * 1_100_000}}
        msg = _inbox_file(tmp_path, "big.skc.json", big)
        assert msg.stat().st_size > 1_000_000

        loop._on_inbox_file(msg)

        assert not msg.exists()
        assert (_deadletter_dir(tmp_path) / "big.skc.json").exists()
        assert not loop._executor.submit.called

    def test_missing_sender_deadlettered(self, tmp_path):
        loop = _make_loop(tmp_path)
        msg = _inbox_file(tmp_path, "nosender.skc.json", {"payload": {"content": "hi"}})

        loop._on_inbox_file(msg)

        assert not msg.exists()
        assert (_deadletter_dir(tmp_path) / "nosender.skc.json").exists()

    def test_deadletter_collision_uses_uuid(self, tmp_path):
        """F8: two malformed files with the same name both survive in deadletter."""
        loop = _make_loop(tmp_path)
        m1 = _inbox_file(tmp_path, "dup.skc.json", "{bad")
        loop._on_inbox_file(m1)
        m2 = _inbox_file(tmp_path, "dup.skc.json", "{bad again", peer="lumina")
        loop._on_inbox_file(m2)

        dead = _deadletter_dir(tmp_path)
        files = list(dead.iterdir())
        assert len(files) == 2, f"both malformed copies kept, got {files}"


class TestDuplicateDirected:
    def test_duplicate_directed_removed(self, tmp_path):
        loop = _make_loop(tmp_path)
        env = _directed(message_id="abc123")
        first = _inbox_file(tmp_path, "first.skc.json", env)
        loop._on_inbox_file(first)
        assert not first.exists(), "first directed copy staged out"

        dup = _inbox_file(tmp_path, "dup.skc.json", env, peer="lumina")
        loop._on_inbox_file(dup)

        assert not dup.exists(), "duplicate directed envelope must be removed"


class TestProcessedIdOrdering:
    """F7: a message dropped by backpressure must NOT be marked processed."""

    def test_backpressure_drop_not_marked(self, tmp_path):
        loop = _make_loop(tmp_path)

        # Force the backpressure branch: qsize() over the threshold.
        class _FullQueue:
            def qsize(self):
                return 10_000

        loop._executor._work_queue = _FullQueue()
        loop._config.max_concurrent_requests = 1

        env = _directed(message_id="drop-me")
        msg = _inbox_file(tmp_path, "drop.skc.json", env)

        loop._on_inbox_file(msg)

        with loop._processed_ids_lock:
            assert "drop-me" not in loop._processed_ids, "dropped msg must not be marked processed"
        assert msg.exists(), "dropped message left on disk for retry/TTL"
        assert not loop._executor.submit.called

    def test_marked_processed_after_successful_stage(self, tmp_path):
        loop = _make_loop(tmp_path)
        env = _directed(message_id="keep-me")
        msg = _inbox_file(tmp_path, "keep.skc.json", env)

        loop._on_inbox_file(msg)

        with loop._processed_ids_lock:
            assert "keep-me" in loop._processed_ids
