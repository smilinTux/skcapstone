"""Config instances manage files on the host: whole files, a marked block, symlinks."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from skcapstone.deploy import files, rollout, tools
from skcapstone.deploy.manifest import Manifest

BEGIN = "# >>> sk-deploy (managed by skcapstone deploy; edits here are overwritten) >>>"
END = "# <<< sk-deploy <<<"
BLOCK = "HERMES_MEMORY_OWNER=jarvis\nNCTALK_URL=https://talk.example\n"


def _inst(tmp_path: Path, files_yaml: str, sources: dict[str, str]) -> Path:
    inst = tmp_path / "inst"
    inst.mkdir()
    (inst / "files.yaml").write_text(files_yaml)
    for rel, text in sources.items():
        (inst / rel).parent.mkdir(parents=True, exist_ok=True)
        (inst / rel).write_text(text)
    return inst


def _block_world(tmp_path: Path, fmt: str = "env", block: str = BLOCK):
    inst = _inst(
        tmp_path,
        f"files:\n  - path: ~/.hermes/.env\n    mode: block\n    source: hermes.env\n"
        f"    format: {fmt}\n",
        {"hermes.env": block},
    )
    home = tmp_path / "home"
    (home / ".hermes").mkdir(parents=True)
    return inst, home, home / ".hermes/.env"


def test_load_files_absent_is_empty(tmp_path):
    assert files.load_files(tmp_path) == []


def test_block_appended_when_absent(tmp_path):
    inst, home, env = _block_world(tmp_path)
    mine = b"OPENAI_API_KEY=sk-x\n# jarvis notes\nNO_NEWLINE=1"
    env.write_bytes(mine)
    prev = files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert prev == {str(env): mine.decode()}
    out = env.read_bytes()
    assert out.startswith(mine), "the existing bytes are kept"
    assert out == mine + f"\n{BEGIN}\n{BLOCK}{END}\n".encode()


def test_block_replaced_in_place(tmp_path):
    inst, home, env = _block_world(tmp_path)
    head = "A=1\n# keep me\n"
    tail = "\nZ=26\n  trailing text  \n"
    env.write_text(f"{head}{BEGIN}\nHERMES_MEMORY_OWNER=old\nSTALE=1\n{END}\n{tail}")
    files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert env.read_text() == f"{head}{BEGIN}\n{BLOCK}{END}\n{tail}"
    again = files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert again == {}, "a second apply writes nothing"


def test_whole_mode_and_file_mode(tmp_path):
    inst = _inst(
        tmp_path,
        "files:\n  - path: ~/.config/skcall/join.env\n    mode: whole\n    source: join.env\n"
        "  - path: ~/.config/skcall/pub.txt\n    mode: whole\n    source: pub.txt\n"
        "    format: text\n    file_mode: 0o644\n",
        {"join.env": "JOIN=1\n", "pub.txt": "hello\n"},
    )
    home = tmp_path / "home"
    prev = files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    join = home / ".config/skcall/join.env"
    assert prev == {str(join): None, str(home / ".config/skcall/pub.txt"): None}
    assert join.read_text() == "JOIN=1\n"
    assert stat.S_IMODE(join.stat().st_mode) == 0o600
    assert stat.S_IMODE((home / ".config/skcall/pub.txt").stat().st_mode) == 0o644
    join.chmod(0o644)
    undo = files.FilesUndo()
    files.apply_files(files.load_files(inst), inst, home, tmp_path / "state", undo=undo)
    assert stat.S_IMODE(join.stat().st_mode) == 0o600, "a mode drift alone is repaired"
    undo.restore()
    assert stat.S_IMODE(join.stat().st_mode) == 0o644


def test_env_duplicate_key_outside_block_refuses(tmp_path):
    inst, home, env = _block_world(tmp_path)
    mine = "A=1\n# owner was\nexport HERMES_MEMORY_OWNER=lumina\nB=2\n"
    env.write_text(mine)
    with pytest.raises(files.FilesError, match=r"HERMES_MEMORY_OWNER.*line 3"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert env.read_text() == mine


def test_yaml_duplicate_top_level_key_refuses(tmp_path):
    inst, home, env = _block_world(tmp_path, fmt="yaml", block="owner: jarvis\nplugins: []\n")
    mine = "model: x\nplugins:\n  - a\n  owner: nested is fine\n"
    env.write_text(mine)
    with pytest.raises(files.FilesError, match=r"plugins.*line 2"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert env.read_text() == mine


def test_block_marker_twice_refuses(tmp_path):
    inst, home, env = _block_world(tmp_path)
    mine = f"{BEGIN}\nX=1\n{END}\n{BEGIN}\nY=1\n{END}\n"
    env.write_text(mine)
    with pytest.raises(files.FilesError, match="marker"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert env.read_text() == mine
    env.write_text(f"A=1\n{BEGIN}\nX=1\n")
    with pytest.raises(files.FilesError, match="marker"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")


def test_missing_source_and_bad_spec_refuse(tmp_path):
    inst = _inst(
        tmp_path,
        "files:\n  - path: ~/a.env\n    mode: whole\n    source: nope.env\n",
        {},
    )
    home = tmp_path / "home"
    with pytest.raises(files.FilesError, match="nope.env"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert not (home / "a.env").exists()
    with pytest.raises(ValueError):
        files.FileSpec(path="~/x", mode="link", source="a")
    with pytest.raises(ValueError):
        files.FileSpec(path="~/x", mode="whole", source="../escape")


def test_link_moves_real_dir_aside_and_undo_restores(tmp_path):
    inst = _inst(
        tmp_path,
        "files:\n  - path: ~/.hermes/plugins/nctalk\n    mode: link\n"
        "    target: '@TOOL:nctalk@/plugin'\n",
        {},
    )
    home = tmp_path / "home"
    root = home / ".local/share/sk-tools"
    (root / "nctalk/abc/plugin").mkdir(parents=True)
    os.symlink("abc", root / "nctalk/current")
    plug = home / ".hermes/plugins/nctalk"
    plug.mkdir(parents=True)
    (plug / "hand.py").write_text("mine\n")
    specs = files.load_files(inst)
    with pytest.raises(tools.ToolError, match="unknown tool nctalk"):
        files.apply_files(specs, inst, home, tmp_path / "state", tools_on_node=["other"])
    assert plug.is_dir() and not plug.is_symlink()
    undo = files.FilesUndo()
    files.apply_files(
        specs, inst, home, tmp_path / "state", tools_on_node=["nctalk"], tools_root=root, undo=undo
    )
    assert os.readlink(plug) == f"{root}/nctalk/current/plugin"
    moved = list((tmp_path / "state/displaced-files").rglob("hand.py"))
    assert len(moved) == 1 and moved[0].read_text() == "mine\n"
    assert undo.displaced
    undo.restore()
    assert plug.is_dir() and not plug.is_symlink()
    assert (plug / "hand.py").read_text() == "mine\n"


def test_link_target_missing_in_the_tool_refuses(tmp_path):
    inst = _inst(
        tmp_path,
        "files:\n  - path: ~/p\n    mode: link\n    target: '@TOOL:nctalk@/gone'\n",
        {},
    )
    home = tmp_path / "home"
    root = home / ".local/share/sk-tools"
    (root / "nctalk/abc").mkdir(parents=True)
    with pytest.raises(files.FilesError, match="gone"):
        files.apply_files(
            files.load_files(inst),
            inst,
            home,
            tmp_path / "state",
            tools_on_node=["nctalk"],
            tools_root=root,
            exports={"nctalk": root / "nctalk/abc"},
        )
    assert not (home / "p").is_symlink()


def test_diff_files_shows_the_block(tmp_path):
    inst, home, env = _block_world(tmp_path)
    env.write_text("A=1\n")
    out = files.diff_files(files.load_files(inst), inst, home)
    assert len(out) == 1 and f"+{BEGIN}" in out[0] and "+HERMES_MEMORY_OWNER=jarvis" in out[0]
    files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert files.diff_files(files.load_files(inst), inst, home) == []


# ----------------------------------------------------------------- integration


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


UNIT = "[Service]\nExecStart=/usr/bin/node @TOOL@/src/index.mjs\n"
FILES_YAML = (
    "files:\n"
    "  - path: ~/.hermes/.env\n    mode: block\n    source: hermes.env\n"
    "  - path: ~/.config/skcall/join.env\n    mode: whole\n    source: join.env\n"
    "  - path: ~/.hermes/plugins/fw\n    mode: link\n    target: '@TOOL:fw@/src'\n"
)


@pytest.fixture
def world(tmp_path: Path):
    fw = _repo(tmp_path / "fw", {"src/index.mjs": "//\n"})
    cfg = _repo(
        tmp_path / "cfg",
        {
            "node1/systemd/gw.service": UNIT,
            "node1/files.yaml": FILES_YAML,
            "node1/hermes.env": BLOCK,
            "node1/join.env": "JOIN=1\n",
        },
    )
    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (home / ".hermes/plugins/fw").mkdir(parents=True)
    (home / ".hermes/plugins/fw/hand.py").write_text("mine\n")
    (home / ".hermes/.env").write_text("OPENAI_API_KEY=x\n")
    manifest = Manifest.model_validate(
        {
            "packages": {},
            "tools": {"fw": {"url": f"file://{fw}", "ref": "main"}},
            "services": {"gw.service": {"smoke": "false"}},
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
    return tmp_path, cfg, home, units, manifest


def _no_systemd(cmd, **kw):
    if cmd[:1] == ["git"]:
        return subprocess.run(cmd, **kw)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def test_apply_tools_writes_files_after_publish_and_restarts(world):
    tmp, cfg, home, units, manifest = world
    root = home / ".local/share/sk-tools"
    change = tools.apply_tools(
        manifest, "n1", tmp / "state", root, units, home, runner=_no_systemd
    )
    env = (home / ".hermes/.env").read_text()
    assert env.startswith("OPENAI_API_KEY=x\n") and "HERMES_MEMORY_OWNER=jarvis" in env
    assert (home / ".config/skcall/join.env").read_text() == "JOIN=1\n"
    assert os.readlink(home / ".hermes/plugins/fw") == f"{root}/fw/current/src"
    assert (home / ".hermes/plugins/fw/index.mjs").exists()
    assert str(home / ".hermes/.env") in change.files.previous
    again = tools.apply_tools(manifest, "n1", tmp / "state", root, units, home, runner=_no_systemd)
    assert again.empty and again.units == []
    (cfg / "node1/join.env").write_text("JOIN=2\n")
    _git(cfg, "commit", "-qam", "bump")
    bumped = tools.apply_tools(
        manifest, "n1", tmp / "state", root, units, home, runner=_no_systemd
    )
    assert "gw.service" in bumped.units


def test_invalid_file_publishes_nothing(world):
    tmp, cfg, home, units, manifest = world
    (home / ".hermes/.env").write_text("X=1\nHERMES_MEMORY_OWNER=me\n")
    root = home / ".local/share/sk-tools"
    with pytest.raises(tools.ToolError, match="HERMES_MEMORY_OWNER.*line 2"):
        tools.apply_tools(manifest, "n1", tmp / "state", root, units, home, runner=_no_systemd)
    assert not (root / "fw/current").exists() and not (root / "fw-config/current").exists()
    assert not (units / "gw.service").exists()
    assert not (home / ".config/skcall/join.env").exists()
    assert not (home / ".hermes/plugins/fw").is_symlink()


def test_failed_smoke_restores_files(world, monkeypatch):
    tmp, cfg, home, units, manifest = world
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(rollout.time, "sleep", lambda _s: None)
    before = (home / ".hermes/.env").read_bytes()
    (home / ".hermes/.env").chmod(0o640)

    def runner(cmd, **kw):
        if cmd[:1] == ["git"]:
            return subprocess.run(cmd, **kw)
        if "is-active" in cmd:
            return subprocess.CompletedProcess(cmd, 0, "active\n", "")
        if cmd[:2] == ["bash", "-c"]:
            return subprocess.CompletedProcess(cmd, 1, "", "smoke said no")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    paths = rollout.Paths(
        tmp / ".skenv", tmp / ".skenv-releases", tmp / "state", home / ".local/share/sk-tools"
    )
    result = rollout.apply_tools_only(manifest, paths, runner=runner, node="n1")
    assert not result.ok and result.rolled_back and "gw.service" in result.restarted
    assert (home / ".hermes/.env").read_bytes() == before
    assert stat.S_IMODE((home / ".hermes/.env").stat().st_mode) == 0o640
    assert not (home / ".config/skcall/join.env").exists()
    plug = home / ".hermes/plugins/fw"
    assert plug.is_dir() and not plug.is_symlink() and (plug / "hand.py").read_text() == "mine\n"


@pytest.mark.parametrize(
    "line3",
    [
        "HERMES_MEMORY_OWNER = x",
        "'HERMES_MEMORY_OWNER'=x",
        'export "HERMES_MEMORY_OWNER" = x',
    ],
)
def test_env_spaced_or_quoted_duplicate_key_refuses(tmp_path, line3):
    inst, home, env = _block_world(tmp_path)
    mine = f"A=1\n# owner was\n{line3}\nB=2\n"
    env.write_text(mine)
    with pytest.raises(files.FilesError, match=r"HERMES_MEMORY_OWNER on line 3"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert env.read_text() == mine


def test_block_body_setting_a_key_twice_refuses(tmp_path):
    inst, home, env = _block_world(tmp_path, block="K=1\nOTHER=2\nK = 3\n")
    env.write_text("A=1\n")
    with pytest.raises(files.FilesError, match=r"hermes.env sets K more than once"):
        files.apply_files(files.load_files(inst), inst, home, tmp_path / "state")
    assert env.read_text() == "A=1\n"
