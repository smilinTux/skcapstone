"""Scratch-fleet drill harness (card a83214e3).

The harness exists so the control-seat promotion runbook can be rehearsed,
and its whole value depends on one property: it must be structurally
incapable of writing into ``~/.skcapstone``, which is a LIVE Syncthing folder
shared to three other machines.

So the refusals are tested directly and NEGATIVE-CONTROLLED. Every guard test
here comes in a pair: the guard fires on a path aimed at production, AND the
same guard accepts an equivalent path aimed at tmp. A guard nobody has seen
fail is not known to work, and a guard that refuses everything would pass a
one-sided test while making the harness useless.

The rest asserts the drill is worth running: the tree it builds makes
``skfleet nodes``, ``get profiles``, ``services`` and ``node doctor`` return
real output, the control seat can be killed and derives as Dead, the standby
promotes, and every step carries an executable revert.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from skcapstone.fleet import drill, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.node_controller import DEAD_AFTER_S, NOT_READY_AFTER_S

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=timezone.utc)

#: The real thing the harness must never reach.
PROD = Path("~/.skcapstone/fleet").expanduser()


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Every file under root as {relpath: (size, mtime_ns)}.

    Size is listed first because it is the load-bearing half: .41's sknoded
    syncs heartbeats in independently, so a heartbeat.json mtime change at an
    identical size is somebody else's write, while any size change or any new
    or missing path would be ours.
    """
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file()
    }


def _json_tail(output: str):
    """Parse the JSON document at the end of mixed stdout/stderr CLI output.

    CliRunner folds stderr into `output` on the click version this repo pins,
    and `node doctor` writes its skip notes to stderr, so the JSON has to be
    found rather than assumed to start at char 0.
    """
    start = output.index("[")
    return json.loads(output[start:])


def _foreign_writes(before: dict, after: dict) -> dict:
    """Differences that this harness could have caused.

    The live tree is written continuously by daemons that have nothing to do
    with the drill: every node's ``sknoded`` rewrites its own
    ``heartbeat.json`` and ``node.json``, and ``skoperator.timer`` refreshes
    ``objects/operatorapp/*.json`` and service specs on a 15-minute cycle.
    Naming those paths one by one made this assertion flaky, because it failed
    whenever a timer tick landed inside the test run rather than whenever the
    harness misbehaved (card ``4c32df6f`` hit exactly that).

    The rule below is both stricter about what matters and immune to that
    noise. If this harness ever touched production it would **create** paths
    (a tree, plus its ``.skfleet-drill`` marker) or **remove** them
    (``teardown`` is a recursive delete), and any spec it wrote would change
    that file's size, since ``write_spec`` bumps ``generation`` and rewrites
    the whole document. A same-size, mtime-only change is the one shape the
    drill cannot produce and the one shape another node's daemon produces
    constantly, so it is the only difference forgiven here.
    """
    out = {}
    for path in set(before) | set(after):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if old is not None and new is not None and old[0] == new[0]:
            continue  # same size, mtime only: another node's daemon, not us
        out[path] = (old, new)
    return out


# ---------------------------------------------------------------------------
# The guard: refusals, each with its negative control
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "case",
    [
        "exact-production-tree",
        "dotdot-walk-back-into-production",
        "deep-under-sovereign-home",
        "sovereign-home-itself",
    ],
)
def test_guard_refuses_paths_that_resolve_into_the_sovereign_tree(case):
    """Aimed at production by any spelling, the guard refuses."""
    home = Path("~/.skcapstone").expanduser()
    candidate = {
        "exact-production-tree": home / "fleet",
        # The literal string points at a harmless-looking sibling; only
        # resolution reveals it lands back on the live tree.
        "dotdot-walk-back-into-production": home / "fleet" / ".." / "fleet",
        "deep-under-sovereign-home": home / "agents" / "lumina" / "memory",
        "sovereign-home-itself": home,
    }[case]
    with pytest.raises(drill.UnsafeDrillRootError, match="sovereign"):
        drill.resolve_drill_root(candidate)


