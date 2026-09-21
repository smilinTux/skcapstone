"""A refused ITIL transition must not print as success.

The incident path has guarded this for a while. change and problem did not,
and the gap cost a live session: five successive transitions on one change
each printed a green "Updated: chg-... -> reviewing" while the status never
moved, which reads as success and sends the diagnosis the wrong way.
"""

from unittest.mock import patch

import click
from click.testing import CliRunner

from skcapstone.cli.itil import register_itil_commands
from skcapstone.itil import ITILManager


def _cli() -> click.Group:
    @click.group()
    def main():
        pass

    register_itil_commands(main)
    return main


def _run(tmp_path, *args):
    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        return CliRunner().invoke(_cli(), list(args))


def _flat(result) -> str:
    """Collapse rich's line wrapping so assertions can match whole phrases.

    rich wraps to the terminal width, so "requires a note" can arrive as
    "requires a \\nnote" and a naive substring assertion misses it.
    """
    return " ".join(result.output.split())


def _status(tmp_path, change_id: str) -> str:
    changes = ITILManager(tmp_path).list_changes()
    return next(c for c in changes if c.id == change_id).status.value


def _reviewing_change(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="guard me", managed_by="lumina")
    _run(tmp_path, "itil", "change", "update", chg.id, "--status", "reviewing", "--note", "n")
    return mgr, chg.id


def test_change_illegal_transition_is_not_reported_as_success(tmp_path):
    _, cid = _reviewing_change(tmp_path)
    out = _flat(
        _run(tmp_path, "itil", "change", "update", cid, "--status", "deployed", "--note", "n")
    )
    assert "No change" in out
    assert "not a legal transition" in out
    assert "Allowed from reviewing" in out
    assert "Updated:" not in out


def test_change_cab_gate_names_the_real_reason_not_an_illegal_transition(tmp_path):
    """reviewing -> approved IS legal; the CAB guard is what refuses it.

    Reporting it as an illegal transition would be actively misleading, and
    that is the exact case that made the silent no-op expensive to diagnose.
    """
    _, cid = _reviewing_change(tmp_path)
    out = _flat(
        _run(tmp_path, "itil", "change", "update", cid, "--status", "approved", "--note", "n")
    )
    assert "No change" in out
    assert "approval cannot be granted by a status update" in out
    assert "cab" in out.lower()
    assert "not a legal transition" not in out
    assert "Updated:" not in out


def test_change_note_gated_transition_says_a_note_is_required(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="note gate", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    _run(tmp_path, "itil", "change", "update", chg.id, "--status", "implementing", "--note", "n")
    _run(tmp_path, "itil", "change", "update", chg.id, "--status", "deployed", "--note", "n")
    out = _flat(_run(tmp_path, "itil", "change", "update", chg.id, "--status", "verified"))
    assert "No change" in out
    assert "requires a note" in out
    assert "Updated:" not in out


def test_change_legal_transition_still_reports_success(tmp_path):
    """The guard must not swallow a transition that genuinely happened."""
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="legal move", managed_by="lumina")
    mgr.submit_cab_vote(chg.id, agent="human", decision="approved")
    out = _flat(
        _run(
            tmp_path, "itil", "change", "update", chg.id, "--status", "implementing", "--note", "n"
        )
    )
    assert "Updated:" in out
    assert "No change" not in out
    assert _status(tmp_path, chg.id) == "implementing"


def test_problem_illegal_transition_is_not_reported_as_success(tmp_path):
    mgr = ITILManager(tmp_path)
    prb = mgr.create_problem(title="guard me too", created_by="lumina")
    out = _flat(
        _run(tmp_path, "itil", "problem", "update", prb.id, "--status", "resolved", "--note", "n")
    )
    assert "No change" in out
    assert "not a legal transition" in out
    assert "Updated:" not in out
