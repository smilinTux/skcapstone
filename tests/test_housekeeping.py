"""Tests for skcapstone.housekeeping - storage pruning."""

import os
import time

import pytest

from skcapstone.housekeeping import (
    _seed_outbox_dirs,
    prune_acks,
    prune_comms_outbox,
    prune_seeds,
    run_housekeeping,
)


class TestPerAgentSeedOutbox:
    """The daemon writes seeds to agents/<agent>/sync/outbox, not sync/outbox.

    Regression: housekeeping only ever pruned the shared ``sync/outbox``, so the
    per-agent outboxes grew without bound - 240k files / 12 GB per node on a
    six-node cluster before anyone noticed.
    """

    def _seed(self, outbox, agent, peer, stamp):
        f = outbox / f"{agent}-{peer}-{stamp}.seed.json"
        f.write_text("{}", encoding="utf-8")
        return f

    def test_discovers_per_agent_outboxes(self, tmp_path):
        shared = tmp_path / "sync" / "outbox"
        shared.mkdir(parents=True)
        a = tmp_path / "agents" / "jarvis" / "sync" / "outbox"
        b = tmp_path / "agents" / "aster" / "sync" / "outbox"
        a.mkdir(parents=True)
        b.mkdir(parents=True)

        found = _seed_outbox_dirs(tmp_path)
        assert shared in found
        assert a in found
        assert b in found

    def test_run_housekeeping_prunes_per_agent_seeds(self, tmp_path):
        outbox = tmp_path / "agents" / "jarvis" / "sync" / "outbox"
        outbox.mkdir(parents=True)
        # 30 seeds from one peer; only the newest 10 should survive.
        for i in range(30):
            f = self._seed(outbox, "Jarvis", "chiap01", f"2026081{i // 10}T0{i % 10}0000Z")
            os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))

        assert len(list(outbox.iterdir())) == 30
        run_housekeeping(skcapstone_home=tmp_path, skcomms_home=tmp_path / "skcomms")
        assert len(list(outbox.iterdir())) == 10

    def test_dry_run_counts_per_agent_seeds_without_deleting(self, tmp_path):
        outbox = tmp_path / "agents" / "jarvis" / "sync" / "outbox"
        outbox.mkdir(parents=True)
        for i in range(25):
            f = self._seed(outbox, "Jarvis", "chiap02", f"2026081{i // 10}T0{i % 10}0000Z")
            os.utime(f, (1_700_000_000 + i, 1_700_000_000 + i))

        res = run_housekeeping(
            skcapstone_home=tmp_path, skcomms_home=tmp_path / "skcomms", dry_run=True
        )
        assert res["seed_outbox"]["would_delete"] == 15
        assert len(list(outbox.iterdir())) == 25


@pytest.fixture
def skcomms_home(tmp_path):
    """Create a mock ~/.skcomms directory with test ACK files."""
    acks_dir = tmp_path / "acks"
    acks_dir.mkdir()
    return tmp_path


@pytest.fixture
def skcapstone_home(tmp_path):
    """Create a mock ~/.skcapstone directory with sync structure."""
    sync_dir = tmp_path / "sync"
    comms_out = sync_dir / "comms" / "outbox"
    seed_out = sync_dir / "sync" / "outbox"
    comms_out.mkdir(parents=True)
    seed_out.mkdir(parents=True)
    return tmp_path


class TestPruneAcks:
    """Tests for prune_acks."""

    def test_no_acks_dir(self, tmp_path):
        """Returns 0 when acks directory doesn't exist."""
        assert prune_acks(tmp_path) == 0

    def test_empty_acks_dir(self, skcomms_home):
        """Returns 0 when acks directory is empty."""
        assert prune_acks(skcomms_home) == 0

    def test_deletes_old_acks(self, skcomms_home):
        """Deletes ACK files older than max_age_hours."""
        acks_dir = skcomms_home / "acks"
        # Create 5 old files (mtime set to 48h ago)
        old_time = time.time() - (48 * 3600)
        for i in range(5):
            f = acks_dir / f"ack-{i}.json"
            f.write_text("{}")
            import os

            os.utime(f, (old_time, old_time))

        # Create 3 fresh files
        for i in range(3):
            f = acks_dir / f"fresh-{i}.json"
            f.write_text("{}")

        deleted = prune_acks(skcomms_home, max_age_hours=24)
        assert deleted == 5
        remaining = list(acks_dir.iterdir())
        assert len(remaining) == 3

    def test_respects_max_age(self, skcomms_home):
        """Only deletes files older than the specified max_age."""
        acks_dir = skcomms_home / "acks"
        # File 1h old
        f = acks_dir / "recent.json"
        f.write_text("{}")
        import os

        os.utime(f, (time.time() - 3600, time.time() - 3600))

        assert prune_acks(skcomms_home, max_age_hours=2) == 0
        assert prune_acks(skcomms_home, max_age_hours=0) == 1


