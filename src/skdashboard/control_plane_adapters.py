"""Bounded, read-only cross-estate observation adapters."""

from __future__ import annotations

import hashlib
import json
import math
import os
import signal
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping

ADAPTER_VERSION = "1.0.0"
SCHEMA_VERSION = "1.1.0"


@dataclass(frozen=True)
class AdapterSpec:
    adapter_id: str
    owner: str
    population: str
    fields: tuple[str, ...]
    ttl_seconds: int = 60
    timeout_ms: int = 1_000
    classification: str = "internal"


SPECS = (
    AdapterSpec(
        "skcapstone.portfolio",
        "SKCapstone",
        "portfolio_project_work",
        ("total", "open", "in_progress", "done"),
    ),
    AdapterSpec(
        "skcoord.flow", "skcoord", "task_flow", ("open", "in_progress", "done", "blocked")
    ),
    AdapterSpec(
        "skcoord.agent_presence",
        "skcoord",
        "agent_presence",
        ("total_agents", "active_agents"),
    ),
    AdapterSpec(
        "skcapstone.itil",
        "SKCapstone ITIL",
        "itil_records",
        ("open_incidents", "sev1", "sev2", "awaiting_cab"),
    ),
    AdapterSpec(
        "skcapstone.service_release",
        "SKCapstone",
        "service_release_observations",
        ("services", "releases"),
    ),
    AdapterSpec(
        "cmdb.configuration",
        "CMDB",
        "configuration_items",
        ("total", "operational", "degraded", "other_status", "fresh", "stale", "unknown"),
    ),
    AdapterSpec(
        "skcapstone.fleet",
        "SKCapstone Fleet",
        "fleet_runtime",
        ("graded", "skipped", "error", "warn", "info", "ok"),
    ),
    AdapterSpec(
        "skcounter.harness",
        "SKCounter",
        "harness_reported",
        (
            "tokens_total",
            "cost_usd",
            "cost_state",
            "observation_count",
            "fresh_collectors",
            "delayed_collectors",
            "stale_collectors",
        ),
    ),
    AdapterSpec(
        "skgateway.observed",
        "SKGateway",
        "gateway_observed",
        (
            "tokens_total",
            "cost_usd",
            "cost_state",
            "observation_count",
            "fresh_collectors",
            "delayed_collectors",
            "stale_collectors",
        ),
    ),
    AdapterSpec("skperf.aggregate", "SKPerf", "approved_benchmarks", ("regressions", "capacity_pressure")),
    AdapterSpec("skjoule.wallet", "SKJoule", "wallets", ("total_supply", "active_agents")),
    AdapterSpec("capauth.policy", "CapAuth", "policy_health", ("available", "denials"), classification="confidential"),
    AdapterSpec("atlas.conditions", "Atlas", "operator_conditions", ("open_conditions", "ready_actions"), classification="confidential"),
    AdapterSpec("skos.discovery", "SKOS", "module_discovery", ("discovered", "unavailable")),
    AdapterSpec("sklegal.global", "SKLegal", "policy_filtered_global_aggregate", ("matters", "deadline_pressure"), classification="confidential"),
    AdapterSpec("hammertime.pipeline", "HammerTime", "approved_aggregate_pipeline", ("approved_releases", "pipeline_failures"), classification="confidential"),
)

@dataclass(frozen=True)
class Reader:
    payload: dict | None = None
    adapter_id: str | None = None
    home: Path | None = None
    timeout_ms: int | None = None
    failure: str | None = None

    def __call__(self) -> dict:
        failures = {
            "timeout": TimeoutError,
            "unreachable": ConnectionError,
            "unauthorized": PermissionError,
            "unavailable": RuntimeError,
        }
        if self.failure in failures:
            raise failures[self.failure]
        if self.payload is not None:
            return self.payload
        if self.adapter_id and self.home is not None and self.timeout_ms is not None:
            return _subprocess_read(self.adapter_id, self.home, self.timeout_ms)
        raise RuntimeError


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _error(
    spec: AdapterSpec,
    projected_at: str,
    code: str,
    message: str,
    *,
    truth_state: str = "unavailable",
    visibility: dict | None = None,
) -> dict:
    visibility = (
        _visibility(spec)
        if spec.adapter_id == "sklegal.global"
        else (visibility or _visibility(spec))
    )
    return {
        "adapter_id": spec.adapter_id,
        "adapter_version": ADAPTER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "owner": spec.owner,
        "population": spec.population,
        "classification": spec.classification,
        "visibility": visibility,
        "query_budget": {"max_items": 1, "timeout_ms": spec.timeout_ms},
        "ttl_seconds": spec.ttl_seconds,
        "age_seconds": None,
        "observed_at": None,
        "projected_at": projected_at,
        "watermark": {"source": spec.adapter_id, "value": None},
        "truth_state": truth_state,
        "coverage": {"expected": None, "reporting": None},
        "aggregate": None,
        "errors": [{"code": code, "message": message, "retryable": True}],
    }


