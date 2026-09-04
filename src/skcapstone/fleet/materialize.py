"""Render a fleet Service object into systemd user unit text (pure).

The fleet actuation surface (``fleet/actuation.py``) can start and restart a
unit but cannot create one, and CronJob objects do not actuate at all
(``fleet/cron.py``: "skscheduler wiring for CronJob is a later operational
card"). Unit files have therefore come from per-repo imperative install.sh
scripts, which is the reason a documented fleet setup can be live on four
hosts and absent on the fifth.

This module renders. ``fleet/materialize_apply.py`` writes and reloads. The
split exists so the grammar is testable without touching the box.

Specifiers are copied VERBATIM. ``%h`` and friends belong to systemd at start
time, not to us at write time; resolving them here bakes one node's absolute
paths into an object meant to be placed on any node.

Note: ``fleet.services.normalize_service_spec`` is deliberately not used here.
It returns a fixed-shape dict that drops ``exec``, ``type``, ``environment``
and ``description``, which are exactly the fields a unit file is made of.
"""

from __future__ import annotations

import re

_SCHEDULE_NAMED = {"@hourly": "hourly", "@daily": "daily", "@weekly": "weekly"}
_SCHEDULE_INTERVAL = re.compile(r"^[1-9][0-9]*[mh]$")
_HEADER = "# Managed by skcapstone fleet materialize. Edits are overwritten."


class UnitRenderError(ValueError):
    """A Service spec cannot be rendered into a systemd unit."""


def render_service_unit(name: str, spec: dict) -> str:
    """Render the base .service text for a fleet Service object.

    Args:
        name: Fleet object name; becomes ``<name>.service`` on disk.
        spec: Raw Service spec. Reads ``runtime``, ``exec``, ``type``,
            ``environment``, ``description`` and ``restartPolicy``.

    Returns:
        Unit file text with a trailing newline. ``spec.exec`` is copied
        verbatim into ``ExecStart``; systemd specifiers are never expanded.

    Raises:
        UnitRenderError: runtime is not ``systemd-user`` or ``exec`` is
            missing or blank.
    """
    runtime = spec.get("runtime", "systemd-user")
    if runtime != "systemd-user":
        raise UnitRenderError(f"only systemd-user renders to a unit, got {runtime!r}")
    execstart = spec.get("exec")
    if not isinstance(execstart, str) or not execstart.strip():
        raise UnitRenderError(f"spec.exec is required for {name!r}")
    description = spec.get("description") or f"SKWorld fleet service {name}"
    stype = spec.get("type", "simple")
    lines = [
        _HEADER,
        f"# Fleet object: service/{name}",
        "",
        "[Unit]",
        f"Description={description}",
        "",
        "[Service]",
        f"Type={stype}",
    ]
    environment = spec.get("environment") or {}
    for key in sorted(environment):
        lines.append(f"Environment={key}={environment[key]}")
    lines.append(f"ExecStart={execstart}")
    if stype != "oneshot":
        lines.append(f"Restart={spec.get('restartPolicy', 'on-failure')}")
    lines += ["", "[Install]", "WantedBy=default.target", ""]
    return "\n".join(lines)


def render_timer_unit(name: str, schedule: str) -> str:
    """Render the .timer text that drives ``<name>.service``.

    Args:
        name: Fleet object name; the timer targets ``<name>.service``.
        schedule: Either a named schedule (``@hourly``, ``@daily``,
            ``@weekly``) mapped to ``OnCalendar``, or an interval such as
            ``5m`` or ``2h`` mapped to ``OnActiveSec`` plus
            ``OnUnitActiveSec`` (see the comment in the body for why both).

    Returns:
        Timer unit text with a trailing newline. Interval schedules render
        two trigger lines; named schedules render one.

    Raises:
        UnitRenderError: the schedule is neither a known name nor an
            ``<int>m`` / ``<int>h`` interval.
    """
    if schedule in _SCHEDULE_NAMED:
        triggers = [f"OnCalendar={_SCHEDULE_NAMED[schedule]}"]
    elif _SCHEDULE_INTERVAL.match(schedule or ""):
        # OnUnitActiveSec alone is a timer that never fires. It measures from
        # the last time <name>.service was active, and a freshly installed
        # service has never been active, so systemd arms the timer with no
        # elapse point at all: NextElapseUSecMonotonic=infinity while the
        # timer reports enabled, active and Result=success. Reproduced on
        # systemd 255 with a hand-written unit. A first-fire trigger is
        # required to seed the chain; after the first run OnUnitActiveSec
        # keeps the cadence.
        #
        # OnActiveSec is the right seed, not OnBootSec. OnActiveSec counts
        # from the moment the TIMER unit is activated, which apply_unit does
        # via "enable --now" at install time, so the first run lands exactly
        # one interval after install regardless of host uptime. OnBootSec
        # counts from boot: on a node that has been up for days the elapse
        # point is already in the past and the service fires the instant the
        # timer is enabled, and on a fresh boot it adds nothing OnActiveSec
        # does not already cover (the timer activates at boot anyway).
        triggers = [f"OnActiveSec={schedule}", f"OnUnitActiveSec={schedule}"]
    else:
        raise UnitRenderError(f"invalid schedule: {schedule!r}")
    return "\n".join(
        [
            _HEADER,
            f"# Fleet object: service/{name}",
            "",
            "[Unit]",
            f"Description=Run {name}",
            "",
            "[Timer]",
            *triggers,
            "Persistent=true",
            f"Unit={name}.service",
            "",
            "[Install]",
            "WantedBy=timers.target",
            "",
        ]
    )
