"""Fail-closed deterministic cross-provider pairing."""

from __future__ import annotations

import json
import re

HEALTH_SCHEMA = "skgateway.route-health/v1"
PAIR_SCHEMA = "skfleet.provider-pair/v1"
_WORK_CLASSES = frozenset({"s", "m", "l"})
_HEALTH_KEYS = {"schema", "revision", "routes"}
_ROUTE_KEYS = {
    "logical_route",
    "provider_family",
    "work_class",
    "focused",
    "qualified",
    "healthy",
}
_POLICY_KEYS = {"rotation_counter", "automatic_families", "explicit_parent_route"}
_PAIR_KEYS = {"schema", "state", "snapshot_revision", "producer", "reviewer"}
_MEMBER_KEYS = {"provider_family", "logical_route"}
_FAMILY = re.compile(r"[a-z][a-z0-9-]{0,62}[a-z0-9]|[a-z]")
_ROUTE = re.compile(r"sk-([a-z][a-z0-9-]{0,62}[a-z0-9]|[a-z])-(s|m|l)")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _copy(value: object) -> dict:
    """Copy bounded finite JSON without retaining caller aliases."""
    if not isinstance(value, dict):
        raise ValueError("provider pairing input must be an object")
    encoded = json.dumps(value, allow_nan=False)
    if len(encoded.encode()) > 131072:
        raise ValueError("provider pairing input exceeds bounded size")
    return json.loads(encoded)


def _family(value: object) -> str:
    """Require one canonical lowercase provider-family identifier."""
    if not isinstance(value, str) or not _FAMILY.fullmatch(value):
        raise ValueError("invalid provider family")
    return value


def _route(value: object) -> tuple[str, str, str]:
    """Require and split one canonical focused route."""
    if not isinstance(value, str):
        raise ValueError("invalid focused logical route")
    match = _ROUTE.fullmatch(value)
    if match is None:
        raise ValueError("invalid focused logical route")
    family, work_class = match.groups()
    return value, family, work_class


def _member(value: object, name: str) -> dict | None:
    """Validate one optional exact provider-pair member."""
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _MEMBER_KEYS:
        raise ValueError(f"invalid {name} provider fields")
    family = _family(value["provider_family"])
    route, route_family, _ = _route(value["logical_route"])
    if family != route_family:
        raise ValueError(f"{name} provider family does not match route")
    return {"provider_family": family, "logical_route": route}


def validate_provider_pair(value: object) -> dict:
    """Validate the finite persisted provider-pair result contract."""
    pair = _copy(value)
    if set(pair) != _PAIR_KEYS or pair.get("schema") != PAIR_SCHEMA:
        raise ValueError("invalid provider pair fields or schema")
    if pair["state"] not in {"paired", "independent_review_unavailable"}:
        raise ValueError("invalid provider pair state")
    if not isinstance(pair["snapshot_revision"], str) or not _SHA256.fullmatch(
        pair["snapshot_revision"]
    ):
        raise ValueError("invalid provider pair snapshot revision")
    producer = _member(pair["producer"], "producer")
    reviewer = _member(pair["reviewer"], "reviewer")
    if pair["state"] == "paired":
        if producer is None or reviewer is None:
            raise ValueError("paired provider result requires producer and reviewer")
        if producer["provider_family"] == reviewer["provider_family"]:
            raise ValueError("independent review requires different provider families")
        if (
            producer["logical_route"].rsplit("-", 1)[1]
            != reviewer["logical_route"].rsplit("-", 1)[1]
        ):
            raise ValueError("producer and reviewer work classes must match")
    elif reviewer is not None:
        raise ValueError("unavailable independent review cannot name a reviewer")
    pair["producer"], pair["reviewer"] = producer, reviewer
    return pair


