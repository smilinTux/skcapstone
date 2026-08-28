"""Tests for dependency enforcement on `coord claim` and the blocked/unblocked
distinction in `coord status` (card 34be7725)."""

from pathlib import Path

import click
from click.testing import CliRunner
from skcoord.card_store import current_claim_precondition
from skcoord.lifecycle import transition_task

from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _chain_board(tmp_path: Path) -> Board:
    """Board with dep -> child (child depends on dep, which is not done)."""
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="aa000001", title="root dependency"))
    board.create_task(Task(id="bb000002", title="child task", dependencies=["aa000001"]))
    board.create_task(Task(id="cc000003", title="no deps"))
    return board


def test_claim_blocked_task_fails(tmp_path: Path):
    """Claiming a card with an incomplete dependency exits non-zero."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "claim", "bb000002", "--home", str(tmp_path), "--agent", "opus"],
    )
    assert result.exit_code == 1
    assert "incomplete dependencies" in result.output.lower()
    assert "aa000001" in result.output
    board = Board(tmp_path)
    agent = board.load_agent("opus")
    assert agent is None or "bb000002" not in agent.claimed_tasks


def test_claim_blocked_task_with_force_lists_every_gate_and_exits_nonzero(tmp_path: Path):
    """--force remains accepted but cannot bypass any dependency gate."""
    board = _chain_board(tmp_path)
    board.create_task(Task(id="dd000004", title="review dependency"))
    board.claim_task("reviewer", "dd000004")
    transition_task(tmp_path, task_id="dd000004", column="review", actor="ops")
    board.create_task(Task(id="ee000005", title="human dependency", tags=["human-gate"]))
    board.create_task(
        Task(
            id="ff000006",
            title="gated task",
            dependencies=["aa000001", "dd000004", "ee000005", "missing07"],
        )
    )
    result = CliRunner().invoke(
        _main(),
        ["coord", "claim", "ff000006", "--home", str(tmp_path), "--agent", "opus", "--force"],
    )
    assert result.exit_code != 0
    assert "incomplete dependencies" in result.output.lower()
    for dependency in ("aa000001", "dd000004", "ee000005", "missing07"):
        assert dependency in result.output
    board = Board(tmp_path)
    agent = board.load_agent("opus")
    assert agent is None or "ff000006" not in agent.claimed_tasks


def test_complete_blocked_task_fails_with_dependency_id(tmp_path: Path):
    """Normal CLI completion refuses the same folded dependency gate as Board."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "complete", "bb000002", "--home", str(tmp_path), "--agent", "opus"],
    )
    assert result.exit_code == 1
    assert "incomplete dependencies" in result.output.lower()
    assert "aa000001" in result.output
    agent = Board(tmp_path).load_agent("opus")
    assert agent is None or "bb000002" not in agent.completed_tasks


def test_claim_unblocked_after_dependency_done(tmp_path: Path):
    """Once the dependency is done, the child claims without --force."""
    board = _chain_board(tmp_path)
    board.claim_task("jarvis", "aa000001")
    board.complete_task("jarvis", "aa000001")
    result = CliRunner().invoke(
        _main(),
        ["coord", "claim", "bb000002", "--home", str(tmp_path), "--agent", "opus"],
    )
    assert result.exit_code == 0, result.output


