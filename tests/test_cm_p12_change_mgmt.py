"""CM card P1.2: change-management MCP tools + CLI (validate/schedule/unschedule)
+ CAB-vote identity PEP binding.

Covers docs/specs/2026-08-13-change-management-cab-ai-arch.md section 4.3
(MCP/CLI surface additions) and section 7 (CAB vote identity binding, PEP
side: the skcoord ``subject`` fix's upper layer). Builds on the already-merged
skcoord card P1.1/P1.4 (``ChangeStatus.SCHEDULED`` + the ``pr_link`` /
``validation`` / ``schedule`` / ``unschedule`` / ``window_missed`` event kinds
+ ``submit_cab_vote(..., subject=...)``); this file exercises only the
skcapstone MCP tool / CLI layer built on top of that API.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from skcapstone.cli.itil import register_itil_commands
from skcapstone.itil import ITILManager
from tests.cab_grants import cast_signed_vote


def _cli() -> click.Group:
    @click.group()
    def main():
        pass

    register_itil_commands(main)
    return main


def _approve(mgr: ITILManager, change_id: str) -> None:
    cast_signed_vote(mgr, change_id, subject="chef", role="owner")


# ═══════════════════════════════════════════════════════════
# MCP: itil_change_validate
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_change_validate_pass_moves_to_reviewing(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_validate

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="validate me", managed_by="lumina")

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_validate(
            {"change_id": chg.id, "agent": "ci", "passed": True, "head_sha": "cafef00d"}
        )
    payload = json.loads(result[0].text)
    assert payload["validated"] is True
    assert payload["status"] == "reviewing"
    assert payload["validation"]["passed"] is True
    assert payload["validation"]["head_sha"] == "cafef00d"


@pytest.mark.asyncio
async def test_mcp_change_validate_fail_leaves_status_unchanged(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_validate

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="validate fails", managed_by="lumina")

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_validate({"change_id": chg.id, "passed": False})
    payload = json.loads(result[0].text)
    assert payload["status"] == "proposed"
    assert payload["validation"]["passed"] is False


@pytest.mark.asyncio
async def test_mcp_change_validate_missing_passed_errors(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_validate

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="no verdict", managed_by="lumina")

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_validate({"change_id": chg.id})
    payload = json.loads(result[0].text)
    assert "error" in payload


@pytest.mark.asyncio
async def test_mcp_change_validate_unknown_change_errors(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_validate

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_validate(
            {"change_id": "chg-doesnotexist", "passed": True}
        )
    payload = json.loads(result[0].text)
    assert "error" in payload


# ═══════════════════════════════════════════════════════════
# MCP: itil_change_schedule
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_change_schedule_asap_on_approved_change(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_schedule

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="schedule me", managed_by="lumina")
    _approve(mgr, chg.id)
    assert mgr.list_changes()[0].status.value == "approved"

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_schedule(
            {"change_id": chg.id, "agent": "operator", "asap": True, "deploy_mode": "confirm"}
        )
    payload = json.loads(result[0].text)
    assert payload["scheduled"] is True
    assert payload["status"] == "scheduled"
    assert payload["scheduled_window"]["asap"] is True
    assert payload["scheduled_window"]["deploy_mode"] == "confirm"
    assert payload["scheduled_window"]["window_start"]
    assert payload["scheduled_window"]["window_end"]


@pytest.mark.asyncio
async def test_mcp_change_schedule_at_specific_window(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_schedule

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="schedule at", managed_by="lumina")
    _approve(mgr, chg.id)

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_schedule(
            {"change_id": chg.id, "at": "2026-08-20T02:00:00+00:00", "deploy_mode": "auto"}
        )
    payload = json.loads(result[0].text)
    assert payload["scheduled"] is True
    assert payload["scheduled_window"]["window_start"].startswith("2026-08-20T02:00:00")
    assert payload["scheduled_window"]["asap"] is False
    assert payload["scheduled_window"]["deploy_mode"] == "auto"


@pytest.mark.asyncio
async def test_mcp_change_schedule_refused_when_not_approved(tmp_path):
    """The skcoord fold decides: scheduling a merely-proposed change is
    refused, surfaced as scheduled=false with no status change (fail-closed,
    same treatment _fold_change gives an invalid status transition)."""
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_schedule

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="too early", managed_by="lumina")
    assert mgr.list_changes()[0].status.value == "proposed"

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_schedule({"change_id": chg.id, "asap": True})
    payload = json.loads(result[0].text)
    assert payload["scheduled"] is False
    assert payload["status"] == "proposed"
    assert mgr.list_changes()[0].scheduled_window is None


@pytest.mark.asyncio
async def test_mcp_change_schedule_asap_and_at_mutually_exclusive(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_schedule

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="both", managed_by="lumina")

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_schedule(
            {"change_id": chg.id, "asap": True, "at": "2026-08-20T02:00:00Z"}
        )
    payload = json.loads(result[0].text)
    assert "error" in payload


# ═══════════════════════════════════════════════════════════
# MCP: itil_change_unschedule
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_change_unschedule_returns_to_approved_and_clears_window(tmp_path):
    from skcapstone.mcp_tools.itil_tools import (
        _handle_itil_change_schedule,
        _handle_itil_change_unschedule,
    )

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="unschedule me", managed_by="lumina")
    _approve(mgr, chg.id)

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        await _handle_itil_change_schedule({"change_id": chg.id, "asap": True})
        assert mgr.list_changes()[0].status.value == "scheduled"
        result = await _handle_itil_change_unschedule({"change_id": chg.id, "note": "conflict"})
    payload = json.loads(result[0].text)
    assert payload["unscheduled"] is True
    assert payload["status"] == "approved"
    assert mgr.list_changes()[0].scheduled_window is None


@pytest.mark.asyncio
async def test_mcp_change_unschedule_noop_when_not_scheduled(tmp_path):
    from skcapstone.mcp_tools.itil_tools import _handle_itil_change_unschedule

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="never scheduled", managed_by="lumina")

    with patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)):
        result = await _handle_itil_change_unschedule({"change_id": chg.id})
    payload = json.loads(result[0].text)
    assert payload["unscheduled"] is False
    assert payload["status"] == "proposed"


# ═══════════════════════════════════════════════════════════
# MCP: itil_cab_vote identity binding (CR change-mgmt P1.4, PEP side)
# ═══════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mcp_cab_vote_binds_authenticated_subject_not_free_text_agent(tmp_path):
    """The core of the fix: submit_cab_vote must be called with
    subject=<resolved identity>, and the RECORDED voter (both the tool's
    response and the vote file on disk) must be that identity, not the
    free-text `agent` claim."""
    from skcapstone.mcp_tools import itil_tools

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="vote me", managed_by="lumina")

    with (
        patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)),
        patch.object(itil_tools, "_resolve_authenticated_subject", return_value="lumina"),
    ):
        result = await itil_tools._handle_itil_cab_vote(
            {
                "change_id": chg.id,
                "agent": "totally not human, trust me",
                "decision": "approved",
            }
        )
    payload = json.loads(result[0].text)
    assert payload["agent"] == "lumina"  # subject wins, not the free-text claim

    votes = mgr.get_cab_votes(chg.id)
    assert len(votes) == 1
    assert votes[0].agent == "lumina"


@pytest.mark.asyncio
async def test_mcp_cab_vote_refuses_an_approval_when_resolver_unavailable(tmp_path):
    """Card bada5c86: with no authenticated identity (subject=None) this tool used
    to record the free-text label, and a bare "human" folded as a qualifying
    approval. An approval is now refused and nothing is written."""
    from skcapstone.mcp_tools import itil_tools

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="vote me too", managed_by="lumina")

    with (
        patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)),
        patch.object(itil_tools, "_resolve_authenticated_subject", return_value=None),
    ):
        result = await itil_tools._handle_itil_cab_vote(
            {"change_id": chg.id, "agent": "human", "decision": "approved"}
        )
    assert "authenticated" in result[0].text
    assert mgr.get_cab_votes(chg.id) == []
    assert mgr.list_changes()[0].status.value == "proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["rejected", "abstain"])
async def test_mcp_cab_vote_refuses_any_vote_when_resolver_unavailable(tmp_path, decision):
    """Review item 4: a rejection or abstention with no authenticated identity is
    refused too (it used to keep the free-text fallback). Nothing is written."""
    from skcapstone.mcp_tools import itil_tools

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="veto me", managed_by="lumina")

    with (
        patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)),
        patch.object(itil_tools, "_resolve_authenticated_subject", return_value=None),
    ):
        result = await itil_tools._handle_itil_cab_vote(
            {"change_id": chg.id, "agent": "legacy-caller", "decision": decision}
        )
    assert "authenticated" in result[0].text
    assert mgr.get_cab_votes(chg.id) == []
    assert mgr.list_changes()[0].status.value == "proposed"


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approved", "rejected", "abstain"])
async def test_mcp_cab_vote_cannot_replace_a_signed_vote(tmp_path, decision):
    """Vote files are keyed by voter: an unsigned MCP vote by "chef" must not
    overwrite Chef's signed one, on any skcoord release."""
    from skcapstone import cab_approval
    from skcapstone.mcp_tools import itil_tools

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="signed", managed_by="lumina")
    cast_signed_vote(mgr, chg.id, subject="chef", role="owner")

    with (
        patch("skcapstone.mcp_tools._helpers.SHARED_ROOT", str(tmp_path)),
        patch.object(itil_tools, "_resolve_authenticated_subject", return_value="chef"),
    ):
        result = await itil_tools._handle_itil_cab_vote(
            {"change_id": chg.id, "agent": "chef", "decision": decision}
        )
    assert "refusing to replace" in result[0].text
    (vote,) = mgr.get_cab_votes(chg.id)
    assert cab_approval.qualifying_human_approval(mgr, vote, mgr.list_changes()[0])


