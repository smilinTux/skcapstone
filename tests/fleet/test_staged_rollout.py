"""Tests for staged rollout (spec A.4, staged-rollout Task 2).

Deploy to one node, gate it, and only then proceed. On the first failure,
stop and report which node failed and why, leaving the remaining nodes
provably untouched. These tests hold ``plan_rollout``/``execute_rollout``
to the three rules the brief states: a plan visits nodes in a deterministic
order, ``dry_run`` defaults to True and a dry run mutates nothing, and a
failure at any node halts the rollout rather than continuing past it.

The gate is deliberately not re-invented here: ``default_gate_node`` is
exercised directly (readiness verdict file plus ``detect_drift``) rather
than replaced with a third notion of "healthy" for the tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from skcapstone.fleet.paths import paths_for_home
from skcapstone.fleet.rollout_drift import Drift
from skcapstone.fleet.rollout_history import previous_manifest
from skcapstone.fleet.staged_rollout import (
    DeployOutcome,
    GateOutcome,
    NodeResult,
    default_deploy_node,
    default_gate_node,
    execute_rollout,
    plan_rollout,
)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    """A throwaway estate home, never the real ~/.skcapstone."""
    return tmp_path / "home"


def _manifest(revision: str = "rev-1") -> dict:
    return {
        "revision": revision,
        "git_sha": "deadbeef",
        "package_version": "0.1.0",
        "required_env": ["SKFLEET_TARGET"],
        "units": ["skcapstone.service"],
    }


def _write_verdict(home: Path, node: str, ready: bool) -> None:
    path = paths_for_home(home).status_path(f"node-{node}", "readiness", "verdict")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"ready": ready, "lines": []}), encoding="utf-8")


# --- plan_rollout: deterministic order --------------------------------


def test_plan_visits_nodes_in_the_given_order() -> None:
    plan = plan_rollout(["chiap02", "chiap01", "chiap03"], _manifest())
    assert plan.nodes == ("chiap02", "chiap01", "chiap03")


def test_plan_order_is_stable_across_repeated_calls() -> None:
    nodes = ["chiap01", "chiap02", "chiap03", "chiap08"]
    first = plan_rollout(nodes, _manifest())
    second = plan_rollout(nodes, _manifest())
    assert first.nodes == second.nodes == tuple(nodes)


def test_plan_rollout_rejects_an_unsafe_node_name() -> None:
    with pytest.raises(ValueError):
        plan_rollout(["../etc"], _manifest())


def test_plan_rollout_carries_the_manifest_unchanged() -> None:
    manifest = _manifest("rev-7")
    plan = plan_rollout(["chiap01"], manifest)
    assert plan.manifest == manifest


# --- dry_run defaults to True and mutates nothing ----------------------


def test_dry_run_defaults_to_true(home: Path) -> None:
    plan = plan_rollout(["chiap01"], _manifest())
    calls: list[str] = []

    def fake_deploy(node: str, manifest: dict) -> DeployOutcome:
        calls.append(node)
        return DeployOutcome(ok=True, step=None, reason=None)

    # dry_run is omitted entirely: the default must still be a preview.
    execute_rollout(plan, home=home, deploy=fake_deploy)

    assert calls == []


def test_dry_run_never_calls_deploy_or_gate(home: Path) -> None:
    plan = plan_rollout(["chiap01", "chiap02"], _manifest())
    deploy_calls: list[str] = []
    gate_calls: list[str] = []

    def fake_deploy(node: str, manifest: dict) -> DeployOutcome:
        deploy_calls.append(node)
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollout(plan, dry_run=True, home=home, deploy=fake_deploy, gate=fake_gate)

    assert deploy_calls == []
    assert gate_calls == []
    assert result.dry_run is True
    assert result.halted_at is None
    assert [n.node for n in result.completed] == ["chiap01", "chiap02"]
    assert all(not n.deployed for n in result.completed)


def test_dry_run_writes_no_rollout_history(home: Path) -> None:
    plan = plan_rollout(["chiap01"], _manifest())

    execute_rollout(plan, dry_run=True, home=home)

    history = home / ".skcapstone" / "fleet" / "status"
    assert not history.exists()


# --- halt on first failure ----------------------------------------------


def test_failure_at_node_two_of_four_halts_and_leaves_the_rest_untouched(
    home: Path,
) -> None:
    plan = plan_rollout(["chiap01", "chiap02", "chiap03", "chiap08"], _manifest())
    deploy_calls: list[str] = []
    gate_calls: list[str] = []

    def fake_deploy(node: str, manifest: dict) -> DeployOutcome:
        deploy_calls.append(node)
        if node == "chiap02":
            return DeployOutcome(
                ok=False,
                step="pip_install",
                reason="ImportError: cannot import name 'GATED_EXIT_CODE'",
            )
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollout(plan, dry_run=False, home=home, deploy=fake_deploy, gate=fake_gate)

    # chiap01 deployed and gated; chiap02 failed; chiap03/chiap08 never ran.
    assert deploy_calls == ["chiap01", "chiap02"]
    assert gate_calls == ["chiap01"]
    assert result.halted_at == "chiap02"
    assert "pip_install" in result.reason
    assert "GATED_EXIT_CODE" in result.reason
    assert [n.node for n in result.completed] == ["chiap01"]
    assert result.remaining == ("chiap03", "chiap08")


def test_gate_failure_after_successful_deploy_also_halts(home: Path) -> None:
    plan = plan_rollout(["chiap01", "chiap02", "chiap03"], _manifest())
    gate_calls: list[str] = []

    def fake_deploy(node: str, manifest: dict) -> DeployOutcome:
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        if node == "chiap02":
            return GateOutcome(
                ready=False,
                drift=(Drift("dispatcher:skfleet-rotate.py", "changed", "aaa", "bbb", node),),
                reason="unambiguous drift: changed:dispatcher:skfleet-rotate.py",
            )
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollout(plan, dry_run=False, home=home, deploy=fake_deploy, gate=fake_gate)

    assert result.halted_at == "chiap02"
    assert "drift" in result.reason
    assert gate_calls == ["chiap01", "chiap02"]  # chiap03's gate never ran
    assert result.remaining == ("chiap03",)


def test_all_nodes_succeed_leaves_nothing_halted(home: Path) -> None:
    plan = plan_rollout(["chiap01", "chiap02"], _manifest())

    def fake_deploy(node: str, manifest: dict) -> DeployOutcome:
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollout(plan, dry_run=False, home=home, deploy=fake_deploy, gate=fake_gate)

    assert result.halted_at is None
    assert result.reason is None
    assert result.remaining == ()
    assert [n.node for n in result.completed] == ["chiap01", "chiap02"]
    assert all(n.deployed and n.ready for n in result.completed)


# --- record_deployment happens before the change, not after ------------


def test_default_deploy_records_before_the_change_locally(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercises the real (non-fake) deploy path for the local node: the
    manifest must land in rollout history before any shell step runs.
    """
    monkeypatch.setenv("SKFLEET_NODE", "node-chiap01")
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    manifest = _manifest("rev-9")
    outcome = default_deploy_node("chiap01", manifest, home=home, runner=fake_runner)

    assert outcome.ok is True
    # record_deployment already ran for real, before any shell step: the
    # manifest is on disk, and every subsequent step was a shell command.
    assert previous_manifest(home) is None  # only one record so far: no previous
    from skcapstone.fleet.rollout_history import _history_path, _valid_entries

    entries = _valid_entries(_history_path(home))
    assert entries == [manifest]
    assert len(calls) == 4  # git pull, pip install, copy, converge -- record was direct


