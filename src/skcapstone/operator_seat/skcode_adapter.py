"""skcode operator adapter: Atlas operates skcode-hostd too (spec 4.2).

Conformant to the adapter contract (explain / observe / act). skcode is the
furthest-along subapp: its session plane already ships in skharness (the two-plane
harness contract, skcode-hostd on :9394). This adapter lets Atlas watch and steer
that host.

Conditions (spec 4.2 semantics):
  - HostdReady: the :9394 API answers on this host.
  - SessionsHealthy: no running session is stale past the wedge threshold (the
    runaway/wedge detector).
  - RegistryConsistent: every registry entry reconciles against a live PTY/tmux
    backing; an orphan (registry entry with no backing) flips it False.
  - AuthEnforced: a REAL verifier is active (not the P0 deny-all placeholder and
    not a permissive stub).

Actions:
  - restart-hostd (standard, reversible, low): systemctl --user restart + verify.
  - archive-stale-session (standard, reversible): archive is stop + persist, never
    a destructive kill (per harness.py::archive).
  - kill-runaway-session (NOT standard, reversible false): policy.classify_change
    forces MAJOR by the irreversibility rule, so it escalates with options.
  - pause-dispatch (not standard, reversible, low): the emergency brake on the
    RCE surface once dispatch (P2) exists.

The act verb wraps the tested systemd path (restart-hostd) and the skcode-hostd
operator CLI (archive-stale-session, via an injected runner; the CLI lands with
card R2.14). Every probe fails SAFE (reports healthy) when hostd is unreachable.
"""

from __future__ import annotations

import os
from typing import Any, Callable
from urllib.parse import urlencode

from ..fleet import store
from . import actuator

CONDITIONS = ["HostdReady", "SessionsHealthy", "RegistryConsistent", "AuthEnforced"]

#: A running session with no event for longer than this is wedged/runaway.
_SESSION_STALE_S = 900
_HOSTD_UNIT = "skcode-hostd.service"
_HOSTD_HEALTH_URL = "http://localhost:9394/api/v1/hosts/self"
_HOSTD_URL = "http://localhost:9394"
_TARGET_KINDS = frozenset({"session", "run", "agent", "job"})
_CONTROL_ACTIONS = frozenset(
    {"message", "needs_input_response", "cancel", "pause", "resume", "retry"}
)

_ACTIONS = [
    {
        "name": "restart-hostd",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "systemctl --user restart skcode-hostd and verify HostdReady",
        "kedb_refs": ["ke-hostd-wedge"],
    },
    {
        "name": "archive-stale-session",
        "standard": True,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "archive a wedged session (stop + persist, never a destructive kill)",
        "kedb_refs": ["ke-session-runaway"],
    },
    {
        "name": "kill-runaway-session",
        "standard": False,
        "reversible": False,
        "blast_radius": "low",
        "runbook": "kill a runaway session (irreversible: escalates as MAJOR with options)",
        "kedb_refs": ["ke-session-runaway"],
    },
    {
        "name": "pause-dispatch",
        "standard": False,
        "reversible": True,
        "blast_radius": "low",
        "runbook": "flip the dispatch-enable flag off (emergency brake on the RCE surface)",
        "kedb_refs": [],
    },
]


def _b(value: bool) -> str:
    return "True" if value else "False"


# --- pure probe logic (unit-tested directly) ---------------------------------


def _sessions_healthy(sessions: list[dict], stale_s: int = _SESSION_STALE_S) -> bool:
    """False when any running session's last event is older than the threshold.

    A session dict carries ``state`` and ``last_event_age_s``. Non-running
    sessions and sessions with unknown age never fire (fail safe)."""
    for s in sessions or ():
        if s.get("state") != "running":
            continue
        age = s.get("last_event_age_s")
        if age is not None and age > stale_s:
            return False
    return True


def _registry_consistent(registry_ids, live_ids) -> bool:
    """False when a registry entry has no live backing (an orphan). Consistent
    means every registered session id is backed by a live PTY/tmux id."""
    return set(registry_ids or ()) <= set(live_ids or ())


# --- real signal readers (each fails safe = healthy) -------------------------


