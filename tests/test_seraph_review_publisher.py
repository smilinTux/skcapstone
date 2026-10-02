"""Adversarial tests for Seraph review publication trust boundaries."""

from __future__ import annotations

import hashlib
import os
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from skcapstone.card_store import CardCore, CardStore
from skcapstone.seraph_review_cardstore import (
    RECEIPT_LINK,
    LiveCardStoreGateway,
)
from skcapstone.seraph_review_publisher import (
    SERAPH_CAPAUTH_URI,
    AuthenticatedCaller,
    BranchProtection,
    CardSnapshot,
    ConnectorCapabilities,
    LivePullRequest,
    PublishedReview,
    ReviewPublicationError,
    SeraphPassEvidence,
    VerifiedCaller,
    _digest,
    publish_seraph_pass,
)

HEAD = "a" * 40
SOURCE_REVISION = "b" * 64
REVIEW_REVISION = "c" * 64
SERVICE = "seraph-review-bot"
REQUIRED = frozenset({"tests/unit-3.11", "tests/unit-3.12"})
EVIDENCE_BYTES = b"independent Seraph review: PASS\n"
EVIDENCE_HASH = hashlib.sha256(EVIDENCE_BYTES).hexdigest()


def evidence(**changes: object) -> SeraphPassEvidence:
    """Build hash-sealed Seraph PASS evidence."""
    values = {
        "repository": "smilinTux/skcapstone",
        "number": 471,
        "head_sha": HEAD,
        "source_card": "c184138e",
        "source_card_revision": SOURCE_REVISION,
        "review_card": "c1841391",
        "review_card_revision": REVIEW_REVISION,
        "reviewer_identity": SERAPH_CAPAUTH_URI,
        "verdict": "PASS",
    }
    values.update(changes)
    values.setdefault("evidence_sha256", EVIDENCE_HASH)
    return SeraphPassEvidence(**values)


def caller(**changes: object) -> AuthenticatedCaller:
    """Build an opaque caller presentation."""
    return AuthenticatedCaller(changes.get("presentation", "valid-presentation"))


class FakeCapAuth:
    """Authoritative verifier fixture with no credential material."""

    def __init__(self, *, outcome: str = "valid") -> None:
        self.outcome = outcome

    def verify(self, presentation: object, **request: str) -> VerifiedCaller:
        if self.outcome == "unavailable":
            raise ConnectionError("offline")
        if self.outcome == "negative" or presentation != "valid-presentation":
            raise ReviewPublicationError("capauth_verification_denied")
        principal = SERAPH_CAPAUTH_URI
        capabilities = frozenset({"forge-review:publish"})
        request_sha256 = request["request_sha256"]
        if self.outcome == "principal-mismatch":
            principal = "capauth:lumina@skworld.io"
        if self.outcome == "capability-mismatch":
            capabilities = frozenset({"forge-review:read"})
        if self.outcome == "binding-mismatch":
            request_sha256 = "0" * 64
        return VerifiedCaller(principal, capabilities, request_sha256)


def policy(required: frozenset[str] = REQUIRED) -> BranchProtection:
    """Build trusted, hash-sealed branch-protection input."""
    values = {
        "repository": "smilinTux/skcapstone",
        "base_branch": "main",
        "required_checks": sorted(required),
    }
    return BranchProtection(
        repository=values["repository"],
        base_branch=values["base_branch"],
        required_checks=required,
        policy_revision=_digest(values),
    )


class FakeProtections:
    """Trusted branch-protection reader fixture."""

    def __init__(self, value: BranchProtection | None = None) -> None:
        self.value = value or policy()

    def read(self, repository: str, base_branch: str) -> BranchProtection:
        return self.value


class FakeCards:
    """Current CardStore facts plus an idempotent receipt-event sink."""

    def __init__(self) -> None:
        self.snapshots = {
            "c184138e": CardSnapshot("c184138e", SOURCE_REVISION, "review", "PASS", "31f90374"),
            "c1841391": CardSnapshot("c1841391", REVIEW_REVISION, "done", "PASS", "c184138e"),
        }
        self.snapshots = {
            key: replace(
                value,
                repository="smilinTux/skcapstone",
                number=471,
                head_sha=HEAD,
                producer_identity="worker",
                reviewer_identity="seraph" if key == "c1841391" else None,
                candidate_evidence_sha256=EVIDENCE_HASH,
            )
            for key, value in self.snapshots.items()
        }
        self.events: dict[str, str] = {}
        self.fail_append = False

    def read_card(self, card_id: str) -> CardSnapshot:
        try:
            return self.snapshots[card_id]
        except KeyError as exc:
            raise ReviewPublicationError("card_not_found") from exc

    def append_publication_receipt(self, **values: object) -> str:
        if self.fail_append:
            raise ReviewPublicationError("cardstore_receipt_write_failed")
        transition_id = str(values["transition_id"])
        self.events.setdefault(transition_id, "event-1")
        return self.events[transition_id]


