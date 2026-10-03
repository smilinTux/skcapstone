"""Native replacement preserves invalid evidence and never approves a producer."""

import hashlib
import json
import socket
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from skcapstone import review_replacement as replacement
from skcapstone.link_review_work import reconcile_review_work, review_card_id
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway
from skcapstone.seraph_review_contracts import _digest
from tests.test_guarded_review_work import prepared
from tests.test_provisional_verdict_producer import COMMIT, TREE, _run


def artifact(home, name, value):
    """Retain actual isolated receipt bytes under the private evidence boundary."""
    path = home / "evidence" / "work" / "incident" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) if isinstance(value, dict) else value)
    return {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}


@pytest.fixture
def invalid_review(tmp_path, monkeypatch):
    """Prepare native false CI, exact claims and immutable operator proof."""
    store, source, producer, evidence, args = prepared(tmp_path)
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    old = json.loads(result.output)["review_card_id"]
    owner, claim, invocation = f"pi-seraph-chiap08-{old}", "f" * 32, "a" * 32
    store.append_event(old, "claim", owner, owner=owner, claim_revision=claim)
    store.append_event(old, "link", owner, link_key="pr", link_value="local-branch-not-a-PR")
    store.append_event(old, "link", owner, link_key="ci_check_docs", link_value="SUCCESS invented")
    store.append_event(old, "link", owner, link_key="verdict", link_value="PASS")
    launch = artifact(
        tmp_path,
        "launch.json",
        {
            "schema": "skfleet.independent-review-checkout-readback/v1",
            "review_card": old,
            "source_head": COMMIT,
            "source_tree": TREE,
            "owner": owner,
            "claim_revision": claim,
            "unit": f"skfleet-worker-qwen-{old}.service",
            "unit_properties": {"InvocationID": invocation, "MainPID": "2147483647"},
        },
    )
    incident = artifact(
        tmp_path,
        "incident.json",
        {
            "schema": "skfleet.independent-review-discrepancy/v1",
            "review_card": old,
            "source_card": source,
            "source_head": COMMIT,
            "expected_invocation_id": invocation,
            "native_review": {"owner": owner, "claim_revision": claim},
            "unit": {"ActiveState": "inactive", "MainPID": "0", "InvocationID": ""},
            "committed_artifacts": {
                "COMPLETION-EVIDENCE.md": artifact(
                    tmp_path, "report.md", "Actual retained bad report"
                ),
                "REVIEW-DECISION.json": artifact(
                    tmp_path, "decision.json", {"source_head": "bad"}
                ),
            },
        },
    )
    invalidated = _run(
        tmp_path,
        "link",
        old,
        "operator_review_invalidation",
        incident["path"] + " sha256=" + incident["sha256"],
        "--agent",
        "jarvis",
    )
    assert invalidated.exit_code == 0, invalidated.output
    store = type(store)(tmp_path)
    # Native CLI legacy projections may have no event_id. Bind their exact bytes.
    matching = [
        row
        for row in replacement._events(store, old)
        if row.get("link_key") == "operator_review_invalidation"
    ]
    assert len(matching) == 1
    event = matching[0]
    assert "event_id" not in event
    store.append_event(
        old,
        "link",
        "jarvis",
        link_key="verdict",
        link_value=f"BLOCKED blocked_on=capability referent=review-evidence:{old} invalid review",
    )
    store.append_event(old, "add_label", "jarvis", label="do-not-claim")
    binding, canonical = replacement._source(tmp_path, source)
    assert canonical == old
    request = dict(
        schema=replacement.SCHEMA,
        **binding,
        predecessor=old,
        predecessor_owner=owner,
        predecessor_claim=claim,
        predecessor_revision=replacement._fold_digest(store.fold(old)),
        predecessor_events_sha256=_digest(replacement._events(store, old)),
        invalidation=dict(incident, event_sha256=_digest(event)),
        launch_receipt=launch,
        authority_host=socket.gethostname().split(".")[0],
    )
    props = dict(
        LoadState="loaded",
        ActiveState="inactive",
        SubState="dead",
        MainPID="0",
        InvocationID=invocation,
        ControlGroup="",
        Job="",
        TasksCurrent="0",
    )

    def systemd(argv, **kwargs):
        assert argv[:3] == ["systemctl", "--user", "show"]
        assert argv[3] == f"skfleet-worker-qwen-{old}.service"
        return subprocess.CompletedProcess(
            argv, 0, "\n".join(f"{k}={v}" for k, v in props.items()), ""
        )

    monkeypatch.setattr(replacement.subprocess, "run", systemd)
    return store, source, old, request, props


