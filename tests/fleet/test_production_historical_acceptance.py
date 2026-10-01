"""Historical acceptance is immutable native evidence, not current runtime admission."""

import copy
import json
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_review_finish as finish
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_tests as native
from tests.fleet.test_production_tests import receipt_fixture, setup  # noqa: F401


@pytest.fixture
def accepted(setup, monkeypatch):  # noqa: F811
    s = setup
    receipt_fixture(s, monkeypatch)
    audit = native.validate_test_receipt(s.home, s.binding, s.workspace)
    directory = s.home / "evidence/accepted-pair"
    directory.mkdir(parents=True, mode=0o700)
    context = {"controller": "test-authority", "test_binding": s.binding,
               "source_workspace": str(s.workspace)}
    for role, card in (("source", s.binding["source_card"]), ("review", "abcd1234")):
        path = directory / (role + ".md")
        path.write_text(role)
        path.chmod(0o600)
        context[role] = {"card": card, "owner": role + "-worker", "claim": role + "-claim",
                         "revision": role + "-before", "evidence_path": str(path),
                         "evidence_sha256": finish._sha(path.read_bytes())}
    decision = directory / "decision.json"
    decision.write_text('{"verdict":"PASS"}')
    decision.chmod(0o600)
    context["review"].update(decision_path=str(decision),
        decision_sha256=finish._sha(decision.read_bytes()), proposal={"verdict": "PASS"})
    context["source"].update(repository="https://example.org/public.git",
                              ref="refs/heads/candidate", head=s.binding["source_head"],
                              tree=s.binding["source_tree"], owner=s.binding["source_owner"],
                              claim=s.binding["source_claim_revision"])
    binding = {"controller": context["controller"], "context_sha256": finish._digest(context),
               "test_receipt": audit}
    finish.once(directory / "finish-intent.json", binding)
    expected = {role: context[role]["revision"] for role in ("source", "review")}
    for index, (role, action, key, value) in enumerate(finish.steps(context, audit)):
        item = context[role]
        step = {"binding": finish._digest(binding), "index": index, "role": role,
                "action": action, "key": key, "value": value, "before": expected,
                "card": item["card"], "owner": item["owner"], "claim": item["claim"],
                "native_before": {"fixture": True}}
        finish.once(directory / f"step-{index:02d}.intent.json", step)
        expected = dict(expected, **{role: f"accepted-{index}"})
        finish.once(directory / f"step-{index:02d}.ack.json",
                    {"step_sha256": finish._digest(step), "after": expected})
    historical = {"schema": "skfleet.source-review-acceptance/v1",
                  "controller": context["controller"], "context_sha256": finish._digest(context),
                  "revisions": expected, "source_card": context["source"]["card"],
                  "review_card": context["review"]["card"], "accepted": True,
                  "test_receipt": audit, "governed_pr_ci": False}
    finish.once(directory / "finished.json", historical)
    states = {context[role]["card"]: {"revision": expected[role], "status": "done",
                                     "owner": None, "claim_revision": None}
              for role in ("source", "review")}
    rows = {card: SimpleNamespace(links={"test_acceptance": json.dumps(audit)}) for card in states}
    monkeypatch.setattr(finish, "CardStore", lambda home: SimpleNamespace(fold=rows.get))
    return SimpleNamespace(s=s, directory=directory, context=context, historical=historical,
                           states=states, rows=rows)


def replay(a):
    def forbidden(*args, **kwargs):
        pytest.fail("historical acceptance attempted a mutation or live execution guard")

    return finish.finish_pair(a.s.home, a.directory, a.context, guard=forbidden,
                              command=forbidden, inspect=lambda home, card: a.states[card])


def test_accepted_pair_survives_runtime_upgrade_without_any_write(accepted, monkeypatch):
    a = accepted
    before = {p: p.read_bytes() for p in a.s.home.rglob("*") if p.is_file()}
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "0" * 64)
    with pytest.raises(native.TestEvidenceError, match="stale"):
        native.validate_test_receipt(a.s.home, a.s.binding, a.s.workspace)
    monkeypatch.setattr(finish, "once", lambda *args: pytest.fail("historical write attempted"))
    assert replay(a) == a.historical
    assert {p: p.read_bytes() for p in a.s.home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("target", ["plan", "receipt", "log", "junit", "report",
                                    "intent", "ack", "native-proof", "native-revision",
                                    "native-owner", "context"])
def test_changed_historical_proof_never_accepts(accepted, target):
    a = accepted
    paths = {"plan": a.s.plan_path, "receipt": a.s.directory / "receipt.json",
             "log": a.s.directory / "pytest.log", "junit": a.s.directory / "pytest.xml",
             "report": a.directory / "source.md", "intent": a.directory / "finish-intent.json",
             "ack": a.directory / "step-00.ack.json"}
    if target in paths:
        paths[target].write_bytes(paths[target].read_bytes() + b" ")
        if target in {"intent", "ack"}:
            paths[target].write_text("{}")
    elif target == "native-proof":
        next(iter(a.rows.values())).links["test_acceptance"] = "{}"
    elif target == "native-revision":
        next(iter(a.states.values()))["revision"] = "changed"
    elif target == "native-owner":
        next(iter(a.states.values()))["owner"] = "new-owner"
    else:
        a.context = copy.deepcopy(a.context)
        a.context["controller"] = "forged"
    with pytest.raises((ValueError, KeyError)):
        replay(a)


@pytest.mark.parametrize("missing", ["finish-intent.json", "step-00.intent.json",
                                     "step-00.ack.json"])
def test_incomplete_history_never_replays_a_native_write(accepted, missing):
    a = accepted
    (a.directory / missing).unlink()
    with pytest.raises((ValueError, OSError)):
        replay(a)


def test_unfinished_pair_still_requires_current_runtime(accepted, monkeypatch):
    a = accepted
    (a.directory / "finished.json").unlink()
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "0" * 64)
    with pytest.raises(native.TestEvidenceError, match="stale"):
        replay(a)
