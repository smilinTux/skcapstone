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
    runtime_dir = home / "test-runtime"
    runtime_dir.mkdir()
    pi = runtime_dir / "pi"
    pi.write_text("#!/bin/sh\nexit 0\n")
    wrapper = runtime_dir / "skfleet-worker-wrapper.py"
    wrapper.write_text("# synthetic review wrapper\n")
    monkeypatch.setenv("SKFLEET_PI", str(pi))
    monkeypatch.setattr(production, "review_wrapper_path", lambda: str(wrapper))
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
