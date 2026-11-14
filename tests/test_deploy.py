"""Tests for skcapstone.deploy: manifest, plan, pre-flight, switch, apply, drift."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from skcapstone.deploy import drift as drift_mod
from skcapstone.deploy import resolve_manifest, rollout
from skcapstone.deploy.inspect import Dist, exec_paths_in_trees, git_tree_of
from skcapstone.deploy.manifest import Manifest, load_manifest

MANIFEST = {
    "schema": 1,
    "packages": {
        "skfoo": {"source": "pypi", "version": "1.2.0", "repo": "o/skfoo"},
        "skbar": {"source": "git", "repo": "o/skbar", "ref": "v0.3.0", "branch": "master"},
    },
    "requirements": ["uvicorn==0.30.6"],
    "services": {
        "skfoo.service": {"packages": ["skfoo"], "smoke": "true"},
        "skbar.service": {"packages": ["skbar"]},
    },
    "smoke": ["true"],
    "nodes": {
        "default": {"packages": "all"},
        "small": {"packages": ["skfoo"], "services": ["skfoo.service"]},
    },
}


def _m(**over) -> Manifest:
    data = json.loads(json.dumps(MANIFEST))
    data.update(over)
    return Manifest.model_validate(data)


class FakeRunner:
    """Records commands and answers them from a rule list."""

    def __init__(self, rules=None):
        self.calls: list[list[str]] = []
        self.rules = rules or []

    def __call__(self, cmd, **kw):
        self.calls.append(list(cmd))
        for match, rc, out in self.rules:
            if match(cmd):
                return subprocess.CompletedProcess(cmd, rc, out, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")


# ------------------------------------------------------------------ manifest


def test_manifest_requirement_lines_pin_every_package():
    lines = _m().requirement_lines()
    assert lines[0] == "skfoo==1.2.0"
    assert lines[1] == "skbar @ git+https://github.com/o/skbar@v0.3.0"
    assert lines[-1] == "uvicorn==0.30.6"


def test_manifest_node_profile_narrows_packages_and_services():
    m = _m()
    assert list(m.packages_for("small")) == ["skfoo"]
    assert list(m.services_for("small")) == ["skfoo.service"]
    assert set(m.packages_for("unknown-host")) == {"skfoo", "skbar"}


def test_manifest_rejects_unknown_service_package():
    data = json.loads(json.dumps(MANIFEST))
    data["services"]["x.service"] = {"packages": ["nope"]}
    with pytest.raises(ValueError, match="unknown package"):
        Manifest.model_validate(data)


def test_manifest_rejects_pypi_without_version(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("schema: 1\npackages:\n  a: {source: pypi, repo: o/a}\n")
    with pytest.raises(ValueError, match="needs a version"):
        load_manifest(p)


def test_release_tag_template():
    m = Manifest.model_validate(
        {"packages": {"skbrain": {"version": "1.0.1", "repo": "o/b", "tag": "skbrain-v{version}"}}}
    )
    assert m.packages["skbrain"].release_tag() == "skbrain-v1.0.1"


# ----------------------------------------------------------------- preflight


def test_preflight_accepts_tags_on_main():
    runner = FakeRunner([(lambda c: c[0] == "gh", 0, "behind\n")])
    rollout.preflight(_m(), runner)
    assert any("o/skbar/compare/master...v0.3.0" in " ".join(c) for c in runner.calls)


def test_preflight_refuses_a_tag_not_on_main():
    runner = FakeRunner(
        [
            (lambda c: "o/skfoo" in " ".join(c), 0, "diverged\n"),
            (lambda c: "o/skbar" in " ".join(c), 1, ""),
        ]
    )
    with pytest.raises(rollout.DeployError) as exc:
        rollout.preflight(_m(), runner)
    assert "diverged" in str(exc.value) and "not found" in str(exc.value)


# ---------------------------------------------------------------------- plan


def test_plan_flags_editable_and_version_change(monkeypatch):
    dists = [Dist("skfoo", "1.1.0", False, ""), Dist("skbar", "0.3.0", True, "file:///tree")]
    monkeypatch.setattr(rollout, "list_dists", lambda _p: dists)
    plan = rollout.make_plan(_m(), Path("/x/python"))
    assert plan.changed == {"skfoo": ("1.1.0", "1.2.0"), "skbar": ("0.3.0", "v0.3.0")}
    assert plan.editable == ["skbar"]
    assert sorted(plan.restarts) == ["skbar.service", "skfoo.service"]


def test_plan_empty_when_node_matches(monkeypatch):
    dists = [Dist("skfoo", "1.2.0", False, ""), Dist("skbar", "0.3.0", False, "")]
    monkeypatch.setattr(rollout, "list_dists", lambda _p: dists)
    assert rollout.make_plan(_m(), Path("/x/python")).empty


# -------------------------------------------------------------------- switch


def _paths(tmp_path: Path) -> rollout.Paths:
    return rollout.Paths(tmp_path / ".skenv", tmp_path / ".skenv-releases", tmp_path / "state")


def test_switch_moves_a_directory_venv_aside_then_symlinks(tmp_path):
    paths = _paths(tmp_path)
    paths.venv_link.mkdir()
    (paths.venv_link / "marker").write_text("old")
    new = tmp_path / "new"
    new.mkdir()
    previous = rollout.switch(paths, new)
    assert paths.venv_link.is_symlink() and paths.venv_link.resolve() == new
    assert previous.name.startswith("legacy-") and (previous / "marker").read_text() == "old"
    assert rollout.switch(paths, previous) == new
    assert paths.venv_link.resolve() == previous


# --------------------------------------------------------------------- apply


def _fake_build(monkeypatch, problems=()):
    def build(manifest, dest, python, runner, node):
        (dest / "bin").mkdir(parents=True)

    monkeypatch.setattr(rollout, "build_venv", build)
    monkeypatch.setattr(rollout, "verify_venv", lambda *a, **k: list(problems))
    monkeypatch.setattr(rollout, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(rollout, "list_dists", lambda _p: [Dist("skfoo", "1.1.0", False, "")])
    monkeypatch.setattr(rollout.time, "sleep", lambda _s: None)


def test_apply_does_not_switch_when_verify_fails(tmp_path, monkeypatch):
    _fake_build(monkeypatch, problems=["import skfoo: boom"])
    paths = _paths(tmp_path)
    (paths.venv_link / "bin").mkdir(parents=True)
    (paths.venv_link / "bin" / "python").write_text("")
    res = rollout.apply(_m(), "abc123", paths, "3.12", FakeRunner())
    assert not res.ok and not res.rolled_back
    assert not paths.venv_link.is_symlink()


def test_apply_rolls_back_when_a_restarted_unit_dies(tmp_path, monkeypatch):
    _fake_build(monkeypatch)
    paths = _paths(tmp_path)
    (paths.venv_link / "bin").mkdir(parents=True)
    (paths.venv_link / "bin" / "python").write_text("")
    answers = iter(["active\n", "failed\n"])  # before restart, after restart

    def runner(cmd, **kw):
        out = ""
        if cmd[:3] == ["systemctl", "--user", "is-active"]:
            out = next(answers, "active\n")
        return subprocess.CompletedProcess(cmd, 0, out, "")

    res = rollout.apply(_m(), "abc123", paths, "3.12", runner)
    assert not res.ok and res.rolled_back
    assert paths.venv_link.resolve() == res.previous.resolve()
    assert res.previous.name.startswith("legacy-")


def test_apply_succeeds_and_records_state(tmp_path, monkeypatch):
    _fake_build(monkeypatch)
    paths = _paths(tmp_path)
    (paths.venv_link / "bin").mkdir(parents=True)
    (paths.venv_link / "bin" / "python").write_text("")
    runner = FakeRunner([(lambda c: c[:3] == ["systemctl", "--user", "is-active"], 0, "active\n")])
    res = rollout.apply(_m(), "abc123def456", paths, "3.12", runner)
    assert res.ok, res.problems
    assert sorted(res.restarted) == ["skbar.service", "skfoo.service"]  # skbar not installed yet
    state = rollout.load_state(paths)
    assert state["current"]["manifest"] == "abc123def456"
    assert state["previous"]["release"].endswith(res.previous.name)


def test_rollback_without_previous_refuses(tmp_path):
    with pytest.raises(rollout.DeployError):
        rollout.rollback(_paths(tmp_path), [], FakeRunner())


# --------------------------------------------------------------------- drift


def _home_with_tree(tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    tree = home / "repo"
    (tree / ".git").mkdir(parents=True)
    (tree / "scripts").mkdir()
    units = home / ".config" / "systemd" / "user"
    units.mkdir(parents=True)
    (units / "a.service").write_text(
        "[Service]\nExecStart=/usr/bin/python3 %h/repo/scripts/run.py --x\n"
    )
    (units / "b.service").write_text("[Service]\nExecStart=%h/.skenv/bin/skfoo daemon\n")
    (home / ".claude.json").write_text(
        json.dumps({"mcpServers": {"m": {"command": str(tree / "mcp")}}})
    )
    return home, units


def test_exec_paths_in_trees_finds_units_and_mcp(tmp_path):
    home, units = _home_with_tree(tmp_path)
    hits = exec_paths_in_trees(home, units)
    origins = sorted(ep.origin for ep, _t in hits)
    assert origins == ["claude mcp m", "unit a.service"]
    assert git_tree_of(str(home / ".skenv/bin/x")) is None


def test_exec_paths_allowlist(tmp_path):
    home, units = _home_with_tree(tmp_path)
    assert exec_paths_in_trees(home, units, allow=[str(home / "repo")]) == []


def test_drift_compute_and_alert_dedupe(tmp_path, monkeypatch):
    home, units = _home_with_tree(tmp_path)
    monkeypatch.setattr(
        drift_mod,
        "list_dists",
        lambda _p: [Dist("skfoo", "1.1.0", True, "file:///t"), Dist("skbar", "0.3.0", False, "")],
    )
    report = drift_mod.compute(home, Path("/x/python"), _m(), unit_dir=units)
    assert report.editable == ["skfoo 1.1.0 -> /t"]
    assert report.behind == ["skfoo 1.1.0 != manifest 1.2.0"]
    assert len(report.exec_in_tree) == 2

    sent: list[list[str]] = []

    def fake_run(cmd, **kw):
        sent.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(drift_mod.subprocess, "run", fake_run)
    state = tmp_path / "s.json"
    assert drift_mod.alert_on_change(report, state, sender="sk-alert") == "sent"
    assert drift_mod.alert_on_change(report, state, sender="sk-alert") == "unchanged"
    assert len(sent) == 1 and "deploy/drift/" in " ".join(sent[0])
    clean = drift_mod.DriftReport()
    assert drift_mod.alert_on_change(clean, state, sender="sk-alert") == "resolved"
    assert "--resolve" in sent[-1]
    assert drift_mod.alert_on_change(clean, state, sender="sk-alert") == "unchanged"


def test_drift_without_manifest_is_unknown_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(drift_mod, "list_dists", lambda _p: [])
    report = drift_mod.compute(tmp_path, Path("/x/python"), None, unit_dir=tmp_path / "none")
    assert report.lines() == [] and report.unknown


def test_list_dists_runs_against_a_real_interpreter():
    from skcapstone.deploy.inspect import list_dists

    names = {d.name.lower() for d in list_dists(Path(sys.executable))}
    assert "pyyaml" in names


# ------------------------------------------------------------------ manifest repo


def test_resolve_manifest_reads_origin_main_and_refuses_off_main_ref(tmp_path, monkeypatch):
    origin = tmp_path / "origin"
    origin.mkdir()

    def git(*a):
        return subprocess.run(
            ["git", *a], cwd=origin, check=True, capture_output=True, text=True
        ).stdout.strip()

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    import yaml

    (origin / "manifest.yaml").write_text(yaml.safe_dump(MANIFEST))
    git("add", ".")
    git("commit", "-qm", "one")
    first = git("rev-parse", "HEAD")
    git("checkout", "-qb", "side")
    (origin / "manifest.yaml").write_text(yaml.safe_dump({**MANIFEST, "smoke": []}))
    git("commit", "-qam", "side")
    side = git("rev-parse", "HEAD")
    git("checkout", "-q", "main")

    state = tmp_path / "state"
    subprocess.run(["git", "clone", "-q", str(origin), str(state / "manifest-repo")], check=True)
    monkeypatch.setenv("SK_DEPLOY_MANIFEST_REPO", "o/sk-release")
    monkeypatch.delenv("SK_DEPLOY_MANIFEST", raising=False)
    manifest, mid = resolve_manifest(state, fetch=False)
    assert mid == first and manifest.smoke == ["true"]
    subprocess.run(
        ["git", "-C", str(state / "manifest-repo"), "fetch", "-q", "origin", "side"], check=True
    )
    from skcapstone.deploy import ManifestUnavailableError

    with pytest.raises(ManifestUnavailableError, match="not on the manifest repo's main"):
        resolve_manifest(state, ref=side, fetch=False)


# --------------------------------------------------------------------- doctor


def test_doctor_deploy_checks_shape(monkeypatch, tmp_path):
    from skcapstone import doctor

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("SK_DEPLOY_MANIFEST_REPO", raising=False)
    monkeypatch.delenv("SK_DEPLOY_MANIFEST", raising=False)
    checks = doctor._check_deploy()
    names = [c.name for c in checks]
    assert names == ["deploy:editable", "deploy:exec-in-tree", "deploy:manifest"]
    manifest_check = checks[-1]
    assert manifest_check.unknown and not manifest_check.passed


def test_editable_exception_is_declared_but_still_drift(monkeypatch, tmp_path):
    data = json.loads(json.dumps(MANIFEST))
    data["packages"]["skingest"] = {
        "source": "editable",
        "repo": "o/skingest",
        "path": "/t/skingest",
        "reason": "unmerged fix branch",
    }
    m = Manifest.model_validate(data)
    assert "-e /t/skingest" in m.requirement_lines()
    runner = FakeRunner([(lambda c: c[0] == "gh", 0, "identical\n")])
    rollout.preflight(m, runner)
    assert not any("skingest" in " ".join(c) for c in runner.calls)
    monkeypatch.setattr(
        drift_mod, "list_dists", lambda _p: [Dist("skingest", "0.2.0", True, "file:///t/skingest")]
    )
    report = drift_mod.compute(tmp_path, Path("/x/python"), m, unit_dir=tmp_path / "none")
    assert report.editable == [
        "skingest 0.2.0 -> /t/skingest (declared exception: unmerged fix branch)"
    ]


def test_editable_exception_needs_reason():
    with pytest.raises(ValueError, match="path and a reason"):
        Manifest.model_validate(
            {"packages": {"a": {"source": "editable", "repo": "o/a", "path": "/x"}}}
        )


def test_build_venv_passes_requirements_and_constraints(tmp_path):
    seen: dict[str, str] = {}

    def runner(cmd, **kw):
        if cmd[:3] == ["uv", "pip", "install"]:
            seen["r"] = Path(cmd[cmd.index("-r") + 1]).read_text()
            seen["c"] = Path(cmd[cmd.index("-c") + 1]).read_text()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    m = _m(constraints=["numpy==2.3.4"])
    rollout.build_venv(m, tmp_path / "rel" / "new", "3.12", runner)
    assert "skfoo==1.2.0" in seen["r"] and "uvicorn==0.30.6" in seen["r"]
    assert seen["c"].strip() == "numpy==2.3.4"
    assert not list((tmp_path / "rel").glob(".*.txt"))


def test_build_venv_refuses_existing_dest(tmp_path):
    (tmp_path / "d").mkdir()
    with pytest.raises(rollout.DeployError, match="already exists"):
        rollout.build_venv(_m(), tmp_path / "d", "3.12", FakeRunner())


def test_lock_replaces_requirements_and_installs_no_deps(tmp_path):
    seen: dict[str, object] = {}

    def runner(cmd, **kw):
        if cmd[:3] == ["uv", "pip", "install"]:
            seen["cmd"] = list(cmd)
            seen["r"] = Path(cmd[cmd.index("-r") + 1]).read_text()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    m = _m(lock=["numpy==2.3.4", "uvicorn==0.30.1"])
    rollout.build_venv(m, tmp_path / "rel" / "new", "3.12", runner)
    assert "--no-deps" in seen["cmd"]
    assert "uvicorn==0.30.1" in seen["r"] and "uvicorn==0.30.6" not in seen["r"]


def test_verify_flags_only_new_pip_check_problems(tmp_path, monkeypatch):
    live, new = tmp_path / "live", tmp_path / "new"
    for d in (live, new):
        (d / "bin").mkdir(parents=True)
    monkeypatch.setattr(rollout, "list_dists", lambda _p: [])

    def runner(cmd, **kw):
        if cmd[:3] == ["uv", "pip", "check"]:
            old = "a 1 has requirement b<2, but you have b 2"
            fresh = "skfoo 1.2.0 has requirement c>=3, but you have c 1"
            out = old if str(live) in cmd[-1] else f"{old}\n{fresh}"
            return subprocess.CompletedProcess(cmd, 1, out, "")
        return subprocess.CompletedProcess(cmd, 0, "[]", "")

    problems = rollout.verify_venv(_m(), new, live, runner)
    assert len(problems) == 1 and "skfoo 1.2.0" in problems[0] and "b<2" not in problems[0]


def test_carry_bin_copies_files_and_symlinks_but_not_over_packaged(tmp_path):
    live, dest = tmp_path / "live", tmp_path / "dest"
    (live / "bin").mkdir(parents=True)
    (dest / "bin").mkdir(parents=True)
    (live / "bin" / "shim").write_text("#!/bin/sh\necho hi\n")
    (live / "bin" / "shim").chmod(0o755)
    os.symlink("/somewhere/tool.py", live / "bin" / "tool")
    (live / "bin" / "pkg").write_text("old")
    (dest / "bin" / "pkg").write_text("packaged")
    m = _m(carry_bin=["shim", "tool", "pkg", "missing"])
    assert rollout.carry_bin(m, live, dest) == ["shim", "tool"]
    assert os.access(dest / "bin" / "shim", os.X_OK)
    assert os.readlink(dest / "bin" / "tool") == "/somewhere/tool.py"
    assert (dest / "bin" / "pkg").read_text() == "packaged"
