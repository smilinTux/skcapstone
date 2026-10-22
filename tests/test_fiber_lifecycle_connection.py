"""Isolated terminal-to-review integration through the actual mediated CLI."""

import base64
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ops/fiber-f1be0929"))
import fiber_control as control
import fiber_lifecycle as lifecycle
from fiber_admission import Refused, card_fingerprint
from fiber_dispatch import connect
from fiber_refill import refill_queue

from skcapstone.card_store import CardCore, CardStore
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from tests.test_provisional_verdict_producer import _run


@pytest.fixture
def rig(tmp_path, monkeypatch):
    home, state, config, data = [tmp_path / name for name in ("home", "state", "config", "data")]
    qualified = config / "qualified"
    for path in (home, state, config, data, qualified):
        path.mkdir(mode=0o700)
    card, owner, claim, token = "1234abcd", "pi-fiber-implementation-1234abcd", "d" * 32, "a" * 32
    store = CardStore(home)
    store.create(
        CardCore(
            id=card,
            title="[S] Source fixture",
            description="Original exact task",
            created_by=owner,
            acceptance_criteria=["Run the original required tests."],
            initial_labels=["source-only", "sklegal"],
            meta={
                "repository": "https://example.invalid/approved/repo.git",
                "base_ref": "main",
                "base_revision": "b" * 40,
            },
        )
    )
    store.append_event(card, "claim", owner, owner=owner, claim_revision=claim)

    def cards(ids):
        return {cid: store.fold(cid).model_dump(mode="json") for cid in ids}

    monkeypatch.setattr(control, "cards", cards)
    monkeypatch.setattr(
        control, "native_revision", lambda cid: LiveCardStoreGateway(home).read_card(cid).revision
    )
    job = control.describe(card, "implementation", "deepseek") | {"host": "chiap02"}
    receipt = {
        "card": card,
        "token": token,
        "host": "chiap02",
        "unit": "skfleet-fiber-" + card + "-" + token + ".service",
        "invocation": "c" * 32,
        "provider": "deepseek",
        "state": "terminal",
        "main_pid": 0,
        "exit_code": 0,
    }
    with connect(state / "admission.sqlite3") as db:
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                card,
                token,
                job["host"],
                receipt["unit"],
                json.dumps(job),
                "terminal",
                receipt["invocation"],
                json.dumps(receipt),
            ),
        )
    raw, evidence = (
        b"synthetic opaque bundle",
        b"Proposal for independent review, not acceptance.\n",
    )
    packet = {
        "commit": "e" * 40,
        "tree": "f" * 40,
        "ref": "refs/heads/work/fiber-" + card + "-" + token[:8],
        "evidence_sha256": hashlib.sha256(evidence).hexdigest(),
        "evidence_b64": base64.b64encode(evidence).decode(),
        "bundle_sha256": hashlib.sha256(raw).hexdigest(),
        "bundle_b64": base64.b64encode(raw).decode(),
    }
    proof = {key: receipt[key] for key in ("card", "token", "host", "unit", "invocation")}
    proof.update(
        schema="skfleet.candidate/v1",
        owner=owner,
        claim_revision=claim,
        base_revision="b" * 40,
        execution_sha256="9" * 64,
        candidate=packet,
        accepted=False,
    )
    policy = {
        "schema": "skfleet.selection/v1",
        "cards": {card: card_fingerprint(cards([card])[card])},
        "repository": job["repository"],
        "qualified_at": 900,
        "expires_at": 1100,
        "evidence_sha256": "8" * 64,
    }
    policy_path = config / "lifecycle.json"
    policy_path.write_text(json.dumps(policy))
    policy_path.chmod(0o600)
    calls = []

    def collect(run):
        calls.append("collect")
        return json.loads(json.dumps(proof))

    def record(intent):
        calls.append("record")
        p = intent["candidate"]
        result = _run(
            home,
            "verdict",
            card,
            "PASS_FOR_REVIEW",
            "--agent",
            owner,
            "--candidate",
            intent["evidence_path"],
            "--commit",
            p["commit"],
            "--tree",
            p["tree"],
            "--ref",
            p["ref"],
            "--expected-candidate-sha256",
            p["evidence_sha256"],
            "--expected-source-revision",
            intent["source_revision"],
            "--expected-claim-revision",
            claim,
            "--transition-id",
            intent["transition_id"],
        )
        if result.exit_code:
            raise Refused(result.output)

    def opened(intent, revision):
        calls.append("open")
        result = _run(
            home,
            "review-work",
            card,
            "--producer",
            owner,
            "--expected-source-revision",
            revision,
            "--expected-claim-revision",
            claim,
        )
        if result.exit_code:
            raise Refused(result.output)
        return json.loads(result.output)

    args = dict(
        home=home,
        data_root=data,
        read_cards=cards,
        collect_candidate=collect,
        snapshot_revision=lambda cid: LiveCardStoreGateway(home).read_card(cid).revision,
        record_verdict=record,
        open_review=opened,
        describe=control.describe,
        eligible=lambda row, board: row.get("owner") is None,
        verify=control.verify_refill_request,
        now=1000,
        clock=lambda: 1000,
    )

    def run(**overrides):
        return lifecycle.connect_reviews(
            state, qualified, policy_path, config / "HOLD", **(args | overrides)
        )

    return locals()


