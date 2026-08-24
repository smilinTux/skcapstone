from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from jsonschema import ValidationError

from skdashboard.control_plane_client import (
    ContractValidators,
    ControlPlaneClient,
    ControlPlaneClientError,
)
from skdashboard.control_plane_fixture import (
    BEARER,
    INSIGHT,
    ORIGIN,
    REPORT,
    SCOPE,
    WINDOW,
    create_fixture_app,
)

DISCOVERY = ORIGIN + "/.well-known/skworld-module.json"


async def _client() -> ControlPlaneClient:
    return await ControlPlaneClient.discover(
        DISCOVERY,
        BEARER,
        transport=httpx.ASGITransport(app=create_fixture_app()),
    )


def _insight_query() -> dict:
    return {
        "question": "Summarize the public synthetic portfolio evidence.",
        "scope": SCOPE,
        "window": WINDOW,
        "intent": "brief",
        "metric_families": ["portfolio"],
        "baseline": None,
    }


def test_client_discovers_same_origin_and_never_discloses_bearer() -> None:
    async def run() -> None:
        client = await _client()
        try:
            assert client.origin == ORIGIN
            assert client.manifest["health"] == ORIGIN + "/api/v1/health"
            assert BEARER not in repr(client)
            assert not any(BEARER in json.dumps(value) for value in client.manifest.values())
        finally:
            await client.aclose()

    asyncio.run(run())


def test_discovery_rejects_redirects_cross_origin_and_non_https() -> None:
    manifest = {
        "schemaVersion": "1.1",
        "entry": {"url": "https://other.test/"},
        "auth": {"audience": "skdashboard", "scopes": ["skdashboard.read"]},
        "health": "https://other.test/api/v1/health",
    }

    async def handler(_request):
        return httpx.Response(200, json=manifest)

    async def run() -> None:
        with pytest.raises(ControlPlaneClientError, match="canonical health route"):
            await ControlPlaneClient.discover(
                DISCOVERY, BEARER, transport=httpx.MockTransport(handler)
            )
        with pytest.raises(ControlPlaneClientError, match="canonical HTTPS"):
            await ControlPlaneClient.discover(
                "http://synthetic.test/.well-known/skworld-module.json", BEARER
            )

    asyncio.run(run())


def test_client_reads_validates_etag_and_keeps_cached_data_immutable() -> None:
    async def run() -> None:
        client = await _client()
        try:
            first = await client.health()
            first.data["items"][0]["state"] = "tampered"
            unchanged = await client.health()
            assert unchanged.not_modified is True
            assert unchanged.etag == first.etag
            assert unchanged.data["items"][0]["state"] == "current"

            overview = await client.overview()
            metric = overview.data["metrics"][0]
            assert metric["metric_id"] == "portfolio.synthetic_count"
            assert metric["truth_state"] == "current"
            assert metric["calculation"]["definition_hash"]
            assert metric["data_quality"]["errors"] == []
            assert metric["source"]["evidence_refs"] == ["evidence:synthetic:metric-r1"]
            assert client.evidence_refs(overview.data) == ["evidence:synthetic:metric-r1"]
        finally:
            await client.aclose()

    asyncio.run(run())


def test_ui_api_document_and_typed_client_agree_on_measurement_semantics() -> None:
    async def run() -> None:
        transport = httpx.ASGITransport(app=create_fixture_app())
        async with httpx.AsyncClient(transport=transport) as raw_http:
            raw = await raw_http.get(
                ORIGIN + "/api/v1/overview",
                headers={"Authorization": f"Bearer {BEARER}"},
            )
        client = await _client()
        try:
            typed = await client.overview()
            assert typed.data == raw.json()
            metric = typed.data["metrics"][0]
            assert {
                "value": metric["value"],
                "definition_hash": metric["calculation"]["definition_hash"],
                "scope": metric["scope"],
                "truth_state": metric["truth_state"],
                "freshness": typed.data["freshness"],
                "quality": metric["data_quality"],
                "evidence_refs": metric["source"]["evidence_refs"],
            } == {
                "value": 1,
                "definition_hash": "sha256:" + "a" * 64,
                "scope": SCOPE,
                "truth_state": "current",
                "freshness": raw.json()["freshness"],
                "quality": raw.json()["metrics"][0]["data_quality"],
                "evidence_refs": ["evidence:synthetic:metric-r1"],
            }
        finally:
            await client.aclose()

    asyncio.run(run())


