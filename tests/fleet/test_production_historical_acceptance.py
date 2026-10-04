"""Historical acceptance is immutable native evidence, not current runtime admission."""

import copy
import json
from types import SimpleNamespace
from xml.etree import ElementTree

import pytest

from skcapstone.fleet import production_review_finish as finish
from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_tests as native
from skcapstone.fleet.production_test_profile import recipe_checks
from tests.fleet.test_production_test_node import specimen
from tests.fleet.test_production_tests import receipt_fixture, setup  # noqa: F401


@pytest.fixture(params=["legacy", "python-profile", "node"])
def accepted(setup, monkeypatch, request):  # noqa: F811
    s = setup
    receipt_fixture(s, monkeypatch)
    audit = native.validate_test_receipt(s.home, s.binding, s.workspace)
    if request.param != "legacy":
        # Synthetic retained history, with every native proof bound below.
        receipt = native.read_json(s.directory / "receipt.json")
        if request.param == "node":
            profile, root = specimen()
            raw_junit = ElementTree.tostring(root)
            junit_name = "vitest.xml"
        else:
            profile = {
                "recipe": {
                    "pytest": dict.fromkeys(native.TEST_FILES, 1),
                    "compile": [],
                    "lint": [],
                    "changelog": False,
                }
            }
            raw_junit = (s.directory / "pytest.xml").read_bytes()
            junit_name = "pytest.xml"
        s.plan.update(profile=profile, checks=recipe_checks(profile["recipe"]))
        s.plan_path.write_text(json.dumps(s.plan))
        s.digest = plan.sha(s.plan_path.read_bytes())
        s.directory = native.run_directory(s.home, s.digest)
        s.directory.mkdir(mode=0o700)
        receipt.update(plan_sha256=s.digest, checks=[])
        for check in s.plan["checks"]:
            raw = b"synthetic retained output\n"
            path = s.directory / (check["id"] + ".log")
            path.write_bytes(raw)
            path.chmod(0o600)
            receipt["checks"].append({**check, "exit_code": 0, "output_sha256": plan.sha(raw)})
        path = s.directory / junit_name
        path.write_bytes(raw_junit)
        path.chmod(0o600)
        receipt.update(
            junit_sha256=plan.sha(raw_junit), counts=plan.junit_counts(raw_junit, profile)
        )
        native.write_once(s.directory / "receipt.json", receipt)
        audit.update(
            plan_sha256=s.digest,
            receipt_path=str(s.directory / "receipt.json"),
            receipt_sha256=plan.sha((s.directory / "receipt.json").read_bytes()),
            checks=receipt["checks"],
            counts=receipt["counts"],
        )
    directory = s.home / "evidence/accepted-pair"
    directory.mkdir(parents=True, mode=0o700)
    context = {
        "controller": "test-authority",
        "test_binding": s.binding,
        "source_workspace": str(s.workspace),
    }
    for role, card in (("source", s.binding["source_card"]), ("review", "abcd1234")):
        path = directory / (role + ".md")
        path.write_text(role)
        path.chmod(0o600)
        context[role] = {
            "card": card,
            "owner": role + "-worker",
            "claim": role + "-claim",
            "revision": role + "-before",
            "evidence_path": str(path),
            "evidence_sha256": finish._sha(path.read_bytes()),
        }
    decision = directory / "decision.json"
    decision.write_text('{"verdict":"PASS"}')
    decision.chmod(0o600)
    context["review"].update(
        decision_path=str(decision),
        decision_sha256=finish._sha(decision.read_bytes()),
        proposal={"verdict": "PASS"},
    )
    context["source"].update(
        repository="https://example.org/public.git",
        ref="refs/heads/candidate",
        head=s.binding["source_head"],
        tree=s.binding["source_tree"],
        owner=s.binding["source_owner"],
        claim=s.binding["source_claim_revision"],
    )
    binding = {
        "controller": context["controller"],
        "context_sha256": finish._digest(context),
        "test_receipt": audit,
    }
    finish.once(directory / "finish-intent.json", binding)
    expected = {role: context[role]["revision"] for role in ("source", "review")}
    for index, (role, action, key, value) in enumerate(finish.steps(context, audit)):
        item = context[role]
        step = {
            "binding": finish._digest(binding),
            "index": index,
            "role": role,
            "action": action,
            "key": key,
            "value": value,
            "before": expected,
            "card": item["card"],
            "owner": item["owner"],
            "claim": item["claim"],
            "native_before": {"fixture": True},
        }
        finish.once(directory / f"step-{index:02d}.intent.json", step)
        expected = dict(expected, **{role: f"accepted-{index}"})
        finish.once(
            directory / f"step-{index:02d}.ack.json",
            {"step_sha256": finish._digest(step), "after": expected},
        )
    historical = {
        "schema": "skfleet.source-review-acceptance/v1",
        "controller": context["controller"],
        "context_sha256": finish._digest(context),
        "revisions": expected,
        "source_card": context["source"]["card"],
        "review_card": context["review"]["card"],
        "accepted": True,
        "test_receipt": audit,
        "governed_pr_ci": False,
    }
    finish.once(directory / "finished.json", historical)
    states = {
        context[role]["card"]: {
            "revision": expected[role],
            "status": "done",
            "owner": None,
            "claim_revision": None,
        }
        for role in ("source", "review")
    }
    rows = {card: SimpleNamespace(links={"test_acceptance": json.dumps(audit)}) for card in states}
    monkeypatch.setattr(finish, "CardStore", lambda home: SimpleNamespace(fold=rows.get))
    return SimpleNamespace(
        s=s, directory=directory, context=context, historical=historical, states=states, rows=rows
    )


