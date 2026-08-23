"""Read-only SKCounter projections for the Economy workspace.

The dashboard reads validated aggregate observations from the central
SKCounter data root. It never scans coding-harness session stores and never
accepts raw prompt or response material.
"""

from __future__ import annotations

import json
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = "skcounter.snapshot.v1"
LANES = frozenset({"harness_reported", "gateway_observed"})
VIEWS = frozenset(
    {
        "models",
        "daily",
        "hourly",
        "agents",
        "workspaces",
        "sessions",
        "tasks",
        "time_metrics",
    }
)
TOKEN_FIELDS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")
MAX_OBSERVATION_BYTES = 5 * 1024 * 1024
MAX_OBSERVATION_FILES = 10_000
FRESH_SECONDS = 45 * 60
DELAYED_SECONDS = 24 * 60 * 60
_SHA256_RE = re.compile(r"^[a-f0-9]{64}$")

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "idempotency_key",
        "measurement_lane",
        "node_id",
        "principal_id",
        "collector",
        "observed_at",
        "bucket_timezone",
        "window",
        "source_state_digest",
        "aggregates",
        "payload_hash",
    }
)
_AGGREGATE_FIELDS = frozenset(
    {
        "view",
        "bucket_start",
        "client",
        "provider",
        "model",
        "agent",
        "workspace_key",
        "workspace_label",
        "session_key",
        "task_label",
        "tokens",
        "message_count",
        "cost",
        "performance",
        "activity",
    }
)
_PROHIBITED_FIELDS = frozenset(
    {
        "prompt",
        "response",
        "content",
        "tool_input",
        "tool_output",
        "workspace_path",
        "source_path",
        "sessions_path",
        "session_id",
        "credential",
        "capability_token",
        "api_key",
        "cookie",
        "oauth_token",
    }
)


class SnapshotError(ValueError):
    """A snapshot cannot enter the dashboard read model."""


def _data_root(home: Path) -> Path:
    configured = os.environ.get("SKCOUNTER_DATA_DIR")
    return Path(configured).expanduser() if configured else Path(home).expanduser() / "skcounter"