def _health(value: object) -> dict:
    """Validate the exact sanitized route-health snapshot."""
    snapshot = _copy(value)
    if set(snapshot) != _HEALTH_KEYS or snapshot.get("schema") != HEALTH_SCHEMA:
        raise ValueError("invalid route-health fields or schema")
    if not isinstance(snapshot["revision"], str) or not _SHA256.fullmatch(snapshot["revision"]):
        raise ValueError("invalid route-health revision")
    routes = snapshot["routes"]
    if not isinstance(routes, list) or len(routes) > 64:
        raise ValueError("invalid bounded route-health routes")
    seen_routes, seen_families = set(), set()
    for row in routes:
        if not isinstance(row, dict) or set(row) != _ROUTE_KEYS:
            raise ValueError("invalid route-health route fields")
        route, route_family, route_class = _route(row["logical_route"])
        family = _family(row["provider_family"])
        if row["work_class"] not in _WORK_CLASSES:
            raise ValueError("invalid route work class")
        if family != route_family or row["work_class"] != route_class:
            raise ValueError("route identity fields do not match")
        if any(type(row[key]) is not bool for key in ("focused", "qualified", "healthy")):
            raise ValueError("route health flags must be booleans")
        if route in seen_routes:
            raise ValueError("duplicate logical route")
        if family in seen_families:
            raise ValueError("duplicate provider family")
        seen_routes.add(route)
        seen_families.add(family)
    return snapshot


def _policy(value: object) -> dict:
    """Validate deterministic rotation and explicit-parent policy."""
    policy = _copy(value)
    if set(policy) != _POLICY_KEYS:
        raise ValueError("invalid provider pairing policy fields")
    counter = policy["rotation_counter"]
    if type(counter) is not int or not 0 <= counter <= 2**53 - 1:
        raise ValueError("invalid provider rotation counter")
    families = policy["automatic_families"]
    if not isinstance(families, list) or len(families) > 32:
        raise ValueError("invalid automatic provider families")
    families = [_family(item) for item in families]
    if len(set(families)) != len(families):
        raise ValueError("duplicate automatic provider family")
    explicit = policy["explicit_parent_route"]
    if explicit is not None:
        _route(explicit)
    policy["automatic_families"] = families
    return policy


def _pair_member(row: dict) -> dict:
    """Project a health row into the sealed pair member shape."""
    return {"provider_family": row["provider_family"], "logical_route": row["logical_route"]}


def select_provider_pair(work_class: str, health_snapshot: object, policy: object) -> dict:
    """Choose a deterministic producer and different-family reviewer."""
    if not isinstance(work_class, str) or work_class not in _WORK_CLASSES:
        raise ValueError("unsupported provider-pair work class")
    snapshot = _health(health_snapshot)
    policy = _policy(policy)
    rows = snapshot["routes"]
    eligible = sorted(
        (
            row
            for row in rows
            if row["work_class"] == work_class
            and row["focused"]
            and row["qualified"]
            and row["healthy"]
        ),
        key=lambda row: (row["provider_family"], row["logical_route"]),
    )
    automatic = [
        row
        for row in eligible
        if row["provider_family"] != "codex"
        and row["provider_family"] in policy["automatic_families"]
    ]
    explicit = policy["explicit_parent_route"]
    if explicit is None:
        producer = automatic[policy["rotation_counter"] % len(automatic)] if automatic else None
    else:
        producer = next((row for row in eligible if row["logical_route"] == explicit), None)
        if producer is None:
            raise ValueError("explicit parent route is not healthy, qualified and focused")
        if producer["work_class"] != work_class:
            raise ValueError("explicit parent route has the wrong work class")
    reviewer_rows = [
        row
        for row in automatic
        if producer is not None and row["provider_family"] != producer["provider_family"]
    ]
    reviewer = None
    if reviewer_rows:
        after = [
            row for row in reviewer_rows if row["provider_family"] > producer["provider_family"]
        ]
        reviewer = (after or reviewer_rows)[0]
    result = {
        "schema": PAIR_SCHEMA,
        "state": (
            "paired"
            if producer is not None and reviewer is not None
            else "independent_review_unavailable"
        ),
        "snapshot_revision": snapshot["revision"],
        "producer": _pair_member(producer) if producer is not None else None,
        "reviewer": _pair_member(reviewer) if reviewer is not None else None,
    }
    return validate_provider_pair(result)
