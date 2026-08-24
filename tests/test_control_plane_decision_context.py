from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, Lock

import pytest
from capauth import (
    ClientKind,
    ControlPlaneBinding,
    ControlPlaneDecisionAuthorizer,
    ControlPlaneInvocationV1,
    DecisionCode,
    DecisionState,
    OwnerPolicyDecision,
    RequestBoundary,
    export_control_plane_bearer,
)
from capauth.delegated import (
    CapabilityAuthorizer,
    CapabilityIssuer,
    InMemoryAuditSink,
    InMemoryPrincipalPolicyBackend,
    InMemoryReplayBackend,
    InMemoryRevocationBackend,
    IssuerGrant,
    Principal,
    StaticTrustedIssuerBackend,
)
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from skdashboard.control_plane_api import _protected_handler
from skdashboard.dashboard import create_app

UTC = timezone.utc
ORIGIN = "http://10.0.0.139:7778"
NOW = datetime.now(UTC)
REVISION = "b" * 64


class Signer:
    def __init__(self) -> None:
        self.values = {}
        self.count = 0

    @property
    def issuer_fingerprint(self) -> str:
        return "A" * 40

    def sign(self, payload: bytes) -> str:
        self.count += 1
        signature = f"signature-{self.count}"
        self.values[signature] = payload
        return signature

    def verify(self, token) -> bool:
        return self.values.get(token.signature) == token.payload.model_dump_json().encode()


class Owner:
    def decide(self, binding, _decision):
        return OwnerPolicyDecision(
            state=DecisionState.ALLOW,
            revision=binding.owner_policy_revision,
            resource_type=binding.resource_type,
            resource_id=binding.resource_id,
            reason_code="allow",
        )


class Rig:
    def __init__(self, subject: str = "human@example.test") -> None:
        self.principal = Principal(principal_id=subject, subject=subject, kind="human")
        self.binding = ControlPlaneBinding(
            principal=self.principal,
            node_id="chiap04",
            purpose="control-plane reporting",
            capability="skdashboard.read",
            target="/api/v1/overview",
            resource_type="skdashboard.control_plane.projection",
            resource_id="overview",
            owner_policy_revision=REVISION,
            expires_at=NOW + timedelta(minutes=2),
        )
        signer = Signer()

        def clock():
            return NOW

        capability = CapabilityAuthorizer(
            trusted_issuers=StaticTrustedIssuerBackend(
                (
                    IssuerGrant(
                        fingerprint=signer.issuer_fingerprint,
                        capabilities=frozenset({"skdashboard.read"}),
                        audiences=frozenset({"skdashboard"}),
                        principal_kinds=frozenset({"human"}),
                    ),
                )
            ),
            principals=InMemoryPrincipalPolicyBackend((self.principal,)),
            revocations=InMemoryRevocationBackend(),
            replay=InMemoryReplayBackend(clock=clock),
            audit=InMemoryAuditSink(),
            signature_verifier=signer,
            clock=clock,
        )
        self.authorizer = ControlPlaneDecisionAuthorizer(
            capability_authorizer=capability,
            owner_policy=Owner(),
            allowed_origins=frozenset({ORIGIN}),
            clock=clock,
        )
        self.bearer = export_control_plane_bearer(
            CapabilityIssuer(signer, clock=clock).issue_root(
                principal=self.principal,
                scope=self.binding.capability_scope(),
                ttl_seconds=120,
            )
        )

    def factory(self, request, capability: str, target: str):
        return ControlPlaneInvocationV1(
            node_id=self.binding.node_id,
            purpose=self.binding.purpose,
            capability=capability,
            target=target,
            resource_type=self.binding.resource_type,
            resource_id=self.binding.resource_id,
            correlation_id=request.headers.get("x-request-id", "request-1"),
            boundary=RequestBoundary(client_kind=ClientKind.BROWSER, origin=ORIGIN),
        )


def _app(handler, *, authorizer=None, decision_authorizer=None, factory=None):
    counters = {"denied": 0}
    protected = _protected_handler(
        handler,
        "skdashboard.read",
        authorize=authorizer or (lambda *_: False),
        decision_authorizer=decision_authorizer,
        invocation_factory=factory,
        counters=counters,
    )
    return Starlette(routes=[Route("/api/v1/overview", protected)]), counters


