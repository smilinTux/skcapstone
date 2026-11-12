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
satisfy this gate, and it does.

The gate also validates SHAPE, not just presence: a `commit_sha` link must be
either a genuine 40 character hex git commit SHA or the exact literal `none`.
Any other non-blank string (a placeholder such as `wip`, a truncated SHA, an
arbitrary word) is refused, naming what was found and what is required. A
genuine SHA additionally requires an accompanying `branch` link, repo-
qualified as `<repo>:<name>`: the branch is the only part of the evidence
record that tells another host what to fetch, so a real SHA with no branch
is a promise with nothing to verify it against. The literal `none` sentinel
needs no branch, because there is no code to fetch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.card import CardEvent, CardEventLog
from skcapstone.coord_completion import (
    GatesPending,
    commit_sha_is_valid,
    complete_coord_task,
    move_coord_task,
)
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
    _link(home, "aaaa1111", "branch", "skcapstone:fix/aaaa1111")

    result = complete_coord_task(home, "reviewer", "aaaa1111")

    assert not isinstance(result, GatesPending)
    assert result.agent == "reviewer"
    assert "aaaa1111" in result.completed_tasks


def test_repo_card_with_placeholder_commit_sha_is_refused(tmp_path: Path) -> None:
    """A non-blank string that is neither a real SHA nor the none sentinel
    must not satisfy the gate. Before this behaviour existed, any non-blank
    string, including "wip" or "x", satisfied the gate, which made it a
    speed bump rather than a record."""
    home = tmp_path / "home"
    _make_card(home, "aaaa2222", "fix the widget", tags=["repo:skcapstone"])
    _link(home, "aaaa2222", "commit_sha", "wip")

    result = complete_coord_task(home, "reviewer", "aaaa2222")

    assert isinstance(result, GatesPending)
    entry = result.outstanding[0]
    assert entry["gate"] == "commit_sha"
    message = entry.get("message", "")
    assert "wip" in message
    assert "none" in message


def test_repo_card_with_truncated_commit_sha_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _make_card(home, "aaaa3333", "fix the widget", tags=["repo:skcapstone"])
    _link(home, "aaaa3333", "commit_sha", "a" * 39)

    result = complete_coord_task(home, "reviewer", "aaaa3333")

    assert isinstance(result, GatesPending)
    assert result.outstanding[0]["gate"] == "commit_sha"


def test_repo_card_with_valid_sha_but_no_branch_is_refused(tmp_path: Path) -> None:
    """A genuine SHA with no branch link tells nobody what to fetch, so it is
    refused exactly like a missing commit_sha link, naming the branch command."""
    home = tmp_path / "home"
    _make_card(home, "aaaa4444", "fix the widget", tags=["repo:skcapstone"])
    _link(home, "aaaa4444", "commit_sha", "b" * 40)

    result = complete_coord_task(home, "reviewer", "aaaa4444")

    assert isinstance(result, GatesPending)
    entry = result.outstanding[0]
    assert entry["gate"] == "commit_sha"
    message = entry.get("message", "")
    assert "coord link aaaa4444 branch" in message


def test_repo_card_commit_sha_none_needs_no_branch(tmp_path: Path) -> None:
    """The none sentinel needs no branch link: there is no code to fetch."""
    home = tmp_path / "home"
    _make_card(home, "aaaa5555", "investigate the widget", tags=["repo:skcapstone"])
    _link(home, "aaaa5555", "commit_sha", "none")

    result = complete_coord_task(home, "reviewer", "aaaa5555")

    assert not isinstance(result, GatesPending)
    assert "aaaa5555" in result.completed_tasks


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