def test_real_native_handoff_prepares_one_opposite_provider_review(rig):
    r = rig
    first = r["run"]()
    review = first["reviews"][0]["review"]
    assert first["reviews"][0]["state"] == "prepared"
    assert r["store"].fold(review).owner is None
    receipt = json.loads((r["qualified"] / (review + ".json")).read_text())
    assert receipt["job"]["provider"] == "zai"
    assert receipt["job"]["producer_provider"] == "deepseek"
    assert receipt["job"]["base_revision"] == r["packet"]["commit"]
    assert receipt["job"]["source_bundle"]["sha256"] == r["packet"]["bundle_sha256"]
    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    assert r["run"]()["reviews"] == first["reviews"]
    assert r["calls"] == ["collect", "record", "open"]
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    queue = r["config"] / "jobs"
    result = refill_queue(
        r["state"],
        queue,
        r["qualified"],
        r["config"] / "HOLD",
        read_cards=r["cards"],
        eligible=r["args"]["eligible"],
        verify=control.verify_refill_request,
        now=1000,
    )
    assert result["added"] == [review]
    assert r["store"].fold(r["card"]).owner == r["owner"]
    assert r["store"].fold(r["card"]).status.value != "done"
    assert json.loads((queue / (review + ".json")).read_text())["_qualification"] == receipt


@pytest.mark.parametrize("point", ["record", "open"])
def test_lost_native_acknowledgement_recovers_without_duplicate_generation(rig, point):
    r = rig
    callback = r["record"] if point == "record" else r["opened"]

    def failed(*args):
        callback(*args)
        raise Refused("synthetic lost acknowledgement")

    with pytest.raises(Refused, match="lost acknowledgement"):
        r["run"](**{"record_verdict" if point == "record" else "open_review": failed})
    assert r["run"]()["reviews"][0]["state"] == "prepared"
    outcomes = [e for e in r["store"]._read_events(r["card"]) if e["action"] == "verdict"]
    assert len(outcomes) == 1
    assert len(r["store"].list_card_ids()) == 2
    assert len(list(r["qualified"].glob("*.json"))) == 1


@pytest.mark.parametrize("change", ["claim", "contract", "packet", "accepted", "unit", "failed"])
def test_stale_or_wrong_input_never_writes_a_board_outcome(rig, change):
    r = rig
    if change == "claim":
        r["store"].append_event(
            r["card"], "claim", r["owner"], owner=r["owner"], claim_revision="e" * 32
        )
    elif change == "contract":
        r["store"].append_event(r["card"], "describe", r["owner"], description="amended")
    elif change == "packet":
        r["proof"]["candidate"]["evidence_sha256"] = "e" * 64
    elif change == "accepted":
        r["proof"]["accepted"] = True
    else:
        receipt = r["receipt"] | (
            {"invocation": "wrong"} if change == "unit" else {"exit_code": 1}
        )
        with sqlite3.connect(r["state"] / "admission.sqlite3") as db:
            db.execute("UPDATE runs SET receipt=?", (json.dumps(receipt),))
    with pytest.raises(Refused):
        r["run"]()
    assert not [e for e in r["store"]._read_events(r["card"]) if e["action"] == "verdict"]
    assert r["store"].list_card_ids() == [r["card"]]


