"""Provider-neutral SKGateway route capacity for independent reviews."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from skcapstone.review_admission import LOGICAL_REVIEWER_SEATS, reviewer_candidate_reasons
from skcapstone.seat_boundaries import canonical_principal

from ..fleet_lane_health import MAX_AGE_SECONDS, _domain_state, gateway_root

_SIZE = {"S": 0, "M": 1, "L": 2, "XL": 3}
_LOCAL_POLICY = {"local-only", "no-egress", "sovereign-only"}

# Three probes at this bound stay well inside the 190s Seraph dispatch budget.
_DEFAULT_PROBE_TIMEOUT = 20


def _probe_timeout_seconds() -> int:
    """Return the per-probe gateway timeout, overridable for slow gateways.

    The old hardcoded 8s sat below the gateway own worst case. On 2026-09-20
    /v1/models on chiap01:18790 measured 6.36s while codex carried 5 active
    requests (peak 15), so any load spike pushed the probe past 8s.

    A probe failure is NOT recorded as "unknown": it writes routes=[], and
    aggregate_review_capacity turns an empty route list into zero capacity, so
    every Seraph cycle then reports seraph_no_available_capacity and the review
    pipeline stops dispatching altogether. A timed-out health probe must never
    be able to masquerade as a fully occupied fleet.
    """
    raw = os.environ.get("SKFLEET_ROUTE_PROBE_TIMEOUT", "").strip()
    if raw:
        try:
            parsed = int(raw)
        except ValueError:
            return _DEFAULT_PROBE_TIMEOUT
        if parsed > 0:
            return parsed
    return _DEFAULT_PROBE_TIMEOUT


def _fetch(url: str, opener: Callable[..., Any]) -> dict[str, Any]:
    with opener(url, timeout=_probe_timeout_seconds()) as response:
        payload = response.read(1_048_577)
    if len(payload) > 1_048_576:
        raise ValueError("gateway route document exceeds bound")
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ValueError("gateway route document is not an object")
    return value


def _capacity_domain(queue: Mapping[str, Any], provider: str) -> str | None:
    matches = []
    for name, row in queue.items():
        if not isinstance(row, dict):
            continue
        members = row.get("members")
        if name == provider or (isinstance(members, list) and provider in members):
            if row.get("capacityDomain") == name:
                matches.append(name)
    return matches[0] if len(matches) == 1 else None


def acquire_review_route_snapshot(
    base_url: str,
    path: Path,
    cycle_id: str,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    now: Callable[[], float] = time.time,
    occupancy: Mapping[str, int] | None = None,
    occupancy_ambiguous: bool = False,
    physical_maximum: int | None = None,
) -> dict[str, Any]:
    """Seal one bounded model, health, and queue view for Seraph selection."""
    # Normalize to the gateway ORIGIN. This function appends "/v1" itself for
    # the models probe while /health and /queue are root-relative, so a base
    # URL that already carries "/v1" would request /v1/v1/models, /v1/health
    # and /v1/queue: all three 404, routes comes back empty, and
    # aggregate_review_capacity reports zero codex capacity. That is a total
    # review outage from a config value that looks right, and it is the same
    # trap that cost this fleet three days of zero dispatch through
    # fleet_lane_health on 2026-09-18.
    endpoint = gateway_root(base_url)
    observed_at = now()
    try:
        models_doc = _fetch(endpoint + "/v1/models", opener)
        health_doc = _fetch(endpoint + "/health", opener)
        queue_doc = _fetch(endpoint + "/queue", opener)
        models = models_doc.get("data")
        health = health_doc.get("backends")
        queue = queue_doc.get("backends")
        stamp = datetime.datetime.fromisoformat(
            str(queue_doc["timestamp"]).replace("Z", "+00:00")
        ).timestamp()
        if (
            not isinstance(models, list)
            or not isinstance(health, dict)
            or not isinstance(queue, dict)
            or health_doc.get("status") != "ok"
            or abs(observed_at - stamp) > MAX_AGE_SECONDS
        ):
            raise ValueError("gateway route snapshot schema or freshness")
    except Exception as exc:
        snapshot = {
            "schema_version": 1,
            "cycle_id": cycle_id,
            "observed_at": observed_at,
            "endpoint": endpoint,
            "routes": [],
            "error": type(exc).__name__,
        }
    else:
        routes = []
        for model in models:
            if not isinstance(model, dict):
                continue
            card = model.get("card")
            route = str(model.get("id") or "")
            provider = str(model.get("provider") or model.get("owned_by") or "")
            size = str(card.get("size_class") or "") if isinstance(card, dict) else ""
            domain = _capacity_domain(queue, provider)
            if (
                not route
                or not provider
                or size not in _SIZE
                or domain is None
                or model.get("advertised") is not True
                or model.get("stale") is not False
                or model.get("tools") is not True
                or not isinstance(card, dict)
                or card.get("reasoning") is not True
            ):
                continue
            state = _domain_state(health, queue, domain, observed_at)
            health_row = health.get(domain, {})
            capacity = health_row.get("capacity") if isinstance(health_row, dict) else None
            if (
                isinstance(capacity, dict)
                and capacity.get("current") is True
                and capacity.get("state") in {"throttled", "unavailable"}
            ):
                state = {**state, "state": "owner-down"}
            queue_row = queue[domain]
            routes.append(
                {
                    "logical_route": route,
                    "model_or_bucket": route,
                    "provider": provider,
                    "capacity_domain": domain,
                    "size_class": size,
                    "policy_tier": str(card.get("tier") or ""),
                    "state": state.get("state"),
                    "max": int(queue_row["max"]),
                    "gateway_active": int(queue_row.get("active", 0)),
                }
            )
        snapshot = {
            "schema_version": 1,
            "cycle_id": cycle_id,
            "observed_at": observed_at,
            "endpoint": endpoint,
            "routes": sorted(routes, key=lambda row: row["logical_route"]),
            "error": None,
        }
    snapshot = seal_review_capacity_truth(
        snapshot,
        occupancy or {},
        occupancy_ambiguous=occupancy_ambiguous,
        physical_maximum=physical_maximum,
    )
    encoded = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return snapshot


def seal_review_capacity_truth(
    snapshot: Mapping[str, Any],
    occupancy: Mapping[str, int],
    *,
    occupancy_ambiguous: bool = False,
    physical_maximum: int | None = None,
) -> dict[str, Any]:
    """Bind route and occupancy observations into one revisioned truth."""
    sealed = {key: value for key, value in snapshot.items() if key != "capacity_revision"}
    sealed["occupancy"] = {
        str(domain): int(count)
        for domain, count in sorted(occupancy.items())
        if str(domain) and int(count) >= 0
    }
    sealed["occupancy_ambiguous"] = bool(occupancy_ambiguous)
    if physical_maximum is not None:
        sealed["physical_maximum"] = max(0, int(physical_maximum))
    material = json.dumps(sealed, sort_keys=True, separators=(",", ":")).encode()
    sealed["capacity_revision"] = hashlib.sha256(material).hexdigest()
    return sealed


def _review_capacity_truth_is_current(snapshot: Mapping[str, Any]) -> bool:
    """Return whether the sealed capacity revision matches its exact facts."""
    revision = str(snapshot.get("capacity_revision") or "")
    if len(revision) != 64:
        return False
    material = {key: value for key, value in snapshot.items() if key != "capacity_revision"}
    actual = hashlib.sha256(
        json.dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return revision == actual


def load_route_occupancy(home: Path, *, now: float | None = None) -> tuple[dict[str, int], bool]:
    """Read fresh typed runtime route identities; flag ambiguous live records."""
    current = time.time() if now is None else now
    occupancy: dict[str, int] = {}
    ambiguous = False
    directory = home / "fleet" / "direct-seats"
    for path in sorted(directory.glob("*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
            observed = datetime.datetime.fromisoformat(
                str(row["heartbeat_at"]).replace("Z", "+00:00")
            ).timestamp()
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            continue
        if row.get("completion_state") != "running" or not 0 <= current - observed <= 120:
            continue
        domains = row.get("capacity_domains")
        if (
            row.get("route_schema") != "skfleet.runtime-route/v1"
            or not isinstance(domains, list)
            or not domains
            or any(not isinstance(value, str) or not value for value in domains)
        ):
            ambiguous = True
            continue
        for domain in set(domains):
            occupancy[domain] = occupancy.get(domain, 0) + 1
    return occupancy, ambiguous


def eligible_gateway_routes(
    snapshot: Mapping[str, Any],
    required_size: str,
    labels: Sequence[str],
    occupancy: Mapping[str, int],
) -> list[dict[str, Any]]:
    """Return healthy policy-compatible routes with per-domain free capacity."""
    if (
        required_size not in _SIZE
        or snapshot.get("schema_version") != 1
        or snapshot.get("error") is not None
    ):
        return []
    normalized = {str(label).strip().lower() for label in labels}
    routes = []
    for raw in snapshot.get("routes", []):
        if not isinstance(raw, dict) or raw.get("state") != "healthy":
            continue
        size = str(raw.get("size_class") or "")
        if size not in _SIZE or _SIZE[size] < _SIZE[required_size]:
            continue
        if normalized & _LOCAL_POLICY and raw.get("policy_tier") != "local":
            continue
        domain = str(raw.get("capacity_domain") or "")
        busy = max(int(raw.get("gateway_active", 0)), int(occupancy.get(domain, 0)))
        free = int(raw.get("max", 0)) - busy
        if free > 0:
            routes.append({**raw, "free": free})
    return sorted(
        routes,
        key=lambda row: (_SIZE[str(row["size_class"])], str(row["logical_route"])),
    )


def eligible_review_routes(
    snapshot: Mapping[str, Any],
    required_size: str,
    labels: Sequence[str],
    producer: str,
    reviewer: str,
    occupancy: Mapping[str, int],
    *,
    declared_seat: str | None = None,
    qualified_seats: set[str] | frozenset[str] = LOGICAL_REVIEWER_SEATS,
    available: bool = True,
) -> list[dict[str, Any]]:
    """Return eligible gateway routes after enforcing reviewer independence."""
    if canonical_principal(producer) == canonical_principal(reviewer):
        return []
    if declared_seat is not None and reviewer_candidate_reasons(
        reviewer,
        producer=producer,
        declared_seat=declared_seat,
        qualified_seats=qualified_seats,
        available=available,
    ):
        return []
    return eligible_gateway_routes(snapshot, required_size, labels, occupancy)


def evaluate_review_capacity(
    snapshot: Mapping[str, Any],
    required_size: str,
    labels: Sequence[str],
    producer: str,
    reviewer: str,
    *,
    declared_seat: str | None = None,
    qualified_seats: set[str] | frozenset[str] = LOGICAL_REVIEWER_SEATS,
    physical_free: int | None = None,
) -> dict[str, Any]:
    """Evaluate one reviewer from the exact sealed route-capacity revision."""
    revision = str(snapshot.get("capacity_revision") or "")
    occupancy = snapshot.get("occupancy")
    valid_occupancy = isinstance(occupancy, dict) and all(
        isinstance(domain, str) and domain and type(count) is int and count >= 0
        for domain, count in occupancy.items()
    )
    routes: list[dict[str, Any]] = []
    logical_available = 0
    physical_maximum = snapshot.get("physical_maximum")
    if type(physical_maximum) is not int or physical_maximum < 0:
        physical_maximum = None
    if not _review_capacity_truth_is_current(snapshot) or not valid_occupancy:
        reason = "route-snapshot-ambiguity"
    elif snapshot.get("schema_version") != 1 or snapshot.get("error") is not None:
        reason = "route-snapshot-ambiguity"
    elif snapshot.get("occupancy_ambiguous") is True:
        reason = "occupancy-ambiguity"
    else:
        routes = eligible_review_routes(
            snapshot,
            required_size,
            labels,
            producer,
            reviewer,
            occupancy,
            declared_seat=declared_seat,
            qualified_seats=qualified_seats,
        )
        logical_available = aggregate_review_capacity(
            routes, sum(int(route["free"]) for route in routes)
        )
        if physical_free is None:
            physical_free = (
                logical_available
                if physical_maximum is None
                else max(0, physical_maximum - sum(occupancy.values()))
            )
        else:
            physical_free = max(0, int(physical_free))
        if physical_free <= 0:
            reason = "physical-exhaustion"
        elif routes:
            reason = "eligible"
        elif eligible_gateway_routes(snapshot, required_size, (), occupancy):
            reason = "policy-incompatibility"
        else:
            reason = "route-exhaustion"
    if physical_free is None:
        physical_free = 0
    available = min(logical_available, physical_free) if reason == "eligible" else 0
    return {
        "capacity_revision": revision,
        "reason": reason,
        "routes": routes,
        "occupancy": dict(occupancy) if valid_occupancy else {},
        "physical_maximum": physical_maximum,
        "physical_free": physical_free,
        "logical_available": logical_available,
        "available": available,
    }


def eligible_review_launch_lanes(
    lanes: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    reservations: Mapping[str, int],
    physical_free: int,
    health: Mapping[str, tuple[bool, str]],
) -> list[str]:
    """Return healthy physical lanes backed by free logical capacity domains."""
    if physical_free <= 0:
        return []
    domains = {
        str(route.get("capacity_domain") or "")
        for route in routes
        if int(route.get("free", 0))
        > int(reservations.get(str(route.get("capacity_domain") or ""), 0))
    }
    return [
        str(lane["name"])
        for lane in lanes
        if int(lane.get("free", 0)) > 0
        and health.get(str(lane["name"]), (True, "healthy"))[0]
        and domains.intersection(str(value) for value in lane.get("capacity_domains", ()))
    ]


def review_physical_free(
    lanes: Sequence[Mapping[str, Any]],
    routes: Sequence[Mapping[str, Any]],
    reservations: Mapping[str, int],
    physical_maximum: int,
) -> int:
    """Return remaining physical review slots across matching logical domains."""
    domains = {str(route.get("capacity_domain") or "") for route in routes}
    busy = sum(
        len(lane.get("busy", ()))
        for lane in lanes
        if domains.intersection(str(value) for value in lane.get("capacity_domains", ()))
    )
    reserved = sum(int(reservations.get(domain, 0)) for domain in domains)
    return max(0, int(physical_maximum) - busy - reserved)


def aggregate_review_capacity(routes: Sequence[Mapping[str, Any]], target: int) -> int:
    """Aggregate free capacity once per shared capacity domain."""
    domains: dict[str, int] = {}
    for route in routes:
        domain = str(route.get("capacity_domain") or "")
        domains[domain] = max(domains.get(domain, 0), int(route.get("free", 0)))
    return min(max(0, int(target)), sum(domains.values()))


def choose_review_route(
    routes: Sequence[Mapping[str, Any]], reservations: Mapping[str, int]
) -> dict[str, Any] | None:
    """Choose one route without oversubscribing its shared capacity domain."""
    for route in routes:
        domain = str(route["capacity_domain"])
        if int(route["free"]) > int(reservations.get(domain, 0)):
            return dict(route)
    return None
