"""A cluster manifest carrying a public stack: schema relaxations, stacks and roles."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from skcapstone.deploy import ManifestUnavailableError, resolve_manifest, tools
from skcapstone.deploy.manifest import Manifest, ToolConfigSpec, ToolSpec, load_manifest
from skcapstone.deploy.stack import StackFile, merge_stacks


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


# --------------------------------------------------------------- stacks and roles


def _stack_yaml(version: str, skcall_ref: str = "v2") -> dict:
    return {
        "stack": "sidekick",
        "version": version,
        "config_schema": 1,
        "tools": {
            "hermes": {
                "repo": "smilinTux/hermes-skworld",
                "ref": "v1",
                "setup": ["@STACK:sidekick@/bin/setup-hermes", "--quiet"],
            },
            "skcall": {"repo": "smilinTux/skcall", "ref": skcall_ref},
            "render": {"repo": "smilinTux/render", "ref": "v1"},
        },
        # restart order: skcall first, the gateway last
        "services": {
            "skcall.service": {"smoke": "@STACK:sidekick@/bin/check skcall"},
            "hermes-gateway.service": {"smoke": "true"},
            "render.service": {},
        },
        "roles": {
            # listed out of stack order on purpose: services keep stack order
            "agent": {
                "tools": ["hermes", "skcall"],
                "services": ["hermes-gateway.service", "skcall.service"],
            },
            "render": {"tools": ["render"], "services": ["render.service"]},
        },
    }


def _cluster(stack_url: str, **tools_extra) -> dict:
    return {
        "schema": 1,
        "packages": {},
        "tools": {"skgateway": {"repo": "smilinTux/skgateway", "ref": "v1"}, **tools_extra},
        "services": {"skgateway-chiap01.service": {"smoke": "true"}},
        "stacks": {"sidekick": {"url": stack_url, "ref": "v0.1.0"}},
        "nodes": {
            "chiap01": {"tools": ["skgateway"], "services": ["skgateway-chiap01.service"]},
            "chiap04": {
                "packages": [],
                "tools": [],
                "services": [],
                "roles": {"sidekick": ["agent"]},
            },
            "chiap02": {
                "packages": [],
                "roles": {"sidekick": ["agent"]},
                "stacks": {"sidekick": "v0.1.1-rc.1"},
            },
            "default": {"tools": "all"},
        },
    }


SHA = "0123456789abcdef0123456789abcdef01234567"


def _sh(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def stack_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "sidekick"
    repo.mkdir()
    (repo / "bin").mkdir()
    (repo / "bin/check").write_text("#!/bin/sh\n")
    _sh(repo, "init", "-q", "-b", "main")
    (repo / "stack.yaml").write_text(yaml.safe_dump(_stack_yaml("0.1.0"), sort_keys=False))
    _sh(repo, "add", ".")
    _sh(repo, "commit", "-qm", "v0.1.0")
    _sh(repo, "tag", "v0.1.0")
    (repo / "stack.yaml").write_text(
        yaml.safe_dump(_stack_yaml("0.1.1-rc.1", skcall_ref="v3"), sort_keys=False)
    )
    _sh(repo, "commit", "-qam", "rc")
    _sh(repo, "tag", "v0.1.1-rc.1")
    _sh(repo, "checkout", "-qb", "side")
    (repo / "stack.yaml").write_text(yaml.safe_dump(_stack_yaml("9.9.9"), sort_keys=False))
    _sh(repo, "commit", "-qam", "side")
    _sh(repo, "tag", "v9.9.9-side")
    _sh(repo, "checkout", "-q", "main")
    return repo


def _write_manifest(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def test_roles_expand_to_tools_and_services(tmp_path, stack_repo):
    url = f"file://{stack_repo}"
    m, _mid = resolve_manifest(
        tmp_path / "state", path=str(_write_manifest(tmp_path, _cluster(url))), node="chiap04"
    )
    assert set(m.tools_for("chiap04")) == {"hermes", "skcall", "sidekick-release"}
    assert list(m.services_for("chiap04")) == ["skcall.service", "hermes-gateway.service"]
    release = m.tools["sidekick-release"]
    # pinned to the commit stack.yaml was read at, not the movable tag
    tag_sha = _sh(stack_repo, "rev-parse", "v0.1.0^{commit}")
    assert (release.url, release.ref, release.branch) == (url, tag_sha, "main")
    assert release.checkouts == []
    # R2: roles are the only way in; tools: all means every cluster tool only
    assert set(m.tools_for("chiap01")) == {"skgateway"}
    assert set(m.tools_for("elsewhere")) == {"skgateway"}
    assert list(m.services_for("elsewhere")) == ["skgateway-chiap01.service"]
    assert set(m.tools_for("chiap02")) == {"skgateway", "hermes", "skcall", "sidekick-release"}
    assert list(m.services_for("chiap02")) == [
        "skgateway-chiap01.service",
        "skcall.service",
        "hermes-gateway.service",
    ]
    # the mirror is a bare repo under the deploy state dir
    assert (tmp_path / "state/stacks/sidekick/HEAD").is_file()


def test_unknown_role_refused():
    stack = StackFile.model_validate(_stack_yaml("0.1.0"))
    data = _cluster("file:///x")
    data["nodes"]["chiap04"]["roles"] = {"sidekick": ["nope"]}
    with pytest.raises(ValueError, match="chiap04.*role nope.*sidekick"):
        merge_stacks(data, {"sidekick": stack}, {"sidekick": SHA})


def test_stack_file_role_entries_must_exist():
    bad = _stack_yaml("0.1.0")
    bad["roles"]["agent"]["tools"].append("ghost")
    with pytest.raises(ValidationError, match="ghost"):
        StackFile.model_validate(bad)
    bad = _stack_yaml("0.1.0")
    bad["roles"]["render"]["services"] = ["ghost.service"]
    with pytest.raises(ValidationError, match="ghost.service"):
        StackFile.model_validate(bad)


def test_stack_clash_refused():
    stack = StackFile.model_validate(_stack_yaml("0.1.0"))
    data = _cluster("file:///x", skcall={"repo": "smilinTux/skcall", "ref": "v1"})
    with pytest.raises(ValueError, match="tool skcall.*cluster manifest.*stack sidekick"):
        merge_stacks(data, {"sidekick": stack}, {"sidekick": SHA})
    data = _cluster("file:///x")
    data["services"]["skcall.service"] = {}
    with pytest.raises(ValueError, match="service skcall.service.*cluster manifest.*sidekick"):
        merge_stacks(data, {"sidekick": stack}, {"sidekick": SHA})


def test_node_canary_ref(tmp_path, stack_repo):
    path = str(_write_manifest(tmp_path, _cluster(f"file://{stack_repo}")))
    canary, _ = resolve_manifest(tmp_path / "state", path=path, node="chiap02")
    rc_sha = _sh(stack_repo, "rev-parse", "v0.1.1-rc.1^{commit}")
    assert canary.tools["sidekick-release"].ref == rc_sha
    assert canary.tools["skcall"].ref == "v3"
    m, _ = resolve_manifest(tmp_path / "state", path=path, node="chiap04")
    assert m.tools["sidekick-release"].ref == _sh(stack_repo, "rev-parse", "v0.1.0^{commit}")
    assert m.tools["skcall"].ref == "v2"
    # a node without roles takes no stack at all (I2)
    m, _ = resolve_manifest(tmp_path / "state", path=path, node="chiap01")
    assert "sidekick-release" not in m.tools and "skcall" not in m.tools


@pytest.mark.parametrize("ref", ["v7.7.7-missing", "v9.9.9-side"])
def test_stack_ref_missing_or_off_main_refused(tmp_path, stack_repo, ref):
    data = _cluster(f"file://{stack_repo}")
    data["nodes"]["chiap02"]["stacks"] = {"sidekick": ref}
    path = str(_write_manifest(tmp_path, data))
    with pytest.raises(ManifestUnavailableError, match=f"sidekick.*{ref}"):
        resolve_manifest(tmp_path / "state", path=path, node="chiap02")
    # refused while reading the manifest: no export was attempted
    assert not (tmp_path / "home/.local/share/sk-tools").exists()
    assert not list((tmp_path / "state").glob("tool-src/*"))
    # the main ref still resolves for the other nodes
    assert resolve_manifest(tmp_path / "state", path=path, node="chiap04")[0].tools[
        "sidekick-release"
    ].ref == _sh(stack_repo, "rev-parse", "v0.1.0^{commit}")


def test_stack_placeholder_in_smoke(tmp_path):
    stack = StackFile.model_validate(_stack_yaml("0.1.0"))
    data = _cluster("file:///x")
    data["smoke"] = ["test -x @STACK:sidekick@/bin/check"]
    root = tmp_path / "home/.local/share/sk-tools"
    m = merge_stacks(data, {"sidekick": stack}, {"sidekick": SHA}, tools_root=root)
    base = f"{root}/sidekick-release/current"
    assert m.services["skcall.service"].smoke == f"{base}/bin/check skcall"
    # setup runs at export time, before current is published: the versioned export
    pinned = f"{root}/sidekick-release/{SHA[:12]}"
    assert m.tools["hermes"].setup == [f"{pinned}/bin/setup-hermes", "--quiet"]
    assert m.smoke == [f"test -x {base}/bin/check"]
    data["services"]["skgateway-chiap01.service"]["smoke"] = "@STACK:nope@/x"
    with pytest.raises(ValueError, match="unknown stack nope"):
        merge_stacks(data, {"sidekick": stack}, {"sidekick": SHA}, tools_root=root)


# sk-release-chi main at 334b2771 (2026-11-15), comments dropped.
CHI_MANIFEST = """
schema: 1
python: '3.12'
packages: {}
smoke: []
services:
  skgateway-chiap01.service:
    packages: []
    smoke: >-
      for i in $(seq 45); do curl -sf -m3 http://127.0.0.1:18790/health >/dev/null && break;
      sleep 2; done; curl -sf -m5 http://127.0.0.1:18790/health | grep -q '"status":"ok"'
