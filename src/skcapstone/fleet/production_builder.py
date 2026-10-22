"""Production builder policy and resource-bounded native service custody."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
from pathlib import Path

from . import production_routes, scheduler, store
from .production_policy import load_production_policy


def policy() -> dict | None:
    """Require the shared policy and its explicit authority in production mode."""
    path = os.environ.get("SKFLEET_PRODUCTION_POLICY")
    if path is None:
        return None
    authority = os.environ.get("SKFLEET_AUTHORITY_HOST", "")
    if not path or not authority:
        raise ValueError("production builder policy authority is missing")
    return load_production_policy(Path(path), host=authority)


def digest(value: dict) -> str:
    """Bind only validated canonical policy content, never a caller path."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def node_binding(paths, node: str, value: dict) -> dict:
    """Resolve a node's explicit host identity and qualified worker resources."""
    spec = store.read_spec(paths, "node", node) or {}
    host = (spec.get("labels") or {}).get("host")
    limits = value.get("node_quotas", {}).get(host)
    if not isinstance(host, str) or not isinstance(limits, dict):
        raise ValueError("production builder node lacks qualified worker resources")
    return {
        "authority": value["authority_host"],
        "policy_sha256": digest(value),
        "host": host,
        "resources": dict(limits),
    }


def workload(card: str, binding: dict):
    """Use real per-worker resource requests in the existing scheduler."""
    limits = binding["resources"]
    return scheduler.Workload(
        "job",
        card,
        requests={
            "cores": limits["cpu_quota_percent"] / 100,
            "ram_gb": limits["memory_max_bytes"] / (1024**3),
        },
    )


def ready_nodes(paths, views: list, value: dict, card: str) -> list:
    """Retain native readiness, taints and resource checks without count caps."""
    result = []
    for view in views:
        try:
            binding = node_binding(paths, view.name, value)
        except ValueError:
            continue
        if scheduler.feasible(view, workload(card, binding)) is None:
            result.append(view)
    return result


def route_binding(
    value: dict, card: str, route: str, labels: list[str], *, family=None, model=None
) -> dict:
    """Select the smallest qualified route from actual card and gateway requirements."""
    eligible = [
        row
        for row in production_routes.candidates(value, route, labels)
        if family in (None, row["family"])
        and ("qwen-first" not in labels or row["family"] == "qwen")
        and model in (None, row["model"])
    ]
    if not eligible:
        raise production_routes.RouteUnavailableError(
            "production card has no currently qualified gateway route"
        )
    sizes = {"S": 0, "M": 1, "L": 2, "XL": 3}
    smallest = min(sizes[row["size_class"]] for row in eligible)
    eligible = [row for row in eligible if sizes[row["size_class"]] == smallest]
    return eligible[int(hashlib.sha256(card.encode()).hexdigest(), 16) % len(eligible)]


def validate_request(paths, node: str, request: dict, *, local: bool = False) -> dict | None:
    """Refuse stale policy, node, route or resource bindings before a launch."""
    value = policy()
    bound = request.get("production")
    if value is None:
        if bound is not None:
            raise ValueError("production request requires production policy")
        return None
    expected = node_binding(paths, node, value)
    if not isinstance(bound, dict) or any(
        bound.get(key) != item for key, item in expected.items()
    ):
        raise ValueError("production request policy changed")
    expected.update(
        route_binding(
            value,
            request["card_id"],
            request["logical_route"],
            request["labels"],
            family=bound.get("family"),
            model=bound.get("model"),
        )
    )
    if bound != expected:
        raise ValueError("production request policy changed")
    if local and expected["host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production request belongs to another execution host")
    return expected


def unit_name(request: dict, attempt: int) -> str:
    """Give every request attempt its own non-reusable service identity."""
    card, token = request["card_id"], request["request_id"]
    if not re.fullmatch(r"[0-9a-f]{8}", card) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("production service request identity invalid")
    if type(attempt) is not int or attempt <= 0:
        raise ValueError("production service attempt invalid")
    return f"skfleet-builder-{card}-{token}-{attempt}.service"


def service_command(request: dict, attempt: int, command: list[str], workspace: Path) -> list[str]:
    """Apply per-worker quotas to the actual service, without a worker ceiling."""
    limits = request["production"]["resources"]
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--wait",
        "--pipe",
        "--unit=" + unit_name(request, attempt),
        "--working-directory=" + str(workspace),
        "--property=CPUQuota=" + str(limits["cpu_quota_percent"]) + "%",
        "--property=MemoryMax=" + str(limits["memory_max_bytes"]),
        "--property=TasksMax=" + str(limits["tasks_max"]),
        "--property=RuntimeMaxSec=" + str(limits["runtime_max_seconds"]),
        "--property=KillMode=control-group",
        "--property=UMask=0077",
        "--",
        *command,
    ]


def service_state(status: dict, process=None) -> tuple[bool | None, int | None]:
    """Unknown unit or invocation custody never authorizes releasing a claim."""
    unit = status.get("unit")
    try:
        if unit != unit_name(status, status.get("attempt")):
            return None, None
    except (KeyError, ValueError):
        return None, None
    if not isinstance(unit, str) or not re.fullmatch(
        r"skfleet-builder-[0-9a-f]{8}-[0-9a-f]{64}-[1-9][0-9]*\.service", unit
    ):
        return None, None
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,InvocationID,ExecMainStatus",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode:
        return None, None
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    invocation = values.get("InvocationID", "")
    expected = status.get("invocation")
    if expected and invocation and invocation != expected:
        return None, None
    if values.get("LoadState") == "loaded" and values.get("ActiveState") in {
        "active",
        "activating",
        "deactivating",
    }:
        if re.fullmatch(r"[0-9a-f]{32}", invocation):
            status["invocation"] = invocation
        return True, None
    code = process.poll() if process is not None else None
    if code is not None and code >= 0:
        # This exact systemd-run --wait process has observed its service exit.
        return False, int(code)
    if expected and invocation == expected and values.get("ActiveState") in {"inactive", "failed"}:
        try:
            return False, int(values["ExecMainStatus"])
        except (KeyError, ValueError):
            pass
    return None, None
