"""Released custody remains usable across the real guarded authority CLI."""

import pytest

from skcapstone.fleet import production_review_finish as finish
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_released_source_acceptance import release
from tests.fleet.test_source_bundle import source  # noqa: F401


@pytest.mark.host_systemd
@pytest.mark.parametrize("applied", [False, True])
@pytest.mark.parametrize("later", [None, "link", "outcome"])
def test_pending_source_link_accepts_only_release_and_finishes_once(
    pair, applied, later  # noqa: F811
):
    home, directory, context, store, *_ = pair
    stopped = []

    def command(home, args):
        if args[:3] == ["link", context["source"]["card"], "verdict"] and not stopped:
            stopped.append(True)
            if applied:
                finish.native_command(home, args)
            raise OSError("interrupted source PASS")
        return finish.native_command(home, args)

    with pytest.raises(OSError, match="interrupted source PASS"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    assert store.fold(context["review"]["card"]).status.value == "done"
    release(pair)
    if later:
        store.append_event(
            context["source"]["card"],
            "link",
            "other",
            link_key="verdict" if later == "outcome" else "unrelated",
            link_value="PASS" if later == "outcome" else "changed",
        )
        before = store._read_events(context["source"]["card"])
        with pytest.raises(ValueError):
            finish.finish_pair(home, directory, context, guard=lambda: None)
        assert store._read_events(context["source"]["card"]) == before
        return
    result = finish.finish_pair(home, directory, context, guard=lambda: None)
    assert result["accepted"]
    events = {role: store._read_events(context[role]["card"]) for role in ("source", "review")}
    assert (
        sum(
            e.get("link_key") == "verdict" and e.get("link_value") == "PASS"
            for e in events["source"]
        )
        == 1
    )
    assert all(sum(e["action"] == "complete" for e in rows) == 1 for rows in events.values())
    assert finish.finish_pair(home, directory, context, guard=lambda: None) == result
    assert all(store._read_events(context[role]["card"]) == rows for role, rows in events.items())


@pytest.mark.host_systemd
@pytest.mark.parametrize("boundary", ["link", "complete"])
@pytest.mark.parametrize(
    "change", ["claim", "assign", "head", "tree", "repository", "outcome", "link"]
)
def test_native_boundary_refuses_changes_after_finish_proof(pair, boundary, change):  # noqa: F811
    home, directory, context, store = release(pair)
    card = context["source"]["card"]
    before = []

    def command(home, args):
        if args[:2] == [boundary, card] and not before:
            if change in {"claim", "assign"}:
                store.append_event(card, change, "other", owner="other", claim_revision="f" * 32)
            else:
                key = "verdict" if change == "outcome" else change
                value = "FAIL" if change == "outcome" else "f" * 40
                store.append_event(card, "link", "other", link_key=key, link_value=value)
            before.extend(store._read_events(card))
        return finish.native_command(home, args)

    with pytest.raises(ValueError, match="guarded native acceptance refused"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    assert before and store._read_events(card) == before


@pytest.mark.host_systemd
@pytest.mark.parametrize("boundary", ["link", "complete"])
def test_released_native_write_lost_reply_is_idempotent(pair, boundary):  # noqa: F811
    home, directory, context, store = release(pair)
    card = context["source"]["card"]
    stopped = []

    def command(home, args):
        reply = finish.native_command(home, args)
        if args[:2] == [boundary, card] and not stopped:
            stopped.append(True)
            raise OSError("lost native reply")
        return reply

    with pytest.raises(OSError, match="lost native reply"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    events = store._read_events(card)
    assert sum(e["action"] == "complete" for e in events) == 1
    assert (
        sum(e.get("link_key") == "verdict" and e.get("link_value") == "PASS" for e in events) == 1
    )


@pytest.mark.host_systemd
@pytest.mark.parametrize("boundary", ["link", "complete"])
@pytest.mark.parametrize("mode", ["no-opt-in", "wrong-owner", "wrong-claim", "bad-json"])
def test_released_cli_requires_explicit_exact_binding(pair, boundary, mode):  # noqa: F811
    import json

    home, _, context, store = release(pair)
    item = context["source"]
    args = [boundary, item["card"]]
    if boundary == "link":
        args += ["evidence", item["evidence_path"], "--transition-id", "f" * 64, "--json"]
    else:
        args += [
            "--review-card",
            context["review"]["card"],
            "--expected-review-revision",
            finish.native_state(home, context["review"]["card"])["revision"],
        ]
    args += [
        "--agent",
        item["owner"],
        "--expected-source-revision",
        finish.native_state(home, item["card"])["revision"],
        "--expected-claim-revision",
        item["claim"],
    ]
    binding = dict(item)
    if mode == "wrong-owner":
        binding["owner"] = "other"
    if mode == "wrong-claim":
        binding["claim"] = "f" * 32
    if mode != "no-opt-in":
        args += [
            "--released-source-binding",
            "null" if mode == "bad-json" else json.dumps(binding),
        ]
    before = store._read_events(item["card"])
    with pytest.raises(ValueError, match="guarded native acceptance refused"):
        finish.native_command(home, args)
    assert store._read_events(item["card"]) == before


@pytest.mark.host_systemd
def test_source_released_with_completion_already_pending(pair):  # noqa: F811
    home, directory, context, store, *_ = pair
    card = context["source"]["card"]

    def command(home, args):
        if args[:2] == ["complete", card]:
            raise OSError("completion pending")
        return finish.native_command(home, args)

    with pytest.raises(OSError, match="completion pending"):
        finish.finish_pair(home, directory, context, guard=lambda: None, command=command)
    release(pair)
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    assert sum(e["action"] == "complete" for e in store._read_events(card)) == 1
