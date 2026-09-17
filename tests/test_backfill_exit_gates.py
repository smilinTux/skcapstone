"""Backfill splits criteria; it never rewrites or drops one."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from backfill_exit_gates import (  # noqa: E402
    discover_candidates,
    main,
    split_criteria,
    write_backup,
)


def _write_event(events_dir: Path, node: str, seq: int, **fields) -> None:
    events_dir.mkdir(parents=True, exist_ok=True)
    log = events_dir / f"{node}.jsonl"
    row = {"seq": seq, "node": node, **fields}
    with log.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def _make_card(
    cards_dir: Path,
    card_id: str,
    acceptance_criteria: list[str],
    claims: int = 0,
    complete: bool = False,
    void: bool = False,
) -> Path:
    card_dir = cards_dir / card_id
    card_dir.mkdir(parents=True)
    core = {
        "id": card_id,
        "kind": "task",
        "title": f"card {card_id}",
        "acceptance_criteria": acceptance_criteria,
    }
    (card_dir / "core.json").write_text(json.dumps(core, indent=2) + "\n", encoding="utf-8")
    events_dir = card_dir / "events"
    seq = 0
    for _ in range(claims):
        _write_event(events_dir, "w", seq, action="claim", owner="w")
        seq += 1
    if complete:
        _write_event(events_dir, "w", seq, action="complete")
        seq += 1
    if void:
        _write_event(events_dir, "w", seq, action="void")
        seq += 1
    return card_dir


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


# --- Tightened gate pattern: bare tokens are no longer gates on their own ---


def test_bare_merged_as_adjective_is_not_a_gate():
    kept, gates = split_criteria(
        ["Remove only a completed merged worktree and merged task branch " "after remote readback"]
    )
    assert len(kept) == 1
    assert gates == []


def test_bare_approval_describing_forbidden_behavior_is_not_a_gate():
    kept, gates = split_criteria(
        ["No Approval, dispatch, or external action is created or implied"]
    )
    assert len(kept) == 1
    assert gates == []


def test_bare_approval_in_a_worker_owned_test_name_is_not_a_gate():
    kept, gates = split_criteria(["Stale Approval, wrong Matter, fail closed"])
    assert len(kept) == 1
    assert gates == []


def test_documenting_the_approval_workflow_is_not_a_gate():
    kept, gates = split_criteria(["document the approval workflow"])
    assert len(kept) == 1
    assert gates == []


def test_phrase_forms_still_gate():
    criteria = [
        "Independent review PASS on the successor commit before merge",
        "Approved by the operator",
        "Sign-off from the lead",
        "Reviewed by two other seats",
        "Merged to main",
        "Awaiting review",
    ]
    kept, gates = split_criteria(criteria)
    assert kept == []
    assert len(gates) == len(criteria)


# --- Partition survives breadth (test item 4) -------------------------------


def test_partition_handles_duplicate_criteria():
    kept, gates = split_criteria(["Tests pass", "Tests pass"])
    assert kept == ["Tests pass", "Tests pass"]
    assert gates == []


def test_partition_handles_empty_list():
    kept, gates = split_criteria([])
    assert kept == []
    assert gates == []


def test_partition_handles_a_list_where_nothing_matches():
    criteria = ["Tests pass", "Docs updated"]
    kept, gates = split_criteria(criteria)
    assert kept == criteria
    assert gates == []


# --- The --apply guard (test item 1) ----------------------------------------


def test_dry_run_mutates_nothing(tmp_path):
    home = tmp_path / "home"
    cards_dir = home / "cards"
    card_dir = _make_card(
        cards_dir,
        "aaaa1111",
        ["Tests pass", "Independent review PASS before merge"],
        claims=3,
    )
    before = (card_dir / "core.json").read_bytes()

    exit_code = main(["--home", str(home)])

    after = (card_dir / "core.json").read_bytes()
    assert exit_code == 0
    assert after == before


# --- The scope filter's bounds (test item 2) --------------------------------


def test_scope_filter_excludes_two_claims(tmp_path):
    home = tmp_path / "home"
    cards_dir = home / "cards"
    _make_card(
        cards_dir,
        "bbbb2222",
        ["Tests pass", "Independent review PASS before merge"],
        claims=2,
    )
    candidates, skipped = discover_candidates(home, min_claims=3)
    assert candidates == []
    assert skipped == []


def test_scope_filter_includes_three_claims(tmp_path):
    home = tmp_path / "home"
    cards_dir = home / "cards"
    _make_card(
        cards_dir,
        "cccc3333",
        ["Tests pass", "Independent review PASS before merge"],
        claims=3,
    )
    candidates, _skipped = discover_candidates(home, min_claims=3)
    ids = [core["id"] for _card_dir, core, _kept, _gates in candidates]
    assert ids == ["cccc3333"]


def test_scope_filter_excludes_completed_card_even_with_five_claims(tmp_path):
    home = tmp_path / "home"
    cards_dir = home / "cards"
    _make_card(
        cards_dir,
        "dddd4444",
        ["Tests pass", "Independent review PASS before merge"],
        claims=5,
        complete=True,
    )
    candidates, skipped = discover_candidates(home, min_claims=3)
    assert candidates == []
    assert skipped == []


# --- The zero-kept guard (test item 3) --------------------------------------


def test_zero_kept_guard_skips_and_reports(tmp_path, capsys):
    home = tmp_path / "home"
    cards_dir = home / "cards"
    card_dir = _make_card(
        cards_dir,
        "eeee5555",
        ["Independent review PASS before merge", "Approved by the operator"],
        claims=3,
    )
    before = (card_dir / "core.json").read_bytes()

    candidates, skipped = discover_candidates(home, min_claims=3)
    assert candidates == []
    assert skipped == ["eeee5555"]

    exit_code = main(["--home", str(home)])
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "eeee5555" in out
    assert (card_dir / "core.json").read_bytes() == before


# --- The backup (test item 5) -----------------------------------------------


def test_backup_written_before_any_card_is_modified(tmp_path):
    home = tmp_path / "home"
    cards_dir = home / "cards"
    card_dir = _make_card(
        cards_dir,
        "ffff6666",
        ["Tests pass", "Independent review PASS before merge"],
        claims=3,
    )
    candidates, _skipped = discover_candidates(home, min_claims=3)
    backup_path = tmp_path / "backup.json"

    before = (card_dir / "core.json").read_bytes()
    write_backup(backup_path, candidates)
    after = (card_dir / "core.json").read_bytes()

    assert after == before
    payload = json.loads(backup_path.read_text(encoding="utf-8"))
    assert payload == {"ffff6666": ["Tests pass", "Independent review PASS before merge"]}