def _probe_hostd() -> dict:
    """Best-effort skcode-hostd read. Fails SAFE (all healthy) when unreachable."""
    try:
        import json
        import urllib.request

        url = os.environ.get("SKCODE_HOSTD_HEALTH", _HOSTD_HEALTH_URL)
        with urllib.request.urlopen(url, timeout=8) as r:
            body = json.loads(r.read())
        sessions = body.get("sessions", []) if isinstance(body, dict) else []
        registry_ids = [s.get("id") for s in sessions]
        live_ids = [s.get("id") for s in sessions if s.get("backing_alive", True)]
        auth = body.get("auth_enforced") if isinstance(body, dict) else None
        return {
            "hostd_ready": True,
            "sessions_healthy": _sessions_healthy(sessions),
            "registry_consistent": _registry_consistent(registry_ids, live_ids),
            "auth_enforced": True if auth is None else bool(auth),
        }
    except Exception as exc:
        return {"_probe_error": type(exc).__name__}


def _base_url() -> str:
    return os.environ.get("SKCODE_HOSTD_URL", _HOSTD_URL).rstrip("/")


def _request_json(
    request,
    *,
    opener: Callable[..., object] | None = None,
    timeout: float = 8,
) -> dict[str, Any]:
    import json
    import urllib.request

    with (opener or urllib.request.urlopen)(request, timeout=timeout) as response:
        value = json.loads(response.read())
    if not isinstance(value, dict):
        raise ValueError("skcode-hostd returned a non-object response")
    return value


def _bearer(token: str) -> dict[str, str]:
    if not isinstance(token, str) or not token.strip():
        raise ValueError("a skcode audience token is required")
    return {"Authorization": f"Bearer {token.strip()}", "Accept": "application/json"}


def skcode_activity(
    *,
    token: str,
    after: int = 0,
    limit: int = 200,
    filters: dict[str, str] | None = None,
    opener: Callable[..., object] | None = None,
) -> dict[str, Any]:
    """Replay Atlas activity from a durable cursor; this method never actuates."""

    import urllib.request

    if after < 0 or not 1 <= limit <= 500:
        raise ValueError("invalid activity cursor or limit")
    allowed = {
        "session_id",
        "run_id",
        "agent_id",
        "job_id",
        "card_id",
        "contract_id",
        "lease_id",
        "role",
        "kind",
    }
    supplied = filters or {}
    if set(supplied) - allowed:
        raise ValueError("unknown activity filter")
    query = urlencode({"after": after, "limit": limit, **supplied})
    request = urllib.request.Request(
        f"{_base_url()}/api/v1/activity?{query}",
        headers=_bearer(token),
        method="GET",
    )
    return _request_json(request, opener=opener)