def test_typed_context_exists_only_during_handler_and_is_sanitized() -> None:
    rig = Rig()
    observed = {}

    async def handler(request):
        observed["request"] = request
        context = request.state.control_plane_decision
        observed["context"] = context
        return JSONResponse({"subject": context.binding.principal.subject})

    app, _ = _app(handler, decision_authorizer=rig.authorizer, factory=rig.factory)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json() == {"subject": "human@example.test"}
    assert "control_plane_decision" not in observed["request"].state._state
    serialized = observed["context"].model_dump_json()
    assert rig.bearer not in serialized
    assert "owner_decision" not in serialized
    assert "reason_code" not in serialized


def test_legacy_boolean_allow_never_creates_typed_authority() -> None:
    async def handler(request):
        return JSONResponse({"has_context": hasattr(request.state, "control_plane_decision")})

    app, _ = _app(handler, authorizer=lambda *_: True)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": "Bearer legacy", "Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert response.json() == {"has_context": False}


@pytest.mark.parametrize("value", [True, {}, None])
def test_wrong_typed_results_deny_without_legacy_fallback(value) -> None:
    class Wrong:
        def authorize(self, *_):
            return value

    calls = []

    async def handler(_request):
        calls.append(True)
        return JSONResponse({})

    rig = Rig()
    app, counters = _app(
        handler, authorizer=lambda *_: True, decision_authorizer=Wrong(), factory=rig.factory
    )
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": "Bearer secret-material", "Origin": ORIGIN},
    )
    assert response.status_code == 403
    assert calls == []
    assert counters["denied"] == 1
    assert "secret-material" not in response.text


def test_typed_pair_is_required_and_public_origin_denies_before_provider(tmp_path) -> None:
    rig = Rig()
    with pytest.raises(ValueError, match="both injected components"):
        create_app(tmp_path, control_plane_decision_authorizer=rig.authorizer)

    class Explodes:
        def authorize(self, *_):
            raise AssertionError("provider must not run")

    async def handler(_request):
        raise AssertionError("handler must not run")

    app, _ = _app(handler, decision_authorizer=Explodes(), factory=rig.factory)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": "Bearer secret", "Origin": "https://public.example"},
    )
    assert response.status_code == 403
    assert response.json()["code"] == "ORIGIN_DENIED"


def test_revoked_followup_cannot_reuse_prior_request_context() -> None:
    rig = Rig()
    allowed = rig.authorizer.authorize(
        rig.bearer,
        rig.factory(
            type("R", (), {"headers": {}, "url": type("U", (), {"path": "/api/v1/overview"})()})(),
            "skdashboard.read",
            "/api/v1/overview",
        ),
    )

    class Sequence:
        def __init__(self):
            self.values = [
                allowed,
                allowed.model_copy(
                    update={
                        "allow": False,
                        "state": DecisionState.DENY,
                        "code": DecisionCode.CAPAUTH_DENIED,
                        "context": None,
                    }
                ),
            ]

        def authorize(self, *_):
            return self.values.pop(0)

    seen = []

    async def handler(request):
        seen.append(request.state.control_plane_decision.authenticated_identity_ref)
        return JSONResponse({})

    sequence = Sequence()
    app, _ = _app(handler, decision_authorizer=sequence, factory=rig.factory)
    headers = {"Authorization": "Bearer any", "Origin": ORIGIN}
    client = TestClient(app)
    assert client.get("/api/v1/overview", headers=headers).status_code == 200
    assert client.get("/api/v1/overview", headers=headers).status_code == 403
    assert len(seen) == 1


def test_sse_body_cannot_observe_cleared_context() -> None:
    rig = Rig()

    async def handler(request):
        async def stream():
            state = "present" if hasattr(request.state, "control_plane_decision") else "cleared"
            yield f"data: {state}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    app, _ = _app(handler, decision_authorizer=rig.authorizer, factory=rig.factory)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
    )
    assert response.status_code == 200
    assert "data: cleared" in response.text