def test_guard_refuses_a_symlink_pointing_at_production(tmp_path):
    """A symlink is resolved before it is judged, so it cannot smuggle a root in."""
    link = tmp_path / "innocent-looking-scratch"
    link.symlink_to(PROD, target_is_directory=True)
    # Sanity: the literal path really does look safe, which is the point.
    assert str(link).startswith(str(tmp_path))
    with pytest.raises(drill.UnsafeDrillRootError, match="sovereign"):
        drill.resolve_drill_root(link)


def test_guard_refuses_a_root_under_a_symlinked_parent(tmp_path):
    """Traversal through a symlinked PARENT is caught too, not just a leaf link."""
    link = tmp_path / "hop"
    link.symlink_to(Path("~/.skcapstone").expanduser(), target_is_directory=True)
    with pytest.raises(drill.UnsafeDrillRootError, match="sovereign"):
        drill.resolve_drill_root(link / "fleet" / "objects")


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_guard_refuses_a_missing_root(blank):
    """No implicit target: an omitted root is an error, never a default."""
    with pytest.raises(drill.UnsafeDrillRootError, match="explicitly"):
        drill.resolve_drill_root(blank)


def test_guard_refuses_degenerate_targets():
    """The filesystem root and $HOME are never drill roots."""
    with pytest.raises(drill.UnsafeDrillRootError, match="filesystem root"):
        drill.resolve_drill_root("/")
    with pytest.raises(drill.UnsafeDrillRootError, match="HOME"):
        drill.resolve_drill_root(Path.home())


# --- negative controls: the same guard, aimed somewhere legitimate ----------


def test_guard_accepts_a_plain_scratch_root(tmp_path):
    """NEGATIVE CONTROL: the guard is not simply refusing everything."""
    assert drill.resolve_drill_root(tmp_path / "scratch") == (tmp_path / "scratch").resolve()


def test_guard_accepts_dotdot_and_symlinks_that_stay_out_of_production(tmp_path):
    """NEGATIVE CONTROL: it is containment being enforced, not a ban on `..`.

    Same two shapes that were refused above (a `..` walk and a symlink), only
    resolving somewhere harmless, so the refusals cannot be passing for the
    incidental reason that the path contained a `..` or was a link.
    """
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert drill.resolve_drill_root(tmp_path / "a" / ".." / "b") == (tmp_path / "b").resolve()
    assert drill.resolve_drill_root(link) == real.resolve()


def test_the_guard_is_what_protects_the_production_path_specifically(tmp_path):
    """create() and teardown() both refuse production, not just the resolver."""
    with pytest.raises(drill.UnsafeDrillRootError, match="sovereign"):
        drill.create(PROD)
    with pytest.raises(drill.UnsafeDrillRootError, match="sovereign"):
        drill.attach(PROD)
    with pytest.raises(drill.UnsafeDrillRootError, match="sovereign"):
        drill.DrillFleet(root=PROD).teardown()


# ---------------------------------------------------------------------------
# The marker: the harness may only touch trees it created
# ---------------------------------------------------------------------------


def test_create_refuses_an_existing_directory_it_did_not_make(tmp_path):
    """An unmarked existing directory is somebody else's data."""
    victim = tmp_path / "someones-work"
    victim.mkdir()
    (victim / "important.txt").write_text("do not delete me")
    with pytest.raises(drill.UnsafeDrillRootError, match="marker"):
        drill.create(victim)
    assert (victim / "important.txt").read_text() == "do not delete me"


def test_teardown_refuses_an_unmarked_directory(tmp_path):
    """The destructive call re-checks the marker rather than trusting the handle."""
    victim = tmp_path / "not-a-drill"
    victim.mkdir()
    (victim / "keep.txt").write_text("x")
    with pytest.raises(drill.UnsafeDrillRootError, match="marker"):
        drill.DrillFleet(root=victim).teardown()
    assert victim.exists()


