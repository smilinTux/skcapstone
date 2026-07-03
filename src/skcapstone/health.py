"""
Startup pillar-health check.

On agent/daemon startup (``AgentRuntime.awaken``) the runtime discovers the
state of every pillar. This module evaluates those pillar statuses and, when
any pillar is *degraded*, emits a single desktop notification so the operator
is alerted immediately.

It reuses the existing notification system (:mod:`skcapstone.notifications`) —
it does NOT invent a new transport. The check is best-effort: a notification
failure must never break agent startup.
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

from .models import AgentManifest, PillarStatus

logger = logging.getLogger("skcapstone.health")

# Pillar statuses that count as "degradation" worth alerting on.
#
# A pillar that is simply MISSING was never installed for this agent, so it is
# not treated as a degradation here (``awaken()`` already logs missing pillars
# separately). DEGRADED / ERROR mean the pillar exists but is unhealthy.
DEGRADED_STATUSES = frozenset({PillarStatus.DEGRADED, PillarStatus.ERROR})

Notifier = Callable[[str, str, str], bool]


def degraded_pillars(manifest: AgentManifest) -> dict[str, PillarStatus]:
    """Return the pillars whose status indicates degradation.

    Args:
        manifest: The awakened agent manifest.

    Returns:
        Mapping of pillar name → status for every degraded pillar. Empty when
        all pillars are healthy (ACTIVE or MISSING).
    """
    return {
        name: status
        for name, status in manifest.pillar_summary.items()
        if status in DEGRADED_STATUSES
    }


def startup_health_check(
    manifest: AgentManifest,
    notifier: Optional[Notifier] = None,
) -> dict[str, PillarStatus]:
    """Evaluate pillar health at startup and notify on degradation.

    Reuses the existing desktop-notification system. If any pillar is degraded
    (DEGRADED or ERROR), a single ``critical`` notification is emitted
    summarizing the affected pillars. When every pillar is healthy, no
    notification is sent.

    Args:
        manifest: The awakened agent manifest to evaluate.
        notifier: Optional notification callable ``(title, body, urgency) ->
            bool``. Defaults to :func:`skcapstone.notifications.notify`.
            Injectable for tests.

    Returns:
        The mapping of degraded pillars (empty when all healthy).
    """
    degraded = degraded_pillars(manifest)
    if not degraded:
        logger.debug(
            "Startup health check: all pillars healthy for '%s'", manifest.name
        )
        return degraded

    detail = ", ".join(
        f"{name} ({status.value})" for name, status in sorted(degraded.items())
    )
    logger.warning(
        "Startup health check: %d degraded pillar(s) for '%s' — %s",
        len(degraded),
        manifest.name,
        detail,
    )

    if notifier is None:
        # Lazy import keeps notifications an optional dependency at import time.
        from .notifications import notify as notifier

    title = f"{manifest.name}: pillar degradation"
    body = f"{len(degraded)} pillar(s) degraded at startup: {detail}"
    try:
        notifier(title, body, "critical")
    except Exception as exc:  # a notification must never break startup
        logger.debug("Startup health notification failed: %s", exc)

    return degraded