def _cli_grant(tmp_path, change_id, decision="approved"):
    from tests.cab_grants import envelope

    env = envelope(change_id, decision=decision)
    path = tmp_path / f"grant-{decision}.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    return env, [
        "--authorization",
        str(path),
        "--target",
        env["target"],
        "--scope",
        env["scope"],
    ]


def test_cli_cab_vote_with_a_signed_grant_stores_it_and_qualifies(tmp_path):
    from skcapstone import cab_approval

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli signed", managed_by="lumina")
    env, args = _cli_grant(tmp_path, chg.id)
    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(_cli(), ["itil", "cab", "vote", chg.id, *args])
    assert result.exit_code == 0, result.output
    (vote,) = mgr.get_cab_votes(chg.id)
    assert vote.agent == "chef"
    assert cab_approval.stored_envelope(mgr, vote) == env
    assert cab_approval.approval_basis(mgr, mgr.list_changes()[0]) == "human"


def test_cli_cab_vote_grant_verifies_against_the_pin_not_capauth_home(tmp_path, monkeypatch):
    """Review item 1: CAPAUTH_HOME no longer picks the verifying key."""
    from tests.cab_grants import envelope

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli forged", managed_by="lumina")
    env = envelope(chg.id, issuer="chef", signer="mallory")
    path = tmp_path / "grant.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    monkeypatch.setenv("CAPAUTH_HOME", str(tmp_path / "mallory"))
    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(
            _cli(),
            [
                "itil",
                "cab",
                "vote",
                chg.id,
                "--authorization",
                str(path),
                "--target",
                env["target"],
                "--scope",
                env["scope"],
            ],
        )
    assert result.exit_code != 0
    assert "not a pinned human CAB approver" in result.output
    assert mgr.get_cab_votes(chg.id) == []