def test_handler_exception_still_clears_request_state() -> None:
    rig = Rig()
    observed = {}

    async def handler(request):
        observed["request"] = request
        assert request.state.control_plane_decision.binding == rig.binding
        raise RuntimeError("handler failed")

    app, _ = _app(handler, decision_authorizer=rig.authorizer, factory=rig.factory)
    with pytest.raises(RuntimeError, match="handler failed"):
        TestClient(app).get(
            "/api/v1/overview",
            headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
        )
    assert "control_plane_decision" not in observed["request"].state._state


def test_factory_and_returned_binding_must_match_exactly() -> None:
    rig = Rig()
    invocation = rig.factory(
        type("Request", (), {"headers": {}})(),
        "skdashboard.read",
        "/api/v1/overview",
    )
    allowed = rig.authorizer.authorize(rig.bearer, invocation)

    class Stale:
        def authorize(self, *_):
            return allowed

    def mismatched_factory(request, capability, target):
        return rig.factory(request, capability, target).model_copy(update={"node_id": "other"})

    async def handler(_request):
        raise AssertionError("mismatched binding reached handler")

    app, _ = _app(handler, decision_authorizer=Stale(), factory=mismatched_factory)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": "Bearer opaque", "Origin": ORIGIN},
    )
    assert response.status_code == 403


def test_stale_correlation_cannot_be_reused_for_a_new_invocation() -> None:
    rig = Rig()
    original = rig.factory(
        type("Request", (), {"headers": {}})(),
        "skdashboard.read",
        "/api/v1/overview",
    )
    allowed = rig.authorizer.authorize(rig.bearer, original)

    class Stale:
        def authorize(self, *_):
            return allowed

    def fresh_factory(request, capability, target):
        return rig.factory(request, capability, target).model_copy(
            update={"correlation_id": "new-request"}
        )

    async def handler(_request):
        raise AssertionError("stale correlation reached handler")

    app, _ = _app(handler, decision_authorizer=Stale(), factory=fresh_factory)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": "Bearer opaque", "Origin": ORIGIN},
    )
    assert response.status_code == 403


def test_origin_bearing_request_cannot_be_reclassified_as_native() -> None:
    rig = Rig()

    class Explodes:
        def authorize(self, *_):
            raise AssertionError("native-spoofed request reached authorizer")

    def native_factory(request, capability, target):
        return rig.factory(request, capability, target).model_copy(
            update={"boundary": RequestBoundary(client_kind=ClientKind.NATIVE)}
        )

    async def handler(_request):
        raise AssertionError("native-spoofed request reached handler")

    app, _ = _app(handler, decision_authorizer=Explodes(), factory=native_factory)
    response = TestClient(app).get(
        "/api/v1/overview",
        headers={"Authorization": "Bearer opaque", "Origin": ORIGIN},
    )
    assert response.status_code == 403


def test_concurrent_requests_do_not_share_identity() -> None:
    rigs = {name: Rig(name) for name in ("a@example.test", "b@example.test")}
    by_bearer = {rig.bearer: rig for rig in rigs.values()}
    lock = Lock()
    barrier = Barrier(2)
    seen = []

    class Multiplexed:
        def authorize(self, bearer, invocation):
            return by_bearer[bearer].authorizer.authorize(bearer, invocation)

    def factory(request, capability, target):
        return rigs[request.headers["x-test-principal"]].factory(request, capability, target)

    async def handler(request):
        barrier.wait(timeout=5)
        with lock:
            seen.append(request.state.control_plane_decision.binding.principal.subject)
        return JSONResponse({})

    app, _ = _app(handler, decision_authorizer=Multiplexed(), factory=factory)

    def read(item):
        name, rig = item
        return TestClient(app).get(
            "/api/v1/overview",
            headers={
                "Authorization": f"Bearer {rig.bearer}",
                "Origin": ORIGIN,
                "X-Test-Principal": name,
            },
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(read, rigs.items()))
    for response in responses:
        assert response.status_code == 200
    assert sorted(seen) == ["a@example.test", "b@example.test"]
