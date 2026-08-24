"""Frozen v1 read-only control-plane projections."""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

SCHEMA_VERSION = "1.1.0"
MAX_LIMIT = 200
MAX_BEARER_BYTES = 64 * 1024
ALLOWED_BROWSER_ORIGINS = frozenset(
    {"http://10.0.0.139:7778", "http://100.81.238.58:7778"}
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error(request, status: int, code: str, message: str, *, retryable: bool = False):
    request_id = request.headers.get("x-request-id", "")[:128] or uuid4().hex
    return JSONResponse(
        {"code": code, "message": message, "retryable": retryable, "request_id": request_id},
        status_code=status,
    )


def _limit(request) -> int:
    raw = request.query_params.get("limit", "50")
    try:
        limit = int(raw)
    except ValueError as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return limit


def _cursor(raw: str | None) -> int:
    if not raw:
        return 0
    if len(raw) > 512:
        raise ValueError("cursor is too long")
    try:
        decoded = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)).decode("ascii")
        prefix, value = decoded.split(":", 1)
        if prefix != "v1":
            raise ValueError
        offset = int(value)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("cursor is invalid") from exc
    if offset < 0:
        raise ValueError("cursor is invalid")
    return offset


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"v1:{offset}".encode()).decode().rstrip("=")


def _visibility() -> dict:
    # SKCP-02 adds capability authorization. Until then, this local read plane
    # is explicitly visible and never manufactures a policy decision.
    return {"state": "visible", "authorization": "authorized"}


def _envelope(
    request,
    owner: str,
    items: list[dict],
    errors: list[str],
    *,
    observed_at=None,
    truth_state=None,
):
    projected_at = _now()
    truth = truth_state or ("partial" if errors else ("current" if items else "unknown"))
    request_id = request.headers.get("x-request-id", "")[:128] or uuid4().hex
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "source_owner": owner,
        "scope": {},
        "freshness": {
            "truth_state": truth,
            "visibility": _visibility(),
            "observed_at": observed_at or projected_at,
            "projected_at": projected_at,
            "ttl_seconds": 60,
            "age_seconds": 0,
        },
        "visibility": _visibility(),
        "metrics": [],
        "items": items,
        "errors": [
            {
                "code": "SOURCE_PARTIAL",
                "message": str(message)[:500],
                "retryable": True,
                "request_id": request_id,
            }
            for message in errors[:64]
        ],
    }
    if not items and not errors:
        envelope["errors"] = [{
            "code": "SOURCE_UNKNOWN",
            "message": "The source returned no observations",
            "retryable": True,
            "request_id": request_id,
        }]
    return envelope


def _page(request, owner: str, items: list[dict], errors: list[str], *, observed_at=None):
    limit = _limit(request)
    offset = _cursor(request.query_params.get("cursor"))
    if offset > len(items):
        raise ValueError("cursor is outside the result set")
    page_items = items[offset : offset + limit]
    result = _envelope(request, owner, page_items, errors, observed_at=observed_at)
    next_offset = offset + len(page_items)
    result["page"] = {
        "limit": limit,
        "next_cursor": _encode_cursor(next_offset) if next_offset < len(items) else None,
        "has_more": next_offset < len(items),
    }
    return result


def _response(request, body: dict):
    serialized = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    # Request IDs and projection clocks are delivery metadata, not source
    # changes. Hash only the bounded projection so conditional GETs are useful.
    projection = {
        key: body.get(key)
        for key in ("schema_version", "source_owner", "scope", "metrics", "items", "page", "errors")
    }
    for error in projection.get("errors") or []:
        error.pop("request_id", None)
    def source_projection(value):
        if isinstance(value, dict):
            return {
                key: source_projection(item)
                for key, item in value.items()
                if key not in {"projected_at", "observed_at", "age_seconds", "request_id"}
            }
        if isinstance(value, list):
            return [source_projection(item) for item in value]
        return value

    etag_bytes = json.dumps(
        source_projection(projection), sort_keys=True, separators=(",", ":")
    ).encode()
    etag = f'"{hashlib.sha256(etag_bytes).hexdigest()}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return Response(serialized, media_type="application/json", headers={"ETag": etag})