class FakeConnector:
    """In-memory connector fixture with no network or credential."""

    def __init__(self) -> None:
        self.live = LivePullRequest(
            repository="smilinTux/skcapstone",
            number=471,
            head_sha=HEAD,
            base_branch="main",
            author="jarvis1openclaw",
            checks={name: "success" for name in REQUIRED},
        )
        self.scope = ConnectorCapabilities(
            SERVICE,
            frozenset({"metadata:read", "contents:read", "pull_requests:write"}),
        )
        self.approval: PublishedReview | None = None
        self.create_calls = 0

    def capabilities(self) -> ConnectorCapabilities:
        return self.scope

    def read_pull_request(self, repository: str, number: int) -> LivePullRequest:
        return self.live

    def find_approval(
        self, repository: str, number: int, *, service_identity: str, head_sha: str
    ) -> PublishedReview | None:
        return self.approval

    def create_review(self, repository: str, number: int, **values: object) -> None:
        self.create_calls += 1
        self.approval = PublishedReview(
            "review-1",
            repository,
            number,
            str(values["commit_sha"]),
            str(values["service_identity"]),
            str(values["event"]),
        )


def publish(
    tmp_path: Path,
    connector: FakeConnector,
    *,
    cards: FakeCards | LiveCardStoreGateway | None = None,
    auth: AuthenticatedCaller | None = None,
    capauth: FakeCapAuth | None = None,
    protection: FakeProtections | None = None,
    review_evidence: SeraphPassEvidence | None = None,
):
    """Invoke the publisher through injected fixtures."""

    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir(exist_ok=True)
    artifact = artifact_root / "review.md"
    if not artifact.exists():
        artifact.write_bytes(EVIDENCE_BYTES)
    card_gateway = cards or FakeCards()
    if isinstance(card_gateway, FakeCards):
        snapshot = card_gateway.snapshots["c1841391"]
        if snapshot.evidence_path is None:
            card_gateway.snapshots["c1841391"] = replace(
                snapshot,
                evidence_path=str(artifact),
                evidence_sha256=EVIDENCE_HASH,
            )
    return publish_seraph_pass(
        review_evidence or evidence(),
        caller=auth or caller(),
        capauth=capauth or FakeCapAuth(),
        service_identity=SERVICE,
        connector=connector,
        cards=card_gateway,
        protections=protection or FakeProtections(),
        receipt_dir=tmp_path / "receipts",
        approved_evidence_root=artifact_root,
        clock=lambda: datetime(2026, 9, 7, tzinfo=timezone.utc),
    )


def test_valid_publication_is_durable_and_idempotent(tmp_path: Path) -> None:
    connector = FakeConnector()
    cards = FakeCards()

    first = publish(tmp_path, connector, cards=cards)
    second = publish(tmp_path, connector, cards=cards)

    assert first == second
    assert connector.create_calls == 1
    assert len(cards.events) == 1
    assert next((tmp_path / "receipts").glob("*.json")).stat().st_mode & 0o222 == 0


def test_forged_caller_is_rejected_by_authoritative_verifier(tmp_path: Path) -> None:
    connector = FakeConnector()

    with pytest.raises(ReviewPublicationError, match="capauth_verification_denied"):
        publish(
            tmp_path,
            connector,
            auth=caller(presentation="forged"),
        )

    assert connector.create_calls == 0


@pytest.mark.parametrize(
    ("outcome", "error"),
    [
        ("unavailable", "capauth_verifier_unavailable"),
        ("negative", "capauth_verification_denied"),
        ("principal-mismatch", "capauth_principal_mismatch"),
        ("capability-mismatch", "capauth_capability_mismatch"),
        ("binding-mismatch", "capauth_request_binding_mismatch"),
    ],
)
def test_capauth_verifier_fails_closed(tmp_path: Path, outcome: str, error: str) -> None:
    connector = FakeConnector()

    with pytest.raises(ReviewPublicationError, match=error):
        publish(tmp_path, connector, capauth=FakeCapAuth(outcome=outcome))

    assert connector.create_calls == 0


def test_caller_evidence_hash_substitution_is_rejected(tmp_path: Path) -> None:
    connector = FakeConnector()

    with pytest.raises(ReviewPublicationError, match="caller_evidence_hash_mismatch"):
        publish(
            tmp_path,
            connector,
            review_evidence=evidence(evidence_sha256="f" * 64),
        )

    assert connector.create_calls == 0


