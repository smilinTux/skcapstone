"""Focused parity tests for governed-review admission diagnostics."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore

from skcapstone.card_store import CardStore
from skcapstone.cli import main
from skcapstone.review_admission import assert_governed_review_claim, governed_review_gate_reasons


def test_original_incomplete_review_shape_reports_exact_reasons() -> None:
    core = {"title": "Review CapAuth candidate", "links": {}, "meta": {}}

    assert governed_review_gate_reasons(
        core,
        ["review", "seat-link"],
        dependency_blocked=True,
        owned=True,
        capacity_available=False,
    ) == (
        "wrong-seat",
        "absent-typed-metadata",
        "absent-source-binding",
        "dependency",
        "ownership",
        "capacity",
    )


def test_complete_seraph_review_has_no_review_gate_reasons() -> None:
    core = {
        "title": "Review candidate",
        "meta": {
            "producer_identity": "producer",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "source01",
            "link_head_revision": "a" * 40,
        },
    }

    assert governed_review_gate_reasons(core, ["review", "seat-seraph"]) == ()


def test_coord_gates_reports_live_review_contract_reasons(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    monkeypatch.setenv("SKFLEET_SEAT_TARGET", "0")
    CardStore(tmp_path).create(
        CardCore(
            id="90dea47b",
            title="CapAuth candidate review",
            created_by="producer",
            dependencies=["deadbeef"],
            initial_labels=["review", "seat-link"],
            initial_owner="worker",
            initial_claim_revision="claim-revision",
        )
    )

    result = CliRunner().invoke(main, ["coord", "gates", "90dea47b", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["eligible"] is False
    assert report["reasons"] == [
        "wrong-seat",
        "absent-typed-metadata",
        "absent-source-binding",
        "dependency",
        "ownership",
        "capacity",
    ]


def test_coord_gates_uses_two_seat_seraph_default(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    monkeypatch.delenv("SKFLEET_SEAT_TARGET", raising=False)
    monkeypatch.delenv("SKFLEET_SERAPH_BATCH_SIZE", raising=False)
    metadata = {
        "producer_identity": "producer",
        "candidate_evidence_sha256": "a" * 64,
        "link_source_card": "source01",
        "link_head_revision": "b" * 40,
    }
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="aabb0001",
            title="first review",
            created_by="scheduler",
            initial_labels=["review", "seat-seraph"],
            initial_owner="pi-seraph-first",
            initial_claim_revision="revision-1",
            meta=metadata,
        )
    )
    store.create(
        CardCore(
            id="aabb0002",
            title="second review",
            created_by="scheduler",
            initial_labels=["review", "seat-seraph"],
            meta={**metadata, "link_source_card": "source02"},
        )
    )

    result = CliRunner().invoke(main, ["coord", "gates", "aabb0002", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["capacity"] == {"busy": 1, "target": 2}
    assert report["eligible"] is True

    claim = CliRunner().invoke(
        main,
        [
            "coord",
            "claim",
            "aabb0002",
            "--agent",
            "pi-seraph-second",
            "--home",
            str(tmp_path),
        ],
    )
    assert claim.exit_code == 0, claim.output
    assert store.fold("aabb0001").owner == "pi-seraph-first"
    assert store.fold("aabb0002").owner == "pi-seraph-second"


def test_governed_review_claim_rejects_exact_producer_identity(tmp_path) -> None:
    CardStore(tmp_path).create(
        CardCore(
            id="aabb0003",
            title="self review",
            created_by="scheduler",
            initial_labels=["review", "seat-seraph"],
            meta={
                "producer_identity": "pi-seraph-producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "source03",
                "link_head_revision": "b" * 40,
            },
        )
    )

    with pytest.raises(ValueError, match="producer-self-review"):
        assert_governed_review_claim(tmp_path, "aabb0003", "pi-seraph-producer")
