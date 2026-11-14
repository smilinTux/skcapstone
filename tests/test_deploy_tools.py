"""Tool exports: scripts units run, exported from main and pointed at by drop-ins."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skcapstone.deploy import drift as drift_mod
from skcapstone.deploy import tools
from skcapstone.deploy.manifest import Manifest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A repository with a script on main and another only on a side branch."""
    repo = tmp_path / "origin"
    (repo / "scripts").mkdir(parents=True)
    (repo / "scripts/relay.py").write_text("print('main')\n")
    (repo / "README.md").write_text("x\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "main")
    _git(repo, "checkout", "-qb", "side")
    (repo / "scripts/relay.py").write_text("print('side')\n")
    _git(repo, "commit", "-qam", "side")
    _git(repo, "checkout", "-q", "main")
    return repo


def _setup(tmp_path: Path, origin: Path, ref: str = "main"):
    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "relay.service").write_text(
        "[Service]\nWorkingDirectory=%h/src/thing\n"
        "ExecStart=/usr/bin/python3 %h/src/thing/scripts/relay.py --port 1\n"
        "Environment=PYTHONPATH=%h/src/thing:%h/other\n"
    )
    (units / "unrelated.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    manifest = Manifest.model_validate(
        {
            "packages": {},
            "tools": {
                "thing": {
                    "url": f"file://{origin}",
                    "ref": ref,
                    "checkout": str(home / "src/thing"),
                }
            },
        }
    )
    return home, units, manifest


def _no_systemd(cmd, **_kw):
    if cmd[:1] == ["git"]:
        return subprocess.run(cmd, **_kw)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _apply(tmp_path, home, units, manifest):
    return tools.apply_tools(
        manifest,
        None,
        tmp_path / "state",
        root=home / ".local/share/sk-tools",
        unit_dir=units,
        home=home,
        runner=_no_systemd,
    )