def _visibility(spec: AdapterSpec) -> dict:
    if spec.adapter_id == "sklegal.global":
        return {
            "state": "policy_filtered",
            "authorization": "unknown",
            "reason": "Tenant and Matter policy was not evaluated at global scope",
        }
    return {"state": "visible", "authorization": "authorized"}


def _project(spec: AdapterSpec, reader: Reader | None, now: datetime | None) -> dict:
    def projected_at() -> str:
        instant = now or datetime.now(timezone.utc)
        return instant.isoformat().replace("+00:00", "Z")

    if reader is None or not isinstance(reader, Reader):
        return _error(
            spec,
            projected_at(),
            "SOURCE_UNAVAILABLE",
            "No authorized aggregate reader is configured",
        )

    try:
        raw = reader()
    except PermissionError:
        return _error(
            spec,
            projected_at(),
            "SOURCE_UNAUTHORIZED",
            "The aggregate reader is not authorized",
            truth_state="unknown",
            visibility={"state": "unauthorized", "authorization": "denied"},
        )
    except ConnectionError:
        return _error(
            spec,
            projected_at(),
            "SOURCE_UNREACHABLE",
            "The aggregate reader is unreachable",
            truth_state="unreachable",
        )
    except TimeoutError:
        return _error(
            spec,
            projected_at(),
            "SOURCE_TIMEOUT",
            "The aggregate reader exceeded its query budget",
        )
    except ValueError:
        return _error(
            spec,
            projected_at(),
            "SOURCE_MALFORMED",
            "The aggregate reader returned malformed data",
        )
    except Exception:  # noqa: BLE001 - source failures are masked at this boundary
        return _error(
            spec,
            projected_at(),
            "SOURCE_UNAVAILABLE",
            "The aggregate reader is unavailable",
        )
    projected = projected_at()
    instant = now or datetime.now(timezone.utc)
    if not isinstance(raw, dict) or raw.get("schema_version") != SCHEMA_VERSION:
        return _error(
            spec,
            projected,
            "SOURCE_MALFORMED",
            "The aggregate reader returned an incompatible schema",
        )

    observed = _iso(raw.get("observed_at"))
    aggregate = raw.get("aggregate")
    coverage = raw.get("coverage")
    watermark = raw.get("watermark")
    errors = raw.get("errors", [])
    has_observations = raw.get("has_observations")
    if (
        set(raw)
        != {
            "schema_version",
            "observed_at",
            "watermark",
            "coverage",
            "aggregate",
            "errors",
            "has_observations",
        }
        or not isinstance(errors, list)
        or len(errors) > 16
        or observed is None
        or not isinstance(aggregate, dict)
        or set(aggregate) != set(spec.fields)
        or any(
            not isinstance(value, (str, int, float, bool, type(None)))
            or (isinstance(value, str) and len(value) > 128)
            or (isinstance(value, float) and not math.isfinite(value))
            for value in aggregate.values()
        )
        or not isinstance(coverage, dict)
        or set(coverage) != {"expected", "reporting"}
        or not all(
            value is None or (isinstance(value, int) and not isinstance(value, bool))
            for value in coverage.values()
        )
        or not isinstance(watermark, str)
        or not watermark
        or len(watermark) > 256
        or any(not isinstance(value, str) for value in errors)
        or not isinstance(has_observations, bool)
        or len(json.dumps(aggregate, separators=(",", ":"), default=str).encode()) > 4_096
    ):
        return _error(
            spec, projected, "SOURCE_MALFORMED", "The aggregate reader returned malformed data"
        )

    expected = coverage["expected"]
    reporting = coverage["reporting"]
    if expected is not None and reporting is not None and (expected < 0 or reporting < 0 or reporting > expected):
        return _error(
            spec, projected, "SOURCE_MALFORMED", "The aggregate reader returned invalid coverage"
        )

    age = (instant - observed).total_seconds()
    if age < -300:
        return _error(
            spec, projected, "SOURCE_MALFORMED", "The source observation is in the future"
        )
    if errors and not has_observations:
        return _error(
            spec,
            projected,
            "SOURCE_UNAVAILABLE",
            "The aggregate source returned no usable evidence",
        )

    age_seconds = max(0, int(age))
    truth_state = "current" if has_observations else "unknown"
    if errors or (expected is not None and reporting is not None and reporting < expected):
        truth_state = "partial"
    elif has_observations and age_seconds > spec.ttl_seconds:
        truth_state = "stale"

    result = {
        "adapter_id": spec.adapter_id,
        "adapter_version": ADAPTER_VERSION,
        "schema_version": SCHEMA_VERSION,
        "owner": spec.owner,
        "population": spec.population,
        "classification": spec.classification,
        "visibility": _visibility(spec),
        "query_budget": {"max_items": 1, "timeout_ms": spec.timeout_ms},
        "ttl_seconds": spec.ttl_seconds,
        "age_seconds": age_seconds,
        "observed_at": observed.isoformat().replace("+00:00", "Z"),
        "projected_at": projected,
        "watermark": {"source": spec.adapter_id, "value": watermark},
        "truth_state": truth_state,
        "coverage": coverage,
        "aggregate": (
            {key: aggregate[key] for key in spec.fields}
            if truth_state != "unknown"
            else None
        ),
        "errors": [
            {"code": "SOURCE_PARTIAL", "message": "The aggregate reader reported partial evidence", "retryable": True}
            for _ in errors[:1]
        ],
    }
    return result


