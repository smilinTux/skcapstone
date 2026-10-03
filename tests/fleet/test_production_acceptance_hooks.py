"""The production controller excludes exact source reviews from legacy release."""

import ast
import glob
import os
import re
from pathlib import Path
from types import SimpleNamespace

from tests.test_skfleet_review_claim_release import _load


def test_production_review_never_reaches_legacy_success_release(tmp_path):
    card = "ab000001"
    cards = tmp_path / "cards"
    (cards / card).mkdir(parents=True)
    namespace = {
        "HOST": "chiap08",
        "AUTHORITY_HOST": "chiap08",
        "DRY": False,
        "PRODUCTION_POLICY": {"authority_host": "chiap08"},
        "CARDS": str(cards),
        "glob": glob,
        "os": os,
        "re": re,
        "Path": Path,
        "CardStore": lambda home: SimpleNamespace(
            fold=lambda card: SimpleNamespace(
                labels=["source-only", "review"],
                owner="pi-seraph-chiap08-" + card,
                meta={"_claim_revision": "a" * 32},
            )
        ),
        "_current_claim_identity_fresh": lambda card: ("pi-seraph-chiap08-" + card, 1, "a" * 32),
        "_current_claim": lambda card: ("pi-seraph-chiap08-" + card, 1),
        "_durable_review_outcome": lambda card: "PASS",
        "_card_process_snapshot": lambda card: {"sessions": [], "units": []},
    }
    _load("release_finished_review_claims", namespace)
    # No subprocess exists: reaching a mutation would fail the test.
    assert namespace["release_finished_review_claims"]() == 0


def test_production_source_never_reaches_legacy_unguarded_parent_completion(tmp_path):
    card = "ab000001"
    cards = tmp_path / "cards"
    (cards / card).mkdir(parents=True)
    namespace = {
        "HOST": "chiap08",
        "AUTHORITY_HOST": "chiap08",
        "PRODUCTION_POLICY": {"authority_host": "chiap08"},
        "CARDS": str(cards),
        "os": os,
        "Path": Path,
        "CardStore": lambda home: SimpleNamespace(
            fold=lambda card: SimpleNamespace(labels=["source-only"])
        ),
        "_load_outcomes": lambda: {
            card: ("stamp", "PASS_FOR_REVIEW"),
            "ab000002": ("stamp", "PASS"),
        },
        "_reviews_by_parent": lambda: {card: ["ab000002"]},
        "_PROVISIONAL_PASS_RE": re.compile(r"^(PASS_FOR_REVIEW)"),
        "lifecycle_state": lambda cid: "open" if cid == card else "complete",
        "_PASS_ONLY_RE": re.compile(r"^PASS$"),
        "_parent_review_generation": lambda *args: ("generation",),
        "_review_names_generation": lambda *args: True,
        "_matching_outcome_events": lambda *args: [{}],
        "_review_join_value": lambda *args: "join",
    }
    _load("close_reviewed_parents", namespace)
    assert namespace["close_reviewed_parents"]() == 0


def test_production_resource_gate_counts_native_builder_and_test_units():
    path = Path(__file__).parents[2] / "scripts/fleet/skfleet-rotate.py"
    tree = ast.parse(path.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "local_worker_admission"
    ]
    assert len(calls) == 1
    assert ast.unparse(calls[0].args[2]) == "active_resource_units()"
