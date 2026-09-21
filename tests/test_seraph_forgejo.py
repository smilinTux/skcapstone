"""Private-forge publication and current card-binding regression tests."""

import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.forgejo import SKGIT_REPOSITORY
from skcapstone.seat_boundaries import Action, BoundaryError, require_authority
from skcapstone.seraph_forgejo import (
    ForgejoReviewConnector,
    ProvisionedCredential,
    attest_private_credential,
    main,
)
from skcapstone.seraph_review_cardstore import LiveCardStoreGateway, _candidate
from skcapstone.seraph_review_contracts import ConnectorCapabilities, ReviewPublicationError
from tests.test_seraph_review_publisher import (
    HEAD,
    SERVICE,
    FakeCards,
    FakeConnector,
    evidence,
    publish,
)

ROOT = "/api/v1/repos/smilinTux/sklegal"


class FakeForge:
    """Serve real Forgejo response shapes without credentials or network."""

    def __init__(self):
        self.calls = []
        self.pr = {
            "number": 2,
            "state": "open",
            "merged": False,
            "head": {"sha": HEAD},
            "base": {"sha": "b" * 40, "ref": "main"},
            "user": {"login": "producer"},
        }
        self.reviews = []
        self.protection = {
            "rule_name": "main",
            "enable_status_check": True,
            "status_check_contexts": ["SKLegal CI / check"],
            "required_approvals": 1,
            "apply_to_admins": True,
            "block_on_rejected_reviews": True,
            "block_on_outdated_branch": True,
            "enable_push": False,
            "dismiss_stale_approvals": True,
            "ignore_stale_approvals": False,
        }
        self.branch = {"commit": {"id": "b" * 40}}
        self.parents = [{"sha": "b" * 40}]

    def request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if method == "POST":
            self.reviews.append(
                {
                    "id": 21,
                    "user": {"login": SERVICE},
                    "commit_id": payload["commit_id"],
                    "state": payload["event"],
                    "dismissed": False,
                    "stale": False,
                    "official": True,
                }
            )
            return self.reviews[-1]
        if path.endswith("branch_protections/main"):
            return self.protection
        if path.endswith("branches/main"):
            return self.branch
        if "/git/commits/" in path:
            return {"sha": path.rsplit("/", 1)[-1], "parents": self.parents}
        if "page=2" in path:
            return []
        if "/reviews?" in path:
            return self.reviews
        if "/statuses?" in path:
            return [
                {"id": 9, "context": "SKLegal CI / check", "status": "success"},
                {"id": 8, "context": "SKLegal CI / check", "status": "failure"},
            ]
        if path.startswith(ROOT + "/pulls?"):
            return [self.pr]
        if path == ROOT + "/pulls/2":
            return self.pr
        raise AssertionError(path)


def connector(forge):
    """Inject an explicitly test-only trusted credential attestor."""
    return ForgejoReviewConnector(
        forge,
        attest_capabilities=lambda client, repository: ConnectorCapabilities(
            SERVICE, frozenset({"write:repository"}), "forgejo", frozenset({SKGIT_REPOSITORY})
        ),
    )


class CredentialState:
    """Serve authoritative service-account scope records."""

    def __init__(self):
        self.user = {"login": SERVICE, "is_admin": False}
        self.repository = {"full_name": "smilinTux/sklegal", "private": True}
        self.teams = [
            {
                "name": "sklegal-seraph-reviewers",
                "includes_all_repositories": False,
                "can_create_org_repo": False,
                "units_map": {"repo.code": "read", "repo.pulls": "write", "repo.wiki": "none"},
            }
        ]

    def request(self, method, path, payload=None):
        if path == "/api/v1/user":
            return self.user
        if "teams?" in path:
            return self.teams if "page=1" in path else []
        if path == ROOT:
            return self.repository
        raise AssertionError(path)


def credential(**changes):
    token = "synthetic"
    values = {
        "token": token,
        "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
        "token_id": 17,
        "token_name": "sklegal-seraph-review-publisher",
        "scopes": ("write:repository",),
        "repositories": ("smilinTux/sklegal",),
    }
    values.update(changes)
    return ProvisionedCredential(**values)


def test_live_credential_attestation_requires_exact_identity_scope_and_team():
    state = CredentialState()
    value = attest_private_credential(state, SKGIT_REPOSITORY, credential())
    value.validate(SERVICE)