tools:
  skgateway:
    repo: smilinTux/skgateway
    ref: skgateway-v1.1.17
    checkout: ~/.local/share/skgateway/releases/skgateway-v1.1.17
    setup: [npm, ci, --omit=dev]
    reason: Node server, not a Python package.
nodes:
  chiap01:
    packages: []
    tools: [skgateway]
    services: [skgateway-chiap01.service]
    configs:
      skgateway:
        url: git@github-skgateway-config-chi:smilinTux/skgateway-config-chi.git
        ref: b2fdac5dc245b70bd51277b14fa5e90d8b9db14a
        instance: chiap01
"""


def test_manifest_without_stacks_unchanged(tmp_path, monkeypatch):
    import skcapstone.deploy.stack as stack_mod

    def _no_network(*_a, **_k):
        raise AssertionError("a manifest without stacks must not fetch anything")

    monkeypatch.setattr(stack_mod, "fetch_stack", _no_network)
    path = tmp_path / "manifest.yaml"
    path.write_text(CHI_MANIFEST)
    want = load_manifest(path)
    state = tmp_path / "state"
    for node in (None, "chiap01", "chiap04"):
        got, _ = resolve_manifest(state, path=str(path), node=node)
        assert got == want
        assert got.model_dump() == want.model_dump()
    assert not (state / "stacks").exists()
    # and read from a manifest repo clone, offline
    origin = tmp_path / "origin"
    origin.mkdir()
    (origin / "manifest.yaml").write_text(CHI_MANIFEST)
    _sh(origin, "init", "-q", "-b", "main")
    _sh(origin, "add", ".")
    _sh(origin, "commit", "-qm", "chi")
    subprocess.run(["git", "clone", "-q", str(origin), str(state / "manifest-repo")], check=True)
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_URL", str(origin))
    monkeypatch.delenv("SK_DEPLOY_MANIFEST", raising=False)
    got, _ = resolve_manifest(state, fetch=False, node="chiap01")
    assert got == want


def _local_stack(tmp_path: Path) -> tuple[Path, Path]:
    """A stack whose agent role is one local tool provisioned by the stack's script."""
    tool = tmp_path / "hermes"
    tool.mkdir()
    (tool / "run.sh").write_text("#!/bin/sh\n")
    _sh(tool, "init", "-q", "-b", "main")
    _sh(tool, "add", ".")
    _sh(tool, "commit", "-qm", "hermes")
    repo = tmp_path / "sidekick-local"
    (repo / "bin").mkdir(parents=True)
    setup = repo / "bin/setup-hermes"
    setup.write_text('#!/bin/sh\necho "$0" > provisioned-by\n')
    setup.chmod(0o755)
    stack = {
        "stack": "sidekick",
        "version": "0.1.0",
        "config_schema": 1,
        "tools": {
            "hermes": {
                "url": f"file://{tool}",
                "ref": "main",
                "setup": ["@STACK:sidekick@/bin/setup-hermes"],
            }
        },
        "services": {},
        "roles": {"agent": {"tools": ["hermes"]}},
    }
    (repo / "stack.yaml").write_text(yaml.safe_dump(stack, sort_keys=False))
    _sh(repo, "init", "-q", "-b", "main")
    _sh(repo, "add", ".")
    _sh(repo, "commit", "-qm", "v0.1.0")
    _sh(repo, "tag", "v0.1.0")
    return repo, tool


