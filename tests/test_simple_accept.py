"""`coord accept`: simple, fact-based acceptance of a source card."""

from __future__ import annotations

import hashlib
import os
import subprocess
from types import SimpleNamespace

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


def _git(repo, *args):
    out = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        capture_output=True, text=True, check=True,
    )  # fmt: skip
    return out.stdout.strip()


def _commit(repo, name, text):
    (repo / name).write_text(text)
    _git(repo, "add", name)
    _git(repo, "commit", "-qm", f"edit {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def gitrepo(tmp_path):
    """Real repo: base on main; ff = fast-forwarded feature; sq = squashed feature."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    base = _commit(repo, "a.txt", "one\ntwo\nthree\n")
    _git(repo, "checkout", "-qb", "feat")
    _commit(repo, "b.txt", "bee\n")
    feat = _commit(repo, "c.txt", "sea\n")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--squash", "feat")
    _git(repo, "commit", "-qm", "squash feat")
    squash = _git(repo, "rev-parse", "HEAD")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-qb", "ffbranch", "main")
    ff = _commit(repo, "d.txt", "dee\n")
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "-q", "--ff-only", "ffbranch")
    _git(repo, "update-ref", "refs/remotes/origin/main", "main")
    _git(repo, "checkout", "-qb", "side", base)
    side = _commit(repo, "e.txt", "eee\n")
    _git(repo, "checkout", "-q", "main")
    return SimpleNamespace(path=repo, base=base, feat=feat, squash=squash, ff=ff, side=side)


def _acc(home, repo, head, actor="lumina-nor", **kw):
    kw.setdefault("merged_commit", head)
    return accept_source(home, SRC, actor, repo_path=repo.path, **kw)


def _link(home, card, key, value, writer="fixture"):
    CardEventLog(home).append(
        CardEvent(card_id=card, action="link", link_key=key, link_value=value, writer=writer)
    )


def _build(
    home,
    *,
    reviewer=REVIEWER,
    review_head=HEAD,
    test_commit=None,
    producer_link=PRODUCER,
    log_text=OK_LOG,
    sha=None,
    claim_source=False,
):
    test_commit = test_commit or review_head
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
    if producer_link:
        _link(home, SRC, "producer_identity", producer_link)
    _link(home, SRC, "independent_review", REV)
    _link(home, SRC, "simple_finish_test_log", str(log))
    _link(home, SRC, "simple_finish_test_log_sha256", digest)
    _link(home, SRC, "simple_finish_test_commit", test_commit)
    return store


def _status(store, card):
    return store.fold(card).status.value


def test_happy_path_completes_both_and_records_links(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.ff)
    msg = _acc(tmp_path, gitrepo, gitrepo.ff)
    assert msg.startswith("accepted")
    assert _status(store, REV) == "done" and _status(store, SRC) == "done"
    links = CardStore(tmp_path).fold(SRC).links
    assert recorded_verdict(SRC, tmp_path) == "PASS"
    assert links["accepted_review"] == REV
    assert links["accepted_test_log_sha256"] == links["simple_finish_test_log_sha256"]


def test_idempotent_rerun(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff)
    _acc(tmp_path, gitrepo, gitrepo.ff)
    assert _acc(tmp_path, gitrepo, gitrepo.ff) == f"already accepted: {SRC}"


def test_same_principal_reviewer_refused(tmp_path, gitrepo):
    store = _build(tmp_path, reviewer=PRODUCER, review_head=gitrepo.ff)
    with pytest.raises(AcceptRefusedError, match="self-review"):
        _acc(tmp_path, gitrepo, gitrepo.ff)
    assert _status(store, SRC) != "done"


def test_commit_mismatch_refused(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff, test_commit="4" * 40)
    with pytest.raises(AcceptRefusedError, match="tests ran on"):
        _acc(tmp_path, gitrepo, gitrepo.ff)


def test_log_sha_mismatch_refused(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff, sha="0" * 64)
    with pytest.raises(AcceptRefusedError, match="sha256 does not match"):
        _acc(tmp_path, gitrepo, gitrepo.ff)


def test_failed_log_refused(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff, log_text="== 2 failed, 5 passed in 1s ==\n")
    with pytest.raises(AcceptRefusedError, match="passing summary"):
        _acc(tmp_path, gitrepo, gitrepo.ff)


def test_non_operator_refused(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff)
    with pytest.raises(AcceptRefusedError, match="operator"):
        _acc(tmp_path, gitrepo, gitrepo.ff, actor="pi-glm-chiap08-a1b2c3d4")


def test_dead_owner_claim_auto_released(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.ff, claim_source=True)
    assert store.fold(SRC).owner == PRODUCER
    _acc(tmp_path, gitrepo, gitrepo.ff)
    assert _status(store, SRC) == "done"
    release = [e for e in store._read_events(SRC) if e["action"] == "release_claim"][-1]
    assert release["writer"] == "lumina-nor"
    assert release["finished_work_override_reason"] == "simple-accept"


def test_live_worker_blocks(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.ff, claim_source=True)
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
        _acc(tmp_path, gitrepo, gitrepo.ff)
    assert _status(store, SRC) != "done"


def test_log_summary_parser():
    assert log_shows_success("Tests  12 passed (12)\n")
    assert not log_shows_success("12 passed, 1 error in 3s\n")
    assert not log_shows_success("no tests ran\n")


def test_cli_happy_and_refusal(tmp_path, gitrepo):
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    _build(tmp_path, review_head=gitrepo.ff)
    base = ["coord", "accept", SRC, "--home", str(tmp_path)]
    merge = ["--merged-commit", gitrepo.ff, "--repo-path", str(gitrepo.path)]
    bad = CliRunner().invoke(main, [*base, "--agent", "pi-glm-chiap08-a1b2c3d4", *merge])
    assert bad.exit_code != 0 and "refused" in bad.output
    unmerged = CliRunner().invoke(main, [*base, "--agent", "lumina-nor"])
    assert unmerged.exit_code != 0 and "merged_commit" in unmerged.output
    ok = CliRunner().invoke(main, [*base, "--agent", "lumina-nor", *merge])
    assert ok.exit_code == 0, ok.output
    assert "accepted" in ok.output
    assert CardStore(tmp_path).fold(SRC).links["merged_commit"] == gitrepo.ff


def test_merged_via_ancestor(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff)
    assert "via ancestor" in _acc(tmp_path, gitrepo, gitrepo.ff)
    assert CardStore(tmp_path).fold(SRC).links["merged_commit"] == gitrepo.ff


def test_merged_via_card_link(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff)
    _link(tmp_path, SRC, "merged_commit", gitrepo.ff)
    msg = accept_source(tmp_path, SRC, "lumina-nor", repo_path=gitrepo.path)
    assert "via ancestor" in msg


def test_merged_via_squash_patch_id(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.feat)
    assert "via squash" in _acc(tmp_path, gitrepo, gitrepo.squash)
    assert _status(store, SRC) == "done"


def test_not_merged_refused(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.side)
    with pytest.raises(AcceptRefusedError, match="reviewed code not on origin/main"):
        _acc(tmp_path, gitrepo, gitrepo.side)
    assert _status(store, SRC) != "done"


def test_patch_id_mismatch_refused(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.side)
    with pytest.raises(AcceptRefusedError, match="patch-id"):
        _acc(tmp_path, gitrepo, gitrepo.squash)
    assert _status(store, SRC) != "done"


def test_missing_merged_commit_or_repo_path_refused(tmp_path, gitrepo):
    _build(tmp_path, review_head=gitrepo.ff)
    with pytest.raises(AcceptRefusedError, match="merged_commit"):
        accept_source(tmp_path, SRC, "lumina-nor", repo_path=gitrepo.path)
    with pytest.raises(AcceptRefusedError, match="--repo-path"):
        accept_source(tmp_path, SRC, "lumina-nor", merged_commit=gitrepo.ff)


def test_producer_override_works_and_is_recorded(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.ff, producer_link=None)
    with pytest.raises(AcceptRefusedError, match="self-review"):
        _acc(tmp_path, gitrepo, gitrepo.ff, producer=REVIEWER)
    assert _status(store, SRC) != "done"
    assert "accepted" in _acc(tmp_path, gitrepo, gitrepo.ff, producer="someone-else")
    links = CardStore(tmp_path).fold(SRC).links
    assert links["producer_identity_override"] == "someone-else"


def test_unknown_producer_refused_then_override_allows(tmp_path, gitrepo):
    store = CardStore(tmp_path)
    store.create(CardCore(id=SRC, kind="task", title="s", created_by="t"))
    store.create(
        CardCore(
            id=REV,
            kind="task",
            title="r",
            created_by="t",
            meta={"link_source_card": SRC, "link_head_revision": gitrepo.ff},
        )  # fmt: skip
    )
    _link(tmp_path, REV, "verdict", "PASS", REVIEWER)
    log = tmp_path / "test.log"
    log.write_text(OK_LOG)
    for k, v in {
        "simple_finish_test_log": str(log),
        "simple_finish_test_log_sha256": hashlib.sha256(log.read_bytes()).hexdigest(),
        "simple_finish_test_commit": gitrepo.ff,
    }.items():
        _link(tmp_path, SRC, k, v)
    with pytest.raises(AcceptRefusedError, match="producer identity of the source is unknown"):
        _acc(tmp_path, gitrepo, gitrepo.ff)
    with pytest.raises(AcceptRefusedError, match="self-review"):
        _acc(tmp_path, gitrepo, gitrepo.ff, producer=REVIEWER.upper())
    assert "accepted" in _acc(tmp_path, gitrepo, gitrepo.ff, producer=PRODUCER)
    assert CardStore(tmp_path).fold(SRC).links["producer_identity_override"] == PRODUCER


SKIP_LOG = "collected 4 items\n\n===== 3 passed, 1 skipped in 0.10s =====\n"


def test_skipped_tests_refused_and_allowed_with_flag(tmp_path, gitrepo):
    store = _build(tmp_path, review_head=gitrepo.ff, log_text=SKIP_LOG)
    with pytest.raises(AcceptRefusedError, match="skipped"):
        _acc(tmp_path, gitrepo, gitrepo.ff)
    assert _status(store, SRC) != "done"
    assert "accepted" in _acc(tmp_path, gitrepo, gitrepo.ff, allow_skips=True)
    assert CardStore(tmp_path).fold(SRC).links["accepted_allow_skips"] == "true"