@pytest.mark.parametrize(
    "change,error",
    [
        (lambda state: state.user.update(login="producer"), "identity"),
        (lambda state: state.user.update(is_admin=True), "identity"),
        (lambda state: state.repository.update(full_name="smilinTux/other"), "identity"),
        (lambda state: state.teams[0].update(includes_all_repositories=True), "team"),
        (lambda state: state.teams[0]["units_map"].update({"repo.issues": "write"}), "team"),
    ],
)
def test_live_credential_attestation_rejects_broader_authority(change, error):
    state = CredentialState()
    change(state)
    with pytest.raises(ReviewPublicationError, match=error):
        attest_private_credential(state, SKGIT_REPOSITORY, credential())


@pytest.mark.parametrize(
    "binding",
    [
        credential(token_sha256="0" * 64),
        credential(token_id=0),
        credential(scopes=("all",)),
        credential(repositories=("smilinTux/other",)),
    ],
)
def test_live_credential_attestation_rejects_unsealed_or_broad_token(binding):
    with pytest.raises(ReviewPublicationError, match="scope"):
        attest_private_credential(CredentialState(), SKGIT_REPOSITORY, binding)


def test_forgejo_native_approval_exact_head_and_replay():
    forge = FakeForge()
    adapter = connector(forge)
    live = adapter.read_pull_request(SKGIT_REPOSITORY, 2)
    assert live.head_sha == HEAD
    assert live.checks == {"SKLegal CI / check": "success"}
    assert adapter.read(SKGIT_REPOSITORY, "main").required_checks == frozenset(live.checks)
    for _ in range(2):
        adapter.create_review(
            SKGIT_REPOSITORY,
            2,
            commit_sha=HEAD,
            event="APPROVE",
            service_identity=SERVICE,
            idempotency_key="c" * 64,
        )
    writes = [call for call in forge.calls if call[0] == "POST"]
    assert len(writes) == 1
    assert writes[0][1] == ROOT + "/pulls/2/reviews"
    assert writes[0][2] == {
        "event": "APPROVED",
        "commit_id": HEAD,
        "body": "Independent Seraph PASS. Publication receipt: " + "c" * 64,
    }
    approval = adapter.find_approval(SKGIT_REPOSITORY, 2, service_identity=SERVICE, head_sha=HEAD)
    assert approval.commit_sha == HEAD and approval.event == "APPROVE"


@pytest.mark.parametrize(
    "state,dismissed",
    [("REQUEST_CHANGES", False), ("APPROVED", True), ("COMMENT", False), ("APPROVED", None)],
)
def test_later_rejection_or_dismissal_does_not_reuse_old_approval(state, dismissed):
    forge = FakeForge()
    forge.reviews = [
        {
            "id": identity,
            "user": {"login": SERVICE},
            "commit_id": HEAD,
            "state": value,
            "dismissed": flag,
        }
        for identity, value, flag in [(1, "APPROVED", False), (2, state, dismissed)]
    ]
    with pytest.raises(ReviewPublicationError, match="forge_review_not_approved"):
        connector(forge).find_approval(
            SKGIT_REPOSITORY, 2, service_identity=SERVICE, head_sha=HEAD
        )


def test_authorization_missing_cannot_post():
    forge = FakeForge()
    with pytest.raises(ReviewPublicationError, match="attestation_unavailable"):
        ForgejoReviewConnector(forge).create_review(
            SKGIT_REPOSITORY,
            2,
            commit_sha=HEAD,
            event="APPROVE",
            service_identity=SERVICE,
            idempotency_key="c" * 64,
        )
    assert forge.calls == []


@pytest.mark.parametrize(
    "changes",
    [
        {"permissions": frozenset({"write:repository", "write:admin"})},
        {"repositories": frozenset()},
        {"forge": "github"},
    ],
)
def test_unqualified_scopes_fail_closed(changes):
    forge = FakeForge()
    capability = connector(forge).capabilities()
    adapter = ForgejoReviewConnector(
        forge, attest_capabilities=lambda *_: replace(capability, **changes)
    )
    with pytest.raises(ReviewPublicationError):
        adapter.capabilities()
    assert forge.calls == []


