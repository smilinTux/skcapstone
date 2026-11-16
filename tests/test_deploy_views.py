"""``deploy plan``, ``status`` and ``drift`` for stacks and managed files (Task 5)."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import click
import pytest
import yaml
from click.testing import CliRunner

from skcapstone.deploy import drift, files, rollout
from skcapstone.deploy.tools import ToolsChange
from tests.test_deploy_stacks import _cluster, _sh, _stack_yaml


@pytest.fixture(autouse=True)
def _no_xdg_config_home(monkeypatch):
    """Unit files follow XDG_CONFIG_HOME, which GitHub runners set: keep them in the test HOME."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


FILES_YAML = "files:\n  - path: ~/.hermes/.env\n    mode: block\n    source: hermes.env\n"
UNIT = "[Service]\nExecStart=@TOOL@/bin/run\n"
BLOCK = "sk-deploy"


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    stack = tmp_path / "sidekick"
    stack.mkdir()
    (stack / "stack.yaml").write_text(yaml.safe_dump(_stack_yaml("0.1.0"), sort_keys=False))
    _sh(stack, "init", "-q", "-b", "main")
    _sh(stack, "add", ".")
    _sh(stack, "commit", "-qm", "v0.1.0")
    _sh(stack, "tag", "v0.1.0")
    cfg = tmp_path / "cfg"
    (cfg / "chiap04/systemd").mkdir(parents=True)
    (cfg / "chiap04/files.yaml").write_text(FILES_YAML)
    (cfg / "chiap04/hermes.env").write_text("HERMES_MEMORY_OWNER=a\n")
    (cfg / "chiap04/systemd/hermes-gateway.service").write_text(UNIT)
    _sh(cfg, "init", "-q", "-b", "main")
    _sh(cfg, "add", ".")
    _sh(cfg, "commit", "-qm", "init")
    data = _cluster(f"file://{stack}")
    data["nodes"]["chiap04"]["configs"] = {
        "hermes": {"url": f"file://{cfg}", "ref": "main", "instance": "chiap04"}
    }
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(yaml.safe_dump(data, sort_keys=False))
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    (home / ".hermes/.env").write_text("OPENAI_API_KEY=x\n")
    (home / ".config/systemd/user").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("SK_DEPLOY_MANIFEST_URL", raising=False)
    monkeypatch.delenv("SK_DEPLOY_MANIFEST_REPO", raising=False)
    paths = rollout.Paths(
        tmp_path / ".skenv",
        tmp_path / ".skenv-releases",
        tmp_path / "state",
        home / ".local/share/sk-tools",
    )
    return manifest, home, paths, stack, cfg


def _run(monkeypatch, paths, *args):
    from skcapstone.cli import deploy_cmd

    monkeypatch.setattr(deploy_cmd, "_paths", lambda: paths)
    group = click.Group()
    deploy_cmd.register_deploy_commands(group)
    return CliRunner().invoke(group, ["deploy", *args])


def _hashes(*roots: Path, skip=("stacks", "tool-src", "manifest-repo")) -> dict[str, str]:
    out: dict[str, str] = {}
    for root in roots:
        for p in sorted(root.rglob("*")) if root.exists() else []:
            if any(part in skip for part in p.relative_to(root).parts):
                continue
            key = str(p)
            if p.is_symlink():
                out[key] = "link " + os.readlink(p)
            elif p.is_file():
                out[key] = hashlib.sha256(p.read_bytes()).hexdigest()
            else:
                out[key] = "dir"
    return out


def test_plan_changes_nothing(world, monkeypatch, tmp_path):
    manifest, home, paths, _stack, _cfg = world
    # --no-resolve: the resolve step (uv) fills its cache under $HOME, which is
    # not node state; this test is about what the stack plan writes.
    args = ("plan", "--manifest", str(manifest), "--node", "chiap04", "--no-resolve")
    assert _run(monkeypatch, paths, *args).exit_code == 0  # mirrors now exist
    before = _hashes(home, paths.state_dir)
    tmpdirs = set(Path("/tmp").glob("sk-deploy-plan-*"))
    out = _run(monkeypatch, paths, *args)
    assert out.exit_code == 0, out.output
    assert _hashes(home, paths.state_dir) == before
    assert set(Path("/tmp").glob("sk-deploy-plan-*")) == tmpdirs  # scratch removed


