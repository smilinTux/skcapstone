"""The skoperator CLI: status, freeze/unfreeze (human), pending, decide."""

from __future__ import annotations

from click.testing import CliRunner

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.paths import FleetPaths
from skcapstone.itil import ITILManager
from skcapstone.operator_seat import cli, decisions


def _enroll(tmp_path, monkeypatch):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr(
        "skcapstone.fleet.sknoded.node_capacity",
        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None},
    )
    paths = FleetPaths(root=tmp_path / "fleet")
    op = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=op)
    sknoded.run_once(paths, "node-158")
    return paths


def test_status_and_freeze_cycle(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    r = CliRunner()
    assert "active" in r.invoke(cli.operator, ["status"]).output
    assert r.invoke(cli.operator, ["freeze", "--reason", "drill"]).exit_code == 0
    assert "FROZEN" in r.invoke(cli.operator, ["status"]).output
    assert r.invoke(cli.operator, ["unfreeze"]).exit_code == 0
    assert "active" in r.invoke(cli.operator, ["status"]).output


def test_pending_and_decide(tmp_path, monkeypatch):
    paths = _enroll(tmp_path, monkeypatch)
    ddir = str(paths.root / "decisions")
    decisions.park(
        ddir,
        [{"action": "delete_object", "object": "x"}],
        decision_id="d1",
        created_iso="2026-07-29T00:00:00Z",
    )
    r = CliRunner()
    assert "d1" in r.invoke(cli.operator, ["pending"]).output
    out = r.invoke(cli.operator, ["decide", "d1", "--approve", "--choice", "0"])
    assert out.exit_code == 0 and "approved" in out.output
    assert decisions.list_pending(ddir) == []  # resolved, no longer pending


def test_pending_empty(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    out = CliRunner().invoke(cli.operator, ["pending"])
    assert "no pending decisions" in out.output


def _stub_run_once(monkeypatch):
    # Keep the CLI test off the live adapter probes (subprocess + network): the
    # publish wiring is what we are testing, not the observe pass.
    quiet = {
        "frozen": False,
        "brief": {"firing": [], "stale": [], "quiet": True, "counts": {"firing": 0, "stale": 0}},
        "route": "quiet",
        "proposals": [],
        "outcomes": [],
        "report": "all quiet",
    }
    monkeypatch.setattr(cli.loop, "run_once", lambda *a, **k: quiet)


def test_run_publishes_a_brief_artifact(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    _stub_run_once(monkeypatch)
    pub = tmp_path / "atlas-brief"
    out = CliRunner().invoke(cli.operator, ["run", "--publish-dir", str(pub)])
    assert out.exit_code == 0, out.output
    index = pub / "index.html"
    assert index.exists()
    assert index.read_text().startswith("<!doctype html>")
    assert (pub / "brief.md").exists()


def test_run_no_publish_skips_artifact(tmp_path, monkeypatch):
    _enroll(tmp_path, monkeypatch)
    _stub_run_once(monkeypatch)
    pub = tmp_path / "atlas-brief-skip"
    out = CliRunner().invoke(
        cli.operator, ["run", "--no-publish", "--publish-dir", str(pub)]
    )
    assert out.exit_code == 0, out.output
    assert not pub.exists()


def _redirect_kedb_home(tmp_path, monkeypatch):
    """Point the operator KEDB home at tmp_path so bootstrap seeding stays
    hermetic (the CLI reads SHARED_ROOT at call time via `from .. import`)."""
    home = tmp_path / "shared"
    monkeypatch.setattr("skcapstone.SHARED_ROOT", str(home))
    return home


def _kedb_ids(home) -> set[str]:
    kedb_dir = ITILManager(home).kedb_dir
    if not kedb_dir.exists():
        return set()
    return {p.stem for p in kedb_dir.glob("*.json")}


def test_run_bootstraps_apps_and_kedb(tmp_path, monkeypatch):
    paths = _enroll(tmp_path, monkeypatch)
    _stub_run_once(monkeypatch)
    home = _redirect_kedb_home(tmp_path, monkeypatch)

    out = CliRunner().invoke(cli.operator, ["run", "--no-publish"])
    assert out.exit_code == 0, out.output
    assert "bootstrap:" in out.output

    # All six app adapters registered as Operatorapp objects.
    for app in ("skchat", "skcode", "skcomms", "skgateway", "skmemory", "skos"):
        assert store.read_spec(paths, "operatorapp", app) is not None, f"{app} not registered"

    # The KEDB was seeded (every adapter kedb_ref now resolves to a real entry).
    seeded = _kedb_ids(home)
    assert seeded, "KEDB was not seeded on run"
    assert "ke-telegram-wedge" in seeded


def test_run_no_bootstrap_skips_apps_and_kedb(tmp_path, monkeypatch):
    paths = _enroll(tmp_path, monkeypatch)
    _stub_run_once(monkeypatch)
    home = _redirect_kedb_home(tmp_path, monkeypatch)

    out = CliRunner().invoke(cli.operator, ["run", "--no-publish", "--no-bootstrap"])
    assert out.exit_code == 0, out.output
    assert "bootstrap:" not in out.output

    # Nothing registered, nothing seeded.
    assert store.read_spec(paths, "operatorapp", "skchat") is None
    assert _kedb_ids(home) == set()


def test_run_bootstrap_is_idempotent_and_preserves_ratification(tmp_path, monkeypatch):
    paths = _enroll(tmp_path, monkeypatch)
    _stub_run_once(monkeypatch)
    home = _redirect_kedb_home(tmp_path, monkeypatch)
    r = CliRunner()

    # First run registers + seeds.
    assert r.invoke(cli.operator, ["run", "--no-publish"]).exit_code == 0
    first_kedb = _kedb_ids(home)
    assert first_kedb

    # A human ratifies one proposed standard action between runs.
    rat = r.invoke(cli.operator, ["apps", "ratify", "skchat", "restart-daemon"])
    assert rat.exit_code == 0, rat.output

    # Second run: seeds nothing new (create-or-skip), does not clobber.
    out2 = r.invoke(cli.operator, ["run", "--no-publish"])
    assert out2.exit_code == 0, out2.output
    assert "kedb current" in out2.output  # no new ids seeded the second pass

    # KEDB unchanged (no duplicates), and the human ratification survived.
    assert _kedb_ids(home) == first_kedb
    spec = store.read_spec(paths, "operatorapp", "skchat")["spec"]
    assert "restart-daemon" in spec["ratifiedStandardActions"]


def test_apps_register_list_and_ratify(tmp_path, monkeypatch):
    _redirect_kedb_home(tmp_path, monkeypatch)
    _enroll(tmp_path, monkeypatch)
    r = CliRunner()

    # Nothing registered yet.
    assert "no registered apps" in r.invoke(cli.operator, ["apps", "list"]).output

    # Register all six app adapters.
    reg = r.invoke(cli.operator, ["apps", "register"])
    assert reg.exit_code == 0
    assert "skchat" in reg.output and "skcode" in reg.output

    # List shows them, none ratified yet.
    listed = r.invoke(cli.operator, ["apps", "list"]).output
    assert "skchat" in listed
    assert "0/2 ratified" in listed  # skchat proposes 2, none ratified

    # Human ratifies one.
    rat = r.invoke(cli.operator, ["apps", "ratify", "skchat", "restart-daemon"])
    assert rat.exit_code == 0 and "auto-standard" in rat.output

    # Ratifying an unproposed action is a clean error, not a crash.
    bad = r.invoke(cli.operator, ["apps", "ratify", "skchat", "purge-outbox"])
    assert bad.exit_code != 0
