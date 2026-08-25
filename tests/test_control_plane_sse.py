from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from test_control_plane_decision_context import ORIGIN
from test_control_plane_decision_context import Rig as DecisionRig

from skdashboard import dashboard, dashboard_kanban
from skdashboard.control_plane_api import _stream_policy_boundary, _typed_context
from skdashboard.dashboard import create_app
from skdashboard.dashboard_kanban import (
    PUBLIC_STREAM_LANE,
    SSE_BOUND,
    Bus,
    StreamReset,
    stream_sse,
)

EVENT_HEADERS = {
    "Authorization": "Bearer valid-events",
    "Origin": "https://100.81.238.58:7778",
}


def _authorizer(bearer: str, capability: str, _target: str) -> bool:
    return bearer in {"valid-events", "other-events"} and capability == "skdashboard.events.read"


def test_legacy_cursor_is_unavailable_after_policy_partition_upgrade() -> None:
    old = Bus(stream_id="0" * 32)
    legacy = old.publish({"type": "board"}, public=True).event_id.replace("djI6", "djE6", 1)
    # A correctly encoded prior v1 cursor is accepted syntactically but cannot
    # identify the new policy lane, so restart or upgrade requires reset.
    raw = f"sse:v1:{'0' * 32}:1".encode()
    import base64

    legacy = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    reset = old.open_stream(legacy)
    assert reset.reset == StreamReset("replay window unavailable")


def test_ids_replay_resume_and_explicit_reset_are_exact_and_bounded() -> None:
    bus = Bus(stream_id="a" * 32)
    events = [bus.publish({"type": "board", "value": value}, public=True) for value in range(3)]
    assert all(events)
    assert [event.sequence for event in events] == [1, 2, 3]
    assert len({event.event_id for event in events}) == 3
    assert all(not event.event_id.endswith(str(event.sequence)) for event in events)

    resumed = bus.open_stream(events[0].event_id, boundary=PUBLIC_STREAM_LANE)
    assert resumed.replay == tuple(events[1:])
    resumed.close()

    for value in range(3, SSE_BOUND + 2):
        bus.publish({"type": "board", "value": value}, public=True)
    assert bus.replay_size == SSE_BOUND
    expired = bus.open_stream(events[0].event_id, boundary=PUBLIC_STREAM_LANE)
    assert expired.queue is None
    assert expired.reset == StreamReset("replay window unavailable")

    other = Bus(stream_id="b" * 32).publish({"type": "board"}, public=True)
    unknown_after_rollback = bus.open_stream(other.event_id, boundary=PUBLIC_STREAM_LANE)
    assert unknown_after_rollback.reset == StreamReset("replay window unavailable")


def test_policy_boundary_partitions_buffers_cursors_subscribers_and_delivery() -> None:
    bus = Bus(stream_id="b" * 32)
    tenant_a_caller_a = "tenant-a:caller-a"
    tenant_a_caller_b = "tenant-a:caller-b"
    tenant_b_caller_a = "tenant-b:caller-a"
    a = bus.open_stream(boundary=tenant_a_caller_a)
    same = bus.open_stream(boundary=tenant_a_caller_a)
    other_caller = bus.open_stream(boundary=tenant_a_caller_b)
    other_tenant = bus.open_stream(boundary=tenant_b_caller_a)

    event = bus.publish(
        {"type": "board", "value": "tenant-a-caller-a"}, boundary=tenant_a_caller_a
    )
    assert a.queue.get_nowait() == event
    assert same.queue.get_nowait() == event
    assert other_caller.queue.empty()
    assert other_tenant.queue.empty()

    a.close()
    same.close()
    other_caller.close()
    other_tenant.close()
    resumed = bus.open_stream(event.event_id, boundary=tenant_a_caller_a)
    assert resumed.replay == ()
    resumed.close()
    for boundary in (tenant_a_caller_b, tenant_b_caller_a):
        denied = bus.open_stream(event.event_id, boundary=boundary)
        assert denied.queue is None
        assert denied.reset == StreamReset("replay window unavailable")

    for value in range(SSE_BOUND + 1):
        bus.publish({"type": "board", "value": value}, boundary=f"lane-{value % 3}")
    assert bus.replay_size == SSE_BOUND