def test_modified_evidence_is_rejected(tmp_path: Path) -> None:
    connector = FakeConnector()
    cards = FakeCards()
    publish(tmp_path, connector, cards=cards)
    Path(cards.snapshots["c1841391"].evidence_path or "").write_bytes(b"modified\n")

    with pytest.raises(ReviewPublicationError, match="evidence_artifact_hash_mismatch"):
        publish(tmp_path, connector, cards=cards)


def test_evidence_symlink_and_path_escape_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_bytes(EVIDENCE_BYTES)
    for linked_path, error in (
        (outside, "evidence_artifact_invalid"),
        (tmp_path / "evidence" / "linked.md", "evidence_symlink_rejected"),
    ):
        connector = FakeConnector()
        cards = FakeCards()
        if linked_path.name == "linked.md":
            linked_path.parent.mkdir(exist_ok=True)
            os.symlink(outside, linked_path)
        cards.snapshots["c1841391"] = replace(
            cards.snapshots["c1841391"],
            evidence_path=str(linked_path),
            evidence_sha256=EVIDENCE_HASH,
        )
        with pytest.raises(ReviewPublicationError, match=error):
            publish(tmp_path, connector, cards=cards)
        assert connector.create_calls == 0


def test_replay_rejects_evidence_mutation(tmp_path: Path) -> None:
    connector = FakeConnector()
    cards = FakeCards()
    publish(tmp_path, connector, cards=cards)
    Path(cards.snapshots["c1841391"].evidence_path or "").write_bytes(b"changed\n")

    with pytest.raises(ReviewPublicationError, match="evidence_artifact_hash_mismatch"):
        publish(tmp_path, connector, cards=cards)

    assert connector.create_calls == 1


def test_nonexistent_card_fails_closed(tmp_path: Path) -> None:
    connector = FakeConnector()
    live_cards = LiveCardStoreGateway(tmp_path / "empty-home")

    with pytest.raises(ReviewPublicationError, match="card_not_found"):
        publish(tmp_path, connector, cards=live_cards)

    assert connector.create_calls == 0


def test_missing_required_check_is_rejected(tmp_path: Path) -> None:
    connector = FakeConnector()
    connector.live = replace(connector.live, checks={"tests/unit-3.11": "success"})

    with pytest.raises(ReviewPublicationError, match="required_check_missing"):
        publish(tmp_path, connector)

    assert connector.create_calls == 0


def test_broad_connector_credential_is_rejected(tmp_path: Path) -> None:
    connector = FakeConnector()
    connector.scope = replace(
        connector.scope,
        permissions=connector.scope.permissions | {"administration:write"},
    )

    with pytest.raises(ReviewPublicationError, match="connector_permissions_not_least_privilege"):
        publish(tmp_path, connector)

    assert connector.create_calls == 0


def test_stale_receipt_is_not_returned_after_head_change(tmp_path: Path) -> None:
    connector = FakeConnector()
    cards = FakeCards()
    publish(tmp_path, connector, cards=cards)
    connector.live = replace(connector.live, head_sha="d" * 40)

    with pytest.raises(ReviewPublicationError, match="stale_head"):
        publish(tmp_path, connector, cards=cards)


def test_replay_requires_live_github_review(tmp_path: Path) -> None:
    connector = FakeConnector()
    cards = FakeCards()
    publish(tmp_path, connector, cards=cards)
    connector.approval = None

    with pytest.raises(ReviewPublicationError, match="github_review_missing"):
        publish(tmp_path, connector, cards=cards)

    assert connector.create_calls == 1


def test_cardstore_receipt_failure_never_returns_success(tmp_path: Path) -> None:
    connector = FakeConnector()
    cards = FakeCards()
    cards.fail_append = True

    with pytest.raises(ReviewPublicationError, match="cardstore_receipt_write_failed"):
        publish(tmp_path, connector, cards=cards)

    assert connector.create_calls == 1
    assert len(list((tmp_path / "receipts").glob("*.json"))) == 1
    cards.fail_append = False
    receipt = publish(tmp_path, connector, cards=cards)
    assert receipt.github_review_id == "review-1"
    assert len(cards.events) == 1


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("status", "doing", "card_status_not_publishable"),
        ("verdict", "FAIL", "review_card_verdict_not_pass"),
        ("parent_id", "wrong-parent", "review_parent_mismatch"),
        ("revision", "d" * 64, "card_revision_mismatch"),
    ],
)
def test_review_card_state_is_verified(tmp_path: Path, field: str, value: str, error: str) -> None:
    connector = FakeConnector()
    cards = FakeCards()
    cards.snapshots["c1841391"] = replace(cards.snapshots["c1841391"], **{field: value})

    with pytest.raises(ReviewPublicationError, match=error):
        publish(tmp_path, connector, cards=cards)

    assert connector.create_calls == 0


