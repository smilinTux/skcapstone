"""Public synthetic ASGI fixture for typed client and MCP development."""

from __future__ import annotations

import hashlib
import json

from starlette.applications import Starlette
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .dashboard_reports import build_report_snapshot

ORIGIN = "https://synthetic.test"
BEARER = "fixture-read"
WINDOW = {
    "start": "2026-08-24T00:00:00Z",
    "end": "2026-08-25T00:00:00Z",
    "timezone": "UTC",
    "baseline": None,
}
SCOPE = {"portfolio_id": "synthetic-estate"}
HASH = "sha256:" + "a" * 64


def _metric() -> dict:
    return {
        "schema_version": "1.1.0",
        "metric_id": "portfolio.synthetic_count",
        "definition_version": "1.0.0",
        "value": 1,
        "unit": "items",
        "polarity": "context_only",
        "numerator": 1,
        "denominator": None,
        "sample_size": 1,
        "scope": SCOPE,
        "window": WINDOW,
        "truth_state": "current",
        "visibility": {"state": "visible", "authorization": "authorized"},
        "measurement_kind": "measured",
        "source": {
            "owner": "synthetic.fixture",
            "adapter_id": "synthetic.fixture",
            "adapter_version": "1.0.0",
            "observed_at": "2026-08-24T12:00:00Z",
            "projected_at": "2026-08-24T12:00:30Z",
            "freshness_ttl_seconds": 300,
            "watermarks": [{"source": "synthetic.fixture", "value": "fixture-r1"}],
            "evidence_refs": ["evidence:synthetic:metric-r1"],
        },
        "data_quality": {
            "coverage_numerator": 1,
            "coverage_denominator": 1,
            "errors": [],
            "exclusions": ["production and protected content"],
        },
        "calculation": {"definition_hash": HASH, "method": "count"},
        "classification": {"level": "public", "purpose": "client qualification"},
    }


REPORT = build_report_snapshot(
    report_type="daily_operations",
    audience=["public synthetic development"],
    generated_at="2026-08-24T12:00:30Z",
    as_of="2026-08-24T12:00:00Z",
    scope=SCOPE,
    baseline=None,
    sections=[
        {
            "section_id": "synthetic",
            "title": "Public synthetic evidence",
            "metric_results": [_metric()],
            "insights": [],
        }
    ],
)

INSIGHT = {
    "insight_id": "ins-synthetic-abstention",
    "schema_version": "1.1.0",
    "status": "abstained",
    "kind": "brief",
    "summary": "The public synthetic fixture abstains from operational advice.",
    "scope": SCOPE,
    "window": WINDOW,
    "metric_refs": [],
    "evidence_refs": ["evidence:synthetic:metric-r1"],
    "calculation_refs": [],
    "uncertainty": ["No production owner evidence is available."],
    "contradictions": [],
    "exclusions": ["production state", "protected content"],
    "visibility": {"state": "visible", "authorization": "authorized"},
    "model_provenance": {
        "logical_route": "skdashboard.synthetic.fixture",
        "transport_profile": "offline-fixture",
        "gateway_revision": "not_applicable",
        "backend": "deterministic-fixture",
        "requested_model": "not_applicable",
        "served_model": "not_applicable",
        "model_revision": "not_applicable",
        "prompt_hash": HASH,
        "schema_hash": HASH,
    },
    "policy_decision_ref": "policy:synthetic-fixture",
    "abstention_reason": {
        "code": "insufficient_evidence",
        "message": "The fixture contains no production owner evidence.",
        "evidence_refs": ["evidence:synthetic:metric-r1"],
    },
    "recommendations": [],
    "next_steps": [
        {
            "label": "Open synthetic evidence",
            "kind": "open_evidence",
            "preview_only": True,
            "target_ref": "evidence:synthetic:metric-r1",
        }
    ],
}


