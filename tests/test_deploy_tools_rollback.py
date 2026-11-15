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
    # I1: the line added outside the block is kept, so it is not an overwrite;
    # the edited whole file is restored and its edited copy named
    assert len(res.notes) == 1 and str(join) in res.notes[0], res.notes
    assert join.read_text() == "JOIN=a\n"
    assert "LATER=1" in env.read_text() and "HERMES_MEMORY_OWNER=a" in env.read_text()


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
    monkeypatch.setattr(deploy_cmd, "_try_manifest", lambda *a, **k: (manifest, "c" * 40))
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


# ------------------------------------------------------------- fix round 1


def _with_real_plugin_dir(home: Path) -> Path:
    """The nctalk case: a real directory where the config wants a link."""
    plug = home / ".hermes/plugins/fw"
    plug.mkdir(parents=True)
    (plug / "hand.py").write_text("mine\n")
    return plug


def test_link_over_real_dir_round_trips_through_the_record(world):
    fw, cfg, home, units, paths = world
    plug = _with_real_plugin_dir(home)
    original = _snapshot(home)
    _apply(_manifest(fw, cfg), paths)
    assert plug.is_symlink()
    rec = json.loads((paths.state_dir / "tools-changes/last.json").read_text())
    moved = [e for e in rec["files"]["entries"] if e["was"] == "moved"]
    assert [e["path"] for e in moved] == [str(plug)]
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok and res.notes == []
    assert plug.is_dir() and not plug.is_symlink() and not (plug / "fw").exists()
    assert _snapshot(home) == original


def test_displaced_dropins_round_trip_through_the_record(world):
    fw, cfg, home, units, paths = world
    hand = units / "gw.service.d/override.conf"
    hand.parent.mkdir(parents=True)
    hand.write_text("[Service]\nEnvironment=MINE=1\n")
    original = _snapshot(home)
    _apply(_manifest(fw, cfg), paths)
    assert not hand.exists()
    rec = json.loads((paths.state_dir / "tools-changes/last.json").read_text())
    assert list(rec["displaced"]) == [str(hand)]
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok and res.notes == []
    assert hand.read_text() == "[Service]\nEnvironment=MINE=1\n"
    assert _snapshot(home) == original


def test_real_dir_back_at_a_moved_path_refuses_and_changes_nothing(world):
    fw, cfg, home, units, paths = world
    plug = _with_real_plugin_dir(home)
    _apply(_manifest(fw, cfg), paths)
    plug.unlink()
    plug.mkdir()  # someone put a directory back by hand
    (plug / "new.py").write_text("new\n")
    before = _snapshot(home)
    calls = Calls()
    with pytest.raises(rollout.DeployError, match="real directory now"):
        rollout.rollback_tools(paths, runner=calls)
    assert _snapshot(home) == before and calls.cmds == []


def test_rollback_stopped_partway_finishes_on_the_next_run(world, monkeypatch):
    fw, cfg, home, units, paths = world
    plug = _with_real_plugin_dir(home)
    original = _snapshot(home)
    _apply(_manifest(fw, cfg), paths)
    rec_path = rollout.last_change_path(paths)
    real_unlink = Path.unlink
    hits: list[Path] = []

    def unlink(self, missing_ok=False):
        # the first step after files.restore(): unpublishing fw/current
        if self.name == "current" and not hits:
            hits.append(self)
            raise OSError("disk said no")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(rollout.DeployError, match="stopped partway"):
        rollout.rollback_tools(paths, runner=Calls())
    assert hits and plug.is_dir() and not plug.is_symlink()  # files already restored
    assert json.loads(rec_path.read_text())["rolled_back"] is False
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok, res.problems
    assert res.notes == []
    assert _snapshot(home) == original
    assert json.loads(rec_path.read_text())["rolled_back"] is True


def test_venv_rollback_undoes_every_newer_tools_record(world, monkeypatch):
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
    original = _snapshot(home)
    _apply(manifest, paths, mid="a" * 40)
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    out = _cli(monkeypatch, paths, manifest)("--node", "n1")
    assert out.exit_code == 0, out.output
    assert os.readlink(paths.venv_link) == str(paths.releases / "r1")
    assert _snapshot(home) == original
    assert rollout.last_change(paths) is None


