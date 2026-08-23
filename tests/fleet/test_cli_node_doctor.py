"""skfleet node doctor (card 76dad234).

Two properties matter beyond the output shape. The command must write
NOTHING, because it is meant to run everywhere including on the node it is
judging. And an unbound node must be a skip, never a failure, because
"no role yet" is the normal state during rollout.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import store
from skcapstone.fleet.cli import fleet

WORKER_PROFILE = {
    "description": "gpu worker",
    "units": {
        "required": ["skai-beellama.service"],
        "allowed": ["skai-beellama.service"],
        "mustNot": ["skchat-daemon.service"],
    },
    "packages": {"required": [], "allowed": ["skcapstone"], "mustNot": ["skmemory"]},
    "unitsIgnore": ["gpg-agent*.socket"],
    "stateTier": "none",
    "capauthIdentityClass": "worker",
    "syncFolders": ["skfleet-control"],
}


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-under-test"}


def _json_payload(output: str) -> list:
    """Parse the JSON array out of mixed stdout+stderr.

    CliRunner does not separate the streams, so skip notes (written to
    stderr) land ahead of the JSON in `result.output`.
    """
    return json.loads(output[output.index("[") :])


def _snapshot(root):
    return {p: (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*") if p.is_file()}


@pytest.fixture
def fleet_tree(paths, operator, monkeypatch):
    """A node bound to a worker profile, with a drifted published inventory."""
    store.write_spec(paths, "profile", "worker-gpu", WORKER_PROFILE, writer=operator)
    store.write_spec(
        paths,
        "node",
        "node-under-test",
        {"role": "worker-gpu", "cordoned": False},
        writer=operator,
    )
    monkeypatch.setattr(
        "skcapstone.fleet.nodeinventory.collect",
        lambda **kw: {
            "units": {
                "user": {
                    "skchat-daemon.service": "enabled",  # forbidden
                    "gpg-agent.socket": "enabled",  # ignored
                    "extra.service": "enabled",  # unexpected
                }
            },
            "packages": {"skmemory": "1.0"},  # forbidden
            "collectedAt": "2026-08-15T00:00:00Z",
        },
    )
    return paths


# ------------------------------------------------------------------ json ---


def test_json_carries_all_six_categories(fleet_tree) -> None:
    result = CliRunner().invoke(fleet, ["node", "doctor", "--json"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert len(payload) == 1
    report = payload[0]
    for category in (
        "missing_required_units",
        "forbidden_units",
        "unexpected_units",
        "missing_required_packages",
        "forbidden_packages",
        "unexpected_packages",
    ):
        assert category in report
    assert report["node"] == "node-under-test"
    assert report["role"] == "worker-gpu"
    assert report["forbidden_units"] == ["skchat-daemon.service"]
    assert report["forbidden_packages"] == ["skmemory"]
    assert report["unexpected_units"] == ["extra.service"]  # gpg-agent ignored
    assert report["severity"] == "error"


# ------------------------------------------------------------ exit codes ---


def test_exits_zero_with_drift_by_default(fleet_tree) -> None:
    """Report only. Drift is information, not a failure."""
    result = CliRunner().invoke(fleet, ["node", "doctor"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    assert "ERROR" in result.output
    assert "skchat-daemon.service" in result.output


def test_strict_exits_one_on_a_forbidden_finding(fleet_tree) -> None:
    result = CliRunner().invoke(fleet, ["node", "doctor", "--strict"], env=_env(fleet_tree))
    assert result.exit_code == 1


def test_strict_exits_zero_when_only_info_findings(paths, operator, monkeypatch) -> None:
    """--strict gates on error grade only, so a manifest lagging reality
    does not start failing everyone's pipeline."""
    store.write_spec(paths, "profile", "worker-gpu", WORKER_PROFILE, writer=operator)
    store.write_spec(paths, "node", "node-under-test", {"role": "worker-gpu"}, writer=operator)
    monkeypatch.setattr(
        "skcapstone.fleet.nodeinventory.collect",
        lambda **kw: {
            "units": {"user": {"skai-beellama.service": "enabled", "extra.service": "enabled"}},
            "packages": {},
            "collectedAt": "t",
        },
    )
    result = CliRunner().invoke(fleet, ["node", "doctor", "--strict"], env=_env(paths))
    assert result.exit_code == 0, result.output