def test_teardown_refuses_after_the_marker_is_removed(tmp_path):
    """NEGATIVE CONTROL on the marker: deleting it withdraws permission."""
    handle = drill.create(tmp_path / "scratch", now=NOW)
    drill.marker_path(handle.root).unlink()
    with pytest.raises(drill.UnsafeDrillRootError, match="marker"):
        handle.teardown()
    assert handle.root.exists()


def test_a_foreign_marker_file_does_not_count(tmp_path):
    """The marker must be OUR marker, not merely a file of that name."""
    victim = tmp_path / "spoofed"
    victim.mkdir()
    drill.marker_path(victim).write_text(json.dumps({"kind": "something-else"}))
    assert drill.read_marker(victim) is None
    with pytest.raises(drill.UnsafeDrillRootError, match="marker"):
        drill.create(victim)


def test_create_reclaims_its_own_previous_tree(tmp_path):
    """A marked root is reusable, so re-drilling does not need a manual rm."""
    first = drill.create(tmp_path / "scratch", now=NOW)
    (first.root / "leftover.txt").write_text("stale")
    second = drill.create(tmp_path / "scratch", now=NOW)
    assert second.root == first.root
    assert not (second.root / "leftover.txt").exists()
    assert drill.read_marker(second.root) is not None


# ---------------------------------------------------------------------------
# SKFLEET_ROOT is never the drill target
# ---------------------------------------------------------------------------


def test_ambient_skfleet_root_is_never_used_as_the_target(tmp_path, monkeypatch):
    """An operator with SKFLEET_ROOT exported still cannot aim the drill at it.

    This is the realistic accident: on the control node the variable is
    exported and points at production, so any code path that treated it as a
    default would silently drill against the live tree.
    """
    monkeypatch.setenv("SKFLEET_ROOT", str(PROD))
    before = _snapshot(PROD)
    handle = drill.create(tmp_path / "scratch", now=NOW)
    assert handle.root == (tmp_path / "scratch").resolve()
    assert (handle.root / "objects" / "node").is_dir()
    assert not _foreign_writes(before, _snapshot(PROD))


def test_drill_module_never_calls_default_paths():
    """default_paths() is the one function whose return value is production.

    Asserted against the source because it is a property of the module, not
    of any one code path: no test can cover every future branch, but a call
    site cannot hide from the file it lives in.
    """
    source = Path(drill.__file__).read_text()
    assert "default_paths(" not in source
    assert "import default_paths" not in source


def test_env_does_not_mutate_the_process_environment(tmp_path, monkeypatch):
    """env() hands a child a pointer; it does not repoint this process."""
    monkeypatch.setenv("SKFLEET_ROOT", str(PROD))
    handle = drill.create(tmp_path / "scratch", now=NOW)
    child = handle.env()
    assert child["SKFLEET_ROOT"] == str(handle.root)
    assert child["SKFLEET_NODE"] == handle.control
    assert os.environ["SKFLEET_ROOT"] == str(PROD)


# ---------------------------------------------------------------------------
# The tree is populated enough to rehearse against
# ---------------------------------------------------------------------------


@pytest.fixture
def drilled(tmp_path):
    """A freshly created scratch fleet, torn down afterwards."""
    handle = drill.create(tmp_path / "scratch", now=NOW)
    yield handle
    if handle.root.exists():
        handle.teardown()


def test_create_populates_nodes_profiles_placements_and_status(drilled):
    """Empty output cannot be told apart from a broken drill, so nothing is empty."""
    paths = drilled.paths
    assert sorted(s["name"] for s in store.list_specs(paths, "node")) == sorted(drilled.nodes)
    assert sorted(s["name"] for s in store.list_specs(paths, "profile")) == [
        "builder-standby",
        "control",
        "worker-gpu",
    ]
    assert store.list_placements(paths, "service")
    assert store.read_status(paths, "service", "drill-gateway", drilled.control)
    for node in drilled.nodes:
        assert store.read_node_file(paths, node, "heartbeat.json")
        assert store.read_node_file(paths, node, "node.json")


def test_every_node_starts_ready(drilled):
    assert drilled.node_phases(now=NOW) == {node: "Ready" for node in drilled.nodes}