def test_plan_shows_roles_and_file_diff(world, monkeypatch):
    manifest, home, paths, stack, _cfg = world
    sha = _sh(stack, "rev-parse", "v0.1.0^{commit}")
    out = _run(monkeypatch, paths, "plan", "--manifest", str(manifest), "--node", "chiap04")
    assert out.exit_code == 0, out.output
    assert "sidekick" in out.output and "v0.1.0" in out.output
    assert "agent" in out.output
    assert "+HERMES_MEMORY_OWNER=a" in out.output
    assert "hermes-gateway.service" in out.output
    out = _run(
        monkeypatch, paths, "plan", "--manifest", str(manifest), "--node", "chiap04", "--json"
    )
    data = json.loads(out.output)
    assert data["stacks"]["sidekick"] == {"ref": "v0.1.0", "sha": sha}
    assert data["roles"] == {"sidekick": ["agent"]}
    diffs = data["files"]["hermes-config"]["diffs"]
    assert len(diffs) == 1 and "+HERMES_MEMORY_OWNER=a" in diffs[0]
    assert data["restarts"] == ["hermes-gateway.service"]


def _record(world_, extra: str = "") -> Path:
    """Write the managed file the way an apply does, and save its change record."""
    _manifest, home, paths, _stack, cfg = world_
    inst = cfg / "chiap04"
    change = ToolsChange()
    files.apply_files(
        files.load_files(inst),
        inst,
        home,
        paths.state_dir,
        undo=change.files,
        written=change.written,
    )
    return rollout.save_change(paths, change, "m" * 40, home / ".config/systemd/user")


def _drift(world_, monkeypatch):
    _manifest, home, paths, *_ = world_
    runner = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "")  # noqa: E731
    return drift.compute(
        home,
        paths.venv_link / "bin" / "python",
        None,
        unit_dir=home / ".config/systemd/user",
        state_dir=paths.state_dir,
        runner=runner,
    )


def test_drift_flags_edit_inside_block_not_outside(world, monkeypatch):
    _manifest, home, paths, *_ = world
    env = home / ".hermes/.env"
    assert _drift(world, monkeypatch).files == []  # no record: nothing to compare
    _record(world)
    assert _drift(world, monkeypatch).files == []
    env.write_text("# mine\n" + env.read_text() + "LATER=1\n")  # outside the block
    assert _drift(world, monkeypatch).files == []
    env.write_text(env.read_text().replace("OWNER=a", "OWNER=hand"))  # inside it
    report = _drift(world, monkeypatch)
    assert len(report.files) == 1 and str(env) in report.files[0]
    assert report.lines() == [f"files: {report.files[0]}"]
    env.unlink()
    assert "missing" in _drift(world, monkeypatch).files[0]


def test_status_shows_last_tools_change(world, monkeypatch):
    _manifest, home, paths, *_ = world
    out = _run(monkeypatch, paths, "status")
    assert "last_tools_change" not in out.output
    change = ToolsChange(exported={"hermes": "bbbbbbbbbbbb"}, published={"hermes": "aaaaaaaaaaaa"})
    change.units = ["hermes-gateway.service"]
    rollout.save_change(paths, change, "c" * 40, home / ".config/systemd/user")
    out = _run(monkeypatch, paths, "status")
    assert out.exit_code == 0, out.output
    body = out.output[out.output.index("{") :]
    last = json.loads(body)["last_tools_change"]
    assert last["manifest"] == "c" * 40
    assert last["tools"]["hermes"] == {"now": "bbbbbbbbbbbb", "before": "aaaaaaaaaaaa"}
    assert last["when"] and last["at"] > 0


def _install_meta(root: Path, name: str, ref: str) -> None:
    """A published export of *name* at *ref* (just its metadata)."""
    dest = root / name / "aaaaaaaaaaaa"
    dest.mkdir(parents=True)
    (dest / ".sk-tool.json").write_text(json.dumps({"name": name, "ref": ref}))
    os.symlink("aaaaaaaaaaaa", root / name / "current")


def test_plan_lists_tool_moves_and_their_units(world, monkeypatch):
    manifest, home, paths, *_ = world
    args = ("plan", "--manifest", str(manifest), "--node", "chiap04")
    out = _run(monkeypatch, paths, *args)
    assert "tool hermes: - -> v1" in out.output
    assert "nothing to do" not in out.output
    _install_meta(paths.tools_root, "hermes", "v0")  # a stack bump moves hermes
    data = json.loads(_run(monkeypatch, paths, *args, "--json").output)
    assert data["tools"]["hermes"] == {"from": "v0", "to": "v1"}
    assert "hermes-gateway.service" in data["restarts"]


def _settle(world_) -> None:
    """Put the node where an apply of the world would leave it, except the tool exports."""
    from skcapstone.deploy import views
    from skcapstone.deploy.manifest import Manifest
    from skcapstone.deploy.tools import render_units

    manifest_path, home, paths, _stack, cfg = world_
    from skcapstone.deploy import resolve_manifest

    m, _ = resolve_manifest(paths.state_dir, path=str(manifest_path), node="chiap04")
    cfg_spec = m.configs_for("chiap04")["hermes"]
    tmp, _sha, _ref = views._unpack(
        "hermes-config", cfg_spec, paths.state_dir / "x.git", subprocess.run
    )
    try:
        for unit, text in render_units(
            m, "chiap04", paths.tools_root, home, {"hermes-config": tmp}
        ).items():
            (home / ".config/systemd/user" / unit).write_text(text)
    finally:
        import shutil

        shutil.rmtree(tmp)
    _record(world_)
    assert isinstance(m, Manifest)


