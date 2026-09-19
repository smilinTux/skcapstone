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

import shlex

import json
from pathlib import Path

import pytest

from skcapstone.fleet.paths import paths_for_home
from skcapstone.fleet.rollout_drift import Drift
from skcapstone.fleet.rollout_history import previous_manifest, record_deployment
from skcapstone.fleet.staged_rollout import (
    DeployOutcome,
    GateOutcome,
    NodeResult,
    RollbackResult,
    default_deploy_node,
    default_gate_node,
    default_rollback_deploy_node,
    execute_rollback,
    execute_rollout,
    plan_rollback,
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


def test_default_gate_node_fails_closed_on_malformed_remote_drift_entry(home: Path) -> None:
    """A remote node on an older/newer drift-report schema must fail the
    gate cleanly, not raise a ``KeyError`` out of ``execute_rollout``: an
    uncaught exception there would abort the whole rollout with no
    ``RolloutResult`` at all, so a caller could never learn which later
    nodes were provably untouched.
    """
    _write_verdict(home, "chiap01", ready=True)

    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            # Missing the required "kind"/"expected" fields entirely.
            stdout = json.dumps(
                {"node": "node-chiap01", "git_sha": "deadbeef", "drifts": [{"artifact": "x"}]}
            )
            stderr = ""

        return _Result()

    outcome = default_gate_node("chiap01", _manifest(), home=home, runner=fake_runner)
    assert outcome.ready is False
    assert outcome.drift == ()
    assert outcome.reason  # a real reason, not a swallowed traceback


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


# ==========================================================================
# rollback: returning a node to the manifest rollout_history recorded
# before its last change (staged-rollout Task 3)
# ==========================================================================


def _lookup_runner(manifest: dict | None):
    """A fake ``runner`` that answers only the remote ``previous_manifest``
    lookup command with ``manifest`` (or JSON ``null``), regardless of
    node. Used whenever a test injects its own ``rollback``/``gate`` fakes,
    so the only real thing ``execute_rollback`` still does for each node is
    the lookup itself.
    """

    def runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = json.dumps(manifest)
            stderr = ""

        assert "previous_manifest" in " ".join(cmd)
        return _Result()

    return runner


# --- plan_rollback: deterministic order, same validation as plan_rollout --


def test_plan_rollback_visits_nodes_in_the_given_order() -> None:
    plan = plan_rollback(["chiap02", "chiap01", "chiap03"])
    assert plan.nodes == ("chiap02", "chiap01", "chiap03")


def test_plan_rollback_rejects_an_unsafe_node_name() -> None:
    with pytest.raises(ValueError):
        plan_rollback(["../etc"])


# --- rollback carries no manifest: it is looked up per node -------------


def test_rollback_plan_has_no_manifest_field() -> None:
    plan = plan_rollback(["chiap01"])
    assert not hasattr(plan, "manifest")


# --- dry_run defaults to True and mutates nothing ------------------------


def test_rollback_dry_run_defaults_to_true(home: Path) -> None:
    plan = plan_rollback(["chiap01"])
    calls: list[str] = []

    def fake_rollback(node: str, manifest: dict) -> DeployOutcome:
        calls.append(node)
        return DeployOutcome(ok=True, step=None, reason=None)

    # dry_run is omitted entirely: the default must still be a preview.
    execute_rollback(plan, home=home, rollback=fake_rollback)

    assert calls == []


def test_rollback_dry_run_never_calls_rollback_or_gate_or_looks_up_anything(
    home: Path,
) -> None:
    """A dry run makes zero calls, including the previous-manifest lookup
    itself: for a remote node that lookup is an ssh round trip, and a dry
    run must never make a network call any more than a live deploy dry run
    does.
    """
    plan = plan_rollback(["chiap01", "chiap02"])
    rollback_calls: list[str] = []
    gate_calls: list[str] = []

    def fake_rollback(node: str, manifest: dict) -> DeployOutcome:
        rollback_calls.append(node)
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        return GateOutcome(ready=True, drift=(), reason="ok")

    def exploding_runner(cmd: list[str]):
        raise AssertionError(f"dry run must never shell out, got {cmd!r}")

    result = execute_rollback(
        plan,
        dry_run=True,
        home=home,
        rollback=fake_rollback,
        gate=fake_gate,
        runner=exploding_runner,
    )

    assert rollback_calls == []
    assert gate_calls == []
    assert result.dry_run is True
    assert result.halted_at is None
    assert [n.node for n in result.completed] == ["chiap01", "chiap02"]
    assert all(not n.deployed for n in result.completed)


def test_rollback_dry_run_writes_no_rollout_history(home: Path) -> None:
    plan = plan_rollback(["chiap01"])

    execute_rollback(plan, dry_run=True, home=home)

    history = home / ".skcapstone" / "fleet" / "status"
    assert not history.exists()


# --- no recorded previous manifest refuses clearly, naming the node ------


def test_rollback_refuses_clearly_when_local_node_has_no_recorded_previous_manifest(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """This machine IS the target node and its history is empty: rollback
    must refuse rather than guess or reconstruct a "previous" state.
    """
    monkeypatch.setenv("SKFLEET_NODE", "node-chiap01")
    plan = plan_rollback(["chiap01"])
    rollback_calls: list[str] = []
    gate_calls: list[str] = []

    def fake_rollback(node: str, manifest: dict) -> DeployOutcome:
        rollback_calls.append(node)
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollback(
        plan, dry_run=False, home=home, rollback=fake_rollback, gate=fake_gate
    )

    assert result.halted_at == "chiap01"
    assert "chiap01" in result.reason
    assert "no recorded previous manifest" in result.reason
    assert rollback_calls == []  # refused before ever attempting a rollback
    assert gate_calls == []
    assert result.completed == ()
    assert result.remaining == ()


def test_rollback_refuses_clearly_when_local_node_has_only_one_recorded_entry(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One recorded deployment and nothing before it: still no previous."""
    monkeypatch.setenv("SKFLEET_NODE", "node-chiap01")
    record_deployment(home, _manifest("rev-1"))
    plan = plan_rollback(["chiap01"])

    result = execute_rollback(
        plan,
        dry_run=False,
        home=home,
        rollback=lambda n, m: DeployOutcome(ok=True, step=None, reason=None),
        gate=lambda n, m: GateOutcome(ready=True, drift=(), reason="ok"),
    )

    assert result.halted_at == "chiap01"
    assert "no recorded previous manifest" in result.reason


def test_rollback_refuses_clearly_when_remote_node_reports_no_previous_manifest(
    home: Path,
) -> None:
    plan = plan_rollback(["chiap02"])

    result = execute_rollback(
        plan,
        dry_run=False,
        home=home,
        runner=_lookup_runner(None),
        rollback=lambda n, m: DeployOutcome(ok=True, step=None, reason=None),
        gate=lambda n, m: GateOutcome(ready=True, drift=(), reason="ok"),
    )

    assert result.halted_at == "chiap02"
    assert "chiap02" in result.reason
    assert "no recorded previous manifest" in result.reason


# --- halt on first failure, same rule as the forward path ----------------


def test_rollback_failure_at_node_two_of_four_halts_and_leaves_the_rest_untouched(
    home: Path,
) -> None:
    plan = plan_rollback(["chiap01", "chiap02", "chiap03", "chiap08"])
    rollback_calls: list[str] = []
    gate_calls: list[str] = []

    def fake_rollback(node: str, manifest: dict) -> DeployOutcome:
        rollback_calls.append(node)
        if node == "chiap02":
            return DeployOutcome(
                ok=False, step="pip_install", reason="ImportError: broken rollback"
            )
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollback(
        plan,
        dry_run=False,
        home=home,
        runner=_lookup_runner(_manifest("rev-1")),
        rollback=fake_rollback,
        gate=fake_gate,
    )

    assert rollback_calls == ["chiap01", "chiap02"]
    assert gate_calls == ["chiap01"]
    assert result.halted_at == "chiap02"
    assert "pip_install" in result.reason
    assert [n.node for n in result.completed] == ["chiap01"]
    assert result.remaining == ("chiap03", "chiap08")


def test_rollback_all_nodes_succeed_leaves_nothing_halted(home: Path) -> None:
    plan = plan_rollback(["chiap01", "chiap02"])

    result = execute_rollback(
        plan,
        dry_run=False,
        home=home,
        runner=_lookup_runner(_manifest("rev-1")),
        rollback=lambda n, m: DeployOutcome(ok=True, step=None, reason=None),
        gate=lambda n, m: GateOutcome(ready=True, drift=(), reason="ok"),
    )

    assert result.halted_at is None
    assert result.reason is None
    assert result.remaining == ()
    assert [n.node for n in result.completed] == ["chiap01", "chiap02"]
    assert all(n.deployed and n.ready for n in result.completed)


# --- gating decision: rollback re-gates each node, same as the forward path -


def test_rollback_gates_every_successfully_rolled_back_node(home: Path) -> None:
    """The gate decision this task makes: rollback re-runs the gate after
    each node, exactly like the forward path, so a rollback can never leave
    a node silently unverified.
    """
    plan = plan_rollback(["chiap01", "chiap02", "chiap03"])
    gate_calls: list[str] = []

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        return GateOutcome(ready=True, drift=(), reason="ok")

    execute_rollback(
        plan,
        dry_run=False,
        home=home,
        runner=_lookup_runner(_manifest("rev-1")),
        rollback=lambda n, m: DeployOutcome(ok=True, step=None, reason=None),
        gate=fake_gate,
    )

    assert gate_calls == ["chiap01", "chiap02", "chiap03"]


def test_rollback_gate_failure_after_successful_rollback_halts_and_labels_it_as_rollback(
    home: Path,
) -> None:
    plan = plan_rollback(["chiap01", "chiap02", "chiap03"])
    gate_calls: list[str] = []

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gate_calls.append(node)
        if node == "chiap02":
            return GateOutcome(
                ready=False,
                drift=(Drift("dispatcher:skfleet-rotate.py", "changed", "aaa", "bbb", node),),
                reason="unambiguous drift: changed:dispatcher:skfleet-rotate.py",
            )
        return GateOutcome(ready=True, drift=(), reason="ok")

    result = execute_rollback(
        plan,
        dry_run=False,
        home=home,
        runner=_lookup_runner(_manifest("rev-1")),
        rollback=lambda n, m: DeployOutcome(ok=True, step=None, reason=None),
        gate=fake_gate,
    )

    assert result.halted_at == "chiap02"
    # The reason names this as a rollback-time gate failure, not a forward
    # deploy failure, so an operator reading it does not misdiagnose which
    # path they are recovering from.
    assert "rollback" in result.reason
    assert "drift" in result.reason
    assert gate_calls == ["chiap01", "chiap02"]  # chiap03's gate never ran
    assert result.remaining == ("chiap03",)
    # chiap02 itself already rolled back (that already happened); it is
    # simply not marked completed/ready, and nothing later was touched.
    assert [n.node for n in result.completed] == ["chiap01"]


# --- the manifest each node is returned to is the one rollout_history ----
# --- actually recorded, never a recomputed guess --------------------------


def test_rollback_target_manifest_is_read_from_history_not_recomputed(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Seed two real, distinct recorded deployments on the local node, then
    roll back for real (local lookup) and confirm the exact manifest handed
    to ``rollback`` is byte-for-byte the FIRST one recorded -- the one
    ``previous_manifest`` itself reports -- not some other value assembled
    from bits of the current manifest.
    """
    monkeypatch.setenv("SKFLEET_NODE", "node-chiap01")
    first = _manifest("rev-1")
    second = _manifest("rev-2")
    record_deployment(home, first)
    record_deployment(home, second)
    assert previous_manifest(home) == first  # sanity: this is what history says

    plan = plan_rollback(["chiap01"])
    seen: list[dict] = []

    def fake_rollback(node: str, manifest: dict) -> DeployOutcome:
        seen.append(manifest)
        return DeployOutcome(ok=True, step=None, reason=None)

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        assert manifest == first
        return GateOutcome(ready=True, drift=(), reason="ok")

    execute_rollback(plan, dry_run=False, home=home, rollback=fake_rollback, gate=fake_gate)

    assert seen == [first]
    assert seen[0] != second


def test_rollback_records_the_target_manifest_as_the_newest_history_entry(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rolling back is itself a deployment: it must be recorded, so a
    second rollback later still has a real trail to read, not a gap.

    The recorded entry is marked internally (``kind="rollback"``), so it
    is not byte-for-byte ``first`` again -- see
    ``test_second_rollback_does_not_redeploy_the_manifest_the_first_one_escaped``
    below for the behavior that marker exists to protect: this test only
    checks that the append happened and carries the right manifest content,
    not the storage shape of the marker itself.
    """
    monkeypatch.setenv("SKFLEET_NODE", "node-chiap01")
    first = _manifest("rev-1")
    second = _manifest("rev-2")
    record_deployment(home, first)
    record_deployment(home, second)

    from skcapstone.fleet.rollout_history import _history_path, _valid_entries

    outcome = default_rollback_deploy_node(
        "chiap01",
        first,
        home=home,
        runner=lambda cmd: type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    assert outcome.ok is True
    entries = _valid_entries(_history_path(home))
    assert len(entries) == 3
    assert entries[0] == first
    assert entries[1] == second
    assert entries[2] != first  # marked, not a plain re-recording of first
    assert entries[2]["revision"] == first["revision"]
    assert entries[2]["git_sha"] == first["git_sha"]


def test_second_rollback_does_not_redeploy_the_manifest_the_first_one_escaped(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect this fix closes, reproduced end to end through
    ``execute_rollback`` (not just ``rollout_history`` in isolation).

    After ``[v1, v2-bad]``, rolling back once correctly targets v1. But
    that rollback is itself recorded, so the history becomes
    ``[v1, v2-bad, v1]``. Before this fix, a second rollback read
    ``entries[-2]`` naively and resolved to v2-bad -- silently redeploying
    exactly the manifest the first rollback escaped. This is reachable on
    the documented recovery path, not by misuse: a rollback that halts at
    a later node's gate, gets re-run over the SAME node list once the
    operator fixes the problem, re-rolls-back every node already
    completed, including this one -- precisely the moment someone is under
    pressure and least able to notice a wrong target.
    """
    monkeypatch.setenv("SKFLEET_NODE", "node-chiap01")
    v1 = _manifest("rev-1")
    v2_bad = _manifest("rev-2-bad")
    record_deployment(home, v1)
    record_deployment(home, v2_bad)

    def fake_runner(cmd: list[str]):
        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    gated: list[dict] = []

    def fake_gate(node: str, manifest: dict) -> GateOutcome:
        gated.append(manifest)
        return GateOutcome(ready=True, drift=(), reason="ok")

    plan = plan_rollback(["chiap01"])

    first_rollback = execute_rollback(
        plan, dry_run=False, home=home, runner=fake_runner, gate=fake_gate
    )
    assert first_rollback.halted_at is None
    assert gated == [v1]  # first rollback correctly targets v1

    second_rollback = execute_rollback(
        plan, dry_run=False, home=home, runner=fake_runner, gate=fake_gate
    )
    assert second_rollback.halted_at is None
    # The behaviour under test: the SECOND rollback must still target v1,
    # never v2-bad -- the manifest the first rollback escaped.
    assert gated == [v1, v1]
    assert v2_bad not in gated


# --- default_rollback_deploy_node: record first, then checkout the sha ---


def test_default_rollback_deploy_node_records_before_the_change_locally(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    outcome = default_rollback_deploy_node("chiap01", manifest, home=home, runner=fake_runner)

    assert outcome.ok is True
    from skcapstone.fleet.rollout_history import _history_path, _valid_entries

    entries = _valid_entries(_history_path(home))
    assert len(entries) == 1
    # Recorded with the rollback marker (see previous_manifest's docstring
    # for why), so the entry is not byte-for-byte the manifest that went
    # in -- but every manifest field survives unchanged.
    assert entries[0]["revision"] == manifest["revision"]
    assert entries[0]["git_sha"] == manifest["git_sha"]
    assert len(calls) == 4  # checkout, pip install, copy, converge


def test_default_rollback_deploy_node_checks_out_the_manifests_git_sha(home: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    manifest = _manifest("rev-1")
    default_rollback_deploy_node("chiap03", manifest, home=home, runner=fake_runner)

    joined = [" ".join(c) for c in calls]
    checkout_index = next(i for i, c in enumerate(joined) if "checkout" in c)
    assert manifest["git_sha"] in joined[checkout_index]
    pip_index = next(i for i, c in enumerate(joined) if "pip" in c and "install" in c)
    copy_index = next(i for i, c in enumerate(joined) if "skfleet-rotate.py" in c and "cp" in c)
    converge_index = next(i for i, c in enumerate(joined) if "sknoded" in c)
    assert checkout_index < pip_index < copy_index < converge_index


def test_default_rollback_deploy_node_records_before_change_remotely(home: Path) -> None:
    calls: list[list[str]] = []

    def fake_runner(cmd: list[str]):
        calls.append(cmd)

        class _Result:
            returncode = 0
            stdout = ""
            stderr = ""

        return _Result()

    manifest = _manifest("rev-2")
    outcome = default_rollback_deploy_node("chiap02", manifest, home=home, runner=fake_runner)

    assert outcome.ok is True
    assert len(calls) == 5  # record, checkout, pip install, copy, converge
    first_cmd = " ".join(calls[0])
    assert "ssh" in calls[0]
    assert "record_deployment" in first_cmd
    second_cmd = " ".join(calls[1])
    assert "git" in second_cmd and "checkout" in second_cmd


def test_default_rollback_deploy_node_stops_at_the_first_failing_step(home: Path) -> None:
    def fake_runner(cmd: list[str]):
        class _Result:
            pass

        result = _Result()
        joined = " ".join(cmd)
        if "pip" in joined and "install" in joined:
            result.returncode = 1
            result.stdout = ""
            result.stderr = "pip install failed during rollback"
        else:
            result.returncode = 0
            result.stdout = ""
            result.stderr = ""
        return result

    outcome = default_rollback_deploy_node("chiap01", _manifest(), home=home, runner=fake_runner)

    assert outcome.ok is False
    assert outcome.step == "pip_install"
    assert "pip install failed during rollback" in outcome.reason


def test_default_rollback_deploy_node_refuses_manifest_with_no_git_sha(home: Path) -> None:
    broken = _manifest()
    del broken["git_sha"]

    def fake_runner(cmd: list[str]):
        raise AssertionError("must never shell out with no usable git_sha")

    outcome = default_rollback_deploy_node("chiap01", broken, home=home, runner=fake_runner)

    assert outcome.ok is False
    assert "git_sha" in outcome.reason


# --- RollbackPlan / RollbackResult are frozen, simple data ---------------


def test_rollback_plan_is_a_frozen_dataclass() -> None:
    plan = plan_rollback(["chiap01"])
    with pytest.raises(Exception):
        plan.nodes = ("other",)  # type: ignore[misc]


def test_rollback_result_is_a_frozen_dataclass() -> None:
    result = RollbackResult(dry_run=True, completed=(), halted_at=None, reason=None, remaining=())
    with pytest.raises(Exception):
        result.dry_run = False  # type: ignore[misc]


def test_ssh_argv_survives_sshs_own_argument_flattening():
    """Regression: the staged rollout never deployed to a remote host.

    `ssh host a b c` does NOT deliver a, b and c as three remote argv
    entries. It joins them with spaces and hands the result to the remote
    login shell to re-parse. Passing the command unquoted meant the remote
    saw:

        bash -lc git -C ~/work/skcapstone pull

    and `bash -lc` takes only its FIRST word as the command string, so it ran
    bare `git` with the path and `pull` landing in $0/$1/$2. Git printed its
    usage and every rollout halted on step one with output that looked
    nothing like the real cause.

    Verified live against chiap03: unquoted returns git's usage text, quoted
    returns the revision. The existing tests could not catch this because
    they substitute a fake runner and never hand the argv to a real ssh.
    """
    from skcapstone.fleet.staged_rollout import _ssh

    argv = _ssh("chiap03", "git -C ~/work/skcapstone pull")
    assert argv[:4] == ["ssh", "chiap03", "bash", "-lc"]

    # The whole command must be ONE word after the remote shell re-parses the
    # joined string. Re-joining and re-splitting the way ssh and the remote
    # shell do is the actual property under test.
    rejoined = " ".join(argv[2:])
    assert shlex.split(rejoined) == [
        "bash",
        "-lc",
        "git -C ~/work/skcapstone pull",
    ], f"remote would re-parse as {shlex.split(rejoined)!r}"


def test_every_deploy_step_survives_the_same_flattening():
    """Not just git_pull: `cd X && pip install -e .` and the cp step contain
    spaces and shell operators too."""
    from skcapstone.fleet.staged_rollout import (
        _DEPLOY_STEPS,
        _ROLLBACK_STEPS,
        DEFAULT_REMOTE_REPO_ROOT,
        _ssh,
    )

    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        for name, template in steps:
            command = template.format(repo=DEFAULT_REMOTE_REPO_ROOT, git_sha="'abc123'")
            argv = _ssh("node", command)
            rejoined = " ".join(argv[2:])
            assert shlex.split(rejoined) == [
                "bash",
                "-lc",
                command,
            ], f"step {name!r} would be mangled: {shlex.split(rejoined)!r}"
