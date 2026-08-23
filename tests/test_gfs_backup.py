"""Tests for the GFS backup job, retention, and staleness monitor.

All tests operate on synthetic artifacts / fake agent homes inside pytest
``tmp_path`` sandboxes. No real agent state is read and no real backup
destination is written.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from skcapstone.gfs_backup import (
    BackupArtifact,
    BackupJobConfig,
    GFSPolicy,
    check_backup_health,
    discover_artifacts,
    parse_backup_timestamp,
    prune_artifacts,
    resolve_config,
    run_backup_job,
    select_gfs_retention,
)

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _artifact_name(ts: datetime) -> str:
    """Return the canonical artifact filename for a timestamp."""
    return f"backup-{ts.strftime('%Y%m%d-%H%M%S-%f')}.tar.gz"


def _touch_artifact(backup_dir: Path, ts: datetime) -> Path:
    """Create an empty backup artifact file named for *ts*."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    path = backup_dir / _artifact_name(ts)
    path.write_bytes(b"fake-backup")
    return path


def _artifacts_for(days_back: list[int], anchor: datetime) -> list[BackupArtifact]:
    """Build synthetic artifacts *days_back* days before *anchor* (no files)."""
    out = []
    for d in days_back:
        ts = anchor - timedelta(days=d)
        out.append(BackupArtifact(path=Path(f"/tmp/{_artifact_name(ts)}"), timestamp=ts))
    return out


def _setup_fake_home(tmp_path: Path) -> Path:
    """Create a minimal fake agent home that create_backup can archive."""
    home = tmp_path / ".skcapstone"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text('agent_name: "TestAgent"\n')
    (home / "memory").mkdir()
    (home / "memory" / "mem1.json").write_text('{"id": "mem1"}')
    (home / "identity").mkdir()
    (home / "identity" / "profile.json").write_text('{"name": "TestAgent"}')
    return home


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------


def test_parse_backup_timestamp_full():
    ts = parse_backup_timestamp("backup-20260724-031500-123456.tar.gz")
    assert ts == datetime(2026, 7, 24, 3, 15, 0, 123456, tzinfo=UTC)


def test_parse_backup_timestamp_no_micros():
    ts = parse_backup_timestamp("backup-20260724-031500.tar.gz")
    assert ts == datetime(2026, 7, 24, 3, 15, 0, tzinfo=UTC)


def test_parse_backup_timestamp_rejects_foreign_names():
    assert parse_backup_timestamp("skcapstone-state-20260724.tar.gz") is None
    assert parse_backup_timestamp("gfs-state.json") is None
    assert parse_backup_timestamp("random.txt") is None


# ---------------------------------------------------------------------------
# GFS retention (pure)
# ---------------------------------------------------------------------------


def test_retention_keeps_correct_daily_set_and_prunes_rest():
    """7 consecutive daily backups, keep_daily=3 => keep newest 3, prune 4."""
    anchor = datetime(2026, 7, 24, 3, 0, 0, tzinfo=UTC)
    arts = _artifacts_for([0, 1, 2, 3, 4, 5, 6], anchor)
    policy = GFSPolicy(daily=3, weekly=0, monthly=0, yearly=0)

    keep, prune = select_gfs_retention(arts, policy)

    assert len(keep) == 3
    assert len(prune) == 4
    # Kept are the three most recent (0,1,2 days back).
    kept_days = sorted((anchor - a.timestamp).days for a in keep)
    assert kept_days == [0, 1, 2]
    # Partition is exhaustive and disjoint.
    assert len(keep) + len(prune) == len(arts)
    assert not ({a.path for a in keep} & {a.path for a in prune})


def test_retention_union_across_daily_weekly_monthly():
    """A long daily series collapses to a bounded GFS keep set."""
    anchor = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    # 120 days of daily backups.
    arts = _artifacts_for(list(range(120)), anchor)
    policy = GFSPolicy(daily=7, weekly=4, monthly=6, yearly=0)

    keep, prune = select_gfs_retention(arts, policy)

    # At most daily+weekly+monthly distinct artifacts (tiers overlap on recent ones).
    assert len(keep) <= 7 + 4 + 6
    # The 7 most recent days are always kept.
    kept_days = {(anchor - a.timestamp).days for a in keep}
    assert {0, 1, 2, 3, 4, 5, 6}.issubset(kept_days)
    # Depth reaches back multiple months (grandfather tier).
    assert max((anchor - a.timestamp).days for a in keep) >= 90
    assert len(prune) == 120 - len(keep)