def _real_runner(cmd, **kw):
    if cmd[:1] == ["systemctl"]:
        return subprocess.CompletedProcess(cmd, 0, "", "")
    return subprocess.run(cmd, **kw)


def test_stack_setup_runs_on_a_fresh_node(tmp_path, monkeypatch):
    repo, _tool = _local_stack(tmp_path)
    home = tmp_path / "home"
    (home / ".config/systemd/user").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    data = {
        "packages": {},
        "stacks": {"sidekick": {"url": f"file://{repo}", "ref": "v0.1.0"}},
        "nodes": {"chiap04": {"tools": [], "services": [], "roles": {"sidekick": ["agent"]}}},
    }
    state = tmp_path / "state"
    m, _ = resolve_manifest(state, path=str(_write_manifest(tmp_path, data)), node="chiap04")
    assert list(m.tools_for("chiap04")) == ["sidekick-release", "hermes"], "release first"
    root = home / ".local/share/sk-tools"
    assert not root.exists(), "a fresh node: no current anywhere"
    change = tools.apply_tools(
        m,
        "chiap04",
        state,
        root=root,
        unit_dir=home / ".config/systemd/user",
        home=home,
        runner=_real_runner,
    )
    sha12 = _sh(repo, "rev-parse", "v0.1.0^{commit}")[:12]
    assert change.exported["sidekick-release"] == sha12
    ran = (root / "hermes/current/provisioned-by").read_text().strip()
    assert ran == f"{root}/sidekick-release/{sha12}/bin/setup-hermes"
    assert (root / "sidekick-release/current").resolve() == root / "sidekick-release" / sha12


