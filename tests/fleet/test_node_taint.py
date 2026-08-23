"""The taint setter and the travel-taint loop (card 5e11d880, epic 3bbf39ea).

The scheduler has honored taints since v1, but nothing could ever WRITE one:
`admission.PRESETS` documented a "travel taint applied by runbook" against a
mechanism that did not exist. These tests cover the missing half, end to end:
set the taint through the operator action, then prove the scheduler that
consumes it actually changes its answer.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from click.testing import CliRunner

from skcapstone.fleet import node_controller, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.scheduler import Workload, feasible

NOW = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
TRAVEL = {"key": "travel", "value": "true", "effect": "NoSchedule"}


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


def _beat(paths, node: str, age_s: float = 10) -> None:
    writer = store.Writer(role="sknoded", node=node, identity="")
    ts = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.write_node_file(
        paths,
        writer,
        "heartbeat.json",
        {"kind": "Node", "name": node, "node": node, "ts": ts},
        if_changed=False,
    )
    store.write_node_file(
        paths,
        writer,
        "node.json",
        {
            "kind": "Node",
            "name": node,
            "node": node,
            "observedGeneration": 1,
            "conditions": [],
            "status": {"allocatable": {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0}},
        },
        if_changed=False,
    )


@pytest.fixture
def admitted(paths, operator):
    """One admitted node carrying every other spec field, so the taint setter
    can be proven not to clobber any of them."""
    store.write_spec(
        paths,
        "node",
        "node-41",
        {
            "taints": [],
            "cordoned": False,
            "address": {"hostname": "chi-41"},
            "identity": "capauth:chef@skworld.io",
            "actuate": True,
            "role": "builder-standby",
        },
        writer=operator,
        labels={"heavy-build": "true"},
    )
    _beat(paths, "node-41")
    return paths


def _taints(paths, name: str = "node-41") -> list:
    return store.read_spec(paths, "node", name)["spec"]["taints"]


# ------------------------------------------------------------- set_taint ---


def test_set_taint_writes_the_entry(admitted, operator) -> None:
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    assert _taints(admitted) == [TRAVEL]


def test_set_taint_preserves_every_other_field(admitted, operator) -> None:
    before = store.read_spec(admitted, "node", "node-41")

    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)

    after = store.read_spec(admitted, "node", "node-41")
    for field in ("role", "cordoned", "address", "identity", "actuate"):
        assert after["spec"][field] == before["spec"][field], f"set_taint clobbered {field}"
    assert after["labels"] == {"heavy-build": "true"}


def test_set_taint_bumps_generation_by_exactly_one(admitted, operator) -> None:
    before = store.read_spec(admitted, "node", "node-41")["generation"]
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    after = store.read_spec(admitted, "node", "node-41")["generation"]
    assert after == before + 1


def test_retainting_the_same_key_replaces_rather_than_duplicates(admitted, operator) -> None:
    """A duplicated key would make feasible() depend on list order."""
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    node_controller.set_taint(
        admitted, "node-41", "travel", "packed", "PreferNoSchedule", writer=operator
    )
    assert _taints(admitted) == [
        {"key": "travel", "value": "packed", "effect": "PreferNoSchedule"}
    ]


def test_set_taint_keeps_other_keys_and_their_order(admitted, operator) -> None:
    node_controller.set_taint(
        admitted, "node-41", "dedicated", "model-serving", "NoSchedule", writer=operator
    )
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    node_controller.set_taint(
        admitted, "node-41", "dedicated", "builds", "NoSchedule", writer=operator
    )
    assert [t["key"] for t in _taints(admitted)] == ["dedicated", "travel"]
    assert _taints(admitted)[0]["value"] == "builds"


def test_set_taint_is_write_on_change(admitted, operator) -> None:
    """Re-asserting an identical taint must not churn the generation: this
    tree is a live Syncthing folder and a runbook re-runs on every suspend."""
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    gen = store.read_spec(admitted, "node", "node-41")["generation"]
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    assert store.read_spec(admitted, "node", "node-41")["generation"] == gen


def test_set_taint_on_a_node_with_no_taints_key(paths, operator) -> None:
    """A pre-taint node object has no `taints` key at all, not an empty list."""
    store.write_spec(paths, "node", "node-legacy", {"cordoned": False}, writer=operator)
    node_controller.set_taint(
        paths, "node-legacy", "travel", "true", "NoSchedule", writer=operator
    )
    assert _taints(paths, "node-legacy") == [TRAVEL]


def test_set_taint_on_an_unknown_node_raises(paths, operator) -> None:
    with pytest.raises(LookupError, match="no such node object"):
        node_controller.set_taint(
            paths, "node-ghost", "travel", "true", "NoSchedule", writer=operator
        )


@pytest.mark.parametrize("bad", ["NoExecute", "noschedule", "", "NoSchedule "])
def test_set_taint_rejects_effects_the_scheduler_does_not_honor(admitted, operator, bad) -> None:
    """NoExecute is the dangerous one: nothing here evicts a running
    workload, so accepting it would write policy that silently does nothing."""
    with pytest.raises(ValueError, match="invalid taint effect"):
        node_controller.set_taint(admitted, "node-41", "travel", "true", bad, writer=operator)


@pytest.mark.parametrize("bad", ["../escape", "/abs", "_hidden", ""])
def test_set_taint_rejects_unsafe_keys(admitted, operator, bad: str) -> None:
    with pytest.raises(ValueError, match="invalid taint key"):
        node_controller.set_taint(admitted, "node-41", bad, "true", "NoSchedule", writer=operator)


def test_the_honored_effects_are_exactly_what_the_scheduler_implements() -> None:
    assert node_controller.TAINT_EFFECTS == ("NoSchedule", "PreferNoSchedule")


# ----------------------------------------------------------- clear_taint ---


def test_clear_taint_removes_the_entry_and_bumps_generation(admitted, operator) -> None:
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    gen = store.read_spec(admitted, "node", "node-41")["generation"]

    node_controller.clear_taint(admitted, "node-41", "travel", writer=operator)

    assert _taints(admitted) == []
    assert store.read_spec(admitted, "node", "node-41")["generation"] == gen + 1


def test_clear_taint_leaves_the_other_taints_alone(admitted, operator) -> None:
    node_controller.set_taint(
        admitted, "node-41", "dedicated", "model-serving", "NoSchedule", writer=operator
    )
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    node_controller.clear_taint(admitted, "node-41", "travel", writer=operator)
    assert _taints(admitted) == [
        {"key": "dedicated", "value": "model-serving", "effect": "NoSchedule"}
    ]


def test_clear_taint_of_an_absent_key_is_a_silent_noop(admitted, operator) -> None:
    """The runbook resume path clears unconditionally; it must not fail and
    it must not bump the generation on a node that never travelled."""
    gen = store.read_spec(admitted, "node", "node-41")["generation"]
    node_controller.clear_taint(admitted, "node-41", "travel", writer=operator)
    assert store.read_spec(admitted, "node", "node-41")["generation"] == gen


def test_clear_taint_on_an_unknown_node_raises(paths, operator) -> None:
    with pytest.raises(LookupError, match="no such node object"):
        node_controller.clear_taint(paths, "node-ghost", "travel", writer=operator)


# ------------------------------------------------------- scheduler loop ---


def _view(paths, name: str = "node-41"):
    return {v.name: v for v in node_controller.node_views(paths, now=NOW)}[name]


def test_a_travel_tainted_node_is_excluded_by_feasible(admitted, operator) -> None:
    workload = Workload(kind="job", name="card-1")
    assert feasible(_view(admitted), workload) is None

    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)

    assert feasible(_view(admitted), workload) == "untolerated NoSchedule taint travel=true"


def test_a_matching_toleration_still_admits_the_travel_tainted_node(admitted, operator) -> None:
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    exact = Workload(kind="job", name="card-1", tolerations=({"key": "travel", "value": "true"},))
    key_only = Workload(kind="job", name="card-1", tolerations=({"key": "travel"},))
    wrong = Workload(kind="job", name="card-1", tolerations=({"key": "travel", "value": "false"},))
    assert feasible(_view(admitted), exact) is None
    assert feasible(_view(admitted), key_only) is None
    assert feasible(_view(admitted), wrong) == "untolerated NoSchedule taint travel=true"


def test_untaint_makes_the_node_schedulable_again(admitted, operator) -> None:
    workload = Workload(kind="job", name="card-1")
    node_controller.set_taint(admitted, "node-41", "travel", "true", "NoSchedule", writer=operator)
    node_controller.clear_taint(admitted, "node-41", "travel", writer=operator)
    assert feasible(_view(admitted), workload) is None


def test_a_prefernoschedule_travel_taint_never_excludes(admitted, operator) -> None:
    """The softer variant is a ranking signal only, so a travelling box stays
    usable as a last resort instead of vanishing from the fleet."""
    node_controller.set_taint(
        admitted, "node-41", "travel", "true", "PreferNoSchedule", writer=operator
    )
    assert feasible(_view(admitted), Workload(kind="job", name="card-1")) is None


# ------------------------------------------------------------------ CLI ---


def test_cli_taint_then_describe_then_untaint(admitted) -> None:
    runner = CliRunner()

    tainted = runner.invoke(
        fleet, ["taint", "node-41", "travel=true:NoSchedule"], env=_env(admitted)
    )
    assert tainted.exit_code == 0, tainted.output
    assert "travel=true:NoSchedule" in tainted.output

    described = runner.invoke(fleet, ["describe", "node", "node-41"], env=_env(admitted))
    payload = json.loads(described.output)["spec"]
    assert payload["spec"]["taints"] == [TRAVEL]
    gen = payload["generation"]

    cleared = runner.invoke(fleet, ["untaint", "node-41", "travel"], env=_env(admitted))
    assert cleared.exit_code == 0, cleared.output

    described = runner.invoke(fleet, ["describe", "node", "node-41"], env=_env(admitted))
    payload = json.loads(described.output)["spec"]
    assert payload["spec"]["taints"] == []
    assert payload["generation"] == gen + 1


def test_cli_untaint_of_an_absent_key_says_so_without_failing(admitted) -> None:
    result = CliRunner().invoke(fleet, ["untaint", "node-41", "travel"], env=_env(admitted))
    assert result.exit_code == 0, result.output
    assert "nothing to do" in result.output


@pytest.mark.parametrize("bad", ["travel", "travel=true", "travel:NoSchedule", "=true:NoSchedule"])
def test_cli_taint_rejects_a_malformed_argument(admitted, bad: str) -> None:
    result = CliRunner().invoke(fleet, ["taint", "node-41", bad], env=_env(admitted))
    assert result.exit_code != 0
    assert "want KEY=VALUE:EFFECT" in result.output


def test_cli_taint_rejects_an_unhonored_effect(admitted) -> None:
    result = CliRunner().invoke(
        fleet, ["taint", "node-41", "travel=true:NoExecute"], env=_env(admitted)
    )
    assert result.exit_code != 0
    assert "invalid taint effect" in result.output


def test_cli_taint_on_a_missing_node_is_a_clean_error(paths) -> None:
    result = CliRunner().invoke(
        fleet, ["taint", "node-ghost", "travel=true:NoSchedule"], env=_env(paths)
    )
    assert result.exit_code != 0
    assert "no such node object" in result.output


def test_cli_untaint_on_a_missing_node_is_a_clean_error(paths) -> None:
    result = CliRunner().invoke(fleet, ["untaint", "node-ghost", "travel"], env=_env(paths))
    assert result.exit_code != 0
    assert "no such node object" in result.output
