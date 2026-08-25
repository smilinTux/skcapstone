"""Frozen v1 read-only control-plane projections."""

from __future__ import annotations

import base64
import hashlib
import inspect
import json
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

SCHEMA_VERSION = "1.1.0"
MAX_LIMIT = 200
MAX_BEARER_BYTES = 64 * 1024
TENANT_RESOURCE_TYPE = "tenant"
ALLOWED_BROWSER_ORIGINS = frozenset({"https://10.0.0.139:7778", "https://100.81.238.58:7778"})
SSE_CURRENTNESS_SECONDS = 1


class ControlPlaneInvocationFactory(Protocol):
    """Build trusted invocation facts without consulting bearer claims."""

    def __call__(self, request, capability: str, target: str): ...


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
    scope=None,
):
    projected_at = _now()
    truth = truth_state or ("partial" if errors else ("current" if items else "unknown"))
    request_id = request.headers.get("x-request-id", "")[:128] or uuid4().hex
    envelope = {
        "schema_version": SCHEMA_VERSION,
        "request_id": request_id,
        "source_owner": owner,
        "scope": dict(scope or {}),
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
        envelope["errors"] = [
            {
                "code": "SOURCE_UNKNOWN",
                "message": "The source returned no observations",
                "retryable": True,
                "request_id": request_id,
            }
        ]
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
        for key in (
            "schema_version",
            "source_owner",
            "scope",
            "metrics",
            "items",
            "page",
            "errors",
        )
    }
    for error in projection.get("errors") or []:
        error.pop("request_id", None)

    def source_projection(value):
        if isinstance(value, dict):
            return {
                key: source_projection(
                    {
                        child_key: child_value
                        for child_key, child_value in item.items()
                        if child_key not in {"capauth_decision_id", "expires_at"}
                    }
                    if key == "policy_decision" and isinstance(item, dict)
                    else item
                )
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
        from capauth import canonical_subject
        from capauth.authz import decide
        from capauth.tokens import has_scope, import_token, verify_audience_token

        decoded = base64.b64decode(bearer.encode("ascii"), altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(decoded).decode("ascii") != bearer:
            return False
        token = import_token(decoded.decode("utf-8"))
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
        if len(payload.subject) not in {40, 64}:
            return False
        policy_subject = canonical_subject(f"device:{payload.subject}")
        decision = decide(
            policy_subject,
            capability,
            resource={"target": target},
            context={"service": "skdashboard-api"},
            base_dir=home,
        )
        return bool(decision.allow)
    except Exception:
        return False


def _clear_decision(request) -> None:
    verifier = getattr(request.state, "control_plane_currentness_verifier", None)
    try:
        from capauth import ControlPlaneCurrentnessVerifier

        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
    except Exception:
        pass
    try:
        del request.state.control_plane_currentness_verifier
    except (AttributeError, KeyError):
        pass
    try:
        del request.state.control_plane_decision
    except (AttributeError, KeyError):
        pass


def _typed_context(
    request,
    bearer: str,
    capability: str,
    target: str,
    *,
    decision_authorizer,
    invocation_factory: ControlPlaneInvocationFactory,
):
    """Return one exact sanitized CapAuth context or fail closed."""

    from capauth import (
        ClientKind,
        ControlPlaneAuthorizationResultV1,
        ControlPlaneCurrentnessVerifier,
        ControlPlaneInvocationV1,
        DecisionCode,
        DecisionState,
        SanitizedControlPlaneDecisionV1,
    )

    invocation = invocation_factory(request, capability, target)
    if type(invocation) is not ControlPlaneInvocationV1:
        return None
    boundary = invocation.boundary
    observed_origin = request.headers.get("origin")
    if boundary.client_kind is ClientKind.BROWSER:
        if observed_origin is None or boundary.origin != observed_origin:
            return None
    elif observed_origin is not None:
        return None
    verifier = None
    authorize_with_currentness = getattr(decision_authorizer, "authorize_with_currentness", None)
    if callable(authorize_with_currentness):
        issued = authorize_with_currentness(bearer, invocation)
        if type(issued) is not tuple or len(issued) != 2:
            if type(issued) in {tuple, list}:
                for value in issued:
                    if type(value) is ControlPlaneCurrentnessVerifier:
                        value.close()
            return None
        result, verifier = issued
        if type(verifier) is not ControlPlaneCurrentnessVerifier:
            return None
    else:
        result = decision_authorizer.authorize(bearer, invocation)
    if type(result) is not ControlPlaneAuthorizationResultV1:
        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
        return None
    if (
        not result.allow
        or result.state is not DecisionState.ALLOW
        or result.code is not DecisionCode.ALLOW
        or type(result.context) is not SanitizedControlPlaneDecisionV1
    ):
        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
        return None
    try:
        # Revalidate even model_construct output before it reaches request state.
        validated = SanitizedControlPlaneDecisionV1(
            **{
                name: getattr(result.context, name)
                for name in SanitizedControlPlaneDecisionV1.model_fields
            }
        )
    except Exception:
        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
        return None
    if validated != result.context:
        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
        return None
    context = result.context
    binding = context.binding
    if (
        context.boundary != boundary
        or binding.node_id != invocation.node_id
        or binding.purpose != invocation.purpose
        or binding.audience != invocation.audience
        or binding.capability != capability
        or binding.target != target
        or binding.resource_type != invocation.resource_type
        or binding.resource_id != invocation.resource_id
        or context.capauth_decision.correlation_id != invocation.correlation_id
        or context.joined_decision.scope != binding.capability_scope()
    ):
        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
        return None
    now = datetime.now(timezone.utc)
    if not context.issued_at <= now < context.expires_at:
        if type(verifier) is ControlPlaneCurrentnessVerifier:
            verifier.close()
        return None
    return context, verifier


def _stream_policy_boundary(context) -> str:
    """Return an opaque exact Tenant/caller boundary from typed CapAuth context."""
    from capauth import SanitizedControlPlaneDecisionV1

    if type(context) is not SanitizedControlPlaneDecisionV1:
        raise TypeError("protected SSE requires typed authorization context")
    binding = context.binding
    joined = context.joined_decision
    scopes = (binding.capability_scope(), context.capauth_decision.scope, joined.scope)
    resource_types = {
        binding.resource_type,
        joined.resource_type,
        *(scope.resource_type for scope in scopes),
    }
    tenant_ids = {
        binding.resource_id,
        joined.resource_id,
        *(scope.resource_id for scope in scopes),
    }
    if resource_types != {TENANT_RESOURCE_TYPE} or len(tenant_ids) != 1 or None in tenant_ids:
        raise ValueError("protected SSE requires one exact typed authenticated Tenant")
    tenant_id = binding.resource_id
    facts = {
        "tenant": tenant_id,
        "principal": binding.principal.model_dump(mode="json"),
        "agent_id": binding.agent_id,
        "node_id": binding.node_id,
        "purpose": binding.purpose,
        "audience": binding.audience,
        "capability": binding.capability,
        "target": binding.target,
        "resource_type": binding.resource_type,
        "owner_policy_revision": binding.owner_policy_revision,
    }
    encoded = json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class _StreamAuthority:
    """Keep one signed CapAuth decision current for a protected SSE iterator."""

    def __init__(self, authorizer, context, verifier, boundary) -> None:
        self._authorizer = authorizer
        self._context = context
        self._verifier = verifier
        self._boundary = boundary
        self._capauth = verifier._authorizer
        self._presented = verifier._presented
        self._request = verifier._request
        self._prior = verifier._prior
        self._receipt = None

    def check(self) -> bool:
        from capauth import DecisionState, join_policy_decisions

        context = self._context
        if context is None or datetime.now(timezone.utc) >= context.expires_at:
            self.close()
            return False
        try:
            first = self._authorizer._owner_decision(context.binding, self._prior)
            if self._verifier is not None:
                verifier, self._verifier = self._verifier, None
                allowed = (
                    verifier.check_before_owner_read(context) is DecisionState.ALLOW
                    and verifier.check_after_owner_read(context) is DecisionState.ALLOW
                )
                verifier.close()
                if not allowed:
                    raise ValueError
            else:
                current = self._capauth.revalidate_current(
                    self._presented, self._request, self._prior, self._receipt
                )
                self._receipt = None
                if current is not self._prior:
                    raise ValueError
            second = self._authorizer._owner_decision(context.binding, self._prior)
            if (
                first is None
                or first != second
                or join_policy_decisions(context.binding, self._prior, second)
                != context.joined_decision
                or _stream_policy_boundary(context) != self._boundary
            ):
                raise ValueError
            self._receipt = self._capauth._mint_currentness_receipts(
                self._presented, self._request, self._prior, count=1
            )[0]
        except Exception:
            self.close()
            return False
        return True

    def close(self) -> None:
        verifier, self._verifier = self._verifier, None
        if verifier is not None:
            verifier.close()
        if self._receipt is not None:
            self._capauth.discard_currentness_receipts((self._receipt,))
            self._receipt = None
        self._presented = None
        self._request = None
        self._prior = None
        self._context = None


def _protected_handler(
    handler,
    capability: str,
    *,
    authorize,
    decision_authorizer,
    invocation_factory,
    counters,
    session_resolver=None,
    session_capability_issuer=None,
    require_stream_context=False,
):
    async def wrapped(request):
        _clear_decision(request)
        origin = request.headers.get("origin")
        if origin is not None and origin not in ALLOWED_BROWSER_ORIGINS:
            counters["denied"] += 1
            response = _error(request, 403, "ORIGIN_DENIED", "browser origin is not allowed")
            response.headers["Cache-Control"] = "no-store"
            return response
        header = request.headers.get("authorization", "")
        if session_capability_issuer is not None and header:
            counters["denied"] += 1
            response = _error(request, 401, "UNAUTHORIZED", "a browser bearer is not accepted")
            response.headers["Cache-Control"] = "no-store"
            return response
        if session_resolver is not None and not header:
            try:
                resolved = await session_resolver(request)
                state = getattr(resolved, "state", None)
                if state in {"corrupt", "unavailable"}:
                    counters["denied"] += 1
                    response = _error(
                        request,
                        503,
                        "SESSION_UNAVAILABLE",
                        "session authorization is temporarily unavailable",
                        retryable=True,
                    )
                    response.headers["Retry-After"] = "5"
                    response.headers["Cache-Control"] = "no-store"
                    return response
                if session_capability_issuer is None:
                    bearer = getattr(resolved, "access_token", resolved)
                elif state == "authenticated":
                    bearer = session_capability_issuer(
                        request, resolved, capability, request.url.path
                    )
                    if inspect.isawaitable(bearer):
                        bearer = await bearer
                    if bearer == getattr(resolved, "access_token", None):
                        bearer = None
                else:
                    bearer = None
            except Exception:
                bearer = None
            if bearer:
                header = f"Bearer {bearer}"
        if not header.startswith("Bearer ") or header.count(" ") != 1:
            counters["denied"] += 1
            response = _error(request, 401, "UNAUTHORIZED", "a bearer capability is required")
            response.headers["Cache-Control"] = "no-store"
            return response
        bearer = header[7:]
        if not bearer or len(bearer.encode()) > MAX_BEARER_BYTES:
            counters["denied"] += 1
            response = _error(request, 401, "UNAUTHORIZED", "the bearer capability is invalid")
            response.headers["Cache-Control"] = "no-store"
            return response
        if decision_authorizer is not None:
            try:
                authority = _typed_context(
                    request,
                    bearer,
                    capability,
                    request.url.path,
                    decision_authorizer=decision_authorizer,
                    invocation_factory=invocation_factory,
                )
            except Exception:
                authority = None
            if authority is None:
                counters["denied"] += 1
                response = _error(
                    request, 403, "FORBIDDEN", "the capability decision denied access"
                )
                response.headers["Cache-Control"] = "no-store"
                return response
            context, verifier = authority
            if require_stream_context:
                try:
                    boundary = _stream_policy_boundary(context)
                    request.state.control_plane_stream_boundary = boundary
                    request.state.control_plane_stream_authority = _StreamAuthority(
                        decision_authorizer, context, verifier, boundary
                    )
                except (TypeError, ValueError):
                    if verifier is not None:
                        verifier.close()
                    counters["denied"] += 1
                    response = _error(
                        request,
                        403,
                        "FORBIDDEN",
                        "typed Tenant and caller context is required",
                    )
                    response.headers["Cache-Control"] = "no-store"
                    return response
            request.state.control_plane_decision = context
            if verifier is not None and not require_stream_context:
                request.state.control_plane_currentness_verifier = verifier
        else:
            if require_stream_context:
                counters["denied"] += 1
                response = _error(
                    request,
                    403,
                    "FORBIDDEN",
                    "typed Tenant and caller context is required",
                )
                response.headers["Cache-Control"] = "no-store"
                return response
            try:
                allowed = authorize(bearer, capability, request.url.path)
            except Exception:
                allowed = False
            if not allowed:
                counters["denied"] += 1
                response = _error(
                    request, 403, "FORBIDDEN", "the capability decision denied access"
                )
                response.headers["Cache-Control"] = "no-store"
                return response
        response = None
        try:
            response = await handler(request)
            response.headers["Cache-Control"] = "no-store"
            return response
        finally:
            authority = getattr(request.state, "control_plane_stream_authority", None)
            if authority is not None and not isinstance(response, StreamingResponse):
                authority.close()
            try:
                del request.state.control_plane_stream_boundary
            except (AttributeError, KeyError):
                pass
            try:
                del request.state.control_plane_stream_authority
            except (AttributeError, KeyError):
                pass
            _clear_decision(request)

    return wrapped


def routes(
    home: Path,
    *,
    board_reader,
    health_reader,
    authorizer=None,
    decision_authorizer=None,
    invocation_factory: ControlPlaneInvocationFactory | None = None,
    project_provider=None,
    schedule_provider=None,
    schedule_forecast_provider=None,
    reliability_provider=None,
    session_resolver=None,
    session_capability_issuer=None,
    architecture_provider=None,
    governance_provider=None,
    report_provider=None,
):
    if (decision_authorizer is None) != (invocation_factory is None):
        raise ValueError("typed control-plane authorization requires both injected components")
    if (
        project_provider is not None
        or schedule_provider is not None
        or schedule_forecast_provider is not None
        or reliability_provider is not None
        or architecture_provider is not None
        or governance_provider is not None
        or report_provider is not None
    ) and decision_authorizer is None:
        raise ValueError("owner projection requires typed control-plane authorization")
    hits: dict[str, deque[float]] = defaultdict(deque)
    counters = {"requests": 0, "denied": 0}
    authorize = authorizer or (
        lambda bearer, capability, target: _capauth_authorize(home, bearer, capability, target)
    )

    def limited(handler):
        async def wrapped(request):
            now = time.monotonic()
            key = request.client.host if request.client else "local"
            recent = hits[key]
            while recent and recent[0] <= now - 60:
                recent.popleft()
            if len(recent) >= 120:
                response = _error(
                    request, 429, "RATE_LIMITED", "read rate limit exceeded", retryable=True
                )
                response.headers["Retry-After"] = "60"
                return response
            recent.append(now)
            counters["requests"] += 1
            return await handler(request)

        return wrapped

    def protected(handler, capability: str, *, require_stream_context=False):
        return limited(
            _protected_handler(
                handler,
                capability,
                authorize=authorize,
                decision_authorizer=decision_authorizer,
                invocation_factory=invocation_factory,
                counters=counters,
                session_resolver=session_resolver,
                session_capability_issuer=session_capability_issuer,
                require_stream_context=require_stream_context,
            )
        )

    async def health(request):
        raw = health_reader(home)
        errors = [raw["error"]] if raw.get("error") else []
        safe = (
            []
            if errors
            else [
                {
                    "component": "skcapstone",
                    "state": raw.get("consciousness", "unknown").lower(),
                    "pillars": raw.get("pillars", {}),
                }
            ]
        )
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
                {
                    "lane": lane,
                    "from": request.query_params.get("from", ""),
                    "to": request.query_params.get("to", ""),
                },
            )
            summary = raw.get("summary", {})
            coverage = raw.get("coverage", {})
            cost = summary.get("cost_usd") if summary.get("cost_state") == "available" else None
            items = [
                {
                    "measurement_lane": raw.get("selected_lane"),
                    "available_lanes": raw.get("available_lanes", []),
                    "tokens": {
                        key: summary.get(key, 0) for key in dashboard_skcounter.TOKEN_FIELDS
                    },
                    "cost_usd": cost,
                    "cost_state": summary.get("cost_state", "unavailable"),
                    "collectors": raw.get("collectors", []),
                    "expected_nodes": coverage.get("expected_nodes", 0),
                    "reporting_nodes": coverage.get("reporting_nodes", 0),
                    "missing_nodes": coverage.get("missing_nodes", []),
                }
            ]
            return _response(
                request,
                _page(
                    request,
                    "skcounter",
                    items,
                    [str(x) for x in raw.get("errors", [])],
                    observed_at=raw.get("generated_at"),
                ),
            )
        except ValueError as exc:
            return _error(request, 400, "INVALID_QUERY", str(exc))

    async def overview(request):
        from .control_plane_adapters import default_readers, project_estate
        from .control_plane_quality import project_data_quality
        from .control_plane_scope import (
            ProtectedScopeDenied,
            ScopeQueryError,
            parse_now_scope,
        )

        try:
            scope = parse_now_scope(request.query_params)
        except ProtectedScopeDenied:
            return _error(
                request,
                403,
                "PROTECTED_SCOPE_DENIED",
                "protected scope is not available",
            )
        except ScopeQueryError as exc:
            return _error(request, 400, "INVALID_SCOPE", str(exc))
        from skcoord.authorized_card_snapshot import (
            AuthorizedCardScopeV1,
            unavailable_authorized_card_snapshot,
        )

        adapter_items = project_estate(default_readers(home))
        project_scope = AuthorizedCardScopeV1(
            role=scope.role,
            scope=scope.scope,
            service=scope.service,
            window=scope.window,
            baseline=scope.baseline,
        )
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        project = unavailable_authorized_card_snapshot(project_scope)
        if project_provider is not None and context is not None and verifier is not None:
            project = project_provider.read(
                context,
                project_scope,
                home,
                currentness_verifier=verifier,
            )
        errors = [
            f"{item.get('adapter_id', item.get('projection_type', 'source'))}: {error['code']}"
            for item in [*adapter_items, project]
            for error in item["errors"]
        ]
        states = {item["truth_state"] for item in [*adapter_items, project]}
        truth = (
            "current"
            if states == {"current"}
            else ("unavailable" if states == {"unavailable"} else "partial")
        )
        quality = project_data_quality(adapter_items)
        return _response(
            request,
            _envelope(
                request,
                "skdashboard",
                [*adapter_items, project, quality],
                errors,
                truth_state=truth,
                scope=scope.as_dict(),
            ),
        )

    async def schedule(request):
        allowed = {
            "role",
            "scope",
            "window",
            "baseline",
            "service",
            "lens",
            "timezone",
            "selected_item",
        }
        pairs = list(request.query_params.multi_items())
        if any(key not in allowed or not value or len(value) > 128 for key, value in pairs) or len(
            {key for key, _value in pairs}
        ) != len(pairs):
            return _error(request, 400, "INVALID_SCHEDULE_SCOPE", "unsupported schedule scope")
        query = dict(pairs)
        if (
            query.get("role")
            not in {"project-manager", "operator", "architect", "service", "team"}
            or query.get("scope") != "estate"
            or query.get("window") != "latest"
            or query.get("baseline") != "none"
            or query.get("service") != "all"
            or query.get("lens") not in {"roadmap", "gantt", "flow"}
            or not query.get("timezone")
        ):
            return _error(request, 400, "INVALID_SCHEDULE_SCOPE", "unsupported schedule scope")
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if schedule_provider is None or context is None or verifier is None:
            return _error(
                request,
                503,
                "SCHEDULE_UNAVAILABLE",
                "the authorized schedule projection is unavailable",
                retryable=True,
            )
        projection = schedule_provider.read(
            context,
            query,
            home,
            currentness_verifier=verifier,
        )
        if not isinstance(projection, dict):
            return _error(request, 503, "SCHEDULE_UNAVAILABLE", "invalid schedule projection")
        serialized = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(serialized).hexdigest()}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(serialized, media_type="application/json", headers={"ETag": etag})

    async def schedule_forecasts(request):
        from capauth import DecisionState

        allowed = {"role", "scope", "window", "baseline", "service", "lens", "timezone"}
        pairs = list(request.query_params.multi_items())
        if any(key not in allowed or not value or len(value) > 128 for key, value in pairs) or len({key for key, _value in pairs}) != len(pairs):
            return _error(request, 400, "INVALID_SCHEDULE_SCOPE", "unsupported schedule scope")
        query = dict(pairs)
        if query.get("role") not in {"project-manager", "operator", "architect", "service", "team"} or query.get("scope") != "estate" or query.get("window") != "latest" or query.get("baseline") != "none" or query.get("service") != "all" or query.get("lens") not in {"roadmap", "gantt", "flow"} or not query.get("timezone"):
            return _error(request, 400, "INVALID_SCHEDULE_SCOPE", "unsupported schedule scope")
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if schedule_forecast_provider is None or context is None or verifier is None:
            return _error(request, 503, "SCHEDULE_FORECAST_UNAVAILABLE", "the authorized schedule forecast is unavailable", retryable=True)
        try:
            if verifier.check_before_owner_read(context) is not DecisionState.ALLOW:
                return _error(request, 503, "SCHEDULE_FORECAST_UNAVAILABLE", "the authorized schedule forecast is unavailable", retryable=True)
            result = schedule_forecast_provider.read(context, query, home, currentness_verifier=verifier)
            if verifier.check_after_owner_read(context) is not DecisionState.ALLOW:
                return _error(request, 503, "SCHEDULE_FORECAST_UNAVAILABLE", "the authorized schedule forecast is unavailable", retryable=True)
        except Exception:
            return _error(request, 503, "SCHEDULE_FORECAST_UNAVAILABLE", "the authorized schedule forecast is unavailable", retryable=True)
        allowed_keys = {
            "schema_version", "artifact_kind", "state", "abstention_reason", "method", "calculation_owner",
            "method_discrimination", "cohort", "scope", "history_window", "sample_periods", "period_cadence_days",
            "remaining_work", "iterations", "seed", "assumptions", "exclusions", "individual_ranking_prohibited",
            "completion_quantiles_periods", "milestone_confidence", "writes_owner_records",
        }
        if not isinstance(result, dict):
            return _error(request, 503, "SCHEDULE_FORECAST_UNAVAILABLE", "invalid schedule forecast")
        quantiles = result.get("completion_quantiles_periods")
        exclusions = result.get("exclusions")
        typed = (
            result.get("schema_version") == "1.0.0"
            and result.get("artifact_kind") == "aggregate_schedule_forecast"
            and result.get("method") == "aggregate_throughput_bootstrap_monte_carlo"
            and result.get("calculation_owner") == "deterministic_engine"
            and result.get("state") in {"ready", "abstained"}
            and isinstance(result.get("cohort"), str)
            and isinstance(result.get("scope"), str)
            and result.get("method_discrimination") == {"throughput_forecast": "probabilistic aggregate flow in periods", "date_critical_path": "not calculated or blended by this artifact"}
            and isinstance(result.get("history_window"), dict)
            and set(result["history_window"]) == {"start", "end"}
            and all(value is None or isinstance(value, str) for value in result["history_window"].values())
            and isinstance(result.get("sample_periods"), int)
            and (result.get("period_cadence_days") is None or isinstance(result.get("period_cadence_days"), int))
            and isinstance(result.get("remaining_work"), int)
            and isinstance(result.get("iterations"), int)
            and isinstance(result.get("seed"), int)
            and result.get("individual_ranking_prohibited") is True
            and isinstance(result.get("assumptions"), list)
            and all(isinstance(item, str) for item in result["assumptions"])
            and isinstance(exclusions, list)
            and all(isinstance(item, dict) and set(item) == {"period_id", "timing_basis", "reason"} and all(isinstance(value, str) for value in item.values()) for item in exclusions)
            and isinstance(quantiles, dict)
            and set(quantiles) == {"p50", "p85", "p95"}
            and all(value is None or isinstance(value, int) for value in quantiles.values())
        )
        ready = typed and result.get("state") == "ready" and result.get("abstention_reason") is None and all(type(value) is int for value in quantiles.values()) and quantiles["p50"] <= quantiles["p85"] <= quantiles["p95"] and (result.get("milestone_confidence") is None or isinstance(result.get("milestone_confidence"), float) and 0 <= result["milestone_confidence"] <= 1)
        abstained = typed and result.get("state") == "abstained" and isinstance(result.get("abstention_reason"), str) and bool(result["abstention_reason"]) and result.get("milestone_confidence") is None and all(value is None for value in quantiles.values())
        if result.get("writes_owner_records") is not False or set(result) - allowed_keys or not (ready or abstained):
            return _error(request, 503, "SCHEDULE_FORECAST_UNAVAILABLE", "invalid schedule forecast")
        return _response(request, result)

    async def reliability(request):
        allowed = {"role", "scope", "window", "baseline", "service"}
        pairs = list(request.query_params.multi_items())
        if any(key not in allowed or not value or len(value) > 128 for key, value in pairs) or len(
            {key for key, _value in pairs}
        ) != len(pairs):
            return _error(
                request,
                400,
                "INVALID_RELIABILITY_SCOPE",
                "unsupported reliability scope",
            )
        query = dict(pairs)
        if (
            query.get("role") not in {"operator", "architect", "service-owner"}
            or query.get("scope") != "estate"
            or query.get("window") != "latest"
            or query.get("baseline") != "none"
            or query.get("service") != "all"
        ):
            return _error(
                request,
                400,
                "INVALID_RELIABILITY_SCOPE",
                "unsupported reliability scope",
            )
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if reliability_provider is None or context is None or verifier is None:
            return _error(
                request,
                503,
                "RELIABILITY_UNAVAILABLE",
                "the authorized reliability projection is unavailable",
                retryable=True,
            )
        projection = reliability_provider.read(
            context,
            query,
            home,
            currentness_verifier=verifier,
        )
        if not isinstance(projection, dict):
            return _error(
                request,
                503,
                "RELIABILITY_UNAVAILABLE",
                "invalid reliability projection",
            )
        serialized = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(serialized).hexdigest()}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(serialized, media_type="application/json", headers={"ETag": etag})

    async def architecture(request):
        allowed = {"role", "scope", "window", "baseline", "service", "environment"}
        pairs = list(request.query_params.multi_items())
        if any(key not in allowed or not value or len(value) > 128 for key, value in pairs) or len(
            {key for key, _value in pairs}
        ) != len(pairs):
            return _error(
                request,
                400,
                "INVALID_ARCHITECTURE_SCOPE",
                "unsupported architecture scope",
            )
        query = dict(pairs)
        if (
            query.get("role") not in {"architect", "operator", "service-owner"}
            or query.get("scope") != "estate"
            or query.get("window") != "latest"
            or query.get("baseline") != "none"
            or query.get("service") != "all"
            or query.get("environment") != "all"
        ):
            return _error(
                request,
                400,
                "INVALID_ARCHITECTURE_SCOPE",
                "unsupported architecture scope",
            )
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if architecture_provider is None or context is None or verifier is None:
            return _error(
                request,
                503,
                "ARCHITECTURE_UNAVAILABLE",
                "the authorized architecture projection is unavailable",
                retryable=True,
            )
        projection = architecture_provider.read(
            context,
            query,
            home,
            currentness_verifier=verifier,
        )
        if not isinstance(projection, dict):
            return _error(
                request,
                503,
                "ARCHITECTURE_UNAVAILABLE",
                "invalid architecture projection",
            )
        serialized = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(serialized).hexdigest()}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(serialized, media_type="application/json", headers={"ETag": etag})

    async def governance(request):
        allowed = {"role", "scope", "window", "baseline", "service"}
        pairs = list(request.query_params.multi_items())
        if any(key not in allowed or not value or len(value) > 128 for key, value in pairs) or len(
            {key for key, _value in pairs}
        ) != len(pairs):
            return _error(
                request,
                400,
                "INVALID_GOVERNANCE_SCOPE",
                "unsupported governance scope",
            )
        query = dict(pairs)
        if (
            query.get("role") not in {"governance", "auditor", "operator"}
            or query.get("scope") != "estate"
            or query.get("window") != "latest"
            or query.get("baseline") != "none"
            or query.get("service") != "all"
        ):
            return _error(
                request,
                400,
                "INVALID_GOVERNANCE_SCOPE",
                "unsupported governance scope",
            )
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if governance_provider is None or context is None or verifier is None:
            return _error(
                request,
                503,
                "GOVERNANCE_UNAVAILABLE",
                "the authorized governance projection is unavailable",
                retryable=True,
            )
        projection = governance_provider.read(
            context,
            query,
            home,
            currentness_verifier=verifier,
        )
        if not isinstance(projection, dict):
            return _error(
                request,
                503,
                "GOVERNANCE_UNAVAILABLE",
                "invalid governance projection",
            )
        serialized = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(serialized).hexdigest()}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(serialized, media_type="application/json", headers={"ETag": etag})

    async def reports(request):
        allowed = {
            "role",
            "scope",
            "window",
            "baseline",
            "service",
            "report_type",
            "snapshot",
            "compare",
        }
        pairs = list(request.query_params.multi_items())
        if any(key not in allowed or not value or len(value) > 128 for key, value in pairs) or len(
            {key for key, _value in pairs}
        ) != len(pairs):
            return _error(request, 400, "INVALID_REPORT_SCOPE", "unsupported report scope")
        query = dict(pairs)
        if (
            query.get("role") not in {"project-manager", "operator", "architect", "auditor"}
            or query.get("scope") != "estate"
            or query.get("window") != "latest"
            or query.get("baseline") not in {"none", "previous"}
            or query.get("service") != "all"
            or query.get("report_type", "all")
            not in {
                "all",
                "daily_operations",
                "weekly_portfolio",
                "sprint_flow",
                "monthly_service",
                "monthly_ai_economy",
                "quarterly_strategy",
                "ad_hoc_evidence",
            }
        ):
            return _error(request, 400, "INVALID_REPORT_SCOPE", "unsupported report scope")
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if report_provider is None or context is None or verifier is None:
            return _error(
                request,
                503,
                "REPORTS_UNAVAILABLE",
                "the authorized report projection is unavailable",
                retryable=True,
            )
        try:
            projection = report_provider.read(context, query, home, currentness_verifier=verifier)
        except KeyError:
            return _error(
                request, 404, "REPORT_NOT_FOUND", "the immutable report snapshot was not found"
            )
        except ValueError:
            return _error(
                request,
                503,
                "REPORTS_UNAVAILABLE",
                "the immutable report store is unavailable",
                retryable=True,
            )
        if not isinstance(projection, dict):
            return _error(request, 503, "REPORTS_UNAVAILABLE", "invalid report projection")
        serialized = json.dumps(projection, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{hashlib.sha256(serialized).hexdigest()}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(serialized, media_type="application/json", headers={"ETag": etag})

    async def report_snapshot(request):
        snapshot_id = request.path_params.get("snapshot_id", "")
        if len(snapshot_id) > 96:
            return _error(
                request, 404, "REPORT_NOT_FOUND", "the immutable report snapshot was not found"
            )
        context = getattr(request.state, "control_plane_decision", None)
        verifier = getattr(request.state, "control_plane_currentness_verifier", None)
        if report_provider is None or context is None or verifier is None:
            return _error(
                request,
                503,
                "REPORTS_UNAVAILABLE",
                "the authorized report snapshot is unavailable",
                retryable=True,
            )
        try:
            snapshot = report_provider.read_snapshot(
                context, snapshot_id, home, currentness_verifier=verifier
            )
        except KeyError:
            return _error(
                request, 404, "REPORT_NOT_FOUND", "the immutable report snapshot was not found"
            )
        except ValueError:
            return _error(
                request, 404, "REPORT_NOT_FOUND", "the immutable report snapshot was not found"
            )
        if not isinstance(snapshot, dict):
            return _error(request, 503, "REPORTS_UNAVAILABLE", "invalid report snapshot")
        serialized = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        etag = f'"{snapshot["report_hash"].removeprefix("sha256:")}"'
        if request.headers.get("if-none-match") == etag:
            return Response(status_code=304, headers={"ETag": etag})
        return Response(serialized, media_type="application/json", headers={"ETag": etag})

    async def events(request):
        from .dashboard_kanban import BUS, SSE_HEARTBEAT_SECONDS, stream_sse

        cursor_query = request.query_params.get("cursor")
        cursor_header = request.headers.get("last-event-id")
        raw_cursor = cursor_query or cursor_header
        topics = tuple(
            value for value in request.query_params.get("topics", "").split(",") if value
        )
        try:
            if cursor_query and cursor_header and cursor_query != cursor_header:
                raise ValueError("resume cursors disagree")
            if len(topics) > 16 or any(len(value) > 64 for value in topics):
                raise ValueError("topics exceed the bounded contract")
            boundary = request.state.control_plane_stream_boundary
            try:
                subscription = BUS.open_stream(raw_cursor, topics, boundary=boundary)
            except ValueError:
                # Frozen v1 accepted its pagination-shaped placeholder cursor;
                # it remains an unavailable replay window, not a malformed query.
                if not raw_cursor:
                    raise
                _cursor(raw_cursor)
                from .dashboard_kanban import StreamReset, StreamSubscription

                subscription = StreamSubscription(
                    BUS, (), None, boundary, StreamReset("replay window unavailable")
                )
        except ValueError as exc:
            return _error(request, 400, "INVALID_QUERY", str(exc))
        except RuntimeError:
            return _error(
                request,
                503,
                "STREAM_CAPACITY_REACHED",
                "event stream capacity reached",
                retryable=True,
            )

        return StreamingResponse(
            stream_sse(
                subscription,
                authority=request.state.control_plane_stream_authority,
                currentness_seconds=SSE_CURRENTNESS_SECONDS,
            ),
            media_type="text/event-stream",
            headers={
                "X-Accel-Buffering": "no",
                "X-Heartbeat-Interval": str(SSE_HEARTBEAT_SECONDS),
            },
        )

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
        Route("/api/v1/schedule/projection", protected(schedule, "skdashboard.read")),
        Route("/api/v1/schedule/forecasts", protected(schedule_forecasts, "skdashboard.read")),
        Route("/api/v1/reliability/projection", protected(reliability, "skdashboard.read")),
        Route("/api/v1/architecture/projection", protected(architecture, "skdashboard.read")),
        Route("/api/v1/governance/projection", protected(governance, "skdashboard.read")),
        Route("/api/v1/reports/projection", protected(reports, "skdashboard.read")),
        Route(
            "/api/v1/reports/{snapshot_id}",
            protected(report_snapshot, "skdashboard.reports.read"),
        ),
        Route("/api/v1/board/summary", protected(board, "skdashboard.read")),
        Route("/api/v1/fleet/summary", protected(fleet, "skdashboard.read")),
        Route("/api/v1/economy/summary", protected(economy, "skdashboard.read")),
        Route(
            "/api/v1/events",
            protected(events, "skdashboard.events.read", require_stream_context=True),
        ),
        Route("/metrics", protected(metrics, "skdashboard.read")),
    ]
