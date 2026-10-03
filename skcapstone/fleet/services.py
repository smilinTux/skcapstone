"""Service kind model (spec 5.2): normalization, validation, workload map.

The spec side only. Actuation lives in converge.py (sknoded side) and
placement policy in service_controller.py (control-plane side).
"""

from __future__ import annotations

from .scheduler import DEFAULT_REQUESTS, Workload

RUNTIMES = frozenset({"systemd-user", "docker"})
FAILOVER_MODES = frozenset({"manual", "auto"})
RESTART_POLICIES = frozenset({"on-failure", "never"})


class ServiceSpecError(ValueError):
    """A Service spec is malformed and must not be actuated."""


def normalize_service_spec(spec: dict) -> dict:
    """Return a full Service spec with defaults applied, or raise.

    Defaults are deliberately conservative (R4): failover manual, restart
    on-failure with backoff, one replica, not paused. A spec that fails
    validation must never reach an actuation verb; callers treat
    ServiceSpecError as "do not touch the unit" (degrade-safe).

    Raises:
        ServiceSpecError: missing unit, unknown runtime/failover/policy,
            or an unsupported healthCheck shape.
    """
    unit = spec.get("unit")
    if not unit or not isinstance(unit, str):
        raise ServiceSpecError("spec.unit is required (unit name or container)")
    runtime = spec.get("runtime", "systemd-user")
    if runtime not in RUNTIMES:
        raise ServiceSpecError(f"unknown runtime {runtime!r} (known: {sorted(RUNTIMES)})")
    failover = spec.get("failover", "manual")
    if failover not in FAILOVER_MODES:
        raise ServiceSpecError(f"unknown failover {failover!r} (known: {sorted(FAILOVER_MODES)})")
    policy = spec.get("restartPolicy", "on-failure")
    if policy not in RESTART_POLICIES:
        raise ServiceSpecError(
            f"unknown restartPolicy {policy!r} (known: {sorted(RESTART_POLICIES)})"
        )
    health = spec.get("healthCheck")
    if health is not None and (not isinstance(health, dict) or "port" not in health):
        raise ServiceSpecError("healthCheck must be {'port': int} in v1")
    return {
        "runtime": runtime,
        "unit": unit,
        "replicas": 1,  # v1: always one (spec 5.2, replicas almost always 1)
        "nodeSelector": dict(spec.get("nodeSelector", {})),
        "tolerations": list(spec.get("tolerations", [])),
        "resources": dict(spec.get("resources", DEFAULT_REQUESTS)),
        "healthCheck": dict(health) if health else None,
        "restartPolicy": policy,
        "failover": failover,
        "paused": bool(spec.get("paused", False)),
        "deleted": bool(spec.get("deleted", False)),
        "compose": dict(spec["compose"]) if spec.get("compose") else None,
    }


def service_workload(payload: dict) -> Workload:
    """Map a full Service spec file to the scheduler's Workload."""
    spec = normalize_service_spec(payload.get("spec", {}))
    return Workload(
        kind="service",
        name=payload["name"],
        node_selector=spec["nodeSelector"],
        tolerations=tuple(spec["tolerations"]),
        requests=spec["resources"],
    )
