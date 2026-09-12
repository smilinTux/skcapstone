from __future__ import annotations

import datetime
import io
import json

import pytest

from skcapstone.fleet.review_capacity import (
    acquire_review_route_snapshot,
    aggregate_review_capacity,
    choose_review_route,
    eligible_gateway_routes,
    eligible_review_routes,
    load_route_occupancy,
)


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _documents(now: float):
    models = {
        "data": [
            {
                "id": "route-local-large",
                "provider": "local-a",
                "advertised": True,
                "stale": False,
                "tools": True,
                "card": {"size_class": "L", "reasoning": True, "tier": "local"},
            },
            {
                "id": "route-cloud-medium",
                "provider": "cloud-b",
                "advertised": True,
                "stale": False,
                "tools": True,
                "card": {
                    "size_class": "M",
                    "reasoning": True,
                    "tier": "paid-cloud",
                },
            },
            {
                "id": "route-cloud-alias",
                "provider": "cloud-b",
                "advertised": True,
                "stale": False,
                "tools": True,
                "card": {
                    "size_class": "M",
                    "reasoning": True,
                    "tier": "paid-cloud",
                },
            },
            {
                "id": "route-unhealthy-xl",
                "provider": "cloud-c",
                "advertised": True,
                "stale": False,
                "tools": True,
                "card": {
                    "size_class": "XL",
                    "reasoning": True,
                    "tier": "paid-cloud",
                },
            },
        ]
    }
    health = {
        "status": "ok",
        "backends": {
            name: {
                "status": "up" if name != "cloud-c" else "unknown",
                "observed": name != "cloud-c",
                "lastCheck": now * 1000,
                "quarantined": False,
            }
            for name in ("local-a", "cloud-b", "cloud-c")
        },
    }
    queue = {
        "timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        "pool": {},
        "backends": {
            "local-a": {
                "capacityDomain": "local-a",
                "members": ["local-a"],
                "max": 1,
                "active": 0,
            },
            "cloud-b": {
                "capacityDomain": "cloud-b",
                "members": ["cloud-b"],
                "max": 2,
                "active": 0,
            },
            "cloud-c": {
                "capacityDomain": "cloud-c",
                "members": ["cloud-c"],
                "max": 4,
                "active": 0,
            },
        },
    }
    return models, health, queue


def test_provider_neutral_routes_tier_policy_and_shared_capacity(tmp_path):
    now = 2_000_000_000.0
    documents = dict(zip(("/v1/models", "/health", "/queue"), _documents(now)))

    def opener(url, timeout):
        assert timeout == 8
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener, now=lambda: now
    )
    routes = eligible_review_routes(
        snapshot, "M", [], "producer", "pi-seraph-review", {"cloud-b": 1}
    )
    assert [row["logical_route"] for row in routes] == [
        "route-cloud-alias",
        "route-cloud-medium",
        "route-local-large",
    ]
    assert aggregate_review_capacity(routes, 8) == 2
    first = choose_review_route(routes, {})
    second = choose_review_route(routes, {first["capacity_domain"]: 1})
    assert first["capacity_domain"] == "cloud-b"
    assert second["capacity_domain"] == "local-a"
    assert (
        eligible_review_routes(snapshot, "M", ["local-only"], "producer", "pi-seraph-review", {})[
            0
        ]["logical_route"]
        == "route-local-large"
    )
    assert eligible_review_routes(snapshot, "XL", [], "producer", "reviewer", {}) == []
    assert eligible_review_routes(snapshot, "M", [], "same", "same", {}) == []


def test_size_s_consumes_healthy_equal_or_larger_buckets_only(tmp_path):
    """S work must use healthy L/XL free domains when no healthy S route exists."""
    now = 2_000_000_000.0
    models, health, queue = _documents(now)
    models["data"] = [
        row
        for row in models["data"]
        if isinstance(row, dict) and (row.get("card") or {}).get("size_class") in {"L", "XL"}
    ]
    documents = dict(zip(("/v1/models", "/health", "/queue"), (models, health, queue)))

    def opener(url, timeout):
        assert timeout == 8
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener, now=lambda: now
    )
    assert {row["size_class"] for row in snapshot["routes"]} <= {"L", "XL"}
    routes = eligible_review_routes(snapshot, "S", [], "producer", "pi-seraph-review", {})
    assert [row["logical_route"] for row in routes] == ["route-local-large"]
    assert aggregate_review_capacity(routes, 8) == 1
    assert choose_review_route(routes, {})["capacity_domain"] == "local-a"
    exact = [row for row in routes if row["size_class"] == "S"]
    assert exact == []


