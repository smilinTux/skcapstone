"""Tests for the read-only SKCounter projection in the Economy workspace."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skdashboard.dashboard import create_app
from skdashboard.dashboard_skcounter import get_ai_usage

DIGEST = "a" * 64


def _aggregate(
    *,
    view="models",
    bucket="2026-08-23T00:00:00Z",
    client="codex",
    provider="openai",
    model="gpt-5.6-sol",
    total=100,
    cost=1.25,
):
    return {
        "view": view,
        "bucket_start": bucket,
        "client": client,
        "provider": provider,
        "model": model,
        "tokens": {
            "input": total // 10,
            "output": total // 10,
            "cache_read": total - 2 * (total // 10),
            "cache_write": 0,
            "reasoning": total // 20,
            "total": total,
        },
        "message_count": 4,
        "cost": {
            "amount": cost,
            "currency": "USD",
            "estimated": True,
            "pricing_revision": "fixture-pricing-v1",
        },
        "performance": {
            "duration_ms": 500,
            "timed_tokens": total,
            "sample_count": 4,
            "token_coverage": 1.0,
            "ms_per_1k_tokens": 5000.0,
        },
    }


def _snapshot(
    *,
    lane="harness_reported",
    node="chiap08",
    principal="jarvis",
    observed="2026-08-23T12:00:00Z",
    aggregates=None,
):
    return {
        "schema_version": "skcounter.snapshot.v1",
        "idempotency_key": DIGEST,
        "measurement_lane": lane,
        "node_id": node,
        "principal_id": principal,
        "collector": {
            "product": "skcounter",
            "facade_version": "0.1.0",
            "backend": "tokscale",
            "backend_version": "4.13.0",
        },
        "observed_at": observed,
        "bucket_timezone": "America/Chicago",
        "window": {
            "start": "2026-08-23T00:00:00Z",
            "end": "2026-08-24T00:00:00Z",
        },
        "source_state_digest": DIGEST,
        "aggregates": aggregates or [_aggregate()],
        "payload_hash": DIGEST,
    }


@pytest.fixture
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "skcounter"
    (root / "observations").mkdir(parents=True)
    monkeypatch.setenv("SKCOUNTER_DATA_DIR", str(root))
    return root


def _write(root: Path, name: str, document: dict):
    (root / "observations" / name).write_text(json.dumps(document), encoding="utf-8")


def test_empty_projection_is_well_formed(data_root, tmp_path):
    result = get_ai_usage(tmp_path)

    assert result["status"] == "empty"
    assert result["summary"]["tokens"]["total"] == 0
    assert result["series"] == []
    assert result["collectors"] == []
    assert result["errors"] == []


def test_lanes_remain_separate_and_latest_observation_wins(data_root, tmp_path):
    _write(
        data_root,
        "harness-old.json",
        _snapshot(observed="2026-08-23T11:00:00Z", aggregates=[_aggregate(total=100)]),
    )
    _write(
        data_root,
        "harness-new.json",
        _snapshot(observed="2026-08-23T12:00:00Z", aggregates=[_aggregate(total=250)]),
    )
    _write(
        data_root,
        "gateway.json",
        _snapshot(
            lane="gateway_observed",
            principal="skgateway",
            aggregates=[_aggregate(total=900, provider="skgateway")],
        ),
    )

    harness = get_ai_usage(
        tmp_path,
        now=datetime(2026, 8, 23, 12, 10, tzinfo=timezone.utc),
    )
    gateway = get_ai_usage(
        tmp_path,
        {"lane": "gateway_observed"},
        now=datetime(2026, 8, 23, 12, 10, tzinfo=timezone.utc),
    )

    assert harness["summary"]["tokens"]["total"] == 250
    assert gateway["summary"]["tokens"]["total"] == 900
    assert harness["available_lanes"] == ["gateway_observed", "harness_reported"]
    assert harness["collectors"][0]["status"] == "fresh"


def test_daily_series_breakdowns_filters_and_activity(data_root, tmp_path):
    rows = [
        _aggregate(total=300),
        _aggregate(total=200, client="pi", provider="skgateway", model="sk-codex"),
        _aggregate(view="daily", total=500),
        {
            **_aggregate(view="time_metrics", total=0, cost=0),
            "activity": {
                "active_seconds": 3600,
                "longest_continuous_seconds": 900,
                "max_concurrent": 3,
            },
        },
    ]
    _write(data_root, "usage.json", _snapshot(aggregates=rows))

    all_usage = get_ai_usage(tmp_path)
    codex_only = get_ai_usage(tmp_path, {"client": "codex"})

    assert all_usage["summary"]["tokens"]["total"] == 500
    assert all_usage["summary"]["active_seconds"] == 3600
    assert all_usage["series"][0]["tokens"]["total"] == 500
    assert [row["model"] for row in all_usage["breakdowns"]["models"]] == [
        "gpt-5.6-sol",
        "sk-codex",
    ]
    assert codex_only["summary"]["tokens"]["total"] == 300
    assert set(all_usage["facets"]["clients"]) == {"codex", "pi"}


def test_malformed_raw_data_is_rejected_without_breaking_valid_projection(data_root, tmp_path):
    _write(data_root, "valid.json", _snapshot())
    invalid = _snapshot()
    invalid["aggregates"][0]["prompt"] = "do not display this"
    _write(data_root, "invalid.json", invalid)

    result = get_ai_usage(tmp_path)

    assert result["status"] == "degraded"
    assert result["summary"]["tokens"]["total"] == 100
    assert len(result["errors"]) == 1
    assert "prohibited raw-data field" in result["errors"][0]
    assert "do not display" not in result["errors"][0]


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda snapshot: snapshot.update({"bucket_timezone": ""}), "bucket_timezone"),
        (
            lambda snapshot: snapshot.update(
                {"window": {"start": "2026-08-24T00:00:00Z", "end": "2026-08-23T00:00:00Z"}}
            ),
            "window.start cannot be after window.end",
        ),
        (lambda snapshot: snapshot.update({"unexpected": "raw"}), "unsupported fields"),
    ],
)
def test_invalid_envelope_fields_fail_closed(data_root, tmp_path, mutation, message):
    invalid = _snapshot()
    mutation(invalid)
    _write(data_root, "invalid-envelope.json", invalid)

    result = get_ai_usage(tmp_path)

    assert result["status"] == "degraded"
    assert result["summary"]["tokens"]["total"] == 0
    assert message in result["errors"][0]


def test_stale_and_missing_coverage_are_visible(data_root, tmp_path, monkeypatch):
    _write(data_root, "old.json", _snapshot(observed="2026-08-20T12:00:00Z"))
    monkeypatch.setenv("SKCOUNTER_EXPECTED_NODES", "chiap01,chiap04,chiap08")

    result = get_ai_usage(
        tmp_path,
        now=datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
    )

    assert result["collectors"][0]["status"] == "stale"
    assert result["coverage"]["expected_nodes"] == 3
    assert result["coverage"]["reporting_nodes"] == 1
    assert result["coverage"]["missing_nodes"] == ["chiap01", "chiap04"]
    assert result["coverage"]["percent"] == pytest.approx(33.3)


def test_gateway_coverage_uses_its_own_eligible_node_inventory(
    data_root, tmp_path, monkeypatch
):
    _write(
        data_root,
        "gateway.json",
        _snapshot(lane="gateway_observed", node="chiap01", principal="skgateway"),
    )
    monkeypatch.setenv("SKCOUNTER_EXPECTED_NODES", "chiap01,chiap04,chiap08")
    monkeypatch.setenv("SKCOUNTER_EXPECTED_GATEWAY_NODES", "chiap01")

    result = get_ai_usage(
        tmp_path,
        {"lane": "gateway_observed"},
        now=datetime(2026, 8, 23, 12, 10, tzinfo=timezone.utc),
    )

    assert result["coverage"] == {
        "expected_nodes": 1,
        "reporting_nodes": 1,
        "fresh_collectors": 1,
        "delayed_collectors": 0,
        "stale_collectors": 0,
        "missing_nodes": [],
        "percent": 100.0,
    }


def test_collectors_are_ordered_by_freshness_then_identity(data_root, tmp_path):
    _write(data_root, "stale.json", _snapshot(node="chiap01", observed="2026-08-20T12:00:00Z"))
    _write(data_root, "delayed.json", _snapshot(node="chiap04", observed="2026-08-23T00:00:00Z"))
    _write(data_root, "fresh.json", _snapshot(node="chiap08", observed="2026-08-23T11:50:00Z"))

    result = get_ai_usage(
        tmp_path,
        now=datetime(2026, 8, 23, 12, 0, tzinfo=timezone.utc),
    )

    assert [(row["node_id"], row["status"]) for row in result["collectors"]] == [
        ("chiap08", "fresh"),
        ("chiap04", "delayed"),
        ("chiap01", "stale"),
    ]


class _FakeRequest:
    headers = {}
    path_params = {}

    def __init__(self, query_params=None):
        self.query_params = query_params or {}


def _route_endpoint(app, path):
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"no route registered for path {path!r}")


def test_economy_route_includes_ai_usage_and_accepts_filters(data_root, tmp_path):
    _write(data_root, "usage.json", _snapshot())
    app = create_app(tmp_path)
    handler = _route_endpoint(app, "/api/economy")

    response = asyncio.run(handler(_FakeRequest({"client": "codex"})))
    document = json.loads(response.body)

    assert response.status_code == 200
    assert document["ai_usage"]["summary"]["tokens"]["total"] == 100
    assert document["ai_usage"]["filters"]["client"] == "codex"
    assert any(getattr(route, "path", None) == "/economy" for route in app.routes)


def test_economy_page_separates_usage_autopilot_and_joule():
    page = (
        Path(__file__).parents[1]
        / "src"
        / "skdashboard"
        / "static"
        / "economy.html"
    ).read_text(encoding="utf-8")

    assert 'id="eco-ai-usage"' in page
    assert 'id="eco-autopilot"' in page
    assert 'id="eco-joule"' in page
    assert "One measurement lane at a time" in page
    assert "No implicit token or USD conversion" in page
    assert "Provider quota connectors are not configured" in page
