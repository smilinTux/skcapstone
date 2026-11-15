"""Schema relaxations that let a cluster manifest carry a public stack."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from skcapstone.deploy import tools
from skcapstone.deploy.manifest import Manifest, ToolConfigSpec, ToolSpec


def test_tool_without_checkout():
    spec = ToolSpec(repo="smilinTux/sidekick", ref="v1")
    assert spec.checkouts == []
    assert ToolSpec(repo="a/b", ref="v1", checkout="").checkouts == []
    assert ToolSpec(repo="a/b", ref="v1", checkout="~/x").checkouts == ["~/x"]
    m = Manifest.model_validate(
        {
            "packages": {},
            "tools": {"sk": {"repo": "a/b", "ref": "v1"}},
            "nodes": {"n": {"tools": ["sk"]}},
        }
    )
    pairs = tools.rewrite_pairs(m, "n", Path("/h/.local/share/sk-tools"), Path("/h"))
    assert all(hasattr(old, "pattern") for old, _ in pairs), "only versioned-export regexes"


def test_instance_relative_path_ok():
    spec = ToolConfigSpec(repo="a/b", ref="v1", instance="render/chiap04")
    assert spec.instance == "render/chiap04"
    assert ToolConfigSpec(repo="a/b", ref="v1", instance="node1").instance == "node1"


@pytest.mark.parametrize("bad", ["../x", "/abs", "render/../x", ""])
def test_instance_rejects_escape(bad):
    with pytest.raises(ValidationError):
        ToolConfigSpec(repo="a/b", ref="v1", instance=bad)


def _render(unit_text: str, tmp_path: Path):
    home = tmp_path / "home"
    root = home / ".local/share/sk-tools"
    dest = tmp_path / "exp"
    (dest / "node1/systemd").mkdir(parents=True)
    (dest / "node1/systemd/a.service").write_text(unit_text)
    (dest / tools._META).write_text(json.dumps({"url": "u", "sha": "0" * 40}))
    m = Manifest.model_validate(
        {
            "packages": {},
            "tools": {
                "fw": {"repo": "a/b", "ref": "v1"},
                "sk": {"repo": "a/c", "ref": "v1"},
            },
            "nodes": {
                "n": {
                    "tools": ["fw", "sk"],
                    "configs": {"fw": {"repo": "a/cfg", "ref": "v1", "instance": "node1"}},
                }
            },
        }
    )
    return tools.render_units(m, "n", root, home, {"fw-config": dest})


def test_render_tool_placeholder(tmp_path):
    out = _render("[Service]\nExecStart=@TOOL:sk@/bin/run @TOOL@/x\n", tmp_path)
    text = out["a.service"]
    base = "%h/.local/share/sk-tools"
    assert f"ExecStart={base}/sk/current/bin/run {base}/fw/current/x" in text


def test_render_unknown_tool_placeholder(tmp_path):
    with pytest.raises(tools.ToolError, match="unknown tool nope in a.service"):
        _render("[Service]\nExecStart=@TOOL:nope@/bin/run\n", tmp_path)