def skcode_control(
    *,
    token: str,
    idempotency_key: str,
    target_kind: str,
    target_id: str,
    action: str,
    payload: dict[str, Any] | None = None,
    expected_state: str = "",
    ttl_s: int = 300,
    opener: Callable[..., object] | None = None,
) -> dict[str, Any]:
    """Submit one explicit Atlas command; callers must inspect its receipt."""

    import json
    import urllib.request

    if target_kind not in _TARGET_KINDS or action not in _CONTROL_ACTIONS:
        raise ValueError("unknown skcode control target or action")
    if not idempotency_key or not target_id:
        raise ValueError("control idempotency_key and target_id are required")
    body = json.dumps(
        {
            "idempotency_key": idempotency_key,
            "target_kind": target_kind,
            "target_id": target_id,
            "action": action,
            "expected_state": expected_state,
            "payload": payload or {},
            "ttl_s": ttl_s,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    headers = _bearer(token)
    headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{_base_url()}/api/v1/control", headers=headers, data=body, method="POST"
    )
    return _request_json(request, opener=opener)


def skcode_control_receipt(
    command_id: str,
    *,
    token: str,
    opener: Callable[..., object] | None = None,
) -> dict[str, Any]:
    """Read the latest receipt; queued is intentionally not treated as applied."""

    import urllib.parse
    import urllib.request

    if not command_id:
        raise ValueError("command_id is required")
    encoded = urllib.parse.quote(command_id, safe="")
    request = urllib.request.Request(
        f"{_base_url()}/api/v1/control/{encoded}",
        headers=_bearer(token),
        method="GET",
    )
    return _request_json(request, opener=opener)


def skcode_live_contract() -> dict[str, Any]:
    """Return the transport contract used by Atlas/UI websocket clients."""

    base = _base_url()
    ws = ("wss:" if base.startswith("https:") else "ws:") + base.split(":", 1)[1]
    return {
        "activity_replay": f"{base}/api/v1/activity",
        "activity_stream": f"{ws}/api/v1/activity/stream",
        "control_submit": f"{base}/api/v1/control",
        "control_receipt": f"{base}/api/v1/control/{{command_id}}",
        "monitor_scope": "skcode.stream",
        "message_scope": "skcode.inject",
        "action_scope": "skcode.dispatch",
        "lineage_fields": (
            "card_id",
            "card_hash",
            "trajectory_id",
            "team_id",
            "parent_agent_id",
            "contract_id",
            "contract_hash",
            "plan_hash",
            "lease_id",
            "attempt_id",
            "base_commit",
            "evidence_id",
        ),
    }


# --- contract verbs ----------------------------------------------------------


def skcode_explain() -> dict:
    """skcode-hostd's self-description in the adapter-contract shape."""
    return {
        "kinds": ["hostd", "session", "registry", "dispatch", "activity", "control"],
        "conditions": list(CONDITIONS),
        "actions": [dict(a) for a in _ACTIONS],
    }


def skcode_observe(probe: Callable[[], dict] | None = None) -> dict:
    """Read-only skcode-hostd health snapshot in the adapter-contract shape."""
    st = (probe or _probe_hostd)()
    unknown = bool(st.get("_probe_error"))
    return {
        "conditions": [
            {
                "type": "HostdReady",
                "status": "Unknown" if unknown else _b(bool(st.get("hostd_ready", True))),
                "object": "skcode-hostd",
            },
            {
                "type": "SessionsHealthy",
                "status": "Unknown" if unknown else _b(bool(st.get("sessions_healthy", True))),
                "object": "sessions",
            },
            {
                "type": "RegistryConsistent",
                "status": "Unknown" if unknown else _b(bool(st.get("registry_consistent", True))),
                "object": "registry",
            },
            {
                "type": "AuthEnforced",
                "status": "Unknown" if unknown else _b(bool(st.get("auth_enforced", True))),
                "object": "verifier",
            },
        ]
    }


def skcode_act(
    paths,
    proposal: dict,
    classification: dict,
    *,
    runner=None,
    cli_runner: Callable[[list[str]], object] | None = None,
) -> dict:
    """Apply a reversible standard skcode action, refusing when frozen.

    restart-hostd maps onto the tested `actuator.honor` systemd path.
    archive-stale-session invokes the skcode-hostd operator CLI through an
    injected runner (the CLI lands with card R2.14). kill-runaway-session and
    pause-dispatch are not act-verb actions here: kill escalates as MAJOR by
    construction, and pause-dispatch waits on the dispatch (P2) surface.
    """
    if store.is_frozen(paths):
        raise RuntimeError("fleet is frozen: the operator does not actuate")
    action = proposal.get("action")
    if action == "restart-hostd":
        honor_action = {"action": "restart_service", "ts": proposal.get("ts")}
        return actuator.honor(paths, honor_action, _HOSTD_UNIT, runner=runner)
    if action == "archive-stale-session":
        session = proposal.get("object") or proposal.get("session")
        if cli_runner is None:
            raise ValueError(
                "archive-stale-session requires the skcode-hostd operator CLI (card R2.14)"
            )
        cp = cli_runner(
            ["skcode-hostd", "operator", "act", "archive-stale-session", "--session", str(session)]
        )
        return {
            "performed": getattr(cp, "returncode", 1) == 0,
            "action": action,
            "session": session,
        }
    raise ValueError(f"no ops-channel mapping for skcode action {action!r}")


#: A loop-compatible observe (paths, now_iso) -> {conditions}; ignores both.
def observe(paths=None, now_iso: str | None = None) -> dict:
    return skcode_observe()


__all__ = [
    "skcode_explain",
    "skcode_observe",
    "skcode_act",
    "observe",
    "CONDITIONS",
    "skcode_activity",
    "skcode_control",
    "skcode_control_receipt",
    "skcode_live_contract",
]
