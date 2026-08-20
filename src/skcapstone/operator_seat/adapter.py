"""Adapter contract conformance validator for the operator seat (Seat O3a).

Pure validation of the explain/observe payload shape that every app adapter
must satisfy to plug into the one operator. No filesystem or network access,
no CLI wiring; callers own reading the adapter output and any wiring.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

BLAST_RADII = frozenset({"low", "medium", "delete", "drain_always_on", "fleet_restart"})

OBSERVE_STATUSES = frozenset({"True", "False", "Unknown"})
OBSERVATION_SCHEMA = "skoperator.observation/v1"
POLARITIES = frozenset({"problem_when_true", "problem_when_false"})

_ACTION_REQUIRED_KEYS = (
    "name",
    "standard",
    "reversible",
    "blast_radius",
    "runbook",
    "kedb_refs",
)


def action_contract_id(app: str, action: str) -> str:
    """Return the canonical globally unambiguous action contract identifier."""
    if not app or not action or "." in app:
        raise ValueError("action contract requires an unqualified app and non-empty action")
    return f"{app}.{action}"


def validate_explain(payload: dict[str, Any]) -> list[str]:
    """Validate an adapter's explain output against the O3 contract.

    Args:
        payload: The adapter's explain output. Expected keys: ``kinds``
            (list of str), ``conditions`` (list of str), and ``actions``
            (list of dicts, see :func:`_validate_action`).

    Returns:
        A list of human-readable contract violations. Empty means the
        payload is conformant.
    """
    violations: list[str] = []

    kinds = payload.get("kinds")
    if not isinstance(kinds, list) or not all(isinstance(k, str) for k in kinds):
        violations.append("kinds must be a list of str")

    conditions = payload.get("conditions")
    if not isinstance(conditions, list) or not all(isinstance(c, str) for c in conditions):
        violations.append("conditions must be a list of str")

    actions = payload.get("actions")
    if not isinstance(actions, list):
        violations.append("actions must be a list of dicts")
    else:
        for index, action in enumerate(actions):
            violations.extend(_validate_action(index, action))

    return violations


def _validate_action(index: int, action: Any) -> list[str]:
    """Validate a single entry of an explain payload's ``actions`` list.

    Args:
        index: Position of the action within the ``actions`` list, used to
            label any violations.
        action: The candidate action dict.

    Returns:
        A list of human-readable contract violations for this action.
    """
    if not isinstance(action, dict):
        return [f"actions[{index}] must be a dict"]

    violations: list[str] = []
    for key in _ACTION_REQUIRED_KEYS:
        if key not in action:
            violations.append(f"actions[{index}] missing key '{key}'")

    if "name" in action and not isinstance(action["name"], str):
        violations.append(f"actions[{index}].name must be a str")

    if "standard" in action and not isinstance(action["standard"], bool):
        violations.append(f"actions[{index}].standard must be a bool")

    if "reversible" in action and not isinstance(action["reversible"], bool):
        violations.append(f"actions[{index}].reversible must be a bool")

    if "blast_radius" in action:
        blast_radius = action["blast_radius"]
        if not isinstance(blast_radius, str) or blast_radius not in BLAST_RADII:
            violations.append(
                f"actions[{index}].blast_radius must be one of {sorted(BLAST_RADII)}"
            )

    if "runbook" in action and not isinstance(action["runbook"], str):
        violations.append(f"actions[{index}].runbook must be a str")

    if "kedb_refs" in action and not isinstance(action["kedb_refs"], list):
        violations.append(f"actions[{index}].kedb_refs must be a list")

    return violations


def validate_observe(payload: dict[str, Any]) -> list[str]:
    """Validate an adapter's observe output against the O3 contract.

    Args:
        payload: The adapter's observe output. Expected key: ``conditions``
            (list of dicts with ``type`` and ``status`` keys, status one of
            True, False, Unknown).

    Returns:
        A list of human-readable contract violations. Empty means the
        payload is conformant.
    """
    conditions = payload.get("conditions")
    if not isinstance(conditions, list):
        return ["conditions must be a list of dicts"]

    violations: list[str] = []
    for index, condition in enumerate(conditions):
        violations.extend(_validate_observed_condition(index, condition))

    return violations


def condition_schema(
    names: list[str] | tuple[str, ...],
    *,
    problem_when_true: set[str] | frozenset[str] = frozenset(),
    ttl_seconds: int = 300,
) -> dict[str, dict[str, Any]]:
    """Build the authoritative condition schema owned by an adapter.

    Polarity belongs to this schema, never to a consumer-maintained lookup.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    return {
        name: {
            "polarity": (
                "problem_when_true" if name in problem_when_true else "problem_when_false"
            ),
            "ttl_seconds": ttl_seconds,
        }
        for name in names
    }


def normalize_observe(
    app: str,
    payload: Mapping[str, Any] | None,
    schema: Mapping[str, Mapping[str, Any]],
    *,
    observed_at: str | None = None,
    provenance: str | None = None,
    scope: str = "local",
) -> dict[str, Any]:
    """Return a versioned observation envelope, failing missing evidence Unknown.

    Every declared condition is emitted exactly once. Malformed, absent, or
    undeclared evidence cannot silently become healthy.
    """
    now = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    raw = payload if isinstance(payload, Mapping) else {}
    items = raw.get("conditions")
    by_type = {
        item.get("type"): item
        for item in items
        if isinstance(items, list) and isinstance(item, Mapping) and item.get("type") in schema
    } if isinstance(items, list) else {}
    conditions: list[dict[str, Any]] = []
    for name, definition in schema.items():
        polarity = definition.get("polarity")
        ttl = definition.get("ttl_seconds")
        if polarity not in POLARITIES:
            raise ValueError(f"invalid polarity for {name!r}: {polarity!r}")
        if not isinstance(ttl, int) or ttl <= 0:
            raise ValueError(f"invalid ttl_seconds for {name!r}: {ttl!r}")
        candidate = by_type.get(name)
        valid = isinstance(candidate, Mapping) and candidate.get("status") in OBSERVE_STATUSES
        item = dict(candidate) if valid else {
            "type": name,
            "status": "Unknown",
            "reason": "ProbeUnavailable",
            "message": "adapter did not provide valid evidence",
        }
        item.update(
            {
                "type": name,
                "app": app,
                "observed_at": item.get("observed_at", now),
                "ttl_seconds": ttl,
                "provenance": item.get("provenance", provenance or f"builtin:{app}"),
                "scope": item.get("scope", scope),
                "polarity": polarity,
            }
        )
        conditions.append(item)
    return {"schema": OBSERVATION_SCHEMA, "app": app, "conditions": conditions}


def _validate_observed_condition(index: int, condition: Any) -> list[str]:
    """Validate a single entry of an observe payload's ``conditions`` list.

    Args:
        index: Position of the condition within the ``conditions`` list,
            used to label any violations.
        condition: The candidate condition dict.

    Returns:
        A list of human-readable contract violations for this condition.
    """
    if not isinstance(condition, dict):
        return [f"conditions[{index}] must be a dict"]

    violations: list[str] = []

    if "type" not in condition:
        violations.append(f"conditions[{index}] missing key 'type'")
    elif not isinstance(condition["type"], str):
        violations.append(f"conditions[{index}].type must be a str")

    if "status" not in condition:
        violations.append(f"conditions[{index}] missing key 'status'")
    elif condition["status"] not in OBSERVE_STATUSES:
        violations.append(f"conditions[{index}].status must be one of {sorted(OBSERVE_STATUSES)}")

    return violations
