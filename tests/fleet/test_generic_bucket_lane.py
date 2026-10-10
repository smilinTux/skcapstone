"""Generic skgateway buckets route as one gateway lane; the gateway picks members."""

from __future__ import annotations

import datetime
import io
import json

import pytest

from skcapstone.fleet.production_dispatch import enabled_family_routes, production_lanes
from skcapstone.fleet.production_review import provider_family, review_family_allowed
from skcapstone.fleet.production_routes import served_backend_matches
from skcapstone.fleet.review_capacity import acquire_review_route_snapshot


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


def _documents(now: float):
    bucket = {
        "id": "sk-m",
        "kind": "bucket",
        "provider": "skgateway",
        "owned_by": "skgateway",
        "advertised": True,
        "stale": False,
        "tools": True,
        "members": ["glm-5", "deepseek-flash"],
        "member_backends": ["zai", "deepseek"],
        "card": {"size_class": "M", "reasoning": True, "tier": "paid-cloud"},
    }
    health = {
        "status": "ok",
        "backends": {
            "sk-m": {
                "status": "up",
                "observed": True,
                "lastCheck": now * 1000,
                "quarantined": False,
                "capacity": {"current": True, "state": "available"},
            }
        },
    }
    queue = {
        "timestamp": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat(),
        "pool": {},
        "backends": {
            "sk-m": {
                "capacityDomain": "sk-m",
                "members": ["zai", "deepseek"],
                "max": 30,
                "active": 2,
            }
        },
    }
    return {"data": [bucket]}, health, queue


def test_generic_bucket_snapshot_row_uses_bucket_domain_and_members(tmp_path):
    now = 2_000_000_000.0
    documents = dict(zip(("/v1/models", "/health", "/queue"), _documents(now)))

    def opener(url, timeout):
        suffix = next(key for key in documents if url.endswith(key))
        return _Response(json.dumps(documents[suffix]).encode())

    snapshot = acquire_review_route_snapshot(
        "https://gateway", tmp_path / "snapshot.json", "cycle-1", opener=opener, now=lambda: now
    )
    (row,) = [r for r in snapshot["routes"] if r["logical_route"] == "sk-m"]
    assert row["capacity_domain"] == "sk-m"
    assert row["provider"] == "skgateway"
    assert row["bucket_members"] == ["deepseek", "zai"]
    assert row["max"] == 30


def test_gateway_family_and_lane():
    assert provider_family("skgateway") == provider_family("sk-m") == "gateway"
    policy = {
        "lanes": {
            "codex": {"enabled": False, "provider": "skgateway"},
            "glm": {"enabled": False, "provider": "skgateway"},
            "deepseek": {"enabled": False, "provider": "skgateway"},
            "qwen": {"enabled": False, "provider": "skgateway"},
            "kimi": {"enabled": False},
            "gateway": {"enabled": True, "provider": "skgateway"},
        }
    }
    lane = next(lane for lane in production_lanes(policy, 4) if lane["name"] == "gateway")
    assert lane["family"] == "gateway" and "sk-m" in lane["capacity_domains"]
    routes = [
        {
            "provider": "skgateway",
            "capacity_domain": "sk-m",
            "logical_route": "sk-m",
            "model_or_bucket": "sk-m",
        }
    ]
    assert enabled_family_routes(routes, policy=policy) == routes


@pytest.mark.parametrize(
    "served,expected,members,ok",
    [
        ("zai", "sk-m", ["zai", "deepseek"], True),
        ("deepseek", "skgateway", ["zai", "deepseek"], True),
        ("codex", "sk-m", ["zai", "deepseek"], False),
        ("zai", "skgateway", None, True),
        ("zai", "zai", None, True),
        ("deepseek", "zai", None, False),
        ("", "sk-m", None, False),
    ],
)
def test_served_backend_membership(served, expected, members, ok):
    assert served_backend_matches(served, expected, members) is ok


def test_gateway_reviews_are_allowed():
    assert review_family_allowed("gateway", "gateway", ["review"]) is True