def test_venv_rollback_refuses_before_switching_when_tools_cannot_go_back(world, monkeypatch):
    import shutil

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
    a_export = paths.tools_root / "fw" / os.readlink(paths.tools_root / "fw/current")
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    shutil.rmtree(a_export)  # the B record cannot put fw/current back
    before = _snapshot(home)
    out = _cli(monkeypatch, paths, manifest)("--node", "n1")
    assert out.exit_code == 1 and "is gone" in out.output
    assert os.readlink(paths.venv_link) == str(paths.releases / "r2")
    assert _snapshot(home) == before


# ------------------------------------------------------------- fix round 2


def _venv_world(world):
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
    return manifest


def _bump(fw: Path, cfg: Path, tag: str) -> None:
    _commit(fw, {"src/index.mjs": f"// {tag}\n"})
    _commit(cfg, {"node1/join.env": f"JOIN={tag}\n"})


def _count(monkeypatch, name: str) -> list[int]:
    real = getattr(rollout, name)
    calls: list[int] = []

    def wrapped(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(rollout, name, wrapped)
    return calls


def test_venv_rollback_refuses_stacked_moves_and_changes_nothing(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _venv_world(world)
    plug = _with_real_plugin_dir(home)  # D0
    _apply(manifest, paths, mid="a" * 40)  # A moves D0 aside, links
    plug.unlink()
    plug.mkdir()  # D1 replaces the link
    (plug / "d1.py").write_text("d1\n")
    _bump(fw, cfg, "b")
    _apply(manifest, paths, mid="b" * 40)  # B moves D1 aside, links again
    before = _snapshot(home)
    out = _cli(monkeypatch, paths, manifest)("--node", "n1")
    assert out.exit_code == 1 and "moved aside by both" in out.output
    assert str(plug) in out.output.replace("\n", "")
    assert os.readlink(paths.venv_link) == str(paths.releases / "r2")
    assert _snapshot(home) == before
    assert len(rollout.pending_tools_changes(paths, since=1.0)) == 2


def test_venv_rollback_stopped_on_record_2_of_3_finishes_then_switches_once(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _venv_world(world)
    original = _snapshot(home)
    _apply(manifest, paths, mid="a" * 40)
    _bump(fw, cfg, "b")
    _apply(manifest, paths, mid="b" * 40)
    _bump(fw, cfg, "c")
    _apply(manifest, paths, mid="c" * 40)
    run = _cli(monkeypatch, paths, manifest)
    switches = _count(monkeypatch, "rollback")
    real_link = tools._atomic_link
    links: list[Path] = []

    def flaky(link, target):  # C re-points fw and fw-config; the 3rd call is in B
        links.append(link)
        if len(links) == 3:
            raise OSError("disk said no")
        return real_link(link, target)

    monkeypatch.setattr(tools, "_atomic_link", flaky)
    out = run("--node", "n1")
    assert out.exit_code == 1 and "stopped partway" in out.output, out.output
    assert switches == [] and os.readlink(paths.venv_link) == str(paths.releases / "r2")
    assert [r["manifest"][0] for _p, r in rollout.pending_tools_changes(paths, 1.0)] == [
        "b",
        "a",
    ]
    monkeypatch.setattr(tools, "_atomic_link", real_link)
    out = run("--node", "n1")
    assert out.exit_code == 0, out.output
    assert switches == [1]
    assert os.readlink(paths.venv_link) == str(paths.releases / "r1")
    assert _snapshot(home) == original
    assert rollout.last_change(paths) is None


def test_venv_switch_fails_after_tools_and_a_retry_only_switches(world, monkeypatch):
    import shutil

    fw, cfg, home, units, paths = world
    manifest = _venv_world(world)
    original = _snapshot(home)
    _apply(manifest, paths, mid="a" * 40)
    shutil.rmtree(paths.releases / "r1")  # the venv rollback will refuse
    run = _cli(monkeypatch, paths, manifest)
    tool_runs = _count(monkeypatch, "rollback_tools")
    out = run("--node", "n1")
    assert out.exit_code == 1 and "no previous release" in out.output
    assert "tools changes were rolled back" in out.output.replace("\n", " ")
    assert _snapshot(home) == original and tool_runs == [1]
    assert os.readlink(paths.venv_link) == str(paths.releases / "r2")
    (paths.releases / "r1").mkdir()
    out = run("--node", "n1")
    assert out.exit_code == 0, out.output
    assert tool_runs == [1]  # nothing left to undo: only the venv moved
    assert os.readlink(paths.venv_link) == str(paths.releases / "r1")
    assert _snapshot(home) == original


# ------------------------------------------------- final review: I1 (block-region rollback)

BEGIN = "# >>> sk-deploy (managed by skcapstone deploy; edits here are overwritten) >>>"
END = "# <<< sk-deploy <<<"


def test_rollback_keeps_a_line_added_outside_the_block(world):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    env = home / ".hermes/.env"
    _apply(manifest, paths, mid="a" * 40)
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    # Jarvis rotates a key by hand, above and below the block
    text = env.read_text().replace("OPENAI_API_KEY=x", "OPENAI_API_KEY=rotated")
    env.write_text("# top\n" + text + "LATER=1\n")
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok, res.problems
    assert env.read_text() == (
        f"# top\nOPENAI_API_KEY=rotated\n{BEGIN}\nHERMES_MEMORY_OWNER=a\n{END}\nLATER=1\n"
    )
    assert not any(str(env) in n for n in res.notes), res.notes


def test_rollback_removes_the_block_when_the_file_had_none(world):
    fw, cfg, home, units, paths = world
    env = home / ".hermes/.env"
    env.write_text("OPENAI_API_KEY=x")  # no trailing newline: the apply adds one
    _apply(_manifest(fw, cfg), paths)
    assert BEGIN in env.read_text()
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok, res.problems
    assert env.read_bytes() == b"OPENAI_API_KEY=x"
    _apply(_manifest(fw, cfg), paths)
    env.write_text(env.read_text() + "ROTATED=1\n")
    rollout.rollback_tools(paths, runner=Calls())
    assert env.read_text() == "OPENAI_API_KEY=x\nROTATED=1\n"


def test_rollback_of_a_created_block_file_keeps_lines_added_outside(world):
    fw, cfg, home, units, paths = world
    env = home / ".hermes/.env"
    env.unlink()
    _apply(_manifest(fw, cfg), paths)
    env.write_text(env.read_text() + "MINE=1\n")
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok, res.problems
    assert env.read_text() == "MINE=1\n"
    assert any(str(env) in n and "kept" in n for n in res.notes), res.notes
    # with nothing outside the block, the file goes as it came
    env.unlink()
    _apply(_manifest(fw, cfg), paths)
    res = rollout.rollback_tools(paths, runner=Calls())
    assert not env.exists() and res.notes == []


def test_rollback_note_for_an_edit_inside_the_block(world):
    fw, cfg, home, units, paths = world
    env = home / ".hermes/.env"
    _apply(_manifest(fw, cfg), paths)
    env.write_text(env.read_text().replace("OWNER=a", "OWNER=hand") + "LATER=1\n")
    res = rollout.rollback_tools(paths, runner=Calls())
    assert env.read_text() == "OPENAI_API_KEY=x\nLATER=1\n"
    assert any(str(env) in n and "block" in n for n in res.notes), res.notes


def test_rollback_copies_an_edited_whole_file_aside(world):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    join = home / ".config/skcall/join.env"
    _apply(manifest, paths, mid="a" * 40)
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    join.write_text("JOIN=b\nMINE=1\n")
    res = rollout.rollback_tools(paths, runner=Calls())
    assert res.ok, res.problems
    assert join.read_text() == "JOIN=a\n"
    copies = [p for p in (paths.state_dir / "rolled-over").rglob("*") if p.is_file()]
    assert len(copies) == 1
    copy = copies[0]
    assert copy.read_text() == "JOIN=b\nMINE=1\n"
    assert stat.S_IMODE(copy.stat().st_mode) == 0o600
    assert stat.S_IMODE(copy.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((paths.state_dir / "rolled-over").stat().st_mode) == 0o700
    assert [n for n in res.notes if str(join) in n and str(copy) in n], res.notes
    # an unedited whole file is restored without a copy
    _apply(manifest, paths, mid="c" * 40)
    res = rollout.rollback_tools(paths, runner=Calls())
    assert len([p for p in (paths.state_dir / "rolled-over").rglob("*") if p.is_file()]) == 1


def test_rollback_refuses_broken_block_markers_and_changes_nothing(world):
    fw, cfg, home, units, paths = world
    env = home / ".hermes/.env"
    _apply(_manifest(fw, cfg), paths)
    env.write_text(env.read_text().replace(END + "\n", ""))  # the end marker deleted by hand
    before = _snapshot(home)
    calls = Calls()
    with pytest.raises(rollout.DeployError, match="markers are broken"):
        rollout.rollback_tools(paths, runner=calls)
    assert _snapshot(home) == before and calls.cmds == []
    rec = json.loads((paths.state_dir / "tools-changes/last.json").read_text())
    assert [e.get("block") for e in rec["files"]["entries"] if e["path"] == str(env)] == [
        "sk-deploy"
    ]


# ------------------------------------------------- final review: F1, F2, F3, I3


class Enabled(Calls):
    """Every unit is active and enabled; notes whether a unit file exists when stopped."""

    def __init__(self, units: Path):
        super().__init__()
        self.units = units
        self.stopped_with_file: list[tuple[str, bool]] = []

    def __call__(self, cmd, **kw):
        if cmd[:1] == ["systemctl"] and "stop" in cmd:
            self.stopped_with_file.append((cmd[-1], (self.units / cmd[-1]).exists()))
        if cmd[:1] == ["systemctl"] and "is-enabled" in cmd:
            self.cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, "enabled\n", "")
        return super().__call__(cmd, **kw)


def test_rollback_stops_a_unit_the_apply_created_and_does_not_restart_it(world):
    fw, cfg, home, units, paths = world
    _apply(_manifest(fw, cfg), paths)  # gw.service did not exist before
    calls = Enabled(units)
    res = rollout.rollback_tools(paths, runner=calls)
    assert res.ok, res.problems
    assert not (units / "gw.service").exists()
    assert calls.stopped_with_file == [("gw.service", True)], "stopped before its file went"
    assert ["systemctl", "--user", "disable", "gw.service"] in calls.cmds
    assert ["systemctl", "--user", "try-restart", "gw.service"] not in calls.cmds
    assert "gw.service" not in res.restarted


def _failing_save(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("No space left on device")

    monkeypatch.setattr(rollout, "save_change", boom)


def test_unrecorded_change_blocks_a_rollback_of_the_older_one(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    _apply(manifest, paths, mid="a" * 40)
    _bump_to_b(fw, cfg)
    real = rollout.save_change
    _failing_save(monkeypatch)
    res = _apply(manifest, paths, mid="b" * 40)
    assert any("not recorded" in n for n in res.notes)
    monkeypatch.setattr(rollout, "save_change", real)
    before = _snapshot(home)
    calls = Calls()
    with pytest.raises(rollout.DeployError, match="the last tools change was not recorded"):
        rollout.rollback_tools(paths, runner=calls)
    assert _snapshot(home) == before and calls.cmds == []
    # the CLI says the same on a venv node, before switching anything
    with pytest.raises(rollout.DeployError, match="rollback would undo an older one"):
        rollout.check_tools_rollback(paths, rollout.pending_tools_changes(paths))
    # a later change that IS recorded can be rolled back again
    _bump(fw, cfg, "c")
    _apply(manifest, paths, mid="c" * 40)
    assert rollout.rollback_tools(paths, runner=Calls()).ok
    assert (home / ".config/skcall/join.env").read_text() == "JOIN=b\n"


def test_cli_refusal_says_fix_then_run_again(world, monkeypatch):
    fw, cfg, home, units, paths = world
    plug = _with_real_plugin_dir(home)
    _apply(_manifest(fw, cfg), paths)
    plug.unlink()
    plug.mkdir()
    out = _cli(monkeypatch, paths, _manifest(fw, cfg))("--node", "n1")
    assert out.exit_code == 1
    text = out.output.replace("\n", " ")
    assert "fix the problem above, then run deploy rollback again" in text


def test_cli_venv_refusal_says_fix_then_run_again(world, monkeypatch):
    import shutil

    fw, cfg, home, units, paths = world
    manifest = _venv_world(world)
    _apply(manifest, paths, mid="a" * 40)
    a_export = paths.tools_root / "fw" / os.readlink(paths.tools_root / "fw/current")
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    shutil.rmtree(a_export)
    out = _cli(monkeypatch, paths, manifest)("--node", "n1")
    assert out.exit_code == 1
    assert "fix the problem above, then run deploy rollback again" in out.output.replace("\n", " ")


def test_cli_rollback_with_failed_restart_does_not_say_rolled_back(world, monkeypatch):
    fw, cfg, home, units, paths = world
    manifest = _manifest(fw, cfg)
    _apply(manifest, paths, mid="a" * 40)
    _bump_to_b(fw, cfg)
    _apply(manifest, paths, mid="b" * 40)
    run = _cli(monkeypatch, paths, manifest)
    monkeypatch.setattr(
        rollout, "restart_units", lambda units, runner=None, **kw: ["gw.service is failed"]
    )
    out = run("--node", "n1")
    assert out.exit_code == 1
    assert "tools change rolled back" not in out.output
    assert "gw.service is failed" in out.output and "restart" in out.output


def test_cli_rollback_without_a_manifest_on_a_tools_only_node(world, monkeypatch):
    from skcapstone.cli import deploy_cmd
    from skcapstone.deploy import ManifestUnavailableError

    fw, cfg, home, units, paths = world
    _apply(_manifest(fw, cfg), paths, mid="a" * 40)
    run = _cli(monkeypatch, paths, None)

    def bad(*_a, **_k):
        raise ManifestUnavailableError("a bad manifest commit was fetched")

    monkeypatch.setattr(deploy_cmd, "_try_manifest", bad)
    out = run("--node", "n1")
    assert out.exit_code == 0, out.output
    assert "manifest unavailable" in out.output
    assert not (paths.tools_root / "fw/current").exists()
    # a node whose state records a venv release still needs the manifest
    _apply(_manifest(fw, cfg), paths, mid="a" * 40)
    _venv_node(paths)
    rollout.save_state(paths, {"current": {"release": str(paths.releases / "r2"), "at": 1.0}})
    out = run("--node", "n1")
    assert out.exit_code == 2 and "manifest unavailable" in out.output
    assert (paths.tools_root / "fw/current").is_symlink()


# ------------------------------------------------- final review: M1 (undo that fails)


class SmokeFails(Calls):
    def __call__(self, cmd, **kw):
        if cmd[:1] == ["bash"]:
            self.cmds.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 1, "", "smoke said no")
        return super().__call__(cmd, **kw)


def _undo_fails_once(monkeypatch):
    real = tools.ToolsChange.undo
    hits: list[int] = []

    def undo(self, root, unit_dir):
        if not hits:
            hits.append(1)
            raise OSError("disk said no")
        return real(self, root, unit_dir)

    monkeypatch.setattr(tools.ToolsChange, "undo", undo)
    return hits


def test_failed_undo_after_a_failed_smoke_is_recorded(world, monkeypatch):
    fw, cfg, home, units, paths = world
    original = _snapshot(home)
    hits = _undo_fails_once(monkeypatch)
    res = rollout.apply_tools_only(_manifest(fw, cfg), paths, runner=SmokeFails(), node="n1")
    assert hits and not res.ok
    assert any("deploy rollback" in p for p in res.problems), res.problems
    assert rollout.last_change(paths) is not None
    assert rollout.rollback_tools(paths, runner=Calls()).ok
    assert _snapshot(home) == original


def test_failed_undo_of_a_failed_files_step_is_recorded(world, monkeypatch):
    from skcapstone.deploy import files as files_mod

    fw, cfg, home, units, paths = world
    original = _snapshot(home)

    def boom(*_a, **_k):
        raise OSError("files said no")

    real_apply, real_undo = files_mod.apply_files, tools.ToolsChange.undo
    monkeypatch.setattr(files_mod, "apply_files", boom)
    _undo_fails_once(monkeypatch)
    res = rollout.apply_tools_only(_manifest(fw, cfg), paths, runner=Calls(), node="n1")
    assert not res.ok
    assert any("deploy rollback" in p for p in res.problems), res.problems
    assert rollout.last_change(paths) is not None
    monkeypatch.setattr(files_mod, "apply_files", real_apply)
    monkeypatch.setattr(tools.ToolsChange, "undo", real_undo)
    assert rollout.rollback_tools(paths, runner=Calls()).ok
    assert _snapshot(home) == original


def test_failed_undo_in_a_venv_apply_is_recorded_for_the_rollback(world, monkeypatch):
    from skcapstone.deploy.inspect import Dist

    fw, cfg, home, units, paths = world
    manifest = _venv_world(world)
    original = _snapshot(home)

    def build(_m, dest, *_a, **_k):
        (dest / "bin").mkdir(parents=True)

    monkeypatch.setattr(rollout, "build_venv", build)
    monkeypatch.setattr(rollout, "verify_venv", lambda *a, **k: [])
    monkeypatch.setattr(rollout, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(rollout, "list_dists", lambda _p: [Dist("skmemory", "0", False, "")])
    _undo_fails_once(monkeypatch)
    res = rollout.apply(manifest, "d" * 40, paths, "3.12", SmokeFails(), node="n1")
    assert not res.ok and res.rolled_back
    assert any("deploy rollback" in p for p in res.problems), res.problems
    assert os.readlink(paths.venv_link) == str(paths.releases / "r2")
    pending = rollout.pending_tools_changes(
        paths, since=rollout.load_state(paths)["current"]["at"]
    )
    assert len(pending) == 1, "the record is newer than the switch back"
    assert rollout.rollback_tools(paths, runner=Calls()).ok
    assert _snapshot(home) == original