def test_report_insight_saved_scope_and_metric_family_follow_frozen_schemas() -> None:
    async def run() -> None:
        client = await _client()
        try:
            report = await client.report(REPORT["snapshot_id"])
            insight = await client.insight(_insight_query())
            scope = await client.saved_scope({"project_id": "synthetic-estate"})
            metrics = await client.metric_family("portfolio")

            assert report.data == REPORT
            assert insight.data == INSIGHT
            assert insight.data["status"] == "abstained"
            assert scope.data["scope"] == SCOPE
            assert metrics == [scope.data["metrics"][0]]
            assert report.data["sections"][0]["metric_results"][0] == metrics[0]
        finally:
            await client.aclose()

    asyncio.run(run())


def test_pagination_event_resume_and_reset_are_bounded() -> None:
    async def run() -> None:
        client = await _client()
        try:
            pages = [page async for page in client.pages("board", limit="1")]
            assert [page.data["items"][0]["item_id"] for page in pages] == [
                "synthetic-1",
                "synthetic-2",
            ]
            events = await client.events(cursor="djE6MQ", topics=("reports",))
            assert events == [
                {"event": "reset-required", "data": {"reason": "fixture replay unavailable"}}
            ]
            with pytest.raises(ControlPlaneClientError, match="topics"):
                await client.events(topics=tuple(str(index) for index in range(17)))
            with pytest.raises(ControlPlaneClientError, match="pagination"):
                async for _page in client.pages("overview"):
                    pass
        finally:
            await client.aclose()

    asyncio.run(run())


def test_client_rejects_arbitrary_queries_ids_operations_and_invalid_contracts() -> None:
    async def run() -> None:
        client = await _client()
        try:
            with pytest.raises(ControlPlaneClientError, match="allowlist"):
                await client.overview({"matter_id": "protected"})
            with pytest.raises(ControlPlaneClientError, match="snapshot id"):
                await client.report("../../owner-state")
            with pytest.raises(ControlPlaneClientError, match="schema validation"):
                await client.insight({"question": "missing required fields"})
            with pytest.raises(ControlPlaneClientError, match="metric family"):
                await client.metric_family("individual-ranking")
        finally:
            await client.aclose()

    asyncio.run(run())

    bad_report = copy.deepcopy(REPORT)
    bad_report["sections"][0]["metric_results"][0]["truth_state"] = "healthy"
    with pytest.raises(ControlPlaneClientError, match="schema validation"):
        ContractValidators().validate("report", bad_report)


def test_client_validates_frozen_error_contract_before_exposing_status() -> None:
    async def valid_error(request):
        if request.url.path.endswith("skworld-module.json"):
            return httpx.Response(
                200,
                json={
                    "schemaVersion": "1.1",
                    "entry": {"url": ORIGIN + "/"},
                    "auth": {"audience": "skdashboard", "scopes": ["skdashboard.read"]},
                    "health": ORIGIN + "/api/v1/health",
                },
            )
        return httpx.Response(
            503,
            json={
                "code": "SOURCE_UNAVAILABLE",
                "message": "The public synthetic source is unavailable.",
                "retryable": True,
                "request_id": "fixture-error",
            },
        )

    async def invalid_error(request):
        response = await valid_error(request)
        if response.status_code != 200:
            return httpx.Response(503, json={"code": "SOURCE_UNAVAILABLE"})
        return response

    async def run() -> None:
        client = await ControlPlaneClient.discover(
            DISCOVERY, BEARER, transport=httpx.MockTransport(valid_error)
        )
        try:
            with pytest.raises(ControlPlaneClientError, match="503 SOURCE_UNAVAILABLE"):
                await client.health()
        finally:
            await client.aclose()
        client = await ControlPlaneClient.discover(
            DISCOVERY, BEARER, transport=httpx.MockTransport(invalid_error)
        )
        try:
            with pytest.raises(ControlPlaneClientError, match="schema validation"):
                await client.health()
        finally:
            await client.aclose()

    asyncio.run(run())


def test_published_contract_copies_are_exact_and_schema_validators_are_sensitive() -> None:
    validators = ContractValidators()
    validators.validate("report", REPORT)
    validators.validate("insight", INSIGHT)
    validators.validate("insight_query", _insight_query())
    with pytest.raises(ValidationError):
        validators.validators["insight_query"].validate(
            {**_insight_query(), "metric_families": ["people-ranking"]}
        )

    from pathlib import Path

    root = Path(__file__).parents[1]
    names = {
        "openapi.control-plane.v1.1.0.json",
        "control-plane-metric-result.v1.1.0.schema.json",
        "control-plane-recommendation.v1.1.0.schema.json",
        "control-plane-insight.v1.1.0.schema.json",
        "control-plane-report-snapshot.v1.1.0.schema.json",
    }
    for name in names:
        assert (root / "src/skdashboard/contracts/v1.1.0" / name).read_bytes() == (
            root / "docs/contracts/v1.1.0" / name
        ).read_bytes()