def authorize(home, request):
    """Invoke the same mediated API used by the operator CLI."""
    return replacement.authorize_replacement(home, request, actor="jarvis")


def test_default_id_bytes_remain_identical():
    fields = ("1234abcd", COMMIT, "a" * 64, "b" * 64, "review")
    assert review_card_id(*fields) == hashlib.sha256("\0".join(fields).encode()).hexdigest()[:8]


def test_native_applicability_refuses_retained_ci_even_after_later_receipt(tmp_path):
    from skcapstone.review_verdict import _source_only_applicability

    store, source, producer, evidence, args = prepared(tmp_path)
    opened = _run(tmp_path, *args)
    old = json.loads(opened.output)["review_card_id"]
    reviewer = "pi-seraph-native-" + old
    store.append_event(old, "claim", reviewer, owner=reviewer, claim_revision="f" * 32)
    report = artifact(tmp_path, "valid-report.md", "Synthetic actual review report")
    receipt = dict(
        type="source-only-applicability",
        card_id=old,
        source_head=COMMIT,
        reviewer=reviewer,
        evidence_digest=report["sha256"],
        governed_pr_ci=False,
    )

    def link(key, value):
        result = _run(tmp_path, "link", old, key, value, "--agent", reviewer)
        assert result.exit_code == 0, result.output

    link("verdict", "PASS")
    link("review_evidence", report["path"] + "|sha256=" + report["sha256"])
    link("applicability_receipt", json.dumps(receipt))
    assert _source_only_applicability(old, tmp_path)
    link("ci_check_docs", "SUCCESS unsupported")
    link("applicability_receipt", json.dumps(receipt))
    assert not _source_only_applicability(old, tmp_path)


