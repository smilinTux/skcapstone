"""Scheduled fleet CMDB reconciliation entrypoint."""

from __future__ import annotations

import logging
import socket
import time
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Callable, Sequence

from skcoord.cmdb import CMDBManager
from skcoord.cmdb_reconcile import (
    OrchestrationConfig,
    Target,
    apply_retirement_lifecycle,
    read_verified_run_artifacts,
    run_reconcile,
    scan_network,
    write_run_artifact,
)
from skcoord.cmdb_scheduler import (
    ReconcileLease,
    ScheduledReconcileConfig,
    load_reconcile_job_config,
    prune_run_artifacts,
    route_reconcile_incidents,
)

logger = logging.getLogger("skcapstone.cmdb_reconcile_job")


def _package_version() -> str:
    try:
        return version("skcapstone")
    except PackageNotFoundError:
        return "unknown"


def _current_aliases() -> set[str]:
    from .scheduler_jobs import current_host_aliases

    return {socket.gethostname(), *current_host_aliases()}


def _current_agent() -> str:
    from . import active_agent_name

    return active_agent_name() or ""


def _runner_factory(config: ScheduledReconcileConfig):
    from .cli.cmdb import _secure_runner_factory

    targets = [Target(host, ("scheduled-config",)) for host in config.targets]
    mappings = tuple(f"{host}={config.credential_refs[host]}" for host in config.targets)
    factory = _secure_runner_factory(targets, mappings)

    def build(host: str):
        runner = factory(host)
        runner.timeout = max(1, int(config.timeout_seconds))
        return runner

    return build


def _is_due(artifacts: Sequence[dict], config: ScheduledReconcileConfig, now: datetime) -> bool:
    if not artifacts:
        return True
    latest = max(artifacts, key=lambda item: str(item.get("ended_at", "")))
    try:
        ended = datetime.fromisoformat(str(latest["ended_at"]))
    except (KeyError, TypeError, ValueError):
        return True
    return (now - ended).total_seconds() >= config.cadence_seconds


def _scan_with_retries(
    home: Path,
    config: ScheduledReconcileConfig,
    runner_factory: Callable,
    sleep: Callable[[float], None],
):
    targets = [Target(host, ("scheduled-config",)) for host in config.targets]
    bounds = OrchestrationConfig(
        global_concurrency=config.global_concurrency,
        per_host_concurrency=config.per_host_concurrency,
        deadline_seconds=config.timeout_seconds * max(1, len(targets)),
        failure_budget=config.failure_budget,
    )
    result = None
    attempts = 0
    for attempt in range(config.retry_count + 1):
        attempts = attempt + 1
        result = scan_network(home, targets, runner_factory, bounds)
        if result.complete:
            break
        if attempt < config.retry_count and config.retry_backoff_seconds:
            sleep(config.retry_backoff_seconds * (attempt + 1))
    return result, attempts


def run_cmdb_reconcile_job(
    home: Path | None = None,
    *,
    runner_factory: Callable | None = None,
    aliases: set[str] | None = None,
    agent: str | None = None,
    now: datetime | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> dict:
    """Run one safe, lease-protected scheduled reconcile tick."""
    if home is None:
        from . import SHARED_ROOT

        home = Path(SHARED_ROOT).expanduser()
    home = Path(home).expanduser()
    config_path = home / "config" / "cmdb-reconcile.json"
    config = load_reconcile_job_config(config_path)
    if not config.enabled:
        return {"outcome": "disabled", "config": str(config_path)}
    active_aliases = aliases if aliases is not None else _current_aliases()
    if config.owner_node not in active_aliases:
        return {
            "outcome": "wrong_node",
            "owner_node": config.owner_node,
            "active_aliases": sorted(active_aliases),
        }
    active_agent = agent if agent is not None else _current_agent()
    if active_agent != config.agent:
        return {
            "outcome": "wrong_agent",
            "configured_agent": config.agent,
            "active_agent": active_agent,
        }
    current = now or datetime.now(timezone.utc)
    artifacts = read_verified_run_artifacts(home)
    if not _is_due(artifacts, config, current):
        return {"outcome": "not_due", "cadence_seconds": config.cadence_seconds}

    with ReconcileLease(home, config.owner_node, config.agent) as lease:
        if not lease.acquired:
            return {"outcome": "lease_held"}
        artifacts = read_verified_run_artifacts(home)
        if not _is_due(artifacts, config, current):
            return {"outcome": "not_due", "cadence_seconds": config.cadence_seconds}

        factory = runner_factory or _runner_factory(config)
        scan_result, attempts = _scan_with_retries(home, config, factory, sleep)
        mgr = CMDBManager(home)
        scope = scan_result.scope_fingerprint()
        discovered_ids = [item.ci_id for item in scan_result.discovered]
        owned_ids = [
            ci.id
            for ci in mgr.list_cis()
            if "discovered" in (ci.tags or [])
            and str(ci.attributes.get("source_authority", "")).startswith("network:")
            and ci.attributes.get("lifecycle_scope") == scope
        ]
        lifecycle = apply_retirement_lifecycle(
            mgr,
            "network:fleet",
            scope,
            discovered_ids,
            owned_ids,
            scan_result.complete,
            threshold=config.stale_grace_runs,
            apply=False,
            agent=config.agent,
        )
        artifact, _ = run_reconcile(
            mgr,
            scan_result,
            apply=config.apply_safe_observations,
            code_version=_package_version(),
            config_version=config.fingerprint(),
            lifecycle_actions=lifecycle,
            agent=config.agent,
        )
        validation_failures = artifact.get("plan", {}).get("validation_failures", [])
        applied = config.apply_safe_observations and not validation_failures
        if applied:
            apply_retirement_lifecycle(
                mgr,
                "network:fleet",
                scope,
                discovered_ids,
                owned_ids,
                scan_result.complete,
                threshold=config.stale_grace_runs,
                apply=True,
                agent=config.agent,
            )
        artifact["job"] = {
            "owner_node": config.owner_node,
            "agent": config.agent,
            "attempts": attempts,
            "cadence_seconds": config.cadence_seconds,
            "outcome": "complete" if scan_result.complete else "partial",
        }
        incident_ids = route_reconcile_incidents(home, [artifact, *artifacts], config)
        artifact["itil"] = {"incident_ids": incident_ids}
        path, checksum = write_run_artifact(home, artifact)
        removed = prune_run_artifacts(home, config.retention_runs)
        result = {
            "outcome": artifact["job"]["outcome"],
            "scan_id": artifact["scan_id"],
            "applied": applied,
            "attempts": attempts,
            "artifact": str(path),
            "sha256": checksum,
            "incident_ids": incident_ids,
            "retained_runs": config.retention_runs,
            "pruned_runs": len(removed),
        }
        logger.info("CMDB scheduled reconcile: %s", result)
        return result
