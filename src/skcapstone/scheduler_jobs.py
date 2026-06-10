"""skscheduler — JobSpec dataclass, YAML loader, node-affinity resolution,
due-check (cron + interval), and host-alias discovery.

This module is the foundation of the unified fleet job scheduler.  It is
intentionally free of I/O side-effects beyond reading config files and the
environment; all scheduling state lives elsewhere.

Typical usage::

    from pathlib import Path
    from skcapstone.scheduler_jobs import load_jobs, job_runs_here, is_due, current_host_aliases

    jobs = load_jobs(Path("~/.skcapstone/config/jobs.yaml").expanduser())
    aliases = current_host_aliases()
    for job in jobs:
        if job.enabled and job_runs_here(job, aliases) and is_due(job, last_run):
            dispatch(job)
"""

from __future__ import annotations

import os
import re
import socket
import warnings
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Union


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_DURATION_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*$")
_UNIT_SECONDS: dict[str, float] = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def _parse_duration(value: Union[str, int, float]) -> float:
    """Convert a human-readable duration string or plain number to seconds.

    Args:
        value: A string like ``"300s"``, ``"5m"``, ``"1h"``, ``"1d"``, or a
            plain numeric value (int or float treated as seconds already).

    Returns:
        Duration in seconds as a float.

    Raises:
        ValueError: If the string is unparseable, contains a negative value,
            or has an unrecognised suffix.

    Examples:
        >>> _parse_duration("300s")
        300.0
        >>> _parse_duration("5m")
        300.0
        >>> _parse_duration(600)
        600.0
    """
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"duration must be non-negative, got {value!r}")
        return float(value)
    m = _DURATION_RE.match(str(value))
    if not m:
        raise ValueError(f"invalid duration: {value!r}")
    return float(m.group(1)) * _UNIT_SECONDS[m.group(2)]


# ---------------------------------------------------------------------------
# Group A — JobSpec dataclass + load_jobs
# ---------------------------------------------------------------------------

@dataclass
class JobSpec:
    """Describes a single scheduled job as loaded from ``jobs.yaml``.

    Attributes:
        name: Unique job identifier (the YAML key).
        type: Job type — ``"python"``, ``"shell"``, or ``"agent"``.
        schedule: Cron expression (mutually exclusive with ``every_seconds``).
        every_seconds: Interval in seconds (mutually exclusive with ``schedule``).
        nodes: Node-affinity list of host aliases, or the string ``"all"``.
        agent: Agent name for ``type="agent"`` jobs.
        prompt: Prompt text for ``type="agent"`` jobs.
        command: Shell command for ``type="shell"`` jobs.
        callback: Dotted ``module:function`` path for ``type="python"`` jobs.
        timeout: Hard-kill timeout in seconds.
        enabled: Whether the job is active.
    """

    name: str
    type: str = "python"
    schedule: Optional[str] = None
    every_seconds: Optional[float] = None
    nodes: Union[str, list[str]] = "all"
    agent: Optional[str] = None
    prompt: Optional[str] = None
    command: Optional[str] = None
    callback: Optional[str] = None
    timeout: float = 900.0
    enabled: bool = True
    # --- reliability / fleet / observability (added 2026-06-09) ---
    retries: int = 0                 # extra attempts on failure (0 = run once)
    retry_backoff: float = 0.0       # seconds between attempts (linear)
    jitter: float = 0.0              # max random splay (s) before dispatch — avoids
                                     #   fleet thundering-herd on shared cron slots
    notify: str = "off"              # off | on_failure | on_success | always (sk-alert hook)
    notify_level: str = "warn"       # sk-alert level for failure notifications


