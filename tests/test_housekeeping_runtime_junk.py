"""Tests for comms runtime-junk cleanup in housekeeping.

Follow-up to the inbox-GC work. Covers the runtime comms trees that pile up on
a live node and pin the Syncthing scanner:

* ``{home}/skcomms/acks`` — the REAL acks dir. ``prune_acks`` used to be wired
  to ``~/.skcomms/acks`` (a dead path), so the real 179k-file dir was never
  swept. ``run_housekeeping`` must now default ``skcomms_home`` to
  ``{skcapstone_home}/skcomms`` (honoring ``SKCOMMS_HOME``).
* ``{home}/skcomms/inbox`` — static mailbox/federation inbox (266k). TTL 72h.
* ``agents/*/comms/archive`` + ``{home}/comms/archive`` — already-consumed
  messages (~170k). TTL 48h.
* ``agents/*/comms/outbox`` (+ root) FLAT ``*.skc.json`` files (~54k) —
  ``prune_legacy_comms`` only reached per-recipient SUBDIRS, so the flat live
  outbox was never pruned. New flat sweep at TTL 48h.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from skcapstone.housekeeping import (
    DEFAULT_COMMS_ARCHIVE_MAX_AGE_HOURS,
    DEFAULT_OUTBOX_FLAT_MAX_AGE_HOURS,
    DEFAULT_SKCOMMS_INBOX_MAX_AGE_HOURS,
    prune_comms_archive,
    prune_comms_outbox_flat,
    prune_skcomms_inbox,
    run_housekeeping,
)


def _old(path: Path, hours: float) -> None:
    t = time.time() - hours * 3600
    os.utime(path, (t, t))


def _mk(directory: Path, name: str, age_hours: float) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("{}", encoding="utf-8")
    _old(f, age_hours)
    return f


class TestDefaults:
    def test_skcomms_inbox_ttl_is_72h(self):
        assert DEFAULT_SKCOMMS_INBOX_MAX_AGE_HOURS == 72

    def test_comms_archive_ttl_is_48h(self):
        assert DEFAULT_COMMS_ARCHIVE_MAX_AGE_HOURS == 48

    def test_outbox_flat_ttl_is_48h(self):
        assert DEFAULT_OUTBOX_FLAT_MAX_AGE_HOURS == 48


class TestAcksRealPath:
    """prune_acks must target {home}/skcomms/acks — the real dir."""

    def test_defaults_skcomms_home_to_skcapstone_skcomms(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SKCOMMS_HOME", raising=False)
        acks = tmp_path / "skcomms" / "acks"
        _mk(acks, "old.ack.json", 200)
        fresh = _mk(acks, "fresh.ack.json", 1)

        # No skcomms_home passed — mirrors the daemon call.
        results = run_housekeeping(skcapstone_home=tmp_path, dry_run=False)

        assert results["acks"]["path"] == str(acks)
        assert results["acks"]["deleted"] == 1
        assert fresh.exists()

    def test_honors_skcomms_home_env(self, tmp_path, monkeypatch):
        custom = tmp_path / "custom-skcomms"
        monkeypatch.setenv("SKCOMMS_HOME", str(custom))
        acks = custom / "acks"
        _mk(acks, "old.ack.json", 200)

        results = run_housekeeping(skcapstone_home=tmp_path, dry_run=False)

        assert results["acks"]["path"] == str(acks)
        assert results["acks"]["deleted"] == 1

    def test_dry_run_counts_real_acks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SKCOMMS_HOME", raising=False)
        acks = tmp_path / "skcomms" / "acks"
        for i in range(4):
            _mk(acks, f"old{i}.ack.json", 200)

        results = run_housekeeping(skcapstone_home=tmp_path, dry_run=True)

        assert results["acks"]["would_delete"] == 4
        assert len(list(acks.iterdir())) == 4  # dry run deletes nothing


class TestPruneSkcommsInbox:
    def test_prunes_aged_recursive(self, tmp_path):
        peer = tmp_path / "skcomms" / "inbox" / "opus"
        _mk(peer, "old1.skc.json", 200)
        _mk(peer, "old2.skc.json", 200)
        fresh = _mk(peer, "fresh.skc.json", 1)

        deleted = prune_skcomms_inbox(tmp_path)

        assert deleted == 2
        assert fresh.exists()

    def test_prunes_nested_archive_subtree(self, tmp_path):
        # skcomms/inbox has an archive/ subdir with the bulk of the files.
        arch = tmp_path / "skcomms" / "inbox" / "archive"
        _mk(arch, "old.skc.json", 200)
        deleted = prune_skcomms_inbox(tmp_path)
        assert deleted == 1

    def test_only_touches_skc_json(self, tmp_path):
        peer = tmp_path / "skcomms" / "inbox" / "jarvis"
        env = _mk(peer, "old.skc.json", 200)
        keep = _mk(peer, "note.txt", 200)
        prune_skcomms_inbox(tmp_path)
        assert not env.exists()
        assert keep.exists()

    def test_respects_ttl(self, tmp_path):
        peer = tmp_path / "skcomms" / "inbox" / "opus"
        _mk(peer, "borderline.skc.json", 71)  # under 72h — kept
        assert prune_skcomms_inbox(tmp_path) == 0

    def test_no_dir_returns_zero(self, tmp_path):
        assert prune_skcomms_inbox(tmp_path) == 0


class TestPruneCommsArchive:
    def test_prunes_agent_archives(self, tmp_path):
        arch = tmp_path / "agents" / "lumina" / "comms" / "archive"
        aged = _mk(arch, "old.skc.json", 200)
        fresh = _mk(arch, "recent.skc.json", 1)

        deleted = prune_comms_archive(tmp_path)

        assert deleted == 1
        assert not aged.exists()
        assert fresh.exists()

    def test_prunes_root_archive(self, tmp_path):
        arch = tmp_path / "comms" / "archive"
        aged = _mk(arch, "old.skc.json", 200)
        deleted = prune_comms_archive(tmp_path)
        assert deleted == 1
        assert not aged.exists()

    def test_covers_every_agent(self, tmp_path):
        for agent in ("lumina", "opus", "jarvis"):
            _mk(tmp_path / "agents" / agent / "comms" / "archive", "old.skc.json", 200)
        assert prune_comms_archive(tmp_path) == 3

    def test_only_skc_json(self, tmp_path):
        arch = tmp_path / "agents" / "opus" / "comms" / "archive"
        keep = _mk(arch, "keep.txt", 200)
        prune_comms_archive(tmp_path)
        assert keep.exists()

    def test_no_dir_returns_zero(self, tmp_path):
        assert prune_comms_archive(tmp_path) == 0


class TestPruneCommsOutboxFlat:
    def test_prunes_flat_agent_outbox_files(self, tmp_path):
        out = tmp_path / "agents" / "opus" / "comms" / "outbox"
        aged = _mk(out, "old.skc.json", 200)
        fresh = _mk(out, "recent.skc.json", 1)

        deleted = prune_comms_outbox_flat(tmp_path)

        assert deleted == 1
        assert not aged.exists()
        assert fresh.exists()

    def test_prunes_root_outbox_flat(self, tmp_path):
        out = tmp_path / "comms" / "outbox"
        aged = _mk(out, "old.skc.json", 200)
        deleted = prune_comms_outbox_flat(tmp_path)
        assert deleted == 1

    def test_leaves_recipient_subdir_files(self, tmp_path):
        # per-recipient SUBDIR files are owned by prune_legacy_comms — the flat
        # sweep must NOT descend into them.
        sub = tmp_path / "agents" / "lumina" / "comms" / "outbox" / "jarvis"
        aged = _mk(sub, "old.skc.json", 400)
        deleted = prune_comms_outbox_flat(tmp_path)
        assert deleted == 0
        assert aged.exists()

    def test_ignores_dot_and_tmp(self, tmp_path):
        out = tmp_path / "agents" / "opus" / "comms" / "outbox"
        tmpf = _mk(out, ".abc.skc.json.tmp", 200)
        dotf = _mk(out, ".hidden.skc.json", 200)
        prune_comms_outbox_flat(tmp_path)
        assert tmpf.exists()
        assert dotf.exists()

    def test_respects_ttl(self, tmp_path):
        out = tmp_path / "agents" / "opus" / "comms" / "outbox"
        _mk(out, "borderline.skc.json", 47)  # under 48h — kept
        assert prune_comms_outbox_flat(tmp_path) == 0

    def test_no_dir_returns_zero(self, tmp_path):
        assert prune_comms_outbox_flat(tmp_path) == 0


class TestRunHousekeepingIntegration:
    def test_new_targets_in_dry_run(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SKCOMMS_HOME", raising=False)
        for i in range(3):
            _mk(tmp_path / "skcomms" / "inbox" / "opus", f"i{i}.skc.json", 200)
        for i in range(2):
            _mk(tmp_path / "agents" / "lumina" / "comms" / "archive", f"a{i}.skc.json", 200)
        _mk(tmp_path / "agents" / "opus" / "comms" / "outbox", "o0.skc.json", 200)

        results = run_housekeeping(skcapstone_home=tmp_path, dry_run=True)

        assert results["skcomms_inbox"]["would_delete"] == 3
        assert results["comms_archive"]["would_delete"] == 2
        assert results["comms_outbox_flat"]["would_delete"] == 1
        # dry run deletes nothing
        assert (tmp_path / "skcomms" / "inbox" / "opus" / "i0.skc.json").exists()

    def test_full_run_prunes_new_targets(self, tmp_path, monkeypatch):
        monkeypatch.delenv("SKCOMMS_HOME", raising=False)
        for i in range(4):
            _mk(tmp_path / "skcomms" / "inbox" / "opus", f"i{i}.skc.json", 200)
        _mk(tmp_path / "skcomms" / "inbox" / "opus", "fresh.skc.json", 1)
        _mk(tmp_path / "agents" / "lumina" / "comms" / "archive", "a0.skc.json", 200)
        _mk(tmp_path / "agents" / "opus" / "comms" / "outbox", "o0.skc.json", 200)

        results = run_housekeeping(skcapstone_home=tmp_path, dry_run=False)

        assert results["skcomms_inbox"]["deleted"] == 4
        assert results["comms_archive"]["deleted"] == 1
        assert results["comms_outbox_flat"]["deleted"] == 1
        assert (tmp_path / "skcomms" / "inbox" / "opus" / "fresh.skc.json").exists()
