"""Tests for C2 — inbox TTL backstop + derived-junk sweep in housekeeping."""

from __future__ import annotations

import os
import time
from pathlib import Path

from skcapstone.housekeeping import (
    DEFAULT_INBOX_MAX_AGE_HOURS,
    _inbox_dirs,
    prune_derived_junk,
    prune_inbox,
    run_housekeeping,
)


def _old(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def _mk_inbox_envelope(inbox: Path, name: str, age_hours: float) -> Path:
    inbox.mkdir(parents=True, exist_ok=True)
    f = inbox / name
    f.write_text("{}", encoding="utf-8")
    _old(f, age_hours)
    return f


class TestInboxDirs:
    def test_default_ttl_is_seven_days(self):
        assert DEFAULT_INBOX_MAX_AGE_HOURS == 168

    def test_discovers_root_and_per_agent_inboxes(self, tmp_path):
        root_inbox = tmp_path / "comms" / "inbox"
        a_inbox = tmp_path / "agents" / "lumina" / "comms" / "inbox"
        b_inbox = tmp_path / "agents" / "jarvis" / "comms" / "inbox"
        for d in (root_inbox, a_inbox, b_inbox):
            d.mkdir(parents=True)

        found = set(_inbox_dirs(tmp_path))
        assert found == {root_inbox, a_inbox, b_inbox}

    def test_no_dirs_returns_empty(self, tmp_path):
        assert _inbox_dirs(tmp_path) == []


class TestPruneInbox:
    def test_deletes_stale_keeps_fresh(self, tmp_path):
        inbox = tmp_path / "agents" / "lumina" / "comms" / "inbox"
        _mk_inbox_envelope(inbox, "old1.skc.json", 200)
        _mk_inbox_envelope(inbox, "old2.skc.json", 200)
        fresh = _mk_inbox_envelope(inbox, "fresh.skc.json", 1)

        deleted = prune_inbox(tmp_path)

        assert deleted == 2
        assert fresh.exists()

    def test_only_touches_skc_json(self, tmp_path):
        inbox = tmp_path / "comms" / "inbox"
        env = _mk_inbox_envelope(inbox, "old.skc.json", 200)
        other = inbox / "keep.txt"
        other.write_text("x")
        _old(other, 200)

        prune_inbox(tmp_path)

        assert not env.exists()
        assert other.exists(), "non-envelope files are left alone"

    def test_respects_ttl_below_poll_interval_stays(self, tmp_path):
        """A short custom TTL still keeps files newer than it (backstop only)."""
        inbox = tmp_path / "comms" / "inbox"
        recent = _mk_inbox_envelope(inbox, "recent.skc.json", 0.5)

        deleted = prune_inbox(tmp_path, max_age_hours=1)

        assert deleted == 0
        assert recent.exists()

    def test_no_inbox_returns_zero(self, tmp_path):
        assert prune_inbox(tmp_path) == 0


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

    def test_removes_pid_files(self, tmp_path):
        pid = tmp_path / "daemon.pid"
        pid.write_text("1234")
        nested = tmp_path / "agents" / "lumina"
        nested.mkdir(parents=True)
        pid2 = nested / "consciousness.pid"
        pid2.write_text("99")

        removed = prune_derived_junk(tmp_path)

        assert not pid.exists()
        assert not pid2.exists()
        assert removed >= 2

    def test_no_junk_returns_zero(self, tmp_path):
        (tmp_path / "memory").mkdir()
        assert prune_derived_junk(tmp_path) == 0


class TestRunHousekeepingIntegration:
    def test_inbox_and_derived_junk_registered_in_dry_run(self, tmp_path):
        inbox = tmp_path / "agents" / "lumina" / "comms" / "inbox"
        for i in range(3):
            _mk_inbox_envelope(inbox, f"old{i}.skc.json", 200)
        (tmp_path / "run.pid").write_text("1")
        bak = tmp_path / "memory" / "chroma.bak"
        bak.mkdir(parents=True)

        results = run_housekeeping(
            skcapstone_home=tmp_path,
            skcomms_home=tmp_path / ".skcomms",
            dry_run=True,
        )

        assert "inbox" in results
        assert "derived_junk" in results
        assert results["inbox"]["would_delete"] == 3
        assert results["derived_junk"]["would_delete"] >= 2
        # Dry run deletes nothing.
        assert (inbox / "old0.skc.json").exists()
        assert bak.exists()

    def test_full_run_prunes_inbox_and_junk(self, tmp_path):
        inbox = tmp_path / "comms" / "inbox"
        for i in range(4):
            _mk_inbox_envelope(inbox, f"old{i}.skc.json", 200)
        _mk_inbox_envelope(inbox, "fresh.skc.json", 1)
        (tmp_path / "x.pid").write_text("1")

        results = run_housekeeping(
            skcapstone_home=tmp_path,
            skcomms_home=tmp_path / ".skcomms",
            dry_run=False,
        )

        assert results["inbox"]["deleted"] == 4
        assert results["derived_junk"]["deleted"] >= 1
        assert (inbox / "fresh.skc.json").exists()
        # Both keys feed the summary totals.
        assert results["summary"]["total_deleted"] >= 5
