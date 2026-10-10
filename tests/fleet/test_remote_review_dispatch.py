"""D1 remote request, independence, custody and evidence regression contracts."""

import copy
import importlib
import json
from datetime import datetime, timezone

import pytest

from skcapstone.fleet import builder_dispatch as builder
from skcapstone.fleet.paths import FleetPaths

pytest_plugins = (
    "tests.fleet.test_production_review_evidence",
    "tests.fleet.test_remote_review_policy",
)


@pytest.fixture
def dispatch():
    return importlib.import_module("skcapstone.fleet.review_dispatch")


@pytest.fixture
def request_record(remote_policy):
    from skcapstone.fleet.production_builder import digest

    return dict(
        schema="skfleet.builder-dispatch/v2",
        work_kind="review",
        request_id="a" * 64,
        card_id="b4594faa",
        node="node-chiap03",
        reviewer="pi-seraph-chiap03-b4594faa",
        seat="seraph",
        capability="remote-review-v1",
        labels=["review", "seat-seraph", "source-only"],
        logical_route="sk-m",
        review_revision="b" * 64,
        source=dict(
            card="888b4895",
            revision="c" * 64,
            claim="d" * 32,
            owner="pi-codex-builder-node-chiap02-888b4895",
            family="codex",
            head="e" * 40,
            tree="f" * 40,
            evidence_sha256="1" * 64,
            manifest_sha256="2" * 64,
        ),
        production=dict(
            authority="chiap08",
            host="chiap03",
            policy_sha256=digest(remote_policy),
            resources=remote_policy["node_quotas"]["chiap03"],
            family="deepseek",
            provider_family="deepseek",
            provider="skgateway",
            model="review-model",
            capacity_domain="deepseek",
        ),
        policy=remote_policy,
        writer=dict(role="scheduler", node="niobe", identity="native-fixture"),
        lease_expires_at="2099-01-01T00:00:00Z",
    )


def test_review_contract_keeps_role_and_independent_family(
    dispatch, request_record, remote_policy
):
    dispatch.validate_contract(request_record, remote_policy, host="chiap03")
    assert request_record["labels"] == ["review", "seat-seraph", "source-only"]


def test_explicit_glm_review_contract_allows_a_distinct_same_family_reviewer(
    dispatch, request_record, remote_policy
):
    request_record["labels"].extend(["glm-only", "review-distinct-agent"])
    request_record["source"].update(owner="pi-glm-builder-node-chiap01-source1", family="glm")
    request_record["production"].update(family="glm", provider_family="glm", capacity_domain="glm")
    dispatch.validate_contract(request_record, remote_policy, host="chiap03")


def test_same_family_review_is_allowed_with_a_distinct_reviewer(
    dispatch, request_record, remote_policy
):
    """Chef 2026-10-10: independence is a distinct reviewer agent, not a family."""
    request_record["source"].update(owner="pi-glm-builder-node-chiap01-source1", family="glm")
    request_record["production"].update(family="glm", provider_family="glm", capacity_domain="glm")
    dispatch.validate_contract(request_record, remote_policy, host="chiap03")


def test_remote_partition_keeps_legacy_reviews_on_the_existing_seat(dispatch):
    native = (0, "review", "a1b2c3d4", {}, ["review", "seat-seraph", "source-only"])
    legacy = (1, "review", "e5f6a7b8", {}, ["review", "seat-seraph"])
    glm = (
        2,
        "review",
        "a9b8c7d6",
        {},
        ["review", "seat-seraph", "glm-only", "review-distinct-agent"],
    )
    same_family_unsafe = (3, "review", "a9b8c7d7", {}, ["review", "seat-seraph", "glm-only"])
    producer = (4, "ready", "a9b8c7d8", {}, ["source-only"])
    rows = [native, legacy, glm, same_family_unsafe, producer]

    remote, local = dispatch.partition_remote_reviews(rows, {"card_ids": None})

    # A legacy distinct GLM review has no sealed source manifest, so remote
    # offer can only refuse it; it must stay on the local seat path.
    assert remote == [native]
    assert local == [legacy, glm, same_family_unsafe, producer]
    assert dispatch.partition_remote_reviews(rows, {"card_ids": [legacy[2]]}) == ([], rows)


