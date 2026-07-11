"""Tests for inbox / deadletter TTL backstop + derived-junk sweep in housekeeping.

F3 (HIGH/MED): the inbox TTL prune must target the tree the consciousness loop
actually consumes — ``{shared_root}/sync/comms/inbox/{peer}/*.skc.json``
(recursive, per-peer subdirs) — and must NOT touch ``agents/<agent>/comms/inbox``
(SKCHAT's inbox, owned by a different service).

F5 (LOW/MED): the deadletter tree (``sync/comms/deadletter``) is synced +
unbounded; it needs its own TTL prune target.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from skcapstone.housekeeping import (
    DEFAULT_DEADLETTER_MAX_AGE_HOURS,
    DEFAULT_INBOX_MAX_AGE_HOURS,
    prune_deadletter,
    prune_derived_junk,
    prune_inbox,
    run_housekeeping,
)


def _old(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def _mk_envelope(directory: Path, name: str, age_hours: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("{}", encoding="utf-8")
    _old(f, age_hours)
    return f


class TestDefaults:
    def test_inbox_ttl_is_seven_days(self):
        assert DEFAULT_INBOX_MAX_AGE_HOURS == 168

    def test_deadletter_ttl_is_seven_days(self):
        assert DEFAULT_DEADLETTER_MAX_AGE_HOURS == 168


class TestPruneInbox:
    def test_prunes_per_peer_subdirs_recursively(self, tmp_path):
        peer_a = tmp_path / "sync" / "comms" / "inbox" / "jarvis"
        peer_b = tmp_path / "sync" / "comms" / "inbox" / "lumina"
        _mk_envelope(peer_a, "old1.skc.json", 200)
        _mk_envelope(peer_b, "old2.skc.json", 200)
        fresh = _mk_envelope(peer_a, "fresh.skc.json", 1)

        deleted = prune_inbox(tmp_path)

        assert deleted == 2
        assert fresh.exists(), "fresh envelope kept"

    def test_flat_inbox_also_pruned(self, tmp_path):
        inbox = tmp_path / "sync" / "comms" / "inbox"
        env = _mk_envelope(inbox, "old.skc.json", 200)
        deleted = prune_inbox(tmp_path)
        assert deleted == 1
        assert not env.exists()

    def test_does_not_touch_skchat_agent_inbox(self, tmp_path):
        """agents/<agent>/comms/inbox is SKCHAT's — skcapstone must not TTL it."""
        skchat_inbox = tmp_path / "agents" / "lumina" / "comms" / "inbox"
        aged = _mk_envelope(skchat_inbox, "skchat.skc.json", 500)
        # Also the legacy top-level comms/inbox must NOT be swept anymore.
        legacy = _mk_envelope(tmp_path / "comms" / "inbox", "legacy.skc.json", 500)

        deleted = prune_inbox(tmp_path)

        assert deleted == 0
        assert aged.exists(), "SKCHAT inbox is owned by another service — untouched"
        assert legacy.exists(), "legacy top-level comms/inbox is not the consumed tree"

    def test_only_touches_skc_json(self, tmp_path):
        peer = tmp_path / "sync" / "comms" / "inbox" / "jarvis"
        env = _mk_envelope(peer, "old.skc.json", 200)
        other = peer / "keep.txt"
        other.write_text("x")
        _old(other, 200)

        prune_inbox(tmp_path)

        assert not env.exists()
        assert other.exists(), "non-envelope files are left alone"

    def test_no_inbox_returns_zero(self, tmp_path):
        assert prune_inbox(tmp_path) == 0


class TestPruneDeadletter:
    def test_prunes_aged_deadletter_files(self, tmp_path):
        dead = tmp_path / "sync" / "comms" / "deadletter"
        aged = _mk_envelope(dead, "bad.skc.json", 200)
        fresh = _mk_envelope(dead, "recent.skc.json", 1)

        deleted = prune_deadletter(tmp_path)

        assert deleted == 1
        assert not aged.exists()
        assert fresh.exists()

    def test_recursive(self, tmp_path):
        nested = tmp_path / "sync" / "comms" / "deadletter" / "jarvis"
        aged = _mk_envelope(nested, "bad.skc.json", 200)
        deleted = prune_deadletter(tmp_path)
        assert deleted == 1
        assert not aged.exists()

    def test_no_deadletter_returns_zero(self, tmp_path):
        assert prune_deadletter(tmp_path) == 0


class TestPruneDerivedJunk:
    def test_removes_chroma_bak_dir(self, tmp_path):
        bak = tmp_path / "memory" / "chroma.bak.20260710"
        bak.mkdir(parents=True)
        (bak / "chunk.bin").write_text("data")
        keep = tmp_path / "memory" / "chroma"
        keep.mkdir()

        removed = prune_derived_junk(tmp_path)

        assert removed >= 1
        assert not bak.exists()
        assert keep.exists(), "live chroma dir is untouched"

    def test_removes_dead_pid_files(self, tmp_path):
        # garbage / empty pids are unambiguously dead
        pid = tmp_path / "daemon.pid"
        pid.write_text("garbage")
        nested = tmp_path / "agents" / "lumina"
        nested.mkdir(parents=True)
        pid2 = nested / "consciousness.pid"
        pid2.write_text("")

        removed = prune_derived_junk(tmp_path)

        assert not pid.exists()
        assert not pid2.exists()
        assert removed >= 2

    def test_no_junk_returns_zero(self, tmp_path):
        (tmp_path / "memory").mkdir()
        assert prune_derived_junk(tmp_path) == 0


class TestRunHousekeepingIntegration:
    def test_inbox_deadletter_and_junk_registered_in_dry_run(self, tmp_path):
        inbox = tmp_path / "sync" / "comms" / "inbox" / "jarvis"
        for i in range(3):
            _mk_envelope(inbox, f"old{i}.skc.json", 200)
        dead = tmp_path / "sync" / "comms" / "deadletter"
        _mk_envelope(dead, "bad.skc.json", 200)
        (tmp_path / "run.pid").write_text("garbage")
        bak = tmp_path / "memory" / "chroma.bak"
        bak.mkdir(parents=True)

        results = run_housekeeping(
            skcapstone_home=tmp_path,
            skcomms_home=tmp_path / ".skcomms",
            dry_run=True,
        )

        assert results["inbox"]["would_delete"] == 3
        assert results["deadletter"]["would_delete"] == 1
        assert results["derived_junk"]["would_delete"] >= 2
        # Dry run deletes nothing.
        assert (inbox / "old0.skc.json").exists()
        assert bak.exists()

    def test_full_run_prunes_inbox_deadletter_and_junk(self, tmp_path):
        inbox = tmp_path / "sync" / "comms" / "inbox" / "jarvis"
        for i in range(4):
            _mk_envelope(inbox, f"old{i}.skc.json", 200)
        _mk_envelope(inbox, "fresh.skc.json", 1)
        dead = tmp_path / "sync" / "comms" / "deadletter"
        _mk_envelope(dead, "bad.skc.json", 200)
        (tmp_path / "x.pid").write_text("")

        results = run_housekeeping(
            skcapstone_home=tmp_path,
            skcomms_home=tmp_path / ".skcomms",
            dry_run=False,
        )

        assert results["inbox"]["deleted"] == 4
        assert results["deadletter"]["deleted"] == 1
        assert results["derived_junk"]["deleted"] >= 1
        assert (inbox / "fresh.skc.json").exists()
        assert results["summary"]["total_deleted"] >= 6