def test_reconnect_topics_concurrent_consumers_and_slow_consumer_are_bounded() -> None:
    bus = Bus(stream_id="c" * 32)
    first = bus.publish({"type": "board", "value": 1}, public=True)
    bus.publish({"type": "reports", "value": 2}, public=True)
    filtered = bus.open_stream(first.event_id, ("reports",), boundary=PUBLIC_STREAM_LANE)
    assert [event.event for event in filtered.replay] == ["reports"]
    filtered.close()

    one = bus.open_stream(boundary=PUBLIC_STREAM_LANE)
    two = bus.open_stream(boundary=PUBLIC_STREAM_LANE)
    live = bus.publish({"type": "board", "value": 3}, public=True)
    assert one.queue.get_nowait() == live
    assert two.queue.get_nowait() == live

    for value in range(SSE_BOUND + 1):
        bus.publish({"type": "board", "value": value}, public=True)
    reset = one.queue.get_nowait()
    assert reset == StreamReset("slow consumer")
    assert one.queue.qsize() == 0
    assert two.queue.qsize() <= SSE_BOUND
    assert bus.replay_size == SSE_BOUND
    assert bus.subscriber_count == 0

    subscriptions = [bus.open_stream(boundary=PUBLIC_STREAM_LANE) for _ in range(SSE_BOUND)]
    with pytest.raises(RuntimeError, match="capacity"):
        bus.open_stream(boundary=PUBLIC_STREAM_LANE)
    for subscription in subscriptions:
        subscription.close()


def test_heartbeat_cancellation_and_producer_failure_cleanup() -> None:
    async def run() -> None:
        bus = Bus(stream_id="d" * 32)
        subscription = bus.open_stream()
        body = stream_sse(subscription, heartbeat_seconds=0)
        assert await anext(body) == ": heartbeat\n\n"
        assert await anext(body) == ": heartbeat\n\n"
        await body.aclose()
        assert bus.subscriber_count == 0

        subscription = bus.open_stream(boundary=PUBLIC_STREAM_LANE)
        body = stream_sse(subscription)
        assert bus.publish({"type": "board", "bad": object()}, public=True) is None
        assert await anext(body) == ": heartbeat\n\n"
        assert "producer failure" in await anext(body)
        with pytest.raises(StopAsyncIteration):
            await anext(body)
        assert bus.subscriber_count == 0

    asyncio.run(run())


def test_protected_sse_rejects_legacy_boolean_only_context(tmp_path) -> None:
    response = TestClient(create_app(tmp_path, control_plane_authorizer=_authorizer)).get(
        "/api/v1/events",
        headers=EVENT_HEADERS,
    )
    assert response.status_code == 403
    assert response.json()["message"] == "typed Tenant and caller context is required"


def test_protected_event_has_zero_legacy_or_public_lane_leakage() -> None:
    bus = Bus(stream_id="1" * 32)
    legacy = bus.subscribe()
    public = bus.open_stream(boundary=PUBLIC_STREAM_LANE)
    protected = bus.open_stream(boundary="tenant-a:caller-a")

    event = bus.publish({"type": "board", "value": "protected"}, boundary="tenant-a:caller-a")

    assert protected.queue.get_nowait() == event
    assert legacy.empty()
    assert public.queue.empty()
    bus.unsubscribe(legacy)
    public.close()
    protected.close()


def test_real_publishers_are_explicitly_public_lane_only() -> None:
    dashboard_source = Path(dashboard.__file__).read_text(encoding="utf-8")
    kanban_source = Path(dashboard_kanban.__file__).read_text(encoding="utf-8")
    tree = ast.parse(dashboard_source + kanban_source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "publish"
        and (
            isinstance(node.func.value, ast.Attribute)
            and node.func.value.attr == "BUS"
            or isinstance(node.func.value, ast.Name)
            and node.func.value.id == "BUS"
        )
    ]

    assert len(calls) == 9
    assert all(
        [(keyword.arg, keyword.value.value) for keyword in call.keywords] == [("public", True)]
        for call in calls
    )


def test_public_lane_reaches_only_public_and_legacy_subscribers() -> None:
    bus = Bus(stream_id="2" * 32)
    legacy = bus.subscribe()
    public = bus.open_stream(boundary=PUBLIC_STREAM_LANE)
    protected = bus.open_stream(boundary="tenant-a:caller-a")

    event = bus.publish({"type": "board", "value": "public"}, public=True)
    with pytest.raises(ValueError, match="public or protected"):
        bus.publish({"type": "board"}, boundary=PUBLIC_STREAM_LANE)

    assert legacy.get_nowait()["value"] == "public"
    assert public.queue.get_nowait() == event
    assert protected.queue.empty()
    bus.unsubscribe(legacy)
    public.close()
    protected.close()


