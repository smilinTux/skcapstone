"""Focused parity tests for governed-review admission diagnostics."""

from __future__ import annotations

import datetime
import json

import pytest
from click.testing import CliRunner
from skcoord.card_store import CardCore

from skcapstone.card_store import CardStore
from skcapstone.cli import main
from skcapstone.review_admission import (
    assert_governed_review_claim,
    governed_review_gate_reasons,
    reviewer_candidate_reasons,
)


def _write_gateway_capacity(
    home, *, active: int = 0, maximum: int = 1, routes: tuple[str, ...] = ("sk-mid",)
) -> None:
    path = home / "evidence" / "fleet-review-routes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "error": None,
                "routes": [
                    {
                        "logical_route": route,
                        "model_or_bucket": route,
                        "provider": "qualified-provider-" + str(index),
                        "capacity_domain": "qualified-domain-" + str(index),
                        "size_class": "M",
                        "policy_tier": "standard",
                        "state": "healthy",
                        "max": maximum,
                        "gateway_active": active,
                    }
                    for index, route in enumerate(routes)
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_original_incomplete_review_shape_reports_exact_reasons() -> None:
    core = {"title": "Review CapAuth candidate", "links": {}, "meta": {}}

    assert governed_review_gate_reasons(
        core,
        ["review", "seat-atlas"],
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


@pytest.mark.parametrize("seat", ["link", "mero", "seraph"])
def test_governed_review_accepts_host_neutral_logical_seats(seat: str) -> None:
    core = {
        "meta": {
            "producer_identity": "source-producer",
            "candidate_evidence_sha256": "a" * 64,
            "link_source_card": "source01",
            "link_head_revision": "b" * 40,
        }
    }

    assert governed_review_gate_reasons(core, ["review", f"seat-{seat}"]) == ()


@pytest.mark.parametrize(
    ("identity", "seat"),
    [
        ("codex-link-aabb0005", "link"),
        ("pi-mero-worker", "mero"),
        ("pi-seraph-worker", "seraph"),
        ("qualified-reviewer", "qualified-route"),
    ],
)
def test_shared_candidate_contract_accepts_qualified_available_fallback(
    identity: str, seat: str
) -> None:
    assert (
        reviewer_candidate_reasons(
            identity,
            producer="source-producer",
            qualified_seats={"link", "mero", "seraph", "qualified-route"},
            declared_seat=seat,
        )
        == ()
    )


def test_shared_candidate_contract_fails_closed() -> None:
    assert reviewer_candidate_reasons(
        "codex-link-aabb0005",
        producer="link-producer",
        declared_seat="link",
    ) == ("producer-self-review",)
    assert reviewer_candidate_reasons(
        "unknown-worker",
        producer="source-producer",
        declared_seat="unknown",
    ) == ("unqualified-reviewer",)
    assert reviewer_candidate_reasons(
        "pi-seraph-worker",
        producer="source-producer",
        declared_seat="seraph",
        available=False,
        capacity_available=False,
    ) == ("reviewer-unavailable", "capacity")
    assert reviewer_candidate_reasons(
        "pi-seraph-worker",
        producer="source-producer",
        declared_seat="seraph",
        expected_claim_revision="old-generation",
        current_claim_revision="new-generation",
    ) == ("stale-generation",)


@pytest.mark.parametrize(
    "agent",
    ["codex-link-aabb0006", "pi-mero-worker", "pi-seraph-worker"],
)
def test_atomic_claim_accepts_same_logical_reviewer_candidates(tmp_path, agent: str) -> None:
    _write_gateway_capacity(tmp_path)
    CardStore(tmp_path).create(
        CardCore(
            id="aabb0006",
            title="[S] cross-seat review",
            created_by="scheduler",
            initial_labels=["review", "seat-link"],
            meta={
                "producer_identity": "source-producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "source06",
                "link_head_revision": "b" * 40,
            },
        )
    )

    assert_governed_review_claim(tmp_path, "aabb0006", agent)


def test_gates_and_atomic_claim_share_seat_link_admission(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _write_gateway_capacity(tmp_path, routes=("sk-mid", "sk-s"))
    store = CardStore(tmp_path)
    store.create(
        CardCore(
            id="00acc20f",
            title="[S] cross-seat review reproduction",
            created_by="scheduler",
            initial_labels=["review", "seat-link"],
            meta={
                "producer_identity": "codex-c1c1e001",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "c1c1e001",
                "link_head_revision": "b" * 40,
            },
        )
    )

    gates = CliRunner().invoke(main, ["coord", "gates", "00acc20f", "--home", str(tmp_path)])
    assert gates.exit_code == 0, gates.output
    assert json.loads(gates.output)["eligible"] is True

    claim = CliRunner().invoke(
        main,
        [
            "coord",
            "claim",
            "00acc20f",
            "--agent",
            "codex-link-00acc20f",
            "--home",
            str(tmp_path),
        ],
    )
    assert claim.exit_code == 0, claim.output
    first = store.fold("00acc20f")
    assert first.owner == "codex-link-00acc20f"
    assert first.meta["_claim_revision"]

    stale = CliRunner().invoke(
        main,
        [
            "coord",
            "claim",
            "00acc20f",
            "--agent",
            "pi-seraph-other",
            "--home",
            str(tmp_path),
        ],
    )
    assert stale.exit_code == 1
    after = store.fold("00acc20f")
    assert after.owner == first.owner
    assert after.meta["_claim_revision"] == first.meta["_claim_revision"]


def test_atomic_claim_uses_all_qualified_size_compatible_gateway_slots(tmp_path) -> None:
    routes = ("sk-codex-mid", "sk-glm-s", "sk-kimi", "sk-cursor", "sk-qwen")
    _write_gateway_capacity(tmp_path, maximum=2, routes=routes)
    CardStore(tmp_path).create(
        CardCore(
            id="aabb0007",
            title="[S] provider-neutral review",
            created_by="scheduler",
            initial_labels=["review", "seat-link", "sk-s"],
            meta={
                "producer_identity": "source-producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "source07",
                "link_head_revision": "b" * 40,
            },
        )
    )

    assert_governed_review_claim(tmp_path, "aabb0007", "codex-link-aabb0007")


def test_atomic_claim_admits_configured_logical_reviewer_route(tmp_path) -> None:
    _write_gateway_capacity(tmp_path)
    CardStore(tmp_path).create(
        CardCore(
            id="aabb0008",
            title="[S] configured reviewer route",
            created_by="scheduler",
            initial_labels=["review", "seat-qualified-route", "sk-s"],
            meta={
                "producer_identity": "source-producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "source08",
                "link_head_revision": "b" * 40,
                "qualified_reviewer_seats": ["qualified-route"],
            },
        )
    )

    assert_governed_review_claim(tmp_path, "aabb0008", "qualified-route-worker")


def test_atomic_claim_fails_closed_when_gateway_capacity_is_exhausted(tmp_path) -> None:
    _write_gateway_capacity(tmp_path, active=1, maximum=1)
    CardStore(tmp_path).create(
        CardCore(
            id="aabb0009",
            title="[S] full gateway review",
            created_by="scheduler",
            initial_labels=["review", "seat-seraph", "sk-s"],
            meta={
                "producer_identity": "source-producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "source09",
                "link_head_revision": "b" * 40,
            },
        )
    )

    with pytest.raises(ValueError, match="capacity"):
        assert_governed_review_claim(tmp_path, "aabb0009", "pi-seraph-worker")


def test_coord_gates_reports_live_review_contract_reasons(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    monkeypatch.setenv("SKFLEET_SEAT_TARGET", "0")
    CardStore(tmp_path).create(
        CardCore(
            id="90dea47b",
            title="CapAuth candidate review",
            created_by="producer",
            dependencies=["deadbeef"],
            initial_labels=["review", "seat-atlas"],
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


def test_coord_gates_size_s_uses_healthy_larger_provider_slots(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    path = tmp_path / "evidence" / "fleet-review-routes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "error": None,
                "routes": [
                    {
                        "logical_route": "provider-large",
                        "model_or_bucket": "provider-large",
                        "provider": "configured-large",
                        "capacity_domain": "configured-large",
                        "size_class": "L",
                        "policy_tier": "standard",
                        "state": "healthy",
                        "max": 4,
                        "gateway_active": 1,
                    },
                    {
                        "logical_route": "provider-xl",
                        "model_or_bucket": "provider-xl",
                        "provider": "configured-xl",
                        "capacity_domain": "configured-xl",
                        "size_class": "XL",
                        "policy_tier": "standard",
                        "state": "healthy",
                        "max": 8,
                        "gateway_active": 0,
                    },
                    {
                        "logical_route": "provider-small-down",
                        "model_or_bucket": "provider-small-down",
                        "provider": "configured-small",
                        "capacity_domain": "configured-small",
                        "size_class": "S",
                        "policy_tier": "standard",
                        "state": "unknown",
                        "max": 2,
                        "gateway_active": 0,
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    directory = tmp_path / "fleet" / "direct-seats"
    directory.mkdir(parents=True)
    directory.joinpath("untyped.json").write_text(
        json.dumps(
            {
                "completion_state": "running",
                "heartbeat_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            }
        ),
        encoding="utf-8",
    )
    CardStore(tmp_path).create(
        CardCore(
            id="aabb00a1",
            title="[S] seraph review with larger free buckets",
            created_by="scheduler",
            initial_labels=["review", "seat-seraph", "size-s"],
            meta={
                "producer_identity": "source-producer",
                "candidate_evidence_sha256": "a" * 64,
                "link_source_card": "sourcea1",
                "link_head_revision": "b" * 40,
            },
        )
    )

    result = CliRunner().invoke(main, ["coord", "gates", "aabb00a1", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["capacity"] == {"busy": 0, "target": 11}
    assert report["eligible"] is True
    assert report["seat"] == "seraph"


def test_coord_gates_aggregates_all_qualified_gateway_slots(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    _write_gateway_capacity(tmp_path, active=1, maximum=3, routes=("sk-mid", "sk-s"))
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
            title="[S] first review",
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
            title="[S] second review",
            created_by="scheduler",
            initial_labels=["review", "seat-seraph"],
            meta={**metadata, "link_source_card": "source02"},
        )
    )

    result = CliRunner().invoke(main, ["coord", "gates", "aabb0002", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["capacity"] == {"busy": 0, "target": 4}
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


def test_coord_gates_reports_do_not_claim_exclusion(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    CardStore(tmp_path).create(
        CardCore(
            id="733e74b0",
            title="Correct source repository",
            created_by="scheduler",
            initial_labels=["source-only", "do-not-claim"],
        )
    )

    result = CliRunner().invoke(main, ["coord", "gates", "733e74b0", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["eligible"] is False
    assert report["reasons"] == ["do-not-claim"]


@pytest.mark.parametrize("action", ["archive", "void"])
def test_coord_gates_reports_folded_terminal_exclusion(tmp_path, monkeypatch, action) -> None:
    monkeypatch.setenv("SKCOORD_CARD_STORE", "1")
    store = CardStore(tmp_path)
    store.create(CardCore(id="aabb0004", title="terminal card", created_by="scheduler"))
    store.append_event("aabb0004", action, "scheduler")

    result = CliRunner().invoke(main, ["coord", "gates", "aabb0004", "--home", str(tmp_path)])

    assert result.exit_code == 0, result.output
    report = json.loads(result.output)
    assert report["eligible"] is False
    assert report["reasons"] == ["terminal"]


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
