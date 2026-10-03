"""Grandfather-Father-Son (GFS) backup job with staleness monitoring.

This module builds three things on top of the existing :mod:`skcapstone.backup`
primitive (:func:`~skcapstone.backup.create_backup`):

1. **GFS retention** (:func:`select_gfs_retention`) - a pure function that, given
   a set of timestamped backup artifacts and a :class:`GFSPolicy`, returns which
   artifacts to *keep* and which to *prune*.  It follows the widely-used
   borg/restic semantics: for each retention tier (daily "son", weekly "father",
   monthly "grandfather", optional yearly), the newest backup within each of the
   most-recent ``N`` distinct periods is kept.  The kept set is the union across
   all tiers; everything else is pruned.

2. **A backup-job wrapper** (:func:`run_backup_job`) - runs the underlying backup,
   writes a timestamped artifact into a configurable directory, applies GFS
   pruning, and records a state sidecar (``gfs-state.json``) for monitoring.  The
   prune step is defensive: it only ever deletes files inside its own backup
   directory that match the backup naming convention.

3. **A health monitor** (:func:`check_backup_health`) - reports the age of the most
   recent backup and flags ``missing`` / ``stale`` / ``failed`` against a
   configurable freshness threshold.  :func:`make_backup_monitor_task` wraps it as
   a zero-arg scheduler callback that logs and (best-effort) fires an ``sk-alert``.

All destinations and thresholds are config-driven (``config/config.yaml`` under a
``backup:`` block, overridable by environment variables) with safe defaults.  The
job is safe to re-run and never deletes anything outside its own backup directory.

Scheduler entrypoints (for a ``type: python`` job in ``jobs.yaml`` / ``jobs.d/``)::

    skcapstone.gfs_backup:run_scheduled_backup   # create + prune
    skcapstone.gfs_backup:run_backup_monitor     # staleness check + alert
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger("skcapstone.gfs_backup")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Matches the artifact names produced by skcapstone.backup.create_backup:
#   backup-YYYYMMDD-HHMMSS-ffffff.tar.gz
_ARTIFACT_RE = re.compile(r"^backup-(\d{8})-(\d{6})(?:-(\d{1,6}))?\.tar\.gz$")

# Name of the state sidecar written into the backup directory.
STATE_FILENAME = "gfs-state.json"

# Safe defaults (borg-like retention + one-daily-per-day slack on freshness).
DEFAULT_KEEP_DAILY = 7
DEFAULT_KEEP_WEEKLY = 4
DEFAULT_KEEP_MONTHLY = 6
DEFAULT_KEEP_YEARLY = 0
# 26 hours: a once-daily backup plus a few hours of scheduler slack still counts
# as fresh, but a fully skipped day trips the monitor.
DEFAULT_MAX_AGE_SECONDS = 26 * 3600
# 0 = always create a new artifact when the job runs.  Set higher to make the
# job a no-op when a recent-enough backup already exists (extra idempotency).
DEFAULT_MIN_INTERVAL_SECONDS = 0.0


# ---------------------------------------------------------------------------
# Policy + artifact model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GFSPolicy:
    """Retention counts for a Grandfather-Father-Son rotation.

    Each field is the number of distinct, most-recent periods to keep for that
    tier.  ``0`` disables a tier.  A negative value means "unlimited" (keep every
    period for that tier).

    Attributes:
        daily: Number of most-recent days to keep (the "son" tier).
        weekly: Number of most-recent ISO weeks to keep (the "father" tier).
        monthly: Number of most-recent months to keep (the "grandfather" tier).
        yearly: Number of most-recent years to keep (optional top tier).
    """

    daily: int = DEFAULT_KEEP_DAILY
    weekly: int = DEFAULT_KEEP_WEEKLY
    monthly: int = DEFAULT_KEEP_MONTHLY
    yearly: int = DEFAULT_KEEP_YEARLY

    def is_noop(self) -> bool:
        """Return ``True`` when the policy would keep nothing.

        A policy where every tier is ``0`` retains no artifacts; callers treat
        this as "pruning disabled" rather than "delete everything" to avoid a
        foot-gun that wipes an entire backup directory.

        Returns:
            ``True`` if all four tiers are zero.
        """
        return self.daily == 0 and self.weekly == 0 and self.monthly == 0 and self.yearly == 0


@dataclass(frozen=True)
class BackupArtifact:
    """A single backup file paired with its parsed creation timestamp.

    Attributes:
        path: Absolute path to the ``.tar.gz`` artifact.
        timestamp: UTC-aware creation time parsed from the filename.
    """

    path: Path
    timestamp: datetime


# ---------------------------------------------------------------------------
# Timestamp parsing + artifact discovery
# ---------------------------------------------------------------------------


def parse_backup_timestamp(name: str) -> Optional[datetime]:
    """Parse the UTC creation time encoded in a backup artifact filename.

    The :mod:`skcapstone.backup` writer names artifacts
    ``backup-YYYYMMDD-HHMMSS-ffffff.tar.gz`` (the microsecond suffix is
    optional).  Parsing the name (rather than trusting filesystem mtime) keeps
    retention deterministic across copies, restores, and rsync.

    Args:
        name: A filename or path basename to parse.

    Returns:
        A UTC-aware :class:`~datetime.datetime`, or ``None`` when *name* does not
        match the backup naming convention.

    Example:
        >>> parse_backup_timestamp("backup-20260724-031500-123456.tar.gz").hour
        3
    """
    base = os.path.basename(name)
    m = _ARTIFACT_RE.match(base)
    if not m:
        return None
    date_part, time_part, micro_part = m.group(1), m.group(2), m.group(3)
    try:
        dt = datetime.strptime(date_part + time_part, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    if micro_part:
        dt = dt.replace(microsecond=int(micro_part.ljust(6, "0")))
    return dt.replace(tzinfo=timezone.utc)


def discover_artifacts(backup_dir: Path) -> list[BackupArtifact]:
    """Scan *backup_dir* for backup artifacts, newest first.

    Only files matching the backup naming convention are returned; unrelated
    files (including the ``gfs-state.json`` sidecar) are ignored, which is what
    keeps pruning from ever touching a foreign file.

    Args:
        backup_dir: Directory to scan.  A missing directory yields an empty list.

    Returns:
        List of :class:`BackupArtifact`, sorted by timestamp descending (newest
        first).
    """
    backup_dir = Path(backup_dir)
    if not backup_dir.is_dir():
        return []

    artifacts: list[BackupArtifact] = []
    for child in backup_dir.iterdir():
        if not child.is_file():
            continue
        ts = parse_backup_timestamp(child.name)
        if ts is None:
            continue
        artifacts.append(BackupArtifact(path=child.resolve(), timestamp=ts))

    artifacts.sort(key=lambda a: a.timestamp, reverse=True)
    return artifacts


# ---------------------------------------------------------------------------
# GFS retention (pure)
# ---------------------------------------------------------------------------


def _period_key(dt: datetime, tier: str) -> tuple:
    """Return a hashable bucket key for *dt* within a retention *tier*.

    Args:
        dt: A timezone-aware timestamp.
        tier: One of ``"daily"``, ``"weekly"``, ``"monthly"``, ``"yearly"``.

    Returns:
        A tuple uniquely identifying the calendar period *dt* falls in for the
        given tier (e.g. ISO year+week for ``"weekly"``).

    Raises:
        ValueError: If *tier* is not a recognised tier name.
    """
    if tier == "daily":
        return (dt.year, dt.month, dt.day)
    if tier == "weekly":
        iso = dt.isocalendar()
        return (iso[0], iso[1])
    if tier == "monthly":
        return (dt.year, dt.month)
    if tier == "yearly":
        return (dt.year,)
    raise ValueError(f"unknown retention tier: {tier!r}")


def _select_tier(artifacts_desc: list[BackupArtifact], count: int, tier: str) -> set[Path]:
    """Select the artifacts kept by a single retention tier.

    Walking newest-first, the first artifact seen in each distinct period is
    kept until *count* distinct periods have been retained.

    Args:
        artifacts_desc: Artifacts sorted newest-first.
        count: Number of distinct periods to keep.  ``0`` keeps none; a negative
            value keeps every period (unlimited).
        tier: The tier name passed to :func:`_period_key`.

    Returns:
        Set of resolved artifact paths kept by this tier.
    """
    if count == 0:
        return set()

    kept: set[Path] = set()
    seen_periods: set[tuple] = set()
    for art in artifacts_desc:
        key = _period_key(art.timestamp, tier)
        if key in seen_periods:
            continue
        seen_periods.add(key)
        kept.add(art.path)
        if count > 0 and len(seen_periods) >= count:
            break
    return kept


def select_gfs_retention(
    artifacts: list[BackupArtifact],
    policy: GFSPolicy,
) -> tuple[list[BackupArtifact], list[BackupArtifact]]:
    """Partition *artifacts* into keep / prune sets under a GFS *policy*.

    An artifact is kept if it is selected by *any* tier (daily, weekly, monthly,
    or yearly): for each tier the newest artifact within each of the most-recent
    ``N`` distinct periods is retained, and the overall keep set is the union.
    This matches borg/restic ``--keep-daily/--keep-weekly/--keep-monthly``.

    As a safety valve, a policy that keeps nothing on every tier
    (:meth:`GFSPolicy.is_noop`) is treated as "pruning disabled": all artifacts
    are kept and none are pruned.

    Args:
        artifacts: Backup artifacts (any order; sorted internally).
        policy: The retention counts to apply.

    Returns:
        A ``(keep, prune)`` tuple of artifact lists, each sorted newest-first.

    Example:
        >>> keep, prune = select_gfs_retention(arts, GFSPolicy(daily=7, weekly=4, monthly=6))
        >>> len(keep) + len(prune) == len(arts)
        True
    """
    ordered = sorted(artifacts, key=lambda a: a.timestamp, reverse=True)

    if policy.is_noop():
        return ordered, []

    keep_paths: set[Path] = set()
    keep_paths |= _select_tier(ordered, policy.daily, "daily")
    keep_paths |= _select_tier(ordered, policy.weekly, "weekly")
    keep_paths |= _select_tier(ordered, policy.monthly, "monthly")
    keep_paths |= _select_tier(ordered, policy.yearly, "yearly")

    keep = [a for a in ordered if a.path in keep_paths]
    prune = [a for a in ordered if a.path not in keep_paths]
    return keep, prune


def prune_artifacts(
    prune: list[BackupArtifact],
    backup_dir: Path,
    *,
    dry_run: bool = False,
) -> list[str]:
    """Delete pruned artifacts, refusing to touch anything outside *backup_dir*.

    Each candidate path is resolved and confirmed to live directly inside the
    resolved *backup_dir* and to match the backup naming convention before it is
    unlinked.  Any path failing those checks is skipped and logged - the job can
    never delete outside its own directory.

    Args:
        prune: Artifacts selected for deletion by :func:`select_gfs_retention`.
        backup_dir: The directory the job owns; deletions are confined to it.
        dry_run: When ``True``, report what would be deleted without unlinking.

    Returns:
        List of filenames actually deleted (or that would be, when *dry_run*).
    """
    root = Path(backup_dir).resolve()
    deleted: list[str] = []
    for art in prune:
        path = art.path.resolve()
        # Confinement: parent must be exactly the backup dir, name must match.
        if path.parent != root or not _ARTIFACT_RE.match(path.name):
            logger.warning("Refusing to prune out-of-scope path: %s", path)
            continue
        if dry_run:
            deleted.append(path.name)
            continue
        try:
            path.unlink()
            deleted.append(path.name)
        except OSError as exc:
            logger.warning("Failed to prune %s: %s", path, exc)
    return deleted


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


@dataclass
class BackupJobConfig:
    """Resolved configuration for the GFS backup job and monitor.

    Attributes:
        backup_dir: Directory where artifacts are written and pruned.
        policy: The GFS retention policy.
        max_age_seconds: Freshness threshold for the monitor.
        min_interval_seconds: Skip creating a new backup when the newest existing
            one is younger than this (``0`` = always create).
        agent_name: Agent name recorded in the backup manifest.
    """

    backup_dir: Path
    policy: GFSPolicy
    max_age_seconds: float = DEFAULT_MAX_AGE_SECONDS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL_SECONDS
    agent_name: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


def _read_config_block(home: Path) -> dict[str, Any]:
    """Read the ``backup:`` block from ``<home>/config/config.yaml``.

    Args:
        home: Agent home directory.

    Returns:
        The ``backup`` sub-mapping, or an empty dict when absent or unreadable.
        Config is best-effort: a missing file or parse error never raises.
    """
    cfg_path = Path(home) / "config" / "config.yaml"
    if not cfg_path.is_file():
        return {}
    try:
        import yaml

        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - config is best-effort
        logger.debug("Failed to read backup config: %s", exc)
        return {}
    block = data.get("backup")
    return block if isinstance(block, dict) else {}


def _env_int(name: str, default: int) -> int:
    """Return an int from environment variable *name*, or *default*."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("Invalid int for %s=%r - using default %d", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    """Return a float from environment variable *name*, or *default*."""
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("Invalid float for %s=%r - using default %s", name, raw, default)
        return default


def resolve_config(
    home: Path,
    agent_name: str = "",
    overrides: Optional[dict[str, Any]] = None,
) -> BackupJobConfig:
    """Resolve the effective backup-job config from defaults, YAML, and env.

    Precedence (lowest to highest): built-in defaults < ``config.yaml``
    ``backup:`` block < ``SKCAPSTONE_BACKUP_*`` environment variables <
    explicit *overrides* dict (used by tests and callers).

    Recognised keys (YAML) / env vars:
        ``dir`` / ``SKCAPSTONE_BACKUP_DIR``
        ``keep_daily`` / ``SKCAPSTONE_BACKUP_KEEP_DAILY``
        ``keep_weekly`` / ``SKCAPSTONE_BACKUP_KEEP_WEEKLY``
        ``keep_monthly`` / ``SKCAPSTONE_BACKUP_KEEP_MONTHLY``
        ``keep_yearly`` / ``SKCAPSTONE_BACKUP_KEEP_YEARLY``
        ``max_age_seconds`` / ``SKCAPSTONE_BACKUP_MAX_AGE_SECONDS``
        ``min_interval_seconds`` / ``SKCAPSTONE_BACKUP_MIN_INTERVAL_SECONDS``

    Args:
        home: Agent home directory (source of ``config.yaml`` and the default
            backup dir ``<home>/backups``).
        agent_name: Agent name recorded in the manifest.
        overrides: Highest-precedence explicit values.

    Returns:
        A fully-resolved :class:`BackupJobConfig`.
    """
    home = Path(home)
    block = _read_config_block(home)
    ov = overrides or {}

    def pick(key: str, env: str, default: Any, caster: Callable[[str], Any]) -> Any:
        if key in ov and ov[key] is not None:
            return ov[key]
        if env in os.environ and os.environ[env].strip() != "":
            if caster is int:
                return _env_int(env, default)
            if caster is float:
                return _env_float(env, default)
            return os.environ[env]
        if key in block and block[key] is not None:
            return block[key]
        return default

    dir_val = pick("dir", "SKCAPSTONE_BACKUP_DIR", None, str)
    backup_dir = Path(dir_val).expanduser() if dir_val else (home / "backups")

    policy = GFSPolicy(
        daily=int(pick("keep_daily", "SKCAPSTONE_BACKUP_KEEP_DAILY", DEFAULT_KEEP_DAILY, int)),
        weekly=int(pick("keep_weekly", "SKCAPSTONE_BACKUP_KEEP_WEEKLY", DEFAULT_KEEP_WEEKLY, int)),
        monthly=int(
            pick("keep_monthly", "SKCAPSTONE_BACKUP_KEEP_MONTHLY", DEFAULT_KEEP_MONTHLY, int)
        ),
        yearly=int(pick("keep_yearly", "SKCAPSTONE_BACKUP_KEEP_YEARLY", DEFAULT_KEEP_YEARLY, int)),
    )

    return BackupJobConfig(
        backup_dir=backup_dir,
        policy=policy,
        max_age_seconds=float(
            pick(
                "max_age_seconds",
                "SKCAPSTONE_BACKUP_MAX_AGE_SECONDS",
                DEFAULT_MAX_AGE_SECONDS,
                float,
            )
        ),
        min_interval_seconds=float(
            pick(
                "min_interval_seconds",
                "SKCAPSTONE_BACKUP_MIN_INTERVAL_SECONDS",
                DEFAULT_MIN_INTERVAL_SECONDS,
                float,
            )
        ),
        agent_name=agent_name,
    )


# ---------------------------------------------------------------------------
# State sidecar
# ---------------------------------------------------------------------------


def _write_state(backup_dir: Path, state: dict[str, Any]) -> None:
    """Atomically write the ``gfs-state.json`` sidecar into *backup_dir*.

    Args:
        backup_dir: The backup directory (created if absent).
        state: JSON-serialisable state mapping.
    """
    import json

    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    final = backup_dir / STATE_FILENAME
    tmp = backup_dir / f".{STATE_FILENAME}.tmp"
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    tmp.rename(final)


def _read_state(backup_dir: Path) -> dict[str, Any]:
    """Read the ``gfs-state.json`` sidecar, or ``{}`` when absent/invalid.

    Args:
        backup_dir: The backup directory.

    Returns:
        The parsed state mapping (empty on any failure).
    """
    import json

    path = Path(backup_dir) / STATE_FILENAME
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


# ---------------------------------------------------------------------------
# Job wrapper
# ---------------------------------------------------------------------------


def run_backup_job(
    home: Optional[Path] = None,
    config: Optional[BackupJobConfig] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Create a backup, write it to the backup dir, and apply GFS pruning.

    Steps:
        1. Resolve config (unless *config* is supplied).
        2. If ``min_interval_seconds`` is set and the newest existing artifact is
           younger than it, skip creation (still records state) - extra idempotency.
        3. Otherwise call :func:`~skcapstone.backup.create_backup` writing into the
           configured backup dir.
        4. Re-scan the dir and prune per :func:`select_gfs_retention`.
        5. Write the ``gfs-state.json`` sidecar and return a status dict.

    A failure in the underlying backup is caught and recorded in state (so the
    monitor can flag ``failed``); the exception is re-raised to the caller after
    state is persisted.

    Args:
        home: Agent home directory.  Defaults to the package ``AGENT_HOME``.
        config: Pre-resolved config.  When ``None``, resolved from *home*.
        now: Reference time (UTC) for the interval guard and state stamp.

    Returns:
        Status dict with keys: ``created`` (bool), ``backup_id``, ``filepath``,
        ``kept`` (count), ``pruned`` (count), ``pruned_files`` (list),
        ``backup_dir``, ``skipped`` (bool), ``error`` (str|None).
    """
    from . import AGENT_HOME

    now = now or datetime.now(timezone.utc)
    home_path = Path(home).expanduser() if home else Path(AGENT_HOME).expanduser()
    cfg = config or resolve_config(home_path, agent_name=cfg_agent_name(home_path))

    backup_dir = Path(cfg.backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)

    # Interval guard (idempotency helper): skip a fresh-enough directory.
    existing = discover_artifacts(backup_dir)
    if cfg.min_interval_seconds > 0 and existing:
        age = (now - existing[0].timestamp).total_seconds()
        if age < cfg.min_interval_seconds:
            logger.info(
                "Newest backup is %.0fs old (< min_interval %.0fs) - skipping create",
                age,
                cfg.min_interval_seconds,
            )
            keep, prune = select_gfs_retention(existing, cfg.policy)
            pruned_files = prune_artifacts(prune, backup_dir)
            result = {
                "created": False,
                "skipped": True,
                "backup_id": existing[0].path.stem.replace(".tar", ""),
                "filepath": str(existing[0].path),
                "kept": len(keep),
                "pruned": len(pruned_files),
                "pruned_files": pruned_files,
                "backup_dir": str(backup_dir),
                "error": None,
            }
            _record_run_state(backup_dir, result, now, ok=True)
            return result

    from .backup import create_backup

    error: Optional[str] = None
    backup_result: dict[str, Any] = {}
    try:
        backup_result = create_backup(
            home=home_path,
            output_dir=backup_dir,
            agent_name=cfg.agent_name,
        )
    except Exception as exc:  # noqa: BLE001 - record then re-raise
        error = str(exc)
        logger.error("GFS backup create failed: %s", exc)
        result = {
            "created": False,
            "skipped": False,
            "backup_id": None,
            "filepath": None,
            "kept": len(existing),
            "pruned": 0,
            "pruned_files": [],
            "backup_dir": str(backup_dir),
            "error": error,
        }
        _record_run_state(backup_dir, result, now, ok=False)
        raise

    # Re-scan (now includes the just-written artifact) and prune.
    artifacts = discover_artifacts(backup_dir)
    keep, prune = select_gfs_retention(artifacts, cfg.policy)
    pruned_files = prune_artifacts(prune, backup_dir)

    result = {
        "created": True,
        "skipped": False,
        "backup_id": backup_result.get("backup_id"),
        "filepath": backup_result.get("filepath"),
        "kept": len(keep),
        "pruned": len(pruned_files),
        "pruned_files": pruned_files,
        "backup_dir": str(backup_dir),
        "error": None,
    }
    _record_run_state(backup_dir, result, now, ok=True)
    logger.info(
        "GFS backup: created %s, kept %d, pruned %d in %s",
        result["backup_id"],
        result["kept"],
        result["pruned"],
        backup_dir,
    )
    return result


