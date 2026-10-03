"""Native temp CardStore, real source Git, destination fixture service boundary."""

import json
import time
from pathlib import Path

import pytest
from skcoord.card_store import CardCore

from skcapstone.fleet import builder_dispatch as builder, review_dispatch as review
from skcapstone.fleet import production_admission as admission, production_builder as production
from skcapstone.fleet import production_resources, store
from skcapstone.fleet.paths import FleetPaths
from tests.fleet.test_remote_review_policy import remote_policy  # noqa: F401
from tests.fleet.test_source_bundle import source, publish  # noqa: F401
from tests.fleet.test_production_receipts import snapshot


@pytest.fixture
def execution(source, remote_policy, monkeypatch):
    from skcapstone.fleet.node_controller import NodeView
    from skcapstone.review_replacement import current_review_attempt
    from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
    from skcapstone.fleet.production_review_finish import once

    home = source["home"]
    paths = FleetPaths(home / "fleet")
    artifact = publish(source)
    card = current_review_attempt(home, source["card"], source["head"])
    remote_policy["remote_review"]["card_ids"] = [card]
    metadata = {
        **source["core"]["meta"],
        "repository": source["remote"],
        "source_revision": LiveCardStoreGateway(home).read_card(source["card"]).revision,
    }
    source["store"].create(
        CardCore(
            id=card,
            title="[REVIEW] [M] exact source",
            created_by="link",
            initial_labels=["review", "seat-seraph", "source-only", "parent-" + source["card"]],
            meta=metadata,
        )
    )
    source["store"].append_event(card, "move", "link", status="review", column="review")
    # Existing source terminal protocol, not a caller-supplied provider label.
    producer_request = dict(
        schema="skfleet.builder-dispatch/v1",
        request_id="f" * 64,
        card_id=source["card"],
        production=dict(host="chiap02", family="glm"),
    )
    once(builder.request_path(paths, "node-chiap02", source["card"]), producer_request)
    once(
        builder.status_path(paths, "node-chiap02", source["card"]),
        dict(
            schema="skfleet.builder-dispatch-status/v1",
            node="node-chiap02",
            card_id=source["card"],
            request_id="f" * 64,
            state="awaiting-review",
            exit_code=0,
            claim_released=False,
            owner=source["owner"],
            claim_revision=source["claim"],
            source_artifact=artifact,
            production=producer_request["production"],
            unit=production.unit_name(producer_request, 1),
            invocation="b" * 32,
            attempt=1,
            writer=dict(role="sknoded", node="node-chiap02"),
        ),
    )
    operator = store.Writer(role="operator", node="fixture", identity="fixture")
    store.write_spec(
        paths,
        "node",
        "node-chiap03",
        {
            "role": "builder-standby",
            "actuate": True,
            "cordoned": False,
            "capabilities": ["remote-review-v1"],
        },
        writer=operator,
        labels={"host": "chiap03"},
    )
    monkeypatch.setattr(
        builder,
        "node_views",
        lambda p: [
            NodeView(
                "node-chiap03",
                "Ready",
                role="builder-standby",
                labels={"host": "chiap03"},
                capacity={"cores": 24, "ram_gb": 31},
                allocatable={"cores": 20, "ram_gb": 20},
            )
        ],
    )
    policy_path = home / "production.json"
    policy_path.write_text(json.dumps(remote_policy))
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", str(policy_path))
    monkeypatch.setenv("SKFLEET_AUTHORITY_HOST", "chiap08")
    monkeypatch.setenv("SKFLEET_PI", "/test/pi")
    monkeypatch.setattr(review, "_activation", lambda *args: "a" * 64)
    monkeypatch.setattr(review, "process_snapshot", lambda card: dict(sessions=[], units=[]))
    monkeypatch.setattr(builder, "_guard_path", lambda: "/test/guard")
    from skcapstone.fleet import worker_git

    monkeypatch.setattr(worker_git, "preflight", lambda *args: None)
    snap = snapshot()
    monkeypatch.setattr(production.production_routes, "snapshot", lambda policy: snap)
    monkeypatch.setattr(production.production_routes, "preflight", lambda *args: {})
    (home / "evidence/fleet-review-routes.json").write_text(json.dumps(snap))
    host = ["chiap08"]
    monkeypatch.setattr(review.socket, "gethostname", lambda: host[0])
    observations = []
    capacity = [True]

    def resources(policy, machine, rows):
        observations.append(machine)
        return capacity[0] and machine == "chiap03", "memory_available=0 required=3221225472"

    monkeypatch.setattr(production_resources, "active_resource_units", lambda *a: [])
    monkeypatch.setattr(admission, "active_resource_units", lambda *a: [])
    monkeypatch.setattr(production_resources, "local_worker_admission", resources)
    monkeypatch.setattr(admission, "local_worker_admission", resources)
    launches = []
    state = {}

    def launch(argv, cwd):
        launches.append((argv, cwd))
        reservation = next(
            a.split("=", 2)[2] for a in argv if a.startswith("--setenv=SKFLEET_ADMISSION_ID=")
        )
        state.update(
            Id="skfleet-worker-deepseek-" + card + ".service",
            LoadState="loaded",
            ActiveState="active",
            InvocationID="c" * 32,
            SKFLEET_ADMISSION_ID=reservation,
            MemoryMax=str(3 * 1024**3),
            MemoryCurrent="0",
        )
        return object()

    monkeypatch.setattr(admission, "unit_state", lambda *a, **kw: dict(state))
    writer = store.Writer(role="scheduler", node="niobe", identity="fixture-authority")
    return dict(
        home=home,
        paths=paths,
        card=card,
        policy=remote_policy,
        host=host,
        observations=observations,
        launches=launches,
        launcher=launch,
        capacity=capacity,
        writer=writer,
        source=source,
        state=state,
    )