def load_jobs(config_path: Path) -> list[JobSpec]:
    """Load job definitions from a ``jobs.yaml`` config file.

    The YAML file must have a top-level ``jobs`` mapping.  Each key becomes
    the ``name`` of the resulting :class:`JobSpec`.  The ``every`` field is
    parsed via :func:`_parse_duration` and stored as ``every_seconds``; the
    raw ``every`` key is consumed and not passed to the dataclass.

    Args:
        config_path: Path to the ``jobs.yaml`` file.  If the file does not
            exist, an empty list is returned without raising.

    Returns:
        A list of :class:`JobSpec` instances in definition order.

    Example::

        jobs = load_jobs(Path("~/.skcapstone/config/jobs.yaml").expanduser())
    """
    if not config_path.exists():
        return []

    import yaml  # lazy import — pyyaml optional at module level

    with config_path.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    jobs_raw: dict = (data or {}).get("jobs") or {}
    result: list[JobSpec] = []

    _KNOWN_KEYS = {
        "type", "schedule", "every", "nodes", "agent", "prompt",
        "command", "callback", "timeout", "enabled",
        "retries", "retry_backoff", "jitter", "notify", "notify_level",
    }

    for name, raw in jobs_raw.items():
        raw = dict(raw or {})

        # Warn on unrecognised keys before consuming 'every'
        unknown = set(raw.keys()) - _KNOWN_KEYS
        if unknown:
            warnings.warn(
                f"Job {name!r} has unrecognised key(s): {sorted(unknown)}. "
                "Typo in config? Job may not behave as expected.",
                UserWarning,
                stacklevel=2,
            )

        # Convert 'every' → 'every_seconds'
        every_raw = raw.pop("every", None)
        every_seconds: Optional[float] = None
        if every_raw is not None:
            every_seconds = _parse_duration(every_raw)

        result.append(
            JobSpec(
                name=name,
                type=raw.get("type", "python"),
                schedule=raw.get("schedule"),
                every_seconds=every_seconds,
                nodes=raw.get("nodes", "all"),
                agent=raw.get("agent"),
                prompt=raw.get("prompt"),
                command=raw.get("command"),
                callback=raw.get("callback"),
                timeout=float(raw.get("timeout", 900.0)),
                enabled=bool(raw.get("enabled", True)),
                retries=int(raw.get("retries", 0)),
                retry_backoff=float(raw.get("retry_backoff", 0.0)),
                jitter=float(raw.get("jitter", 0.0)),
                notify=str(raw.get("notify", "off")),
                notify_level=str(raw.get("notify_level", "warn")),
            )
        )

    return result


# ---------------------------------------------------------------------------
# Group A2 — jobs.d/ drop-in registration (added 2026-06-09)
# ---------------------------------------------------------------------------

def load_jobs_with_dropins(config_path: Path) -> list[JobSpec]:
    """Load jobs from ``jobs.yaml`` plus every ``jobs.d/*.yaml`` drop-in.

    This is the conf.d-style merge that lets external sk* services
    self-register scheduled work without editing the shared ``jobs.yaml``.
    The base file is loaded first, then each ``jobs.d/<name>.yaml`` (sorted
    by filename) is overlaid.  When two sources define the same job *name*,
    the later (drop-in) definition wins and a :class:`UserWarning` is emitted.

    The drop-in directory is resolved as ``config_path.parent / "jobs.d"``.

    Args:
        config_path: Path to the base ``jobs.yaml``.  Neither the base file
            nor the drop-in directory need exist; missing sources are
            silently skipped (an empty list is returned when nothing exists).

    Returns:
        Merged list of :class:`JobSpec` instances, base jobs first followed
        by drop-in-only jobs, in deterministic order.

    Example::

        jobs = load_jobs_with_dropins(
            Path("~/.skcapstone/config/jobs.yaml").expanduser()
        )
    """
    merged: dict[str, JobSpec] = {}

    for spec in load_jobs(config_path):
        merged[spec.name] = spec

    dropin_dir = config_path.parent / "jobs.d"
    if dropin_dir.is_dir():
        for fragment in sorted(dropin_dir.glob("*.yaml")):
            for spec in load_jobs(fragment):
                if spec.name in merged:
                    warnings.warn(
                        f"Job {spec.name!r} in drop-in {fragment.name!r} "
                        f"overrides an earlier definition.",
                        UserWarning,
                        stacklevel=2,
                    )
                merged[spec.name] = spec

    return list(merged.values())


def _dropin_dir(home: Optional[Path] = None) -> Path:
    """Return the ``config/jobs.d`` drop-in directory under *home*."""
    base = Path(home) if home else Path("~/.skcapstone").expanduser()
    return base / "config" / "jobs.d"


def register_job(spec: dict, home: Optional[Path] = None) -> Path:
    """Register a scheduled job by writing a ``jobs.d/<name>.yaml`` fragment.

    This is the programmatic counterpart to hand-editing ``jobs.yaml`` — it
    lets a service own its own scheduler entry.  The fragment is written
    atomically; calling again with the same ``name`` overwrites it (idempotent
    re-registration on every service start is the intended pattern).

    Args:
        spec: A single job definition.  Must contain ``name`` and exactly one
            of ``schedule`` or ``every``.  Remaining keys mirror the
            ``jobs.yaml`` schema (``type``, ``command``/``callback``/``agent``,
            ``nodes``, ``timeout``, ``retries``, ``notify`` …).
        home: skcapstone root (defaults to ``~/.skcapstone``).

    Returns:
        Path to the written ``jobs.d/<name>.yaml`` fragment.

    Raises:
        ValueError: If ``name`` is missing or neither ``schedule`` nor
            ``every`` is present.
    """
    import yaml  # lazy — pyyaml optional at module level

    spec = dict(spec)
    name = spec.pop("name", None)
    if not name:
        raise ValueError("register_job: spec must include a 'name'")
    if "schedule" not in spec and "every" not in spec:
        raise ValueError(
            f"register_job: job {name!r} must define 'schedule' or 'every'"
        )

    dropin = _dropin_dir(home)
    dropin.mkdir(parents=True, exist_ok=True)

    final = dropin / f"{name}.yaml"
    tmp = dropin / f".{name}.yaml.tmp"
    tmp.write_text(
        yaml.safe_dump({"jobs": {name: spec}}, sort_keys=False),
        encoding="utf-8",
    )
    tmp.rename(final)
    return final


