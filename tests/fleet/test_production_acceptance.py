"""Native proposal collection joins real Git, exact claims and terminal custody."""

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from skcapstone.fleet import production_acceptance as acceptance
from skcapstone.fleet.production_builder import unit_name
from skcapstone.fleet.production_review_custody import exit_path
from skcapstone.fleet.production_review_finish import native_command, native_state, once
from tests.fleet.test_production_review_finish import pair  # noqa: F401
from tests.fleet.test_source_bundle import git, source  # noqa: F401

pytestmark = pytest.mark.host_systemd


@pytest.fixture
def stopped_pair(pair, source, monkeypatch, request):  # noqa: F811
    review_domain = getattr(request, "param", "deepseek")
    home, _, context, store, _, _ = pair
    parent, review = context["source"], context["review"]
    head = parent["head"]
    manifest = json.loads(
        (home / "evidence/work" / parent["card"] / "source-bundles" / (head + ".json")).read_text()
    )
    policy = {
        "authority_host": "control",
        "node_quotas": {
            "control": {
                "cpu_quota_percent": 100,
                "memory_max_bytes": 1024**3,
                "tasks_max": 128,
                "runtime_max_seconds": 600,
            }
        },
    }
    request = {
        "card_id": parent["card"],
        "request_id": "d" * 64,
        "production": {"family": "zai", "host": "worker", "authority": "control"},
    }
    once(home / "fleet/dispatch/node-worker" / (parent["card"] + ".json"), request)
    status = dict(
        schema="skfleet.builder-dispatch-status/v1",
        card_id=parent["card"],
        node="node-worker",
        request_id=request["request_id"],
        state="awaiting-review",
        exit_code=0,
        claim_released=False,
        owner=parent["owner"],
        claim_revision=parent["claim"],
        unit=unit_name(request, 1),
        attempt=1,
        invocation="e" * 32,
        production=request["production"],
        writer={"role": "sknoded", "node": "node-worker"},
        source_artifact=manifest,
    )
    status_path = home / "fleet/status/node-worker/dispatch" / (parent["card"] + ".json")
    once(status_path, status)
    terminal = dict(
        schema="skfleet.production-review-exit/v1",
        card=review["card"],
        owner=review["owner"],
        claim_revision=review["claim"],
        host="control",
        lane="deepseek",
        model="qualified-model",
        unit="skfleet-worker-deepseek-" + review["card"] + ".service",
        invocation="f" * 32,
        source_head=head,
        exit_code=0,
        workspace=str(Path(review["evidence_path"]).parents[4]),
    )
    terminal_path = exit_path(home, review["card"], review["claim"])
    once(terminal_path, terminal)
    store.append_event(
        review["card"],
        "review_assignment_launch",
        review["owner"],
        claim_revision=review["claim"],
        launched=True,
        route_identity={"capacity_domains": [review_domain], "model_or_bucket": "qualified-model"},
    )
    monkeypatch.setattr(
        acceptance,
        "production_receipt_allowed",
        lambda home, policy, launch, card, event: (
            launch["model"] == event["route_identity"]["model_or_bucket"]
        ),
    )
    monkeypatch.setattr(acceptance, "unit_terminal", lambda *a, **k: {"ActiveState": "inactive"})
    return home, policy, review, status_path, terminal_path, store


def test_real_unpublished_candidate_and_committed_review_collect_separately(stopped_pair):
    home, policy, review, _, _, store = stopped_pair
    context = acceptance.collect(
        home,
        policy,
        review["card"],
        review["claim"],
        process_check=lambda card: {"sessions": [], "units": []},
    )
    assert git(Path(context["source_workspace"]), "rev-parse", "HEAD") == context["source"]["head"]
    assert git(Path(context["source_workspace"]), "status", "--porcelain") == ""
    assert context["review"]["review_head"] != context["source"]["head"]
    assert context["test_binding"]["source_owner"] == context["source"]["owner"]
    assert context["controller"] == "fleet-review-closer@control"
    assert store.fold(review["card"]).owner == review["owner"]
    assert store.fold(context["source"]["card"]).owner == context["source"]["owner"]


