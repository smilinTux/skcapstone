"""Native temp CardStore, real source Git, destination fixture service boundary."""

import json
import time
from pathlib import Path

import pytest

from skcapstone.fleet import builder_dispatch as builder, review_dispatch as review
from skcapstone.fleet import production_admission as admission, production_builder as production
from skcapstone.fleet import production_resources, store
from tests.fleet.test_remote_review_policy import remote_policy  # noqa: F401
from tests.fleet.test_source_bundle import source  # noqa: F401


from tests.fleet.remote_review_fixtures import (
    execution,
    offer_and_consume,
    terminal_review,
)  # noqa: F401


def test_native_remote_claim_launch_and_receipt_on_destination(execution):
    e = execution
    request, status = offer_and_consume(e)
    assert status and status["state"] == "running"
    assert e["observations"] == ["chiap03", "chiap03"]
    assert len(e["launches"]) == 1
    assert {"review", "seat-seraph", "source-only"} <= set(request["labels"])
    assert "PRODUCTION SOURCE-ONLY INDEPENDENT REVIEW" in e["launches"][0][0][-1]
    card = e["source"]["store"].fold(e["card"])
    assert card.owner == request["reviewer"]
    events = e["source"]["store"]._read_events(e["card"])
    event = next(row for row in events if row.get("action") == "review_assignment_launch")
    assert event["schema"] == "skfleet.review-assignment-launch/v3"
    from skcapstone.fleet.production_receipts import production_receipt_allowed

    launch = dict(
        host="chiap03",
        owner=card.owner,
        revision=card.meta["_claim_revision"],
        lane="deepseek",
        model="gateway-current",
    )
    assert production_receipt_allowed(e["home"], e["policy"], launch, card, event)
    # Duplicate delivery keeps the exact intent/claim, with no producer recovery.
    builder.consume_one(e["paths"], e["home"], "node-chiap03", launcher=e["launcher"])
    assert len(e["launches"]) == 1


def test_destination_capacity_loss_does_not_claim_or_burn_attempt(execution):
    e = execution
    e["capacity"][0] = False
    request, status = offer_and_consume(e)
    assert status is None and not e["launches"]
    assert e["source"]["store"].fold(e["card"]).owner is None
    assert not list((e["home"] / "fleet/resource-admission").glob("*/*/intent.json"))
    e["host"][0] = "chiap08"
    assert review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"]) is None


def test_remote_review_consumes_the_native_start_fence_once(execution):
    e = execution
    request, status = offer_and_consume(e)
    directory = (
        e["home"] / "fleet/resource-admission/chiap03" / status["execution"]["admission_id"]
    )
    before = (directory / "start.json").read_bytes()
    start = json.loads(before)
    assert start["binding"]["card_id"] == e["card"]
    assert start["binding"]["owner"] == request["reviewer"]
    assert start["binding"]["claim_revision"] == status["claim_revision"]
    builder.consume_one(e["paths"], e["home"], "node-chiap03", launcher=e["launcher"])
    assert len(e["launches"]) == 1
    assert (directory / "start.json").read_bytes() == before


def test_claim_release_after_remote_reservation_refuses_spawn(execution, monkeypatch):
    e = execution
    write_status = builder._write_status

    def release_after_reservation(paths, node, request, state, **extra):
        result = write_status(paths, node, request, state, **extra)
        if state == "admission-pending":
            card = e["source"]["store"].fold(e["card"])
            e["source"]["store"].append_event(
                e["card"],
                "release_claim",
                card.owner,
                released_owner=card.owner,
                expected_claim_revision=card.meta["_claim_revision"],
            )
        return result

    monkeypatch.setattr(builder, "_write_status", release_after_reservation)
    offer_and_consume(e)
    assert not e["launches"]
    root = e["home"] / "fleet/resource-admission/chiap03"
    assert len(list(root.glob("*/intent.json"))) == 1
    assert not list(root.glob("*/start.json"))


