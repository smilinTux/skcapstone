"""Rollback of the tools step on nodes that run only tool exports (Task 4)."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from skcapstone.deploy import rollout, tools
from skcapstone.deploy.manifest import Manifest


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path, content: dict[str, str]) -> Path:
    for rel, text in content.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


def _commit(repo: Path, content: dict[str, str]) -> None:
    for rel, text in content.items():
        (repo / rel).write_text(text)
    _git(repo, "commit", "-qam", "bump")


UNIT_A = "[Service]\nExecStart=/usr/bin/node @TOOL@/src/index.mjs\n"
UNIT_B = "[Service]\nExecStart=/usr/bin/node @TOOL@/src/index.mjs --b\n"
FILES_YAML = (
    "files:\n"
    "  - path: ~/.hermes/.env\n    mode: block\n    source: hermes.env\n"
    "  - path: ~/.config/skcall/join.env\n    mode: whole\n    source: join.env\n"
    "  - path: ~/.hermes/plugins/fw\n    mode: link\n    target: '@TOOL:fw@/src'\n"
)


def _manifest(fw: Path, cfg: Path, packages: dict | None = None) -> Manifest:
    return Manifest.model_validate(
        {
            "packages": packages or {},
            "tools": {"fw": {"url": f"file://{fw}", "ref": "main"}},
            "services": {"gw.service": {"smoke": "true"}},
            "nodes": {
                "n1": {
                    "tools": ["fw"],
                    "configs": {
                        "fw": {"url": f"file://{cfg}", "ref": "main", "instance": "node1"}
                    },
                }
            },
        }
    )


class Calls:
    """A runner: git for real, systemd answered (every unit stays active)."""

    def __init__(self):
        self.cmds: list[list[str]] = []

    def __call__(self, cmd, **kw):
        if cmd[:1] == ["git"]:
            return subprocess.run(cmd, **kw)
        self.cmds.append(list(cmd))
        if "is-active" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "active\n", "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


@pytest.fixture
def world(tmp_path: Path, monkeypatch):
    fw = _repo(tmp_path / "fw", {"src/index.mjs": "// a\n"})
    cfg = _repo(
        tmp_path / "cfg",
        {
            "node1/systemd/gw.service": UNIT_A,
            "node1/files.yaml": FILES_YAML,
            "node1/hermes.env": "HERMES_MEMORY_OWNER=a\n",
            "node1/join.env": "JOIN=a\n",
        },
    )
    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (home / ".hermes").mkdir()
    (home / ".hermes/.env").write_text("OPENAI_API_KEY=x\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(rollout.time, "sleep", lambda _s: None)
    paths = rollout.Paths(
        tmp_path / ".skenv",
        tmp_path / ".skenv-releases",
        tmp_path / "state",
        home / ".local/share/sk-tools",
    )
    return fw, cfg, home, units, paths


def _apply(manifest, paths, mid="m" * 40, runner=None):
    res = rollout.apply_tools_only(
        manifest, paths, runner=runner or Calls(), node="n1", manifest_id=mid
    )
    assert res.ok, res.problems
    return res


def _snapshot(home: Path) -> dict[str, object]:
    """Every file and link under *home*; of the tool exports only the ``current`` links."""
    out: dict[str, object] = {}
    for p in sorted(home.rglob("*")):
        if "/.local/share/sk-tools/" in str(p) and p.name != "current":
            continue
        if p.is_symlink():
            out[str(p)] = ("link", os.readlink(p))
        elif p.is_file():
            out[str(p)] = ("file", p.read_bytes(), stat.S_IMODE(p.stat().st_mode))
    return out


def _bump_to_b(fw: Path, cfg: Path) -> None:
    _commit(fw, {"src/index.mjs": "// b\n"})
    _commit(
        cfg,
        {
            "node1/systemd/gw.service": UNIT_B,
            "node1/hermes.env": "HERMES_MEMORY_OWNER=b\n",
            "node1/join.env": "JOIN=b\n",
        },
    )


def test_rollback_restores_previous_tools_apply(world):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    root = paths.tools_root
    original = _snapshot(home)
    _apply(manifest, paths, mid="a" * 40)
    after_a = _snapshot(home)
    link_a = os.readlink(root / "fw/current")
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    assert os.readlink(root / "fw/current") != link_a
    assert (home / ".config/skcall/join.env").read_text() == "JOIN=b\n"

    calls = Calls()
    res = rollout.rollback_tools(paths, runner=calls)
    assert res.ok, res.problems
    assert res.notes == []
    assert os.readlink(root / "fw/current") == link_a
    assert (units / "gw.service").read_text() == after_a[str(units / "gw.service")][1].decode()
    assert (home / ".config/skcall/join.env").read_text() == "JOIN=a\n"
    assert "HERMES_MEMORY_OWNER=a" in (home / ".hermes/.env").read_text()
    assert _snapshot(home) == after_a
    assert ["systemctl", "--user", "daemon-reload"] in calls.cmds
    assert ["systemctl", "--user", "try-restart", "gw.service"] in calls.cmds
    assert "gw.service" in res.restarted

    # the B record is marked, and last.json now names A's record: a second
    # rollback undoes A and leaves the node as it was before any apply
    changes = paths.state_dir / "tools-changes"
    marked = [json.loads(p.read_text()) for p in changes.glob("[0-9]*.json")]
    assert sorted(r["manifest"][:1] for r in marked if r["rolled_back"]) == ["b"]
    assert json.loads((changes / "last.json").read_text())["manifest"] == "a" * 40
    rollout.rollback_tools(paths, runner=Calls())
    assert _snapshot(home) == original
    assert not (root / "fw/current").exists()
    with pytest.raises(rollout.DeployError, match="no recorded tools change"):
        rollout.rollback_tools(paths, runner=Calls())


def test_rollback_reports_hand_edits(world):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    _apply(manifest, paths, mid="a" * 40)
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    join = home / ".config/skcall/join.env"
    join.write_text("JOIN=b\nMINE=1\n")
    env = home / ".hermes/.env"
    env.write_text(env.read_text() + "LATER=1\n")  # outside the block, still overwritten

    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok, res.problems
    assert sorted(res.notes) == sorted([str(env), str(join)])
    assert join.read_text() == "JOIN=a\n"
    assert "LATER=1" not in env.read_text() and "HERMES_MEMORY_OWNER=a" in env.read_text()


def test_rollback_without_record_refuses(world):
    fw, cfg, home, units, paths = world
    # a node whose tools were applied before change records existed
    tools.apply_tools(
        _manifest(fw, cfg), "n1", paths.state_dir, paths.tools_root, units, home, runner=Calls()
    )
    before = _snapshot(home)
    calls = Calls()
    with pytest.raises(rollout.DeployError, match="no recorded tools change"):
        rollout.rollback_tools(paths, runner=calls)
    assert _snapshot(home) == before
    assert calls.cmds == []


def test_record_is_0600(world):
    fw, cfg, home, units, paths = world
    _apply(_manifest(fw, cfg), paths, mid="a" * 40)
    changes = paths.state_dir / "tools-changes"
    assert stat.S_IMODE(changes.stat().st_mode) == 0o700
    records = list(changes.glob("[0-9]*.json"))
    assert len(records) == 1
    assert stat.S_IMODE(records[0].stat().st_mode) == 0o600
    last = changes / "last.json"
    assert last.resolve() == records[0].resolve()
    rec = json.loads(last.read_text())
    assert rec["manifest"] == "a" * 40 and rec["rolled_back"] is False
    assert rec["units"] == ["gw.service"]
    assert set(rec) >= {"published", "dropins", "unit_files", "displaced", "files", "legacy"}
    # R3: the sha256 of each managed whole file and block body as written
    join = str(home / ".config/skcall/join.env")
    assert rec["written"][join]["sha256"] == hashlib.sha256(b"JOIN=a\n").hexdigest()
    env = rec["written"][str(home / ".hermes/.env")]
    assert env["block_sha256"] == hashlib.sha256(b"HERMES_MEMORY_OWNER=a\n").hexdigest()
    assert env["sha256"] == hashlib.sha256((home / ".hermes/.env").read_bytes()).hexdigest()
    # the record restores in a fresh process: plain JSON, no pickling
    change = tools.ToolsChange.from_dict(rec)
    assert change.to_dict()["files"] == rec["files"]


def test_no_op_apply_writes_no_record(world):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    _apply(manifest, paths)
    _apply(manifest, paths)
    assert len(list((paths.state_dir / "tools-changes").glob("[0-9]*.json"))) == 1


# --------------------------------------------------------------------------- CLI


def _cli(monkeypatch, paths, manifest):
    from skcapstone.cli import deploy_cmd

    real = rollout.rollback_tools
    # never reach the real systemd from a test
    monkeypatch.setattr(rollout, "rollback_tools", lambda p, runner=None: real(p, runner=Calls()))
    monkeypatch.setattr(rollout, "restart_units", lambda units, runner=None, **kw: [])
    monkeypatch.setattr(deploy_cmd, "_paths", lambda: paths)
    monkeypatch.setattr(deploy_cmd, "_manifest", lambda *a, **k: (manifest, "c" * 40))
    group = click.Group()
    deploy_cmd.register_deploy_commands(group)
    return lambda *args: CliRunner().invoke(group, ["deploy", "rollback", *args])


def test_cli_rollback_on_tools_only_node(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    _apply(manifest, paths, mid="a" * 40)
    run = _cli(monkeypatch, paths, manifest)
    out = run("--node", "n1")
    assert out.exit_code == 0, out.output
    assert not (paths.tools_root / "fw/current").exists()
    assert not (home / ".config/skcall/join.env").exists()
    out = run("--node", "n1")
    assert out.exit_code == 1 and "no recorded tools change" in out.output


def _venv_node(paths: Path) -> None:
    for name in ("r1", "r2"):
        (paths.releases / name).mkdir(parents=True)
    os.symlink(paths.releases / "r2", paths.venv_link)


def test_venv_node_rollback_unchanged(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg, packages={"skmemory": {"repo": "o/skmemory", "version": "1"}})
    _apply(manifest, paths, mid="a" * 40)  # a tools change OLDER than the venv switch
    _venv_node(paths)
    rollout.save_state(
        paths,
        {
            "current": {"release": str(paths.releases / "r2"), "at": 4e9},
            "previous": {"release": str(paths.releases / "r1")},
        },
    )
    out = _cli(monkeypatch, paths, manifest)("--node", "n1")
    assert out.exit_code == 0, out.output
    assert os.readlink(paths.venv_link) == str(paths.releases / "r1")
    assert (paths.tools_root / "fw/current").is_symlink()  # tools untouched
    assert (home / ".config/skcall/join.env").read_text() == "JOIN=a\n"


def test_venv_node_rollback_also_undoes_a_newer_tools_change(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg, packages={"skmemory": {"repo": "o/skmemory", "version": "1"}})
    _venv_node(paths)
    rollout.save_state(
        paths,
        {
            "current": {"release": str(paths.releases / "r2"), "at": 1.0},
            "previous": {"release": str(paths.releases / "r1")},
        },
    )
    _apply(manifest, paths, mid="a" * 40)
    out = _cli(monkeypatch, paths, manifest)("--node", "n1")
    assert out.exit_code == 0, out.output
    assert os.readlink(paths.venv_link) == str(paths.releases / "r1")
    assert not (paths.tools_root / "fw/current").exists()