def test_collected_producer_terminal_can_enter_independent_review(stopped_pair):
    home, _, review, status_path, _, store = stopped_pair
    status = json.loads(status_path.read_text())
    status["exit_code"] = None
    status["terminal_proof"] = "qualified-terminal"
    status_path.write_text(json.dumps(status))
    source_card = store.fold(status["card_id"])
    terminal = acceptance._producer_terminal(home, source_card, status["source_artifact"])
    assert terminal["invocation"] == status["invocation"]

    status.pop("terminal_proof")
    status_path.write_text(json.dumps(status))
    with pytest.raises(
        acceptance.ReviewEvidenceError, match="native producer terminal custody differs"
    ):
        acceptance._producer_terminal(home, source_card, status["source_artifact"])


def test_direct_seat_terminal_receipt_can_enter_review_and_is_rechecked(stopped_pair, monkeypatch):
    home, _, _, status_path, _, store = stopped_pair
    status = json.loads(status_path.read_text())
    status_path.unlink()
    source_card = store.fold(status["card_id"])
    monkeypatch.setattr(acceptance.socket, "gethostname", lambda: "worker")
    manifest_path = (
        home
        / "evidence/work"
        / source_card.id
        / "source-bundles"
        / f"{status['source_artifact']['head']}.json"
    )
    artifact = {
        **status["source_artifact"],
        "manifest": str(manifest_path),
        "manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
    }
    receipt = {
        "schema": "skfleet.direct-seat/v1",
        "card": source_card.id,
        "owner": source_card.owner,
        "claim_revision": source_card.meta["_claim_revision"],
        "completion_state": "awaiting-review",
        "host": "worker",
        "lane": "glm",
        "unit": f"skfleet-worker-glm-{source_card.id}.service",
        "invocation": "e" * 32,
        "exit_code": 0,
        "pid": 123,
        "route_schema": "skfleet.runtime-route/v1",
        "logical_route": "sk-s",
        "provider": "skgateway",
        "capacity_domains": ["zai"],
        "model_or_bucket": "glm-4.5",
        "source_disposition": {
            "state": "awaiting-review",
            "claim_released": False,
            "process_terminal": True,
            "source_artifact": artifact,
        },
    }
    path = home / "fleet/direct-seats" / f"{source_card.owner}.json"
    once(path, receipt)
    path.chmod(0o600)

    terminal = acceptance._producer_terminal(home, source_card, status["source_artifact"])
    assert terminal["receipt_kind"] == "direct-seat"
    assert terminal["family"] == "zai"
    context = {
        "source": {
            "card": source_card.id,
            "owner": source_card.owner,
            "terminal": terminal,
        },
        "review": {
            "card": "review-card",
            "terminal": {
                "unit": "review.service",
                "invocation": "f" * 32,
                "host": "control",
            },
        },
    }
    acceptance.terminal_guard(home, context, lambda card: {"sessions": [], "units": []})

    receipt["source_disposition"]["source_artifact"]["manifest_sha256"] = "0" * 64
    path.write_text(json.dumps(receipt))
    with pytest.raises(acceptance.ReviewEvidenceError, match="manifest digest differs"):
        acceptance._producer_terminal(home, source_card, status["source_artifact"])

    receipt["source_disposition"]["source_artifact"]["manifest_sha256"] = artifact[
        "manifest_sha256"
    ]
    receipt["completion_state"] = "running"
    path.write_text(json.dumps(receipt))
    with pytest.raises(acceptance.ReviewEvidenceError, match="receipt changed"):
        acceptance.terminal_guard(home, context, lambda card: {"sessions": [], "units": []})


