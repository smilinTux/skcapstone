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
        or value.keys() - required - {"node_quotas", "cycle_budget_seconds", "scan_budget"}
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
    if not isinstance(lanes, dict) or set(lanes) != {*LANES, "kimi"}:
        raise ValueError("production policy lanes are invalid")
    for lane in LANES:
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
    for key in ("cycle_budget_seconds", "scan_budget"):
        if key in value and (type(value[key]) is not int or value[key] <= 0):
            raise ValueError("production policy cycle budget is invalid")
    if value.get("cycle_budget_seconds", 250) > 250:
        raise ValueError("production policy cycle budget exceeds dispatcher timeout margin")
    return value