def test_roles_are_bound_as_the_promotion_expects(drilled):
    assert drilled.role_of(drilled.control) == "control"
    assert drilled.role_of(drilled.standby) == "builder-standby"
    assert drilled.role_of(drilled.worker) == "worker-gpu"


@pytest.mark.parametrize(
    "args, expect",
    [
        (["nodes"], "node-drill-control"),
        (["get", "profiles"], "builder-standby"),
        (["services"], "drill-gateway"),
        (["node", "doctor", "--all"], "node-drill-worker"),
        (["placements"], "drill-gateway"),
    ],
)
def test_cli_reads_the_drill_tree(drilled, args, expect):
    """The real commands return real output when pointed at the scratch root."""
    result = CliRunner().invoke(fleet, args, env=drilled.env())
    assert result.exit_code == 0, result.output
    assert expect in result.output


def test_node_doctor_reports_the_seeded_drift(drilled):
    """seed_drift must actually reach the doctor, or the report proves nothing."""
    result = CliRunner().invoke(fleet, ["node", "doctor", "--all", "--json"], env=drilled.env())
    assert result.exit_code == 0, result.output
    reports = {r["node"]: r for r in _json_tail(result.output)}
    assert reports[drilled.worker]["unexpected_units"] == ["rogue-drill.service"]
    assert reports[drilled.control]["severity"] == "ok"


def test_seed_drift_off_produces_a_clean_worker(tmp_path):
    """NEGATIVE CONTROL on seed_drift: the finding is seeded, not accidental."""
    handle = drill.create(tmp_path / "clean", now=NOW, seed_drift=False)
    result = CliRunner().invoke(fleet, ["node", "doctor", "--all", "--json"], env=handle.env())
    reports = {r["node"]: r for r in _json_tail(result.output)}
    assert reports[handle.worker]["unexpected_units"] == []
    handle.teardown()


# ---------------------------------------------------------------------------
# The drill itself: kill, promote, revert
# ---------------------------------------------------------------------------


def test_kill_control_drives_the_seat_to_dead(drilled):
    drilled.kill_control(now=NOW)
    phases = drilled.node_phases(now=NOW)
    assert phases[drilled.control] == "Dead"
    assert phases[drilled.standby] == "Ready"


def test_the_two_thresholds_are_both_exercised(drilled):
    """NotReady and Dead are distinct outcomes of the same aging knob."""
    drilled.beat(drilled.control, age_s=NOT_READY_AFTER_S + 10, now=NOW)
    assert drilled.node_phases(now=NOW)[drilled.control] == "NotReady"
    drilled.beat(drilled.control, age_s=DEAD_AFTER_S + 10, now=NOW)
    assert drilled.node_phases(now=NOW)[drilled.control] == "Dead"
    drilled.beat(drilled.control, age_s=0, now=NOW)
    assert drilled.node_phases(now=NOW)[drilled.control] == "Ready"


def test_promote_refuses_while_the_seat_is_alive(drilled):
    """Two live seats is a split brain, so the precondition is checked."""
    with pytest.raises(drill.DrillPreconditionError, match="split brain"):
        drilled.promote(now=NOW)
    assert drilled.role_of(drilled.standby) == "builder-standby"


def test_force_promotes_anyway_so_that_failure_can_be_drilled(drilled):
    """NEGATIVE CONTROL on the precondition: it is a gate, not a hard block."""
    drilled.promote(now=NOW, force=True)
    assert drilled.role_of(drilled.standby) == "control"


def test_full_promotion_moves_the_seat(drilled):
    drilled.kill_control(now=NOW)
    steps = drilled.promote(now=NOW)
    assert [s.action for s in steps] == [
        "cordon the lost seat",
        "taint the lost seat",
        "promote the warm replica",
    ]
    assert all(s.revert for s in steps)
    assert drilled.role_of(drilled.standby) == "control"
    control_spec = store.read_spec(drilled.paths, "node", drilled.control)["spec"]
    assert control_spec["cordoned"] is True
    assert control_spec["taints"] == [
        {"key": "control-seat", "value": "lost", "effect": "NoSchedule"}
    ]


