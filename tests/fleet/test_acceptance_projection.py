"""Preserve sealed acceptance across one exact legacy/native PASS projection."""

import json

import pytest

from skcapstone.fleet import production_review_finish as finish
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway, card_revision
from skcapstone.seraph_review_contracts import _digest
from tests.fleet.test_production_acceptance import stopped_pair  # noqa: F401
from tests.fleet.test_production_review_finish import pair, source  # noqa: F401


def legacy_revision(home, store, item):
    """Hash the supported legacy projection of the real native PASS event."""
    snapshot = LiveCardStoreGateway(home).read_card(item["card"])
    fields = (
        "repository",
        "number",
        "head_sha",
        "producer_identity",
        "reviewer_identity",
        "unresolved_review",
        "candidate_evidence_sha256",
    )
    bindings = {k: getattr(snapshot, k) for k in fields}
    outcomes = [e for e in store._read_events(item["card"]) if e.get("link_key") == "verdict"]
    assert len(outcomes) == 1
    projection = {
        k: outcomes[0][k] for k in ("ts", "writer", "seq", "action", "link_key", "link_value")
    }
    projection["origin"] = "legacy-overlay"
    return _digest(
        {
            "card": card_revision(store.fold(item["card"])),
            "bindings": bindings,
            "outcome": projection,
        }
    )


@pytest.mark.host_systemd
@pytest.mark.parametrize("interrupt", [False, True])
def test_exact_legacy_projection_finishes_and_replays(pair, interrupt):  # noqa: F811
    home, directory, context, store, _, _ = pair
    current = context["review"]["revision"]
    context["review"]["revision"] = legacy_revision(home, store, context["review"])
    assert context["review"]["revision"] != current
    finish.once(directory / "context.json", context)
    raw = (directory / "context.json").read_bytes()
    if interrupt:

        def interrupted(home, args):
            finish.native_command(home, args)
            raise RuntimeError("lost CLI acknowledgement")

        with pytest.raises(RuntimeError, match="lost CLI"):
            finish.finish_pair(home, directory, context, guard=lambda: None, command=interrupted)
    result = finish.finish_pair(home, directory, context, guard=lambda: None)
    assert result["accepted"]
    assert (directory / "context.json").read_bytes() == raw
    intent = finish.read_json(directory / "finish-intent.json")
    assert intent["review_revision"] == current
    for role in ("source", "review"):
        assert store.fold(context[role]["card"]).status.value == "done"
        assert not any(
            e["action"] == "release_claim" for e in store._read_events(context[role]["card"])
        )
    assert finish.finish_pair(home, directory, context, guard=lambda: None) == result


@pytest.mark.host_systemd
@pytest.mark.parametrize("mutation", ["label", "criteria", "owner", "wrong-pin", "unbound-pass"])
def test_projection_does_not_accept_material_changes(pair, mutation):  # noqa: F811
    home, directory, context, store, _, _ = pair
    item = context["review"]
    item["revision"] = legacy_revision(home, store, item)
    if mutation == "label":
        store.append_event(item["card"], "add_label", "operator", label="new-scope")
    elif mutation == "criteria":
        store.append_event(item["card"], "amend_criteria", "operator", criteria=["new"])
    elif mutation == "owner":
        item["owner"] = "another-reviewer"
    elif mutation == "wrong-pin":
        item["revision"] = "0" * 64
    else:
        store.append_event(
            item["card"], "link", item["owner"], link_key="verdict", link_value="PASS"
        )
    before = store._read_events(item["card"])
    with pytest.raises(ValueError):
        finish.finish_pair(home, directory, context, guard=lambda: None)
    assert store._read_events(item["card"]) == before
    assert not (directory / "finish-intent.json").exists()


@pytest.mark.host_systemd
@pytest.mark.parametrize("ancillary_refresh", [True, False])
def test_controller_keeps_context_and_waits_for_real_tests(
    stopped_pair,  # noqa: F811
    monkeypatch,
    ancillary_refresh,
):
    import sys

    from skcapstone.fleet import production_acceptance as acceptance
    from skcapstone.fleet import production_test_profile

    home, policy, review, _, _, store = stopped_pair
    context = acceptance.collect(
        home,
        policy,
        review["card"],
        review["claim"],
        process_check=lambda card: {"sessions": [], "units": []},
    )
    context["review"]["revision"] = legacy_revision(home, store, context["review"])
    directory = acceptance.review_directory(home, review["card"], review["claim"])
    path = directory / "context.json"
    path.write_text(json.dumps(context))
    original = path.read_bytes()
    calls = []
    if not ancillary_refresh:
        # Legacy cards can lack metadata required by the separate receipt refresh.
        monkeypatch.setattr(acceptance, "_review_context_still_bound", lambda *a: False)
    monkeypatch.setattr(production_test_profile, "seal_candidate", lambda *a: calls.append("seal"))
    tests = sys.modules["skcapstone.fleet.production_tests"]
    monkeypatch.setattr(tests, "run_or_read_tests", lambda *a: None, raising=False)
    monkeypatch.setattr(tests, "admission_refusal", lambda *a: None, raising=False)
    results = acceptance.reconcile(
        home, policy, process_check=lambda card: {"sessions": [], "units": []}
    )
    assert results[0]["state"] == "awaiting-trusted-tests", results
    assert calls == ["seal"]
    assert path.read_bytes() == original
    assert not (directory / "finish-intent.json").exists()
    assert store.fold(review["card"]).owner == review["owner"]


@pytest.mark.host_systemd
@pytest.mark.parametrize("change", ["scope", "claim"])
def test_early_projection_gate_refuses_changed_review(pair, monkeypatch, change):  # noqa: F811
    """An exact projection cannot replace current claim and scope proof."""
    from skcapstone.fleet import production_acceptance as acceptance

    home, directory, context, store, _, _ = pair
    item = context["review"]
    item["revision"] = legacy_revision(home, store, item)
    if change == "scope":
        store.append_event(item["card"], "add_label", "operator", label="changed-scope")
    else:
        item["claim"] = "0" * 32
    monkeypatch.setattr(acceptance, "_review_context_still_bound", lambda *a: False)
    before = store._read_events(item["card"])
    with pytest.raises(acceptance.ReviewEvidenceError, match="native pair changed"):
        acceptance.acceptance_review_revision(home, directory, context)
    assert store._read_events(item["card"]) == before
    assert not (directory / "review-pair-refresh.json").exists()