def test_export_publishes_and_rewrites_the_unit(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    change = _apply(tmp_path, home, units, manifest)
    current = home / ".local/share/sk-tools/thing/current"
    assert (current / "scripts/relay.py").read_text() == "print('main')\n"
    dropin = (units / "relay.service.d" / tools.DROPIN).read_text()
    assert (
        "ExecStart=\nExecStart=/usr/bin/python3 %h/.local/share/sk-tools/thing/current" in dropin
    )
    assert "WorkingDirectory=%h/.local/share/sk-tools/thing/current\n" in dropin
    assert "Environment=PYTHONPATH=%h/.local/share/sk-tools/thing/current:%h/other" in dropin
    assert not (units / "unrelated.service.d").exists()
    assert change.units == ["relay.service"]
    assert tools.installed(home / ".local/share/sk-tools", "thing")["ref"] == "main"


def test_second_apply_changes_nothing(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    _apply(tmp_path, home, units, manifest)
    again = _apply(tmp_path, home, units, manifest)
    assert again.empty and again.units == []


def test_ref_not_on_main_is_refused(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin, ref="side")
    with pytest.raises(tools.ToolError, match="not on main"):
        _apply(tmp_path, home, units, manifest)
    assert not (units / "relay.service.d").exists()
    assert not (home / ".local/share/sk-tools/thing/current").exists()


def test_undo_restores_previous_state(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    change = _apply(tmp_path, home, units, manifest)
    change.undo(home / ".local/share/sk-tools", units)
    assert not (units / "relay.service.d" / tools.DROPIN).exists()
    assert not (home / ".local/share/sk-tools/thing/current").exists()


def test_removed_tool_drops_its_managed_dropin(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    _apply(tmp_path, home, units, manifest)
    empty = Manifest.model_validate({"packages": {}})
    change = _apply(tmp_path, home, units, empty)
    assert not (units / "relay.service.d" / tools.DROPIN).exists()
    assert change.units == ["relay.service"]


def test_paths_limit_the_rewrite(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    manifest.tools["thing"].paths = ["scripts/relay.py"]
    _apply(tmp_path, home, units, manifest)
    dropin = (units / "relay.service.d" / tools.DROPIN).read_text()
    assert "sk-tools/thing/current/scripts/relay.py --port 1" in dropin
    assert "WorkingDirectory" not in dropin
    exported = home / ".local/share/sk-tools/thing/current"
    assert (exported / "scripts/relay.py").is_file() and not (exported / "README.md").exists()


def test_path_boundary_is_respected(tmp_path):
    pairs = [("/h/src/thing", "/x/current")]
    assert tools._substitute("/h/src/thing2/a.py", pairs) == "/h/src/thing2/a.py"
    assert tools._substitute("/h/src/thing/a.py", pairs) == "/x/current/a.py"
    assert tools._substitute('"/h/src/thing"', pairs) == '"/x/current"'


def test_drift_reports_unexported_tool_and_clears_after_apply(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    root = home / ".local/share/sk-tools"

    def report():
        return drift_mod.compute(
            home, Path("/x/python"), manifest, unit_dir=units, tools_root=root, runner=_raise
        )

    before = report()
    assert before.tools == ["thing not exported (manifest main)"]
    _apply(tmp_path, home, units, manifest)
    assert report().tools == []


def _raise(*_a, **_k):
    raise OSError("no systemd")


def test_tool_needs_a_source():
    with pytest.raises(ValueError, match="package, repo or url"):
        Manifest.model_validate({"packages": {}, "tools": {"t": {"checkout": "~/x", "ref": "a"}}})
    with pytest.raises(ValueError, match="relative"):
        Manifest.model_validate(
            {
                "packages": {},
                "tools": {
                    "t": {"repo": "o/r", "ref": "a", "checkout": "~/x", "paths": ["../etc"]}
                },
            }
        )


def _systemctl(units_out: str, files_out: str):
    def run(cmd, **_kw):
        out = units_out if "list-units" in cmd else files_out
        return subprocess.CompletedProcess(cmd, 0, out, "")

    return run


def test_live_units_counts_enabled_active_triggered_and_templates(tmp_path):
    from skcapstone.deploy.inspect import live_units

    for name in ["a", "b", "c", "dead", "t@"]:
        (tmp_path / f"{name}.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    run = _systemctl(
        "b.service loaded active running x\nt@x.service loaded active running y\n",
        "a.service enabled enabled\nc.timer enabled enabled\ndead.service disabled enabled\n",
    )
    assert live_units(tmp_path, run) == {"a.service", "b.service", "c.service", "t@.service"}


def test_live_units_unknown_without_systemd(tmp_path):
    from skcapstone.deploy.inspect import live_units

    (tmp_path / "a.service").write_text("[Service]\n")
    assert live_units(tmp_path, _raise) is None


def test_missing_paths_checks_program_script_and_workdir(tmp_path):
    from skcapstone.deploy.inspect import unit_missing_paths

    home = tmp_path / "h"
    (home / "ok").mkdir(parents=True)
    (tmp_path / "u.service").write_text(
        "[Service]\nWorkingDirectory=%h/gone\n"
        "ExecStart=/usr/bin/python3 -u %h/ok/missing.py remote:/not/a/path %h/out.log\n"
    )
    (tmp_path / "m.service").write_text("[Service]\nExecStart=/usr/bin/python3 -m pkg.mod\n")
    found = sorted(ep.path for ep in unit_missing_paths(tmp_path, home))
    assert found == [str(home / "gone"), str(home / "ok/missing.py")]


def test_continuation_lines_are_joined(tmp_path):
    from skcapstone.deploy.inspect import unit_exec_paths

    home = tmp_path / "h"
    (tmp_path / "c.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 \\\n    %h/src/thing/a.py \\\n    --flag\n"
    )
    assert [ep.path for ep in unit_exec_paths(tmp_path, home)] == [str(home / "src/thing/a.py")]


def test_dotfiles_repo_at_home_counts_only_tracked_paths(tmp_path):
    from skcapstone.deploy.inspect import exec_paths_in_trees

    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (home / "bin").mkdir()
    (home / "bin/tracked.sh").write_text("#!/bin/sh\n")
    (home / "bin/untracked.sh").write_text("#!/bin/sh\n")
    _git(home, "init", "-q", "-b", "main")
    _git(home, "add", "bin/tracked.sh")
    _git(home, "commit", "-qm", "dotfiles")
    (units / "a.service").write_text(
        "[Service]\nWorkingDirectory=%h\nExecStart=/bin/sh %h/bin/tracked.sh\n"
    )
    (units / "b.service").write_text("[Service]\nExecStart=/bin/sh %h/bin/untracked.sh\n")
    hits = sorted((ep.origin, ep.path) for ep, _t in exec_paths_in_trees(home, units))
    assert hits == [("unit a.service", str(home / "bin/tracked.sh"))]