def test_expiry_and_hold_after_collection_prevent_native_write(rig):
    r = rig
    with pytest.raises(Refused, match="stale-policy"):
        r["run"](clock=lambda: 1100)
    assert not [e for e in r["store"]._read_events(r["card"]) if e["action"] == "verdict"]

    def holding(run):
        (r["config"] / "HOLD").write_text("test hold")
        return r["collect"](run)

    # The already persisted intent is still subject to the current hold.
    (r["config"] / "HOLD").write_text("test hold")
    assert r["run"](collect_candidate=holding)["state"] == "admission-held"


def test_scope_missing_is_inert_and_never_scans_history(rig):
    r = rig
    r["policy_path"].unlink()
    assert r["run"]()["state"] == "disabled"
    assert r["calls"] == []


def test_changed_collected_contract_is_not_adopted_by_fresh_snapshot(rig):
    r = rig

    def changed(run):
        proof = r["collect"](run)
        r["store"].append_event(r["card"], "describe", r["owner"], description="racing edit")
        return proof

    with pytest.raises(Refused, match="contract-changed"):
        r["run"](collect_candidate=changed)
    assert not [e for e in r["store"]._read_events(r["card"]) if e["action"] == "verdict"]


def test_queued_review_rechecks_native_source_generation(rig):
    r = rig
    review = r["run"]()["reviews"][0]["review"]
    request = json.loads((r["qualified"] / (review + ".json")).read_text())["job"]
    row = r["cards"]([review])[review]
    control.verify_refill_request(row, request)
    r["store"].append_event(r["card"], "verdict", r["owner"], verdict="FAIL")
    with pytest.raises(Refused, match="source-superseded"):
        control.verify_refill_request(row, request)


def test_changed_saved_evidence_is_not_replaced_on_retry(rig):
    r = rig
    r["run"]()
    intent = json.loads(
        (r["state"] / "lifecycle" / r["token"] / "candidate-intent.json").read_text()
    )
    path = Path(intent["evidence_path"])
    path.write_text("tampered retained evidence")
    with pytest.raises(Refused, match="digest"):
        r["run"]()
    assert path.read_text() == "tampered retained evidence"
    assert r["calls"] == ["collect", "record", "open"]


def terminal_review(r, verdict="PASS"):
    """Seed a synthetic returned review without giving it acceptance authority."""
    review = r["run"]()["reviews"][0]["review"]
    qualification = json.loads((r["qualified"] / (review + ".json")).read_text())
    job = qualification["job"] | {"host": "chiap03", "_qualification": qualification}
    claim, token, invocation = "1" * 32, "2" * 32, "3" * 32
    r["store"].append_event(
        review, "claim", job["owner"], owner=job["owner"], claim_revision=claim
    )
    receipt = {
        "card": review,
        "host": job["host"],
        "token": token,
        "unit": "skfleet-fiber-" + review + "-" + token + ".service",
        "invocation": invocation,
        "provider": job["provider"],
        "state": "terminal",
        "main_pid": 0,
        "exit_code": 0,
    }
    with connect(r["state"] / "admission.sqlite3") as db:
        db.execute(
            "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                review,
                token,
                job["host"],
                receipt["unit"],
                json.dumps(job),
                "terminal",
                invocation,
                json.dumps(receipt),
            ),
        )
    report = b"Synthetic independent review, not actual source acceptance.\n"
    decision = {
        "schema": "skfleet.review-proposal/v1",
        "card": review,
        "source_head": job["base_revision"],
        "verdict": verdict,
        "evidence_sha256": hashlib.sha256(report).hexdigest(),
    }
    raw = json.dumps(decision).encode()
    packet = r["packet"] | {
        "commit": "4" * 40,
        "tree": "5" * 40,
        "ref": "refs/heads/work/fiber-" + review + "-" + token[:8],
        "evidence_b64": base64.b64encode(report).decode(),
        "evidence_sha256": decision["evidence_sha256"],
        "review_decision_b64": base64.b64encode(raw).decode(),
        "review_decision_sha256": hashlib.sha256(raw).hexdigest(),
    }
    proof = {key: receipt[key] for key in ("card", "host", "token", "unit", "invocation")}
    proof.update(
        schema="skfleet.candidate/v1",
        owner=job["owner"],
        claim_revision=claim,
        base_revision=job["base_revision"],
        execution_sha256="6" * 64,
        accepted=False,
        candidate=packet,
    )
    calls = []

    def collect(run):
        assert run["card"] == review
        calls.append(review)
        return json.loads(json.dumps(proof))

    return locals()