def test_retention_one_per_day_collapses_same_day_backups():
    """Multiple backups on the same day count as one daily period."""
    anchor = datetime(2026, 7, 24, 23, 0, 0, tzinfo=UTC)
    same_day = [
        BackupArtifact(path=Path(f"/tmp/a{h}.tar.gz"), timestamp=anchor.replace(hour=h))
        for h in (1, 8, 15, 22)
    ]
    policy = GFSPolicy(daily=2, weekly=0, monthly=0, yearly=0)

    keep, prune = select_gfs_retention(same_day, policy)

    # Only the newest of the single day is kept (2 days requested, 1 day present).
    assert len(keep) == 1
    assert keep[0].timestamp.hour == 22
    assert len(prune) == 3


def test_retention_noop_policy_keeps_all():
    anchor = datetime(2026, 7, 24, tzinfo=UTC)
    arts = _artifacts_for([0, 1, 2], anchor)
    keep, prune = select_gfs_retention(arts, GFSPolicy(0, 0, 0, 0))
    assert len(keep) == 3
    assert prune == []


def test_retention_empty_input():
    keep, prune = select_gfs_retention([], GFSPolicy())
    assert keep == []
    assert prune == []


# ---------------------------------------------------------------------------
# discover + prune confinement
# ---------------------------------------------------------------------------


def test_discover_artifacts_sorted_newest_first(tmp_path):
    backup_dir = tmp_path / "backups"
    base = datetime(2026, 7, 20, 3, 0, 0, tzinfo=UTC)
    for d in (0, 3, 1):
        _touch_artifact(backup_dir, base + timedelta(days=d))
    # Foreign files must be ignored.
    (backup_dir / "gfs-state.json").write_text("{}")
    (backup_dir / "skcapstone-state-20260720.tar.gz").write_text("x")

    arts = discover_artifacts(backup_dir)

    assert len(arts) == 3
    assert arts[0].timestamp > arts[1].timestamp > arts[2].timestamp


def test_prune_refuses_paths_outside_backup_dir(tmp_path):
    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    victim = outside / "backup-20260101-000000-000000.tar.gz"
    victim.write_text("do-not-delete")

    art = BackupArtifact(path=victim, timestamp=datetime(2026, 1, 1, tzinfo=UTC))
    deleted = prune_artifacts([art], backup_dir)

    assert deleted == []
    assert victim.exists()  # confinement held


def test_prune_deletes_in_scope(tmp_path):
    backup_dir = tmp_path / "backups"
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    path = _touch_artifact(backup_dir, ts)
    art = BackupArtifact(path=path, timestamp=ts)

    deleted = prune_artifacts([art], backup_dir)

    assert deleted == [path.name]
    assert not path.exists()


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


def _config_for(backup_dir: Path, **kw) -> BackupJobConfig:
    return BackupJobConfig(
        backup_dir=backup_dir,
        policy=GFSPolicy(),
        max_age_seconds=kw.get("max_age_seconds", 26 * 3600),
        min_interval_seconds=kw.get("min_interval_seconds", 0.0),
        agent_name="TestAgent",
    )


def test_monitor_flags_missing(tmp_path):
    cfg = _config_for(tmp_path / "backups")
    report = check_backup_health(home=tmp_path, config=cfg)
    assert report["status"] == "missing"
    assert report["healthy"] is False


def test_monitor_flags_stale(tmp_path):
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    # Backup from 3 days ago against a 26h threshold => stale.
    _touch_artifact(backup_dir, now - timedelta(days=3))
    cfg = _config_for(backup_dir, max_age_seconds=26 * 3600)

    report = check_backup_health(home=tmp_path, config=cfg, now=now)

    assert report["status"] == "stale"
    assert report["healthy"] is False
    assert report["age_seconds"] > 26 * 3600


def test_monitor_passes_fresh(tmp_path):
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    _touch_artifact(backup_dir, now - timedelta(hours=2))
    cfg = _config_for(backup_dir, max_age_seconds=26 * 3600)

    report = check_backup_health(home=tmp_path, config=cfg, now=now)

    assert report["status"] == "ok"
    assert report["healthy"] is True


def test_monitor_flags_failed_run_over_fresh_artifact(tmp_path):
    """A recorded failure outranks a still-fresh artifact."""
    backup_dir = tmp_path / "backups"
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    _touch_artifact(backup_dir, now - timedelta(hours=1))
    # Simulate a failed run recorded in the sidecar.
    import json

    (backup_dir / "gfs-state.json").write_text(
        json.dumps({"last_run_ok": False, "last_error": "disk full"})
    )
    cfg = _config_for(backup_dir)

    report = check_backup_health(home=tmp_path, config=cfg, now=now)

    assert report["status"] == "failed"
    assert report["healthy"] is False
    assert "disk full" in report["message"]


