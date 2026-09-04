"""Tests for the registry delta on ``skcapstone atlas eyes`` (card 0e050200).

Three things are pinned here.

First, the operator surface: the render leads with what is wrong, names every
referenced-only Service and every placement with no object behind it, and
collapses agreement to one line. A report that buries eighteen findings under
seventy lines of "fine" has failed at its only job. And it must not invent
findings either: a ``placements/job/`` record has no spec and no unit, and the
surface says so rather than fabricating ``<id>.service`` (card 0e0a0600).

Second, the wiring: ``atlas eyes`` carries the delta on the same read-only pass,
in both the text and the ``--json`` shape, and the CLI tests never touch a live
registry (``observe`` is patched, exactly as ``eyes.assess`` already is).

Third, the freeze: the AST proofs in ``tests/test_atlas_reconcile.py`` already
cover every module under ``atlas/``, this one included, so they are extended
rather than repeated. What is new here is the board proof: Atlas provably does
not read the coordination board (``docs/fleet/seat-charters.md``), and this file
holds the shipped reconcile path to that at runtime with an audit hook, not
just by inspection.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from skcapstone.atlas import delta
from skcapstone.atlas import reconcile as rc
from skcapstone.cli import main
from skcapstone.fleet.paths import FleetPaths

# ---------------------------------------------------------------------------
# Fixtures: a fleet tree on disk shaped like the real drift, and injected
# declared/actual readers so no test needs skos or systemd.
# ---------------------------------------------------------------------------

_JOB_PLACEMENTS = [f"{i:08x}" for i in range(18)]

_SERVICES = {
    # name: (unit, runtime)
    "skchat-daemon": ("skchat-daemon.service", "systemd-user"),
    "skchat-nostr-relay": ("nostr-relay.service", "systemd-user"),
    "skchat-coturn": ("coturn", "docker"),
    "capauth-keystore": ("capauth-keystore", "docker"),
}


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload))


def _fleet_tree(root: Path) -> FleetPaths:
    """A fleet tree with 18 object-less job placements and 4 exec-less Services."""
    paths = FleetPaths(root=root)
    for index, name in enumerate(_JOB_PLACEMENTS):
        node = "node-41" if index < 6 else "node-test"
        _write(
            paths.placement_path("job", name),
            {"kind": "Job", "name": name, "node": node, "placementGeneration": 1},
        )
    for name, (unit, runtime) in _SERVICES.items():
        _write(
            paths.spec_path("service", name),
            {
                "kind": "Service",
                "name": name,
                "generation": 1,
                "spec": {"unit": unit, "runtime": runtime, "conditions": ["Ready"]},
            },
        )
    _write(
        paths.spec_path("cronjob", "skmem-health"),
        {
            "kind": "Cronjob",
            "name": "skmem-health",
            "generation": 1,
            "spec": {"command": "deploy/ops/skmem-health.sh", "schedule": "@daily"},
        },
    )
    return paths


def _actual(*items: tuple[str, str]) -> rc.ActualReader:
    rows = [rc.ActualItem(unit=u, state=s, load="loaded") for u, s in items]
    return lambda node: rows


def _observe(paths: FleetPaths, *, actual: rc.ActualReader | None = None) -> rc.ReconcileReport:
    return delta.observe(
        paths,
        "node-test",
        declared_reader=lambda: [],
        actual_reader=actual
        or _actual(
            ("skchat-daemon.service", "active"),
            ("skmemory-sync@lumina.service", "failed"),
            ("hermes-gateway.service", "active"),
        ),
    )


# ---------------------------------------------------------------------------
# The operator surface.
# ---------------------------------------------------------------------------


def test_render_names_every_job_placement_without_inventing_a_unit(tmp_path):
    report = _observe(_fleet_tree(tmp_path / "fleet"))
    text = delta.render(report)
    lines = text.splitlines()
    assert lines[0].startswith("REGISTRY DELTA  node-test")
    # Desired counts objects only; the 18 job placements are not desired state.
    assert "desired 5 /" in lines[0]
    [head] = [ln for ln in lines if ln.startswith(" ~  18 job placements with no object")]
    assert "'job' is not a fleet object kind, advisory routing only" in head
    assert "6 -> node-41" in head and "12 -> node-test" in head
    assert "no unit is derived for these and no claim is made about systemd" in text
    for name in _JOB_PLACEMENTS:
        assert name in text, f"placement {name} not named"
        assert f"{name}.service" not in text, f"a unit was invented for {name}"
    # Advisory records rank below real drift and above hand-installed units.
    assert text.index("reference a unit but declare no exec") < text.index("job placements")
    assert text.index("job placements") < text.index("no fleet object asks for")


def test_placements_without_object_are_their_own_class_not_desired_not_actual(tmp_path):
    """Before card 0e0a0600 all 18 were desired_not_actual under a fabricated
    ``<id>.service``: a third of that count on the live node was invented."""
    report = _observe(_fleet_tree(tmp_path / "fleet"))
    rows = report.by_kind(rc.PLACEMENT_WITHOUT_OBJECT)
    assert sorted(d.name for d in rows) == _JOB_PLACEMENTS
    assert {d.unit for d in rows} == {""}
    assert {d.node for d in rows} == {"node-41", "node-test"}
    assert all(not d.has_object for d in rows)
    assert not any(d.object_kind == "job" for d in report.by_kind(rc.DESIRED_NOT_ACTUAL))
    assert not any(d.kind == "job" for d in report.desired)
    # Only real objects remain in that class: the cronjob and the referenced-only
    # Service whose unit is absent. Nothing named after a job placement.
    assert sorted(d.unit for d in report.by_kind(rc.DESIRED_NOT_ACTUAL)) == [
        "nostr-relay.service",
        "skmem-health.timer",
    ]
    assert report.summary()[rc.PLACEMENT_WITHOUT_OBJECT] == 18


def test_service_placement_whose_object_is_gone_reads_as_drift(tmp_path):
    paths = _fleet_tree(tmp_path / "fleet")
    _write(
        paths.placement_path("service", "gone"),
        {"kind": "Service", "name": "gone", "node": "node-test", "placementGeneration": 2},
    )
    text = delta.render(_observe(paths))
    assert " !  1 service placement with no object: objects/service/<name>.json is gone" in text
    assert "gone.service" not in text


def test_render_names_referenced_only_services_and_where_their_unit_is(tmp_path):
    report = _observe(_fleet_tree(tmp_path / "fleet"))
    text = delta.render(report)
    assert " !! 4 service objects reference a unit but declare no exec" in text
    assert "skchat-nostr-relay -> nostr-relay.service  not on this node (unplaced)" in text
    assert "unit present for: skchat-daemon" in text
    assert "not observable via systemd: capauth-keystore skchat-coturn" in text
    # The cronjob declares a command, so it is desired-not-actual, not referenced-only.
    assert sorted(d.name for d in report.referenced_only()) == sorted(_SERVICES)
    assert " !  1 cronjob desired, no unit on this node:" in text
    assert "skmem-health.timer" in text


def test_render_puts_failed_orphan_units_before_the_rest(tmp_path):
    text = delta.render(_observe(_fleet_tree(tmp_path / "fleet")))
    assert "FAILED: skmemory-sync@lumina.service" in text
    assert text.index("FAILED:") < text.index("hermes-gateway.service")


def test_render_collapses_agreement_to_one_line():
    report = rc.reconcile(
        FleetPaths(root=Path("/nonexistent-fleet-root")),
        "node-test",
        declared_reader=lambda: [
            rc.DeclaredItem(kind="service", name="a", pack="p", unit="a.service")
        ],
        desired_reader=lambda paths, node: [
            rc.DesiredItem(kind="service", name="a", node=node, unit="a.service")
        ],
        actual_reader=_actual(("a.service", "active")),
    )
    text = delta.render(report)
    assert text.splitlines() == [
        "REGISTRY DELTA  node-test  (declared 1 / desired 1 / actual 1)",
        " ok  the three registries agree on this node",
    ]


def test_render_defers_long_lists_to_json():
    many = [(f"skx-{i}.service", "active") for i in range(delta.INLINE_LIMIT + 5)]
    report = rc.reconcile(
        FleetPaths(root=Path("/nonexistent-fleet-root")),
        "node-test",
        declared_reader=lambda: [],
        desired_reader=lambda paths, node: [],
        actual_reader=_actual(*many),
    )
    text = delta.render(report)
    assert "(+5 more, see --json)" in text


def test_docker_runtime_is_unobservable_not_absent(tmp_path):
    report = _observe(_fleet_tree(tmp_path / "fleet"))
    unobservable = report.by_kind(rc.DESIRED_UNOBSERVABLE)
    assert sorted(d.name for d in unobservable) == ["capauth-keystore", "skchat-coturn"]
    assert not any(
        d.name in ("capauth-keystore", "skchat-coturn")
        for d in report.by_kind(rc.DESIRED_NOT_ACTUAL)
    )


def test_bare_unit_name_is_read_as_a_service_like_systemctl_does():
    assert rc.unit_for("service", "x", {"unit": "capauth-keystore"}) == "capauth-keystore.service"
    assert rc.unit_for("service", "x", {"unit": "coturn", "runtime": "docker"}) == "coturn"


def test_to_dict_is_json_ready_and_carries_the_findings(tmp_path):
    payload = delta.to_dict(_observe(_fleet_tree(tmp_path / "fleet")))
    json.dumps(payload)
    assert payload["schema"] == "skatlas.delta/v1"
    assert payload["node"] == "node-test"
    assert payload["in_agreement"] is False
    assert payload["counts"]["desired"] == 5
    assert payload["counts"]["placements_without_object"] == 18
    assert payload["counts"]["referenced_only"] == 4
    assert set(payload["summary"]) == set(rc.DELTA_KINDS)
    assert payload["summary"]["placement_without_object"] == 18
    assert len(payload["placements_without_object"]) == 18
    assert "unit" not in payload["placements_without_object"][0]
    blob = json.dumps(payload)
    for name in _JOB_PLACEMENTS:
        assert f"{name}.service" not in blob, f"a unit was invented for {name}"


# ---------------------------------------------------------------------------
# The CLI wiring. observe is patched: these never read a live registry.
# ---------------------------------------------------------------------------

_CLEAN_ASSESSMENT = {
    "schema": "skoperator.eyes/v1",
    "at": "2026-09-04T00:00:00Z",
    "frozen": True,
    "freeze_reason": "test",
    "apps": [],
    "itil": {"available": False, "detail": "n/a"},
    "unregistered_modules": [],
    "blind_spots": [],
}


def _fake_report() -> rc.ReconcileReport:
    placement = rc.PlacementItem(kind="job", name="deadbeef", node="node-test")
    return rc.ReconcileReport(
        node="node-test",
        placements=[placement],
        deltas=rc.compute_deltas([], [], [], "node-test", [placement]),
    )


def test_atlas_eyes_text_carries_the_delta_after_the_lane_report():
    with (
        patch("skcapstone.operator_seat.eyes.assess", return_value=_CLEAN_ASSESSMENT),
        patch("skcapstone.operator_seat.eyes.render", return_value="rendered report"),
        patch("skcapstone.atlas.delta.observe", return_value=_fake_report()),
    ):
        result = CliRunner().invoke(main, ["atlas", "eyes"])
    assert result.exit_code == 0, result.output
    assert "rendered report" in result.output
    assert "REGISTRY DELTA  node-test" in result.output
    assert " ~  1 job placement with no object" in result.output
    assert "deadbeef" in result.output
    assert "deadbeef.service" not in result.output
    assert result.output.index("rendered report") < result.output.index("REGISTRY DELTA")


def test_atlas_eyes_json_carries_the_delta_block():
    with (
        patch("skcapstone.operator_seat.eyes.assess", return_value=dict(_CLEAN_ASSESSMENT)),
        patch("skcapstone.atlas.delta.observe", return_value=_fake_report()),
    ):
        result = CliRunner().invoke(main, ["atlas", "eyes", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["schema"] == "skoperator.eyes/v1"
    assert payload["registry_delta"]["schema"] == "skatlas.delta/v1"
    assert payload["registry_delta"]["counts"]["placements_without_object"] == 1
    assert payload["registry_delta"]["summary"]["placement_without_object"] == 1
    assert payload["registry_delta"]["summary"]["desired_not_actual"] == 0


# ---------------------------------------------------------------------------
# The board proof. Atlas provably does not read the coordination board.
# ---------------------------------------------------------------------------

_ATLAS_PKG = Path(rc.__file__).parent

#: Module prefixes that are, or lead to, the coordination board.
_BOARD_MODULES = (
    "skcoord",
    "skcapstone.coordination",
    "skcapstone.card",
    "skcapstone.card_store",
    "skcapstone.coord_",
)

#: Path fragments that name the board on disk. Any of these as a string
#: literal in atlas/ (outside a docstring) is a path being built.
_BOARD_PATH_FRAGMENTS = ("coordination/", "/cards", "card_events", "BOARD.md", "/tasks")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                ids.add(id(body[0].value))
    return ids


def test_no_atlas_module_imports_the_coordination_board():
    offenders: list[str] = []
    for mod in sorted(_ATLAS_PKG.rglob("*.py")):
        tree = ast.parse(mod.read_text(), filename=str(mod))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = "." * node.level + (node.module or "")
                names = [base] + [f"{base}.{a.name}" for a in node.names]
            for name in names:
                if name.startswith(_BOARD_MODULES) or any(
                    name.endswith(s) for s in ("coordination", "card_store", ".card")
                ):
                    offenders.append(f"{mod.name}: import {name}")
    assert offenders == [], f"atlas/ must not import the coordination board: {offenders}"


def test_no_atlas_module_builds_a_board_path():
    offenders: list[str] = []
    for mod in sorted(_ATLAS_PKG.rglob("*.py")):
        tree = ast.parse(mod.read_text(), filename=str(mod))
        skip = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if id(node) in skip or not isinstance(node, ast.Constant):
                continue
            if isinstance(node.value, str) and any(f in node.value for f in _BOARD_PATH_FRAGMENTS):
                offenders.append(f"{mod.name}: {node.value!r}")
    assert offenders == [], f"atlas/ must not name a board path: {offenders}"


# One process-wide audit hook. Audit hooks cannot be removed once added, so it
# stays inert until a test switches tracing on, and every test switches it off.
_TRACING = {"on": False}
_TOUCHED: list[str] = []


def _audit(event: str, args: tuple) -> None:
    if not _TRACING["on"]:
        return
    if event in ("open", "os.listdir", "os.scandir") and args:
        _TOUCHED.append(str(args[0]))


sys.addaudithook(_audit)


def test_shipped_reconcile_path_never_opens_the_coordination_board(tmp_path):
    """Run the real default desired reader against a home that has a populated
    board next to the fleet tree, and watch every file open. The AST tests say
    what the code cannot say; this says what it did not do."""
    home = tmp_path / ".skcapstone"
    paths = _fleet_tree(home / "fleet")
    board = home / "coordination"
    _write(board / "cards" / "0e050200.json", {"id": "0e050200", "title": "bait"})
    _write(board / "card_events" / "0e050200.jsonl", {"event": "bait"})
    _write(board / "tasks" / "task.json", {"id": "bait"})
    (board / "BOARD.md").write_text("# bait\n")

    _TOUCHED.clear()
    _TRACING["on"] = True
    try:
        report = delta.observe(
            paths,
            "node-test",
            declared_reader=lambda: [],
            actual_reader=_actual(("skchat-daemon.service", "active")),
        )
        delta.render(report)
        delta.to_dict(report)
    finally:
        _TRACING["on"] = False

    fleet_reads = [p for p in _TOUCHED if str(paths.root) in p]
    assert fleet_reads, "the default desired reader read nothing; the probe is not live"
    board_reads = [p for p in _TOUCHED if str(board) in p]
    assert board_reads == [], f"atlas read the coordination board: {board_reads}"
    assert not board.joinpath("bait").exists()
    assert len(report.placements_without_object()) == 18