@pytest.mark.parametrize("verdict", ["PASS", "FAIL", "BLOCKED"])
def test_terminal_review_proposal_never_substitutes_for_required_test_acceptance(rig, verdict):
    r = rig
    finished = terminal_review(r, verdict)
    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    result = r["run"](collect_candidate=finished["collect"])
    outcome = result["reviews"][0]
    assert outcome["state"] == (
        "awaiting-required-tests" if verdict == "PASS" else "review-needs-disposition"
    )
    assert outcome["proposed_verdict"] == verdict
    assert outcome["accepted"] is False
    assert r["run"](collect_candidate=finished["collect"]) == result
    assert finished["calls"] == [finished["review"]]
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    assert r["store"].fold(r["card"]).owner == r["owner"]
    assert r["store"].fold(finished["review"]).owner == finished["job"]["owner"]


@pytest.mark.parametrize(
    "change", ["claim", "invocation", "evidence", "proposal", "source", "qualification"]
)
def test_wrong_review_return_cannot_be_retained_as_current(rig, change):
    r = rig
    finished = terminal_review(r)
    if change == "claim":
        finished["proof"]["claim_revision"] = "7" * 32
    elif change == "invocation":
        finished["proof"]["invocation"] = "7" * 32
    elif change == "evidence":
        finished["proof"]["candidate"]["evidence_sha256"] = "7" * 64
    elif change == "proposal":
        finished["proof"]["candidate"]["review_decision_sha256"] = "7" * 64
    elif change == "source":
        finished["proof"]["base_revision"] = "7" * 40
    else:
        job = finished["job"] | {"_qualification": {}}
        with sqlite3.connect(r["state"] / "admission.sqlite3") as db:
            db.execute(
                "UPDATE runs SET binding=? WHERE card=?", (json.dumps(job), finished["review"])
            )
    before = {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}
    with pytest.raises(Refused):
        r["run"](collect_candidate=finished["collect"])
    assert not (r["state"] / "lifecycle" / r["token"] / "review-intent.json").exists()
    assert before == {cid: r["store"]._read_events(cid) for cid in r["store"].list_card_ids()}


def test_saved_review_claim_or_report_drift_refuses_on_retry(rig):
    r = rig
    finished = terminal_review(r)
    r["run"](collect_candidate=finished["collect"])
    path = r["state"] / "lifecycle" / r["token"] / "review-intent.json"
    intent = json.loads(path.read_text())
    evidence = Path(intent["evidence_path"])
    original = evidence.read_bytes()
    evidence.write_text("Changed retained review report")
    with pytest.raises(Refused, match="digest"):
        r["run"](collect_candidate=finished["collect"])
    evidence.write_bytes(original)
    r["store"].append_event(
        finished["review"],
        "claim",
        finished["job"]["owner"],
        owner=finished["job"]["owner"],
        claim_revision="7" * 32,
    )
    with pytest.raises(Refused, match="review-intent-changed"):
        r["run"](collect_candidate=finished["collect"])
    assert finished["calls"] == [finished["review"]]


def test_review_collection_race_does_not_preserve_a_stale_acceptance_intent(rig):
    r = rig
    finished = terminal_review(r)

    def raced(run):
        proof = finished["collect"](run)
        r["store"].append_event(
            finished["review"], "describe", "operator", description="changed review"
        )
        return proof

    with pytest.raises(Refused, match="review-raced"):
        r["run"](collect_candidate=raced)
    assert not (r["state"] / "lifecycle" / r["token"] / "review-intent.json").exists()
