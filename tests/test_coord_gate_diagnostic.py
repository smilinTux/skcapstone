"""Focused parity tests for governed-review admission diagnostics."""

from __future__ import annotations

import json

from click.testing import CliRunner
from skcoord.card_store import CardCore

from skcapstone.card_store import CardStore
from skcapstone.cli import main
from skcapstone.coord_gate_diagnostic import diagnose
from skcapstone.review_admission import governed_review_gate_reasons


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
        },
    }

    assert governed_review_gate_reasons(core, ["review", "seat-seraph"]) == ()


def test_raw_void_event_denies_admission_even_when_folded_meta_is_empty(tmp_path) -> None:
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="8daa0872",
            title="Voided admission regression",
            created_by="producer",
            initial_labels=[],
        )
    )
    store.append_event("8daa0872", "void", "producer", reason="terminal")

    folded = store.fold("8daa0872")
    assert folded is not None
    report = diagnose(tmp_path, "8daa0872")
    assert report["eligible"] is False
    assert "voided-card" in report["reasons"]


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
        "dependency",
        "ownership",
        "capacity",
    ]
