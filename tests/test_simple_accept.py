"""`coord accept`: simple, fact-based acceptance of a source card."""

from __future__ import annotations

import hashlib
import os

import click
import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore, CardStore

from skcapstone.card import CardEvent, CardEventLog
from skcapstone.cli.coord import register_coord_commands
from skcapstone.review_verdict import recorded_verdict
from skcapstone.simple_accept import AcceptRefusedError, accept_source, log_shows_success

SRC = "a1b2c3d4"
REV = "e5f6a7b8"
HEAD = "3" * 40
PRODUCER = "producer-x"
REVIEWER = "reviewer-y"
OK_LOG = "collected 3 items\n\n===== 3 passed in 0.10s =====\n"


def _link(home, card, key, value, writer="fixture"):
    CardEventLog(home).append(
        CardEvent(card_id=card, action="link", link_key=key, link_value=value, writer=writer)
    )


def _build(
    home,
    *,
    reviewer=REVIEWER,
    review_head=HEAD,
    test_commit=HEAD,
    log_text=OK_LOG,
    sha=None,
    claim_source=False,
):
    store = CardStore(home)
    store.create(CardCore(id=SRC, kind="task", title="source work", created_by="t"))
    store.create(
        CardCore(
            id=REV,
            kind="task",
            title="check source work",
            created_by="t",
            meta={"link_source_card": SRC, "link_head_revision": review_head},
        )
    )
    store.append_event(SRC, "claim", PRODUCER, owner=PRODUCER)
    revision = store.fold(SRC).meta["_claim_revision"]
    store.append_event(
        SRC, "release_claim", PRODUCER, released_owner=PRODUCER, expected_claim_revision=revision
    )
    if claim_source:
        store.append_event(SRC, "claim", PRODUCER, owner=PRODUCER)
    _link(home, REV, "verdict", "PASS", reviewer)
    log = home / "test.log"
    log.write_text(log_text)
    digest = sha or hashlib.sha256(log.read_bytes()).hexdigest()
    _link(home, SRC, "producer_identity", PRODUCER)
    _link(home, SRC, "independent_review", REV)
    _link(home, SRC, "simple_finish_test_log", str(log))
    _link(home, SRC, "simple_finish_test_log_sha256", digest)
    _link(home, SRC, "simple_finish_test_commit", test_commit)
    return store


def _status(store, card):
    return store.fold(card).status.value


def test_happy_path_completes_both_and_records_links(tmp_path):
    store = _build(tmp_path)
    msg = accept_source(tmp_path, SRC, "lumina-nor")
    assert msg.startswith("accepted")
    assert _status(store, REV) == "done" and _status(store, SRC) == "done"
    links = CardStore(tmp_path).fold(SRC).links
    assert recorded_verdict(SRC, tmp_path) == "PASS"
    assert links["accepted_review"] == REV
    assert links["accepted_test_log_sha256"] == links["simple_finish_test_log_sha256"]


def test_idempotent_rerun(tmp_path):
    _build(tmp_path)
    accept_source(tmp_path, SRC, "lumina-nor")
    assert accept_source(tmp_path, SRC, "lumina-nor") == f"already accepted: {SRC}"


def test_same_principal_reviewer_refused(tmp_path):
    store = _build(tmp_path, reviewer=PRODUCER)
    with pytest.raises(AcceptRefusedError, match="self-review"):
        accept_source(tmp_path, SRC, "lumina-nor")
    assert _status(store, SRC) != "done"


def test_commit_mismatch_refused(tmp_path):
    _build(tmp_path, test_commit="4" * 40)
    with pytest.raises(AcceptRefusedError, match="tests ran on"):
        accept_source(tmp_path, SRC, "lumina-nor")


def test_log_sha_mismatch_refused(tmp_path):
    _build(tmp_path, sha="0" * 64)
    with pytest.raises(AcceptRefusedError, match="sha256 does not match"):
        accept_source(tmp_path, SRC, "lumina-nor")


def test_failed_log_refused(tmp_path):
    _build(tmp_path, log_text="== 2 failed, 5 passed in 1s ==\n")
    with pytest.raises(AcceptRefusedError, match="passing summary"):
        accept_source(tmp_path, SRC, "lumina-nor")


def test_non_operator_refused(tmp_path):
    _build(tmp_path)
    with pytest.raises(AcceptRefusedError, match="operator"):
        accept_source(tmp_path, SRC, "pi-glm-chiap08-a1b2c3d4")


def test_dead_owner_claim_auto_released(tmp_path):
    store = _build(tmp_path, claim_source=True)
    assert store.fold(SRC).owner == PRODUCER
    accept_source(tmp_path, SRC, "lumina-nor")
    assert _status(store, SRC) == "done"
    release = [e for e in store._read_events(SRC) if e["action"] == "release_claim"][-1]
    assert release["writer"] == "lumina-nor"
    assert release["finished_work_override_reason"] == "simple-accept"


def test_live_worker_blocks(tmp_path):
    store = _build(tmp_path, claim_source=True)
    claim = store.fold(SRC).meta["_claim_revision"]
    store.append_event(
        SRC,
        "link",
        "obs",
        link_key="worker_liveness",
        link_value=f"{PRODUCER}|{claim}|active",
        process_pid=os.getpid(),
    )
    with pytest.raises(AcceptRefusedError, match="live worker"):
        accept_source(tmp_path, SRC, "lumina-nor")
    assert _status(store, SRC) != "done"


def test_log_summary_parser():
    assert log_shows_success("Tests  12 passed (12)\n")
    assert not log_shows_success("12 passed, 1 error in 3s\n")
    assert not log_shows_success("no tests ran\n")


def test_cli_happy_and_refusal(tmp_path):
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    _build(tmp_path)
    run = lambda agent: CliRunner().invoke(  # noqa: E731
        main, ["coord", "accept", SRC, "--agent", agent, "--home", str(tmp_path)]
    )
    bad = run("pi-glm-chiap08-a1b2c3d4")
    assert bad.exit_code != 0 and "refused" in bad.output
    ok = run("lumina-nor")
    assert ok.exit_code == 0, ok.output
    assert "accepted" in ok.output