def test_offer_destination_tightens_group_writable_request_directory(dispatch, tmp_path):
    paths = FleetPaths(tmp_path / "fleet")
    directory = builder.request_path(paths, "node-chiap04", "8f4b04d4").parent
    directory.mkdir(parents=True)
    directory.chmod(0o775)

    assert dispatch._offer_directory_safe(paths, "node-chiap04", "8f4b04d4")
    assert directory.stat().st_mode & 0o777 == 0o755


def test_offer_destination_creates_private_request_directory(dispatch, tmp_path):
    paths = FleetPaths(tmp_path / "fleet")
    directory = builder.request_path(paths, "node-chiap04", "8f4b04d4").parent

    assert dispatch._offer_directory_safe(paths, "node-chiap04", "8f4b04d4")
    assert directory.stat().st_mode & 0o777 == 0o700


def test_offer_destination_rejects_symlink_request_directory(dispatch, tmp_path):
    paths = FleetPaths(tmp_path / "fleet")
    directory = builder.request_path(paths, "node-chiap04", "8f4b04d4").parent
    directory.parent.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    directory.symlink_to(target, target_is_directory=True)

    assert not dispatch._offer_directory_safe(paths, "node-chiap04", "8f4b04d4")


@pytest.mark.parametrize(
    "change",
    [
        "host",
        "approved-host",
        "quota",
        "policy",
        "source",
        "unknown-family",
        "contradiction",
        "same-principal",
        "writer",
        "capability",
        "seat",
        "expiry",
    ],
)
def test_request_tampering_denied(dispatch, request_record, remote_policy, change):
    r = request_record
    if change in ("host", "approved-host"):
        r["production"]["host"] = "chiap09" if change == "host" else "chiap04"
    elif change == "quota":
        del remote_policy["node_quotas"]["chiap03"]
    elif change == "policy":
        r["production"]["policy_sha256"] = "0" * 64
    elif change == "source":
        r["source"]["head"] = "../escape"
    elif change == "unknown-family":
        r["source"].update(owner="unknown", family="unknown")
    elif change == "contradiction":
        r["source"]["family"] = "glm"
    elif change == "same-principal":
        r["source"].update(owner=r["reviewer"].upper(), family="codex")
    elif change == "writer":
        r["writer"]["node"] = "worker"
    elif change == "capability":
        r["capability"] = "builder-v1"
    elif change == "seat":
        r["labels"].remove("seat-seraph")
    else:
        r["lease_expires_at"] = "2000-01-01T00:00:00Z"
    with pytest.raises(ValueError):
        dispatch.validate_contract(r, remote_policy, host="chiap03")


def test_expired_unknown_review_offer_remains_held(request_record):
    request_record["lease_expires_at"] = "2000-01-01T00:00:00Z"
    assert builder.request_holds_card(request_record, {}, datetime.now(timezone.utc))
    assert builder.request_holds_card(
        request_record, {"request_id": "different"}, datetime.now(timezone.utc)
    )


def test_exact_expired_review_can_resume_only_before_launch(
    dispatch, request_record, remote_policy, paths, tmp_path, monkeypatch
):
    request_record["lease_expires_at"] = "2000-01-01T00:00:00Z"
    monkeypatch.setattr(dispatch.production, "policy", lambda: remote_policy)
    monkeypatch.setattr(dispatch.socket, "gethostname", lambda: "chiap03")
    monkeypatch.setattr(dispatch, "_capable", lambda *_args: True)
    monkeypatch.setattr(dispatch.production, "validate_request", lambda *_args, **_kw: None)
    monkeypatch.setattr(
        dispatch.production,
        "ready_nodes",
        lambda *_args, **_kw: [type("Node", (), {"name": request_record["node"]})()],
    )
    monkeypatch.setattr(dispatch.dispatch, "_ready_builders", lambda *_args: [])
    monkeypatch.setattr(dispatch.store, "actuation_allowed", lambda *_args: True)
    monkeypatch.setattr(dispatch, "_recorded", lambda *_args: None)
    monkeypatch.setattr(dispatch, "_current", lambda *_args, **_kw: "current-review")
    monkeypatch.setattr(dispatch, "process_snapshot", lambda *_args: {"units": [], "sessions": []})
    recommendation = dict(
        action="review_assignment_recommendation",
        recommendation_id=request_record["request_id"],
        reviewer=request_record["reviewer"],
    )
    events = [recommendation]
    monkeypatch.setattr(dispatch.CardStore, "_read_events", lambda *_args: events)
    assert (
        dispatch.validate_request(paths, tmp_path, request_record["node"], request_record)
        == "current-review"
    )

    events.append({"action": "review_assignment_launch"})
    with pytest.raises(ValueError, match="remote review offer expired"):
        dispatch.validate_request(paths, tmp_path, request_record["node"], request_record)
    events.pop()
    status = dispatch.dispatch.status_path(
        paths, request_record["node"], request_record["card_id"]
    )
    status.parent.mkdir(parents=True, exist_ok=True)
    status.write_text("{}")
    with pytest.raises(ValueError, match="remote review offer expired"):
        dispatch.validate_request(paths, tmp_path, request_record["node"], request_record)


