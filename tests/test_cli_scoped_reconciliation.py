"""The CLI must preserve explicit reconciliation boundaries."""

from unittest.mock import patch

import pytest
from click.testing import CliRunner

from skcapstone.cli import main


@pytest.mark.parametrize("repair", [False, True])
def test_exact_task_scope_reaches_native_lifecycle(tmp_path, repair):
    with (
        patch("skcapstone.jarvis_emergency.authorize_coord_mutation"),
        patch("skcoord.lifecycle.audit_lifecycle") as audit,
        patch("skcoord.lifecycle.repair_lifecycle") as fix,
    ):
        audit.return_value.to_dict.return_value = {"clean": True}
        fix.return_value.to_dict.return_value = {"after": {"clean": True}}
        fix.return_value.receipt_path = tmp_path / "receipt"
        args = ["coord", "reconcile-agents", "--home", str(tmp_path)]
        if repair:
            args.append("--repair")
        args += ["--task-id", "026a08d9", "--task-id", "84a113a1"]
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output
        called, unused = (fix, audit) if repair else (audit, fix)
        assert called.call_args.kwargs["task_ids"] == {"026a08d9", "84a113a1"}
        unused.assert_not_called()


def test_unscoped_audit_retains_default(tmp_path):
    with patch("skcoord.lifecycle.audit_lifecycle") as audit:
        audit.return_value.to_dict.return_value = {"clean": True}
        result = CliRunner().invoke(main, ["coord", "reconcile-agents", "--home", str(tmp_path)])
        assert result.exit_code == 0, result.output
        assert audit.call_args.kwargs["task_ids"] is None


@pytest.mark.parametrize("task_id", ["../escape", "bad/name", ""])
def test_invalid_scope_never_calls_native_repair(tmp_path, task_id):
    with (
        patch("skcapstone.jarvis_emergency.authorize_coord_mutation"),
        patch("skcoord.lifecycle.audit_lifecycle") as audit,
        patch("skcoord.lifecycle.repair_lifecycle") as fix,
    ):
        result = CliRunner().invoke(
            main,
            [
                "coord",
                "reconcile-agents",
                "--home",
                str(tmp_path),
                "--repair",
                "--task-id",
                task_id,
            ],
        )
        assert result.exit_code != 0
        audit.assert_not_called()
        fix.assert_not_called()
