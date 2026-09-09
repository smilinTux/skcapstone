"""CLI coverage for atomic create-and-claim."""

from click.testing import CliRunner

from skcapstone.card_store import CardStore
from skcapstone.cli import main


def test_coord_create_claim_for_me_reports_exact_fold(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    monkeypatch.setattr("skcapstone.active_agent_name", lambda: "maker")

    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a1b2c3e1",
            "--title",
            "Atomic CLI",
            "--claim-for-me",
        ],
    )

    assert result.exit_code == 0, result.output
    card = CardStore(tmp_path).fold("a1b2c3e1")
    assert card is not None
    revision = card.meta["_claim_revision"]
    assert f"owner=maker status=doing claim_revision={revision}" in result.output


def test_coord_create_claim_for_me_fails_before_write_without_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    monkeypatch.setattr("skcapstone.active_agent_name", lambda: None)

    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "a1b2c3e2",
            "--title",
            "No identity",
            "--claim-for-me",
        ],
    )

    assert result.exit_code != 0
    assert "no active agent could be resolved" in result.output
    assert CardStore(tmp_path).fold("a1b2c3e2") is None


def test_coord_create_help_documents_atomic_example():
    result = CliRunner().invoke(main, ["coord", "create", "--help"])

    assert result.exit_code == 0
    assert "--claim-for-me" in result.output
    assert "without exposing an unowned card" in result.output


def test_governed_review_create_lists_all_missing_admission_fields(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "90dea47b",
            "--title",
            "[REVIEW] CapAuth candidate",
        ],
    )

    assert result.exit_code != 0
    for field in (
        "review label",
        "seat-seraph",
        "producer_identity",
        "candidate_evidence_sha256",
        "source_card",
        "head_revision",
    ):
        assert field in result.output
    assert CardStore(tmp_path).fold("90dea47b") is None


def test_complete_governed_review_metadata_is_stored_atomically(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    from skcoord.card_store import CardCore

    CardStore(tmp_path).create(CardCore(id="44ad0d49", title="source", created_by="source-worker"))
    digest = "a" * 64
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "90dea47c",
            "--title",
            "[REVIEW] CapAuth candidate",
            "--tag",
            "review",
            "--tag",
            "seat-seraph",
            "--tag",
            "parent-44ad0d49",
            "--producer-identity",
            "source-worker",
            "--candidate-evidence-sha256",
            digest,
            "--source-card",
            "44ad0d49",
            "--head-revision",
            "b" * 40,
        ],
    )

    assert result.exit_code == 0, result.output
    card = CardStore(tmp_path).fold("90dea47c")
    assert card is not None
    assert card.meta["producer_identity"] == "source-worker"
    assert card.meta["candidate_evidence_sha256"] == digest
    assert card.meta["link_source_card"] == "44ad0d49"
    assert card.meta["link_head_revision"] == "b" * 40


def test_ordinary_repair_card_does_not_require_review_metadata(tmp_path, monkeypatch):
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    from skcoord.card_store import CardCore

    CardStore(tmp_path).create(CardCore(id="deadbeef", title="source", created_by="source-worker"))
    result = CliRunner().invoke(
        main,
        [
            "coord",
            "create",
            "--home",
            str(tmp_path),
            "--id",
            "be4e7d38",
            "--title",
            "[COMPONENT][S][REPAIR] Repair ordinary producer work",
            "--tag",
            "parent-deadbeef",
        ],
    )

    assert result.exit_code == 0, result.output
    card = CardStore(tmp_path).fold("be4e7d38")
    assert card is not None
    assert card.meta == {}