def test_move_to_done_is_gated_the_same_as_complete(tmp_path: Path) -> None:
    """Gating completion alone leaves move-to-done as an unlocked side door.

    This exact bypass already had to be closed once for exit gates: a card
    could be marked done through the column operation without ever satisfying
    the gate that completion enforced. A gate that can be walked around is
    worse than a missing one, because it reads as enforcement while providing
    none.
    """
    home = tmp_path / "home"
    _make_card(home, "eeee5555", "touches skcapstone", tags=["repo:skcapstone"])

    with pytest.raises(ValueError, match="commit_sha"):
        move_coord_task(home, "reviewer", "eeee5555", "done")

    _link(home, "eeee5555", "commit_sha", "none")
    move_coord_task(home, "reviewer", "eeee5555", "done")


def test_move_to_a_non_done_column_is_not_gated(tmp_path: Path) -> None:
    """Only the done transition asserts the lifecycle fact. Others are moves."""
    home = tmp_path / "home"
    _make_card(home, "ffff6666", "touches skcapstone", tags=["repo:skcapstone"])

    move_coord_task(home, "reviewer", "ffff6666", "doing")


def test_move_to_done_refuses_a_real_sha_with_no_branch(tmp_path: Path) -> None:
    """The move-to-done entrypoint enforces the branch requirement exactly like
    complete_coord_task, so it is not a second unlocked side door."""
    home = tmp_path / "home"
    _make_card(home, "gggg7777", "touches skcapstone", tags=["repo:skcapstone"])
    _link(home, "gggg7777", "commit_sha", "c" * 40)

    with pytest.raises(ValueError, match="branch"):
        move_coord_task(home, "reviewer", "gggg7777", "done")

    _link(home, "gggg7777", "branch", "skcapstone:fix/gggg7777")
    move_coord_task(home, "reviewer", "gggg7777", "done")


def test_repo_label_prefix_match_is_case_sensitive(tmp_path: Path) -> None:
    """The gate must match `repo:` with the exact case execute_mux._card_routing
    (src/skcapstone/execute_mux.py) uses to route a card to the code bridge, the
    same signal this gate is documented to reuse exactly. execute_mux does not
    lowercase before checking the prefix, so neither should this: a card whose
    label happens to be capitalized differently is not, by that same standard,
    a card execute_mux treats as code work, and this gate must not disagree."""
    home = tmp_path / "home"
    _make_card(home, "hhhh8888", "fix the widget", tags=["Repo:skcapstone"])

    result = complete_coord_task(home, "reviewer", "hhhh8888")

    assert not isinstance(result, GatesPending)
    assert "hhhh8888" in result.completed_tasks


# ---- commit_sha_is_valid: the single shared shape check ----
#
# This used to be a second copy in scripts/fleet/skfleet-rotate.py
# (_COMMIT_SHA_RE / _valid_commit_sha), pinned there via source-extraction
# because that script cannot be imported. It had five passing tests and was
# called from no production code: this module, which does enforce it (see
# _commit_evidence_problem above), is the one shared home, and these are the
# same five pinning tests, now against a normally importable function.


def test_commit_sha_is_valid_accepts_a_genuine_forty_char_sha() -> None:
    assert commit_sha_is_valid("a" * 40) is True
    assert commit_sha_is_valid("0123456789abcdef0123456789abcdef01234567") is True


def test_commit_sha_is_valid_accepts_uppercase_hex() -> None:
    """Git itself is case-insensitive about hex digits; do not punish a worker for case."""
    assert commit_sha_is_valid("A" * 40) is True


def test_commit_sha_is_valid_rejects_the_none_sentinel() -> None:
    """The literal value none is the explicit no-repository-change sentinel, not a SHA."""
    assert commit_sha_is_valid("none") is False


def test_commit_sha_is_valid_rejects_wrong_length_and_non_hex() -> None:
    assert commit_sha_is_valid("a" * 39) is False
    assert commit_sha_is_valid("a" * 41) is False
    assert commit_sha_is_valid("g" * 40) is False
    assert commit_sha_is_valid("") is False


def test_commit_sha_is_valid_rejects_non_string() -> None:
    assert commit_sha_is_valid(None) is False
    assert commit_sha_is_valid(40) is False
