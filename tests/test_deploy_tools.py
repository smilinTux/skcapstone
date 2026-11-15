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


def test_export_lacking_a_unit_script_is_refused_before_publishing(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    (units / "gone.service").write_text(
        f"[Service]\nExecStart=/usr/bin/python3 {home}/src/thing/scripts/removed.py\n"
    )
    with pytest.raises(tools.ToolError, match="thing/scripts/removed.py"):
        _apply(tmp_path, home, units, manifest)
    assert not (home / ".local/share/sk-tools/thing/current").exists()
    assert not list(units.glob(f"*.service.d/{tools.DROPIN}"))


def test_checkout_list_rewrites_every_location(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    manifest.tools["thing"].checkout = [str(home / "src/thing"), str(home / "elsewhere/thing")]
    (units / "alt.service").write_text(
        f"[Service]\nExecStart=/usr/bin/python3 {home}/elsewhere/thing/scripts/relay.py\n"
    )
    pairs = tools.rewrite_pairs(manifest, None, home / ".local/share/sk-tools", home)
    want = tools.desired_dropins(units, pairs)
    assert set(want) == {"relay.service", "alt.service"}
    assert "%h/.local/share/sk-tools/thing/current/scripts/relay.py" in want["alt.service"]


def test_stop_and_reload_lines_follow_the_export(tmp_path, origin):
    """ExecStop/ExecStopPost/ExecReload must not keep running the checkout.

    hermes-gateway 2026-11-15: only ExecStart was re-stated, so the stop of the
    switch ran the checkout's launcher, which finished a source update there and
    timed the stop out.
    """
    home, units, manifest = _setup(tmp_path, origin)
    (units / "gw.service").write_text(
        "[Service]\n"
        f'ExecStart="{home}/src/thing/scripts/relay.py" run\n'
        f'ExecStop=-"{home}/src/thing/scripts/relay.py" stop-mark\n'
        f'ExecStopPost=-"{home}/src/thing/scripts/relay.py" cleanup\n'
        "ExecReload=/bin/kill -USR1 $MAINPID\n"
    )
    pairs = tools.rewrite_pairs(manifest, None, home / ".local/share/sk-tools", home)
    text = tools.desired_dropins(units, pairs)["gw.service"]
    cur = "%h/.local/share/sk-tools/thing/current/scripts/relay.py"
    assert f'ExecStop=\nExecStop=-"{cur}" stop-mark' in text
    assert f'ExecStopPost=\nExecStopPost=-"{cur}" cleanup' in text
    assert "ExecReload" not in text  # names nothing under the checkout
    assert str(home / "src/thing") not in text


def test_managed_dropin_wins_over_a_late_hand_dropin(tmp_path, origin):
    from skcapstone.deploy.inspect import _unit_settings

    home, units, manifest = _setup(tmp_path, origin)
    (units / "relay.service.d").mkdir()
    (units / "relay.service.d/zz-skenv-call.conf").write_text(
        f"[Service]\nExecStart=\nExecStart=/opt/py {home}/src/thing/scripts/relay.py\n"
    )
    _apply(tmp_path, home, units, manifest)
    effective = _unit_settings(units / "relay.service")["ExecStart"]
    assert effective == ["/opt/py %h/.local/share/sk-tools/thing/current/scripts/relay.py"]


def test_legacy_dropin_name_is_migrated_and_restored_on_undo(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    legacy = units / "relay.service.d" / "zz-sk-deploy-tools.conf"
    legacy.parent.mkdir()
    legacy.write_text("[Service]\nWorkingDirectory=/old\n")
    change = _apply(tmp_path, home, units, manifest)
    assert not legacy.exists() and (units / "relay.service.d" / tools.DROPIN).is_file()
    change.undo(home / ".local/share/sk-tools", units)
    assert legacy.read_text() == "[Service]\nWorkingDirectory=/old\n"
    assert not (units / "relay.service.d" / tools.DROPIN).exists()


def test_empty_dot_git_directory_is_not_a_tree(tmp_path):
    from skcapstone.deploy.inspect import exec_paths_in_trees, git_tree_of

    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (home / ".git").mkdir()
    (home / "bin").mkdir()
    (home / "bin/run.sh").write_text("#!/bin/sh\n")
    (units / "a.service").write_text("[Service]\nExecStart=/bin/sh %h/bin/run.sh\n")
    assert git_tree_of(str(home / "bin/run.sh")) is None
    assert exec_paths_in_trees(home, units) == []
    _git(home, "init", "-q", "-b", "main")
    assert git_tree_of(str(home / "bin/run.sh")) == home


def _real_runner(cmd, **kw):
    """git and the tool's setup run for real; systemctl is stubbed."""
    if cmd[:1] == ["systemctl"]:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, **kw)


def _apply_real(tmp_path, home, units, manifest):
    return tools.apply_tools(
        manifest,
        None,
        tmp_path / "state",
        root=home / ".local/share/sk-tools",
        unit_dir=units,
        home=home,
        runner=_real_runner,
    )


def test_setup_runs_in_the_final_export_before_publishing(tmp_path, origin):
    """A launcher the setup generates (bin/launch) satisfies a unit that runs it."""
    home, units, manifest = _setup(tmp_path, origin)
    (units / "gen.service").write_text(f"[Service]\nExecStart={home}/src/thing/bin/launch\n")
    manifest.tools["thing"].setup = [
        "/bin/sh",
        "-c",
        'mkdir -p bin && printf "%s|%s" "$PWD" "$MARK" > bin/launch',
    ]
    manifest.tools["thing"].setup_env = {"MARK": "~/m"}
    change = _apply_real(tmp_path, home, units, manifest)
    root = home / ".local/share/sk-tools/thing"
    export = root / change.exported["thing"]
    where, mark = (root / "current/bin/launch").read_text().split("|")
    assert Path(where) == export  # at its final path, not the staging directory
    assert mark == str(Path("~/m").expanduser())
    assert tools.installed(root.parent, "thing")["setup"][0] == "/bin/sh"
    assert "gen.service" in change.units


def test_failed_setup_publishes_nothing_and_leaves_no_export(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    manifest.tools["thing"].setup = ["/bin/sh", "-c", "touch half && echo boom >&2 && exit 3"]
    with pytest.raises(tools.ToolError, match=r"setup failed \(3\): boom"):
        _apply_real(tmp_path, home, units, manifest)
    root = home / ".local/share/sk-tools/thing"
    assert not (root / "current").exists()
    assert not [p for p in root.iterdir() if not p.name.startswith(".")]
    assert not list(units.glob(f"*.service.d/{tools.DROPIN}"))


def test_setup_runs_once_per_export(tmp_path, origin):
    home, units, manifest = _setup(tmp_path, origin)
    counter = tmp_path / "count"
    manifest.tools["thing"].setup = ["/bin/sh", "-c", f"echo x >> {counter}"]
    _apply_real(tmp_path, home, units, manifest)
    again = _apply_real(tmp_path, home, units, manifest)
    assert again.empty
    assert counter.read_text() == "x\n"


def test_unfinished_setup_export_is_rebuilt(tmp_path, origin):
    """An export directory without metadata (a setup killed mid-run) is redone."""
    home, units, manifest = _setup(tmp_path, origin)
    manifest.tools["thing"].setup = ["/bin/sh", "-c", "touch done"]
    first = _apply_real(tmp_path, home, units, manifest)
    export = home / ".local/share/sk-tools/thing" / first.exported["thing"]
    (export / tools._META).unlink()
    (export / "stale").write_text("left over\n")
    _apply_real(tmp_path, home, units, manifest)
    assert (export / "done").exists() and not (export / "stale").exists()
