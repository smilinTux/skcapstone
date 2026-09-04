"""Tests for the Atlas three-registry reconciler (card 0e050100, Task 5.1).

Two halves, and the second one is the load-bearing half.

The first half covers the four delta classes over injected readers: the whole
point of the injection is that not one of these tests needs a live systemd, a
populated fleet store, or an installed skos. A test that only passes on the box
it was written on cannot tell you anything at 4am.

The second half is the freeze proof. Atlas runs under docs/ATLAS_CONSTITUTION.md
and the standing governance position that "ATLAS remains frozen and last", so the
reconciler is legal only for as long as it stays pure observation. That is an
architectural claim, and an architectural claim with no test behind it is a
promise. So the freeze tests walk the AST of every module in skcapstone.atlas and
assert that no code path calls an actuation verb or a filesystem write, and that
the read-only command guard actually refuses an actuation argv rather than merely
documenting that it would.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from skcapstone.atlas import reconcile as rc
from skcapstone.fleet.paths import FleetPaths

# ---------------------------------------------------------------------------
# Fixtures: three readers with no live anything behind them.
# ---------------------------------------------------------------------------


def _declared(*items: tuple[str, str, str]) -> rc.DeclaredReader:
    """Build a declared reader from (kind, name, pack) triples."""
    rows = [rc.DeclaredItem(kind=k, name=n, pack=p, unit=rc.unit_for(k, n)) for k, n, p in items]
    return lambda: rows


def _desired(*items: tuple[str, str, str]) -> rc.DesiredReader:
    """Build a desired reader from (kind, name, node) triples."""
    rows = [
        rc.DesiredItem(kind=k, name=n, node=nd, unit=rc.unit_for(k, n), generation=1)
        for k, n, nd in items
    ]
    return lambda paths, node: [r for r in rows if r.node in (node, "")]


def _actual(*items: tuple[str, str]) -> rc.ActualReader:
    """Build an actual reader from (unit, state) pairs."""
    rows = [rc.ActualItem(unit=u, state=s, load="loaded") for u, s in items]
    return lambda node: rows


def _run(declared, desired, actual, node: str = "node-test") -> rc.ReconcileReport:
    return rc.reconcile(
        FleetPaths(root=Path("/nonexistent-fleet-root")),
        node,
        declared_reader=declared,
        desired_reader=desired,
        actual_reader=actual,
    )


def _kinds(report: rc.ReconcileReport, kind: str) -> list[str]:
    return sorted(d.unit for d in report.deltas if d.kind == kind)


# ---------------------------------------------------------------------------
# Delta class 1: declared but not desired.
# A pack said it should exist; the fleet store never got the object.
# ---------------------------------------------------------------------------


def test_declared_but_not_desired():
    report = _run(
        _declared(("cronjob", "skbrain-sync", "skbrain")),
        _desired(),
        _actual(),
    )
    assert _kinds(report, rc.DECLARED_NOT_DESIRED) == ["skbrain-sync.timer"]
    delta = [d for d in report.deltas if d.kind == rc.DECLARED_NOT_DESIRED][0]
    assert delta.name == "skbrain-sync"
    assert delta.object_kind == "cronjob"
    assert "skbrain" in delta.detail
    assert report.in_agreement is False


# ---------------------------------------------------------------------------
# Delta class 2: desired but not actual.
# The noroc2027 skfleet-rotate case: the fleet wants it, systemd has no unit.
# ---------------------------------------------------------------------------


def test_desired_but_not_actual():
    report = _run(
        _declared(("cronjob", "skfleet-rotate", "skfleet")),
        _desired(("cronjob", "skfleet-rotate", "node-test")),
        _actual(),
    )
    assert _kinds(report, rc.DESIRED_NOT_ACTUAL) == ["skfleet-rotate.timer"]
    assert _kinds(report, rc.DECLARED_NOT_DESIRED) == []
    assert report.in_agreement is False


def test_desired_but_not_actual_records_undeclared_orphans():
    """The job/demo shape: in the fleet store, in no pack, and in no unit.

    It is a desired-but-not-actual delta by set membership, and the detail must
    say it is also undeclared, because "the scheduler invented this" and "the
    install has not run yet" are different problems with the same set difference.
    """
    report = _run(
        _declared(),
        _desired(("job", "demo", "node-test")),
        _actual(),
    )
    assert _kinds(report, rc.DESIRED_NOT_ACTUAL) == ["demo.service"]
    delta = [d for d in report.deltas if d.kind == rc.DESIRED_NOT_ACTUAL][0]
    assert delta.declared is False
    assert "undeclared" in delta.detail


# ---------------------------------------------------------------------------
# Delta class 3: actual but not desired.
# Running with nothing in the fleet store asking for it.
# ---------------------------------------------------------------------------


def test_actual_but_not_desired():
    report = _run(
        _declared(),
        _desired(),
        _actual(("skchat-daemon.service", "active")),
    )
    assert _kinds(report, rc.ACTUAL_NOT_DESIRED) == ["skchat-daemon.service"]
    delta = [d for d in report.deltas if d.kind == rc.ACTUAL_NOT_DESIRED][0]
    assert delta.state == "active"
    assert report.in_agreement is False


def test_actual_reader_scope_keeps_the_desktop_out_of_the_report():
    """Out-of-scope units are not drift. An unscoped reader reports the whole
    desktop session as orphaned and the signal becomes worthless."""
    rows = [
        rc.ActualItem(unit="gnome-terminal-server.service", state="active", load="loaded"),
        rc.ActualItem(unit="skchat-daemon.service", state="active", load="loaded"),
    ]
    kept = rc.in_scope(rows, prefixes=rc.ESTATE_UNIT_PREFIXES)
    assert [r.unit for r in kept] == ["skchat-daemon.service"]


# ---------------------------------------------------------------------------
# Delta class 4: full agreement.
# ---------------------------------------------------------------------------


def test_full_agreement_has_no_deltas():
    report = _run(
        _declared(("cronjob", "skbrain-sync", "skbrain"), ("service", "skos-scheduler", "skos")),
        _desired(
            ("cronjob", "skbrain-sync", "node-test"), ("service", "skos-scheduler", "node-test")
        ),
        _actual(("skbrain-sync.timer", "active"), ("skos-scheduler.service", "active")),
    )
    assert report.deltas == []
    assert report.in_agreement is True
    assert len(report.declared) == 2
    assert len(report.desired) == 2
    assert len(report.actual) == 2
    assert report.node == "node-test"


def test_report_lists_are_typed_and_node_scoped():
    report = _run(
        _declared(("cronjob", "skbrain-sync", "skbrain")),
        _desired(("cronjob", "skbrain-sync", "node-test"), ("cronjob", "elsewhere", "node-other")),
        _actual(("skbrain-sync.timer", "active")),
    )
    assert all(isinstance(d, rc.DeclaredItem) for d in report.declared)
    assert all(isinstance(d, rc.DesiredItem) for d in report.desired)
    assert all(isinstance(d, rc.ActualItem) for d in report.actual)
    assert all(isinstance(d, rc.Delta) for d in report.deltas)
    # node-other's object is not this node's problem.
    assert [d.name for d in report.desired] == ["skbrain-sync"]


def test_service_unit_override_is_honoured():
    """A Service spec may name a unit that is not <name>.service; the join must
    follow spec.unit, or a correctly installed service reads as missing."""
    assert rc.unit_for("service", "skgateway", spec={"unit": "skgateway-api.service"}) == (
        "skgateway-api.service"
    )
    assert rc.unit_for("cronjob", "skbrain-sync") == "skbrain-sync.timer"
    assert rc.unit_for("service", "skos-scheduler") == "skos-scheduler.service"
    assert rc.unit_for("job", "demo") == "demo.service"


def test_readers_that_raise_degrade_to_empty_not_to_a_crash():
    """Observation must never take the caller down. An unreadable registry is
    an empty registry, and the report says the read failed."""

    def boom():
        raise RuntimeError("no skos here")

    report = _run(boom, _desired(), _actual(("skchat-daemon.service", "active")))
    assert report.declared == []
    assert any("declared" in note for note in report.notes)
    assert _kinds(report, rc.ACTUAL_NOT_DESIRED) == ["skchat-daemon.service"]


# ---------------------------------------------------------------------------
# The freeze proof. Atlas gains no authority from this module.
# ---------------------------------------------------------------------------

_ATLAS_PKG = Path(rc.__file__).parent

#: Call names that actuate. Any of these reached from atlas/ is a constitutional
#: violation, not a style problem.
_FORBIDDEN_CALLS = {
    "start",
    "stop",
    "restart",
    "enable",
    "disable",
    "reload",
    "apply",
    "write",
    "write_text",
    "write_bytes",
    "writelines",
    "mkdir",
    "makedirs",
    "unlink",
    "rmtree",
    "touch",
    "rename",
    "replace",
    "chmod",
    "system",
    "popen",
    "call",
    "check_call",
    "check_output",
    "write_spec",
    "write_status",
    "write_placement",
    "write_node_file",
    "write_plane_file",
    "set_frozen",
    "systemd_start",
    "systemd_restart",
    "act",
    "dispatch",
}

#: systemctl subcommands that only observe. Anything outside this set appearing
#: as a literal anywhere in atlas/ fails the freeze proof.
_ACTUATING_SYSTEMCTL = {
    "start",
    "stop",
    "restart",
    "reload",
    "enable",
    "disable",
    "mask",
    "unmask",
    "kill",
    "daemon-reload",
    "set-property",
    "isolate",
}


def _atlas_modules() -> list[Path]:
    mods = sorted(_ATLAS_PKG.rglob("*.py"))
    assert mods, "no modules found in skcapstone.atlas"
    return mods


def _called_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name):
            names.add(func.id)
        elif isinstance(func, ast.Attribute):
            names.add(func.attr)
    return names


def test_no_atlas_code_path_calls_an_actuation_verb():
    offenders: list[str] = []
    for mod in _atlas_modules():
        tree = ast.parse(mod.read_text(), filename=str(mod))
        for name in sorted(_called_names(tree) & _FORBIDDEN_CALLS):
            offenders.append(f"{mod.name}: {name}()")
    assert offenders == [], f"atlas/ must not actuate or write: {offenders}"


def test_no_atlas_module_opens_a_file_for_writing():
    offenders: list[str] = []
    for mod in _atlas_modules():
        tree = ast.parse(mod.read_text(), filename=str(mod))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_open = (isinstance(func, ast.Name) and func.id == "open") or (
                isinstance(func, ast.Attribute) and func.attr == "open"
            )
            if not is_open:
                continue
            modes = [a for a in node.args[1:2] if isinstance(a, ast.Constant)]
            modes += [
                kw.value
                for kw in node.keywords
                if kw.arg == "mode" and isinstance(kw.value, ast.Constant)
            ]
            for m in modes:
                if isinstance(m.value, str) and any(c in m.value for c in "wax+"):
                    offenders.append(f"{mod.name}: open(mode={m.value!r})")
    assert offenders == [], f"atlas/ must not open a file for writing: {offenders}"


def test_no_actuating_systemctl_subcommand_appears_in_atlas():
    offenders: list[str] = []
    for mod in _atlas_modules():
        tree = ast.parse(mod.read_text(), filename=str(mod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in _ACTUATING_SYSTEMCTL:
                    offenders.append(f"{mod.name}: {node.value!r}")
    assert offenders == [], f"atlas/ must issue no actuating systemctl verb: {offenders}"


def test_read_only_guard_refuses_an_actuation_argv():
    """The static proof above is a backstop, not the control. The guard is the
    control: it refuses at runtime, so an injected runner cannot be handed an
    actuating command by this module even if a future edit tries."""
    rc.assert_read_only(["systemctl", "--user", "list-units", "--all"])
    rc.assert_read_only(["systemctl", "--user", "show", "skchat-daemon.service"])
    for bad in (
        ["systemctl", "--user", "restart", "skchat-daemon.service"],
        ["systemctl", "--user", "start", "skchat-daemon.service"],
        ["systemctl", "--user", "daemon-reload"],
        ["rm", "-rf", "/"],
    ):
        with pytest.raises(rc.ReadOnlyViolationError):
            rc.assert_read_only(bad)


def test_default_actual_reader_only_ever_issues_read_only_commands():
    """Drive the real default reader with a fake runner and inspect the argv it
    produced. This proves the shipped code path, not a hand-written string."""
    issued: list[list[str]] = []

    def spy(cmd: list[str]) -> subprocess.CompletedProcess:
        issued.append(list(cmd))
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    rc.systemd_actual("node-test", runner=spy)
    assert issued, "the default actual reader issued no command at all"
    for cmd in issued:
        rc.assert_read_only(cmd)


def test_default_actual_reader_parses_units_and_degrades_on_failure():
    listing = (
        "  skchat-daemon.service  loaded active   running SKChat daemon\n"
        "* skbrain-sync.timer     loaded active   waiting Run skbrain sync\n"
        "  nextcloud-lumina-sync.service loaded failed failed Nextcloud sync\n"
    )

    def ok(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 0, stdout=listing, stderr="")

    rows = rc.systemd_actual("node-test", runner=ok)
    by_unit = {r.unit: r for r in rows}
    assert by_unit["skchat-daemon.service"].state == "active"
    assert by_unit["skbrain-sync.timer"].state == "active"
    assert by_unit["nextcloud-lumina-sync.service"].state == "failed"

    def broken(cmd: list[str]) -> subprocess.CompletedProcess:
        raise OSError("no systemd on this box")

    assert rc.systemd_actual("node-test", runner=broken) == []


def test_reconcile_creates_no_files_under_a_missing_fleet_root(tmp_path):
    """Observation must not manufacture the state it observes. Point every
    default reader at paths that do not exist and assert nothing appears."""
    root = tmp_path / "fleet"
    report = rc.reconcile(
        FleetPaths(root=root),
        "node-test",
        declared_reader=lambda: [],
        actual_reader=lambda node: [],
    )
    assert not root.exists(), "reconcile created the fleet tree it was only reading"
    assert report.desired == []
