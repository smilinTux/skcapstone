"""Shared policy binding for the native production dispatcher."""

from pathlib import Path
from typing import Mapping

from .production_policy import load_production_policy


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
                "model": route.get("model", "disabled"),
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
    """Bind capacity evidence to the exact configured model and backend family."""
    return [
        route
        for route in routes
        if route.get("capacity_domain") in lane["capacity_domains"]
        and route.get("logical_route") == lane["model"]
    ]
