"""Production opener uses the qualified native review API and real source custody."""

import ast
import hashlib
import json
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from tests.fleet.test_production_custody import review_candidate  # noqa: F401
from tests.fleet.test_source_bundle import git, source  # noqa: F401
from tests.test_skfleet_provisional_opener import (  # noqa: F401
    ROTATE,
    OpenerHarness,
    qualified_review_api,
)

pytestmark = pytest.mark.host_systemd


@pytest.fixture
def canonical_opener(review_candidate, qualified_review_api, tmp_path):  # noqa: F811
    """Run the real qualified CLI only against a synthetic private CardStore."""
    value = review_candidate
    board = OpenerHarness(tmp_path / "opener")
    board.cards = value["home"] / "cards"
    store = value["store"]
    events = store._read_events(value["card"])
    outcome = next(row for row in reversed(events) if row.get("action") == "verdict")
    board.outcomes[value["card"]] = (outcome["ts"], "PASS_FOR_REVIEW")

    def lifecycle(cid):
        card = store.fold(cid)
        if card.archived:
            return "void"
        if card.status.value == "done":
            return "complete"
        return "claimed" if card.owner else "open"

    def run(command, **kwargs):
        board.calls.append(command)
        assert command[1:3] == ["coord", "review-work"]
        env = dict(kwargs.pop("env"))
        env.pop("PYTHONPATH", None)
        return subprocess.run(command, **kwargs, env=env, cwd=tmp_path)

    board.ns.update(
        CARDS=str(board.cards),
        Path=Path,
        PRODUCTION_POLICY=value["policy"],
        HOST="control",
        SKC=str(qualified_review_api),
        event_rows=store._read_events,
        lifecycle_state=lifecycle,
        folded_labels=lambda cid, core: store.fold(cid).labels,
        _card_process_snapshot=lambda card: {"sessions": [], "units": []},
        subprocess=SimpleNamespace(run=run, SubprocessError=subprocess.SubprocessError),
    )
    return board, value


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_canonical_creation_survives_void_legacy_and_materializes_exact_source(
    canonical_opener, tmp_path, monkeypatch
):
    from skcapstone.fleet.source_bundle import import_review_source
    from skcapstone.review_replacement import current_review_attempt

    board, value = canonical_opener
    before = value["store"]._read_events(value["card"])
    ts, token = board.outcomes[value["card"]]
    legacy = board.ns["_review_card_id"](value["card"], ts, token)
    board.card(legacy, "[REVIEW][S] malformed legacy review", "parent-" + value["card"])
    value["store"].append_event(legacy, "void", "operator")
    expected = current_review_attempt(value["home"], value["card"], value["head"])
    assert expected != legacy
    assert board.open(1) == 1
    assert board.open(1) == 0
    assert len(board.calls) == 1
    review = value["store"].fold(expected)
    assert review.status.value == "review" and review.owner is None
    assert "source-only" in review.labels
    assert review.links["repository"] == value["remote"]
    assert review.links["base_revision"] == value["base"]
    assert review.links["base_ref"] == "main"
    assert review.meta["candidate_tree"] == value["tree"]
    assert review.meta["candidate_ref"] == value["outcome"]["candidate_ref"]
    assert review.meta["candidate_path"] == str(value["shared"])
    assert review.meta["link_head_revision"] == value["head"]
    # Exercise the actual production checkout selector that previously returned
    # None for the legacy card, then feed its exact result into source import.
    node = next(
        n
        for n in ast.parse(ROTATE.read_text()).body
        if isinstance(n, ast.FunctionDef) and n.name == "_source_workspace_spec"
    )
    namespace = {"re": re, "urlsplit": urlsplit, "PRODUCTION_POLICY": value["policy"]}
    exec(compile(ast.Module([node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    repository, base_ref, selected_head = namespace["_source_workspace_spec"](
        review.model_dump(), review.labels
    )
    assert (repository, base_ref, selected_head) == (value["remote"], "main", value["head"])
    workspace = tmp_path / "review-checkout"
    assert import_review_source(review.model_dump(), repository, selected_head, workspace)
    assert git(workspace, "rev-parse", "HEAD") == value["head"]
    assert git(workspace, "rev-parse", "HEAD^{tree}") == value["tree"]
    assert git(workspace, "status", "--porcelain") == ""
    assert workspace.stat().st_mode & 0o077 == 0
    _review_and_collect(value, review, workspace, monkeypatch)
    assert value["store"]._read_events(value["card"]) == before


def _review_and_collect(value, review, workspace, monkeypatch):
    """Complete a synthetic independent proposal and run the real acceptance join."""
    from skcapstone.fleet import production_acceptance as acceptance
    from skcapstone.fleet.production_brief import production_source_review_brief
    from skcapstone.fleet.production_review_custody import exit_path
    from skcapstone.fleet.production_review_finish import native_command, native_state, once
    from skcapstone.seraph_review_cardstore import LiveCardStoreGateway

    store, home = value["store"], value["home"]
    owner = "pi-seraph-control-" + review.id
    store.append_event(review.id, "claim", owner, owner=owner)
    review = store.fold(review.id)
    claim = review.meta["_claim_revision"]
    brief = production_source_review_brief(
        card_id=review.id,
        owner=owner,
        claim_revision=claim,
        workspace=str(workspace),
        source_head=value["head"],
        core=review.model_dump(),
        labels=review.labels,
    )
    assert value["head"] in brief and value["tree"] in brief
    assert "source-only-applicability" in brief and "REVIEW-DECISION.json" in brief
    directory = workspace / "docs/evidence/agents" / review.id
    directory.mkdir(parents=True, mode=0o700)
    report = directory / "COMPLETION-EVIDENCE.md"
    report.write_text("Synthetic reviewer: exact source checkout verified.\n")
    report.chmod(0o600)
    digest = hashlib.sha256(report.read_bytes()).hexdigest()
    decision = {
        "schema": "skfleet.source-review-decision/v1",
        "card": review.id,
        "parent_card": value["card"],
        "source_head": value["head"],
        "source_tree": value["tree"],
        "reviewer_identity": owner,
        "verdict": "PASS",
        "report_sha256": digest,
    }
    decision_path = directory / "REVIEW-DECISION.json"
    decision_path.write_text(json.dumps(decision))
    decision_path.chmod(0o600)
    git(workspace, "config", "user.name", "Synthetic Reviewer")
    git(workspace, "config", "user.email", "review@example.invalid")
    git(workspace, "add", ".")
    git(workspace, "commit", "-qm", "synthetic independent review")
    receipt = dict(
        type="source-only-applicability",
        card_id=review.id,
        source_head=value["head"],
        reviewer=owner,
        evidence_digest=digest,
        governed_pr_ci=False,
    )
    for key, content in (
        ("evidence", str(report)),
        ("review_evidence_sha256", digest),
        ("verdict", "PASS"),
        ("applicability_receipt", json.dumps(receipt)),
    ):
        native_command(
            home,
            [
                "link",
                review.id,
                key,
                content,
                "--agent",
                owner,
                "--expected-source-revision",
                native_state(home, review.id)["revision"],
                "--expected-claim-revision",
                claim,
                "--transition-id",
                hashlib.sha256(key.encode()).hexdigest(),
                "--json",
            ],
        )
    once(
        exit_path(home, review.id, claim),
        dict(
            schema="skfleet.production-review-exit/v1",
            card=review.id,
            owner=owner,
            claim_revision=claim,
            host="control",
            lane="deepseek",
            model="qualified-review",
            unit="skfleet-worker-deepseek-" + review.id + ".service",
            invocation="f" * 32,
            source_head=value["head"],
            exit_code=0,
            workspace=str(workspace),
        ),
    )
    store.append_event(
        review.id,
        "review_assignment_launch",
        owner,
        claim_revision=claim,
        launched=True,
        route_identity={
            "capacity_domains": ["deepseek"],
            "model_or_bucket": "qualified-review",
            "policy_sha256": acceptance.digest(value["policy"]),
        },
    )
    # External route qualification and real unit liveness have separate focused
    # tests. This synthetic integration exercises every source/lineage join.
    monkeypatch.setattr(acceptance, "LiveCardStoreGateway", LiveCardStoreGateway)
    monkeypatch.setattr(acceptance, "production_receipt_allowed", lambda *a: True)
    monkeypatch.setattr(acceptance, "unit_terminal", lambda *a, **k: {})
    context = acceptance.collect(
        home,
        value["policy"],
        review.id,
        claim,
        process_check=lambda cid: {"sessions": [], "units": []},
    )
    assert context["source"]["head"] == value["head"]
    assert context["review"]["card"] == review.id
    assert context["review"]["review_head"] == git(workspace, "rev-parse", "HEAD")
    assert git(Path(context["source_workspace"]), "rev-parse", "HEAD") == value["head"]
    assert store.fold(review.id).owner == owner


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize("fault", ["active-legacy", "unknown", "live", "held", "missing-api"])
def test_selection_fails_closed(canonical_opener, monkeypatch, fault):
    import sys

    board, value = canonical_opener
    if fault == "active-legacy":
        board.card("1234abcd", "[REVIEW][S] active", "parent-" + value["card"])
    elif fault in {"unknown", "live"}:
        board.ns["_card_process_snapshot"] = lambda card: (
            {} if fault == "unknown" else {"sessions": ["live"], "units": []}
        )
    elif fault == "held":
        value["store"].append_event(value["card"], "add_label", "operator", label="hold")
    else:
        monkeypatch.setitem(sys.modules, "skcapstone.review_replacement", None)
    assert board.open(1) == 0
    assert board.calls == []


@pytest.mark.parametrize("source", ["https"], indirect=True)
@pytest.mark.parametrize(
    "fault", ["source-race", "claim-race", "noop", "receipt", "binding", "duplicate"]
)
def test_native_write_or_readback_drift_blocks_admission(canonical_opener, fault):
    board, value = canonical_opener
    real_run = board.ns["subprocess"].run

    def run(command, **kwargs):
        store = value["store"]
        if fault == "source-race":
            store.append_event(value["card"], "add_label", "operator", label="changed")
        elif fault == "claim-race":
            store.append_event(value["card"], "claim", value["owner"], owner=value["owner"])
        elif fault == "noop":
            return SimpleNamespace(returncode=0, stdout="{}")
        result = real_run(command, **kwargs)
        if not result.returncode:
            receipt = json.loads(result.stdout)
            if fault == "receipt":
                result.stdout = "not JSON"
            elif fault == "binding":
                store.append_event(
                    receipt["review_card_id"],
                    "link",
                    "operator",
                    link_key="base_ref",
                    link_value="wrong",
                )
            elif fault == "duplicate":
                board.card("1234abcd", "[REVIEW][S] concurrent", "parent-" + value["card"])
        return result

    board.ns["subprocess"].run = run
    assert board.open(1) == 0
    assert board.ns["_REVIEW_READBACK_BLOCKED"]
    assert not any("OPENED_REVIEW|" in line for line in board.logs)


@pytest.mark.parametrize("source", ["https"], indirect=True)
def test_dry_run_uses_canonical_identity_without_writes(canonical_opener):
    from skcapstone.review_replacement import current_review_attempt

    board, value = canonical_opener
    before = value["store"].list_card_ids()
    expected = current_review_attempt(value["home"], value["card"], value["head"])
    assert board.open(1, dry_run=True) == 1
    assert any("review=" + expected in line for line in board.logs)
    assert value["store"].list_card_ids() == before
    assert board.calls == []
