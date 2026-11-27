"""Per-cluster config repos: exported next to the framework tool, units rendered from them."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skcapstone.deploy import drift as drift_mod
from skcapstone.deploy import tools
from skcapstone.deploy.manifest import Manifest

UNIT = (
    "[Unit]\nDescription=gw\n\n[Service]\nWorkingDirectory=@TOOL@\n"
    "ExecStart=/usr/bin/node @TOOL@/src/index.mjs --port 18780 --config @CONFIG@/gw.yaml\n"
    "Environment=GW_POLICIES=@CONFIG@/policies.yaml\n"
)


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo(path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(text)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")
    return path


@pytest.fixture
def world(tmp_path: Path):
    fw = _repo(tmp_path / "fw", {"src/index.mjs": "//\n", "README.md": "x\n"})
    cfg = _repo(
        tmp_path / "cfg",
        {
            "node1/gw.yaml": "port: 18780\n",
            "node1/policies.yaml": "p: 1\n",
            "node1/systemd/gw.service": UNIT,
            "node2/gw.yaml": "other cluster member\n",
            "node2/systemd/gw-node2.service": UNIT,
        },
    )
    home = tmp_path / "home"
    units = home / ".config/systemd/user"
    (units / "gw.service.d").mkdir(parents=True)
    (units / "gw.service").write_text(
        "[Service]\nExecStart=/usr/bin/node %h/src/fw/src/index.mjs"
        " --config %h/src/fw/config/gw.yaml\n"
    )
    (units / "gw.service.d/override.conf").write_text("[Service]\nEnvironment=OLD=1\n")
    return tmp_path, fw, cfg, home, units


def _manifest(fw: Path, cfg: Path, home: Path, cfg_ref: str = "main", **extra) -> Manifest:
    return Manifest.model_validate(
        {
            "packages": {},
            "tools": {
                "fw": {"url": f"file://{fw}", "ref": "main", "checkout": str(home / "src/fw")}
            },
            "nodes": {
                "n1": {
                    "tools": ["fw"],
                    "configs": {
                        "fw": {
                            "url": f"file://{cfg}",
                            "ref": cfg_ref,
                            "instance": "node1",
                            **extra,
                        }
                    },
                }
            },
        }
    )


def _no_systemd(cmd, **kw):
    if cmd[:1] == ["git"]:
        return subprocess.run(cmd, **kw)
    return subprocess.CompletedProcess(cmd, 0, "", "")


def _apply(tmp, manifest, home, units):
    return tools.apply_tools(
        manifest,
        "n1",
        tmp / "state",
        root=home / ".local/share/sk-tools",
        unit_dir=units,
        home=home,
        runner=_no_systemd,
    )


def test_exports_config_and_renders_the_whole_unit(world):
    tmp, fw, cfg, home, units = world
    change = _apply(tmp, _manifest(fw, cfg, home), home, units)
    root = home / ".local/share/sk-tools"
    exp = root / "fw-config" / "current"
    assert (exp / "node1/gw.yaml").read_text() == "port: 18780\n"
    assert not (exp / "node2").exists(), "only the node's instance is exported"
    text = (units / "gw.service").read_text()
    assert text.startswith("# Managed by `skcapstone deploy`")
    assert "ExecStart=/usr/bin/node %h/.local/share/sk-tools/fw/current/src/index.mjs" in text
    assert "--config %h/.local/share/sk-tools/fw-config/current/node1/gw.yaml" in text
    assert "@" not in text.split("\n", 4)[-1]
    assert not (units / "gw.service.d").exists(), "hand drop-ins moved aside"
    moved = list((tmp / "state/displaced-dropins").rglob("override.conf"))
    assert len(moved) == 1 and "OLD=1" in moved[0].read_text()
    assert not (
        units / "gw.service.d" / tools.DROPIN
    ).exists(), "no rewrite drop-in for a rendered unit"
    assert "gw.service" in change.units
    assert not (units / "gw-node2.service").exists()


def test_second_apply_is_a_no_op_and_a_new_config_ref_restarts(world):
    tmp, fw, cfg, home, units = world
    _apply(tmp, _manifest(fw, cfg, home), home, units)
    again = _apply(tmp, _manifest(fw, cfg, home), home, units)
    assert again.empty and again.units == []
    (cfg / "node1/gw.yaml").write_text("port: 18781\n")
    _git(cfg, "commit", "-qam", "bump")
    bumped = _apply(tmp, _manifest(fw, cfg, home), home, units)
    assert "fw-config" in bumped.published and bumped.units == ["gw.service"]
    old = bumped.unit_files["gw.service"]
    new = (units / "gw.service").read_text()
    assert old.split("\n", 4)[-1] == new.split("\n", 4)[-1], "only the provenance header moved"
    assert old != new


def test_undo_restores_unit_dropins_and_links(world):
    tmp, fw, cfg, home, units = world
    before = (units / "gw.service").read_text()
    change = _apply(tmp, _manifest(fw, cfg, home), home, units)
    change.undo(home / ".local/share/sk-tools", units)
    assert (units / "gw.service").read_text() == before
    assert (units / "gw.service.d/override.conf").read_text() == "[Service]\nEnvironment=OLD=1\n"
    assert not (home / ".local/share/sk-tools/fw-config/current").exists()


def test_config_ref_off_main_is_refused_and_nothing_changes(world):
    tmp, fw, cfg, home, units = world
    _git(cfg, "checkout", "-qb", "side")
    (cfg / "node1/gw.yaml").write_text("side\n")
    _git(cfg, "commit", "-qam", "side")
    _git(cfg, "checkout", "-q", "main")
    before = (units / "gw.service").read_text()
    with pytest.raises(tools.ToolError, match="not on main"):
        _apply(tmp, _manifest(fw, cfg, home, cfg_ref="side"), home, units)
    assert (units / "gw.service").read_text() == before
    assert (units / "gw.service.d/override.conf").exists()


def test_unit_naming_a_missing_config_file_is_refused(world):
    tmp, fw, cfg, home, units = world
    (cfg / "node1/systemd/gw.service").write_text(UNIT.replace("gw.yaml", "missing.yaml"))
    _git(cfg, "commit", "-qam", "typo")
    with pytest.raises(tools.ToolError, match="missing.yaml"):
        _apply(tmp, _manifest(fw, cfg, home), home, units)
    assert "%h/src/fw" in (units / "gw.service").read_text()


def test_unknown_placeholder_and_no_units_are_refused(world):
    tmp, fw, cfg, home, units = world
    (cfg / "node1/systemd/gw.service").write_text(UNIT + "Environment=X=@PORT@\n")
    _git(cfg, "commit", "-qam", "placeholder")
    with pytest.raises(tools.ToolError, match="@PORT@"):
        _apply(tmp, _manifest(fw, cfg, home), home, units)
    with pytest.raises(tools.ToolError, match="no unit files"):
        _apply(tmp, _manifest(fw, cfg, home, instance="node3"), home, units)


def test_manifest_validation():
    with pytest.raises(ValueError, match="does not run"):
        Manifest.model_validate(
            {
                "packages": {},
                "tools": {},
                "nodes": {
                    "n": {
                        "tools": [],
                        "configs": {"fw": {"repo": "o/c", "ref": "x", "instance": "a"}},
                    }
                },
            }
        )
    with pytest.raises(ValueError, match="one directory"):
        Manifest.model_validate(
            {
                "packages": {},
                "tools": {"fw": {"repo": "o/f", "ref": "x", "checkout": "~/f"}},
                "nodes": {
                    "n": {"configs": {"fw": {"repo": "o/c", "ref": "x", "instance": "../a"}}}
                },
            }
        )


def test_drift_reports_a_config_export_off_its_ref(world, tmp_path):
    tmp, fw, cfg, home, units = world
    m = _manifest(fw, cfg, home)
    report = drift_mod.compute(
        home,
        tmp / "nopython",
        manifest=m,
        unit_dir=units,
        node="n1",
        tools_root=home / ".local/share/sk-tools",
        runner=_no_systemd,
    )
    assert any("fw-config not exported" in t for t in report.tools)