def test_default_deploy_records_before_change_remotely(home: Path) -> None:
    """For a node other than this machine, the record step must still be
    the first thing that reaches the wire, before git pull.
    """
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    manifest = _manifest("rev-2")
    outcome = default_deploy_node("chiap02", manifest, home=home, runner=fake_runner)

    assert outcome.ok is True
    assert len(calls) == 5  # record, git pull, pip install, copy, converge
    first_cmd = " ".join(calls[0])
    assert "ssh" in calls[0]
    assert "record_deployment" in first_cmd
    second_cmd = " ".join(calls[1])
    assert "git" in second_cmd and "pull" in second_cmd


def test_deploy_step_order_is_load_bearing_package_before_script(home: Path) -> None:
    """Package first, then script: the dispatcher script imports a name
    from the package, so a newer script against an older package fails.
    """
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    default_deploy_node("chiap03", _manifest(), home=home, runner=fake_runner)

    joined = [" ".join(c) for c in calls]
    pip_index = next(i for i, c in enumerate(joined) if "pip" in c and "install" in c)
    copy_index = next(i for i, c in enumerate(joined) if "skfleet-rotate.py" in c and "cp" in c)
    assert pip_index < copy_index


def test_deploy_stops_at_the_first_failing_step(home: Path) -> None:
    def fake_runner(cmd: list[str]):
        class _Result:
            pass

        result = _Result()
        joined = " ".join(cmd)
        if "pip" in joined and "install" in joined:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "ImportError: cannot import name 'GATED_EXIT_CODE'"
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    outcome = default_deploy_node("chiap01", _manifest(), home=home, runner=fake_runner)

    assert outcome.ok is False
    assert outcome.step == "pip_install"
    assert "GATED_EXIT_CODE" in outcome.reason