@pytest.mark.parametrize(
    "change",
    [
        "host09",
        "host10",
        "host04",
        "claim",
        "writer",
        "invocation",
        "policy",
        "request",
        "source",
        "recommendation",
        "duplicate",
    ],
)
def test_remote_receipt_refuses_changed_native_binding(execution, change):
    e = execution
    request, status = offer_and_consume(e)
    assert status and status["state"] == "running"
    card = e["source"]["store"].fold(e["card"])
    event = next(
        row
        for row in e["source"]["store"]._read_events(e["card"])
        if row.get("action") == "review_assignment_launch"
    )
    launch = dict(
        host="chiap03",
        owner=card.owner,
        revision=card.meta["_claim_revision"],
        lane="deepseek",
        model="gateway-current",
    )
    if change.startswith("host"):
        launch["host"] = "chiap" + change[-2:]
    elif change == "claim":
        launch["revision"] = "f" * 32
    elif change == "writer":
        event["writer"] = "forged"
    elif change in {"invocation", "policy", "request"}:
        key = {"policy": "policy_sha256", "request": "request_sha256"}.get(change, change)
        event["execution"][key] = "0" * (32 if change == "invocation" else 64)
    elif change == "recommendation":
        event["recommendation_id"] = "another"
    elif change == "source":
        e["source"]["store"].append_event(
            e["source"]["card"],
            "link",
            e["source"]["owner"],
            link_key="verdict",
            link_value="BLOCKED",
        )
    else:
        e["source"]["store"].append_event(
            e["card"], "review_assignment_launch", card.owner, claim_revision=launch["revision"]
        )
    from skcapstone.fleet.production_receipts import production_receipt_allowed

    assert not production_receipt_allowed(e["home"], e["policy"], launch, card, event)


def test_authority_imports_real_remote_review_without_original_workspace(terminal_review):
    from skcapstone.fleet.production_acceptance import collect

    e = terminal_review
    e["host"][0] = "chiap08"
    e["workspace"].rename(e["workspace"].with_name("destination-is-not-mounted"))
    context = collect(
        e["home"],
        e["policy"],
        e["card"],
        e["status"]["claim_revision"],
        process_check=lambda card: dict(sessions=[], units=[]),
    )
    assert context["review"]["review_head"] != e["request"]["source"]["head"]
    assert context["source"]["head"] == e["request"]["source"]["head"]
    assert e["source"]["store"].fold(e["source"]["card"]).owner == e["source"]["owner"]


@pytest.mark.parametrize("change", ["missing-packet", "bundle-hash", "owner", "claim", "live"])
def test_remote_completion_rejects_bad_transport_or_custody(terminal_review, monkeypatch, change):
    from skcapstone.fleet import production_acceptance as acceptance

    e = terminal_review
    path = builder.status_path(e["paths"], "node-chiap03", e["card"])
    status = json.loads(path.read_text())
    if change == "missing-packet":
        del status["review_packet"]
    elif change == "bundle-hash":
        status["review_packet"]["sha256"] = "0" * 64
    elif change in {"owner", "claim"}:
        status["owner" if change == "owner" else "claim_revision"] = "forged"
    else:

        def still_live(*a, **kw):
            raise ValueError("wrapper still active")

        monkeypatch.setattr(acceptance, "unit_terminal", still_live)
    path.write_text(json.dumps(status))
    e["host"][0] = "chiap08"
    with pytest.raises((ValueError, KeyError, OSError)):
        acceptance.collect(
            e["home"],
            e["policy"],
            e["card"],
            e["status"]["claim_revision"],
            process_check=lambda card: dict(sessions=[], units=[]),
        )
    assert e["source"]["store"].fold(e["source"]["card"]).status.value != "done"


def test_seraph_offer_is_pending_never_a_successful_launch(execution):
    import subprocess
    from skcapstone.seat_cycle_entrypoint import verify_seraph_dispatch

    e = execution
    request = review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"])
    assert request
    output = (
        "REVIEW_OFFERED|chiap08|"
        + e["card"]
        + "|node=node-chiap03|request="
        + request["request_id"]
    )
    result = verify_seraph_dispatch(
        e["home"],
        subprocess.CompletedProcess([], 0, stdout=output, stderr=""),
        production_policy=e["policy"],
    )
    assert result["reason"] == "seraph_remote_review_pending"
    assert result["recommendations"] == 0
    assert result["pending_offers"] == 1


