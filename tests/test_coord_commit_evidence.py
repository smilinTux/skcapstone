"""Tests for the commit_sha evidence gate on complete_coord_task (task 6).

Task 3 of this plan replaced the mandatory-PR policy with a pushed branch plus
a `coord link <card> commit_sha <sha>` evidence record. Measured on the live
board, 544 completed cards under the OLD (PR-mandatory) policy carried a
code-evidence link only 26.8% of the time. Unenforced, the replacement record
would be produced even less often than that, since it is now optional prose
unless something refuses completion without it. This gate is that refusal.

"Touched a repository" reuses the one signal this codebase already uses for
exactly that question: a `repo:<name>` label. `execute_mux._card_routing`
(src/skcapstone/execute_mux.py) already reads this same label to route a card
to the sandboxed code bridge instead of the comms dispatcher, so a card the
rest of the system already treats as code work is exactly the card this gate
treats as code work too. A card with no `repo:` label is not code work by
that same standard and must complete exactly as it did before this gate
existed, no `commit_sha` link required at all.

Task 3's worker prompt tells a worker to link `commit_sha` to the literal
value `none` when a card needed no repository change, so that value must
satisfy this gate, and it does: this gate only checks that a `commit_sha`
link is present and non-blank, never what it says.
"""

from __future__ import annotations

import json
from pathlib import Path

from skcapstone.card import CardEvent, CardEventLog
from skcapstone.coord_completion import GatesPending, complete_coord_task
from skcapstone.coordination import Board, Task


def _make_card(
    home: Path,
    task_id: str,
    title: str,
    agent: str = "reviewer",
    tags: list[str] | None = None,
) -> None:
    board = Board(home)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title=title, tags=list(tags or [])))
    board.claim_task(agent, task_id)


def _link(home: Path, task_id: str, key: str, value: str, agent: str = "reviewer") -> None:
    CardEventLog(home).append(
        CardEvent(card_id=task_id, action="link", link_key=key, link_value=value, writer=agent)
    )


def _events(home: Path, task_id: str) -> list[dict]:
    events_dir = home / "cards" / task_id / "events"
    out: list[dict] = []
    if not events_dir.exists():
        return out
    for log in events_dir.glob("*.jsonl"):
        for line in log.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(json.loads(line))
    return out


def test_repo_card_with_valid_commit_sha_completes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "aaaa1111", "fix the widget", tags=["repo:skcapstone"])
    _link(home, "aaaa1111", "commit_sha", "a" * 40)

    result = complete_coord_task(home, "reviewer", "aaaa1111")

    assert not isinstance(result, GatesPending)
    assert result.agent == "reviewer"
    assert "aaaa1111" in result.completed_tasks


def test_repo_card_with_commit_sha_none_completes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "bbbb2222", "investigate the widget", tags=["repo:skcapstone"])
    _link(home, "bbbb2222", "commit_sha", "none")

    result = complete_coord_task(home, "reviewer", "bbbb2222")

    assert not isinstance(result, GatesPending)
    assert "bbbb2222" in result.completed_tasks


def test_repo_card_with_no_commit_sha_is_refused_naming_the_fix(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "cccc3333", "fix the other widget", tags=["repo:skcapstone"])

    result = complete_coord_task(home, "reviewer", "cccc3333")

    assert isinstance(result, GatesPending)
    assert len(result.outstanding) == 1
    entry = result.outstanding[0]
    assert entry["gate"] == "commit_sha"
    message = entry.get("message", "")
    assert "coord link cccc3333 commit_sha" in message
    actions = [e.get("action") for e in _events(home, "cccc3333")]
    assert "complete" not in actions


def test_repo_card_prose_commit_link_does_not_satisfy_gate(tmp_path: Path) -> None:
    """The `commit` field is free-form human prose (100 uses, only 4 of them
    a real SHA) and must never be read as evidence in place of commit_sha."""
    home = tmp_path / "home"
    _make_card(home, "dddd4444", "fix yet another widget", tags=["repo:skcapstone"])
    _link(home, "dddd4444", "commit", "fixed it, see the branch")

    result = complete_coord_task(home, "reviewer", "dddd4444")

    assert isinstance(result, GatesPending)
    assert result.outstanding[0]["gate"] == "commit_sha"


def test_non_repo_card_completes_unchanged_with_no_commit_sha(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "eeee5555", "write the design doc")

    result = complete_coord_task(home, "reviewer", "eeee5555")

    assert not isinstance(result, GatesPending)
    assert "eeee5555" in result.completed_tasks


def test_missing_commit_sha_refusal_does_not_write_an_await_gates_event(
    tmp_path: Path,
) -> None:
    """An await_gates event with no core.json exit_gates classifies a card as
    permanently "awaiting-gates" in the fleet dispatcher's own fold
    (scripts/fleet/skfleet-rotate.py, ~line 2260): declared_gates comes only
    from core.json, so an empty declared_gates set can never satisfy the
    `declared_gates and declared_gates <= satisfied_gates` auto-clear check,
    and the card would never be reclaimed again even after commit_sha is
    linked. This refusal must not use that event.
    """
    home = tmp_path / "home"
    _make_card(home, "ffff6666", "fix the last widget", tags=["repo:skcapstone"])

    result = complete_coord_task(home, "reviewer", "ffff6666")

    assert isinstance(result, GatesPending)
    actions = [e.get("action") for e in _events(home, "ffff6666")]
    assert "await_gates" not in actions