# --- the result names the failing node and the reason -------------------


def test_result_names_the_failing_node_and_the_reason(home: Path) -> None:
    plan = plan_rollout(["chiap01", "chiap02"], _manifest())

    def fake_deploy(node: str, manifest: dict) -> DeployOutcome:
        if node == "chiap02":
            return DeployOutcome(ok=False, step="converge", reason="sknoded --once timed out")
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollout(plan, dry_run=False, home=home, deploy=fake_deploy, gate=fake_gate)

    assert result.halted_at == "chiap02"
    assert "converge" in result.reason
    assert "sknoded --once timed out" in result.reason


# --- default_gate_node: readiness verdict plus detect_drift, no third gate --


def test_default_gate_node_fails_closed_with_no_readiness_verdict(home: Path) -> None:
    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = json.dumps({"node": "node-chiap01", "git_sha": "deadbeef", "drifts": []})
            stderr = ""

        return _Result()

    outcome = default_gate_node("chiap01", _manifest(), home=home, runner=fake_runner)
    assert outcome.ready is False
    assert "verdict" in outcome.reason or "readiness" in outcome.reason


def test_default_gate_node_passes_when_ready_and_drift_empty(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_verdict(home, "chiap01", ready=True)

    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = json.dumps({"node": "node-chiap01", "git_sha": "deadbeef", "drifts": []})
            stderr = ""

        return _Result()

    outcome = default_gate_node("chiap01", _manifest(), home=home, runner=fake_runner)
    assert outcome.ready is True
    assert outcome.drift == ()


def test_default_gate_node_fails_on_unambiguous_drift(home: Path) -> None:
    _write_verdict(home, "chiap01", ready=True)

    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = json.dumps(
                {
                    "node": "node-chiap01",
                    "git_sha": "deadbeef",
                    "drifts": [
                        {
                            "artifact": "dispatcher:skfleet-rotate.py",
                            "kind": "changed",
                            "expected": "aaa",
                            "found": "bbb",
                        }
                    ],
                }
            )
            stderr = ""

        return _Result()

    outcome = default_gate_node("chiap01", _manifest(), home=home, runner=fake_runner)
    assert outcome.ready is False
    assert len(outcome.drift) == 1


def test_default_gate_node_ignores_role_ambiguous_missing_findings(home: Path) -> None:
    """A shipped unit reported 'missing' cannot be told apart from a host
    role that never installs it (see cli.py's _drift_is_role_ambiguous), so
    it must never by itself fail the gate.
    """
    _write_verdict(home, "chiap01", ready=True)

    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = json.dumps(
                {
                    "node": "node-chiap01",
                    "git_sha": "deadbeef",
                    "drifts": [
                        {
                            "artifact": "unit:skfleet-atlas.service",
                            "kind": "missing",
                            "expected": "aaa",
                            "found": None,
                        }
                    ],
                }
            )
            stderr = ""

        return _Result()

    outcome = default_gate_node("chiap01", _manifest(), home=home, runner=fake_runner)
    assert outcome.ready is True
    assert outcome.drift == ()


def test_default_gate_node_fails_closed_when_readiness_not_ready(home: Path) -> None:
    _write_verdict(home, "chiap01", ready=False)

    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = json.dumps({"node": "node-chiap01", "git_sha": "deadbeef", "drifts": []})
            stderr = ""

        return _Result()

    outcome = default_gate_node("chiap01", _manifest(), home=home, runner=fake_runner)
    assert outcome.ready is False


# --- RolloutPlan / NodeResult are frozen, simple data ---------------------


def test_rollout_plan_is_a_frozen_dataclass() -> None:
    plan = plan_rollout(["chiap01"], _manifest())
    with pytest.raises(Exception):
        plan.nodes = ("other",)  # type: ignore[misc]


def test_node_result_is_a_frozen_dataclass() -> None:
    result = NodeResult(
        node="chiap01", dry_run=True, deployed=False, ready=False, drift=(), detail="x"
    )
    with pytest.raises(Exception):
        result.node = "other"  # type: ignore[misc]