@pytest.mark.parametrize(
    "kind",
    [
        "missing-exit",
        "exit-failure",
        "stale-claim",
        "false-ci",
        "source-change",
        "source-running",
        "same-family",
        "live-review",
        "review-hold",
    ],
)
def test_collection_refuses_missing_forged_stale_or_nonindependent_proof(stopped_pair, kind):
    home, policy, review, status_path, terminal_path, store = stopped_pair
    if kind == "missing-exit":
        terminal_path.unlink()
    elif kind == "exit-failure":
        value = json.loads(terminal_path.read_text())
        value["exit_code"] = 1
        terminal_path.write_text(json.dumps(value))
    elif kind == "stale-claim":
        store.append_event(review["card"], "claim", "new-reviewer", owner="new-reviewer")
    elif kind == "false-ci":
        state = native_state(home, review["card"])
        native_command(
            home,
            [
                "link",
                review["card"],
                "hosted_checks",
                "SUCCESS",
                "--agent",
                review["owner"],
                "--expected-source-revision",
                state["revision"],
                "--expected-claim-revision",
                review["claim"],
                "--transition-id",
                "9" * 64,
                "--json",
            ],
        )
    elif kind == "source-change":
        parent = store.fold(review["card"]).meta["link_source_card"]
        store.append_event(parent, "add_label", "operator", label="source-changed")
    elif kind == "review-hold":
        store.append_event(review["card"], "add_label", "operator", label="do-not-claim")
    elif kind in {"source-running", "same-family"}:
        value = json.loads(status_path.read_text())
        if kind == "source-running":
            value["state"] = "running"
        else:
            value["production"]["family"] = "deepseek"
        status_path.write_text(json.dumps(value))

    def process(card):
        return {"sessions": ["active"] if kind == "live-review" else [], "units": []}

    with pytest.raises((ValueError, OSError)):
        acceptance.collect(home, policy, review["card"], review["claim"], process_check=process)
    assert not any(e["action"] == "complete" for e in store._read_events(review["card"]))


@pytest.mark.parametrize("stopped_pair", ["zai"], indirect=True)
def test_same_provider_family_is_refused_despite_valid_launch_and_source(stopped_pair):
    home, policy, review, _, _, _ = stopped_pair
    with pytest.raises(ValueError, match="family is not independent"):
        acceptance.collect(
            home,
            policy,
            review["card"],
            review["claim"],
            process_check=lambda card: {"sessions": [], "units": []},
        )


@pytest.mark.parametrize("tests_ready", [False, True])
def test_controller_requires_trusted_tests_and_finishes_without_legacy_release(
    stopped_pair, monkeypatch, tests_ready
):
    home, policy, review, _, _, store = stopped_pair
    monkeypatch.setattr(acceptance.socket, "gethostname", lambda: "control")
    import hashlib

    receipt_path = next((home / "fleet/test-runs").glob("*/receipt.json"))
    raw = receipt_path.read_bytes()
    retained = json.loads(raw)
    receipt = {
        "receipt_path": str(receipt_path),
        "receipt_sha256": hashlib.sha256(raw).hexdigest(),
        "plan_sha256": retained["plan_sha256"],
        "source_head": retained["binding"]["source_head"],
        "checks": retained["checks"],
        "counts": retained["counts"],
    }
    monkeypatch.setitem(
        sys.modules,
        "skcapstone.fleet.production_tests",
        SimpleNamespace(
            run_or_read_tests=lambda *args: receipt if tests_ready else None,
            validate_test_receipt=lambda *args: dict(receipt),
        ),
    )
    results = acceptance.reconcile(
        home, policy, process_check=lambda card: {"sessions": [], "units": []}
    )
    assert len(results) == 1
    assert results[0]["state"] == (
        "accepted" if tests_ready else "awaiting-trusted-tests"
    ), results
    row = store.fold(review["card"])
    assert row.status.value == ("done" if tests_ready else "doing")
    assert row.owner == (None if tests_ready else review["owner"])
    assert not any(e["action"] == "release_claim" for e in store._read_events(review["card"]))
    before = store._read_events(review["card"])
    assert (
        acceptance.reconcile(
            home, policy, process_check=lambda card: {"sessions": [], "units": []}
        )
        == results
    )
    assert store._read_events(review["card"]) == before


def test_bad_retained_exit_does_not_block_next_independent_card(stopped_pair, monkeypatch):
    home, policy, review, _, _, _ = stopped_pair
    monkeypatch.setattr(acceptance.socket, "gethostname", lambda: "control")
    once(exit_path(home, "00000001", "a" * 32), {"schema": "invalid"})
    monkeypatch.setitem(
        sys.modules,
        "skcapstone.fleet.production_tests",
        SimpleNamespace(run_or_read_tests=lambda *args: None),
    )
    results = acceptance.reconcile(
        home, policy, process_check=lambda card: {"sessions": [], "units": []}
    )
    assert [(row["card"], row["state"]) for row in results] == [
        ("00000001", "pending"),
        (review["card"], "awaiting-trusted-tests"),
    ]