def test_revert_puts_the_tree_back(drilled):
    """The documented reverts are executable, which is the only proof they work."""
    before = store.read_spec(drilled.paths, "node", drilled.standby)["spec"]["role"]
    drilled.kill_control(now=NOW)
    drilled.promote(now=NOW)
    drilled.revert_promotion()
    control_spec = store.read_spec(drilled.paths, "node", drilled.control)["spec"]
    assert store.read_spec(drilled.paths, "node", drilled.standby)["spec"]["role"] == before
    assert control_spec["cordoned"] is False
    assert control_spec["taints"] == []


def test_summary_exposes_the_thresholds_it_judged_by(drilled):
    payload = drill.summary(drilled, now=NOW)
    assert payload["thresholds"] == {
        "notReadyAfterS": NOT_READY_AFTER_S,
        "deadAfterS": DEAD_AFTER_S,
    }
    assert payload["marker"]["kind"] == drill.MARKER_KIND


def test_teardown_removes_everything(tmp_path):
    handle = drill.create(tmp_path / "scratch", now=NOW)
    assert handle.root.exists()
    removed = handle.teardown()
    assert removed == handle.root
    assert not handle.root.exists()


def test_operations_on_a_torn_down_tree_refuse(tmp_path):
    """A stale handle fails closed rather than recreating anything."""
    handle = drill.create(tmp_path / "scratch", now=NOW)
    handle.teardown()
    with pytest.raises(drill.UnsafeDrillRootError, match="no such drill root"):
        handle.node_phases(now=NOW)


# ---------------------------------------------------------------------------
# End to end, with production watched the whole way
# ---------------------------------------------------------------------------


def test_full_drill_lifecycle_writes_nothing_into_production(tmp_path, monkeypatch):
    """The complete rehearsal, with the live tree snapshotted around it.

    SKFLEET_ROOT is pointed at production for the duration, which is the
    hostile version of the operator's normal shell, so the run proves
    containment under the condition that would actually break it.
    """
    monkeypatch.setenv("SKFLEET_ROOT", str(PROD))
    before = _snapshot(PROD)

    handle = drill.create(tmp_path / "gameday", now=NOW)
    later = NOW + timedelta(seconds=600)
    assert handle.node_phases(now=NOW)[handle.control] == "Ready"
    handle.kill_control(now=later)
    assert handle.node_phases(now=later)[handle.control] == "Dead"
    handle.promote(now=later)
    assert handle.role_of(handle.standby) == "control"
    handle.revert_promotion()
    handle.teardown()

    assert not _foreign_writes(before, _snapshot(PROD))


def test_cli_drill_lifecycle_writes_nothing_into_production(tmp_path, monkeypatch):
    """Same lifecycle through the CLI, since that is what an operator runs."""
    monkeypatch.setenv("SKFLEET_ROOT", str(PROD))
    before = _snapshot(PROD)
    runner = CliRunner()
    root = str(tmp_path / "cli-gameday")

    for args in (
        ["drill", "create", "--root", root],
        ["drill", "kill-control", "--root", root],
        ["drill", "promote", "--root", root],
        ["drill", "status", "--root", root, "--json"],
        ["drill", "teardown", "--root", root],
    ):
        result = runner.invoke(fleet, args)
        assert result.exit_code == 0, (args, result.output)

    assert not Path(root).exists()
    assert not _foreign_writes(before, _snapshot(PROD))


def test_cli_drill_refuses_production_root():
    """The refusal survives the CLI layer instead of becoming a traceback."""
    result = CliRunner().invoke(fleet, ["drill", "create", "--root", str(PROD)])
    assert result.exit_code != 0
    assert "sovereign" in result.output


def test_cli_drill_requires_an_explicit_root():
    """Omitting --root is a usage error, never a fallback to the live tree."""
    result = CliRunner().invoke(fleet, ["drill", "create"])
    assert result.exit_code != 0
    assert "--root" in result.output