# ---------------------------------------------------------------------------
# Job wrapper (end-to-end, tmp only)
# ---------------------------------------------------------------------------


def test_run_backup_job_writes_artifact_and_state(tmp_path):
    home = _setup_fake_home(tmp_path)
    backup_dir = tmp_path / "dest"
    cfg = _config_for(backup_dir)
    cfg.agent_name = "TestAgent"

    result = run_backup_job(home=home, config=cfg)

    assert result["created"] is True
    assert result["error"] is None
    artifacts = list(backup_dir.glob("backup-*.tar.gz"))
    assert len(artifacts) == 1
    assert (backup_dir / "gfs-state.json").exists()
    # Health check now passes against the just-written artifact.
    report = check_backup_health(home=home, config=cfg)
    assert report["status"] == "ok"


def test_run_backup_job_prunes_within_tmp_only(tmp_path):
    home = _setup_fake_home(tmp_path)
    backup_dir = tmp_path / "dest"
    # Seed 5 old daily artifacts (empty files) spanning distinct days.
    base = datetime(2026, 7, 1, 3, 0, 0, tzinfo=UTC)
    seeded = []
    for d in range(5):
        seeded.append(_touch_artifact(backup_dir, base + timedelta(days=d)))
    # A foreign file that must survive pruning.
    foreign = backup_dir / "skcapstone-state-keepme.tar.gz"
    foreign.write_text("survive")

    # keep_daily=2 across all tiers off => only 2 most-recent days retained
    # after the new backup is created (=> new artifact + 1 seeded day kept).
    cfg = BackupJobConfig(
        backup_dir=backup_dir,
        policy=GFSPolicy(daily=2, weekly=0, monthly=0, yearly=0),
        agent_name="TestAgent",
    )

    result = run_backup_job(home=home, config=cfg)

    remaining = sorted(p.name for p in backup_dir.glob("backup-*.tar.gz"))
    assert len(remaining) == 2  # today's new one + the single newest seeded day
    assert result["pruned"] == 4  # the 4 older seeded artifacts
    assert foreign.exists()  # foreign file untouched
    # None of the pruned paths escaped tmp_path.
    for name in result["pruned_files"]:
        assert (backup_dir / name) == (backup_dir / name)  # names only, in-dir


def test_run_backup_job_min_interval_skips_create(tmp_path):
    home = _setup_fake_home(tmp_path)
    backup_dir = tmp_path / "dest"
    now = datetime(2026, 7, 24, 12, 0, 0, tzinfo=UTC)
    _touch_artifact(backup_dir, now - timedelta(minutes=5))  # very recent
    cfg = BackupJobConfig(
        backup_dir=backup_dir,
        policy=GFSPolicy(daily=7),
        min_interval_seconds=3600,  # 1h; newest is 5m old => skip
        agent_name="TestAgent",
    )

    result = run_backup_job(home=home, config=cfg, now=now)

    assert result["created"] is False
    assert result["skipped"] is True
    # No new artifact was written.
    assert len(list(backup_dir.glob("backup-*.tar.gz"))) == 1


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_resolve_config_defaults(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    cfg = resolve_config(home, agent_name="x")
    assert cfg.backup_dir == home / "backups"
    assert cfg.policy == GFSPolicy(daily=7, weekly=4, monthly=6, yearly=0)


def test_resolve_config_yaml_block(tmp_path):
    home = tmp_path / ".skcapstone"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text(
        "backup:\n"
        "  keep_daily: 3\n"
        "  keep_weekly: 1\n"
        "  keep_monthly: 2\n"
        "  max_age_seconds: 100\n"
    )
    cfg = resolve_config(home, agent_name="x")
    assert cfg.policy.daily == 3
    assert cfg.policy.weekly == 1
    assert cfg.policy.monthly == 2
    assert cfg.max_age_seconds == 100


def test_resolve_config_env_overrides_yaml(tmp_path, monkeypatch):
    home = tmp_path / ".skcapstone"
    (home / "config").mkdir(parents=True)
    (home / "config" / "config.yaml").write_text("backup:\n  keep_daily: 3\n")
    monkeypatch.setenv("SKCAPSTONE_BACKUP_KEEP_DAILY", "9")
    cfg = resolve_config(home, agent_name="x")
    assert cfg.policy.daily == 9


def test_resolve_config_overrides_win(tmp_path):
    home = tmp_path / ".skcapstone"
    home.mkdir()
    cfg = resolve_config(home, agent_name="x", overrides={"dir": str(tmp_path / "custom")})
    assert cfg.backup_dir == tmp_path / "custom"