def _record_run_state(
    backup_dir: Path,
    result: dict[str, Any],
    now: datetime,
    *,
    ok: bool,
) -> None:
    """Persist the outcome of a backup run to the state sidecar.

    Args:
        backup_dir: The backup directory.
        result: The run result dict.
        now: Run timestamp (UTC).
        ok: Whether the run succeeded.
    """
    state = _read_state(backup_dir)
    state["last_run_at"] = now.isoformat()
    state["last_run_ok"] = ok
    state["last_error"] = result.get("error")
    if ok and not result.get("error"):
        state["last_success_at"] = now.isoformat()
        if result.get("backup_id"):
            state["last_backup_id"] = result["backup_id"]
    state["last_kept"] = result.get("kept")
    state["last_pruned"] = result.get("pruned")
    _write_state(backup_dir, state)


def cfg_agent_name(home: Path) -> str:
    """Best-effort agent name for the manifest, from env or the home dir name.

    Args:
        home: Agent home directory.

    Returns:
        The active agent name, or the home directory's own name as a fallback.
    """
    name = (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
    )
    if name:
        return name
    # agents/<name>/ layout - use the leaf dir name when it looks agent-shaped.
    home = Path(home)
    if home.parent.name == "agents":
        return home.name
    return ""


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


def check_backup_health(
    home: Optional[Path] = None,
    config: Optional[BackupJobConfig] = None,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Report backup freshness and flag missing / stale / failed states.

    Status semantics:
        ``missing`` - no backup artifacts exist in the backup dir.
        ``failed``  - the last recorded run errored (from ``gfs-state.json``).
        ``stale``   - newest artifact is older than ``max_age_seconds``.
        ``ok``      - a fresh backup exists and the last run succeeded.

    Args:
        home: Agent home directory.  Defaults to the package ``AGENT_HOME``.
        config: Pre-resolved config.  When ``None``, resolved from *home*.
        now: Reference "current" time (UTC).

    Returns:
        A status object dict: ``status``, ``healthy`` (bool), ``backup_dir``,
        ``backup_count``, ``newest_backup`` (filename|None), ``newest_at``
        (ISO|None), ``age_seconds`` (float|None), ``threshold_seconds``,
        ``last_error`` (str|None), ``message`` (human summary).
    """
    from . import AGENT_HOME

    now = now or datetime.now(timezone.utc)
    home_path = Path(home).expanduser() if home else Path(AGENT_HOME).expanduser()
    cfg = config or resolve_config(home_path, agent_name=cfg_agent_name(home_path))

    backup_dir = Path(cfg.backup_dir)
    artifacts = discover_artifacts(backup_dir)
    state = _read_state(backup_dir)
    last_error = state.get("last_error")
    last_run_ok = state.get("last_run_ok", True)

    base: dict[str, Any] = {
        "backup_dir": str(backup_dir),
        "backup_count": len(artifacts),
        "threshold_seconds": cfg.max_age_seconds,
        "last_error": last_error,
    }

    if not artifacts:
        return {
            **base,
            "status": "missing",
            "healthy": False,
            "newest_backup": None,
            "newest_at": None,
            "age_seconds": None,
            "message": f"No backups found in {backup_dir}",
        }

    newest = artifacts[0]
    age = (now - newest.timestamp).total_seconds()

    # A recorded failure outranks a merely-fresh artifact: the last run may have
    # errored after an older artifact was already present.
    if last_run_ok is False or last_error:
        return {
            **base,
            "status": "failed",
            "healthy": False,
            "newest_backup": newest.path.name,
            "newest_at": newest.timestamp.isoformat(),
            "age_seconds": age,
            "message": f"Last backup run failed: {last_error or 'unknown error'}",
        }

    if age > cfg.max_age_seconds:
        return {
            **base,
            "status": "stale",
            "healthy": False,
            "newest_backup": newest.path.name,
            "newest_at": newest.timestamp.isoformat(),
            "age_seconds": age,
            "message": (
                f"Newest backup {newest.path.name} is {age / 3600:.1f}h old "
                f"(threshold {cfg.max_age_seconds / 3600:.1f}h)"
            ),
        }

    return {
        **base,
        "status": "ok",
        "healthy": True,
        "newest_backup": newest.path.name,
        "newest_at": newest.timestamp.isoformat(),
        "age_seconds": age,
        "message": f"Backup fresh: {newest.path.name} ({age / 3600:.1f}h old)",
    }


def _send_alert(message: str, level: str = "warn") -> None:
    """Best-effort ``sk-alert`` notification; never raises.

    Mirrors the notification path used by
    :meth:`skcapstone.scheduled_tasks.TaskScheduler._maybe_notify`.

    Args:
        message: Alert body.
        level: sk-alert level (``info``/``warn``/``error``).
    """
    alert = shutil.which("sk-alert") or os.path.expanduser("~/.skenv/bin/sk-alert")
    try:
        subprocess.run([alert, "-l", level, message], timeout=30, check=False)
    except Exception as exc:  # noqa: BLE001 - alerting must never break the caller
        logger.warning("backup alert failed: %s", exc)


# ---------------------------------------------------------------------------
# Scheduler callbacks (zero-arg entrypoints for jobs.yaml type=python)
# ---------------------------------------------------------------------------


def run_scheduled_backup() -> None:
    """Zero-arg entrypoint: run the GFS backup job for the active agent.

    Suitable as a ``type: python`` job callback
    (``skcapstone.gfs_backup:run_scheduled_backup``).  Resolves the active
    agent's home, then delegates to :func:`run_backup_job`.  Exceptions
    propagate so the scheduler records the failure and can notify.
    """
    from . import AGENT_HOME, agent_home

    try:
        home = agent_home(cfg_agent_name(Path(AGENT_HOME)) or None)
    except Exception:  # noqa: BLE001 - fall back to the shared root
        home = Path(AGENT_HOME)
    run_backup_job(home=home)


def make_backup_monitor_task(
    home: Optional[Path] = None,
    *,
    alert: Optional[bool] = None,
) -> Callable[[], None]:
    """Return a zero-arg scheduler callback that checks backup freshness.

    The callback runs :func:`check_backup_health`, logs at WARNING when unhealthy
    (DEBUG when fresh), and optionally fires an ``sk-alert``.  Alerting defaults
    to the ``SKCAPSTONE_BACKUP_ALERT`` env flag (``1``/``true`` enables it) so it
    stays quiet unless the operator opts in.

    Args:
        home: Agent home directory (defaults to the active agent's home).
        alert: Force alerting on/off.  ``None`` consults the env flag.

    Returns:
        A zero-arg callable for ``TaskScheduler.register`` or a python job.
    """

    def _run() -> None:
        report = check_backup_health(home=home)
        if report["healthy"]:
            logger.debug("Backup health: %s", report["message"])
            return
        logger.warning("Backup health [%s]: %s", report["status"], report["message"])

        want_alert = alert
        if want_alert is None:
            want_alert = os.environ.get("SKCAPSTONE_BACKUP_ALERT", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        if want_alert:
            level = "error" if report["status"] in {"failed", "missing"} else "warn"
            _send_alert(f"💾 backup {report['status']}: {report['message']}", level=level)

    return _run


def run_backup_monitor() -> None:
    """Zero-arg entrypoint: check backup freshness and alert if unhealthy.

    Suitable as a ``type: python`` job callback
    (``skcapstone.gfs_backup:run_backup_monitor``).  Alerting follows the
    ``SKCAPSTONE_BACKUP_ALERT`` env flag (see :func:`make_backup_monitor_task`).
    """
    make_backup_monitor_task()()