def _envelope(items: list[dict], *, page: dict | None = None) -> dict:
    body = {
        "schema_version": "1.1.0",
        "request_id": "fixture-request",
        "source_owner": "synthetic.fixture",
        "scope": SCOPE,
        "freshness": {
            "truth_state": "current",
            "visibility": {"state": "visible", "authorization": "authorized"},
            "observed_at": "2026-08-24T12:00:00Z",
            "projected_at": "2026-08-24T12:00:30Z",
            "ttl_seconds": 300,
            "age_seconds": 30,
        },
        "visibility": {"state": "visible", "authorization": "authorized"},
        "metrics": [_metric()],
        "items": items,
        "errors": [],
    }
    if page is not None:
        body["page"] = page
    return body


def _etag_response(request, body: dict):
    content = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    etag = f'"{hashlib.sha256(content).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(content, media_type="application/json", headers={"ETag": etag})


def _authorized(request) -> bool:
    return request.headers.get("authorization") == f"Bearer {BEARER}"


def _error(code: str, message: str, status: int) -> JSONResponse:
    return JSONResponse(
        {
            "code": code,
            "message": message,
            "retryable": False,
            "request_id": "fixture-error",
        },
        status_code=status,
    )


def create_fixture_app() -> Starlette:
    async def manifest(_request):
        return JSONResponse(
            {
                "schemaVersion": "1.1",
                "id": "skdashboard-fixture",
                "name": "SK Control Plane public synthetic fixture",
                "grade": "B",
                "entry": {"url": f"{ORIGIN}/"},
                "auth": {"audience": "skdashboard", "scopes": ["skdashboard.read"]},
                "health": f"{ORIGIN}/api/v1/health",
            }
        )

    async def health(request):
        return _etag_response(request, _envelope([{"component": "fixture", "state": "current"}]))

    async def projection(request):
        if not _authorized(request):
            return _error("FORBIDDEN", "The public synthetic read was denied.", 403)
        return _etag_response(
            request, _envelope([{"evidence_refs": ["evidence:synthetic:metric-r1"]}])
        )

    async def page(request):
        if not _authorized(request):
            return _error("FORBIDDEN", "The public synthetic read was denied.", 403)
        cursor = request.query_params.get("cursor")
        items = [{"item_id": "synthetic-2"}] if cursor else [{"item_id": "synthetic-1"}]
        state = {
            "limit": 1,
            "next_cursor": None if cursor else "fixture-page-2",
            "has_more": not bool(cursor),
        }
        return _etag_response(request, _envelope(items, page=state))

    async def report(request):
        if not _authorized(request) or request.path_params["snapshot_id"] != REPORT["snapshot_id"]:
            return _error("REPORT_NOT_FOUND", "The synthetic report was not found.", 404)
        return _etag_response(request, REPORT)

    async def insight(request):
        if not _authorized(request):
            return _error("FORBIDDEN", "The public synthetic query was denied.", 403)
        try:
            query = await request.json()
        except Exception:
            return _error("INVALID_QUERY", "The synthetic query was invalid.", 400)
        if not isinstance(query, dict):
            return _error("INVALID_QUERY", "The synthetic query was invalid.", 400)
        return _etag_response(request, INSIGHT)

    async def events(request):
        if not _authorized(request):
            return _error("FORBIDDEN", "The public synthetic stream was denied.", 403)
        cursor = request.query_params.get("cursor")
        prefix = (
            'event: reset-required\ndata: {"reason":"fixture replay unavailable"}\n\n'
            if cursor
            else ""
        )
        return Response(prefix + ": heartbeat\n\n", media_type="text/event-stream")

    return Starlette(
        routes=[
            Route("/.well-known/skworld-module.json", manifest),
            Route("/api/v1/health", health),
            Route("/api/v1/overview", projection),
            Route("/api/v1/board/summary", page),
            Route("/api/v1/fleet/summary", page),
            Route("/api/v1/economy/summary", page),
            Route("/api/v1/reports/{snapshot_id}", report),
            Route("/api/v1/insights/query", insight, methods=["POST"]),
            Route("/api/v1/events", events),
        ]
    )


__all__ = ["BEARER", "INSIGHT", "ORIGIN", "REPORT", "create_fixture_app"]
