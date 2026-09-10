"""Production contract tests for role-bounded Niobe fan-out."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.niobe_fanout import (
    FanoutBoundaryError,
    LifecycleFanoutRequest,
    NiobeRuntimeIdentity,
    RequestingSeatIdentity,
    append_fanout_receipt,
    pending_fanout_request,
    reconcile_fanout_receipt,
    reconcile_runtime_state,
    resolve_niobe_runtime_identity,
    resolve_requesting_seat_identity,
    submit_fanout_request,
)

HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _verified_niobe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "skcapstone.niobe_fanout.resolve_niobe_runtime_identity",
        lambda _home: NiobeRuntimeIdentity("decision", "f" * 64, "chiap08", "unit", "a" * 32),
    )
    monkeypatch.setattr(
        "skcapstone.niobe_fanout.resolve_requesting_seat_identity",
        lambda _home: RequestingSeatIdentity("link", "chiap08", "control-1", "link-unit"),
    )


def _card(home: Path, card_id: str = "deadbeef", route: str = "integration") -> CardStore:
    store = CardStore(home)
    store.create(
        CardCore(
            id=card_id,
            title="Bounded lifecycle child",
            created_by="link",
            initial_labels=[f"fanout-scope-{route}"],
        )
    )
    return store


def _request(card_id: str = "deadbeef", **changes: str) -> LifecycleFanoutRequest:
    values = {
        "card_id": card_id,
        "source_head": HEAD,
        "requester": "link",
        "route": "integration",
    }
    values.update(changes)
    return LifecycleFanoutRequest(**values)


def test_link_request_flows_through_niobe_with_exact_immutable_receipts(tmp_path: Path) -> None:
    store = _card(tmp_path)
    request = submit_fanout_request(tmp_path, _request())

    assert pending_fanout_request(tmp_path, "deadbeef") == request
    append_fanout_receipt(
        tmp_path,
        request,
        state="materialized",
        process={"host": "chiap08", "workspace": "/work/deadbeef"},
    )
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    claimed = append_fanout_receipt(
        tmp_path,
        request,
        state="claimed",
        claim_owner="worker",
        claim_revision="claim-1",
    )
    launched = append_fanout_receipt(
        tmp_path,
        request,
        state="launched",
        claim_owner="worker",
        claim_revision="claim-1",
        process={
            "request_id": request.request_id,
            "card_id": "deadbeef",
            "owner": "worker",
            "claim_revision": "claim-1",
            "session_id": "worker-session",
            "unit": "skfleet-worker-codex-deadbeef.service",
            "alive": True,
        },
    )

    assert pending_fanout_request(tmp_path, "deadbeef") is None
    assert claimed["writer"] == launched["writer"] == "niobe"
    assert claimed["claim_revision"] == launched["claim_revision"] == "claim-1"
    assert (
        len(
            [
                row
                for row in store._read_events("deadbeef")
                if row["action"] == "niobe_fanout_receipt"
            ]
        )
        == 4
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"requester": "jarvis"}, "verified runtime identity"),
        ({"route": "review"}, "card scope"),
        ({"model": "sk-codex-high"}, "bounded default"),
    ],
)
def test_role_and_card_scope_intersection_fails_closed(
    tmp_path: Path, changes: dict[str, str], message: str
) -> None:
    _card(tmp_path)
    with pytest.raises(FanoutBoundaryError, match=message):
        submit_fanout_request(tmp_path, _request(**changes))


def test_verified_mero_runtime_cannot_request_link_route(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _card(tmp_path)
    monkeypatch.setattr(
        "skcapstone.niobe_fanout.resolve_requesting_seat_identity",
        lambda _home: RequestingSeatIdentity("mero", "chiap08", "control-1", "mero-unit"),
    )
    with pytest.raises(FanoutBoundaryError, match="requester role"):
        submit_fanout_request(tmp_path, _request(requester="mero"))


def test_request_rejects_claimed_card_and_non_niobe_actuator(tmp_path: Path) -> None:
    store = _card(tmp_path)
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    with pytest.raises(FanoutBoundaryError, match="unclaimed"):
        submit_fanout_request(tmp_path, _request())


def test_source_head_dedupes_across_cards_and_ambiguous_requests_fail(tmp_path: Path) -> None:
    store = _card(tmp_path)
    _card(tmp_path, "feedface")
    first = submit_fanout_request(tmp_path, _request())
    submit_fanout_request(tmp_path, _request("feedface"))
    assert pending_fanout_request(tmp_path, first.card_id) == first
    assert pending_fanout_request(tmp_path, "feedface") is None

    store.append_event(
        "deadbeef",
        "niobe_fanout_request",
        "link",
        request_id="b" * 64,
        source_head=HEAD,
        route="integration",
        model="sk-codex-mid",
        schema="skfleet.niobe-fanout-request/v1",
    )
    with pytest.raises(FanoutBoundaryError, match="canonical|ambiguous"):
        pending_fanout_request(tmp_path, "deadbeef")


def test_source_head_reservation_is_atomic_across_cards(tmp_path: Path) -> None:
    _card(tmp_path)
    _card(tmp_path, "feedface")
    submit_fanout_request(tmp_path, _request())
    submit_fanout_request(tmp_path, _request("feedface"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda card_id: pending_fanout_request(tmp_path, card_id), ("deadbeef", "feedface")
            )
        )

    assert sum(result is not None for result in results) == 1


def test_runtime_identity_rejects_spoofed_seat_without_systemd_service(tmp_path: Path) -> None:
    home = tmp_path / "home"
    core = home / "cards/c4e7a9b2/core.json"
    core.parent.mkdir(parents=True)
    core.write_text('{"id":"c4e7a9b2"}\n')
    revision = hashlib.sha256(core.read_bytes()).hexdigest()
    activation = {
        "schema": "skfleet.niobe-activation/v1",
        "state": "active",
        "seat": "niobe",
        "decision_id": "casey-niobe",
        "authorized_by": "casey",
        "card_id": "c4e7a9b2",
        "card_revision": revision,
        "host": "chiap08",
        "live_unit": "skfleet-niobe-live.timer",
        "product_scope": ["skcapstone", "skdashboard", "skworld"],
        "card_label": "seat-niobe",
        "allowed_actions": ["claim", "release", "launch", "stop", "reassign"],
        "denied_actions": ["merge", "deploy", "application_actuation", "external_dispatch"],
        "expires_at": "2099-10-06T22:00:00+00:00",
        "rollback": {
            "owner": "casey",
            "action": "disable_skfleet-niobe-live.timer_enable_skfleet-niobe-shadow.timer",
        },
    }
    path = home / "coordination/niobe-activation.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(activation))
    environment = {
        "SKFLEET_NIOBE_ACTIVATION": str(path),
        "INVOCATION_ID": "a" * 32,
        "SKAGENT": "niobe",
        "SKCAPSTONE_AGENT": "niobe",
    }

    with pytest.raises(FanoutBoundaryError, match="activation is missing"):
        resolve_niobe_runtime_identity(home, environ={}, hostname="chiap08", cgroup_text="")
    with pytest.raises(FanoutBoundaryError, match="host is not authorized"):
        resolve_niobe_runtime_identity(
            home,
            environ=environment,
            hostname="chiap03",
            cgroup_text="0::/skfleet-niobe-live.service",
        )
    with pytest.raises(FanoutBoundaryError, match="agent identity is mismatched"):
        resolve_niobe_runtime_identity(
            home,
            environ={**environment, "SKAGENT": "link"},
            hostname="chiap08",
            cgroup_text="0::/skfleet-niobe-live.service",
        )
    with pytest.raises(FanoutBoundaryError, match="no verified systemd invocation"):
        resolve_niobe_runtime_identity(
            home,
            environ={
                "SKFLEET_NIOBE_ACTIVATION": str(path),
                "SKAGENT": "niobe",
                "SKCAPSTONE_AGENT": "niobe",
            },
            hostname="chiap08",
            cgroup_text="0::/skfleet-niobe-live.service",
        )
    with pytest.raises(FanoutBoundaryError, match="outside its authorized service"):
        resolve_niobe_runtime_identity(
            home, environ=environment, hostname="chiap08", cgroup_text="0::/user.slice"
        )
    assert (
        resolve_niobe_runtime_identity(
            home,
            environ=environment,
            hostname="chiap08",
            cgroup_text="0::/skfleet-niobe-live.service",
        ).decision_id
        == "casey-niobe"
    )
    core.write_text('{"id":"changed"}\n')
    with pytest.raises(FanoutBoundaryError, match="activation card is stale"):
        resolve_niobe_runtime_identity(
            home,
            environ=environment,
            hostname="chiap08",
            cgroup_text="0::/skfleet-niobe-live.service",
        )
    activation["card_revision"] = hashlib.sha256(core.read_bytes()).hexdigest()
    activation["expires_at"] = "2000-01-01T00:00:00+00:00"
    path.write_text(json.dumps(activation))
    with pytest.raises(ValueError, match="activation has expired"):
        resolve_niobe_runtime_identity(
            home,
            environ=environment,
            hostname="chiap08",
            cgroup_text="0::/skfleet-niobe-live.service",
        )


@pytest.mark.parametrize("entrypoint", ["append", "reconcile"])
def test_receipt_mutations_require_verified_niobe_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entrypoint: str
) -> None:
    store, request = _launched_request(tmp_path)
    monkeypatch.setattr(
        "skcapstone.niobe_fanout.resolve_niobe_runtime_identity",
        lambda _home: (_ for _ in ()).throw(FanoutBoundaryError("unauthorized runtime")),
    )

    with pytest.raises(FanoutBoundaryError, match="unauthorized runtime"):
        if entrypoint == "append":
            append_fanout_receipt(
                tmp_path,
                request,
                state="launched",
                claim_owner="worker",
                claim_revision="claim-1",
            )
        else:
            reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())

    assert len(store._read_events("deadbeef")) == 3


@pytest.mark.parametrize("with_request", [False, True])
def test_reconciliation_skips_irrelevant_cards_without_niobe_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_request: bool
) -> None:
    store = _card(tmp_path, card_id="3a11f071")
    if with_request:
        submit_fanout_request(tmp_path, _request(card_id="3a11f071"))
    store.append_event("3a11f071", "void", "jarvis", reason="superseded review")
    monkeypatch.setattr(
        "skcapstone.niobe_fanout.resolve_niobe_runtime_identity",
        lambda _home: (_ for _ in ()).throw(FanoutBoundaryError("activation missing")),
    )

    assert (
        reconcile_fanout_receipt(tmp_path, "3a11f071", live_sessions=set(), live_units=set())
        is None
    )


def test_reconciliation_rejects_forged_writer_receipt(tmp_path: Path) -> None:
    store = _card(tmp_path)
    request = submit_fanout_request(tmp_path, _request())
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    store.append_event(
        "deadbeef",
        "niobe_fanout_receipt",
        "niobe",
        schema="skfleet.niobe-fanout-receipt/v1",
        request_id=request.request_id,
        source_head=request.source_head,
        state="launched",
        claim_owner="worker",
        claim_revision="claim-1",
        process={
            "request_id": request.request_id,
            "card_id": "deadbeef",
            "owner": "worker",
            "claim_revision": "claim-1",
            "session_id": "session-1",
            "unit": "unit-1.service",
        },
    )

    with pytest.raises(FanoutBoundaryError, match="authority is stale or forged"):
        reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())


def test_reconciliation_rejects_replayed_runtime_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launched_request(tmp_path)
    monkeypatch.setattr(
        "skcapstone.niobe_fanout.resolve_niobe_runtime_identity",
        lambda _home: NiobeRuntimeIdentity("decision", "e" * 64, "chiap08", "unit", "b" * 32),
    )

    with pytest.raises(FanoutBoundaryError, match="authority is stale or forged"):
        reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())


def test_exact_receipt_replay_is_idempotent(tmp_path: Path) -> None:
    store = _card(tmp_path)
    request = submit_fanout_request(tmp_path, _request())

    first = append_fanout_receipt(tmp_path, request, state="materialized")
    second = append_fanout_receipt(tmp_path, request, state="materialized")

    assert second["event_id"] == first["event_id"]
    assert (
        sum(row.get("action") == "niobe_fanout_receipt" for row in store._read_events("deadbeef"))
        == 1
    )


def test_requesting_seat_identity_rejects_caller_spoof(tmp_path: Path) -> None:
    control = tmp_path / "coordination/seat-control-plane.json"
    control.parent.mkdir(parents=True)
    control.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "active_host": "chiap08",
                "revision": "control-1",
                "seats": {
                    "link": ["chiap08"],
                    "mero": ["chiap08"],
                    "seraph": ["chiap08"],
                    "niobe": ["chiap08"],
                    "tank": ["chiap08"],
                    "atlas": ["chiap08"],
                },
            }
        )
    )
    environment = {
        "SKAGENT": "link",
        "SKCAPSTONE_AGENT": "link",
        "INVOCATION_ID": "a" * 32,
    }
    with pytest.raises(FanoutBoundaryError, match="outside its authorized service"):
        resolve_requesting_seat_identity(
            tmp_path,
            environ=environment,
            hostname="chiap08",
            cgroup_text="0::/skfleet-mero.service",
        )
    assert (
        resolve_requesting_seat_identity(
            tmp_path,
            environ=environment,
            hostname="chiap08",
            cgroup_text="0::/skfleet-link.service",
        ).seat
        == "link"
    )


@pytest.mark.parametrize(
    ("lifecycle", "owner", "revision", "alive", "expected"),
    [
        ("doing", "worker", "claim-1", True, "occupied"),
        ("doing", "worker", "claim-1", False, "stopped"),
        ("doing", None, None, False, "released"),
        ("doing", "worker-b", "claim-2", True, "occupied"),
        ("done", None, None, False, "retired"),
    ],
)
def test_crash_release_reassignment_and_retirement_truth(
    lifecycle: str,
    owner: str | None,
    revision: str | None,
    alive: bool,
    expected: str,
) -> None:
    assert (
        reconcile_runtime_state(
            lifecycle=lifecycle,
            claim_owner=owner,
            claim_revision=revision,
            process_alive=alive,
            prior_owner="worker",
            prior_revision="claim-1",
        )
        == expected
    )


def test_reconciler_emits_stop_and_terminal_retirement_receipts(tmp_path: Path) -> None:
    store = _card(tmp_path)
    request = submit_fanout_request(tmp_path, _request())
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    append_fanout_receipt(
        tmp_path,
        request,
        state="launched",
        claim_owner="worker",
        claim_revision="claim-1",
        process={
            "request_id": request.request_id,
            "card_id": "deadbeef",
            "owner": "worker",
            "claim_revision": "claim-1",
            "session_id": "worker-session",
            "unit": "worker-unit.service",
        },
    )

    stopped = reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())
    assert stopped is not None and stopped["state"] == "stopped"
    assert (
        reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())
        is None
    )

    store.append_event("deadbeef", "complete", "worker")
    retired = reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())
    assert retired is not None and retired["state"] == "retired"


def _launched_request(tmp_path: Path) -> tuple[CardStore, LifecycleFanoutRequest]:
    store = _card(tmp_path)
    request = submit_fanout_request(tmp_path, _request())
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    append_fanout_receipt(
        tmp_path,
        request,
        state="launched",
        claim_owner="worker",
        claim_revision="claim-1",
        process={
            "request_id": request.request_id,
            "card_id": "deadbeef",
            "owner": "worker",
            "claim_revision": "claim-1",
            "session_id": "session-1",
            "unit": "unit-1.service",
        },
    )
    return store, request


def test_live_exact_old_worker_cannot_be_released_after_card_claim_disappears(
    tmp_path: Path,
) -> None:
    store, _request_value = _launched_request(tmp_path)
    store.append_event(
        "deadbeef",
        "release_claim",
        "niobe",
        released_owner="worker",
        expected_claim_revision="claim-1",
    )

    receipt = reconcile_fanout_receipt(
        tmp_path,
        "deadbeef",
        live_sessions={"session-1"},
        live_units=set(),
    )

    assert receipt is not None and receipt["state"] == "occupied"
    assert receipt["claim_owner"] == "worker"
    assert pending_fanout_request(tmp_path, "deadbeef") is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "f" * 64),
        ("card_id", "feedface"),
        ("owner", "other-worker"),
        ("claim_revision", "claim-2"),
        ("session_id", ""),
        ("unit", ""),
    ],
)
def test_recovery_rejects_mismatched_or_incomplete_worker_tuple(
    tmp_path: Path, field: str, value: str
) -> None:
    store, request = _launched_request(tmp_path)
    rows = store._read_events("deadbeef")
    launched = next(row for row in reversed(rows) if row.get("state") == "launched")
    process = dict(launched["process"])
    process[field] = value
    receipt = {
        "request_id": request.request_id,
        "source_head": request.source_head,
        "state": "occupied",
        "claim_owner": "worker",
        "claim_revision": "claim-1",
        "process": process,
        "authority_decision": "decision",
        "authority_revision": "f" * 64,
        "authority_host": "chiap08",
        "authority_unit": "unit",
        "authority_invocation": "a" * 32,
    }
    store.append_event(
        "deadbeef",
        "niobe_fanout_receipt",
        "niobe",
        schema="skfleet.niobe-fanout-receipt/v1",
        **receipt,
    )

    with pytest.raises(FanoutBoundaryError, match="worker tuple is stale or incomplete"):
        reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())


def test_stale_prior_request_receipt_cannot_authorize_current_retry(tmp_path: Path) -> None:
    store, old = _launched_request(tmp_path)
    newer = _request(source_head="b" * 40).normalized()
    store.append_event(
        "deadbeef",
        "niobe_fanout_request",
        "link",
        **{
            key: value
            for key, value in newer.as_event().items()
            if key not in {"card_id", "requester"}
        },
    )
    store.append_event(
        "deadbeef",
        "niobe_fanout_receipt",
        "niobe",
        schema="skfleet.niobe-fanout-receipt/v1",
        request_id=old.request_id,
        source_head=old.source_head,
        state="released",
        claim_owner="worker",
        claim_revision="claim-1",
        process={"alive": False},
    )

    assert (
        reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())
        is None
    )


def test_exact_terminal_worker_allows_stopped_then_released_retry(tmp_path: Path) -> None:
    store, request = _launched_request(tmp_path)
    stopped = reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())
    assert stopped is not None and stopped["state"] == "stopped"
    store.append_event(
        "deadbeef",
        "release_claim",
        "niobe",
        released_owner="worker",
        expected_claim_revision="claim-1",
    )
    released = reconcile_fanout_receipt(
        tmp_path, "deadbeef", live_sessions=set(), live_units=set()
    )
    assert released is not None and released["state"] == "released"
    assert pending_fanout_request(tmp_path, "deadbeef") == request


def test_reconciler_ignores_receipt_from_stale_request(tmp_path: Path) -> None:
    store = _card(tmp_path)
    old = submit_fanout_request(tmp_path, _request())
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    append_fanout_receipt(
        tmp_path, old, state="launched", claim_owner="worker", claim_revision="claim-1"
    )
    newer = _request(source_head="b" * 40).normalized()
    store.append_event(
        "deadbeef",
        "niobe_fanout_request",
        "link",
        **{
            key: value
            for key, value in newer.as_event().items()
            if key not in {"card_id", "requester"}
        },
    )

    assert (
        reconcile_fanout_receipt(tmp_path, "deadbeef", live_sessions=set(), live_units=set())
        is None
    )


def test_runtime_receipt_rejects_stale_claim_generation(tmp_path: Path) -> None:
    store = _card(tmp_path)
    request = submit_fanout_request(tmp_path, _request())
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-2")

    with pytest.raises(FanoutBoundaryError, match="claim generation is stale"):
        append_fanout_receipt(
            tmp_path,
            request,
            state="launched",
            claim_owner="worker",
            claim_revision="claim-1",
        )


def test_production_dispatcher_uses_one_niobe_path_before_claim_and_launch() -> None:
    source = (Path(__file__).parents[1] / "scripts" / "fleet" / "skfleet-rotate.py").read_text(
        encoding="utf-8"
    )
    loop = source[source.index("for _LANE,(_,_,cid,core,_labels,_nb) in picks:") :]
    authorize = loop.index("pending_fanout_request(")
    materialize = loop.index('state="materialized"')
    claim = loop.index('claim=subprocess.run([SKC,"coord","claim",cid')
    claimed = loop.index('state="claimed"')
    launch = loop.index("_worker_launch_command(unit,workspace,inner)")
    receipt = loop.index('state="launched" if ok else "launch_failed"')
    assert authorize < materialize < claim < claimed < launch < receipt
    assert 'actor="niobe"' not in loop[authorize:materialize]
