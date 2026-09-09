"""Tests for the fenced Link and Mero recurring entrypoint."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

import skcapstone.seat_cycle_entrypoint as seat_entrypoint
from skcapstone.card import Column
from skcapstone.card_store import CardCore, CardStore
from skcapstone.link_review_work import card_generation, reconcile_review_work
from skcapstone.seat_cycle_entrypoint import (
    link_operation,
    load_control_plane,
    run_cycle,
    seraph_operation,
)


@pytest.fixture(autouse=True)
def installed_dispatcher(tmp_path, monkeypatch):
    """Provide the wheel-owned launcher expected by Seraph unit tests."""

    bindir = tmp_path / "skenv-bin"
    bindir.mkdir()
    interpreter = bindir / "python3"
    interpreter.touch(mode=0o755)
    dispatcher = bindir / "skfleet-rotate.py"
    dispatcher.touch(mode=0o755)
    monkeypatch.setattr(seat_entrypoint.sys, "executable", str(interpreter))
    return dispatcher


def review_events(
    owner: str,
    revision: str,
    *,
    author: str = "builder",
    launched: bool = True,
    release_revision: str | None = None,
) -> list[dict[str, object]]:
    events = [
        {
            "action": "review_assignment_recommendation",
            "writer": "link",
            "recommendation_id": "recommendation-1",
            "author": author,
            "reviewer": owner,
        },
        {"action": "claim", "owner": owner, "claim_revision": revision},
        {
            "action": "review_assignment_launch",
            "recommendation_id": "recommendation-1",
            "reviewer": owner,
            "claim_revision": revision,
            "launched": launched,
        },
    ]
    if not launched:
        events.append(
            {
                "action": "release_claim",
                "released_owner": owner,
                "expected_claim_revision": release_revision or revision,
            }
        )
    return events


def control(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "revision": "r1",
                "active_host": "chiap08",
                "seats": {
                    seat: ["chiap08"]
                    for seat in ("link", "mero", "seraph", "niobe", "tank", "atlas")
                },
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


def test_seraph_accepts_typed_launch_receipts_on_stderr(tmp_path, monkeypatch) -> None:
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=0,
                stdout="dispatcher diagnostics\n",
                stderr=(
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
        meta={
            "_claim_revision": "revision-1",
            "link_source_card": "source01",
            "link_head_revision": "a" * 40,
        },
        links={"producer_identity": "builder"},
    )
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda *_: review_events("pi-seraph-chiap08-review01", "revision-1"),
    )
    assert seraph_operation(tmp_path)["reason"] == "seraph_dispatch_complete"


def test_seraph_dispatch_is_bounded_claimed_live_and_seat_scoped(
    tmp_path, monkeypatch, installed_dispatcher
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
        meta={
            "_claim_revision": "revision-1",
            "link_source_card": "source01",
            "link_head_revision": "a" * 40,
        },
        links={"producer_identity": "builder"},
    )

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda *_: review_events("pi-seraph-chiap08-review01", "revision-1"),
    )
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_dispatch_complete"
    assert calls[0][0] == str(installed_dispatcher)
    assert captured["SKFLEET_ONLY_SEAT"] == "seraph"
    assert captured["SKFLEET_MAX_LAUNCH"] == "2"
    assert captured["SKFLEET_SEAT_TARGET"] == "2"
    assert "SKFLEET_TARGET" not in captured
    assert captured["SKFLEET_CODEX_MODEL_S"] == "sk-codex-mid"
    assert calls[1][-1] == "skfleet-worker-codex-review01.service"


def test_seraph_rejects_missing_wheel_owned_dispatcher(tmp_path, installed_dispatcher) -> None:
    installed_dispatcher.unlink()

    assert seraph_operation(tmp_path)["reason"] == "seraph_dispatcher_missing"


def test_seraph_preserves_invoked_venv_when_python_is_a_symlink(tmp_path, monkeypatch) -> None:
    bindir = tmp_path / "venv" / "bin"
    bindir.mkdir(parents=True)
    interpreter = bindir / "python3"
    interpreter.symlink_to("/usr/bin/python3")
    dispatcher = bindir / "skfleet-rotate.py"
    dispatcher.touch(mode=0o755)
    captured = {}

    def run(command, **_kwargs):
        captured["command"] = command
        return SimpleNamespace(
            returncode=0,
            stdout="NOOP_RECEIPT|chiap08|reason=no_eligible_work|seat=seraph\n",
            stderr="",
        )

    monkeypatch.setattr(seat_entrypoint.sys, "executable", str(interpreter))
    monkeypatch.setattr(seat_entrypoint.subprocess, "run", run)

    assert seraph_operation(tmp_path)["reason"] == "seraph_no_eligible_work"
    assert captured["command"][0] == str(dispatcher)


def test_seraph_zero_eligible_work_is_truthful_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="NOOP_RECEIPT|chiap08|reason=no_eligible_work|seat=seraph\n",
        ),
    )
    result = seraph_operation(tmp_path)
    assert result == {
        "cards_examined": 0,
        "recommendations": 0,
        "suppressed": 0,
        "reason": "seraph_no_eligible_work",
    }


def test_seraph_zero_available_capacity_is_truthful_noop(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="NOOP_RECEIPT|chiap08|reason=no_available_capacity|seat=seraph\n",
        ),
    )
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_no_available_capacity"
    assert result["suppressed"] == 0


def test_seraph_all_suppressed_noop_is_accepted_from_stderr(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="REVIEW_ASSIGNMENT_BLOCKED|chiap08|review01|incomplete producer evidence\n",
            stderr="NOOP_RECEIPT|chiap08|reason=all_candidates_suppressed|seat=seraph\n",
        ),
    )
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_all_candidates_suppressed"
    assert result["suppressed"] == 0


def test_seraph_rejects_duplicate_noop_receipts(tmp_path, monkeypatch) -> None:
    receipt = "NOOP_RECEIPT|chiap08|reason=all_candidates_suppressed|seat=seraph\n"
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0, stdout=receipt + receipt, stderr=""
        ),
    )
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_launch_receipt_missing"
    assert result["suppressed"] == 1


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
    assert result["reason"] == "seraph_dispatch_failed"
    assert result["recommendations"] == 0


def test_seraph_rejects_wrong_model_receipt(tmp_path, monkeypatch) -> None:
    receipt = (
        "LAUNCHED|chiap08|codex-auto-review01|review01|lane=codex|"
        "model=sk-codex-fast|owner=pi-seraph-chiap08-review01|"
        "claim_revision=revision-1\n"
    )
    card = SimpleNamespace(
        labels=["review", "seat-seraph"],
        status=SimpleNamespace(value="doing"),
        owner="pi-seraph-chiap08-review01",
        meta={
            "_claim_revision": "revision-1",
            "link_source_card": "source01",
            "link_head_revision": "a" * 40,
        },
        links={"producer_identity": "builder"},
    )
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=receipt),
    )
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda *_: review_events("pi-seraph-chiap08-review01", "revision-1"),
    )
    result = seraph_operation(tmp_path)
    assert result["reason"] == "seraph_dispatch_failed"


def test_seraph_reports_partial_batch_and_leaves_failed_item_retryable(
    tmp_path, monkeypatch
) -> None:
    owners = {
        "review01": "pi-seraph-chiap08-review01",
        "review02": "pi-seraph-chiap08-review02",
    }
    cards = {
        card_id: SimpleNamespace(
            labels=["review", "seat-seraph"],
            status=(SimpleNamespace(value="doing") if card_id == "review01" else Column.BACKLOG),
            owner=owner if card_id == "review01" else None,
            archived=False,
            dependencies=[],
            meta={
                "_claim_revision": "revision-1" if card_id == "review01" else None,
                "link_source_card": "source" + card_id[-2:],
                "link_head_revision": card_id[-1] * 40,
            },
            links={"producer_identity": "builder" + card_id[-2:]},
        )
        for card_id, owner in owners.items()
    }
    output = "".join(
        [
            (
                f"LAUNCHED|chiap08|codex-auto-{card_id}|{card_id}|lane=codex|"
                f"model=sk-codex-mid|owner={owner}|claim_revision=revision-1\n"
                if card_id == "review01"
                else f"LAUNCH_FAILED|chiap08|codex-auto-{card_id}|{card_id}|lane=codex|"
                f"model=sk-codex-mid|owner={owner}|claim_revision=revision-2\n"
            )
            for card_id, owner in owners.items()
        ]
    )

    def run(command, **_kwargs):
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore.fold",
        lambda _store, card_id: cards[card_id],
    )
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda _store, card_id: review_events(
            owners[card_id],
            "revision-1" if card_id == "review01" else "revision-2",
            author="builder" + card_id[-2:],
            launched=card_id == "review01",
        ),
    )

    result = seraph_operation(tmp_path)

    assert result == {
        "cards_examined": 2,
        "recommendations": 1,
        "suppressed": 1,
        "dispatch_succeeded": 1,
        "dispatch_failed": 1,
        "dispatch_retryable": 1,
        "reason": "seraph_dispatch_partial",
    }
    assert cards["review02"].owner is None


def _failed_seraph_result(
    tmp_path,
    monkeypatch,
    *,
    status="backlog",
    owner=None,
    labels=None,
    release_revision=None,
    events=None,
):
    reviewer = "pi-seraph-chiap08-review01"
    output = (
        "LAUNCH_FAILED|chiap08|codex-auto-review01|review01|lane=codex|"
        f"model=sk-codex-mid|owner={reviewer}|claim_revision=revision-1\n"
    )
    card = SimpleNamespace(
        labels=labels or ["review", "seat-seraph"],
        status=Column(status),
        owner=owner,
        archived=False,
        dependencies=[],
        meta={"link_source_card": "source01", "link_head_revision": "a" * 40},
        links={"producer_identity": "builder"},
    )
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda *_: events
        or review_events(
            reviewer,
            "revision-1",
            launched=False,
            release_revision=release_revision,
        ),
    )
    return seraph_operation(tmp_path)


def test_seraph_failed_launch_rejects_claim_release_generation_mismatch(
    tmp_path, monkeypatch
) -> None:
    result = _failed_seraph_result(tmp_path, monkeypatch, release_revision="revision-old")
    assert result["reason"] == "seraph_dispatch_failed"
    assert result["dispatch_failed"] == 1
    assert result["dispatch_retryable"] == 0


def test_seraph_failed_launch_rejects_stale_release_order(tmp_path, monkeypatch) -> None:
    events = review_events("pi-seraph-chiap08-review01", "revision-1", launched=False)
    events[-1], events[-2] = events[-2], events[-1]
    result = _failed_seraph_result(tmp_path, monkeypatch, events=events)
    assert result["reason"] == "seraph_dispatch_failed"
    assert result["dispatch_retryable"] == 0


@pytest.mark.parametrize(
    ("status", "owner", "labels"),
    [
        ("backlog", "another-worker", ["review", "seat-seraph"]),
        ("done", None, ["review", "seat-seraph"]),
        ("backlog", None, ["review", "seat-seraph", "not-claimable"]),
    ],
)
def test_seraph_failed_launch_rejects_owned_or_nonclaimable_state(
    tmp_path, monkeypatch, status, owner, labels
) -> None:
    result = _failed_seraph_result(
        tmp_path, monkeypatch, status=status, owner=owner, labels=labels
    )
    assert result["reason"] == "seraph_dispatch_failed"
    assert result["dispatch_retryable"] == 0


def test_seraph_failed_launch_accepts_exact_released_claimable_generation(
    tmp_path, monkeypatch
) -> None:
    result = _failed_seraph_result(tmp_path, monkeypatch)
    assert result["reason"] == "seraph_dispatch_failed"
    assert result["dispatch_failed"] == 1
    assert result["dispatch_retryable"] == 1


def test_seraph_preserves_valid_launch_when_selector_exits_nonzero(tmp_path, monkeypatch) -> None:
    owner = "pi-seraph-chiap08-review01"
    card = SimpleNamespace(
        labels=["review", "seat-seraph"],
        status=SimpleNamespace(value="doing"),
        owner=owner,
        meta={
            "_claim_revision": "revision-1",
            "link_source_card": "source01",
            "link_head_revision": "a" * 40,
        },
        links={"producer_identity": "builder"},
    )
    output = (
        "LAUNCHED|chiap08|codex-auto-review01|review01|lane=codex|"
        f"model=sk-codex-mid|owner={owner}|claim_revision=revision-1\n"
    )

    def run(command, **_kwargs):
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=1, stdout=output)

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda *_: review_events(owner, "revision-1"),
    )

    result = seraph_operation(tmp_path)

    assert result["reason"] == "seraph_dispatch_partial"
    assert result["dispatch_succeeded"] == 1
    assert result["dispatch_failed"] == 1


def test_seraph_rejects_duplicate_source_head_and_producer_reviewer(tmp_path, monkeypatch) -> None:
    owners = {
        "review01": "pi-seraph-chiap08-review01",
        "review02": "pi-seraph-chiap08-review02",
    }
    cards = {
        card_id: SimpleNamespace(
            labels=["review", "seat-seraph"],
            status=SimpleNamespace(value="doing"),
            owner=owner,
            meta={
                "_claim_revision": "revision-1",
                "link_source_card": "same-source",
                "link_head_revision": "a" * 40,
            },
            links={"producer_identity": owner if card_id == "review01" else "builder"},
        )
        for card_id, owner in owners.items()
    }
    output = "".join(
        f"LAUNCHED|chiap08|codex-auto-{card_id}|{card_id}|lane=codex|"
        f"model=sk-codex-mid|owner={owner}|claim_revision=revision-1\n"
        for card_id, owner in owners.items()
    )

    def run(command, **_kwargs):
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore.fold",
        lambda _store, card_id: cards[card_id],
    )
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda _store, card_id: review_events(
            owners[card_id], "revision-1", author=cards[card_id].links["producer_identity"]
        ),
    )

    result = seraph_operation(tmp_path)

    assert result["reason"] == "seraph_dispatch_failed"
    assert result["dispatch_succeeded"] == 0
    assert result["dispatch_failed"] == 2


def test_seraph_batch_size_is_bounded(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_SERAPH_BATCH_SIZE", "9")
    assert seraph_operation(tmp_path)["reason"] == "seraph_batch_size_invalid"


def test_seraph_accepts_only_explicit_escalation_lane_model(tmp_path, monkeypatch) -> None:
    owner = "pi-seraph-chiap08-review01"
    card = SimpleNamespace(
        labels=["review", "seat-seraph"],
        status=SimpleNamespace(value="doing"),
        owner=owner,
        meta={
            "_claim_revision": "revision-1",
            "link_source_card": "source01",
            "link_head_revision": "a" * 40,
        },
        links={"producer_identity": "builder"},
    )
    output = (
        "LAUNCHED|chiap08|esc-auto-review01|review01|lane=escalate|"
        f"model=sk-codex|owner={owner}|claim_revision=revision-1\n"
    )

    def run(command, **_kwargs):
        if command[0] == "systemctl":
            return SimpleNamespace(returncode=0)
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setenv("SKFLEET_ESC_MODEL", "sk-codex")
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.subprocess.run", run)
    monkeypatch.setattr("skcapstone.seat_cycle_entrypoint.CardStore.fold", lambda *_: card)
    monkeypatch.setattr(
        "skcapstone.seat_cycle_entrypoint.CardStore._read_events",
        lambda *_: review_events(owner, "revision-1"),
    )

    assert seraph_operation(tmp_path)["reason"] == "seraph_dispatch_complete"


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
    store.append_event(
        "source01",
        "link",
        "builder",
        link_key="repository",
        link_value="https://github.com/org/repo",
    )
    store.append_event("source01", "link", "builder", link_key="base_ref", link_value="main")
    item = {
        "source_card": "source01",
        "head_revision": "a" * 40,
        "workspace_repository": "https://github.com/org/repo",
        "base_ref": "main",
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
            return SimpleNamespace(
                returncode=0,
                stdout="NOOP_RECEIPT|chiap08|reason=no_eligible_work|seat=seraph\n",
            )
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
        store.append_event(
            card_id,
            "review_assignment_recommendation",
            "link",
            recommendation_id="batch-test",
            author="builder",
            reviewer=owner,
        )
        store.append_event(
            card_id,
            "review_assignment_launch",
            owner,
            recommendation_id="batch-test",
            reviewer=owner,
            claim_revision=revision,
            launched=True,
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
    assert second["reason"] == "seraph_no_eligible_work"
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


def test_control_plane_requires_all_six_seats(tmp_path: Path) -> None:
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
        assert "not provisioned on active host" in str(exc)
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
    tank = (root / "systemd/skfleet-tank.service").read_text()
    tank_timer = (root / "systemd/skfleet-tank.timer").read_text()
    atlas = (root / "systemd/skfleet-atlas.service").read_text()
    atlas_timer = (root / "systemd/skfleet-atlas.timer").read_text()
    assert "TimeoutStartSec=120" in link
    assert "TimeoutStartSec=180" in mero
    assert "--seat link" in link and "--seat mero" in mero
    assert "OnUnitActiveSec=5min" in link_timer
    assert "OnUnitActiveSec=5min" in mero_timer
    assert "skfleet-mero.service" in mero_timer
    assert "TimeoutStartSec=300" in seraph
    assert "--seat seraph" in seraph
    assert "SKFLEET_MAX_LAUNCH" not in seraph
    assert "Environment=SKFLEET_TARGET=2" in seraph
    assert "Environment=SKFLEET_SERAPH_BATCH_SIZE=2" in seraph
    assert "Environment=SKFLEET_CODEX_PHYSICAL_LIMIT=3" in seraph
    assert "skfleet-seraph.service" in seraph_timer
    assert "--seat tank" in tank and "TimeoutStartSec=300" in tank
    assert "OnUnitActiveSec=5min" in tank_timer
    assert "--seat atlas" in atlas and "TimeoutStartSec=300" in atlas
    assert "OnUnitActiveSec=5min" in atlas_timer


def test_tank_and_atlas_presence_cycles_do_not_run_link_work(tmp_path: Path) -> None:
    control_path = tmp_path / "control.json"
    control(control_path)
    for seat in ("tank", "atlas"):
        result = run_cycle(
            seat=seat,
            home=tmp_path / "home",
            control_plane=control_path,
            local_host="chiap08",
            operation=lambda: {
                "cards_examined": 0,
                "recommendations": 0,
                "suppressed": 0,
                "reason": "presence_complete",
            },
        )
        assert result.result == "presence_complete"
        assert result.cards_examined == 0