def test_role_tool_may_carry_a_node_config(tmp_path, stack_repo):
    data = _cluster(f"file://{stack_repo}")
    cfg = {"url": "file:///cfg", "ref": "v1", "instance": "chiap04"}
    data["nodes"]["chiap04"]["configs"] = {"hermes": cfg, "sidekick-release": cfg}
    path = str(_write_manifest(tmp_path, data))
    m, _ = resolve_manifest(tmp_path / "state", path=path, node="chiap04")
    assert set(m.configs_for("chiap04")) == {"hermes", "sidekick-release"}
    # render is in the stack, but not in the agent role: still refused
    data["nodes"]["chiap04"]["configs"] = {"render": cfg}
    path = str(_write_manifest(tmp_path, data))
    with pytest.raises(ValidationError, match="config for tool.*render"):
        resolve_manifest(tmp_path / "state", path=path, node="chiap04")
    # and a node without roles is checked before any stack is read
    data["nodes"]["chiap04"]["configs"] = {}
    data["nodes"]["chiap01"]["configs"] = {"hermes": cfg}
    path = str(_write_manifest(tmp_path, data))
    with pytest.raises(ValidationError, match="config for tool.*hermes"):
        resolve_manifest(tmp_path / "state", path=path, node="chiap01")


# ------------------------------------------------- final review: I2 and M4