def test_status_distinguishes_blocked_from_open(tmp_path: Path):
    """`coord status` labels dep-blocked cards BLOCKED and free cards OPEN."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "BLOCKED" in result.output  # bb000002 (aa000001 not done)
    assert "OPEN" in result.output  # aa000001 and cc000003 remain plainly open


def test_status_filter_blocked(tmp_path: Path):
    """`coord status --status blocked` lists only dep-blocked cards."""
    _chain_board(tmp_path)
    result = CliRunner().invoke(
        _main(),
        ["coord", "status", "--home", str(tmp_path), "--status", "blocked"],
    )
    assert result.exit_code == 0, result.output
    assert "bb000002" in result.output
    assert "aa000001" not in result.output
    assert "cc000003" not in result.output


def test_add_dependency_command_blocks_then_allows_after_completed_gate(tmp_path: Path):
    """The supported command adds a real folded gate without rewriting task birth facts."""
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="a1e00001", title="independent review"))
    work_path = board.create_task(Task(id="b2e00002", title="implementation"))
    birth_bytes = work_path.read_bytes()
    runner = CliRunner()

    added = runner.invoke(
        _main(),
        [
            "coord",
            "add-dependency",
            "b2e00002",
            "--dependency",
            "a1e00001",
            "--reason",
            "independent review is required before implementation",
            "--home",
            str(tmp_path),
            "--agent",
            "reviewer",
        ],
    )
    assert added.exit_code == 0, added.output
    assert work_path.read_bytes() == birth_bytes

    blocked = runner.invoke(
        _main(),
        ["coord", "claim", "b2e00002", "--home", str(tmp_path), "--agent", "implementer"],
    )
    assert blocked.exit_code == 1
    assert "a1e00001" in blocked.output

    board.claim_task("reviewer", "a1e00001")
    board.complete_task("reviewer", "a1e00001")
    eligible = runner.invoke(
        _main(),
        ["coord", "claim", "b2e00002", "--home", str(tmp_path), "--agent", "implementer"],
    )
    assert eligible.exit_code == 0, eligible.output


def test_release_claim_command_is_owner_and_revision_specific(tmp_path: Path):
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="a1e10001", title="release target"))
    board.claim_task("probe", "a1e10001")
    revision = current_claim_precondition(tmp_path, "a1e10001", "probe")
    runner = CliRunner()
    args = [
        "coord",
        "release-claim",
        "a1e10001",
        "--owner",
        "probe",
        "--expected-claim-revision",
        revision,
        "--agent",
        "repair",
        "--home",
        str(tmp_path),
    ]
    first = runner.invoke(_main(), args)
    assert first.exit_code == 0, first.output
    view = next(view for view in Board(tmp_path).get_task_views() if view.task.id == "a1e10001")
    assert view.status.value == "open"
    assert runner.invoke(_main(), args).exit_code != 0


def test_release_claim_requires_expected_revision(tmp_path: Path):
    """The supported mutation boundary never accepts an owner-only release."""
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="a1e10002", title="release target"))
    board.claim_task("probe", "a1e10002")

    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "release-claim",
            "a1e10002",
            "--owner",
            "probe",
            "--agent",
            "repair",
            "--home",
            str(tmp_path),
        ],
    )

    assert result.exit_code != 0
    assert "expected-claim-revision" in result.output
    assert current_claim_precondition(tmp_path, "a1e10002", "probe")


def test_release_claim_preserves_newer_same_owner_generation(tmp_path: Path):
    """An old revision cannot release a newer claim held by the same owner."""
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id="a1e10003", title="release target"))
    board.claim_task("probe", "a1e10003")
    old_revision = current_claim_precondition(tmp_path, "a1e10003", "probe")
    board.release_claim("probe", "a1e10003", actor="worker-exit")
    board.claim_task("probe", "a1e10003")
    new_revision = current_claim_precondition(tmp_path, "a1e10003", "probe")
    assert old_revision != new_revision

    runner = CliRunner()
    stale = runner.invoke(
        _main(),
        [
            "coord",
            "release-claim",
            "a1e10003",
            "--owner",
            "probe",
            "--expected-claim-revision",
            old_revision,
            "--agent",
            "fleet-liveness-reaper",
            "--home",
            str(tmp_path),
        ],
    )

    assert stale.exit_code != 0
    assert "claim revision conflict" in stale.output
    assert current_claim_precondition(tmp_path, "a1e10003", "probe") == new_revision

    current = runner.invoke(
        _main(),
        [
            "coord",
            "release-claim",
            "a1e10003",
            "--owner",
            "probe",
            "--expected-claim-revision",
            new_revision,
            "--agent",
            "fleet-liveness-reaper",
            "--home",
            str(tmp_path),
        ],
    )
    assert current.exit_code == 0, current.output
