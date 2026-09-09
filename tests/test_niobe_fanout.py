"""Production contract tests for role-bounded Niobe fan-out."""

from pathlib import Path

import pytest
from skcoord.card_store import CardCore, CardStore

from skcapstone.niobe_fanout import (
    FanoutBoundaryError,
    LifecycleFanoutRequest,
    append_fanout_receipt,
    pending_fanout_request,
    reconcile_fanout_receipt,
    reconcile_runtime_state,
    submit_fanout_request,
)

HEAD = "a" * 40


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

    assert pending_fanout_request(tmp_path, "deadbeef", actor="niobe") == request
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
        process={"unit": "skfleet-worker-codex-deadbeef.service", "alive": True},
    )

    assert pending_fanout_request(tmp_path, "deadbeef", actor="niobe") is None
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
        == 3
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"requester": "jarvis"}, "only Link or Mero"),
        ({"requester": "mero", "route": "integration"}, "requester role"),
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


def test_request_rejects_claimed_card_and_non_niobe_actuator(tmp_path: Path) -> None:
    store = _card(tmp_path)
    with pytest.raises(FanoutBoundaryError, match="only Niobe"):
        pending_fanout_request(tmp_path, "deadbeef", actor="jarvis")
    store.append_event("deadbeef", "claim", "worker", owner="worker", claim_revision="claim-1")
    with pytest.raises(FanoutBoundaryError, match="unclaimed"):
        submit_fanout_request(tmp_path, _request())


def test_source_head_dedupes_across_cards_and_ambiguous_requests_fail(tmp_path: Path) -> None:
    store = _card(tmp_path)
    _card(tmp_path, "feedface")
    first = submit_fanout_request(tmp_path, _request())
    append_fanout_receipt(
        tmp_path,
        first,
        state="launched",
        claim_owner="worker-a",
        claim_revision="claim-a",
    )
    submit_fanout_request(tmp_path, _request("feedface"))
    assert pending_fanout_request(tmp_path, "feedface", actor="niobe") is None

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
        pending_fanout_request(tmp_path, "deadbeef", actor="niobe")


@pytest.mark.parametrize(
    ("lifecycle", "owner", "revision", "alive", "expected"),
    [
        ("doing", "worker", "claim-1", True, "occupied"),
        ("doing", "worker", "claim-1", False, "stopped"),
        ("doing", None, None, False, "released"),
        ("doing", "worker-b", "claim-2", True, "reassigned"),
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
    )

    stopped = reconcile_fanout_receipt(tmp_path, "deadbeef", process_alive=False)
    assert stopped is not None and stopped["state"] == "stopped"
    assert reconcile_fanout_receipt(tmp_path, "deadbeef", process_alive=False) is None

    store.append_event("deadbeef", "complete", "worker")
    retired = reconcile_fanout_receipt(tmp_path, "deadbeef", process_alive=False)
    assert retired is not None and retired["state"] == "retired"


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
    assert 'actor="niobe"' in loop[authorize:materialize]
