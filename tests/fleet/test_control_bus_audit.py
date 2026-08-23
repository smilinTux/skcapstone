"""control-bus audit (card 912d309b).

The budget in docs/fleet/control-bus-folder.md is only real if something
fails when it is broken, so the properties under test are: an oversized
tree exits non-zero AND names what made it oversized, a path outside the
five known classes fails on its own even when the tree is tiny, the
recommended .stignore excludes nothing inside those classes (asserted by
matching every path, not by reading the text), and the whole command
writes nothing.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import control_bus_audit as cba
from skcapstone.fleet.cli import fleet

KNOWN = ("objects", "placements", "status", "decisions", "atlas")


def _write(root, rel: str, size: int = 32) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def _snapshot(root):
    """Every file under root with its mtime and size, for write detection."""
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size)
        for p in root.rglob("*")
        if p.is_file()
    }


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-under-test"}


@pytest.fixture
def tree(paths):
    """A small in-scope tree resembling the live fleet store."""
    root = paths.root
    _write(root, "objects/service/skgateway.json", 3000)
    _write(root, "objects/_freeze.json", 64)
    _write(root, "placements/service/skgateway.json", 500)
    _write(root, "decisions/2026-08-14.json", 200)
    _write(root, "atlas/brief/index.html", 3800)
    (root / "status" / "node-a").mkdir(parents=True)
    (root / "status" / "node-a" / "node.json").write_text(
        json.dumps({"status": {"inventory": {"units": {"a.service": "enabled"}}}}),
        encoding="utf-8",
    )
    _write(root, "status/node-a/events.jsonl", 8000)
    _write(root, "status/node-a/heartbeat.json", 200)
    return paths


# --- scanning --------------------------------------------------------------


def test_totals_and_class_breakdown(tree):
    report = cba.audit(tree)

    assert report.file_count == 8
    assert report.total_bytes == sum(e.size for e in cba.walk(tree.root))
    names = [c.name for c in report.by_class]
    assert names == ["objects", "placements", "status", "decisions", "atlas"]
    assert all(c.known for c in report.by_class)
    objects = next(c for c in report.by_class if c.name == "objects")
    assert (objects.size, objects.files) == (3064, 2)
    assert report.ok and not report.over_budget


def test_disk_bytes_exceed_content_bytes_on_a_tree_of_small_files(tree):
    """The budget is charged on disk, so the two numbers must not be conflated.

    Eight files of a few KB each cost far more in 4KB blocks than in
    content, and directory inodes are counted too, which is what makes the
    figure reconcile with the `du` numbers in the design note.
    """
    report = cba.audit(tree)

    assert report.disk_bytes > report.total_bytes
    assert report.disk_bytes % 512 == 0
    assert report.disk_bytes >= sum(e.disk_size for e in cba.walk(tree.root))


def test_largest_files_are_named_biggest_first(tree):
    report = cba.audit(tree, top=3)

    assert [e.rel for e in report.largest] == [
        "status/node-a/events.jsonl",
        "atlas/brief/index.html",
        "objects/service/skgateway.json",
    ]


def test_missing_classes_are_reported(paths):
    _write(paths.root, "objects/service/x.json", 10)

    report = cba.audit(paths)

    assert report.missing_classes == ["placements", "status", "decisions", "atlas"]


def test_empty_tree_is_ok(paths):
    report = cba.audit(paths)

    assert report.ok
    assert (report.total_bytes, report.file_count) == (0, 0)
    assert "(empty tree)" in cba.render(report)


# --- budget ----------------------------------------------------------------


def test_over_budget_report_names_the_offending_files(tree):
    _write(tree.root, "objects/service/fat.json", 200_000)

    report = cba.audit(tree, budget=64 * 1024)

    assert report.over_budget and not report.ok
    text = cba.render(report)
    assert "OVER BUDGET" in text
    assert "objects/service/fat.json" in text


def test_parse_size_accepts_human_and_raw():
    assert cba.parse_size("10MB") == 10 * 1024 * 1024
    assert cba.parse_size("512KB") == 512 * 1024
    assert cba.parse_size("4096") == 4096
    with pytest.raises(ValueError):
        cba.parse_size("later")
    with pytest.raises(ValueError):
        cba.parse_size("0MB")


# --- scope -----------------------------------------------------------------


def test_out_of_scope_file_is_named_even_when_tiny(tree):
    _write(tree.root, "backups/agents.tar", 5)
    _write(tree.root, "stray.md", 5)

    report = cba.audit(tree)

    assert not report.ok
    assert not report.over_budget  # scope fails on its own, not via the budget
    assert [e.rel for e in report.out_of_scope] == ["backups/agents.tar", "stray.md"]
    text = cba.render(report)
    assert "OUT OF SCOPE" in text
    assert "backups/agents.tar" in text and "stray.md" in text


def test_syncthing_markers_are_named_but_do_not_fail(tree):
    _write(tree.root, ".stfolder/syncthing-folder-.stfolder", 0)
    _write(tree.root, ".stignore", 100)

    report = cba.audit(tree)

    assert report.ok
    assert report.out_of_scope == []
    assert report.markers == [".stfolder", ".stignore"]
    assert ".stfolder" in cba.render(report)


# --- growth risks ----------------------------------------------------------


def test_both_growth_risks_are_always_named(paths):
    report = cba.audit(paths)  # empty tree

    assert [r.name for r in report.risks] == [
        "status/<node>/events.jsonl",
        "status/<node>/node.json inventory",
    ]
    text = cba.render(report)
    assert "events.jsonl" in text and "node.json inventory" in text


def test_events_risk_states_how_many_nodes_break_the_budget(tree):
    _write(tree.root, "status/node-a/events.jsonl.1", 1000)

    risk = next(r for r in cba.audit(tree).risks if "events" in r.name)

    assert risk.files == 2
    assert risk.size == 9000
    # 10MB budget against a 2MB per-node cap: five saturated nodes spend it all.
    assert "5 node(s)" in risk.detail


def test_inventory_risk_measures_the_published_block(tree):
    risk = next(r for r in cba.audit(tree).risks if "inventory" in r.name)

    assert risk.files == 1
    assert risk.size == len(json.dumps({"units": {"a.service": "enabled"}}).encode())
    assert "node-a" in risk.detail


def test_unreadable_node_report_does_not_break_the_audit(tree):
    (tree.root / "status" / "node-b").mkdir(parents=True)
    (tree.root / "status" / "node-b" / "node.json").write_text("{not json", encoding="utf-8")

    risk = next(r for r in cba.audit(tree).risks if "inventory" in r.name)

    assert risk.files == 2  # counted as a file, contributes zero inventory bytes
    assert "node-a" in risk.detail


# --- .stignore -------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        *[f"{name}/anything.json" for name in KNOWN],
        *[f"{name}/deep/nested/thing.json" for name in KNOWN],
        *KNOWN,
        "objects/_freeze.json",  # the human kill switch, never skippable
        "status/node-a/events.jsonl",
        "atlas/brief/index.html",
    ],
)
def test_stignore_excludes_nothing_inside_the_known_classes(rel):
    assert cba.stignore_ignores(cba.stignore_body(), rel) is False


@pytest.mark.parametrize(
    "rel",
    ["backups/agents.tar", "agents/lumina/memory/x.json", "stray.md", "skcode/build.log"],
)
def test_stignore_excludes_everything_outside_them(rel):
    assert cba.stignore_ignores(cba.stignore_body(), rel) is True


def test_stignore_keeps_the_syncthing_folder_marker():
    body = cba.stignore_body()

    assert cba.stignore_ignores(body, ".stfolder") is False
    assert cba.stignore_ignores(body, ".stfolder/syncthing-folder-.stfolder") is False


def test_stignore_matcher_negative_control():
    """A ruleset that DOES ignore the classes must be caught, or the
    assertions above would pass against any body at all."""
    assert cba.stignore_ignores("/objects\n", "objects/service/x.json") is True


# --- CLI -------------------------------------------------------------------


def test_cli_reports_and_exits_zero_when_in_scope(tree):
    result = CliRunner().invoke(fleet, ["control-bus", "audit"], env=_env(tree))

    assert result.exit_code == 0, result.output
    assert "control-bus audit" in result.output
    assert "out of scope: none" in result.output


def test_cli_exits_one_over_budget_and_names_the_file(tree):
    _write(tree.root, "objects/service/fat.json", 200_000)

    result = CliRunner().invoke(
        fleet, ["control-bus", "audit", "--budget", "64KB"], env=_env(tree)
    )

    assert result.exit_code == 1
    assert "OVER BUDGET" in result.output
    assert "objects/service/fat.json" in result.output


def test_cli_exits_one_on_an_out_of_scope_path(tree):
    _write(tree.root, "backups/agents.tar", 5)

    result = CliRunner().invoke(fleet, ["control-bus", "audit"], env=_env(tree))

    assert result.exit_code == 1
    assert "backups/agents.tar" in result.output


def test_cli_rejects_a_bad_budget(tree):
    result = CliRunner().invoke(
        fleet, ["control-bus", "audit", "--budget", "soon"], env=_env(tree)
    )

    assert result.exit_code == 2
    assert "--budget" in result.output


def test_cli_json_output(tree):
    result = CliRunner().invoke(fleet, ["control-bus", "audit", "--json"], env=_env(tree))

    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["fileCount"] == 8
    assert payload["outOfScope"] == []
    assert len(payload["growthRisks"]) == 2


def test_cli_stignore_flag_prints_a_body(tree):
    result = CliRunner().invoke(fleet, ["control-bus", "audit", "--stignore"], env=_env(tree))

    assert result.exit_code == 0
    assert result.output == cba.stignore_body()


def test_cli_writes_nothing(tree):
    """Meant to run on the node it judges, so it must not touch the tree."""
    before = _snapshot(tree.root)

    for args in (["control-bus", "audit"], ["control-bus", "audit", "--json"]):
        assert CliRunner().invoke(fleet, args, env=_env(tree)).exit_code == 0

    assert _snapshot(tree.root) == before