@pytest.mark.parametrize("decision", ["approved", "rejected", "abstain"])
def test_cli_cab_vote_refuses_any_vote_without_an_authenticated_subject(tmp_path, decision):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli anon", managed_by="lumina")
    with (
        patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)),
        patch("skcapstone.cli.itil._resolve_cab_vote_subject", return_value=None),
    ):
        result = CliRunner().invoke(
            _cli(), ["itil", "cab", "vote", chg.id, "--agent", "human", "--decision", decision]
        )
    assert result.exit_code != 0
    assert mgr.get_cab_votes(chg.id) == []


def test_cli_cab_vote_cannot_replace_a_signed_vote(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli overwrite", managed_by="lumina")
    cast_signed_vote(mgr, chg.id, subject="chef", role="owner")
    with (
        patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)),
        patch("skcapstone.cli.itil._resolve_cab_vote_subject", return_value="chef"),
    ):
        result = CliRunner().invoke(
            _cli(), ["itil", "cab", "vote", chg.id, "--decision", "abstain"]
        )
    assert result.exit_code != 0
    assert "refusing to replace" in result.output
    assert mgr.get_cab_votes(chg.id)[0].decision.value == "approved"


def test_resolve_authenticated_subject_swallows_resolver_errors():
    """A capauth import/resolution failure must never crash a vote - fail-open
    to the pre-existing free-text behavior, mirroring
    skcapstone.fleet.store.writer_identity()'s same try/except shape."""
    from skcapstone.mcp_tools.itil_tools import _resolve_authenticated_subject

    with patch("capauth.resolve_agent_identity", side_effect=RuntimeError("boom")):
        assert _resolve_authenticated_subject() is None