def test_one_replacement_preserves_source_claim_and_bad_history(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    source_before = store._read_events(source)
    old_before = store._read_events(old)
    with pytest.raises(ValueError, match="card_pr_binding_invalid"):
        LiveCardStoreGateway(tmp_path).read_card(old)
    first = authorize(tmp_path, request)
    second = authorize(tmp_path, request)
    assert first["created"] and not second["created"]
    new = first["review_card_id"]
    assert new != old and second["review_card_id"] == new
    assert replacement.current_review_attempt(tmp_path, source, COMMIT) == new
    assert store._read_events(source) == source_before
    assert [r for r in store._read_events(old) if r["action"] != replacement.ACTION] == old_before
    assert store.fold(old).owner == request["predecessor_owner"]
    assert store.fold(old).meta["_claim_revision"] == request["predecessor_claim"]
    assert store.fold(old).links["ci_check_docs"] == "SUCCESS invented"
    assert len(store.list_card_ids()) == 3
    snapshot = LiveCardStoreGateway(tmp_path).read_card(new)
    assert snapshot.head_sha == COMMIT and not snapshot.unresolved_review
    with pytest.raises(ValueError, match="card_pr_binding_invalid"):
        LiveCardStoreGateway(tmp_path).read_card(old)


@pytest.mark.parametrize(
    "kind",
    [
        "claim",
        "source",
        "candidate",
        "old_event",
        "old_claim",
        "report",
        "decision",
        "outcome",
        "invalidation_digest",
    ],
)
def test_changed_custody_refuses_without_any_authorization(tmp_path, invalid_review, kind):
    store, source, old, request, props = invalid_review
    if kind == "claim":
        store.append_event(
            source,
            "claim",
            request["producer"],
            owner=request["producer"],
            claim_revision="b" * 32,
        )
    elif kind == "source":
        store.append_event(source, "describe", request["producer"], description="changed")
    elif kind == "candidate":
        from pathlib import Path

        row = store._read_events(source)[-1]
        Path(row["candidate_path"]).write_text("changed")
    elif kind == "old_event":
        store.append_event(old, "note", request["predecessor_owner"], text="late write")
    elif kind == "old_claim":
        store.append_event(
            old,
            "claim",
            request["predecessor_owner"],
            owner=request["predecessor_owner"],
            claim_revision="b" * 32,
        )
    elif kind in {"report", "decision"}:
        from pathlib import Path

        (
            tmp_path
            / "evidence/work/incident"
            / ("report.md" if kind == "report" else "decision.json")
        ).write_text("changed")
    elif kind == "invalidation_digest":
        request["invalidation"]["event_sha256"] = "0" * 64
    else:
        store.append_event(
            old, "link", request["predecessor_owner"], link_key="verdict", link_value="PASS"
        )
    with pytest.raises(ValueError):
        authorize(tmp_path, request)
    assert not any(r["action"] == replacement.ACTION for r in store._read_events(old))
    assert len(store.list_card_ids()) == 2


@pytest.mark.parametrize(
    "key,value",
    [
        ("ActiveState", "active"),
        ("MainPID", "123"),
        ("InvocationID", "b" * 32),
        ("ControlGroup", "/live"),
        ("Job", "88"),
        ("TasksCurrent", "1"),
        ("LoadState", "error"),
    ],
)
def test_live_or_wrong_invocation_refused(tmp_path, invalid_review, key, value):
    store, source, old, request, props = invalid_review
    props[key] = value
    with pytest.raises(ValueError, match="replacement"):
        authorize(tmp_path, request)
    assert len(store.list_card_ids()) == 2


def test_collected_unit_requires_retained_exact_proof(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    props.update(LoadState="not-found", InvocationID="", TasksCurrent="[not set]")
    assert authorize(tmp_path, request)["created"]


def test_unknown_collected_process_cannot_replace(tmp_path, invalid_review):
    import os

    store, source, old, request, props = invalid_review
    props.update(LoadState="not-found", InvocationID="")
    request["launch_receipt"] = artifact(
        tmp_path,
        "live-pid.json",
        {
            **json.loads(replacement._read(tmp_path, request["launch_receipt"])),
            "unit_properties": {"InvocationID": "a" * 32, "MainPID": str(os.getpid())},
        },
    )
    with pytest.raises(ValueError, match="death unproven"):
        authorize(tmp_path, request)


def test_same_authorization_concurrent_and_claimed_replay(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: authorize(tmp_path, request), range(2)))
    assert sum(row["created"] for row in results) == 1
    new = results[0]["review_card_id"]
    store.append_event(
        new, "claim", "pi-seraph-new", owner="pi-seraph-new", claim_revision="d" * 32
    )
    replay = authorize(tmp_path, request)
    assert replay["review_card_id"] == new and not replay["launchable"]
    assert store.fold(new).owner == "pi-seraph-new"


def test_lost_authorization_ack_resumes_same_attempt(tmp_path, invalid_review, monkeypatch):
    store, source, old, request, props = invalid_review
    original = type(store).append_event

    def lost(self, card, action, agent, **kwargs):
        row = original(self, card, action, agent, **kwargs)
        if action == replacement.ACTION:
            raise ValueError("lost authorization acknowledgement")
        return row

    monkeypatch.setattr(type(store), "append_event", lost)
    with pytest.raises(ValueError, match="lost authorization"):
        authorize(tmp_path, request)
    monkeypatch.setattr(type(store), "append_event", original)
    new = authorize(tmp_path, request)["review_card_id"]
    assert replacement.current_review_attempt(tmp_path, source, COMMIT) == new
    assert sum(r["action"] == replacement.ACTION for r in store._read_events(old)) == 1


def test_late_predecessor_writes_invalidate_current_attempt(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    new = authorize(tmp_path, request)["review_card_id"]
    store.append_event(
        old, "link", request["predecessor_owner"], link_key="verdict", link_value="PASS"
    )
    with pytest.raises(ValueError, match="predecessor"):
        replacement.current_review_attempt(tmp_path, source, COMMIT)
    with pytest.raises(ValueError, match="predecessor"):
        LiveCardStoreGateway(tmp_path).read_card(new)


def test_snapshot_loads_history_once_and_refreshes_after_legacy_write(
    tmp_path, invalid_review, monkeypatch
):
    """One read shares history; a later read must see legacy-only mutations."""
    import skcoord.card_store as native
    from skcoord.card import CardEvent, CardEventLog

    _, _, old, request, _ = invalid_review
    new = authorize(tmp_path, request)["review_card_id"]
    original, reads = native.load_legacy_mutations, []

    def load(home):
        reads.append(home)
        return original(home)

    monkeypatch.setattr(native, "load_legacy_mutations", load)
    gateway = LiveCardStoreGateway(tmp_path)
    assert not gateway.read_card(new).unresolved_review
    assert len(reads) == 1
    CardEventLog(tmp_path).append(
        CardEvent(
            card_id=old,
            action="link",
            writer="late-operator",
            link_key="late_custody_change",
            link_value="changed after the first snapshot",
        )
    )
    reads.clear()  # The native legacy writer performs its own separate validation.
    with pytest.raises(ValueError, match="predecessor"):
        gateway.read_card(new)
    assert len(reads) == 1


def test_arbitrary_attempt_cannot_bypass_native_authorization(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    item = dict(
        source_card=source,
        source_owner=request["producer"],
        head_revision=COMMIT,
        card_generation=request["source_generation"],
        base_revision="e" * 40,
        workspace_repository="https://github.com/org/repo.git",
        base_ref="main",
        reviewer_candidates=[{"identity": "pi-seraph-new"}],
        review_attempt="b" * 64,
    )
    with pytest.raises(ValueError, match="authorization"):
        reconcile_review_work(tmp_path, item, evidence_sha256=request["source_evidence_sha256"])
    assert len(store.list_card_ids()) == 2


def test_producer_cannot_self_authorize(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    with pytest.raises(ValueError, match="operator identity"):
        replacement.authorize_replacement(tmp_path, request, actor=request["producer"])


def test_snapshot_can_read_completed_review_after_guarded_source_progress(
    tmp_path, invalid_review
):
    store, source, old, request, props = invalid_review
    new = authorize(tmp_path, request)["review_card_id"]
    store.append_event(source, "link", request["producer"], link_key="verdict", link_value="PASS")
    store.append_event(source, "complete", request["producer"])
    assert LiveCardStoreGateway(tmp_path).read_card(new).head_sha == COMMIT
    with pytest.raises(ValueError, match="source"):
        replacement.current_review_attempt(tmp_path, source, COMMIT)


def test_native_cli_requires_private_exact_authorization(tmp_path, invalid_review):
    store, source, old, request, props = invalid_review
    path = tmp_path / "authorization.json"
    path.write_text(json.dumps(request))
    path.chmod(0o644)
    args = ["review-replace", source, "--authorization", str(path), "--agent", "jarvis"]
    refused = _run(tmp_path, *args)
    assert refused.exit_code != 0 and "private regular" in refused.output
    path.chmod(0o600)
    checked = _run(tmp_path, *args, "--check")
    assert checked.exit_code == 0, checked.output
    assert json.loads(checked.output)["state"] == "validated-not-opened"
    assert len(store.list_card_ids()) == 2
    assert not any(row["action"] == replacement.ACTION for row in store._read_events(old))
    result = _run(tmp_path, *args)
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["created"]
