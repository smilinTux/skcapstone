"""Backfill splits criteria; it never rewrites or drops one."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from backfill_exit_gates import split_criteria  # noqa: E402


def test_reviewer_criterion_moves_to_exit_gates():
    kept, gates = split_criteria(
        [
            "Root cause found with exact transcript",
            "Independent review PASS on the successor commit before merge",
        ]
    )
    assert kept == ["Root cause found with exact transcript"]
    assert len(gates) == 1
    assert gates[0]["owner"] == "seraph"
    assert "Independent review PASS" in gates[0]["criterion"]


def test_self_satisfiable_criteria_are_all_kept():
    criteria = ["Tests pass", "Ruff and formatting pass"]
    kept, gates = split_criteria(criteria)
    assert kept == criteria
    assert gates == []


def test_nothing_is_lost_in_the_split():
    """Every input criterion appears in exactly one output."""
    criteria = ["Tests pass", "Approved by the operator", "Docs updated"]
    kept, gates = split_criteria(criteria)
    recovered = kept + [g["criterion"] for g in gates]
    assert sorted(recovered) == sorted(criteria)


def test_a_card_with_only_gate_criteria_keeps_an_empty_list():
    kept, gates = split_criteria(["Independent review PASS before merge"])
    assert kept == []
    assert len(gates) == 1
