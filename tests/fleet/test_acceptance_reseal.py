"""Real governed replay after the five interrupted source PASS binding shapes."""

from pathlib import Path

import pytest

from skcapstone.card import CardEvent, CardEventLog
from skcapstone.fleet import acceptance_reseal as recovery
from skcapstone.fleet import production_review_finish as finish
from skcapstone.historical_pr import history_revision
from skcapstone.seraph_review_contracts import _digest
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_source_bundle import source  # noqa: F401


@pytest.fixture
def interrupted(pair, request):  # noqa: F811
    """Reach a real lost PASS reply, then apply the exact operator correction shape."""
    home, directory, context, store, calls, receipt = pair
    shape = getattr(request, "param", "7907fdaf")
    producer = context["source"]
    stale = {}
    if shape != "8f5077b2":
        stale["pr"] = (
            "https://github.com/team/old/pull/147"
            if shape != "a4ed7c20"
            else "https://example.invalid/source/compare/main...branch"
        )
    if shape != "a4ed7c20":
        stale["commit"] = "2bb17f0"
    # Old links precede the typed candidate generation and are hidden after PASS.
    for key, value in stale.items():
        CardEventLog(home).append(
            CardEvent(
                card_id=producer["card"],
                action="link",
                writer="old-producer",
                ts="2000-01-01T00:00:00+00:00",
                link_key=key,
                link_value=value,
            )
        )
    context["source"]["revision"] = finish.native_state(home, producer["card"])["revision"]
    finish.once(directory / "context.json", context)

    def lose_step_eight_reply(path, args):
        reply = finish.native_command(path, args)
        if (directory / "step-08.intent.json").exists() and not (
            directory / "step-08.ack.json"
        ).exists():
            raise RuntimeError("simulated lost native reply")
        return reply

    with pytest.raises(RuntimeError, match="simulated lost native reply"):
        finish.finish_pair(
            home, directory, context, guard=lambda: None, command=lose_step_eight_reply
        )
    step = finish.read_json(directory / "step-08.intent.json")
    assert not (directory / "step-08.ack.json").exists()
    changes = []
    for key, value in stale.items():
        after = "" if key == "pr" else producer["head"]
        finish.native_command(
            home, ["link", producer["card"], key, after, "--agent", "lumina-nor"]
        )
        event = list(recovery._rows(store, producer["card"]))[-1]
        changes.append(dict(event_sha256=_digest(event), key=key, before=value, after=after))
    event = next(
        e for e in store._read_events(producer["card"]) if e.get("transition_id") == _digest(step)
    )
    guards = dict(
        context_sha256=recovery._file_hash(directory / "context.json"),
        finish_sha256=recovery._file_hash(directory / "finish-intent.json"),
        step_sha256=recovery._file_hash(directory / "step-08.intent.json"),
        event_sha256=_digest(event),
        source_revision=finish.native_state(home, producer["card"])["revision"],
        review_revision=finish.native_state(home, context["review"]["card"])["revision"],
        source_history=history_revision(store, producer["card"]),
        review_history=history_revision(store, context["review"]["card"]),
        claim_revision=producer["claim"],
    )
    return pair, changes, guards


def reseal(interrupted):
    """Exercise the same coordinator operation without substituting reviewer identity."""
    (home, directory, context, _, _, _), changes, guards = interrupted
    # Unit fixture uses a short test directory; production uses review_directory.
    return recovery.reseal(
        home, context["review"]["card"], context["review"]["claim"], "lumina-nor", changes, guards
    )


@pytest.fixture(autouse=True)
def retained_directory(monkeypatch, tmp_path):
    """Use the fixture's private journal, never a live card or acceptance directory."""
    monkeypatch.setattr(
        "skcapstone.fleet.production_acceptance.review_directory", lambda *a: tmp_path / "finish"
    )