def offer_and_consume(e):
    request = review.offer_review(e["paths"], e["home"], e["card"], writer=e["writer"])
    assert request is not None
    e["host"][0] = "chiap03"
    status = builder.consume_one(e["paths"], e["home"], "node-chiap03", launcher=e["launcher"])
    return request, status


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


@pytest.fixture
def terminal_review(execution, monkeypatch, request):
    variant = getattr(request, "param", "PASS")
    verdict = "PASS" if variant == "LARGE" else variant
    if verdict == "STRUCTURED_BLOCKED":
        verdict = "BLOCKED blocked_on=capability referent=review-input missing prerequisite"
    import hashlib
    from skcapstone.fleet.production_review_finish import native_command, native_state, once
    from skcapstone.fleet.production_review_custody import exit_path
    from tests.fleet.test_source_bundle import git

    e = execution
    request, status = offer_and_consume(e)
    assert status and status["state"] == "running"
    workspace = Path(status["workspace"])
    git(workspace, "config", "user.name", "Reviewer fixture")
    git(workspace, "config", "user.email", "review@example.invalid")
    directory = workspace / "docs/evidence/agents" / e["card"]
    directory.mkdir(parents=True)
    report = directory / "COMPLETION-EVIDENCE.md"
    import base64, os

    report.write_text(
        base64.b64encode(os.urandom(195000)).decode()
        if variant == "LARGE"
        else "Real independent review of original candidate.\n"
    )
    report.chmod(0o600)
    report_hash = hashlib.sha256(report.read_bytes()).hexdigest()
    decision = dict(
        schema="skfleet.source-review-decision/v1",
        **review.proposal_binding(request),
        verdict=verdict,
        report_sha256=report_hash,
    )
    (directory / "REVIEW-DECISION.json").write_text(json.dumps(decision))
    (directory / "REVIEW-DECISION.json").chmod(0o600)
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "independent review")
    claim, owner = status["claim_revision"], request["reviewer"]
    applicability = dict(
        type="source-only-applicability",
        card_id=e["card"],
        source_head=request["source"]["head"],
        reviewer=owner,
        evidence_digest=report_hash,
        governed_pr_ci=False,
    )
    for key, value in [
        ("evidence", str(report)),
        ("reviewer_evidence_sha256", report_hash),
        (
            "verdict",
            (
                "BLOCKED blocked_on=capability referent=review-input missing prerequisite"
                if verdict == "BLOCKED"
                else verdict
            ),
        ),
        ("applicability_receipt", json.dumps(applicability)),
    ]:
        before = native_state(e["home"], e["card"])
        native_command(
            e["home"],
            [
                "link",
                e["card"],
                key,
                value,
                "--agent",
                owner,
                "--expected-source-revision",
                before["revision"],
                "--expected-claim-revision",
                claim,
                "--transition-id",
                hashlib.sha256(key.encode()).hexdigest(),
                "--json",
            ],
        )
    terminal = dict(
        schema="skfleet.production-review-exit/v1",
        card=e["card"],
        owner=owner,
        claim_revision=claim,
        host="chiap03",
        lane="deepseek",
        model="gateway-current",
        unit=status["execution"]["unit"],
        invocation="c" * 32,
        workspace=str(workspace),
        source_head=request["source"]["head"],
        exit_code=0,
        execution=status["execution"],
    )
    once(exit_path(e["home"], e["card"], claim), terminal)
    from skcapstone.fleet import production_review_custody as custody
    from skcapstone.fleet import production_acceptance as acceptance

    monkeypatch.setattr(custody, "unit_terminal", lambda *a, **kw: dict(ActiveState="inactive"))
    monkeypatch.setattr(acceptance, "unit_terminal", lambda *a, **kw: dict(ActiveState="inactive"))
    status = review.reconcile_review(e["paths"], e["home"], request, status)
    assert status["state"] == (
        "awaiting-review-acceptance"
        if verdict == "PASS"
        else "review-" + verdict.split()[0].lower()
    )
    e.update(request=request, status=status, workspace=workspace, terminal=terminal)
    return e


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
