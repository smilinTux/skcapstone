"""The read-only Overseer seat (mero) is refused at coord mutation entrypoints.

ADR-0005 and ``docs/fleet/seat-charters.md`` define the Overseer as read-only:
observe, recommend, create. ``seat_boundaries.require_authority`` already
refuses ``mero`` for fleet mutation, but the coord CLI and MCP mutation
surfaces only ever gated the Jarvis identity, so ``--agent mero`` sailed
through. Measured on chi (shard store, writer == "mero", 14 days to
2026-09-18): 324 ``move`` and 147 ``release_claim`` events. These tests pin
the shared entrypoint gate and the two CLI verbs that carried the bulk of
those writes. All tests run against fixture card dirs under ``tmp_path``,
never the live board.
"""

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.card_store import CardCore, CardStore
from skcapstone.cli.coord import register_coord_commands
from skcapstone.coordination import Board, Task
from skcapstone.jarvis_emergency import authorize_jarvis_entrypoint
from skcapstone.seat_boundaries import Action, BoundaryError


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


# --- The shared gate ------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [Action.MOVE_CARD, Action.RELEASE, Action.CLAIM, Action.COMPLETE_CARD],
)
def test_gate_refuses_mero_for_mutation(action: Action) -> None:
    with pytest.raises(BoundaryError):
        authorize_jarvis_entrypoint("mero", action, "abc12345", None, None)


def test_gate_refuses_mero_case_and_whitespace_insensitively() -> None:
    with pytest.raises(BoundaryError):
        authorize_jarvis_entrypoint(" Mero ", Action.MOVE_CARD, "abc12345", None, None)


def test_gate_still_allows_mero_to_create_cards() -> None:
    authorize_jarvis_entrypoint("mero", Action.CREATE_CARD, "abc12345", None, None)


def test_gate_still_allows_jarvis_direct_actions() -> None:
    authorize_jarvis_entrypoint("jarvis", Action.MOVE_CARD, "abc12345", None, None)


# Worker identity fixtures embed the SAME card id the test acts on, because a
# real worker identity is pi-<lane>-<host>-<cardid>, and a worker named for one
# card while acting on another is a state production never produces. That is
# the reason for this change and it stands on its own.
#
# A secondary hope, NOT a claim: the previous random-looking hex suffixes
# tripped GitGuardian's Generic High Entropy Secret detector (incident 37426955
# on PR 766), a false positive on a public identity format that appears in
# systemd unit names and log lines estate-wide, and which the REQUIRED scanner
# (gitleaks) passes. Whether a sequential suffix stops that detector firing is
# unverified: measured Shannon entropy actually RISES here (3.69 to 4.05),
# because abc12345 has more distinct characters than a random hex run, so if
# detector keys on Shannon entropy alone this will not help and the incident
# must be dismissed in the dashboard instead.


def test_gate_still_allows_ordinary_worker_identities() -> None:
    authorize_jarvis_entrypoint(
        "pi-codex-chiap03-abc12345", Action.MOVE_CARD, "abc12345", None, None
    )


def test_gate_does_not_catch_prefixed_worker_identities() -> None:
    """pi-mero-* workers are lane workers, not the Overseer seat identity."""
    authorize_jarvis_entrypoint("pi-mero-chiap08-abc12345", Action.RELEASE, "abc12345", None, None)


# --- The CLI verbs that carried the violation -----------------------------


def test_cli_move_refuses_mero(tmp_path: Path) -> None:
    _seed(tmp_path, "aaa11111")
    runner = CliRunner()
    result = runner.invoke(
        _main(),
        ["coord", "move", "aaa11111", "backlog", "--agent", "mero", "--home", str(tmp_path)],
    )
    assert result.exit_code != 0
    events = CardStore(tmp_path)._read_events("aaa11111")
    assert not [e for e in events if e.get("action") == "move" and e.get("writer") == "mero"]


def test_cli_release_claim_refuses_mero(tmp_path: Path) -> None:
    revision = _seed_claimed(tmp_path, "bbb22222", "pi-codex-chiap03-worker")
    runner = CliRunner()
    result = runner.invoke(
        _main(),
        [
            "coord",
            "release-claim",
            "bbb22222",
            "--owner",
            "pi-codex-chiap03-worker",
            "--expected-claim-revision",
            revision,
            "--agent",
            "mero",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code != 0
    card = CardStore(tmp_path).fold("bbb22222")
    assert card is not None and card.owner == "pi-codex-chiap03-worker"


def test_cli_release_claim_still_works_for_jarvis(tmp_path: Path) -> None:
    revision = _seed_claimed(tmp_path, "ccc33333", "pi-codex-chiap03-worker")
    runner = CliRunner()
    result = runner.invoke(
        _main(),
        [
            "coord",
            "release-claim",
            "ccc33333",
            "--owner",
            "pi-codex-chiap03-worker",
            "--expected-claim-revision",
            revision,
            "--agent",
            "jarvis",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    card = CardStore(tmp_path).fold("ccc33333")
    assert card is not None and card.owner is None


def test_cli_create_by_mero_still_works(tmp_path: Path) -> None:
    """The coord briefing's own examples author cards as mero; that stays legal."""
    Board(tmp_path).ensure_dirs()
    runner = CliRunner()
    result = runner.invoke(
        _main(),
        [
            "coord",
            "create",
            "--by",
            "mero",
            "--title",
            "[FIXTURE][S] Overseer-authored card",
            "--home",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