@pytest.mark.host_systemd
@pytest.mark.parametrize(
    "interrupted", ["7907fdaf", "add19bf8", "bb6da93e", "a4ed7c20", "8f5077b2"], indirect=True
)
def test_five_binding_histories_recover_without_second_pass_or_review_completion(interrupted):
    (home, directory, context, store, _, _), _, _ = interrupted
    before = {r: store._read_events(context[r]["card"]) for r in ("source", "review")}
    result = reseal(interrupted)
    assert reseal(interrupted) == result  # lost operator CLI reply
    assert not (directory / "step-08.ack.json").exists()
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]
    assert reseal(interrupted) == result
    for role in ("source", "review"):
        events = store._read_events(context[role]["card"])
        assert len([e for e in events if e["action"] == "complete"]) == 1
        assert [e for e in events if e.get("link_key") == "verdict"] == [
            e for e in before[role] if e.get("link_key") == "verdict"
        ]
    assert not (directory / "step-08.ack.json").exists()
    assert (directory / "resealed/step-11.ack.json").exists()


@pytest.mark.host_systemd
@pytest.mark.parametrize(
    "change",
    [
        "claim",
        "foreign",
        "history",
        "review",
        "receipt",
        "source-report",
        "decision",
        "review-repinned",
    ],
)
def test_changed_or_foreign_history_refuses(interrupted, change):
    (home, directory, context, store, _, _), changes, guards = interrupted
    producer = context["source"]["card"]
    if change == "claim":
        store.append_event(producer, "claim", "replacement", owner="replacement")
    elif change in {"foreign", "history", "review", "review-repinned"}:
        card = context["review"]["card"] if change.startswith("review") else producer
        store.append_event(card, "note", "foreign", note="changed history")
        # Even a caller repinning invisible foreign history cannot bless it.
        if change == "foreign":
            guards["source_history"] = history_revision(store, card)
        if change == "review-repinned":
            guards["review_history"] = history_revision(store, card)
    elif change == "receipt":
        guards["event_sha256"] = "0" * 64
    elif change == "source-report":
        Path(context["source"]["evidence_path"]).write_text("changed")
    else:
        Path(context["review"]["decision_path"]).write_text("changed")
    before = store._read_events(producer)
    with pytest.raises(ValueError):
        reseal(interrupted)
    assert store._read_events(producer) == before
    assert not (directory / "reseal.json").exists()


@pytest.mark.host_systemd
@pytest.mark.parametrize("boundary", ["receipt", "native", "link", "complete"])
def test_crash_recovery_and_lost_replies(interrupted, monkeypatch, boundary):
    (home, directory, context, store, _, _), _, _ = interrupted
    if boundary in {"receipt", "native"}:
        original = recovery.once if boundary == "receipt" else type(store).append_event
        crashed = []

        def crash(*args, **kwargs):
            result = original(*args, **kwargs)
            if not crashed:
                crashed.append(True)
                raise OSError("lost reply")
            return result

        with monkeypatch.context() as m:
            m.setattr(
                recovery if boundary == "receipt" else type(store),
                "once" if boundary == "receipt" else "append_event",
                crash,
            )
            with pytest.raises(OSError, match="lost reply"):
                reseal(interrupted)
        reseal(interrupted)
    else:
        reseal(interrupted)

        def crash(home, args):
            result = finish.native_command(home, args)
            if args[0] == boundary:
                raise OSError("lost reply")
            return result

        with pytest.raises(OSError, match="lost reply"):
            finish.finish_pair(home, directory, context, guard=lambda: None, command=crash)
    assert finish.finish_pair(home, directory, context, guard=lambda: None)["accepted"]


@pytest.mark.host_systemd
def test_history_drift_after_receipt_refuses_even_when_fold_unchanged(interrupted):
    (home, directory, context, store, _, _), _, _ = interrupted
    reseal(interrupted)
    store.append_event(context["source"]["card"], "note", "foreign", note="late event")
    with pytest.raises(ValueError, match="history changed"):
        finish.finish_pair(home, directory, context, guard=lambda: None)


@pytest.mark.host_systemd
def test_every_explicit_guard_is_required_and_exact(interrupted):
    """No automatically refreshed guard may authorize a different preimage."""
    (home, _, context, store, _, _), changes, guards = interrupted
    before = store._read_events(context["source"]["card"])
    for key in guards:
        changed = dict(guards, **{key: "0" * len(guards[key])})
        with pytest.raises(ValueError):
            recovery.reseal(
                home,
                context["review"]["card"],
                context["review"]["claim"],
                "lumina-nor",
                changes,
                changed,
            )
    assert store._read_events(context["source"]["card"]) == before
