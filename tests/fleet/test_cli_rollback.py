"""``skcapstone fleet rollback`` (staged-rollout Task 4, spec A.4).

Same shape as ``test_cli_rollout.py``: mock ``staged_rollout`` itself so this
file tests the CLI's OWN responsibilities (option resolution, rendering,
the dry-run default, --apply, --json, --strict), not
``plan_rollback``/``execute_rollback``'s own correctness, which
``test_staged_rollout.py`` already covers -- including the "no recorded
previous manifest" refusal this command must surface plainly rather than
translate into something friendlier or vaguer.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from skcapstone.fleet import staged_rollout  # noqa: E402
from skcapstone.fleet.cli import fleet  # noqa: E402
from skcapstone.fleet.staged_rollout import NodeResult, RollbackResult  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env() -> dict:
    return {"SKFLEET_NODE": "node-under-test"}


def _dry_run_result(nodes: tuple[str, ...]) -> RollbackResult:
    completed = tuple(
        NodeResult(
            node=n,
            dry_run=True,
            deployed=False,
            ready=False,
            drift=(),
            detail=f"dry run: would look up {n}'s recorded previous manifest; not executed",
        )
        for n in nodes
    )
    return RollbackResult(
        dry_run=True, completed=completed, halted_at=None, reason=None, remaining=()
    )


# --------------------------------------------------------------- dry run ---


def test_dry_run_is_the_default_and_calls_execute_rollback_with_dry_run_true(
    tmp_path, monkeypatch
):
    seen: dict = {}

    def fake_execute_rollback(plan, **kwargs):
        seen["dry_run"] = kwargs["dry_run"]
        seen["plan"] = plan
        return _dry_run_result(plan.nodes)

    monkeypatch.setattr(staged_rollout, "execute_rollback", fake_execute_rollback)

    result = CliRunner().invoke(
        fleet,
        [
            "rollback",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert seen["dry_run"] is True
    assert seen["plan"].nodes == ("chiap01", "chiap02")
    assert "DRY RUN" in result.output
    assert "--apply" in result.output


def test_apply_flag_is_required_to_execute_for_real(tmp_path, monkeypatch):
    seen: dict = {}

    def fake_execute_rollback(plan, **kwargs):
        seen["dry_run"] = kwargs["dry_run"]
        return _dry_run_result(plan.nodes)

    monkeypatch.setattr(staged_rollout, "execute_rollback", fake_execute_rollback)

    CliRunner().invoke(
        fleet, ["rollback", "--node", "chiap01", "--apply", "--home", str(tmp_path)], env=_env()
    )

    assert seen["dry_run"] is False


def test_no_manifest_option_exists_target_is_looked_up_per_node(tmp_path, monkeypatch):
    """Rollback deliberately has no --manifest option: unlike `rollout`, its
    target is never chosen on the command line.
    """
    result = CliRunner().invoke(fleet, ["rollback", "--help"])

    assert result.exit_code == 0, result.output
    options_block = result.output.split("Options:", 1)[1]
    assert "--manifest" not in options_block


# ------------------------------------------------------------------ halt ---


def test_no_recorded_previous_manifest_is_reported_plainly(tmp_path, monkeypatch):
    result_obj = RollbackResult(
        dry_run=False,
        completed=(),
        halted_at="chiap01",
        reason="no recorded previous manifest for chiap01; rollback refuses to guess "
        "or reconstruct one",
        remaining=("chiap02",),
    )
    monkeypatch.setattr(staged_rollout, "execute_rollback", lambda plan, **kwargs: result_obj)

    result = CliRunner().invoke(
        fleet,
        ["rollback", "--node", "chiap01", "--node", "chiap02", "--apply", "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output  # no --strict
    assert "no recorded previous manifest for chiap01" in result.output
    assert "refuses to guess" in result.output
    assert "chiap02" in result.output
    assert "not attempted" in result.output


def test_strict_exits_one_when_rollback_halts(tmp_path, monkeypatch):
    result_obj = RollbackResult(
        dry_run=False,
        completed=(),
        halted_at="chiap01",
        reason="no recorded previous manifest for chiap01",
        remaining=(),
    )
    monkeypatch.setattr(staged_rollout, "execute_rollback", lambda plan, **kwargs: result_obj)

    result = CliRunner().invoke(
        fleet,
        ["rollback", "--node", "chiap01", "--apply", "--strict", "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 1


def test_strict_exits_zero_on_a_completed_rollback(tmp_path, monkeypatch):
    result_obj = RollbackResult(
        dry_run=False,
        completed=(
            NodeResult(
                node="chiap01",
                dry_run=False,
                deployed=True,
                ready=True,
                drift=(),
                detail="chiap01: rolled back to manifest rev-0 and gate passed",
            ),
        ),
        halted_at=None,
        reason=None,
        remaining=(),
    )
    monkeypatch.setattr(staged_rollout, "execute_rollback", lambda plan, **kwargs: result_obj)

    result = CliRunner().invoke(
        fleet,
        ["rollback", "--node", "chiap01", "--apply", "--strict", "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output


# --------------------------------------------------------------- --json ---


def test_json_output_carries_the_full_result_shape(tmp_path, monkeypatch):
    result_obj = RollbackResult(
        dry_run=False,
        completed=(
            NodeResult(
                node="chiap01",
                dry_run=False,
                deployed=True,
                ready=True,
                drift=(),
                detail="chiap01: rolled back to manifest rev-0 and gate passed",
            ),
        ),
        halted_at=None,
        reason=None,
        remaining=(),
    )
    monkeypatch.setattr(staged_rollout, "execute_rollback", lambda plan, **kwargs: result_obj)

    result = CliRunner().invoke(
        fleet,
        ["rollback", "--node", "chiap01", "--apply", "--json", "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["halted_at"] is None
    assert payload["completed"][0]["node"] == "chiap01"


# --------------------------------------------------------------- errors ---


def test_an_invalid_node_name_is_a_clean_cli_error_not_a_traceback(tmp_path):
    result = CliRunner().invoke(
        fleet, ["rollback", "--node", "../etc", "--home", str(tmp_path)], env=_env()
    )

    assert result.exit_code != 0
    assert not isinstance(result.exception, ValueError)


def test_no_node_given_is_a_clean_usage_error(tmp_path):
    result = CliRunner().invoke(fleet, ["rollback", "--home", str(tmp_path)], env=_env())

    assert result.exit_code != 0
    assert "node" in result.output.lower()


# --------------------------------------------------------- real pipeline ---


def test_real_dry_run_against_this_checkout_writes_nothing(tmp_path):
    """No mocking: exercises the real plan_rollback + execute_rollback
    pipeline through the CLI, proving a dry run makes no lookup call (an
    ssh round trip or a local read) and writes nothing under --home.
    """
    home = tmp_path / "home"
    home.mkdir()
    before = sorted(home.rglob("*"))

    result = CliRunner().invoke(
        fleet,
        ["rollback", "--node", "chiap01", "--node", "chiap02", "--home", str(home)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "chiap01" in result.output
    assert "chiap02" in result.output
    assert sorted(home.rglob("*")) == before
