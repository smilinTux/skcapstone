"""skcapstone.sdk — the stable public integration facade for sk* services.

This module is the **only** surface that downstream sk* services
(skmemory, skcomms/skcomms, skchat, sksecurity, capauth, skvoice, skseed,
cloud9, skgateway, …) should import.  Everything here is semver-tracked and
will not break across minor releases; the internal modules it wraps
(``pubsub``, ``scheduler_jobs``, ``coordination``, ``notifications``,
``service_health``) are NOT part of the public contract and may change freely.

The intended consumer pattern is *optional-by-presence, default-on*::

    try:
        from skcapstone import sdk as _sk
        _HAS = (not os.environ.get("SK_STANDALONE")) and _sk.is_available()
    except ImportError:
        _sk, _HAS = None, False

    def alert(topic, payload, level="info"):
        if _HAS:
            return _sk.alert(f"myservice.{topic}", payload, level=level,
                             notify=level in ("warn", "error", "critical"))
        return _native_alert(topic, payload, level)   # service-native fallback

A service that finds skcapstone installed routes alerts through the shared
PubSub bus and registers scheduled work with the fleet scheduler; a service
that does not (or that sets ``SK_STANDALONE=1``) keeps using its own
mechanisms.  See ``docs/ADR-optional-integration-backbone.md``.

Public API:
    is_available()      -> bool
    alert(...)          -> bool
    register_job(...)   -> str (path)
    unregister_job(...) -> bool
    coord_create(...)   -> str (task id)
    register_service(...) -> str (path)

Topic naming convention: ``<service>.<severity>`` (e.g. ``skmemory.error``,
``sksecurity.critical``).  Severities: ``info | warn | error | critical``.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("skcapstone.sdk")

__all__ = [
    "is_available",
    "alert",
    "register_job",
    "unregister_job",
    "coord_create",
    "register_service",
    "SEVERITIES",
]

#: Recognised alert severities, low → high.
SEVERITIES = ("info", "warn", "error", "critical")

#: Severities that, by convention, also raise a desktop/Telegram notification
#: when ``notify`` is left at its default.
_NOTIFY_SEVERITIES = frozenset({"warn", "error", "critical"})

#: severity → desktop notification urgency
_URGENCY = {
    "info": "low",
    "warn": "normal",
    "error": "normal",
    "critical": "critical",
}


def _shared_home() -> Path:
    """Resolve the shared skcapstone root (~/.skcapstone), honouring env."""
    from . import shared_home  # local import keeps facade import cheap

    return shared_home()


def _agent_name() -> str:
    """Best-effort active agent name, or 'anonymous'."""
    from . import active_agent_name

    return active_agent_name() or "anonymous"


def is_available(require_daemon: bool = False) -> bool:
    """Return whether skcapstone integration is usable from this process.

    Because the alert bus, scheduler drop-ins and coordination board are all
    file-based, in-process integration does *not* require the daemon to be
    running — it only requires that the shared home is resolvable and
    writable.  ``is_available()`` therefore returns ``True`` whenever the
    package imported and the home directory can be created.

    Args:
        require_daemon: When ``True``, additionally probe the local daemon's
            ``/health`` endpoint and only return ``True`` if it answers.  Use
            this for capabilities that genuinely need the live daemon (most
            consumers do not).

    Returns:
        ``True`` if skcapstone integration can be used, else ``False``.
    """
    try:
        home = _shared_home()
        home.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("skcapstone unavailable: %s", exc)
        return False

    if not require_daemon:
        return True

    return _daemon_healthy()


def _daemon_healthy() -> bool:
    """Probe the local skcapstone daemon ``/health`` endpoint (best-effort)."""
    import urllib.request

    port = int(os.environ.get("SKCAPSTONE_PORT", "9383"))
    url = os.environ.get("SKCAPSTONE_DAEMON_URL", f"http://127.0.0.1:{port}") + "/health"
    try:
        with urllib.request.urlopen(url, timeout=1.5) as resp:  # noqa: S310 (localhost)
            return 200 <= resp.status < 300
    except Exception as exc:
        logger.debug("daemon health probe failed (%s): %s", url, exc)
        return False


def alert(
    topic: str,
    payload: dict[str, Any],
    *,
    level: str = "info",
    notify: Optional[bool] = None,
    ttl_seconds: int = 86400,
) -> bool:
    """Publish an alert to the shared bus, optionally raising a notification.

    The alert is published to the PubSub topic ``topic`` (callers should use
    the ``<service>.<severity>`` convention).  When ``notify`` is true — or is
    left ``None`` and ``level`` is warn/error/critical — a desktop/Telegram
    notification is also dispatched via the notification manager.

    Args:
        topic: Fully-qualified topic, e.g. ``"skmemory.error"``.
        payload: JSON-serialisable event body.
        level: One of :data:`SEVERITIES`.  Unknown values are treated as
            ``"info"``.
        notify: Force notification on/off.  ``None`` (default) means "notify
            iff severity is warn or higher".
        ttl_seconds: Message TTL on the bus (default 24h).

    Returns:
        ``True`` if the message was published (notification is best-effort and
        does not affect the return value).
    """
    if level not in SEVERITIES:
        level = "info"

    published = False
    try:
        from .pubsub import PubSub

        bus = PubSub(_shared_home(), agent_name=_agent_name())
        bus.publish(topic, dict(payload), ttl_seconds=ttl_seconds, tags=[level])
        published = True
    except Exception as exc:
        logger.warning("sdk.alert publish failed for %r: %s", topic, exc)

    should_notify = (level in _NOTIFY_SEVERITIES) if notify is None else bool(notify)
    if should_notify:
        try:
            from .notifications import notify as _desktop_notify

            summary = payload.get("message") or payload.get("error") or json.dumps(payload)[:200]
            _desktop_notify(f"[{level}] {topic}", str(summary), _URGENCY.get(level, "normal"))
        except Exception as exc:  # pragma: no cover - notification is optional
            logger.debug("sdk.alert notify failed: %s", exc)

    return published


def register_job(spec: dict[str, Any], home: Optional[Path] = None) -> str:
    """Register a scheduled job with the fleet scheduler (jobs.d drop-in).

    Thin wrapper over :func:`skcapstone.scheduler_jobs.register_job`.  The
    ``spec`` must include a ``name`` and exactly one of ``schedule`` (cron) or
    ``every`` (interval, e.g. ``"15m"``).  Re-registering the same ``name`` is
    idempotent, so calling this on every service start is the intended usage.

    Args:
        spec: Job definition (see jobs.yaml schema).
        home: Override skcapstone root (defaults to ~/.skcapstone).

    Returns:
        Filesystem path to the written drop-in fragment, as a string.
    """
    from .scheduler_jobs import register_job as _register_job

    return str(_register_job(spec, home=home))


def unregister_job(name: str, home: Optional[Path] = None) -> bool:
    """Remove a previously registered scheduler drop-in.

    Args:
        name: The job name used at registration.
        home: Override skcapstone root (defaults to ~/.skcapstone).

    Returns:
        ``True`` if a fragment existed and was removed.
    """
    from .scheduler_jobs import unregister_job as _unregister_job

    return _unregister_job(name, home=home)


def coord_create(
    title: str,
    *,
    description: str = "",
    priority: str = "medium",
    tags: Optional[list[str]] = None,
    created_by: str = "",
    acceptance_criteria: Optional[list[str]] = None,
    dependencies: Optional[list[str]] = None,
) -> str:
    """Create a task on the shared coordination board.

    Args:
        title: Task title.
        description: Longer description.
        priority: ``critical | high | medium | low``.
        tags: Optional tag list.
        created_by: Creator name (defaults to the active agent).
        acceptance_criteria: Optional acceptance bullet list.
        dependencies: Optional list of blocking task ids.

    Returns:
        The new task's id.
    """
    from .coordination import Board, Task, TaskPriority

    try:
        prio = TaskPriority(priority)
    except ValueError:
        prio = TaskPriority.MEDIUM

    board = Board(_shared_home())
    task = Task(
        title=title,
        description=description,
        priority=prio,
        tags=tags or [],
        created_by=created_by or _agent_name(),
        acceptance_criteria=acceptance_criteria or [],
        dependencies=dependencies or [],
    )
    board.create_task(task)
    return task.id


def register_service(
    name: str,
    health_url: Optional[str] = None,
    pid_file: Optional[str] = None,
    home: Optional[Path] = None,
) -> str:
    """Advertise a service to skcapstone's discovery registry.

    Writes ``<home>/registry/<name>.json`` describing how to health-check the
    service.  ``service_health.check_all_services()`` unions these registry
    entries with its built-in defaults, so a service that calls this on start
    becomes discoverable without being hardcoded.  Optional — health checks
    still work with an empty registry.

    Args:
        name: Service name (unique key).
        health_url: Optional HTTP URL whose 2xx response means "up".
        pid_file: Optional pid-file path used as a liveness fallback.
        home: Override skcapstone root (defaults to ~/.skcapstone).

    Returns:
        Path to the written registry entry, as a string.
    """
    base = Path(home) if home else _shared_home()
    registry = base / "registry"
    registry.mkdir(parents=True, exist_ok=True)

    entry = {
        "name": name,
        "health_url": health_url,
        "pid_file": pid_file,
        "registered_by": _agent_name(),
        "registered_at": datetime.now(timezone.utc).isoformat(),
    }
    final = registry / f"{name}.json"
    tmp = registry / f".{name}.json.{uuid.uuid4().hex[:8]}.tmp"
    tmp.write_text(json.dumps(entry, indent=2) + "\n", encoding="utf-8")
    tmp.rename(final)
    return str(final)