def test_plan_restarts_only_what_apply_would(world, monkeypatch):
    manifest, home, paths, *_ = world
    args = ("plan", "--manifest", str(manifest), "--node", "chiap04", "--json")
    _settle(world)
    _install_meta(paths.tools_root, "hermes", "v1")
    data = json.loads(_run(monkeypatch, paths, *args).output)
    assert data["tools"].get("hermes") is None and data["restarts"] == []
    # the config's units do not restart just because a tool of the node moves
    # unless they name its current link: this one does
    (paths.tools_root / "hermes/current").unlink()
    _install_meta_again = paths.tools_root / "hermes/bbbbbbbbbbbb"
    _install_meta_again.mkdir()
    (_install_meta_again / ".sk-tool.json").write_text(json.dumps({"ref": "v0"}))
    os.symlink("bbbbbbbbbbbb", paths.tools_root / "hermes/current")
    data = json.loads(_run(monkeypatch, paths, *args).output)
    assert data["tools"]["hermes"]["from"] == "v0"
    assert data["restarts"] == ["hermes-gateway.service"]


# ------------------------------------------------- plan and apply restart the same units


def _tool_repo(path: Path) -> Path:
    path.mkdir()
    (path / "bin").mkdir()
    (path / "bin/run").write_text("#!/bin/sh\n")
    _sh(path, "init", "-q", "-b", "main")
    _sh(path, "add", ".")
    _sh(path, "commit", "-qm", "v1")
    _sh(path, "tag", "v1")
    (path / "bin/run").write_text("#!/bin/sh\n# 2\n")
    _sh(path, "commit", "-qam", "v2")
    _sh(path, "tag", "v2")
    return path


def _ordered_manifest(fw: Path, cfg: Path, ref: str):
    from skcapstone.deploy.manifest import Manifest

    return Manifest.model_validate(
        {
            "packages": {},
            "tools": {"fw": {"url": f"file://{fw}", "ref": ref}},
            # stack order: skcall first, the gateway last (sorted() would flip them)
            "services": {"skcall.service": {}, "hermes-gateway.service": {}},
            "nodes": {
                "n1": {
                    "tools": ["fw"],
                    "configs": {"fw": {"url": f"file://{cfg}", "ref": "main", "instance": "i"}},
                }
            },
        }
    )


def test_apply_restarts_in_stack_order_and_plan_matches(tmp_path, monkeypatch):
    from skcapstone.deploy import tools, views

    fw = _tool_repo(tmp_path / "fw")
    cfg = tmp_path / "cfg"
    (cfg / "i/systemd").mkdir(parents=True)
    for unit in ("hermes-gateway.service", "skcall.service"):
        (cfg / "i/systemd" / unit).write_text(UNIT)
    _sh(cfg, "init", "-q", "-b", "main")
    _sh(cfg, "add", ".")
    _sh(cfg, "commit", "-qm", "i")
    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    state, root = tmp_path / "state", home / ".local/share/sk-tools"
    ok = lambda cmd, **kw: (  # noqa: E731
        subprocess.run(cmd, **kw)
        if cmd[0] == "git"
        else subprocess.CompletedProcess(cmd, 0, "", "")
    )

    def both(manifest):
        plan = views.tools_plan(manifest, "n1", state, [], home=home, root=root, runner=ok)
        change = tools.apply_tools(manifest, "n1", state, root, units, home, ok)
        return plan["restarts"], change.units

    planned, applied = both(_ordered_manifest(fw, cfg, "v1"))
    assert applied == ["skcall.service", "hermes-gateway.service"]
    assert planned == applied
    # the tool moves, no unit text changes: both name its current link
    planned, applied = both(_ordered_manifest(fw, cfg, "v2"))
    assert applied == ["skcall.service", "hermes-gateway.service"]
    assert planned == applied
    # nothing moves: nothing restarts, in both
    planned, applied = both(_ordered_manifest(fw, cfg, "v2"))
    assert planned == applied == []


# ------------------------------------------------- final review: I4 (FileSpec restart), F4