def test_stale_head_cannot_post():
    forge = FakeForge()
    with pytest.raises(ReviewPublicationError, match="stale_head"):
        connector(forge).create_review(
            SKGIT_REPOSITORY,
            2,
            commit_sha="f" * 40,
            event="APPROVE",
            service_identity=SERVICE,
            idempotency_key="c" * 64,
        )
    assert all(call[0] == "GET" for call in forge.calls)


@pytest.mark.parametrize(
    "changes",
    [
        {"enable_status_check": False},
        {"status_check_contexts": []},
        {"rule_name": "*"},
        {"required_approvals": 0},
        {"apply_to_admins": False},
        {"enable_push": True},
        {"block_on_rejected_reviews": False},
        {"block_on_outdated_branch": False},
        {"dismiss_stale_approvals": False},
        {"ignore_stale_approvals": True},
    ],
)
def test_absent_or_ambiguous_branch_policy_cannot_qualify(changes):
    forge = FakeForge()
    forge.protection.update(changes)
    with pytest.raises(ReviewPublicationError):
        connector(forge).read(SKGIT_REPOSITORY, "main")


@pytest.mark.parametrize(
    "card_id,changes,error",
    [
        ("c184138e", {"head_sha": "d" * 40}, "card_candidate_binding_mismatch"),
        ("c1841391", {"head_sha": "d" * 40}, "card_candidate_binding_mismatch"),
        ("c1841391", {"number": 2}, "card_candidate_binding_mismatch"),
        ("c1841391", {"repository": SKGIT_REPOSITORY}, "card_candidate_binding_mismatch"),
        ("c1841391", {"reviewer_identity": "link"}, "reviewer_attribution_mismatch"),
        ("c1841391", {"producer_identity": "seraph"}, "producer_reviewer_not_distinct"),
        ("c1841391", {"unresolved_review": True}, "unresolved_review_lineage"),
        (
            "c1841391",
            {"candidate_evidence_sha256": "d" * 64},
            "review_source_evidence_binding_mismatch",
        ),
    ],
)
def test_authoritative_card_bindings_reject_caller_substitution(tmp_path, card_id, changes, error):
    cards, adapter = FakeCards(), FakeConnector()
    cards.snapshots[card_id] = replace(cards.snapshots[card_id], **changes)
    with pytest.raises(ReviewPublicationError, match=error):
        publish(tmp_path, adapter, cards=cards)
    assert adapter.create_calls == 0


def test_matching_live_head_does_not_make_an_old_pass_current(tmp_path):
    adapter = FakeConnector()
    adapter.live = replace(adapter.live, head_sha="f" * 40)
    with pytest.raises(ReviewPublicationError, match="card_candidate_binding_mismatch"):
        publish(tmp_path, adapter, review_evidence=evidence(head_sha="f" * 40))
    assert adapter.create_calls == 0


def test_card_binding_does_not_confuse_forges_or_conflicting_metadata():
    card = SimpleNamespace(
        links={"pr": SKGIT_REPOSITORY + "/pulls/2"},
        meta={"repository": SKGIT_REPOSITORY, "link_head_revision": HEAD},
    )
    assert _candidate(card) == (SKGIT_REPOSITORY, 2, HEAD)
    card.links["pr"] = "https://github.com/smilinTux/sklegal/pull/2"
    with pytest.raises(ReviewPublicationError, match="card_pr_repository_mismatch"):
        _candidate(card)
    card.links["repository"] = "smilinTux/sklegal"
    with pytest.raises(ReviewPublicationError, match="card_binding_conflict"):
        _candidate(card)


@pytest.mark.parametrize("seat", ["link", "mero", "niobe", "atlas", "tank", "jarvis"])
def test_only_seraph_can_publish_a_review(seat):
    require_authority("seraph", Action.PUBLISH_REVIEW)
    with pytest.raises(BoundaryError):
        require_authority(seat, Action.PUBLISH_REVIEW)


def test_preflight_reports_actual_missing_request_and_unqualified_runtime(tmp_path, capsys):
    assert (
        main(
            [
                "preflight",
                "--request",
                str(tmp_path / "absent.json"),
                "--home",
                str(tmp_path / "home"),
                "--evidence-root",
                str(tmp_path),
            ]
        )
        == 2
    )
    output = capsys.readouterr().out
    assert '"local_evidence": "invalid_or_unavailable"' in output
    assert "authoritative_request_bound_capauth_verifier" in output