def test_ambiguous_untyped_seat_keeps_compatible_free_capacity(tmp_path):
    """Ambiguous live records must not project target=0 over healthy larger buckets."""
    now = 2_000_000_000.0
    models, health, queue = _documents(now)
    models["data"] = [
        row
        for row in models["data"]
        if isinstance(row, dict) and (row.get("card") or {}).get("size_class") in {"L", "XL"}
    ]
    documents = dict(zip(("/v1/models", "/health", "/queue"), (models, health, queue)))

    def opener(url, timeout):
        del timeout
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener, now=lambda: now
    )
    directory = tmp_path / "fleet" / "direct-seats"
    directory.mkdir(parents=True)
    stamp = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()
    (directory / "untyped.json").write_text(
        json.dumps({"completion_state": "running", "heartbeat_at": stamp})
    )
    (tmp_path / "evidence").mkdir(parents=True)
    (tmp_path / "evidence" / "fleet-review-routes.json").write_text(
        json.dumps(snapshot) + "\n", encoding="utf-8"
    )
    occupancy, ambiguous = load_route_occupancy(tmp_path, now=now)
    assert occupancy == {}
    assert ambiguous is True
    from skcapstone.review_admission import reviewer_capacity

    busy, target = reviewer_capacity(
        tmp_path,
        {"title": "[S] review larger buckets"},
        ["review", "seat-seraph"],
        "producer",
        "pi-seraph-review",
    )
    assert busy == 0
    assert target == 1


@pytest.mark.parametrize(
    ("current", "capacity_state", "eligible"),
    [
        (True, "available", True),
        (True, "throttled", False),
        (True, "unavailable", False),
        (False, "unavailable", True),
        (True, "unknown", True),
        (None, "throttled", True),
    ],
)
def test_only_current_unavailable_capacity_excludes_route(
    tmp_path, current, capacity_state, eligible
):
    now = 2_000_000_000.0
    models, health, queue = _documents(now)
    health["backends"]["cloud-b"]["capacity"] = {
        "current": current,
        "state": capacity_state,
    }
    documents = dict(zip(("/v1/models", "/health", "/queue"), (models, health, queue)))

    def opener(url, timeout):
        assert timeout == 8
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener, now=lambda: now
    )
    routes = eligible_review_routes(snapshot, "M", [], "producer", "reviewer", {})
    cloud_routes = [row for row in routes if row["capacity_domain"] == "cloud-b"]
    assert bool(cloud_routes) is eligible
    if eligible:
        assert aggregate_review_capacity(cloud_routes, 8) == 2
        assert {row["logical_route"] for row in cloud_routes} == {
            "route-cloud-alias",
            "route-cloud-medium",
        }


def test_producer_capacity_uses_gateway_truth_not_unrelated_pi_sessions(tmp_path):
    now = 2_000_000_000.0
    models, health, queue = _documents(now)
    queue["backends"]["cloud-b"]["max"] = 32
    documents = dict(zip(("/v1/models", "/health", "/queue"), (models, health, queue)))

    def opener(url, timeout):
        assert timeout == 8
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener, now=lambda: now
    )
    routes = eligible_gateway_routes(snapshot, "M", [], {})

    assert aggregate_review_capacity(routes, 3) == 3
    assert {row["logical_route"] for row in routes} == {
        "route-cloud-alias",
        "route-cloud-medium",
        "route-local-large",
    }
    assert sum(row["free"] for row in routes if row["capacity_domain"] == "cloud-b") == 64
    assert choose_review_route(routes, {"cloud-b": 31})["capacity_domain"] == "cloud-b"


def test_runtime_route_occupancy_is_typed_and_ambiguous_fails_closed(tmp_path):
    directory = tmp_path / "fleet" / "direct-seats"
    directory.mkdir(parents=True)
    current = 2_000_000_000.0
    stamp = datetime.datetime.fromtimestamp(current, datetime.timezone.utc).isoformat()
    (directory / "typed.json").write_text(
        json.dumps(
            {
                "completion_state": "running",
                "heartbeat_at": stamp,
                "route_schema": "skfleet.runtime-route/v1",
                "capacity_domains": ["cloud-b"],
            }
        )
    )
    (directory / "old.json").write_text(
        json.dumps({"completion_state": "running", "heartbeat_at": stamp})
    )
    occupancy, ambiguous = load_route_occupancy(tmp_path, now=current)
    assert occupancy == {"cloud-b": 1}
    assert ambiguous is True


def test_snapshot_fetch_failure_has_no_routes(tmp_path):
    def opener(_url, timeout):
        del timeout
        raise OSError("offline")

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener
    )
    assert snapshot["routes"] == []
    assert snapshot["error"] == "OSError"
    assert eligible_review_routes(snapshot, "S", [], "producer", "reviewer", {}) == []