def _files_world(tmp_path, monkeypatch, files_yaml: str, units: dict[str, str] | None = None):
    fw = _tool_repo(tmp_path / "fw")
    cfg = tmp_path / "cfg"
    (cfg / "i").mkdir(parents=True)
    (cfg / "i/files.yaml").write_text(files_yaml)
    (cfg / "i/join.env").write_text("JOIN=a\n")
    for name, text in (units or {}).items():
        (cfg / "i/systemd").mkdir(exist_ok=True)
        (cfg / "i/systemd" / name).write_text(text)
    _sh(cfg, "init", "-q", "-b", "main")
    _sh(cfg, "add", ".")
    _sh(cfg, "commit", "-qm", "i")
    home = tmp_path / "home"
    unit_dir = home / ".config/systemd/user"
    unit_dir.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    state, root = tmp_path / "state", home / ".local/share/sk-tools"
    return fw, cfg, home, unit_dir, state, root


def _ok(cmd, **kw):
    if cmd[0] == "git":
        return subprocess.run(cmd, **kw)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _plan_and_apply(manifest, home, unit_dir, state, root):
    from skcapstone.deploy import tools, views

    plan = views.tools_plan(manifest, "n1", state, [], home=home, root=root, runner=_ok)
    change = tools.apply_tools(manifest, "n1", state, root, unit_dir, home, _ok)
    return plan, change


JOIN_RESTART = (
    "files:\n  - path: ~/.config/skcall/join.env\n    mode: whole\n    source: join.env\n"
    "    restart: [skcall.service]\n"
)


def test_files_only_instance_restarts_the_units_its_file_names(tmp_path, monkeypatch):
    fw, cfg, home, unit_dir, state, root = _files_world(tmp_path, monkeypatch, JOIN_RESTART)
    m = _ordered_manifest(fw, cfg, "v1")
    plan, change = _plan_and_apply(m, home, unit_dir, state, root)
    assert "skcall.service" in change.units
    assert plan["restarts"] == change.units
    # nothing changed: nothing restarts
    _install = _plan_and_apply(m, home, unit_dir, state, root)
    assert _install[0]["restarts"] == _install[1].units == []
    # a mode change alone is a change, in both
    os.chmod(home / ".config/skcall/join.env", 0o644)
    plan, change = _plan_and_apply(m, home, unit_dir, state, root)
    assert plan["restarts"] == change.units == ["skcall.service"]


def test_file_change_nobody_restarts_for_is_noted(tmp_path, monkeypatch):
    plain = JOIN_RESTART.replace("    restart: [skcall.service]\n", "")
    fw, cfg, home, unit_dir, state, root = _files_world(tmp_path, monkeypatch, plain)
    m = _ordered_manifest(fw, cfg, "v1")
    join = home / ".config/skcall/join.env"
    want = f"{join} changed; no unit restarts for it"
    plan, change = _plan_and_apply(m, home, unit_dir, state, root)
    assert want in plan["notes"] and want in change.notes
    assert plan["restarts"] == change.units == []
    paths = rollout.Paths(tmp_path / ".skenv", tmp_path / "rel", state, root)
    join.write_text("JOIN=hand\n")
    res = rollout.apply_tools_only(m, paths, runner=_ok, node="n1")
    assert res.ok and want in res.notes


def test_plan_errors_are_top_level_and_mark_restarts_incomplete(tmp_path, monkeypatch):
    fw, cfg, home, unit_dir, state, root = _files_world(
        tmp_path,
        monkeypatch,
        JOIN_RESTART,
        units={"skcall.service": "[Service]\nExecStart=@BOGUS@/run\n"},
    )
    from skcapstone.deploy import views

    m = _ordered_manifest(fw, cfg, "v1")
    plan = views.tools_plan(m, "n1", state, ["pkg.service"], home=home, root=root, runner=_ok)
    assert "units" not in plan["files"]
    assert plan["errors"] and "@BOGUS@" in plan["errors"][0]
    assert plan["restarts_complete"] is False
    # and in the CLI
    from skcapstone.deploy.rollout import Paths

    paths = Paths(tmp_path / ".skenv", tmp_path / "rel", state, root)
    from skcapstone.cli import deploy_cmd

    monkeypatch.setattr(deploy_cmd, "_manifest", lambda *a, **k: (m, "c" * 40))
    real = views.tools_plan
    monkeypatch.setattr(
        views, "tools_plan", lambda *a, **k: real(*a, **{**k, "home": home, "root": root})
    )
    out = _run(monkeypatch, paths, "plan", "--node", "n1", "--json")
    data = json.loads(out.output)
    assert "units" not in data["files"] and data["errors"] and data["restarts_complete"] is False
    out = _run(monkeypatch, paths, "plan", "--node", "n1")
    assert "restarts: incomplete, see errors" in out.output
    assert "@BOGUS@" in out.output
    # a config that cannot be read is an error too, not an empty restart list
    bad = _ordered_manifest(fw, tmp_path / "nowhere", "v1")
    plan = views.tools_plan(bad, "n1", state, [], home=home, root=root, runner=_ok)
    assert plan["errors"] and plan["restarts_complete"] is False