@pytest.mark.parametrize("changes", [{"stale": True}, {"official": False}, {"official": None}])
def test_stale_or_unofficial_approval_is_not_accepted(changes):
    forge = FakeForge()
    forge.reviews = [
        {
            "id": 1,
            "user": {"login": SERVICE},
            "commit_id": HEAD,
            "state": "APPROVED",
            "dismissed": False,
            "stale": False,
            "official": True,
            **changes,
        }
    ]
    with pytest.raises(ReviewPublicationError, match="forge_review_not_approved"):
        connector(forge).find_approval(
            SKGIT_REPOSITORY, 2, service_identity=SERVICE, head_sha=HEAD
        )


def test_another_reviewers_unresolved_rejection_blocks_publication():
    forge = FakeForge()
    forge.reviews = [
        {
            "id": 1,
            "user": {"login": "other-reviewer"},
            "commit_id": "f" * 40,
            "state": "REQUEST_CHANGES",
            "dismissed": False,
        }
    ]
    with pytest.raises(ReviewPublicationError, match="forge_unresolved_rejection"):
        connector(forge).create_review(
            SKGIT_REPOSITORY,
            2,
            commit_sha=HEAD,
            event="APPROVE",
            service_identity=SERVICE,
            idempotency_key="c" * 64,
        )
    assert all(call[0] == "GET" for call in forge.calls)


def test_changed_target_branch_blocks_publication():
    forge = FakeForge()
    forge.branch = {"commit": {"id": "f" * 40}}
    with pytest.raises(ReviewPublicationError, match="forge_base_changed"):
        connector(forge).read_pull_request(SKGIT_REPOSITORY, 2)


@pytest.mark.parametrize("parents", [[], [{"sha": "malformed"}], [{"sha": HEAD}]])
def test_missing_current_base_ancestry_fails_closed(parents):
    forge = FakeForge()
    forge.parents = parents
    with pytest.raises(ReviewPublicationError, match="ancestry"):
        connector(forge).read_pull_request(SKGIT_REPOSITORY, 2)


def test_replay_cannot_relabel_a_replacement_approval_as_the_old_receipt(tmp_path):
    adapter = FakeConnector()
    publish(tmp_path, adapter)
    adapter.approval = replace(adapter.approval, review_id="replacement-review")
    with pytest.raises(ReviewPublicationError, match="receipt_idempotency_conflict"):
        publish(tmp_path, adapter)


def test_private_publisher_exercises_full_validation_and_durable_replay(tmp_path):
    forge, cards = FakeForge(), FakeCards()
    cards.snapshots = {
        key: replace(value, repository=SKGIT_REPOSITORY, number=2)
        for key, value in cards.snapshots.items()
    }
    adapter = connector(forge)
    request = evidence(repository=SKGIT_REPOSITORY, number=2)
    receipt = publish(tmp_path, adapter, cards=cards, protection=adapter, review_evidence=request)
    assert receipt.repository == SKGIT_REPOSITORY
    assert (
        publish(tmp_path, adapter, cards=cards, protection=adapter, review_evidence=request)
        == receipt
    )
    assert len([call for call in forge.calls if call[0] == "POST"]) == 1
    assert len(cards.events) == 1


@pytest.mark.parametrize("state,latest", [("done", "FAIL"), ("doing", "PASS")])
def test_newer_sibling_outcome_and_nonterminal_pass_remain_unresolved(tmp_path, state, latest):
    store = CardStore(tmp_path)
    store.create(CardCore(id="source01", title="[M] Source", created_by="worker"))
    meta = {
        "repository": SKGIT_REPOSITORY,
        "pr": SKGIT_REPOSITORY + "/pulls/2",
        "link_head_revision": HEAD,
        "producer_identity": "worker",
    }
    for card_id in ("review01", "review02"):
        store.create(
            CardCore(
                id=card_id,
                title="[S] Candidate [REVIEW]",
                created_by="link",
                initial_labels=["parent-source01"],
                meta=meta,
            )
        )
        store.append_event(card_id, "link", "seraph", link_key="verdict", link_value="PASS")
        store.append_event(card_id, "move", "seraph", column="done")
    store.append_event("review02", "link", "seraph", link_key="result", link_value=latest)
    store.append_event("review02", "move", "seraph", column=state)
    assert LiveCardStoreGateway(tmp_path).read_card("review01").unresolved_review
