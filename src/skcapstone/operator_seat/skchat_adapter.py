"""skchat operator adapter: Atlas manages skchat too (the second app adapter).

Conformant to the adapter contract (explain / observe / act). One operator, many
apps: skchat plugs in by exposing the same three verbs the fleet does.

The observe probes are REAL and injectable (tests never touch a live skchat):
the daemon health endpoint, the telegram bridge poll age (the silent-wedge
detector), the real outbox file count (the flood detector that would have caught
the 1.5M-tombstone incident), and the dataplane-auth state. Every probe fails
SAFE (reports healthy) rather than raising a false alarm when skchat is
unreachable.

The act verb (`skchat_act`) maps the two reversible standard actions
(restart-daemon, restart-telegram-bridge) onto the tested `actuator.honor`
systemd path, refusing when the fleet is frozen. purge-outbox stays declared
irreversible/high so `policy.classify_change` forces it to MAJOR by construction:
it escalates and never reaches the act verb.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

from ..fleet import store
from . import actuator

CONDITIONS = ["DaemonReady", "BridgeAlive", "OutboxBounded", "AuthEnforced"]

#: skchat conditions are health-type (they fire when status is False), so they
#: are NOT problem-when-true; the operator brief treats them correctly by default.
_ACTIONS = [
    {
        "name": "restart-daemon",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the skchat receive daemon and verify DaemonReady",
        "kedb_refs": ["ke-skchat-daemon-down"],
    },
    {
        "name": "restart-telegram-bridge",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "restart the wedged telegram bridge (silent-wedge signature)",
        "kedb_refs": ["ke-telegram-wedge"],
    },
    {
        "name": "purge-outbox",
        "standard": False,
        "reversible": False,
        "blast_radius": "delete",
        "runbook": "drop stranded outbox messages (irreversible: escalates as MAJOR)",
        "kedb_refs": ["ke-outbox-flood"],
    },
]

_OUTBOX_LIMIT = 1000
#: The telegram silent-wedge threshold: a bridge whose last poll is older than
#: this while the daemon is up is wedged (the ConnectTimeout hang signature).
_BRIDGE_POLL_MAX_AGE_S = 600
_DAEMON_HEALTH_URL = "http://localhost:9385/health"
_UNIT_RESTART_DAEMON = "skchat-daemon.service"


def _b(value: bool) -> str:
    return "True" if value else "False"


def _agent() -> str:
    """The active agent, for the per-agent telegram bridge unit name."""
    return (
        os.environ.get("SKAGENT")
        or os.environ.get("SKCAPSTONE_AGENT")
        or os.environ.get("SKMEMORY_AGENT")
        or "lumina"
    )


# --- pure probe logic (unit-tested directly) ---------------------------------


def _bridge_alive(poll_age_s: float | None, daemon_up: bool) -> bool:
    """The silent-wedge rule: a bridge is wedged when the daemon is up but the
    last poll is older than the threshold. Unknown poll age fails SAFE (alive)."""
    if poll_age_s is None:
        return True
    return not (daemon_up and poll_age_s > _BRIDGE_POLL_MAX_AGE_S)


def _count_outbox(outbox_dir: str | Path) -> int:
    """Count queued files under the outbox dir. A missing dir is zero (healthy)."""
    p = Path(outbox_dir)
    if not p.is_dir():
        return 0
    return sum(1 for f in p.iterdir() if f.is_file())


# --- real signal readers (each fails safe = healthy) -------------------------


def _outbox_dir() -> str:
    return os.environ.get("SKCOMMS_OUTBOX", str(Path.home() / ".skcomms" / "outbox"))


def _probe_daemon_health() -> tuple[bool, bool | None]:
    """Read the daemon health endpoint. Returns (daemon_ready, auth_enforced).

    Fails SAFE: an unreachable daemon reports (ready, auth-unknown) so a probe
    failure never raises a false 'daemon down' or 'auth off' alarm.
    """
    try:
        import json
        import urllib.request

        url = os.environ.get("SKCHAT_DAEMON_HEALTH", _DAEMON_HEALTH_URL)
        with urllib.request.urlopen(url, timeout=8) as r:
            body = json.loads(r.read())
        ready = bool(body.get("ok", True)) if isinstance(body, dict) else True
        auth = body.get("dataplane_auth") if isinstance(body, dict) else None
        return ready, (bool(auth) if auth is not None else None)
    except Exception:
        return True, None


def _probe_bridge_poll_age() -> float | None:
    """Age in seconds of the telegram bridge's last-poll heartbeat, or None when
    no heartbeat file is found (fails safe: unknown age reads as alive)."""
    try:
        import time

        candidate = os.environ.get("SKCHAT_BRIDGE_HEARTBEAT")
        if not candidate:
            candidate = str(
                Path.home()
                / ".skcapstone"
                / "agents"
                / _agent()
                / "skwhisper"
                / "telegram_poll.ts"
            )
        p = Path(candidate)
        if not p.is_file():
            return None
        return max(0.0, time.time() - p.stat().st_mtime)
    except Exception:
        return None


def _probe_auth_enforced() -> bool | None:
    """The dataplane-auth state from the env, when the daemon did not report it."""
    val = os.environ.get("SKCHAT_DATAPLANE_AUTH")
    if val is None:
        return None
    return val.strip().lower() in {"1", "true", "yes", "on"}


def _default_probe() -> dict:
    """Best-effort skchat health from real signals. Fails SAFE (healthy) when
    skchat is unreachable, so an inability to probe never raises a false alarm."""
    daemon_ready, auth_from_daemon = _probe_daemon_health()
    poll_age = _probe_bridge_poll_age()
    auth = auth_from_daemon
    if auth is None:
        auth = _probe_auth_enforced()
    return {
        "daemon_ready": daemon_ready,
        "bridge_alive": _bridge_alive(poll_age, daemon_ready),
        "outbox_depth": _count_outbox(_outbox_dir()),
        "outbox_limit": _OUTBOX_LIMIT,
        # Unknown auth fails safe to enforced (True): never cry a false 'auth off'.
        "auth_enforced": True if auth is None else bool(auth),
    }


# --- contract verbs ----------------------------------------------------------


def skchat_explain() -> dict:
    """skchat's self-description in the adapter-contract shape."""
    return {
        "kinds": ["daemon", "bridge", "outbox", "dataplane-auth"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skchat_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skchat health snapshot in the adapter-contract shape."""
    st = (probe or _default_probe)()
    depth = int(st.get("outbox_depth", 0))
    limit = int(st.get("outbox_limit", _OUTBOX_LIMIT))
    return {
        "conditions": [
            {
                "type": "DaemonReady",
                "status": _b(bool(st.get("daemon_ready", True))),
                "object": "skchat-daemon",
            },
            {
                "type": "BridgeAlive",
                "status": _b(bool(st.get("bridge_alive", True))),
                "object": "telegram-bridge",
            },
            {"type": "OutboxBounded", "status": _b(depth <= limit), "object": "outbox"},
            {
                "type": "AuthEnforced",
                "status": _b(bool(st.get("auth_enforced", True))),
                "object": "dataplane-auth",
            },
        ]
    }


#: The reversible standard actions -> systemd units for the act verb.
def _unit_for(action: str, agent: str | None = None) -> str | None:
    if action == "restart-daemon":
        return _UNIT_RESTART_DAEMON
    if action == "restart-telegram-bridge":
        return f"skchat-telegram-{agent or _agent()}.service"
    return None


def skchat_act(
    paths, proposal: dict, classification: dict, *, runner=None, agent: str | None = None
) -> dict:
    """Apply a reversible standard skchat action via the tested systemd path.

    Maps restart-daemon / restart-telegram-bridge onto `actuator.honor` (the same
    `systemctl --user restart` verb the fleet uses), refusing when the fleet is
    frozen. Any other action (e.g. purge-outbox) raises: irreversible and major
    actions escalate through the decision store and never reach the act verb.
    """
    if store.is_frozen(paths):
        raise RuntimeError("fleet is frozen: the operator does not actuate")
    action = proposal.get("action")
    unit = _unit_for(action, agent)
    if unit is None:
        raise ValueError(f"no ops-channel mapping for skchat action {action!r}")
    honor_action = {"action": "restart_service", "ts": proposal.get("ts")}
    return actuator.honor(paths, honor_action, unit, runner=runner)


#: A loop-compatible observe (paths, now_iso) -> {conditions}; ignores both.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skchat_observe()


__all__ = [
    "skchat_explain",
    "skchat_observe",
    "skchat_act",
    "observe",
    "CONDITIONS",
]