def replay(a):
    def forbidden(*args, **kwargs):
        pytest.fail("historical acceptance attempted a mutation or live execution guard")

    return finish.finish_pair(
        a.s.home,
        a.directory,
        a.context,
        guard=forbidden,
        command=forbidden,
        inspect=lambda home, card: a.states[card],
    )


def test_accepted_pair_survives_runtime_upgrade_without_any_write(accepted, monkeypatch):
    a = accepted
    before = {p: p.read_bytes() for p in a.s.home.rglob("*") if p.is_file()}
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "0" * 64)
    if "profile" in a.s.plan:
        with pytest.raises(native.TestEvidenceError):
            native.validate_test_receipt(a.s.home, a.s.binding, a.s.workspace)
    else:
        assert (
            native.validate_test_receipt(a.s.home, a.s.binding, a.s.workspace)
            == a.historical["test_receipt"]
        )
    monkeypatch.setattr(finish, "once", lambda *args: pytest.fail("historical write attempted"))
    assert replay(a) == a.historical
    assert {p: p.read_bytes() for p in a.s.home.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize(
    "target",
    [
        "plan",
        "receipt",
        "log",
        "junit",
        "report",
        "intent",
        "ack",
        "native-proof",
        "native-revision",
        "native-owner",
        "context",
    ],
)
def test_changed_historical_proof_never_accepts(accepted, target):
    a = accepted
    test_id = a.s.plan["checks"][0]["id"]
    paths = {
        "plan": a.s.plan_path,
        "receipt": a.s.directory / "receipt.json",
        "log": a.s.directory / (test_id + ".log"),
        "junit": a.s.directory / (test_id + ".xml"),
        "report": a.directory / "source.md",
        "intent": a.directory / "finish-intent.json",
        "ack": a.directory / "step-00.ack.json",
    }
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


@pytest.mark.parametrize(
    "missing", ["finish-intent.json", "step-00.intent.json", "step-00.ack.json"]
)
def test_incomplete_history_never_replays_a_native_write(accepted, missing):
    a = accepted
    (a.directory / missing).unlink()
    with pytest.raises((ValueError, OSError)):
        replay(a)


def test_unfinished_pair_recovers_only_completed_test(accepted, monkeypatch):
    a = accepted
    (a.directory / "finished.json").unlink()
    monkeypatch.setattr(plan, "runtime_fingerprint", lambda: "0" * 64)
    if "profile" in a.s.plan:
        with pytest.raises(native.TestEvidenceError):
            replay(a)
    else:
        # Every native write was already acknowledged; only the final marker is missing.
        assert replay(a) == a.historical
        assert (a.directory / "finished.json").exists()


@pytest.mark.parametrize(
    "mutation",
    ["missing", "duplicate", "extra", "wrong", "order", "argv", "exit", "binding", "counts"],
)
def test_historical_receipt_requires_exact_checks_and_binding(accepted, mutation):
    a = accepted
    path = a.s.directory / "receipt.json"
    receipt = json.loads(path.read_bytes())
    checks = receipt["checks"]
    if mutation == "missing":
        checks.pop()
    elif mutation == "duplicate":
        checks.append(copy.deepcopy(checks[0]))
    elif mutation == "extra":
        checks.append({**checks[0], "id": "extra"})
    elif mutation == "wrong":
        checks[0]["id"] = "../escape"
    elif mutation == "order":
        checks.reverse()
        if len(checks) == 1:
            checks.append(copy.deepcopy(checks[0]))
    elif mutation == "argv":
        checks[0]["argv"] = ["arbitrary"]
    elif mutation == "exit":
        checks[0]["exit_code"] = 1
    elif mutation == "binding":
        receipt["binding"]["source_claim_revision"] = "changed"
    else:
        receipt["counts"]["total"] += 1
    path.write_text(json.dumps(receipt))
    # Re-anchor the synthetic receipt so rejection exercises semantic validation.
    audit = a.historical["test_receipt"]
    audit.update(receipt_sha256=plan.sha(path.read_bytes()), checks=checks)
    for row in a.rows.values():
        row.links["test_acceptance"] = json.dumps(audit)
    with pytest.raises(ValueError):
        finish._historical_acceptance(
            a.s.home, a.context, a.historical, lambda home, card: a.states[card]
        )


@pytest.mark.parametrize("tag", ["failure", "error", "skipped"])
def test_historical_junit_rejects_unsuccessful_cases_even_with_rehashed_proof(accepted, tag):
    a = accepted
    path = a.s.directory / (a.s.plan["checks"][0]["id"] + ".xml")
    root = ElementTree.fromstring(path.read_bytes())
    ElementTree.SubElement(next(root.iter("testcase")), tag)
    path.write_bytes(ElementTree.tostring(root))
    receipt_path = a.s.directory / "receipt.json"
    receipt = json.loads(receipt_path.read_bytes())
    receipt["junit_sha256"] = plan.sha(path.read_bytes())
    receipt_path.write_text(json.dumps(receipt))
    audit = a.historical["test_receipt"]
    audit["receipt_sha256"] = plan.sha(receipt_path.read_bytes())
    for row in a.rows.values():
        row.links["test_acceptance"] = json.dumps(audit)
    with pytest.raises(ValueError):
        finish._historical_acceptance(
            a.s.home, a.context, a.historical, lambda home, card: a.states[card]
        )