@pytest.mark.parametrize("authorized", [False, True])
def test_shared_inspection_keeps_sibling_and_predecessor_checks(tmp_path, monkeypatch, authorized):
    """Reused stores preserve sibling conflicts and authorized predecessor checks."""
    import skcapstone.review_replacement as replacement

    home = tmp_path / "home"
    home.mkdir()
    store = CardStore(home)
    for card in ("review01", "review02"):
        store.create(CardCore(
            id=card, title="[S] Source", created_by="controller",
            meta={"repository": "smilinTux/skcapstone", "link_head_revision": HEAD},
        ))
        for label in ("review", "parent-source01"):
            store.append_event(card, "add_label", "controller", label=label)
    calls = []
    fold = CardStore.fold

    def replacement_fold(self, card_id):
        row = fold(self, card_id)
        if row is not None and card_id == "review01":
            row.meta["review_attempt"] = 1
        return row

    def predecessor(path, current, sibling, *, _store):
        calls.append(_store)
        assert path == home and current.id == "review01" and sibling.id == "review02"
        return authorized

    monkeypatch.setattr(replacement, "authorized_predecessor", predecessor)
    monkeypatch.setattr(CardStore, "fold", replacement_fold)
    gateway = LiveCardStoreGateway(home)
    shared = gateway.read_card("review01", _store=store)
    assert calls == [store]
    assert shared.unresolved_review is not authorized
    assert gateway.read_card("review01") == shared
    assert calls[-1] is not store


def test_live_cardstore_native_candidate_and_mediated_receipt(tmp_path, monkeypatch):
    """Current native outcomes and suffix review titles work through the real CLI."""
    from types import SimpleNamespace

    from click.testing import CliRunner

    from skcapstone.cli import main

    home = tmp_path / "card-home"
    home.mkdir()
    store = CardStore(home)
    meta = {
        "repository": "https://github.com/smilinTux/skcapstone",
        "pr": "https://github.com/smilinTux/skcapstone/pull/471",
    }
    store.create(CardCore(id="source01", title="[M] Source", created_by="worker", meta=meta))
    store.create(
        CardCore(
            id="review01",
            title="[S] Verify source [REVIEW]",
            created_by="link",
            initial_labels=["parent-source01", "seat-seraph", "review"],
            meta={
                **meta,
                "producer_identity": "worker",
                "link_head_revision": HEAD,
                "candidate_evidence_sha256": EVIDENCE_HASH,
            },
        )
    )
    store.append_event("source01", "move", "worker", column="review")
    store.append_event(
        "source01",
        "verdict",
        "worker",
        verdict="PASS_FOR_REVIEW",
        candidate_commit=HEAD,
        candidate_sha256=EVIDENCE_HASH,
    )
    store.append_event("review01", "link", "seraph", link_key="verdict", link_value="PASS")
    artifact_root = tmp_path / "evidence"
    artifact_root.mkdir()
    artifact = artifact_root / "review.md"
    artifact.write_bytes(EVIDENCE_BYTES)
    for key, value in {
        "evidence": str(artifact),
        "reviewer_evidence_sha256": EVIDENCE_HASH,
    }.items():
        store.append_event("review01", "link", "seraph", link_key=key, link_value=value)
    store.append_event("review01", "complete", "seraph")
    calls = []

    def mediated_run(argv, **kwargs):
        calls.append(argv)
        result = CliRunner().invoke(main, argv[1:])
        assert result.exit_code == 0, result.output
        return SimpleNamespace(returncode=result.exit_code)

    monkeypatch.setattr("skcapstone.seraph_review_cardstore.subprocess.run", mediated_run)
    gateway = LiveCardStoreGateway(home)
    source, review = gateway.read_card("source01"), gateway.read_card("review01")
    bound = evidence(
        source_card=source.card_id,
        source_card_revision=source.revision,
        review_card=review.card_id,
        review_card_revision=review.revision,
    )
    connector = FakeConnector()
    publish(tmp_path, connector, cards=gateway, review_evidence=bound)
    publish(tmp_path, connector, cards=gateway, review_evidence=bound)
    assert len(calls) == 1
    assert calls[0][:3] == ["skcapstone", "coord", "link"]
    assert RECEIPT_LINK in CardStore(home).fold("review01").links
    assert gateway.read_card("review01").revision == review.revision