def _capauth_authorize(home: Path, bearer: str, capability: str, target: str) -> bool:
    """Verify one bounded audience token and its current CapAuth policy."""

    try:
        from capauth.authz import decide
        from capauth.tokens import has_scope, import_token, verify_audience_token

        token = import_token(bearer)
        payload = token.payload
        if (
            payload.expires_at is None
            or payload.expires_at <= payload.issued_at
            or (payload.expires_at - payload.issued_at).total_seconds() > 300
            or "*" in payload.capabilities
            or not has_scope(token, capability)
            or not verify_audience_token(token, "skdashboard", home=home)
        ):
            return False
        decision = decide(
            payload.subject,
            capability,
            resource={"target": target},
            context={"service": "skdashboard-api"},
            base_dir=home,
        )
        return bool(decision.allow)
    except Exception:
        return False


def routes(home: Path, *, board_reader, health_reader, authorizer=None):
    hits: dict[str, deque[float]] = defaultdict(deque)
    counters = {"requests": 0, "denied": 0}
    authorize = authorizer or (
        lambda bearer, capability, target: _capauth_authorize(
            home, bearer, capability, target
        )
    )

    def limited(handler):
        async def wrapped(request):
            now = time.monotonic()
            key = request.client.host if request.client else "local"
            recent = hits[key]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= 120:
                response = _error(request, 429, "RATE_LIMITED", "read rate limit exceeded", retryable=True)
                response.headers["Retry-After"] = "60"
                return response
            recent.append(now)
            counters["requests"] += 1
            return await handler(request)

        return wrapped

    def protected(handler, capability: str):
        async def wrapped(request):
            origin = request.headers.get("origin")
            if origin is not None and origin not in ALLOWED_BROWSER_ORIGINS:
                counters["denied"] += 1
                return _error(request, 403, "ORIGIN_DENIED", "browser origin is not allowed")
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer ") or header.count(" ") != 1:
                counters["denied"] += 1
                return _error(request, 401, "UNAUTHORIZED", "a bearer capability is required")
            bearer = header[7:]
            if not bearer or len(bearer.encode()) > MAX_BEARER_BYTES:
                counters["denied"] += 1
                return _error(request, 401, "UNAUTHORIZED", "the bearer capability is invalid")
            try:
                allowed = authorize(bearer, capability, request.url.path)
            except Exception:
                allowed = False
            if not allowed:
                counters["denied"] += 1
                return _error(request, 403, "FORBIDDEN", "the capability decision denied access")
            return await handler(request)

        return limited(wrapped)

    async def health(request):
        raw = health_reader(home)
        errors = [raw["error"]] if raw.get("error") else []
        safe = [] if errors else [{
            "component": "skcapstone",
            "state": raw.get("consciousness", "unknown").lower(),
            "pillars": raw.get("pillars", {}),
        }]
        return _response(request, _envelope(request, "skcapstone", safe, errors))

    async def board(request):
        try:
            raw = board_reader(home)
            errors = [raw["error"]] if raw.get("error") else []
            items = [
                {
                    "task_id": item.get("id"),
                    "title": item.get("title"),
                    "priority": item.get("priority"),
                    "status": item.get("status"),
                    "claimed_by": item.get("claimed_by"),
                }
                for item in raw.get("tasks", [])
            ]
            return _response(request, _page(request, "skcoord", items, errors))
        except ValueError as exc:
            return _error(request, 400, "INVALID_QUERY", str(exc))

    async def fleet(request):
        from . import dashboard_fleet

        try:
            raw = dashboard_fleet.get_drift(home, alert=False)
            errors = [str(value) for value in raw.get("errors", [])]
            items = [
                {
                    "node_id": node.get("node"),
                    "state": node.get("severity", "unknown"),
                    "counts": node.get("counts", {}),
                }
                for node in raw.get("nodes", [])
            ] + [
                {
                    "node_id": node.get("node"),
                    "state": "unknown",
                    "reason_code": node.get("reason_code", "ungraded"),
                }
                for node in raw.get("skipped", [])
            ]
            return _response(request, _page(request, "skcapstone.fleet", items, errors))
        except ValueError as exc:
            return _error(request, 400, "INVALID_QUERY", str(exc))

    async def economy(request):
        from . import dashboard_skcounter

        try:
            lane = request.query_params.get("measurement_lane", "harness_reported")
            if lane not in dashboard_skcounter.LANES:
                raise ValueError("measurement_lane is invalid")
            raw = dashboard_skcounter.get_ai_usage(
                home,
                {"lane": lane, "from": request.query_params.get("from", ""), "to": request.query_params.get("to", "")},
            )
            summary = raw.get("summary", {})
            coverage = raw.get("coverage", {})
            cost = summary.get("cost_usd") if summary.get("cost_state") == "available" else None
            items = [{
                "measurement_lane": raw.get("selected_lane"),
                "available_lanes": raw.get("available_lanes", []),
                "tokens": {key: summary.get(key, 0) for key in dashboard_skcounter.TOKEN_FIELDS},
                "cost_usd": cost,
                "cost_state": summary.get("cost_state", "unavailable"),
                "collectors": raw.get("collectors", []),
                "expected_nodes": coverage.get("expected_nodes", 0),
                "reporting_nodes": coverage.get("reporting_nodes", 0),
                "missing_nodes": coverage.get("missing_nodes", []),
            }]
            return _response(
                request,
                _page(request, "skcounter", items, [str(x) for x in raw.get("errors", [])], observed_at=raw.get("generated_at")),
            )
        except ValueError as exc:
            return _error(request, 400, "INVALID_QUERY", str(exc))

    async def overview(request):
        from .control_plane_adapters import default_readers, project_estate
        from .control_plane_quality import project_data_quality

        items = project_estate(default_readers(home))
        errors = [
            f"{item['adapter_id']}: {error['code']}"
            for item in items
            for error in item["errors"]
        ]
        states = {item["truth_state"] for item in items}
        truth = (
            "current"
            if states == {"current"}
            else ("unavailable" if states == {"unavailable"} else "partial")
        )
        quality = project_data_quality(items)
        return _response(
            request,
            _envelope(request, "skdashboard", [*items, quality], errors, truth_state=truth),
        )

    async def events(request):
        raw_cursor = request.query_params.get("cursor") or request.headers.get("last-event-id")
        topics = [value for value in request.query_params.get("topics", "").split(",") if value]
        try:
            if len(topics) > 16 or any(len(value) > 64 for value in topics):
                raise ValueError("topics exceed the bounded contract")
            if raw_cursor:
                _cursor(raw_cursor)
        except ValueError as exc:
            return _error(request, 400, "INVALID_QUERY", str(exc))

        async def stream():
            if raw_cursor:
                yield "event: reset-required\ndata: {\"reason\":\"replay window unavailable\"}\n\n"
            yield ": heartbeat\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    async def metrics(_request):
        lines = [
            "# HELP skdashboard_control_plane_up Whether this projection process is serving.",
            "# TYPE skdashboard_control_plane_up gauge",
            "skdashboard_control_plane_up 1",
            "# HELP skdashboard_control_plane_requests_total Bounded control-plane requests.",
            "# TYPE skdashboard_control_plane_requests_total counter",
            f"skdashboard_control_plane_requests_total {counters['requests']}",
            "# HELP skdashboard_control_plane_denied_total Denied control-plane requests.",
            "# TYPE skdashboard_control_plane_denied_total counter",
            f"skdashboard_control_plane_denied_total {counters['denied']}",
        ]
        return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")

    return [
        Route("/api/v1/health", limited(health)),
        Route("/api/v1/overview", protected(overview, "skdashboard.read")),
        Route("/api/v1/board/summary", protected(board, "skdashboard.read")),
        Route("/api/v1/fleet/summary", protected(fleet, "skdashboard.read")),
        Route("/api/v1/economy/summary", protected(economy, "skdashboard.read")),
        Route("/api/v1/events", protected(events, "skdashboard.events.read")),
        Route("/metrics", protected(metrics, "skdashboard.read")),
    ]
