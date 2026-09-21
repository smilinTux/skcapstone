"""One capability table, keyed by seat, at every coord mutation entrypoint.

PR 766 proved the structural defect: ``authorize_jarvis_entrypoint`` checked
exactly one identity by name (then two), so every other seat mutated the
board unchecked. These tests pin the replacement: a single decision function
(``seat_boundaries.require_coord_authority``) over a single per-seat
capability table, consulted by every coord mutation entrypoint, refusing an
identity the table and grammar do not know. Fleet workers
(``pi-codex-chiap04-<cardid>``), known operators, and system writers are
explicitly classified and pass; a bare unknown name is refused.

The coverage test at the bottom fails when a mutation entrypoint exists with
no authorization call, which is exactly how ``release-claim`` shipped without
one. All board fixtures live under ``tmp_path``; nothing touches a live board.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

import skcapstone.cli.coord as cli_coord
import skcapstone.cli.coord_amend as cli_coord_amend
import skcapstone.mcp_tools.coord_card_tools as mcp_card_tools
import skcapstone.mcp_tools.coord_tools as mcp_coord_tools
from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.seat_boundaries import (
    COORD_MUTATIONS,
    COORD_SEAT_CAPABILITIES,
    Action,
    BoundaryError,
    CoordActorClass,
    Seat,
    classify_coord_actor,
    require_coord_authority,
)

# --- Fixtures -------------------------------------------------------------


def _main() -> click.Group:
    @click.group()
    def main():
        pass

    register_coord_commands(main)
    return main


def _seed(tmp_path: Path, task_id: str, title: str = "Fixture card") -> None:
    board = Board(tmp_path)
    board.ensure_dirs()
    board.create_task(Task(id=task_id, title=title))
    CardStore(tmp_path).create(CardCore(id=task_id, title=title))


def _seed_claimed(tmp_path: Path, task_id: str, owner: str) -> str:
    board = Board(tmp_path)
    board.ensure_dirs()
    _, revision = board.create_claimed_task(Task(id=task_id, title="Claimed"), owner)
    return revision


# --- Fail closed on unknown identity --------------------------------------


@pytest.mark.parametrize("action", sorted(COORD_MUTATIONS))
def test_unknown_identity_is_refused_for_every_mutation(action: Action) -> None:
    with pytest.raises(BoundaryError):
        require_coord_authority("zorp", action)


@pytest.mark.parametrize("actor", ["SKAGENT", "chiap01", "somebody"])
def test_bare_unlisted_names_are_refused(actor: str) -> None:
    with pytest.raises(BoundaryError):
        require_coord_authority(actor, Action.MOVE_CARD)


# --- The capability grid, pinned seat by verb ------------------------------

_C = COORD_MUTATIONS
_CARD_WORK = frozenset(
    {
        Action.CLAIM,
        Action.RELEASE,
        Action.MOVE_CARD,
        Action.COMPLETE_CARD,
        Action.LINK_CARD,
        Action.VOID_CARD,
        Action.DESCRIBE_CARD,
        Action.LABEL_CARD,
        Action.AMEND_CRITERIA,
        Action.AMEND_DEPENDENCIES,
        Action.SATISFY_GATE,
        Action.CREATE_CARD,
    }
)

#: The expected grid, written out independently of the production table so a
#: table edit must be mirrored here deliberately.
EXPECTED_GRID: dict[Seat, frozenset[Action]] = {
    Seat.MERO: frozenset({Action.CREATE_CARD}),
    Seat.LINK: _CARD_WORK | {Action.REPRIORITIZE_CARD},
    Seat.SERAPH: _CARD_WORK,
    Seat.NIOBE: _CARD_WORK | {Action.REPRIORITIZE_CARD, Action.MAINTAIN_BOARD},
    Seat.ATLAS: _CARD_WORK,
    Seat.TANK: _CARD_WORK | {Action.REPRIORITIZE_CARD},
    Seat.JARVIS: _C,
}


@pytest.mark.parametrize("seat", sorted(Seat))
@pytest.mark.parametrize("action", sorted(COORD_MUTATIONS))
def test_each_seat_gets_exactly_its_charter_verbs(seat: Seat, action: Action) -> None:
    allowed = action in EXPECTED_GRID[seat]
    if allowed:
        require_coord_authority(seat.value, action)
    else:
        with pytest.raises(BoundaryError):
            require_coord_authority(seat.value, action)


def test_production_table_has_no_extra_coord_grants() -> None:
    for seat in Seat:
        assert COORD_SEAT_CAPABILITIES[seat] & COORD_MUTATIONS == EXPECTED_GRID[seat]


def test_seat_spelling_variants_resolve_to_the_seat() -> None:
    for actor in (" Mero ", "MERO", "capauth:mero", "mero@skworld.io"):
        with pytest.raises(BoundaryError):
            require_coord_authority(actor, Action.MOVE_CARD)


# --- Workers, operators, and system writers are not seats ------------------


@pytest.mark.parametrize(
    "actor",
    [
        "pi-codex-chiap04-fc2d87bf",
        "pi-glm-chiap01-0aec5a64",
        "pi-mero-chiap08-ab12cd34",
        "cursor-w73-live",
        "kimi-herdr-19fa3fb7",
        "jarvis-reconcile-41e9c0de",
        "lifecycle-reconciler",
        "archive-done",
        "stale-sweep",
    ],
)
def test_fleet_worker_and_automation_identities_pass(actor: str) -> None:
    for action in (Action.CLAIM, Action.RELEASE, Action.MOVE_CARD, Action.COMPLETE_CARD):
        require_coord_authority(actor, action)


@pytest.mark.parametrize("actor", ["chef", "casey", "lumina", "Lumina", "human", "codex"])
def test_operator_identities_pass(actor: str) -> None:
    for action in sorted(COORD_MUTATIONS):
        require_coord_authority(actor, action)


def test_identityless_tool_default_passes() -> None:
    require_coord_authority("", Action.LABEL_CARD)
    require_coord_authority("  ", Action.DESCRIBE_CARD)


def test_classification_is_explicit() -> None:
    assert classify_coord_actor("mero")[0] is CoordActorClass.SEAT
    assert classify_coord_actor("pi-codex-chiap04-ab12cd34")[0] is CoordActorClass.DELEGATE
    assert classify_coord_actor("chef")[0] is CoordActorClass.OPERATOR
    assert classify_coord_actor("mcp")[0] is CoordActorClass.DELEGATE
    assert classify_coord_actor("")[0] is CoordActorClass.TOOL
    assert classify_coord_actor("zorp")[0] is CoordActorClass.UNKNOWN


# --- CLI entrypoints enforce the table ------------------------------------


def test_cli_void_refuses_mero(tmp_path: Path) -> None:
    _seed(tmp_path, "abc12345")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "void",
            "abc12345",
            "--reason",
            "test",
            "--agent",
            "mero",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    events = CardStore(tmp_path)._read_events("abc12345")
    assert not [e for e in events if e.get("action") == "void"]


def test_cli_label_refuses_unknown_identity(tmp_path: Path) -> None:
    _seed(tmp_path, "abc12345")
    result = CliRunner().invoke(
        _main(),
        ["coord", "label", "abc12345", "needs-triage", "--agent", "zorp", "--home", str(tmp_path)],
    )
    assert result.exit_code != 0


def test_cli_label_allows_fleet_worker(tmp_path: Path) -> None:
    _seed(tmp_path, "abc12345")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "label",
            "abc12345",
            "needs-triage",
            "--agent",
            "pi-codex-chiap04-fc2d87bf",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_void_allows_seraph(tmp_path: Path) -> None:
    _seed(tmp_path, "abc12345")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "void",
            "abc12345",
            "--reason",
            "dupe",
            "--agent",
            "seraph",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output


def test_cli_add_dependency_refuses_mero(tmp_path: Path) -> None:
    _seed(tmp_path, "abc12345")
    _seed(tmp_path, "def67890")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "add-dependency",
            "abc12345",
            "--dependency",
            "def67890",
            "--reason",
            "gate",
            "--agent",
            "mero",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0


def test_cli_release_claim_allows_operator(tmp_path: Path) -> None:
    revision = _seed_claimed(tmp_path, "abc12345", "pi-codex-chiap04-fc2d87bf")
    result = CliRunner().invoke(
        _main(),
        [
            "coord",
            "release-claim",
            "abc12345",
            "--owner",
            "pi-codex-chiap04-fc2d87bf",
            "--expected-claim-revision",
            revision,
            "--agent",
            "lumina",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output


# --- MCP entrypoints enforce the table ------------------------------------


def test_mcp_void_refuses_mero() -> None:
    result = asyncio.run(
        mcp_card_tools._handle_coord_void(
            {"task_id": "abc12345", "reason": "test", "agent": "mero"}
        )
    )
    assert "not authorized" in result[0].text


def test_mcp_describe_refuses_unknown_identity() -> None:
    result = asyncio.run(
        mcp_card_tools._handle_coord_describe(
            {"task_id": "abc12345", "title": "x", "agent": "zorp"}
        )
    )
    assert "unknown coordination identity" in result[0].text


# --- Coverage: no mutation entrypoint without an authorization call --------

#: Coord CLI verbs that only read (or render derived artifacts such as
#: BOARD.md). A NEW command is treated as a mutation until it is deliberately
#: classified here, so an ungated new verb fails this test by default.
READ_ONLY_CLI = {
    "status",
    "gates",
    # Deliberately classified read-only 2026-09-18: slice-preflight folds one
    # card and prints a decomposition RECOMMENDATION (recommendation-only in
    # its output contract); it creates and changes nothing.
    "slice-preflight",
    "waiting-on-human",
    "board",
    "kanban",
    # Deliberately classified read-only 2026-09-20: show folds ONE card and
    # prints it. It is the single-card counterpart to kanban, which is already
    # classified here for the same reason, and it appends no event.
    "show",
    "parity",
    "changelog",
    "briefing",
}

#: MCP coord handlers that only read.
READ_ONLY_MCP = {"_handle_coord_status", "_handle_coord_kanban"}

GATE_NAME = "authorize_coord_mutation"


def _calls_gate(node: ast.AST) -> bool:
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            func = sub.func
            if isinstance(func, ast.Name) and func.id == GATE_NAME:
                return True
            if isinstance(func, ast.Attribute) and func.attr == GATE_NAME:
                return True
    return False


def _cli_commands(module) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    commands: dict[str, ast.FunctionDef] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for decorator in node.decorator_list:
            if (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr == "command"
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
            ):
                commands[decorator.args[0].value] = node
    return commands


def test_every_mutating_cli_verb_consults_the_gate() -> None:
    commands: dict[str, ast.FunctionDef] = {}
    commands.update(_cli_commands(cli_coord))
    commands.update(_cli_commands(cli_coord_amend))
    assert len(commands) >= 30, sorted(commands)
    stale = READ_ONLY_CLI - set(commands)
    assert not stale, f"read-only allowlist names unknown commands: {stale}"
    ungated = [
        name
        for name, node in sorted(commands.items())
        if name not in READ_ONLY_CLI and not _calls_gate(node)
    ]
    assert not ungated, (
        "coord mutation entrypoints with no authorization call "
        f"(gate them or classify them read-only): {ungated}"
    )


def _mcp_handlers(module) -> dict[str, ast.FunctionDef]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("_handle_coord_")
    }


def test_every_mutating_mcp_handler_consults_the_gate() -> None:
    handlers: dict[str, ast.FunctionDef] = {}
    handlers.update(_mcp_handlers(mcp_coord_tools))
    handlers.update(_mcp_handlers(mcp_card_tools))
    assert len(handlers) >= 12, sorted(handlers)
    stale = READ_ONLY_MCP - set(handlers)
    assert not stale, f"read-only allowlist names unknown handlers: {stale}"
    ungated = [
        name
        for name, node in sorted(handlers.items())
        if name not in READ_ONLY_MCP and not _calls_gate(node)
    ]
    assert not ungated, (
        "MCP coord mutation handlers with no authorization call "
        f"(gate them or classify them read-only): {ungated}"
    )
