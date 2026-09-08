"""Tests for the fenced Link and Mero recurring entrypoint."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

from skcapstone.card_store import CardCore, CardStore
from skcapstone.link_review_work import card_generation, reconcile_review_work
from skcapstone.seat_cycle_entrypoint import (
    link_operation,
    load_control_plane,
    run_cycle,
    seraph_operation,
)


def control(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"], "mero": ["chiap08"], "seraph": ["chiap08"]},
            }
        ),
        encoding="utf-8",
    )


def test_inactive_host_refuses_before_operation(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    called = []
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap01",
        operation=lambda: called.append(True) or {"recommendations": 1},
    )
    assert result.result == "inactive_host_refused"
    assert called == []
    health = (tmp_path / "home/coordination/seat-cycles/link.health.jsonl").read_text()
    assert "inactive_host_refused" in health


def test_dry_run_is_fenced_and_bounded(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    called = []
    result = run_cycle(
        seat="mero",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
        dry_run=True,
        operation=lambda: called.append(True) or {"recommendations": 9},
    )
    assert result.result == "dry_run"
    assert result.recommendations == 0
    assert called == []


def test_active_cycle_records_operation_summary(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
        operation=lambda: {"cards_examined": 4, "recommendations": 2, "suppressed": 1},
    )
    assert result.result == "complete"
    assert (tmp_path / "home/coordination/seat-cycles/link.cycle.receipts.jsonl").exists()
    assert result.cards_examined == 4
    assert result.recommendations == 2


def test_stale_feed_emits_only_signed_lineage_review_work(tmp_path: Path) -> None:
    home = tmp_path / "home"
    lineage = home / "coordination/link-lineage.json"
    lineage.parent.mkdir(parents=True)
    item = {
        "kind": "review-work",
        "reason": "missing_terminal_review",
        "repository": "org/repo",
        "pr": 7,
        "head_revision": "a" * 40,
        "base_revision": "b" * 40,
        "source_card": "source01",
        "card_generation": "2" * 64,
        "source_owner": "builder",
        "reviewer_candidates": [
            {"name": "Seraph", "seat": "seraph", "identity": "seraph", "eligible": True}
        ],
    }
    manifest = {
        "schema": "skfleet.link-lineage/v1",
        "source_revision": "1" * 64,
        "coverage": {"unresolved": 1},
        "records": {},
        "reviewer_candidates": [],
        "diagnostics": [],
        "unresolved_prs": [7],
        "review_work_recommendations": [item],
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    manifest["evidence_hash"] = hashlib.sha256(encoded.encode()).hexdigest()
    lineage.write_text(json.dumps(manifest))
    result = link_operation(home, home / "coordination/missing-feed.json", lineage)
    assert result["reason"].startswith("review_work_only:")
    assert result["recommendations"] == 1
    emitted = json.loads(
        (home / "coordination/seat-cycles/link.review-work.jsonl").read_text().strip()
    )
    assert emitted["head_revision"] == "a" * 40
    assert emitted["source_card"] == "source01"
    assert emitted["reconciliation"]["launchable"] is False
    assert emitted["reconciliation"]["reason"] == "source_card_missing"


def test_cycle_without_operation_is_explicit_noop(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    result = run_cycle(
        seat="link",
        home=tmp_path / "home",
        control_plane=control_path,
        local_host="chiap08",
    )
    assert result.result == "operation_not_configured"
    assert result.reason == "operation_not_configured"


def test_seraph_dispatch_is_exactly_one_claimed_live_and_seat_scoped(
    tmp_path, monkeypatch
) -> None:
    captured = {}
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            captured.update(kwargs["env"])
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    "LAUNCHED|chiap08|codex-auto-review01|review01|lane=codex|"
                    "model=sk-codex-mid|owner=pi-seraph-chiap08-review01|"
                    "claim_revision=revision-1\n"
                ),
            )
        return SimpleNamespace(returncode=0)

    card = SimpleNamespace(
        labels=["review", "seat-seraph"],
        status=SimpleNamespace(value="doing"),
        owner="pi-seraph-chiap08-review01",
        meta={"_claim_revision": "revision-1"},
        links={"producer_identity": "builder"},
    )

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_dispatch_complete"
    assert captured["SKFLEET_ONLY_SEAT"] == "seraph"
    assert captured["SKFLEET_MAX_LAUNCH"] == "1"
    assert captured["SKFLEET_TARGET"] == "1"
    assert calls[1][-1] == "skfleet-worker-codex-review01.service"


def test_seraph_zero_launch_is_not_reported_as_success(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="SELECTION_EMPTY\n"),
    )
    result = seraph_operation(tmp_path)
    assert result == {
        "cards_examined": 0,
        "recommendations": 0,
        "suppressed": 1,
        "reason": "seraph_launch_receipt_missing",
    }


def test_seraph_rejects_duplicate_launch_receipts(tmp_path, monkeypatch) -> None:
    receipt = (
        "LAUNCHED|chiap08|codex-auto-review01|review01|lane=codex|model=model|"
        "owner=pi-seraph-chiap08-review01|claim_revision=revision-1\n"
    )
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=receipt + receipt),
    )
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_launch_receipt_missing"
    assert result["recommendations"] == 0


def test_link_materialization_race_launches_once_and_replay_is_denied(
    tmp_path, monkeypatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    store = CardStore(home)
    store.create(
        CardCore(
            id="source01",
            title="Source candidate",
            created_by="builder",
            created_at="2026-09-08T00:00:00+00:00",
        )
    )
    item = {
        "source_card": "source01",
        "head_revision": "a" * 40,
        "card_generation": card_generation(store.fold("source01")),
        "source_owner": "builder",
        "reviewer_candidates": [{"name": "Seraph", "seat": "seraph", "identity": "seraph"}],
    }
    launched = False

    def run(command, **_kwargs):
        nonlocal launched
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        if launched:
            return SimpleNamespace(returncode=0, stdout="SELECTION_EMPTY\n")
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(
                pool.map(
                    lambda _: reconcile_review_work(home, item, evidence_sha256="f" * 64),
                    range(8),
                )
            )
        assert len({result.review_card_id for result in results}) == 1
        assert sum(result.created for result in results) == 1
        card_id = results[0].review_card_id
        owner = f"pi-seraph-chiap08-{card_id}"
        revision = "revision-1"
        store.append_event(
            card_id,
            "claim",
            owner,
            owner=owner,
            claim_revision=revision,
        )
        launched = True
        return SimpleNamespace(
            returncode=0,
            stdout=(
                f"LAUNCHED|chiap08|codex-auto-{card_id}|{card_id}|lane=codex|"
                f"model=sk-codex-mid|owner={owner}|claim_revision={revision}\n"
            ),
        )

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    first = seraph_operation(home)
    second = seraph_operation(home)

    assert first["reason"] == "seraph_dispatch_complete"
    assert second["reason"] == "seraph_launch_receipt_missing"
    review_cards = [card for card in store.list_cards() if "parent-source01" in card.labels]
    assert len(review_cards) == 1
    assert review_cards[0].owner.startswith("pi-seraph-")
    claim_events = []
    for path in (home / "cards" / review_cards[0].id / "events").glob("*.jsonl"):
        claim_events.extend(
            event
            for line in path.read_text().splitlines()
            if (event := json.loads(line)).get("action") == "claim"
        )
    assert len(claim_events) == 1


def test_control_plane_requires_both_seats(tmp_path: Path) -> None:
    path = tmp_path / "control.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {"link": ["chiap08"]},
            }
        ),
        encoding="utf-8",
    )
    try:
        load_control_plane(path)
    except ValueError as exc:
        assert "mero" in str(exc) or "seraph" in str(exc)
    else:
        raise AssertionError("incomplete control plane accepted")


def test_unit_templates_preserve_limits_and_disabled_install_contract() -> None:
    root = Path(__file__).parents[1]
    link = (root / "systemd/skfleet-link.service").read_text()
    mero = (root / "systemd/skfleet-mero.service").read_text()
    link_timer = (root / "systemd/skfleet-link.timer").read_text()
    mero_timer = (root / "systemd/skfleet-mero.timer").read_text()
    seraph = (root / "systemd/skfleet-seraph.service").read_text()
    seraph_timer = (root / "systemd/skfleet-seraph.timer").read_text()
    assert "TimeoutStartSec=120" in link
    assert "TimeoutStartSec=180" in mero
    assert "--seat link" in link and "--seat mero" in mero
    assert "OnUnitActiveSec=5min" in link_timer
    assert "OnUnitActiveSec=10min" in mero_timer
    assert "skfleet-mero.service" in mero_timer
    assert "TimeoutStartSec=300" in seraph
    assert "--seat seraph" in seraph
    assert "SKFLEET_MAX_LAUNCH" not in seraph
    assert "skfleet-seraph.service" in seraph_timer