@pytest.mark.parametrize(
    "terminal_review", ["FAIL", "BLOCKED", "STRUCTURED_BLOCKED"], indirect=True
)
def test_negative_remote_review_completes_only_review(terminal_review):
    from skcapstone.fleet import production_acceptance as acceptance

    e = terminal_review
    e["host"][0] = "chiap08"
    result = acceptance.finish_remote_disposition(
        e["home"], e["policy"], e["card"], e["status"]["claim_revision"]
    )
    assert result["state"] in {"review-fail", "review-blocked"}
    assert e["source"]["store"].fold(e["card"]).status.value == "done"
    producer = e["source"]["store"].fold(e["source"]["card"])
    assert producer.owner == e["source"]["owner"]
    assert producer.meta["_claim_revision"] == e["source"]["claim"]
    assert producer.status.value != "done"


@pytest.mark.parametrize("change", ["quota", "capability", "host-alias", "cordon", "same-family"])
def test_changed_destination_or_route_refuses_before_claim(execution, monkeypatch, change):
    e = execution
    request = review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"])
    assert request
    if change == "quota":
        value = dict(e["policy"])
        value["node_quotas"] = {k: v for k, v in value["node_quotas"].items() if k != "chiap03"}
        (e["home"] / "production.json").write_text(json.dumps(value))
    elif change in {"capability", "host-alias"}:
        path = e["paths"].spec_path("node", "node-chiap03")
        spec = json.loads(path.read_text())
        if change == "capability":
            spec["spec"]["capabilities"] = []
        else:
            spec["labels"]["host"] = "chiap03.alias"
        path.write_text(json.dumps(spec))
    elif change == "cordon":
        views = builder.node_views(e["paths"])
        views[0].cordoned = True
        monkeypatch.setattr(builder, "node_views", lambda paths: views)
    else:
        routes = production.production_routes.snapshot(e["policy"])
        routes["routes"][0]["provider"] = "zai"
        routes["routes"][0]["capacity_domain"] = "zai"
    e["host"][0] = "chiap03"
    builder.consume_one(e["paths"], e["home"], "node-chiap03", launcher=e["launcher"])
    assert not e["launches"]
    assert e["source"]["store"].fold(e["card"]).owner is None


@pytest.mark.parametrize("terminal_review", ["LARGE"], indirect=True)
def test_large_bounded_review_evidence_does_not_overflow_native_status(terminal_review):
    from skcapstone.fleet.production_acceptance import collect

    e = terminal_review
    e["host"][0] = "chiap08"
    e["workspace"].rename(e["workspace"].with_name("remote-only"))
    context = collect(
        e["home"],
        e["policy"],
        e["card"],
        e["status"]["claim_revision"],
        process_check=lambda card: dict(sessions=[], units=[]),
    )
    assert context["review"]["proposal"]["verdict"] == "PASS"


def test_missing_request_never_reassigns_existing_native_review_offer(execution):
    e = execution
    request = review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"])
    builder.request_path(e["paths"], request["node"], e["card"]).unlink()
    assert review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"]) is None


def test_orphan_destination_custody_is_held_across_seats(execution):
    e = execution
    request, status = offer_and_consume(e)
    builder.request_path(e["paths"], request["node"], e["card"]).unlink()
    assert e["card"] in builder.held_card_ids(e["paths"])
    e["host"][0] = "chiap08"
    assert review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"]) is None


def test_remote_wrapper_receives_policy_and_native_startup_identity(execution):
    e = execution
    request, status = offer_and_consume(e)
    argv = e["launches"][0][0]
    assert "--setenv=SKFLEET_PRODUCTION_POLICY=" + str(e["home"] / "production.json") in argv
    assert "--setenv=SKFLEET_AUTHORITY_HOST=chiap08" in argv
    assert "SKFLEET_SESSION_ID=" + request["reviewer"] in " ".join(argv)
    assert "shell-liveness" in " ".join(argv)


def test_review_child_retains_attributable_heartbeat_and_reaps_emitter(tmp_path):
    import subprocess

    command = production.review_child_command(
        tmp_path, "pi-seraph-chiap03-ab000002", "ab000002", "a" * 32, ["/bin/sleep", "0.3"]
    )
    result = subprocess.run(command, capture_output=True, timeout=5)
    assert result.returncode == 0
    beat = json.loads((tmp_path / "fleet/beats/pi-seraph-chiap03-ab000002.json").read_text())
    assert beat["owner"] == beat["session_id"] == "pi-seraph-chiap03-ab000002"
    assert beat["claim_revision"] == "a" * 32
    assert beat["card_id"] == "ab000002"
    assert beat["proves"] == "shell-liveness"