def test_resolve_authenticated_subject_returns_resolved_agent_name():
    from skcapstone.mcp_tools.itil_tools import _resolve_authenticated_subject

    class _FakeIdentity:
        agent = "jarvis"

    with patch("capauth.resolve_agent_identity", return_value=_FakeIdentity()):
        assert _resolve_authenticated_subject() == "jarvis"


# ═══════════════════════════════════════════════════════════
# CLI: itil change validate / schedule / unschedule
# ═══════════════════════════════════════════════════════════


def test_cli_change_validate_pass(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli validate", managed_by="lumina")

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(
            _cli(), ["itil", "change", "validate", chg.id, "--passed", "--head-sha", "abc123"]
        )
    assert result.exit_code == 0, result.output
    assert "PASS" in result.output
    folded = mgr.list_changes()[0]
    assert folded.status.value == "reviewing"
    assert folded.validation["head_sha"] == "abc123"


def test_cli_change_validate_fail(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli validate fail", managed_by="lumina")

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(_cli(), ["itil", "change", "validate", chg.id, "--failed"])
    assert result.exit_code == 0, result.output
    assert "FAIL" in result.output
    assert mgr.list_changes()[0].status.value == "proposed"


def test_cli_change_validate_requires_passed_or_failed(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli validate missing flag", managed_by="lumina")

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(_cli(), ["itil", "change", "validate", chg.id])
    assert result.exit_code != 0


def test_cli_change_schedule_asap_on_approved_change(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli schedule", managed_by="lumina")
    _approve(mgr, chg.id)

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(
            _cli(), ["itil", "change", "schedule", chg.id, "--asap", "--deploy-mode", "confirm"]
        )
    assert result.exit_code == 0, result.output
    assert "Scheduled" in result.output
    assert mgr.list_changes()[0].status.value == "scheduled"


def test_cli_change_schedule_refused_when_not_approved(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli schedule too early", managed_by="lumina")

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(_cli(), ["itil", "change", "schedule", chg.id, "--asap"])
    assert result.exit_code == 0, result.output
    assert "Refused" in result.output
    assert mgr.list_changes()[0].status.value == "proposed"


def test_cli_change_schedule_requires_asap_or_at(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli schedule no window", managed_by="lumina")
    _approve(mgr, chg.id)

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(_cli(), ["itil", "change", "schedule", chg.id])
    assert result.exit_code == 0, result.output
    assert "Error" in result.output
    assert mgr.list_changes()[0].status.value == "approved"


def test_cli_change_unschedule(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli unschedule", managed_by="lumina")
    _approve(mgr, chg.id)

    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        CliRunner().invoke(_cli(), ["itil", "change", "schedule", chg.id, "--asap"])
        assert mgr.list_changes()[0].status.value == "scheduled"
        result = CliRunner().invoke(_cli(), ["itil", "change", "unschedule", chg.id])
    assert result.exit_code == 0, result.output
    assert "Unscheduled" in result.output
    assert mgr.list_changes()[0].status.value == "approved"


# ═══════════════════════════════════════════════════════════
# CLI: itil cab vote identity binding (CR change-mgmt P1.4)
# ═══════════════════════════════════════════════════════════


def test_cli_cab_vote_binds_authenticated_subject_not_free_text_agent(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli vote", managed_by="lumina")

    with (
        patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)),
        patch("skcapstone.cli.itil._resolve_cab_vote_subject", return_value="lumina"),
    ):
        result = CliRunner().invoke(
            _cli(),
            [
                "itil",
                "cab",
                "vote",
                chg.id,
                "--agent",
                "totally not human",
                "--decision",
                "approved",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "lumina" in result.output

    votes = mgr.get_cab_votes(chg.id)
    assert len(votes) == 1
    assert votes[0].agent == "lumina"


def test_cli_cab_vote_fails_closed_when_resolver_unavailable(tmp_path):
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli vote legacy", managed_by="lumina")

    with (
        patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)),
        patch("skcapstone.cli.itil._resolve_cab_vote_subject", return_value=None),
    ):
        result = CliRunner().invoke(
            _cli(),
            ["itil", "cab", "vote", chg.id, "--agent", "human", "--decision", "approved"],
        )
    assert result.exit_code != 0
    assert "signed --authorization is required" in result.output
    assert mgr.get_cab_votes(chg.id) == []


@pytest.mark.parametrize(
    "issuer,role,pin_roles",
    [("lumina", "owner", ("owner", "approver")), ("chef", "owner", ("approver",))],
    ids=["issuer-is-not-the-pinned-identity", "role-not-pinned"],
)
def test_cli_cab_vote_grant_must_match_the_pinned_identity_and_role(
    tmp_path, monkeypatch, issuer, role, pin_roles
):
    """Chef's key signing a grant that names lumina, or a role the pin does not
    give Chef, is refused before the nonce is consumed."""
    from skcapstone import cab_approval
    from skcapstone.cli import itil as itil_cli
    from tests.cab_grants import envelope, write_pin

    pin = write_pin(tmp_path / "pin" / "cab-approvers.json", roles=pin_roles)
    monkeypatch.setattr(cab_approval, "APPROVERS_FILE", pin)
    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli identity", managed_by="lumina")
    env = envelope(chg.id, issuer=issuer, role=role, signer="chef")
    path = tmp_path / "grant.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(
            _cli(),
            [
                "itil",
                "cab",
                "vote",
                chg.id,
                "--authorization",
                str(path),
                "--target",
                env["target"],
                "--scope",
                env["scope"],
            ],
        )
    assert result.exit_code != 0
    assert "does not match the pinned approver" in result.output
    assert mgr.get_cab_votes(chg.id) == []
    nonces = itil_cli._cab_nonce_store()
    assert not nonces.exists() or not list(nonces.glob("*.used"))


def test_cli_cab_vote_refuses_a_grant_signed_by_another_key(tmp_path):
    """The grant claims Chef's pinned fingerprint but mallory signed it."""
    from tests.cab_grants import envelope, key

    mgr = ITILManager(tmp_path)
    chg = mgr.propose_change(title="cli forged signature", managed_by="lumina")
    envelope(chg.id)  # pins chef
    env = envelope(chg.id, signer="mallory", claimed_fingerprint=key("chef").fingerprint)
    path = tmp_path / "grant.json"
    path.write_text(json.dumps(env), encoding="utf-8")
    with patch("skcapstone.cli.itil.SHARED_ROOT", str(tmp_path)):
        result = CliRunner().invoke(
            _cli(),
            [
                "itil",
                "cab",
                "vote",
                chg.id,
                "--authorization",
                str(path),
                "--target",
                env["target"],
                "--scope",
                env["scope"],
            ],
        )
    assert result.exit_code != 0
    assert mgr.get_cab_votes(chg.id) == []
