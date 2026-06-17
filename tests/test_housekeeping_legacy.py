"""Tests for skcapstone.housekeeping legacy/broadcast comms-outbox sweep."""

import os
import time
from pathlib import Path

import pytest

from skcapstone.housekeeping import (
    DEFAULT_LEGACY_COMMS_MAX_AGE_HOURS,
    prune_legacy_comms,
    run_housekeeping,
)


def _make_envelope(directory: Path, name: str, age_hours: float = 0.0) -> Path:
    """Create a ``*.skc.json`` envelope, optionally back-dated by age_hours."""
    directory.mkdir(parents=True, exist_ok=True)
    f = directory / name
    f.write_text("{}")
    if age_hours:
        ts = time.time() - (age_hours * 3600)
        os.utime(f, (ts, ts))
    return f


class TestPruneLegacyComms:
    """Tests for prune_legacy_comms."""

    def test_no_dirs_returns_zero(self, tmp_path):
        """Returns 0 when no legacy outbox directories exist."""
        assert prune_legacy_comms(tmp_path) == 0

    def test_deletes_stale_keeps_fresh_both_roots(self, tmp_path):
        """Stale envelopes in BOTH legacy roots are deleted; fresh ones kept."""
        # v1 root path: <home>/comms/outbox/<recipient>/
        root_recipient = tmp_path / "comms" / "outbox" / "jarvis"
        for i in range(3):
            _make_envelope(root_recipient, f"old-{i}.skc.json", age_hours=240)
        _make_envelope(root_recipient, "fresh.skc.json", age_hours=1)

        # v1 per-agent path: <home>/agents/<agent>/comms/outbox/<recipient>/
        agent_recipient = tmp_path / "agents" / "lumina" / "comms" / "outbox" / "opus"
        for i in range(2):
            _make_envelope(agent_recipient, f"old-{i}.skc.json", age_hours=240)
        _make_envelope(agent_recipient, "fresh.skc.json", age_hours=1)

        # A non-envelope file should never be touched.
        keep_other = root_recipient / "notes.txt"
        keep_other.write_text("keep me")
        os.utime(keep_other, (time.time() - 240 * 3600,) * 2)

        deleted = prune_legacy_comms(tmp_path)

        assert deleted == 5  # 3 root + 2 per-agent stale envelopes
        assert (root_recipient / "fresh.skc.json").exists()
        assert (agent_recipient / "fresh.skc.json").exists()
        assert keep_other.exists()
        assert not (root_recipient / "old-0.skc.json").exists()

    def test_broadcast_star_dir_removed_regardless_of_age(self, tmp_path):
        """A recipient subdir literally named ``*`` is removed wholesale."""
        star_dir = tmp_path / "comms" / "outbox" / "*"
        # Fresh files (age 0) — must still be removed because the dir is "*".
        for i in range(4):
            _make_envelope(star_dir, f"bcast-{i}.skc.json", age_hours=0)
        # Even nested subdirs under "*" are swept.
        nested = star_dir / "sub"
        _make_envelope(nested, "deep.skc.json", age_hours=0)

        # A normal sibling recipient with a fresh file is left alone.
        normal = tmp_path / "comms" / "outbox" / "lumina"
        _make_envelope(normal, "keep.skc.json", age_hours=1)

        deleted = prune_legacy_comms(tmp_path)

        assert deleted == 5  # 4 + 1 nested, all under the "*" tree
        assert not star_dir.exists()
        assert (normal / "keep.skc.json").exists()

    def test_removes_empty_recipient_and_outbox_dirs(self, tmp_path):
        """Now-empty recipient and outbox dirs are cleaned up."""
        recipient = tmp_path / "comms" / "outbox" / "ghost"
        _make_envelope(recipient, "old.skc.json", age_hours=240)

        prune_legacy_comms(tmp_path)

        assert not recipient.exists()
        assert not (tmp_path / "comms" / "outbox").exists()

    def test_default_max_age_is_seven_days(self):
        """The default legacy TTL is 7 days (168h)."""
        assert DEFAULT_LEGACY_COMMS_MAX_AGE_HOURS == 168


class TestRunHousekeepingLegacy:
    """Integration of legacy_comms into run_housekeeping."""

    def test_dry_run_counts_without_deleting(self, tmp_path):
        """Dry run reports legacy counts and deletes nothing."""
        home = tmp_path / "skcapstone"
        recipient = home / "comms" / "outbox" / "jarvis"
        for i in range(3):
            _make_envelope(recipient, f"old-{i}.skc.json", age_hours=240)
        star_dir = home / "agents" / "lumina" / "comms" / "outbox" / "*"
        for i in range(2):
            _make_envelope(star_dir, f"bcast-{i}.skc.json", age_hours=0)

        results = run_housekeeping(
            skcapstone_home=home,
            skcomms_home=tmp_path / "skcomms",
            dry_run=True,
        )

        assert results.get("dry_run") is True
        # 3 stale envelopes + 2 broadcast files = 5.
        assert results["legacy_comms"]["would_delete"] == 5
        # Nothing deleted.
        assert (recipient / "old-0.skc.json").exists()
        assert star_dir.exists()

    def test_full_run_deletes_and_counts_in_summary(self, tmp_path):
        """Full run prunes legacy files and includes them in the summary total."""
        home = tmp_path / "skcapstone"
        recipient = home / "comms" / "outbox" / "jarvis"
        for i in range(3):
            _make_envelope(recipient, f"old-{i}.skc.json", age_hours=240)
        star_dir = home / "agents" / "lumina" / "comms" / "outbox" / "*"
        _make_envelope(star_dir, "bcast.skc.json", age_hours=0)

        results = run_housekeeping(
            skcapstone_home=home,
            skcomms_home=tmp_path / "skcomms",
            dry_run=False,
        )

        assert results["legacy_comms"]["deleted"] == 4
        assert results["summary"]["total_deleted"] >= 4
        assert not star_dir.exists()
        assert not (recipient / "old-0.skc.json").exists()
