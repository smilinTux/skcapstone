"""Shared production routing policy, separate from activation authorization."""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from urllib.parse import urlsplit

SCHEMA = "skfleet.production/v1"
LANES = frozenset({"codex", "glm", "deepseek", "qwen"})
LEGACY_CEILING_VARIABLES = frozenset(
    {
        "SKFLEET_TARGET",
        "SKFLEET_GLM_TARGET",
        "SKFLEET_QWEN_TARGET",
        "SKFLEET_KIMI_TARGET",
        "SKFLEET_DEEPSEEK_TARGET",
    }
)
_HOST = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}\Z")
_MAX_BYTES = 1024 * 1024
_MAX_LANE_RUNTIME_SECONDS = 5400


def _unique_object(pairs: list[tuple[str, object]]) -> dict:
    """Reject ambiguous duplicate JSON keys at every nesting level."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("production policy contains duplicate keys")
        result[key] = value
    return result


def load_production_policy(path: Path, *, host: str) -> dict:
    """Read current policy bytes and validate transport, routes and worker quotas.

    This grants no card or action authorization. Callers retain activation and
    claim checks; SKGateway owns provider concurrency and request backpressure.
    """
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("production policy must be a regular file")
        raw = stream.read(_MAX_BYTES + 1)
    if len(raw) > _MAX_BYTES:
        raise ValueError("production policy exceeds read bound")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("production policy is not valid JSON") from exc
    required = {"schema", "authority_host", "capacity_authority", "gateway_url", "lanes"}
    if (
        not isinstance(value, dict)
        or not required <= value.keys()
        or value.keys()
        - required
        - {
            "node_quotas",
            "cycle_budget_seconds",
            "scan_budget",
            "worker_destinations",
            "remote_review",
            "node_admission",
            "lane_runtime_max_seconds",
        }
    ):
        raise ValueError("production policy fields are invalid")
    if value["schema"] != SCHEMA or value["capacity_authority"] != "skgateway":
        raise ValueError("production policy requires SKGateway capacity authority")
    authority = value["authority_host"]
    if (
        not isinstance(authority, str)
        or not _HOST.fullmatch(authority)
        or authority != host.strip().lower()
    ):
        raise ValueError("production policy authority host does not match")
    endpoint = value["gateway_url"]
    try:
        url = urlsplit(endpoint) if isinstance(endpoint, str) else None
        valid_url = (
            url is not None
            and not any(c.isspace() or ord(c) < 32 for c in endpoint)
            and url.scheme in {"http", "https"}
            and url.hostname
            and url.username is None
            and url.password is None
            and not url.query
            and not url.fragment
            and url.path in {"", "/"}
            and (url.port is None or url.port > 0)
        )
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("production policy gateway URL is invalid")
    lanes = value["lanes"]
    if not isinstance(lanes, dict) or set(lanes) - {"gateway", "cursor"} != {*LANES, "kimi"}:
        raise ValueError("production policy lanes are invalid")
    for lane in LANES | ({"gateway"} & set(lanes)):
        row = lanes[lane]
        if (
            not isinstance(row, dict)
            or set(row) != {"enabled", "provider"}
            or type(row["enabled"]) is not bool
            or row["provider"] != "skgateway"
        ):
            raise ValueError("production policy route is invalid")
    if lanes["kimi"] != {"enabled": False} or lanes["kimi"].get("enabled") is not False:
        raise ValueError("production policy must disable Kimi")
    if "cursor" in lanes:
        row = lanes["cursor"]
        if (
            not isinstance(row, dict)
            or type(row.get("enabled")) is not bool
            or row != {"enabled": row.get("enabled"), "provider": "cursor", "host": "chiap08"}
        ):
            raise ValueError("Cursor policy requires the fixed chiap08 plan lane")
    quotas = value.get("node_quotas", {})
    if not isinstance(quotas, dict):
        raise ValueError("production policy node quotas are invalid")
    for node, limits in quotas.items():
        if (
            not _HOST.fullmatch(node)
            or not isinstance(limits, dict)
            or set(limits)
            != {"cpu_quota_percent", "memory_max_bytes", "tasks_max", "runtime_max_seconds"}
            or any(type(number) is not int or number <= 0 for number in limits.values())
        ):
            raise ValueError("production policy worker resources are invalid")
    lane_runtime = value.get("lane_runtime_max_seconds", {})
    if not isinstance(lane_runtime, dict) or any(
        lane not in LANES | {"cursor"}
        or type(seconds) is not int
        or seconds <= 0
        or seconds > _MAX_LANE_RUNTIME_SECONDS
        for lane, seconds in lane_runtime.items()
    ):
        raise ValueError("production policy lane runtime limits are invalid")
    for key in ("cycle_budget_seconds", "scan_budget"):
        if key in value and (type(value[key]) is not int or value[key] <= 0):
            raise ValueError("production policy cycle budget is invalid")
    if value.get("cycle_budget_seconds", 250) > 250:
        raise ValueError("production policy cycle budget exceeds dispatcher timeout margin")
    validate_execution_policy(value)
    return value


def validate_execution_policy(value: dict) -> None:
    """Validate additive placement and bounded remote-review rollout fields."""
    quotas = value.get("node_quotas", {})
    destinations = value.get("worker_destinations")
    if destinations is not None and (
        not isinstance(destinations, list)
        or not destinations
        or any(not isinstance(h, str) or not _HOST.fullmatch(h) for h in destinations)
        or len(set(destinations)) != len(destinations)
        or any(h not in quotas for h in destinations)
    ):
        raise ValueError("worker destinations require unique qualified hosts")
    admission = value.get("node_admission", {})
    if not isinstance(admission, dict):
        raise ValueError("node admission must be an object")
    for host, limits in admission.items():
        if host not in (destinations or quotas) or not isinstance(limits, dict):
            raise ValueError("node admission requires an authorized host")
        if set(limits) - {
            "max_concurrent_workers",
            "memory_floor_bytes",
            "protected_service_bindings",
        }:
            raise ValueError("node admission fields are invalid")
        for key in ("max_concurrent_workers", "memory_floor_bytes"):
            if key not in limits or type(limits[key]) is not int or limits[key] <= 0:
                raise ValueError("node admission limits must be positive integers")
        protected = limits.get("protected_service_bindings", {})
        if not isinstance(protected, dict) or any(
            not re.fullmatch(r"skfleet-worker-[a-zA-Z0-9_.-]+\.service", unit)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for unit, digest in protected.items()
        ):
            raise ValueError("protected services require exact definition digests")
    review = value.get("remote_review")
    if review is None:
        return
    if (
        not isinstance(review, dict)
        or set(review) != {"enabled", "destinations", "card_ids"}
        or type(review["enabled"]) is not bool
    ):
        raise ValueError("remote review rollout fields are invalid")
    hosts, cards = review["destinations"], review["card_ids"]
    if (
        not isinstance(hosts, list)
        or not hosts
        or any(not isinstance(h, str) for h in hosts)
        or len(set(hosts)) != len(hosts)
        or destinations is None
        or any(h not in destinations for h in hosts)
    ):
        raise ValueError("remote review destinations must be explicitly authorized")
    if cards is not None and (
        not isinstance(cards, list)
        or not cards
        or any(not isinstance(c, str) or not re.fullmatch(r"[0-9a-f]{8}", c) for c in cards)
        or len(set(cards)) != len(cards)
    ):
        raise ValueError("remote review card restriction is invalid")
    if (
        review["enabled"]
        and not {"worker_destinations", "node_admission", "node_quotas"} <= value.keys()
    ):
        raise ValueError("enabled remote review requires complete execution policy")


def require_destination(value: dict, host: str) -> dict:
    """Enforce explicit placement even when a stale quota still exists."""
    quotas = value.get("node_quotas", {})
    if host not in quotas or host not in value.get("worker_destinations", quotas):
        raise ValueError("worker destination is not authorized and qualified")
    return quotas[host]


# Fields that change how an already sealed offer executes. Lane toggles are
# deliberately absent: only the offer's own lane matters (checked separately).
_EXECUTION_FIELDS = (
    "schema",
    "authority_host",
    "capacity_authority",
    "gateway_url",
    "remote_review",
    "node_admission",
    "worker_destinations",
    "cycle_budget_seconds",
    "scan_budget",
    "lane_runtime_max_seconds",
)


def execution_projection(policy: dict, host: str | None) -> dict:
    """Project a production policy onto what governs execution on one host."""
    value = {key: policy.get(key) for key in _EXECUTION_FIELDS}
    quotas = policy.get("node_quotas")
    value["resources"] = quotas.get(host) if isinstance(quotas, dict) and host else None
    return value


def _lane_for_family(lanes, bound_family):
    """Map an offer's bound family (a lane name or a provider family such as
    ``zai`` for the ``glm`` lane) to exactly one policy lane name."""
    if not isinstance(lanes, dict) or not isinstance(bound_family, str):
        return None
    if bound_family in lanes:
        return bound_family
    from .production_review import provider_family

    wanted = provider_family(bound_family)
    matches = [name for name in lanes if wanted is not None and provider_family(name) == wanted]
    return matches[0] if len(matches) == 1 else None


def offer_policy_compatible(offered, current, bound_family, host=None) -> bool:
    """Whether an offer sealed under `offered` is still valid under `current`.

    Compatible when the execution-relevant projection is unchanged and the
    offer's own bound lane is still enabled with the same provider. A toggle of
    any other lane never invalidates the offer; disabling its own lane does.
    Anything malformed is incompatible (fail closed).
    """
    if not isinstance(offered, dict) or not isinstance(current, dict):
        return False
    if offered == current:
        return True
    lane_name = _lane_for_family(current.get("lanes"), bound_family)
    if lane_name is None:
        return False
    try:
        lane = current["lanes"][lane_name]
        sealed_lane = offered["lanes"][lane_name]
    except (KeyError, TypeError):
        return False
    if not isinstance(lane, dict) or lane.get("enabled") is not True:
        return False
    if not isinstance(sealed_lane, dict) or lane.get("provider") != sealed_lane.get("provider"):
        return False
    return execution_projection(offered, host) == execution_projection(current, host)