def test_typed_http_sse_isolates_tenant_caller_and_resumes_same_boundary(
    tmp_path, monkeypatch
) -> None:
    bus = Bus(stream_id="3" * 32)
    monkeypatch.setattr(dashboard_kanban, "BUS", bus)

    async def finite_stream(subscription):
        try:
            if subscription.reset is not None:
                yield "event: reset-required\n\n"
            for event in subscription.replay:
                yield f"event: {event.event}\ndata: {event.data}\n\n"
        finally:
            subscription.close()

    monkeypatch.setattr(dashboard_kanban, "stream_sse", finite_stream)
    caller_a = DecisionRig(
        subject="caller-a@example.test",
        target="/api/v1/events",
        capability="skdashboard.events.read",
        resource_type="tenant",
        resource_id="tenant-a",
    )
    caller_b = DecisionRig(
        subject="caller-b@example.test",
        target="/api/v1/events",
        capability="skdashboard.events.read",
        resource_type="tenant",
        resource_id="tenant-a",
    )
    tenant_b = DecisionRig(
        subject="caller-a@example.test",
        target="/api/v1/events",
        capability="skdashboard.events.read",
        resource_type="tenant",
        resource_id="tenant-b",
    )
    context, verifier = _typed_context(
        type(
            "Request",
            (),
            {
                "headers": {"origin": ORIGIN},
                "url": type("URL", (), {"path": "/api/v1/events"})(),
            },
        )(),
        caller_a.bearer,
        "skdashboard.events.read",
        "/api/v1/events",
        decision_authorizer=caller_a.authorizer,
        invocation_factory=caller_a.factory,
    )
    verifier.close()
    boundary = _stream_policy_boundary(context)
    first = bus.publish({"type": "board", "value": "first"}, boundary=boundary)
    bus.publish({"type": "board", "value": "second"}, boundary=boundary)

    def response_for(rig, cursor):
        app = create_app(
            tmp_path,
            control_plane_decision_authorizer=rig.authorizer,
            control_plane_invocation_factory=rig.factory,
        )
        response = TestClient(app).get(
            "/api/v1/events",
            headers={
                "Authorization": f"Bearer {rig.fresh_bearer()}",
                "Origin": ORIGIN,
                "Last-Event-ID": cursor,
            },
        )
        return response.status_code, response.text

    same_status, same_body = response_for(caller_a, first.event_id)
    assert same_status == 200
    assert "second" in same_body
    assert "first" not in same_body
    for other in (caller_b, tenant_b):
        denied_status, denied_body = response_for(other, first.event_id)
        assert denied_status == 200
        assert "reset-required" in denied_body
        assert "first" not in denied_body and "second" not in denied_body


def test_stream_boundary_accepts_only_one_exact_typed_tenant() -> None:
    rig = DecisionRig(
        target="/api/v1/events",
        capability="skdashboard.events.read",
        resource_type="tenant",
        resource_id="tenant-a",
    )
    context, verifier = _typed_context(
        type(
            "Request",
            (),
            {
                "headers": {"origin": ORIGIN},
                "url": type("URL", (), {"path": "/api/v1/events"})(),
            },
        )(),
        rig.bearer,
        "skdashboard.events.read",
        "/api/v1/events",
        decision_authorizer=rig.authorizer,
        invocation_factory=rig.factory,
    )
    verifier.close()
    assert _stream_policy_boundary(context)

    for update in (
        {"resource_type": "skdashboard.control_plane.projection", "resource_id": "overview"},
        {"resource_type": "tenant", "resource_id": None},
    ):
        malformed = context.model_copy(
            update={"binding": context.binding.model_copy(update=update)}
        )
        with pytest.raises(ValueError, match="one exact typed authenticated Tenant"):
            _stream_policy_boundary(malformed)

    conflicting = context.model_copy(
        update={
            "joined_decision": context.joined_decision.model_copy(
                update={"resource_id": "tenant-b"}
            )
        }
    )
    with pytest.raises(ValueError, match="one exact typed authenticated Tenant"):
        _stream_policy_boundary(conflicting)


def test_http_sse_rejects_generic_and_missing_tenant_contexts(tmp_path) -> None:
    def response_for(rig):
        return TestClient(
            create_app(
                tmp_path,
                control_plane_decision_authorizer=rig.authorizer,
                control_plane_invocation_factory=rig.factory,
            )
        ).get(
            "/api/v1/events",
            headers={"Authorization": f"Bearer {rig.bearer}", "Origin": ORIGIN},
        )

    for resource_type, resource_id in (
        ("skdashboard.control_plane.projection", "overview"),
        ("tenant", None),
        ("Tenant", "tenant-a"),
    ):
        rig = DecisionRig(
            target="/api/v1/events",
            capability="skdashboard.events.read",
            resource_type=resource_type,
            resource_id=resource_id,
        )
        response = response_for(rig)
        assert response.status_code == 403
        assert response.json()["message"] == "typed Tenant and caller context is required"
