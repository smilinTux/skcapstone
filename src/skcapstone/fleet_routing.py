"""Logical card contracts and deployment-owned fleet lane bindings."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping

BUCKETS = ("sk-s", "sk-m", "sk-l", "sk-xl")
_SIZE = re.compile(r"\[(S|M|L|XL)\]", re.IGNORECASE)
_BACKEND_LANES = {"glm", "kimi", "codex", "escalate"}


def logical_contract(labels: Iterable[str], title: str = "") -> tuple[tuple[str, ...], str]:
    """Resolve one logical size, rejecting ambiguous contracts."""
    normalized = {str(label).strip().lower() for label in labels}
    requested = normalized.intersection(BUCKETS)
    requested.update("sk-" + size.lower() for size in _SIZE.findall(title))
    if len(requested) > 1:
        return (), "conflicting-logical-buckets"
    bucket = next(iter(requested), "sk-m")
    return (bucket,), "logical:" + bucket


def deployment_profile(raw: str | None) -> dict[str, tuple[str, ...]]:
    """Parse a versioned binding, failing closed for any malformed profile.

    Names identify existing worker transports, not the card's model contract.
    The deployment supplies each transport's models and capacity domains. For
    example, an Astra deployment can use the existing escalate transport.
    Qwen is deliberately excluded, even from explicitly configured bindings.
    """
    if raw is None:
        return {bucket: ("glm", "kimi", "codex") for bucket in BUCKETS}
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"version", "buckets"}:
            return {}
        if type(value["version"]) is not int or value["version"] != 1:
            return {}
        bindings = value["buckets"]
        if not isinstance(bindings, dict) or set(bindings) != set(BUCKETS):
            return {}
        for lanes in bindings.values():
            if not isinstance(lanes, list) or any(
                not isinstance(lane, str) or lane not in _BACKEND_LANES for lane in lanes
            ):
                return {}
            if len(lanes) != len(set(lanes)):
                return {}
        return {bucket: tuple(bindings[bucket]) for bucket in BUCKETS}
    except (TypeError, ValueError):
        return {}


def resolve_lanes(
    contract: tuple[str, ...], profile: Mapping[str, tuple[str, ...]]
) -> tuple[str, ...]:
    """Map logical contracts to deployment lanes; retain explicit exceptions."""
    if len(contract) == 1 and contract[0] in BUCKETS:
        return profile.get(contract[0], ())
    return contract