def _parse_time(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise SnapshotError(f"{field} must be a date-time string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotError(f"{field} is not a valid date-time") from exc
    if parsed.tzinfo is None:
        raise SnapshotError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _text(value: Any, field: str, maximum: int = 256, *, optional: bool = False) -> str:
    if value is None and optional:
        return ""
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SnapshotError(f"{field} must be a non-empty bounded string")
    return value


def _number(value: Any, field: str, *, integer: bool = False) -> float | int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SnapshotError(f"{field} must be numeric")
    if not math.isfinite(float(value)) or value < 0:
        raise SnapshotError(f"{field} must be finite and non-negative")
    if integer and not isinstance(value, int):
        raise SnapshotError(f"{field} must be an integer")
    return value


def _exact_fields(value: dict, allowed: frozenset[str], field: str) -> None:
    extras = set(value) - allowed
    if extras:
        raise SnapshotError(f"{field} contains unsupported fields")


def _reject_prohibited(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in _PROHIBITED_FIELDS:
                raise SnapshotError("snapshot contains a prohibited raw-data field")
            _reject_prohibited(child)
    elif isinstance(value, list):
        for child in value:
            _reject_prohibited(child)


def _optional_hash(value: Any, field: str) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise SnapshotError(f"{field} must be a SHA-256 hex digest")
    return value


def _validate_tokens(value: Any) -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != set(TOKEN_FIELDS):
        raise SnapshotError("tokens must contain the exact v1 token fields")
    return {field: int(_number(value[field], f"tokens.{field}", integer=True)) for field in TOKEN_FIELDS}


def _validate_cost(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise SnapshotError("cost must be an object")
    expected = {"amount", "currency", "estimated", "pricing_revision"}
    if set(value) != expected or value.get("currency") != "USD":
        raise SnapshotError("cost must use the exact v1 USD fields")
    if not isinstance(value.get("estimated"), bool):
        raise SnapshotError("cost.estimated must be boolean")
    return {
        "amount": float(_number(value["amount"], "cost.amount")),
        "estimated": value["estimated"],
        "pricing_revision": _text(value["pricing_revision"], "cost.pricing_revision"),
    }


def _validate_performance(value: Any) -> dict[str, Any]:
    if value is None:
        return {
            "duration_ms": 0,
            "timed_tokens": 0,
            "sample_count": 0,
            "token_coverage": 0.0,
            "ms_per_1k_tokens": None,
        }
    if not isinstance(value, dict):
        raise SnapshotError("performance must be an object")
    required = {"duration_ms", "timed_tokens", "sample_count", "token_coverage"}
    allowed = required | {"ms_per_1k_tokens"}
    if not required.issubset(value) or set(value) - allowed:
        raise SnapshotError("performance fields do not match v1")
    coverage = float(_number(value["token_coverage"], "performance.token_coverage"))
    if coverage > 1:
        raise SnapshotError("performance.token_coverage cannot exceed one")
    speed = value.get("ms_per_1k_tokens")
    if speed is not None:
        speed = float(_number(speed, "performance.ms_per_1k_tokens"))
    return {
        "duration_ms": int(_number(value["duration_ms"], "performance.duration_ms", integer=True)),
        "timed_tokens": int(_number(value["timed_tokens"], "performance.timed_tokens", integer=True)),
        "sample_count": int(_number(value["sample_count"], "performance.sample_count", integer=True)),
        "token_coverage": coverage,
        "ms_per_1k_tokens": speed,
    }


def _validate_activity(value: Any) -> dict[str, int]:
    empty = {"active_seconds": 0, "longest_continuous_seconds": 0, "max_concurrent": 0}
    if value is None:
        return empty
    if not isinstance(value, dict) or set(value) != set(empty):
        raise SnapshotError("activity fields do not match v1")
    return {
        field: int(_number(value[field], f"activity.{field}", integer=True)) for field in empty
    }


def _normalize_snapshot(document: Any) -> tuple[dict, list[dict]]:
    if not isinstance(document, dict):
        raise SnapshotError("snapshot must be an object")
    _reject_prohibited(document)
    _exact_fields(document, _TOP_LEVEL_FIELDS, "snapshot")
    if document.get("schema_version") != SCHEMA_VERSION:
        raise SnapshotError("unsupported schema version")

    for digest_field in ("idempotency_key", "source_state_digest", "payload_hash"):
        if not _SHA256_RE.fullmatch(str(document.get(digest_field, ""))):
            raise SnapshotError(f"{digest_field} must be a SHA-256 hex digest")

    lane = document.get("measurement_lane")
    if lane not in LANES:
        raise SnapshotError("unsupported measurement lane")
    observed = _parse_time(document.get("observed_at"), "observed_at")
    node_id = _text(document.get("node_id"), "node_id", 128)
    principal_id = _text(document.get("principal_id"), "principal_id", 128)
    bucket_timezone = _text(document.get("bucket_timezone"), "bucket_timezone", 64)

    window = document.get("window")
    if not isinstance(window, dict) or set(window) != {"start", "end"}:
        raise SnapshotError("window fields do not match v1")
    window_start = _parse_time(window.get("start"), "window.start")
    window_end = _parse_time(window.get("end"), "window.end")
    if window_start > window_end:
        raise SnapshotError("window.start cannot be after window.end")

    collector = document.get("collector")
    if not isinstance(collector, dict):
        raise SnapshotError("collector must be an object")
    if set(collector) != {"product", "facade_version", "backend", "backend_version"}:
        raise SnapshotError("collector fields do not match v1")
    if collector.get("product") != "skcounter":
        raise SnapshotError("collector product must be skcounter")
    normalized_collector = {
        "facade_version": _text(collector.get("facade_version"), "collector.facade_version", 64),
        "backend": _text(collector.get("backend"), "collector.backend", 64),
        "backend_version": _text(
            collector.get("backend_version"), "collector.backend_version", 64
        ),
    }

    aggregates = document.get("aggregates")
    if not isinstance(aggregates, list) or len(aggregates) > 10_000:
        raise SnapshotError("aggregates must be a bounded array")

    meta = {
        "lane": lane,
        "node_id": node_id,
        "principal_id": principal_id,
        "observed_at": observed,
        "observed_at_text": observed.isoformat().replace("+00:00", "Z"),
        "bucket_timezone": bucket_timezone,
        "window_start": window_start,
        "window_end": window_end,
        **normalized_collector,
    }
    rows: list[dict] = []
    for index, aggregate in enumerate(aggregates):
        if not isinstance(aggregate, dict):
            raise SnapshotError(f"aggregate {index} must be an object")
        _exact_fields(aggregate, _AGGREGATE_FIELDS, f"aggregate {index}")
        view = aggregate.get("view")
        if view not in VIEWS:
            raise SnapshotError(f"aggregate {index} has unsupported view")
        bucket = _parse_time(aggregate.get("bucket_start"), f"aggregate {index}.bucket_start")
        rows.append(
            {
                **meta,
                "view": view,
                "bucket_start": bucket,
                "bucket_start_text": bucket.isoformat().replace("+00:00", "Z"),
                "client": _text(aggregate.get("client"), "client", 128, optional=True),
                "provider": _text(aggregate.get("provider"), "provider", 128, optional=True),
                "model": _text(aggregate.get("model"), "model", 256, optional=True),
                "agent": _text(aggregate.get("agent"), "agent", 128, optional=True),
                "workspace_key": _optional_hash(aggregate.get("workspace_key"), "workspace_key"),
                "workspace_label": _text(
                    aggregate.get("workspace_label"), "workspace_label", 128, optional=True
                ),
                "session_key": _optional_hash(aggregate.get("session_key"), "session_key"),
                "task_label": _text(
                    aggregate.get("task_label"), "task_label", 160, optional=True
                ),
                "tokens": _validate_tokens(aggregate.get("tokens")),
                "message_count": int(
                    _number(aggregate.get("message_count", 0), "message_count", integer=True)
                ),
                "cost": _validate_cost(aggregate.get("cost")),
                "performance": _validate_performance(aggregate.get("performance")),
                "activity": _validate_activity(aggregate.get("activity")),
            }
        )
    return meta, rows


def _read_observations(home: Path) -> tuple[list[dict], list[dict], list[str]]:
    root = _data_root(home)
    observation_root = root / "observations"
    if not observation_root.is_dir():
        return [], [], []

    rows: list[dict] = []
    observations: list[dict] = []
    errors: list[str] = []
    paths = sorted(observation_root.rglob("*.json"))[:MAX_OBSERVATION_FILES]
    for path in paths:
        try:
            if path.is_symlink() or path.stat().st_size > MAX_OBSERVATION_BYTES:
                raise SnapshotError("unsafe link or oversized observation")
            document = json.loads(path.read_text(encoding="utf-8"))
            meta, normalized_rows = _normalize_snapshot(document)
            observations.append(meta)
            rows.extend(normalized_rows)
        except (OSError, json.JSONDecodeError, SnapshotError) as exc:
            errors.append(f"{path.name}: {exc}")
    return observations, rows, errors


def _latest_rows(rows: Iterable[dict]) -> list[dict]:
    latest: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row["lane"],
            row["view"],
            row["node_id"],
            row["principal_id"],
            row["bucket_start_text"],
            row["client"],
            row["provider"],
            row["model"],
            row["agent"],
            row["workspace_key"],
            row["session_key"],
        )
        prior = latest.get(key)
        if prior is None or row["observed_at"] > prior["observed_at"]:
            latest[key] = row
    return list(latest.values())


def _empty_totals() -> dict[str, Any]:
    return {
        "tokens": {field: 0 for field in TOKEN_FIELDS},
        "message_count": 0,
        "cost_usd": 0.0,
        "cost_state": "unavailable",
        "pricing_revisions": [],
        "cache_ratio": 0.0,
        "duration_ms": 0,
        "timed_tokens": 0,
        "sample_count": 0,
        "token_coverage": 0.0,
        "ms_per_1k_tokens": None,
        "active_seconds": 0,
        "longest_continuous_seconds": 0,
        "max_concurrent": 0,
    }


def _summarize(rows: Iterable[dict]) -> dict[str, Any]:
    rows = list(rows)
    result = _empty_totals()
    cost_flags: list[bool] = []
    revisions: set[str] = set()
    for row in rows:
        for field in TOKEN_FIELDS:
            result["tokens"][field] += row["tokens"][field]
        result["message_count"] += row["message_count"]
        result["duration_ms"] += row["performance"]["duration_ms"]
        result["timed_tokens"] += row["performance"]["timed_tokens"]
        result["sample_count"] += row["performance"]["sample_count"]
        result["active_seconds"] += row["activity"]["active_seconds"]
        result["longest_continuous_seconds"] = max(
            result["longest_continuous_seconds"],
            row["activity"]["longest_continuous_seconds"],
        )
        result["max_concurrent"] = max(
            result["max_concurrent"], row["activity"]["max_concurrent"]
        )
        if row["cost"] is not None:
            result["cost_usd"] += row["cost"]["amount"]
            cost_flags.append(row["cost"]["estimated"])
            revisions.add(row["cost"]["pricing_revision"])

    input_context = result["tokens"]["input"] + result["tokens"]["cache_read"]
    result["cache_ratio"] = (
        result["tokens"]["cache_read"] / input_context if input_context else 0.0
    )
    total = result["tokens"]["total"]
    result["token_coverage"] = min(1.0, result["timed_tokens"] / total) if total else 0.0
    result["ms_per_1k_tokens"] = (
        result["duration_ms"] / result["timed_tokens"] * 1000
        if result["timed_tokens"]
        else None
    )
    result["cost_usd"] = round(result["cost_usd"], 8)
    result["pricing_revisions"] = sorted(revisions)
    if cost_flags:
        if all(cost_flags):
            result["cost_state"] = "estimated"
        elif not any(cost_flags):
            result["cost_state"] = "billed"
        else:
            result["cost_state"] = "mixed"
    return result


def _breakdown(rows: Iterable[dict], dimension: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        value = row.get(dimension) or "unknown"
        grouped[value].append(row)
    result = [{dimension: key, **_summarize(value)} for key, value in grouped.items()]
    return sorted(result, key=lambda item: (-item["tokens"]["total"], item[dimension]))


def _private_breakdown(rows: Iterable[dict], dimension: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        if dimension == "workspace":
            value = row["workspace_label"] or (
                f"workspace {row['workspace_key'][:8]}" if row["workspace_key"] else "unknown"
            )
        elif dimension == "session":
            value = f"session {row['session_key'][:8]}" if row["session_key"] else "unknown"
        else:
            value = row["task_label"] or "unknown"
        grouped[value].append(row)
    result = [{dimension: key, **_summarize(value)} for key, value in grouped.items()]
    return sorted(result, key=lambda item: (-item["tokens"]["total"], item[dimension]))


def _series(rows: Iterable[dict], granularity: str) -> list[dict]:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = row["bucket_start_text"][:13] if granularity == "hour" else row["bucket_start_text"][:10]
        grouped[key].append(row)
    return [
        {"bucket": key, **_summarize(grouped[key])}
        for key in sorted(grouped)
    ]


def _collector_status(age_seconds: int) -> str:
    if age_seconds <= FRESH_SECONDS:
        return "fresh"
    if age_seconds <= DELAYED_SECONDS:
        return "delayed"
    return "stale"


def _collectors(observations: Iterable[dict], lane: str, now: datetime) -> list[dict]:
    latest: dict[tuple[str, str], dict] = {}
    for observation in observations:
        if observation["lane"] != lane:
            continue
        key = (observation["node_id"], observation["principal_id"])
        if key not in latest or observation["observed_at"] > latest[key]["observed_at"]:
            latest[key] = observation

    result = []
    for observation in latest.values():
        age = max(0, int((now - observation["observed_at"]).total_seconds()))
        result.append(
            {
                "node_id": observation["node_id"],
                "principal_id": observation["principal_id"],
                "facade_version": observation["facade_version"],
                "backend": observation["backend"],
                "backend_version": observation["backend_version"],
                "last_seen": observation["observed_at_text"],
                "age_seconds": age,
                "status": _collector_status(age),
            }
        )
    rank = {"fresh": 0, "delayed": 1, "stale": 2}
    return sorted(
        result,
        key=lambda item: (rank[item["status"]], item["node_id"], item["principal_id"]),
    )


def _apply_filters(rows: Iterable[dict], filters: dict[str, str]) -> list[dict]:
    result = []
    start = filters.get("from", "")
    end = filters.get("to", "")
    for row in rows:
        if filters.get("node") and row["node_id"] != filters["node"]:
            continue
        if filters.get("client") and row["client"] != filters["client"]:
            continue
        if filters.get("provider") and row["provider"] != filters["provider"]:
            continue
        if filters.get("model") and row["model"] != filters["model"]:
            continue
        day = row["bucket_start_text"][:10]
        if start and day < start:
            continue
        if end and day > end:
            continue
        result.append(row)
    return result


def _expected_nodes() -> list[str]:
    return sorted(
        {
            value.strip()
            for value in os.environ.get("SKCOUNTER_EXPECTED_NODES", "").split(",")
            if value.strip()
        }
    )


def get_ai_usage(
    home: Path,
    filters: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
) -> dict:
    """Project append-only SKCounter observations into a dashboard read model."""

    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    filters = {key: str(value) for key, value in (filters or {}).items() if value}
    observations, rows, errors = _read_observations(home)
    rows = _latest_rows(rows)
    available_lanes = sorted({row["lane"] for row in rows})
    requested_lane = filters.get("lane", "harness_reported")
    lane = requested_lane if requested_lane in LANES else "harness_reported"
    lane_rows = [row for row in rows if row["lane"] == lane]
    model_rows_all = [row for row in lane_rows if row["view"] == "models"]
    facets = {
        "nodes": sorted({row["node_id"] for row in model_rows_all}),
        "clients": sorted({row["client"] for row in model_rows_all if row["client"]}),
        "providers": sorted({row["provider"] for row in model_rows_all if row["provider"]}),
        "models": sorted({row["model"] for row in model_rows_all if row["model"]}),
    }

    filtered = _apply_filters(lane_rows, filters)
    models = [row for row in filtered if row["view"] == "models"]
    daily = [row for row in filtered if row["view"] == "daily"]
    hourly = [row for row in filtered if row["view"] == "hourly"]
    agents = [row for row in filtered if row["view"] == "agents"]
    workspaces = [row for row in filtered if row["view"] == "workspaces"]
    sessions = [row for row in filtered if row["view"] == "sessions"]
    tasks = [row for row in filtered if row["view"] == "tasks"]
    activity_rows = [row for row in filtered if row["view"] == "time_metrics"]
    summary = _summarize(models)
    activity = _summarize(activity_rows)
    for field in ("active_seconds", "longest_continuous_seconds", "max_concurrent"):
        summary[field] = activity[field]

    collectors = _collectors(observations, lane, now)
    expected_nodes = _expected_nodes()
    reporting_nodes = sorted({item["node_id"] for item in collectors})
    missing_nodes = sorted(set(expected_nodes) - set(reporting_nodes))
    coverage = {
        "expected_nodes": len(expected_nodes),
        "reporting_nodes": len(reporting_nodes),
        "fresh_collectors": sum(item["status"] == "fresh" for item in collectors),
        "delayed_collectors": sum(item["status"] == "delayed" for item in collectors),
        "stale_collectors": sum(item["status"] == "stale" for item in collectors),
        "missing_nodes": missing_nodes,
        "percent": (
            round(len(set(expected_nodes) & set(reporting_nodes)) / len(expected_nodes) * 100, 1)
            if expected_nodes
            else None
        ),
    }

    status = "empty"
    if rows:
        status = "degraded" if errors else "current"
    elif errors:
        status = "degraded"

    return {
        "status": status,
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat().replace("+00:00", "Z"),
        "selected_lane": lane,
        "available_lanes": available_lanes,
        "filters": {
            key: filters.get(key, "")
            for key in ("node", "client", "provider", "model", "from", "to")
        },
        "facets": facets,
        "summary": summary,
        "series": _series(daily or models, "day"),
        "hourly": _series(hourly, "hour"),
        "breakdowns": {
            "models": _breakdown(models, "model"),
            "clients": _breakdown(models, "client"),
            "providers": _breakdown(models, "provider"),
            "nodes": _breakdown(models, "node_id"),
            "agents": _breakdown(agents, "agent"),
            "workspaces": _private_breakdown(workspaces, "workspace"),
            "sessions": _private_breakdown(sessions, "session"),
            "tasks": _private_breakdown(tasks, "task"),
        },
        "collectors": collectors,
        "coverage": coverage,
        "observation_count": len(observations),
        "errors": errors,
    }
