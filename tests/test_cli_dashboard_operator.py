"""CLI tests for `skcapstone dashboard operator ...` (card 90b5b277).

Before this, ``dashboard`` was a plain command with no subcommands at all, so
the declared Operatorapp cli contract (``skcapstone dashboard operator``,
registration.APP_REGISTRY["skdashboard"]["cli"]) was dead: ``... operator
observe`` exited 2 ("unexpected extra argument"), which ATLAS Eyes reported as
a hard cli-error. These tests exercise the real CLI wiring end to end (no
mocked click internals), mirroring `cmdb operator` (cli/cmdb.py).
"""

from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner

from skcapstone.cli import main


def test_dashboard_operator_group_is_registered():
    result = CliRunner().invoke(main, ["dashboard", "--help"])
    assert result.exit_code == 0
    assert "operator" in result.output


def test_dashboard_operator_explain_matches_the_adapter():
    from skcapstone.operator_seat.skdashboard_adapter import skdashboard_explain

    result = CliRunner().invoke(main, ["dashboard", "operator", "explain"])
    assert result.exit_code == 0
    assert json.loads(result.output) == skdashboard_explain()


def test_dashboard_operator_observe_returns_contract_shaped_json():
    result = CliRunner().invoke(main, ["dashboard", "operator", "observe"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    types = {c["type"] for c in payload["conditions"]}
    assert types == {"DashboardReady", "BoardReadable"}
    for cond in payload["conditions"]:
        assert cond["status"] in ("True", "False", "Unknown")


def test_dashboard_operator_act_invokes_the_adapter_and_reports_the_result():
    with patch(
        "skcapstone.operator_seat.skdashboard_adapter.skdashboard_act",
        return_value={"performed": True, "action": "restart-dashboard"},
    ) as act:
        result = CliRunner().invoke(main, ["dashboard", "operator", "act", "restart-dashboard"])
    assert result.exit_code == 0
    assert act.call_count == 1
    _paths, proposal, _classification = act.call_args[0]
    assert proposal["action"] == "restart-dashboard"
    assert json.loads(result.output)["performed"] is True


def test_dashboard_operator_act_rejects_unknown_action():
    result = CliRunner().invoke(main, ["dashboard", "operator", "act", "delete-everything"])
    assert result.exit_code != 0