class TestPruneCommsOutbox:
    """Tests for prune_comms_outbox."""

    def test_no_outbox_dir(self, tmp_path):
        """Returns 0 when outbox directory doesn't exist."""
        assert prune_comms_outbox(tmp_path) == 0

    def test_empty_outbox(self, skcapstone_home):
        """Returns 0 when outbox is empty."""
        assert prune_comms_outbox(skcapstone_home / "sync") == 0

    def test_deletes_old_envelopes(self, skcapstone_home):
        """Deletes envelope files older than max_age_hours."""
        agent_dir = skcapstone_home / "sync" / "comms" / "outbox" / "lumina"
        agent_dir.mkdir(parents=True)

        old_time = time.time() - (72 * 3600)
        for i in range(4):
            f = agent_dir / f"env-{i}.json"
            f.write_text("{}")
            import os

            os.utime(f, (old_time, old_time))

        f = agent_dir / "fresh.json"
        f.write_text("{}")

        deleted = prune_comms_outbox(skcapstone_home / "sync", max_age_hours=48)
        assert deleted == 4
        assert (agent_dir / "fresh.json").exists()


class TestPruneSeeds:
    """Tests for prune_seeds."""

    def test_no_outbox_dir(self, tmp_path):
        """Returns 0 when seed outbox doesn't exist."""
        assert prune_seeds(tmp_path / "nonexistent") == 0

    def test_keeps_recent_seeds(self, skcapstone_home):
        """Keeps only keep_per_agent most recent seeds."""
        seed_dir = skcapstone_home / "sync" / "sync" / "outbox"

        # Create 15 seeds for agent "opus"
        for i in range(15):
            f = seed_dir / f"opus-170900000{i:01d}.json.gpg"
            f.write_text("{}")
            import os

            os.utime(f, (time.time() - (15 - i) * 300, time.time() - (15 - i) * 300))

        deleted = prune_seeds(seed_dir, keep_per_agent=10)
        assert deleted == 5
        remaining = list(seed_dir.iterdir())
        assert len(remaining) == 10

    def test_handles_multiple_agents(self, skcapstone_home):
        """Keeps seeds per-agent, not globally."""
        seed_dir = skcapstone_home / "sync" / "sync" / "outbox"

        for agent in ("opus", "lumina"):
            for i in range(5):
                f = seed_dir / f"{agent}-170900000{i}.json"
                f.write_text("{}")

        deleted = prune_seeds(seed_dir, keep_per_agent=3)
        assert deleted == 4  # 2 excess per agent

    def test_empty_outbox(self, skcapstone_home):
        """Returns 0 when seed outbox is empty."""
        seed_dir = skcapstone_home / "sync" / "sync" / "outbox"
        assert prune_seeds(seed_dir) == 0


class TestRunHousekeeping:
    """Tests for run_housekeeping."""

    def test_dry_run(self, tmp_path):
        """Dry run reports counts without deleting."""
        # Set up dirs
        acks_dir = tmp_path / "skcomms" / "acks"
        acks_dir.mkdir(parents=True)
        for i in range(3):
            f = acks_dir / f"old-{i}.json"
            f.write_text("{}")
            import os

            os.utime(f, (time.time() - 48 * 3600, time.time() - 48 * 3600))

        results = run_housekeeping(
            skcapstone_home=tmp_path / "skcapstone",
            skcomms_home=tmp_path / "skcomms",
            dry_run=True,
        )

        assert results.get("dry_run") is True
        assert results["acks"]["would_delete"] == 3
        # Files should still exist
        assert len(list(acks_dir.iterdir())) == 3

    def test_full_run(self, tmp_path):
        """Full run deletes files and reports summary."""
        acks_dir = tmp_path / "skcomms" / "acks"
        acks_dir.mkdir(parents=True)
        for i in range(2):
            f = acks_dir / f"old-{i}.json"
            f.write_text("{}")
            import os

            os.utime(f, (time.time() - 48 * 3600, time.time() - 48 * 3600))

        results = run_housekeeping(
            skcapstone_home=tmp_path / "skcapstone",
            skcomms_home=tmp_path / "skcomms",
            dry_run=False,
        )

        assert "summary" in results
        assert results["acks"]["deleted"] == 2
        assert len(list(acks_dir.iterdir())) == 0
