from __future__ import annotations

import datetime
import io
import json

import pytest

from skcapstone.fleet import review_capacity
from skcapstone.fleet.review_capacity import (
    acquire_review_route_snapshot,
    aggregate_review_capacity,
    choose_review_route,
    eligible_gateway_routes,
    eligible_review_launch_lanes,
    eligible_review_routes,
    evaluate_review_capacity,
    load_route_occupancy,
    review_physical_free,
    seal_review_capacity_truth,
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
        assert timeout == 20
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
        assert timeout == 20
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
        assert timeout == 20
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


def test_revisioned_capacity_truth_is_the_shared_admission_input(tmp_path):
    now = 2_000_000_000.0
    documents = dict(zip(("/v1/models", "/health", "/queue"), _documents(now)))

    def opener(url, timeout):
        assert timeout == 20
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway",
        tmp_path / "snapshot.json",
        "cycle-1",
        opener=opener,
        now=lambda: now,
        occupancy={"cloud-b": 1},
        physical_maximum=3,
    )
    evaluation = evaluate_review_capacity(
        snapshot,
        "M",
        [],
        "producer",
        "pi-seraph-review",
        declared_seat="seraph",
    )

    assert len(snapshot["capacity_revision"]) == 64
    assert evaluation["capacity_revision"] == snapshot["capacity_revision"]
    assert evaluation["reason"] == "eligible"
    assert evaluation["physical_maximum"] == 3
    assert evaluation["available"] == 2

    tampered = json.loads(json.dumps(snapshot))
    tampered["routes"][0]["max"] = 99
    assert (
        evaluate_review_capacity(
            tampered,
            "M",
            [],
            "producer",
            "pi-seraph-review",
            declared_seat="seraph",
        )["reason"]
        == "route-snapshot-ambiguity"
    )


def test_capacity_diagnostics_keep_distinct_failure_causes(tmp_path):
    now = 2_000_000_000.0
    documents = dict(zip(("/v1/models", "/health", "/queue"), _documents(now)))

    def opener(url, timeout):
        assert timeout == 20
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway",
        tmp_path / "snapshot.json",
        "cycle-1",
        opener=opener,
        now=lambda: now,
    )

    def reason(value, *, labels=(), size="M", physical_free=None):
        return evaluate_review_capacity(
            value,
            size,
            labels,
            "producer",
            "pi-seraph-review",
            declared_seat="seraph",
            physical_free=physical_free,
        )["reason"]

    assert reason({**snapshot, "error": "TimeoutError"}) == "route-snapshot-ambiguity"
    assert (
        reason(seal_review_capacity_truth(snapshot, {}, occupancy_ambiguous=True))
        == "occupancy-ambiguity"
    )
    assert reason(snapshot, labels=("local-only",), physical_free=1) == "eligible"

    policy_snapshot = seal_review_capacity_truth(
        {
            **snapshot,
            "routes": [row for row in snapshot["routes"] if row["policy_tier"] != "local"],
        },
        {},
    )
    assert reason(policy_snapshot, labels=("local-only",), physical_free=1) == (
        "policy-incompatibility"
    )
    assert reason(snapshot, physical_free=0) == "physical-exhaustion"
    assert reason(snapshot, size="XL", physical_free=1) == "route-exhaustion"


def test_review_launch_lanes_follow_healthy_domains_and_physical_maximum():
    routes = [{"capacity_domain": "review-domain", "free": 1}]
    lanes = [
        {
            "name": "ordinary-review",
            "capacity_domains": ["review-domain"],
            "free": 2,
        },
        {
            "name": "strong-review",
            "capacity_domains": ["review-domain"],
            "free": 2,
        },
        {"name": "unrelated", "capacity_domains": ["other"], "free": 9},
    ]
    health = {
        "ordinary-review": (True, "healthy"),
        "strong-review": (True, "healthy"),
        "unrelated": (True, "healthy"),
    }

    assert eligible_review_launch_lanes(lanes, routes, {}, 2, health) == [
        "ordinary-review",
        "strong-review",
    ]
    assert eligible_review_launch_lanes(lanes, routes, {}, 0, health) == []
    assert (
        eligible_review_launch_lanes(
            lanes,
            routes,
            {"review-domain": 1},
            2,
            health,
        )
        == []
    )
    health["ordinary-review"] = (False, "owner-down")
    assert eligible_review_launch_lanes(lanes, routes, {}, 1, health) == ["strong-review"]
    lanes[0]["busy"] = ["existing"]
    lanes[1]["busy"] = []
    assert review_physical_free(lanes, routes, {}, 3) == 2
    assert review_physical_free(lanes, routes, {"review-domain": 1}, 3) == 1