def test_a_clean_node_reports_clean(paths, operator, monkeypatch) -> None:
    store.write_spec(paths, "profile", "worker-gpu", WORKER_PROFILE, writer=operator)
    store.write_spec(paths, "node", "node-under-test", {"role": "worker-gpu"}, writer=operator)
    monkeypatch.setattr(
        "skcapstone.fleet.nodeinventory.collect",
        lambda **kw: {
            "units": {"user": {"skai-beellama.service": "enabled"}},
            "packages": {},
            "collectedAt": "t",
        },
    )
    result = CliRunner().invoke(fleet, ["node", "doctor"], env=_env(paths))
    assert result.exit_code == 0
    assert "(clean)" in result.output
    assert "OK" in result.output


# ----------------------------------------------------------------- skips ---


def test_a_node_with_no_role_is_skipped_not_failed(paths, operator, monkeypatch) -> None:
    store.write_spec(paths, "node", "node-under-test", {"cordoned": False}, writer=operator)
    monkeypatch.setattr(
        "skcapstone.fleet.nodeinventory.collect",
        lambda **kw: {"units": {"user": {}}, "packages": {}, "collectedAt": "t"},
    )
    result = CliRunner().invoke(fleet, ["node", "doctor"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert "no spec.role set" in result.output


def test_a_role_with_no_profile_object_is_skipped(paths, operator, monkeypatch) -> None:
    store.write_spec(paths, "node", "node-under-test", {"role": "not-authored"}, writer=operator)
    monkeypatch.setattr(
        "skcapstone.fleet.nodeinventory.collect",
        lambda **kw: {"units": {"user": {}}, "packages": {}, "collectedAt": "t"},
    )
    result = CliRunner().invoke(fleet, ["node", "doctor"], env=_env(paths))
    assert result.exit_code == 0
    assert "no valid profile object" in result.output


def test_all_skips_the_roleless_node_and_still_exits_zero(fleet_tree, operator) -> None:
    store.write_spec(fleet_tree, "node", "node-roleless", {"cordoned": False}, writer=operator)
    result = CliRunner().invoke(fleet, ["node", "doctor", "--all"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    assert "node-roleless" in result.output
    assert "no spec.role set" in result.output


def test_all_reads_published_inventory_not_the_local_host(fleet_tree, operator) -> None:
    """--all must not report every node's drift using THIS node's units."""
    sknoded = store.Writer(role="sknoded", node="node-under-test", identity="")
    store.write_node_file(
        fleet_tree,
        sknoded,
        "node.json",
        {
            "kind": "Node",
            "name": "node-under-test",
            "node": "node-under-test",
            "status": {"inventory": {"units": {"user": {"skai-beellama.service": "enabled"}}}},
        },
    )
    result = CliRunner().invoke(fleet, ["node", "doctor", "--all", "--json"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    report = json.loads(result.output)[0]
    assert report["forbidden_units"] == []  # published inventory is clean


# ----------------------------------------------------------- zero writes ---


def test_doctor_writes_nothing_at_all(fleet_tree) -> None:
    """It runs on the node it judges, so it must be inert."""
    before = _snapshot(fleet_tree.root)
    CliRunner().invoke(fleet, ["node", "doctor"], env=_env(fleet_tree))
    CliRunner().invoke(fleet, ["node", "doctor", "--json"], env=_env(fleet_tree))
    CliRunner().invoke(fleet, ["node", "doctor", "--all"], env=_env(fleet_tree))
    assert _snapshot(fleet_tree.root) == before


def test_doctor_never_bumps_a_generation(fleet_tree) -> None:
    before = store.read_spec(fleet_tree, "node", "node-under-test")["generation"]
    CliRunner().invoke(fleet, ["node", "doctor"], env=_env(fleet_tree))
    assert store.read_spec(fleet_tree, "node", "node-under-test")["generation"] == before


def test_a_node_that_has_published_no_inventory_is_skipped_not_graded(
    fleet_tree, operator
) -> None:
    """ABSENT and EMPTY are different answers.

    During rollout every node not yet carrying the inventory publisher has no
    `status.inventory` at all. Feeding that to the diff as {} makes a healthy
    node read as "everything is missing" and grades it WARN, which is the one
    verdict nodeinventory exists to never produce.
    """
    store.write_spec(
        fleet_tree, "node", "node-not-upgraded", {"role": "worker-gpu"}, writer=operator
    )
    sknoded = store.Writer(role="sknoded", node="node-not-upgraded", identity="")
    store.write_node_file(
        fleet_tree,
        sknoded,
        "node.json",
        {
            "kind": "Node",
            "name": "node-not-upgraded",
            "node": "node-not-upgraded",
            "status": {"capacity": {"cores": 4}},  # no inventory key at all
        },
    )

    result = CliRunner().invoke(fleet, ["node", "doctor", "--all", "--json"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    graded = {r["node"] for r in _json_payload(result.output)}
    assert "node-not-upgraded" not in graded
    assert "published no inventory yet" in result.output


def test_a_node_publishing_a_genuinely_empty_inventory_is_still_graded(
    fleet_tree, operator
) -> None:
    """The other half of the distinction: a node that HAS published, and
    really has nothing enabled, is a real finding and must not be skipped."""
    store.write_spec(fleet_tree, "node", "node-bare", {"role": "worker-gpu"}, writer=operator)
    sknoded = store.Writer(role="sknoded", node="node-bare", identity="")
    store.write_node_file(
        fleet_tree,
        sknoded,
        "node.json",
        {
            "kind": "Node",
            "name": "node-bare",
            "node": "node-bare",
            "status": {"inventory": {"units": {"user": {}}, "packages": {}}},
        },
    )

    result = CliRunner().invoke(fleet, ["node", "doctor", "--all", "--json"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    reports = {r["node"]: r for r in _json_payload(result.output)}
    assert "node-bare" in reports
    assert reports["node-bare"]["missing_required_units"] == ["skai-beellama.service"]


def test_naming_another_node_uses_its_published_inventory_not_the_local_one(
    fleet_tree, operator, monkeypatch
) -> None:
    """`node doctor <other-node>` must never grade THIS node's units against
    that node's profile.

    It did, and the result was worse than an error: a confident, well-formed
    report about the wrong machine. `--all` was correct while the named form
    silently disagreed with it, which is the shape of bug that survives
    because both outputs look fine.
    """
    store.write_spec(fleet_tree, "node", "node-other", {"role": "worker-gpu"}, writer=operator)
    sknoded = store.Writer(role="sknoded", node="node-other", identity="")
    store.write_node_file(
        fleet_tree,
        sknoded,
        "node.json",
        {
            "kind": "Node",
            "name": "node-other",
            "node": "node-other",
            # This node is CLEAN against worker-gpu.
            "status": {"inventory": {"units": {"user": {"skai-beellama.service": "enabled"}}}},
        },
    )
    # The LOCAL inventory is drifted (the fixture makes it forbidden-heavy).
    result = CliRunner().invoke(
        fleet, ["node", "doctor", "node-other", "--json"], env=_env(fleet_tree)
    )
    assert result.exit_code == 0, result.output
    report = _json_payload(result.output)[0]
    assert report["node"] == "node-other"
    assert report["severity"] == "ok", "graded the local units instead of the named node's"
    assert report["forbidden_units"] == []


def test_naming_a_node_that_published_nothing_is_a_skip_not_a_wrong_answer(
    fleet_tree, operator
) -> None:
    store.write_spec(fleet_tree, "node", "node-silent", {"role": "worker-gpu"}, writer=operator)
    result = CliRunner().invoke(fleet, ["node", "doctor", "node-silent"], env=_env(fleet_tree))
    assert result.exit_code == 0, result.output
    assert "published no inventory yet" in result.output


def test_the_named_form_and_all_agree_for_a_remote_node(fleet_tree, operator) -> None:
    """For a node that is not this one, both paths read what that node
    published, so they must produce identical reports. That divergence is
    what hid the bug.

    They legitimately DIFFER for the local node: the named form collects
    live while --all reads the last published snapshot, and live is the
    better answer when you are standing on the machine.
    """
    store.write_spec(fleet_tree, "node", "node-remote", {"role": "worker-gpu"}, writer=operator)
    sknoded = store.Writer(role="sknoded", node="node-remote", identity="")
    store.write_node_file(
        fleet_tree,
        sknoded,
        "node.json",
        {
            "kind": "Node",
            "name": "node-remote",
            "node": "node-remote",
            "status": {"inventory": {"units": {"user": {"skchat-daemon.service": "enabled"}}}},
        },
    )
    runner = CliRunner()
    named = _json_payload(
        runner.invoke(
            fleet, ["node", "doctor", "node-remote", "--json"], env=_env(fleet_tree)
        ).output
    )[0]
    every = _json_payload(
        runner.invoke(fleet, ["node", "doctor", "--all", "--json"], env=_env(fleet_tree)).output
    )
    matching = [r for r in every if r["node"] == "node-remote"][0]
    assert named == matching
    assert named["forbidden_units"] == ["skchat-daemon.service"]