def test_historical_policy_widening_only_for_receipt_validation(
    dispatch, request_record, remote_policy
):
    newer = copy.deepcopy(remote_policy)
    newer["remote_review"]["destinations"].append("chiap04")
    with pytest.raises(ValueError):
        dispatch.validate_contract(request_record, newer, host="chiap03")
    dispatch.validate_contract(request_record, newer, host="chiap03", historical=True)
    newer["worker_destinations"].remove("chiap03")
    with pytest.raises(ValueError):
        dispatch.validate_contract(request_record, newer, host="chiap03", historical=True)


def test_offer_expiry_does_not_release_cross_seat_hold(paths, request_record):
    path = builder.request_path(paths, request_record["node"], request_record["card_id"])
    path.parent.mkdir(parents=True)
    request_record["lease_expires_at"] = "2000-01-01T00:00:00Z"
    path.write_text(json.dumps(request_record))
    assert "b4594faa" in builder.held_card_ids(paths)


def test_old_producer_eligibility_does_not_accept_review(request_record):
    assert not builder.eligible({"id": request_record["card_id"]}, request_record["labels"])


@pytest.mark.host_systemd
def test_review_bundle_roundtrip_inspects_real_commit(proposal, tmp_path):
    from skcapstone.fleet import source_bundle
    from skcapstone.fleet.production_review_evidence import inspect_proposal
    from tests.fleet.test_source_bundle import git

    workspace, binding, _, _ = proposal
    packet = source_bundle.export_review_packet(
        tmp_path / "home", workspace, binding, execution={"request_id": "a" * 64}
    )
    target = tmp_path / "authority"
    git(tmp_path, "clone", "-q", str(workspace), str(target))
    git(target, "checkout", "--detach", binding["source_head"])
    # Import has only the source checkout and transported packet, never worker path.
    source_bundle.import_review_packet(
        tmp_path / "home", packet, target, binding, execution={"request_id": "a" * 64}
    )
    actual = inspect_proposal(target, **binding)
    assert actual["review_head"] == git(workspace, "rev-parse", "HEAD")


@pytest.mark.parametrize(
    "change", ["report", "source", "symlink", "wrong-owner", "missing-commit"]
)
def test_review_packet_rejects_invalid_proposal(proposal, tmp_path, change):
    from skcapstone.fleet import source_bundle
    from tests.fleet.test_source_bundle import git

    workspace, binding, decision, report = proposal
    if change == "report":
        report.write_text("tampered")
    elif change == "source":
        (workspace / "source.py").write_text("changed source")
        git(workspace, "add", ".")
        git(workspace, "commit", "-qm", "forbidden source modification")
    elif change == "symlink":
        report.unlink()
        report.symlink_to("/etc/passwd")
    elif change == "wrong-owner":
        binding = {**binding, "reviewer_identity": "pi-seraph-other-12345678"}
    else:
        git(workspace, "checkout", "--detach", binding["source_head"])
    with pytest.raises(ValueError):
        source_bundle.export_review_packet(
            tmp_path / "home", workspace, binding, execution={"request_id": "a" * 64}
        )


def test_review_delta_never_exports_private_intermediate_history(proposal, tmp_path):
    from skcapstone.fleet import source_bundle
    from tests.fleet.test_source_bundle import git

    workspace, binding, decision, report = proposal
    git(workspace, "reset", "--soft", binding["source_head"])
    private = workspace / "original-private-packet.txt"
    private.write_text("must not enter transport")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "private intermediate")
    private.unlink()
    git(workspace, "add", "-u")
    git(workspace, "commit", "-qm", "remove private file from final tree")
    with pytest.raises(ValueError):
        source_bundle.export_review_packet(
            tmp_path / "home", workspace, binding, execution={"request_id": "a" * 64}
        )
