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

FILES_YAML = "files:\n  - path: ~/.hermes/.env\n    mode: block\n    source: hermes.env\n"
UNIT = "[Service]\nExecStart=/bin/true\n"
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
    args = ("plan", "--manifest", str(manifest), "--node", "chiap04")
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