def _strict_opener(documents):
    """Route on the EXACT path a real server would see, and 404 a miss.

    The mocks above resolve with `url.endswith(key)`, which cannot tell
    `/v1/v1/models` from `/v1/models`. That is the same blind spot that let a
    gateway-URL misconfiguration cost this fleet three days of zero dispatch
    through fleet_lane_health: the probe's own test passed because its mock
    only looked at the trailing path segment.
    """
    import urllib.error
    import urllib.parse

    def opener(url, timeout=20):
        path = urllib.parse.urlsplit(url).path.rstrip("/") or "/"
        if path not in documents:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Response(json.dumps(documents[path]).encode())

    return opener


def test_a_v1_suffixed_base_url_still_seals_real_routes(tmp_path):
    """Regression: this function appends "/v1" itself for the models probe
    while /health and /queue are root-relative, so a base URL that already
    carries "/v1" would request /v1/v1/models, /v1/health and /v1/queue.

    All three 404, `routes` comes back empty, and aggregate_review_capacity
    then reports ZERO codex review capacity: a total review outage from a
    config value that looks correct.
    """
    now = 2_000_000_000.0
    documents = dict(zip(("/v1/models", "/health", "/queue"), _documents(now)))
    opener = _strict_opener(documents)

    snapshot = acquire_review_route_snapshot(
        "https://gateway/v1",
        tmp_path / "snap-v1.json",
        "cycle-v1",
        opener=opener,
        now=lambda: now,
    )
    routes = eligible_review_routes(
        snapshot, "M", [], "producer", "pi-seraph-review", {"cloud-b": 1}
    )
    assert routes, "a /v1 base URL must still resolve real routes"
    assert aggregate_review_capacity(routes, 8) == 2


def test_the_origin_form_is_unaffected(tmp_path):
    now = 2_000_000_000.0
    documents = dict(zip(("/v1/models", "/health", "/queue"), _documents(now)))
    snapshot = acquire_review_route_snapshot(
        "https://gateway",
        tmp_path / "snap-root.json",
        "cycle-root",
        opener=_strict_opener(documents),
        now=lambda: now,
    )
    routes = eligible_review_routes(
        snapshot, "M", [], "producer", "pi-seraph-review", {"cloud-b": 1}
    )
    assert aggregate_review_capacity(routes, 8) == 2


def test_the_strict_opener_really_does_404_a_doubled_prefix():
    """Guard the guard, so the regression test cannot pass for the wrong
    reason if the normalization is later removed."""
    import urllib.error

    import pytest as _pytest

    opener = _strict_opener({"/v1/models": {}, "/health": {}, "/queue": {}})
    with _pytest.raises(urllib.error.HTTPError):
        opener("https://gateway/v1/v1/models")
    with _pytest.raises(urllib.error.HTTPError):
        opener("https://gateway/v1/health")


def test_probe_timeout_defaults_to_twenty(monkeypatch) -> None:
    monkeypatch.delenv("SKFLEET_ROUTE_PROBE_TIMEOUT", raising=False)
    assert review_capacity._probe_timeout_seconds() == 20


def test_probe_timeout_env_override_honoured(monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_ROUTE_PROBE_TIMEOUT", "45")
    assert review_capacity._probe_timeout_seconds() == 45


def test_probe_timeout_rejects_garbage_and_non_positive(monkeypatch) -> None:
    for bad in ("", "   ", "abc", "0", "-5"):
        monkeypatch.setenv("SKFLEET_ROUTE_PROBE_TIMEOUT", bad)
        assert review_capacity._probe_timeout_seconds() == 20


def test_fetch_uses_the_resolved_timeout(monkeypatch) -> None:
    """A slow gateway must not be cut off at the old hardcoded 8s."""
    monkeypatch.setenv("SKFLEET_ROUTE_PROBE_TIMEOUT", "30")
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, _n):
            return b'{"ok": true}'

    def _opener(url, timeout):
        seen["timeout"] = timeout
        return _Resp()

    assert review_capacity._fetch("http://gw/v1/models", _opener) == {"ok": True}
    assert seen["timeout"] == 30