def project_estate(readers: Mapping[str, Reader], *, now: datetime | None = None) -> list[dict]:
    """Project one bounded, policy-safe aggregate for every declared population."""
    instant = now.astimezone(timezone.utc) if now else None
    with ThreadPoolExecutor(max_workers=len(SPECS)) as executor:
        futures = [
            executor.submit(_project, spec, readers.get(spec.adapter_id), instant)
            for spec in SPECS
        ]
        return [future.result() for future in futures]


def aggregate_reader(
    aggregate: dict,
    *,
    expected: int | None = 1,
    reporting: int | None = 1,
    observed_at: str | None = None,
    errors: list[str] | None = None,
    has_observations: bool = True,
    watermark_data: object | None = None,
) -> Reader:
    """Wrap an already bounded aggregate in the adapter input contract."""
    safe = json.dumps(
        {
            "aggregate": aggregate,
            "coverage": {"expected": expected, "reporting": reporting},
            "errors": errors or [],
            "has_observations": has_observations,
            "source": watermark_data,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    watermark = f"sha256:{hashlib.sha256(safe.encode()).hexdigest()}"
    timestamp = observed_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return Reader(
        payload={
            "schema_version": SCHEMA_VERSION,
            "observed_at": timestamp,
            "watermark": watermark,
            "coverage": {"expected": expected, "reporting": reporting},
            "aggregate": aggregate,
            "errors": list(errors or []),
            "has_observations": has_observations,
        }
    )


def _local_readers(
    home: Path, *, board_data: dict, default_observed_at: str | None = None
) -> dict[str, Callable[[], dict]]:
    """Reuse existing read models without crossing protected source boundaries."""
    readers: dict[str, Callable[[], dict]] = {}
    summary = board_data.get("summary")
    tasks = board_data.get("tasks")
    agents = board_data.get("agents")
    if (
        not board_data.get("error")
        and isinstance(summary, dict)
        and all(isinstance(summary.get(key), int) for key in ("total", "open", "in_progress", "done"))
        and isinstance(tasks, list)
        and isinstance(agents, list)
        and all(isinstance(task, dict) and isinstance(task.get("status"), str) for task in tasks)
        and all(isinstance(agent, dict) and isinstance(agent.get("state"), str) for agent in agents)
    ):
        readers["skcapstone.portfolio"] = aggregate_reader(
            {key: summary.get(key, 0) for key in ("total", "open", "in_progress", "done")},
            observed_at=default_observed_at,
        )
        readers["skcoord.flow"] = aggregate_reader({
            "open": summary.get("open", 0),
            "in_progress": summary.get("in_progress", 0),
            "done": summary.get("done", 0),
            "blocked": sum(task.get("status") == "blocked" for task in tasks),
        }, observed_at=default_observed_at)
        readers["skcoord.agent_presence"] = aggregate_reader({
            "total_agents": len(agents),
            "active_agents": sum(agent.get("state") == "active" for agent in agents),
        }, observed_at=default_observed_at)

    from . import dashboard_cmdb, dashboard_fleet, dashboard_itil, dashboard_skcounter

    def itil() -> dict:
        raw = dashboard_itil.get_overview(home)
        if raw.get("error"):
            raise RuntimeError
        kpis = raw.get("kpis")
        activity = raw.get("activity")
        fields = ("open_incidents", "sev1", "sev2", "awaiting_cab")
        if not isinstance(kpis, dict) or not all(key in kpis for key in fields) or not isinstance(activity, list):
            raise ValueError
        observed_at = max(
            (item.get("ts") for item in activity if item.get("ts")),
            default=None,
        )
        return aggregate_reader(
            {
                key: kpis.get(key)
                for key in fields
            },
            observed_at=observed_at,
            watermark_data=observed_at,
            has_observations=bool(activity),
        )()

    def cmdb() -> dict:
        raw = dashboard_cmdb.get_overview(home)
        health = raw.get("health")
        freshness = raw.get("evidence_health")
        required_freshness = ("fresh", "stale", "unknown", "unreachable")
        if (
            not isinstance(raw.get("total"), int)
            or not isinstance(health, dict)
            or not all(isinstance(value, int) for value in health.values())
            or not isinstance(freshness, dict)
            or not all(isinstance(freshness.get(key), int) for key in required_freshness)
        ):
            raise ValueError
        stale = freshness.get("stale", 0)
        unknown = freshness.get("unknown", 0) + freshness.get("unreachable", 0)
        reconciliation = raw.get("last_successful_reconciliation")
        return aggregate_reader(
            {
                "total": raw.get("total", 0),
                "operational": health.get("operational", 0),
                "degraded": health.get("degraded", 0),
                "other_status": sum(
                    value
                    for key, value in health.items()
                    if key not in {"operational", "degraded"}
                ),
                "fresh": freshness.get("fresh", 0),
                "stale": stale,
                "unknown": unknown,
            },
            errors=["partial"] if stale or unknown else [],
            has_observations=bool(raw.get("total")) and bool(reconciliation),
            observed_at=reconciliation,
            watermark_data=reconciliation,
        )()

    def fleet() -> dict:
        raw = dashboard_fleet.get_drift(home, alert=False)
        summary = raw.get("summary")
        fields = ("graded", "skipped", "error", "warn", "info", "ok")
        if (
            not isinstance(summary, dict)
            or not all(isinstance(summary.get(key), int) for key in fields)
            or not isinstance(raw.get("errors"), list)
        ):
            raise ValueError
        return aggregate_reader(
            {key: summary[key] for key in fields},
            expected=summary.get("graded", 0) + summary.get("skipped", 0),
            reporting=summary.get("graded", 0),
            errors=["partial"] if raw.get("errors") else [],
            has_observations=bool(summary["graded"] or summary["skipped"]),
            observed_at=default_observed_at,
        )()

    def usage(lane: str) -> dict:
        raw = dashboard_skcounter.get_ai_usage(home, {"lane": lane})
        summary = raw.get("summary")
        coverage = raw.get("coverage")
        collectors = raw.get("collectors")
        required_coverage = (
            "expected_nodes",
            "reporting_nodes",
            "fresh_collectors",
            "delayed_collectors",
            "stale_collectors",
        )
        if (
            not isinstance(summary, dict)
            or "total" not in summary
            or "cost_state" not in summary
            or not isinstance(coverage, dict)
            or not all(isinstance(coverage.get(key), int) for key in required_coverage)
            or not isinstance(collectors, list)
            or not isinstance(raw.get("observation_count"), int)
            or not isinstance(raw.get("errors"), list)
        ):
            raise ValueError
        observed = min(
            (item.get("last_seen") for item in collectors if item.get("last_seen")),
            default=raw.get("generated_at"),
        )
        stale_collectors = coverage.get("stale_collectors", 0)
        delayed_collectors = coverage.get("delayed_collectors", 0)
        collector_states = {
            item.get("status") for item in collectors if item.get("status")
        }
        return aggregate_reader(
            {
                "tokens_total": summary.get("total", 0),
                "cost_usd": summary.get("cost_usd") if summary.get("cost_state") == "available" else None,
                "cost_state": summary.get("cost_state", "unavailable"),
                "observation_count": raw.get("observation_count", 0),
                "fresh_collectors": coverage["fresh_collectors"],
                "delayed_collectors": delayed_collectors,
                "stale_collectors": stale_collectors,
            },
            expected=coverage.get("expected_nodes"),
            reporting=coverage.get("reporting_nodes"),
            observed_at=observed,
            errors=["partial"] if raw.get("errors") or len(collector_states) > 1 else [],
            has_observations=bool(raw.get("observation_count")),
            watermark_data=collectors,
        )()

    def joule() -> dict:
        from skcapstone.skjoule import JouleEngine

        stats = JouleEngine(home=home).get_network_stats()
        balances = stats.agent_balances
        if not isinstance(balances, dict) or not isinstance(stats.active_agents, int):
            raise ValueError
        return aggregate_reader(
            {
                "total_supply": sum(balances.values()),
                "active_agents": stats.active_agents,
            },
            has_observations=bool(balances),
            observed_at=default_observed_at,
        )()

    readers.update({
        "skcapstone.itil": itil,
        "cmdb.configuration": cmdb,
        "skcapstone.fleet": fleet,
        "skcounter.harness": lambda: usage("harness_reported"),
        "skgateway.observed": lambda: usage("gateway_observed"),
        "skjoule.wallet": joule,
    })
    return readers


_IMPLEMENTED = {
    "skcapstone.portfolio",
    "skcoord.flow",
    "skcoord.agent_presence",
    "skcapstone.itil",
    "cmdb.configuration",
    "skcapstone.fleet",
    "skcounter.harness",
    "skgateway.observed",
    "skjoule.wallet",
}


def _read_source(adapter_id: str, home: Path) -> dict:
    from .dashboard import _get_board_state

    board_ids = {"skcapstone.portfolio", "skcoord.flow", "skcoord.agent_presence"}
    board_data = _get_board_state(home) if adapter_id in board_ids else {}
    readers = _local_readers(home, board_data=board_data)
    reader = readers.get(adapter_id)
    if reader is None:
        raise LookupError(adapter_id)
    return reader()


def _subprocess_read(adapter_id: str, home: Path, timeout_ms: int) -> dict:
    environment = os.environ.copy()
    package_root = str(Path(__file__).resolve().parents[1])
    environment["PYTHONPATH"] = os.pathsep.join(
        value for value in (package_root, environment.get("PYTHONPATH")) if value
    )
    completed = _bounded_run(
        [sys.executable, "-m", __name__, "--worker", adapter_id, str(home)],
        timeout_ms=timeout_ms,
        environment=environment,
    )
    failures = {4: ValueError, 5: PermissionError, 6: ConnectionError, 7: ValueError}
    if completed.returncode in failures:
        raise failures[completed.returncode]
    if completed.returncode != 0:
        raise RuntimeError
    if len(completed.stdout.encode()) > 8_192:
        raise ValueError
    raw = json.loads(completed.stdout)
    if not isinstance(raw, dict):
        raise ValueError
    return raw


def _bounded_run(
    command: list[str], *, timeout_ms: int, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout_ms / 1_000)
    except subprocess.TimeoutExpired as exc:
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
        except ProcessLookupError:
            pass
        process.communicate()
        raise TimeoutError from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def default_readers(home: Path) -> dict[str, Reader]:
    """Build independently kill-bounded readers for qualified local aggregates."""
    timeouts = {spec.adapter_id: spec.timeout_ms for spec in SPECS}
    return {
        adapter_id: Reader(
            adapter_id=adapter_id,
            home=home,
            timeout_ms=timeouts[adapter_id],
        )
        for adapter_id in _IMPLEMENTED
    }


def _worker(argv: list[str]) -> int:
    if len(argv) != 3 or argv[0] != "--worker" or argv[1] not in _IMPLEMENTED:
        return 2
    result_fd = os.dup(sys.stdout.fileno())
    devnull = os.open(os.devnull, os.O_WRONLY)
    status = 0
    payload = None
    try:
        os.dup2(devnull, sys.stdout.fileno())
        os.dup2(devnull, sys.stderr.fileno())
        try:
            payload = _read_source(argv[1], Path(argv[2]))
        except PermissionError:
            status = 5
        except ConnectionError:
            status = 6
        except ValueError:
            status = 7
        except Exception:  # noqa: BLE001 - the parent exposes only a fixed failure code
            status = 3
    finally:
        os.close(devnull)
    if status:
        os.close(result_fd)
        return status
    if payload is None:
        os.close(result_fd)
        return 3
    output = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    if len(output.encode()) > 8_192:
        os.close(result_fd)
        return 4
    os.write(result_fd, output.encode())
    os.close(result_fd)
    return 0


if __name__ == "__main__":
    raise SystemExit(_worker(sys.argv[1:]))
