"""Finished review claims release exact generations without losing evidence."""

import ast
import glob
import hashlib
import os
import re
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).parents[1]
ROTATE = ROOT / "scripts/fleet/skfleet-rotate.py"


def _load(name: str, namespace: dict) -> dict:
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    )
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace


def test_releases_four_finished_claims_with_exact_cas_and_keeps_evidence(tmp_path: Path) -> None:
    cards = [f"0000000{index}" for index in range(1, 5)]
    card_root = tmp_path / "cards"
    for card in cards:
        (card_root / card).mkdir(parents=True)
    evidence = tmp_path / "review.md"
    evidence.write_text("review findings\n", encoding="utf-8")
    claims = {card: (f"pi-seraph-chiap08-{card}", 1.0, f"revision-{card}") for card in cards}
    calls = []
    namespace = {
        "HOST": "chiap08",
        "DRY": False,
        "CARDS": str(card_root),
        "SKC": "skcapstone",
        "glob": glob,
        "os": os,
        "re": re,
        "subprocess": SimpleNamespace(
            run=lambda argv, **_kwargs: calls.append(argv) or SimpleNamespace(returncode=0)
        ),
        "_current_claim_identity_fresh": lambda card: claims[card],
        "_durable_review_outcome": lambda _card: "PASS",
        "_card_process_snapshot": lambda _card: {"sessions": [], "units": []},
        "_rows": {card: object() for card in cards},
        "_outcomes": {card: ("stamp", "PASS") for card in cards},
        "log": lambda *_args: None,
        "d": str(tmp_path),
    }
    _load("release_finished_review_claims", namespace)

    assert namespace["release_finished_review_claims"]() == 4
    assert evidence.read_text(encoding="utf-8") == "review findings\n"
    assert len(calls) == 4
    for card, call in zip(cards, calls):
        owner, _stamp, revision = claims[card]
        assert call == [
            "skcapstone",
            "coord",
            "release-claim",
            card,
            "--owner",
            owner,
            "--expected-claim-revision",
            revision,
            "--agent",
            "fleet-review-closer",
        ]


def test_terminal_outcome_requires_matching_readable_evidence_hash(tmp_path: Path) -> None:
    evidence = tmp_path / "review.md"
    evidence.write_text("findings\n", encoding="utf-8")
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()
    rows = [
        {"action": "link", "link_key": "verdict", "link_value": "BLOCKED", "writer": "r"},
        {"action": "link", "link_key": "evidence", "link_value": str(evidence), "writer": "r"},
        {"action": "link", "link_key": "evidence_sha256", "link_value": digest, "writer": "r"},
    ]
    namespace = {
        "event_rows": lambda _card: rows,
        "_load_evidence_events": lambda: {},
        "_native_outcome_value": lambda event: event.get("verdict"),
        "_fold_key": lambda value: str(value or "").lower(),
        "_OUTCOME_KEYS": ("verdict", "outcome", "result", "review_decision"),
        "re": re,
        "os": os,
        "hashlib": hashlib,
    }
    _load("_durable_review_outcome", namespace)

    assert namespace["_durable_review_outcome"]("deadbeef") == "BLOCKED"
    rows[-1]["link_value"] = "0" * 64
    assert namespace["_durable_review_outcome"]("deadbeef") is None


def test_live_process_and_changed_generation_are_never_released(tmp_path: Path) -> None:
    card_root = tmp_path / "cards"
    for card in ("aaaaaaaa", "bbbbbbbb"):
        (card_root / card).mkdir(parents=True)
    reads = {"bbbbbbbb": 0}

    def claim(card: str):
        if card == "aaaaaaaa":
            return f"pi-seraph-chiap08-{card}", 1.0, "same"
        reads[card] += 1
        revision = "old" if reads[card] == 1 else "new"
        return f"pi-seraph-chiap08-{card}", 1.0, revision

    calls = []
    namespace = {
        "HOST": "chiap08",
        "DRY": False,
        "CARDS": str(card_root),
        "SKC": "skcapstone",
        "glob": glob,
        "os": os,
        "re": re,
        "subprocess": SimpleNamespace(run=lambda argv, **_kwargs: calls.append(argv)),
        "_current_claim_identity_fresh": claim,
        "_durable_review_outcome": lambda _card: "FAIL",
        "_card_process_snapshot": lambda card: {
            "sessions": ["live-aaaaaaaa"] if card == "aaaaaaaa" else [],
            "units": [],
        },
        "_rows": {},
        "_outcomes": {},
        "log": lambda *_args: None,
        "d": str(tmp_path),
    }
    _load("release_finished_review_claims", namespace)

    assert namespace["release_finished_review_claims"]() == 0
    assert calls == []


def test_release_runs_before_same_cycle_pool_build() -> None:
    source = ROTATE.read_text(encoding="utf-8")
    call = source.index("    release_finished_review_claims()")
    pool = source.index("pool=[]", call)
    assert call < pool