def unregister_job(name: str, home: Optional[Path] = None) -> bool:
    """Remove a previously registered ``jobs.d/<name>.yaml`` fragment.

    Args:
        name: The job name used at registration.
        home: skcapstone root (defaults to ``~/.skcapstone``).

    Returns:
        ``True`` if a fragment existed and was removed, ``False`` otherwise.
    """
    fragment = _dropin_dir(home) / f"{name}.yaml"
    if fragment.exists():
        fragment.unlink()
        return True
    return False


# ---------------------------------------------------------------------------
# Group B — node affinity
# ---------------------------------------------------------------------------

def job_runs_here(job: JobSpec, host_aliases: set[str]) -> bool:
    """Return ``True`` if *job* should fire on the current node.

    Args:
        job: The :class:`JobSpec` to evaluate.
        host_aliases: The set of aliases that identify the current host
            (see :func:`current_host_aliases`).

    Returns:
        ``True`` when ``job.nodes == "all"`` or when any alias in
        ``job.nodes`` is present in *host_aliases*.

    Example::

        aliases = current_host_aliases()
        if job_runs_here(job, aliases):
            dispatch(job)
    """
    if job.nodes == "all":
        return True
    node_list: list[str] = job.nodes if isinstance(job.nodes, list) else [job.nodes]
    return bool(set(node_list) & host_aliases)


# ---------------------------------------------------------------------------
# Group C — due-check cron + interval with misfire catch-up
# ---------------------------------------------------------------------------

def is_due(
    job: JobSpec,
    last_run: Optional[datetime],
    now: Optional[datetime] = None,
) -> bool:
    """Return ``True`` if *job* is due to run relative to *last_run*.

    Interval jobs (``every_seconds`` set):
        - Never run before → due immediately.
        - Otherwise due when ``now - last_run >= every_seconds``.

    Cron jobs (``schedule`` set):
        - Never run before → due immediately (catches up on first start).
        - Otherwise due when ``last_run`` is *before* the most recent cron
          slot that has already elapsed (misfire/catch-up: at most one fire
          per missed interval, not one per missed slot).

    Jobs with neither field → never due (returns ``False``).

    Args:
        job: The :class:`JobSpec` to evaluate.
        last_run: UTC-aware datetime of the last successful run, or ``None``
            if the job has never run.
        now: Reference "current" time (UTC-aware); defaults to
            ``datetime.now(timezone.utc)``.

    Returns:
        ``True`` if the job should be dispatched now.

    Example::

        if is_due(job, state.last_run):
            dispatch(job)
    """
    if now is None:
        now = datetime.now(timezone.utc)

    # Ensure *now* is tz-aware (default UTC if naive)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    # --- Interval ---
    if job.every_seconds is not None:
        if last_run is None:
            return True
        lr = last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
        elapsed = (now - lr).total_seconds()
        return elapsed >= job.every_seconds

    # --- Cron ---
    if job.schedule is not None:
        if last_run is None:
            return True

        from croniter import croniter  # lazy import

        # croniter.get_prev returns the most recent past slot <= now
        cron = croniter(job.schedule, now)
        prev_slot: datetime = cron.get_prev(datetime)

        # Ensure prev_slot is tz-aware
        if prev_slot.tzinfo is None:
            prev_slot = prev_slot.replace(tzinfo=timezone.utc)

        lr = last_run if last_run.tzinfo else last_run.replace(tzinfo=timezone.utc)
        return lr < prev_slot

    # No schedule defined → never due
    return False


# ---------------------------------------------------------------------------
# Group D — host alias discovery
# ---------------------------------------------------------------------------

def current_host_aliases() -> set[str]:
    """Return the set of aliases that identify the current host.

    Combines:
    - ``socket.gethostname()`` — the OS hostname.
    - Comma-separated values from the ``SK_NODE_ALIAS`` environment variable
      (stripped, non-empty tokens only).

    Returns:
        A :class:`set` of strings usable for node-affinity matching.

    Example::

        # With SK_NODE_ALIAS=".41" set in the environment:
        aliases = current_host_aliases()
        # e.g. {'my-host', '.41'}   — hostname + SK_NODE_ALIAS token
    """
    aliases: set[str] = {socket.gethostname()}
    env_alias = os.environ.get("SK_NODE_ALIAS", "")
    for token in env_alias.split(","):
        stripped = token.strip()
        if stripped:
            aliases.add(stripped)
    return aliases