def _no_stack_view(data: dict) -> dict:
    """*data* as if it had no stacks: no ``stacks:`` and no node that takes one."""
    import copy

    plain = copy.deepcopy(data)
    plain.pop("stacks")
    plain["nodes"] = {
        n: p for n, p in plain["nodes"].items() if not (p.get("roles") or p.get("stacks"))
    }
    return plain


@pytest.mark.parametrize("node", ["chiap01", "elsewhere"])
@pytest.mark.parametrize("fetch", [True, False])
def test_roleless_node_does_not_depend_on_a_stack(tmp_path, monkeypatch, node, fetch):
    import skcapstone.deploy.stack as stack_mod

    def _unreachable(*_a, **_k):
        raise tools.ToolError("stack sidekick: fetch failed: unreachable")

    monkeypatch.setattr(stack_mod, "fetch_stack", _unreachable)
    data = _cluster("file:///nowhere/sidekick")
    got, _ = resolve_manifest(
        tmp_path / "state", path=str(_write_manifest(tmp_path, data)), node=node, fetch=fetch
    )
    plain = tmp_path / "plain.yaml"
    plain.write_text(yaml.safe_dump(_no_stack_view(data), sort_keys=False))
    want = load_manifest(plain)
    assert got.stacks == {}
    assert got.tools == want.tools and got.services == want.services
    assert got.smoke == want.smoke and got.packages == want.packages
    assert got.tools_for(node) == want.tools_for(node)
    assert got.services_for(node) == want.services_for(node)
    assert got.configs_for(node) == want.configs_for(node)
    assert "sidekick-release" not in got.tools


def test_role_node_still_refuses_a_broken_stack(tmp_path):
    data = _cluster("file:///nowhere/sidekick")
    path = str(_write_manifest(tmp_path, data))
    with pytest.raises(ManifestUnavailableError, match="sidekick"):
        resolve_manifest(tmp_path / "state", path=path, node="chiap04")


def test_stack_placeholder_only_for_stacks_the_node_takes(tmp_path, stack_repo):
    data = _cluster(f"file://{stack_repo}")
    data["services"]["skgateway-chiap01.service"]["smoke"] = "@STACK:sidekick@/bin/check gw"
    path = str(_write_manifest(tmp_path, data))
    m, _ = resolve_manifest(tmp_path / "state", path=path, node="chiap04")
    assert (
        "/sidekick-release/current/bin/check gw" in m.services["skgateway-chiap01.service"].smoke
    )
    with pytest.raises(ValueError, match="unknown stack sidekick"):
        resolve_manifest(tmp_path / "state", path=path, node="chiap01")


@pytest.mark.parametrize("name", ["hermes", "sidekick-release"])
def test_node_naming_a_stack_tool_directly_gets_a_hint(tmp_path, stack_repo, name):
    data = _cluster(f"file://{stack_repo}")
    data["nodes"]["chiap01"]["tools"] = ["skgateway", name]
    path = str(_write_manifest(tmp_path, data))
    with pytest.raises(ValueError, match=r"unknown tool.*stack tools come only through roles"):
        resolve_manifest(tmp_path / "state", path=path, node="chiap01")
    data["nodes"]["chiap01"]["tools"] = ["skgateway", "ghost"]
    path = str(_write_manifest(tmp_path, data))
    with pytest.raises(ValueError, match="unknown tool") as exc:
        resolve_manifest(tmp_path / "state", path=path, node="chiap01")
    assert "come only through roles" not in str(exc.value)
