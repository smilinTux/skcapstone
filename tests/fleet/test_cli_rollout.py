"""``skcapstone fleet rollout`` (staged-rollout Task 4, spec A.4).

Follows the same shape ``test_cli_node_drift.py`` established for `node
drift`: mock the modules this command consumes (``deployment_manifest`` and,
here, ``staged_rollout`` itself) to test the CLI's OWN responsibilities --
option resolution, rendering, the dry-run default, --apply, --json, and
--strict -- not re-prove ``plan_rollout``/``execute_rollout``'s own
correctness (that is ``test_staged_rollout.py``'s job).

The one thing this command must get right that no unit test of
``staged_rollout`` can check on its own: ``--apply`` is required before
anything executes, and its absence is the DEFAULT, not an opt-in a caller
has to remember.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.fleet import deployment_manifest, staged_rollout  # noqa: E402
from skcapstone.fleet.cli import fleet  # noqa: E402
from skcapstone.fleet.rollout_drift import Drift  # noqa: E402
from skcapstone.fleet.staged_rollout import NodeResult, RolloutResult  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env() -> dict:
    return {"SKFLEET_NODE": "node-under-test"}


@pytest.fixture
def fake_manifest(monkeypatch):
    """Stub build_manifest, and record how the CLI called it."""
    calls: dict = {}

    def fake_build_manifest(repo_root, home):
        calls["build_manifest"] = (Path(repo_root), Path(home))
        return {"git_sha": "abc12345", "units": []}

    monkeypatch.setattr(deployment_manifest, "build_manifest", fake_build_manifest)
    return calls


def _dry_run_result(nodes: tuple[str, ...]) -> RolloutResult:
    completed = tuple(
        NodeResult(
            node=n,
            dry_run=True,
            deployed=False,
            ready=False,
            drift=(),
            detail=f"dry run: would record deployment, then deploy on {n}; not executed",
        )
        for n in nodes
    )
    return RolloutResult(
        dry_run=True, completed=completed, halted_at=None, reason=None, remaining=()
    )


def _halted_result(completed_nodes, halted_at, remaining) -> RolloutResult:
    completed = tuple(
        NodeResult(node=n, dry_run=False, deployed=True, ready=True, drift=(), detail=f"{n}: ok")
        for n in completed_nodes
    )
    return RolloutResult(
        dry_run=False,
        completed=completed,
        halted_at=halted_at,
        reason=f"gate failed on {halted_at} after deploy: not ready",
        remaining=tuple(remaining),
    )


# --------------------------------------------------------------- dry run ---


def test_dry_run_is_the_default_and_calls_execute_rollout_with_dry_run_true(
    tmp_path, monkeypatch, fake_manifest
):
    seen: dict = {}

    def fake_execute_rollout(plan, **kwargs):
        seen["dry_run"] = kwargs["dry_run"]
        seen["plan"] = plan
        return _dry_run_result(plan.nodes)

    monkeypatch.setattr(staged_rollout, "execute_rollout", fake_execute_rollout)

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--repo-root",
            str(tmp_path),
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


def test_apply_flag_is_required_to_execute_for_real(tmp_path, monkeypatch, fake_manifest):
    seen: dict = {}

    def fake_execute_rollout(plan, **kwargs):
        seen["dry_run"] = kwargs["dry_run"]
        return (
            _dry_run_result(plan.nodes)
            if kwargs["dry_run"]
            else _halted_result(plan.nodes, None, ())
        )

    monkeypatch.setattr(staged_rollout, "execute_rollout", fake_execute_rollout)

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--apply",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert seen["dry_run"] is False


def test_dry_run_output_lists_every_node_in_order(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(
        staged_rollout, "execute_rollout", lambda plan, **kwargs: _dry_run_result(plan.nodes)
    )

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap03",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    out = result.output
    # Ordering: chiap03 line appears before chiap01, which appears before chiap02.
    assert out.index("chiap03") < out.index("chiap01") < out.index("chiap02")
    assert "[1/3]" in out and "[2/3]" in out and "[3/3]" in out


# ------------------------------------------------------------------ halt ---


def test_halted_rollout_reports_the_failing_node_and_untouched_remainder(
    tmp_path, monkeypatch, fake_manifest
):
    monkeypatch.setattr(
        staged_rollout,
        "execute_rollout",
        lambda plan, **kwargs: _halted_result(["chiap01"], "chiap02", ["chiap03", "chiap04"]),
    )

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--node",
            "chiap03",
            "--node",
            "chiap04",
            "--apply",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output  # no --strict: exit 0
    assert "HALTED" in result.output
    assert "chiap02" in result.output
    assert "chiap03" in result.output
    assert "chiap04" in result.output
    assert "not attempted" in result.output


def test_strict_exits_one_when_rollout_halts(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(
        staged_rollout,
        "execute_rollout",
        lambda plan, **kwargs: _halted_result(["chiap01"], "chiap02", ["chiap03"]),
    )

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--node",
            "chiap03",
            "--strict",
            "--apply",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 1


def test_strict_exits_zero_on_a_completed_rollout(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(
        staged_rollout,
        "execute_rollout",
        lambda plan, **kwargs: RolloutResult(
            dry_run=False,
            completed=(
                NodeResult(
                    node="chiap01", dry_run=False, deployed=True, ready=True, drift=(), detail="ok"
                ),
            ),
            halted_at=None,
            reason=None,
            remaining=(),
        ),
    )

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--strict",
            "--apply",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output


def test_strict_is_a_no_op_on_a_dry_run_since_a_dry_run_never_halts(
    tmp_path, monkeypatch, fake_manifest
):
    monkeypatch.setattr(
        staged_rollout, "execute_rollout", lambda plan, **kwargs: _dry_run_result(plan.nodes)
    )

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--strict",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output


# --------------------------------------------------------------- --json ---


def test_json_output_carries_the_full_result_shape(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(
        staged_rollout,
        "execute_rollout",
        lambda plan, **kwargs: RolloutResult(
            dry_run=False,
            completed=(
                NodeResult(
                    node="chiap01",
                    dry_run=False,
                    deployed=True,
                    ready=True,
                    drift=(Drift("unit:x.service", "changed", "a", "b", "chiap01"),),
                    detail="chiap01: deployed and gate passed",
                ),
            ),
            halted_at="chiap02",
            reason="gate failed on chiap02 after deploy: not ready",
            remaining=("chiap03",),
        ),
    )

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--node",
            "chiap03",
            "--apply",
            "--json",
            "--repo-root",
            str(tmp_path),
            "--home",
            str(tmp_path),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is False
    assert payload["halted_at"] == "chiap02"
    assert payload["remaining"] == ["chiap03"]
    assert payload["completed"][0]["node"] == "chiap01"
    assert payload["completed"][0]["drift"] == [
        {"artifact": "unit:x.service", "kind": "changed", "expected": "a", "found": "b"}
    ]


# --------------------------------------------------------------- errors ---


def test_an_invalid_node_name_is_a_clean_cli_error_not_a_traceback(tmp_path, fake_manifest):
    result = CliRunner().invoke(
        fleet,
        ["rollout", "--node", "../etc", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code != 0
    assert not isinstance(result.exception, ValueError)


def test_a_build_manifest_error_is_a_clean_cli_error_not_a_traceback(tmp_path, monkeypatch):
    def boom(repo_root, home):
        raise RuntimeError("could not resolve the git HEAD")

    monkeypatch.setattr(deployment_manifest, "build_manifest", boom)

    result = CliRunner().invoke(
        fleet,
        ["rollout", "--node", "chiap01", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code != 0
    assert not isinstance(result.exception, RuntimeError)
    assert "could not resolve the git HEAD" in result.output


def test_no_node_given_is_a_clean_usage_error(tmp_path, fake_manifest):
    result = CliRunner().invoke(
        fleet, ["rollout", "--repo-root", str(tmp_path), "--home", str(tmp_path)], env=_env()
    )

    assert result.exit_code != 0
    assert "node" in result.output.lower()


# --------------------------------------------------------- real pipeline ---


def test_real_dry_run_against_this_checkout_writes_nothing(tmp_path):
    """No mocking: exercises the real build_manifest + plan_rollout +
    execute_rollout pipeline through the CLI, against this real checkout,
    to prove the wiring works and a dry run truly touches no filesystem
    state under --home.
    """
    home = tmp_path / "home"
    home.mkdir()
    before = sorted(home.rglob("*"))

    result = CliRunner().invoke(
        fleet,
        [
            "rollout",
            "--node",
            "chiap01",
            "--node",
            "chiap02",
            "--repo-root",
            str(REPO_ROOT),
            "--home",
            str(home),
        ],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "DRY RUN" in result.output
    assert "chiap01" in result.output
    assert "chiap02" in result.output
    assert sorted(home.rglob("*")) == before
