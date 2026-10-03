"""Shared policy binding for the native production dispatcher."""

from copy import deepcopy
from pathlib import Path
from typing import Mapping

from .production_policy import load_production_policy
from .production_review import provider_family
from .review_capacity import eligible_gateway_routes


def production_policy_from_environment(environment: Mapping[str, str], host: str):
    """Load explicit production configuration without inventing fallback policy."""
    configured = environment.get("SKFLEET_PRODUCTION_POLICY")
    if not configured:
        return None
    policy = load_production_policy(Path(configured), host=host)
    if host not in policy.get("node_quotas", {}):
        raise ValueError("production authority requires qualified worker resource quotas")
    return policy


def production_lanes(policy, scan_budget):
    """Build route identities with a per-cycle scan budget, never worker ceilings."""
    domains = {
        "codex": ("codex",),
        "glm": ("zai",),
        "deepseek": ("deepseek",),
        "qwen": ("chiap08-qwen38", "chiap01-qwen38"),
        "kimi": (),
    }
    lanes = []
    for name, route in policy["lanes"].items():
        lanes.append(
            {
                "name": name,
                "prefix": name + "-auto-",
                "family": provider_family(name),
                "target": scan_budget if route["enabled"] else 0,
                "capacity_domains": domains[name],
            }
        )
    codex = next(lane for lane in lanes if lane["name"] == "codex")
    lanes.append({**codex, "name": "escalate", "prefix": "esc-auto-"})
    return lanes


def worker_resource_properties(policy, host):
    """Enforce the measured node's per-worker limits on the actual child cgroup."""
    if policy is None:
        return []
    quota = policy["node_quotas"][host]
    return [
        "--property=CPUQuota=%d%%" % quota["cpu_quota_percent"],
        "--property=MemoryMax=%d" % quota["memory_max_bytes"],
        "--property=TasksMax=%d" % quota["tasks_max"],
        "--property=RuntimeMaxSec=%d" % quota["runtime_max_seconds"],
    ]


def routes_for_lane(routes, lane):
    """Bind already qualified evidence to the lane's semantic provider family."""
    return [
        route
        for route in routes
        if lane.get("target", 0) > 0
        and provider_family(route.get("capacity_domain")) == lane["family"]
        and provider_family(route.get("provider")) == lane["family"]
    ]


def enabled_family_routes(routes, *, policy, lane=None):
    """Filter qualified rows by enabled family and matching typed attribution.

    The caller owns fresh gateway snapshot acquisition and qualification. A
    logical card bucket may differ from the exact selected gateway identifier;
    both fields remain unchanged. Transport-only identities cannot bind family.
    """
    wanted = "codex" if lane == "escalate" else provider_family(lane)
    if lane is not None and wanted is None:
        return []
    enabled = {
        provider_family(name)
        for name, row in policy["lanes"].items()
        if row.get("enabled") is True and row.get("provider") == "skgateway"
    } - {None}
    result = []
    for route in routes:
        if not isinstance(route, dict):
            continue
        family = provider_family(route.get("provider"))
        if (
            family not in enabled
            or (wanted is not None and family != wanted)
            or provider_family(route.get("capacity_domain")) != family
            or any(
                not isinstance(route.get(key), str) or not route[key].strip()
                for key in ("logical_route", "model_or_bucket")
            )
        ):
            continue
        result.append(deepcopy(route))
    return result


def resolve_production_routes(routes, *, policy, required_size, labels, lane=None, model_pin=None):
    """Resolve one card against fresh gateway rows, never a static model table.

    ``routes`` must be supplied from the current acquired/sealed snapshot. This
    pure helper does not cache or fetch metadata and cannot establish freshness
    for arbitrary rows. Existing gateway eligibility enforces size, privacy,
    health and actual request capacity. Caller retains claim/action gates.
    """
    pins = {
        "codex-only": "codex",
        "escalation-only": "codex",
        "glm-only": "zai",
        "deepseek-only": "deepseek",
        "qwen-only": "qwen",
        "kimi-only": "kimi",
    }
    required = {
        pins[label] for label in {str(item).strip().lower() for item in labels} if label in pins
    }
    if len(required) > 1 or required == {"kimi"}:
        return []
    if model_pin is not None and (not isinstance(model_pin, str) or not model_pin.strip()):
        return []
    rows = enabled_family_routes(routes, policy=policy, lane=lane)
    rows = [
        row
        for row in rows
        if all(type(row.get(key)) is int and row[key] >= 0 for key in ("max", "gateway_active"))
    ]
    eligible = eligible_gateway_routes(
        {"schema_version": 1, "routes": rows}, required_size, labels, {}
    )
    result = []
    for row in eligible:
        family = provider_family(row["provider"])
        if required and family not in required:
            continue
        if model_pin is not None and row["model_or_bucket"] != model_pin:
            continue
        result.append({**row, "family": family, "lane": "glm" if family == "zai" else family})
    return result


def cycle_budget_seconds(policy, seat=""):
    """Leave cleanup time inside the serialized 600-second seat generation."""
    budget = policy.get("cycle_budget_seconds", 250)
    return min(budget, 125) if seat in {"atlas", "seraph"} else budget


def authoritative_owner_state(store, card_id, state):
    """Use the native validated fold for claim custody, never a second raw fold."""
    card = store.fold(card_id)
    if card is None or card.id != card_id:
        raise ValueError("native production card is missing")
    status = getattr(card.status, "value", card.status)
    return {
        **state,
        "owner": card.owner,
        "status": status,
        "claim_revision": card.meta.get("_claim_revision"),
        "archived": card.archived,
    }
