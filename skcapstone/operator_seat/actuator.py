"""Atlas's muscle: physically honor operator actions via the tested fleet actuation.

fleet_act records a signed INTENT (operatorActions) on an object's spec; this
module carries that intent out through the Phase 3 actuation layer (the same
tested systemd verbs sknoded uses). It is gated: it refuses when the fleet is
frozen OR when the freeze store has never been human-provisioned
(`store.check_actuation_gate`, AUTONOMY_ARCHITECTURE.md section 3.6), and
physical actuation of real services is a deliberate enablement wired into the
converge pass behind a flag, not automatic. The runner is injectable so tests
never touch real systemd.
"""

from __future__ import annotations

from ..fleet import actuation, store


def action_key(action: dict) -> str:
    """Stable identity of one operator action (for consumed-once tracking)."""
    return f"{action.get('ts')}|{action.get('action')}"


def unconsumed_actions(operator_actions, consumed_keys) -> list[dict]:
    """Pure: the operator actions whose key is not yet in consumed_keys."""
    consumed = set(consumed_keys or ())
    return [a for a in (operator_actions or []) if action_key(a) not in consumed]


def honor(paths, action: dict, unit: str, *, runner=None) -> dict:
    """Physically perform one operator action. Refuses when frozen or unprovisioned.

    Uses the tested actuation layer (never raises on a systemd failure). Returns
    a result dict: {performed, action, unit, key, reason?}. `reason` on refusal
    is `store.REASON_FROZEN` or `store.REASON_UNPROVISIONED`, machine-readable
    per `store.check_actuation_gate`. Unmapped actions are a no-op
    success-of-record (the signed intent stands, no physical muscle yet).
    """
    key = action_key(action)
    gate = store.check_actuation_gate(paths)
    if not gate.allowed:
        return {"performed": False, "reason": gate.reason, "key": key}
    runner = runner or actuation.default_runner
    act = action.get("action")
    if act == "restart_service":
        ok = actuation.systemd_restart(unit, runner=runner)
        return {"performed": bool(ok), "action": act, "unit": unit, "key": key}
    return {"performed": False, "reason": f"no physical muscle for {act!r}", "key": key}
