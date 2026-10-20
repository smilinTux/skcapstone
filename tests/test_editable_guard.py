"""Tests for the editable install guard (python policy + shell wrapper)."""

from __future__ import annotations

import stat
import subprocess
from pathlib import Path

import pytest

from skcapstone.fleet import editable_guard as eg

WRAPPER = Path(__file__).resolve().parents[1] / "scripts" / "pip-editable-guard.sh"


def _in_shared(monkeypatch, tmp_path: Path) -> Path:
    shared = tmp_path / ".skenv"
    (shared / "bin").mkdir(parents=True)
    exe = shared / "bin" / "python"
    exe.write_text("")
    monkeypatch.setattr(eg, "SHARED_INTERPRETERS", {shared.resolve()})
    monkeypatch.setattr(eg.sys, "executable", str(exe))
    return shared


def test_public_api_star_import_works():
    ns: dict = {}
    exec("from skcapstone.fleet.editable_guard import *", ns)
    for name in eg.__all__:
        assert name in ns


def test_private_venv_always_allowed(monkeypatch, tmp_path):
    monkeypatch.setattr(eg, "SHARED_INTERPRETERS", {(tmp_path / ".skenv").resolve()})
    monkeypatch.setattr(eg.sys, "executable", str(tmp_path / "other" / "bin" / "python"))
    monkeypatch.setenv("SKFLEET_NODE", "x")
    assert eg.block_editable_install() == "allow"


def test_service_host_blocks_shared(monkeypatch, tmp_path):
    _in_shared(monkeypatch, tmp_path)
    monkeypatch.setenv("SKFLEET_NODE", "chiap04")
    assert eg.block_editable_install(target_path=tmp_path) == "block"
    assert eg.is_editable_install_blocked() is True


def test_hostname_match_blocks_and_near_miss_does_not(monkeypatch, tmp_path):
    _in_shared(monkeypatch, tmp_path)
    monkeypatch.delenv("SKFLEET_NODE", raising=False)
    monkeypatch.setenv("HOSTNAME", "chiap04")
    assert eg.block_editable_install() == "block"
    monkeypatch.setenv("HOSTNAME", "chiap040")
    assert eg.block_editable_install() == "allow"
    monkeypatch.setenv("HOSTNAME", "")
    monkeypatch.setattr(eg.os, "uname", lambda: type("U", (), {"nodename": "chiap04x"})())
    assert eg.block_editable_install() == "allow"


def test_dev_machine_warns_for_scratch_target(monkeypatch, tmp_path):
    _in_shared(monkeypatch, tmp_path)
    monkeypatch.delenv("SKFLEET_NODE", raising=False)
    monkeypatch.setenv("HOSTNAME", "laptop")
    assert eg.block_editable_install(target_path=tmp_path / "scratch" / "pkg") == "warn"
    assert eg.block_editable_install(target_path=tmp_path / "pkg") == "allow"


def test_format_violation_messages():
    assert eg.format_violation("block").startswith("BLOCKED")
    assert "/x" in eg.format_violation("warn", Path("/x"))


def test_pip_query_failures_are_empty(tmp_path):
    assert eg._get_pip_editable_packages(tmp_path / "nope") == []


def test_audit_reports_editable(monkeypatch, tmp_path):
    shared = _in_shared(monkeypatch, tmp_path)
    monkeypatch.setattr(
        eg,
        "_get_pip_editable_packages",
        lambda p: [{"name": "a", "location": "", "path": tmp_path}],
    )
    assert list(eg.audit_editable_installs()) == [str(shared.resolve())]


def _run_wrapper(tmp_path: Path, host: str, args: list[str], extra=None):
    home = tmp_path / "home"
    (home / ".skenv" / "bin").mkdir(parents=True, exist_ok=True)
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    for name, body in (("pip", 'echo "REALPIP $*"'), ("python", "true")):
        f = fake / name
        f.write_text("#!/bin/sh\n" + body + "\n")
        f.chmod(f.stat().st_mode | stat.S_IEXEC)
    shared_py = home / ".skenv" / "bin" / "python"
    shared_py.write_text("#!/bin/sh\n")
    shared_py.chmod(0o755)
    env = {
        "PATH": f"{home / '.skenv' / 'bin'}:{fake}:/usr/bin:/bin",
        "HOME": str(home),
        "HOSTNAME": host,
    }
    env.update(extra or {})
    return subprocess.run(["bash", str(WRAPPER), *args], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("flag", [["-e", "."], ["--editable", "."], ["--editable=."]])
def test_wrapper_blocks_editable_on_service_host(tmp_path, flag):
    r = _run_wrapper(tmp_path, "chiap04", ["install", *flag])
    assert r.returncode == 1
    assert "BLOCKED" in r.stderr
    assert "REALPIP" not in r.stdout


def test_wrapper_allows_plain_install_and_other_hosts(tmp_path):
    assert "REALPIP install ." in _run_wrapper(tmp_path, "chiap04", ["install", "."]).stdout
    assert "REALPIP" in _run_wrapper(tmp_path, "laptop", ["install", "-e", "."]).stdout
    # near-miss hostname must not be treated as a service host
    assert "REALPIP" in _run_wrapper(tmp_path, "chiap040", ["install", "-e", "."]).stdout


def test_wrapper_bypass_variable_only_exact_one(tmp_path):
    ok = _run_wrapper(tmp_path, "chiap04", ["install", "-e", "."], {"SKIP_EDITABLE_GUARD": "1"})
    assert (
        ok.returncode == 0
        and "Skipping editable install guard (SKIP_EDITABLE_GUARD=1)" in ok.stderr
    )
    bad = _run_wrapper(
        tmp_path, "chiap04", ["install", "-e", "."], {"SKIP_EDITABLE_GUARD": "true"}
    )
    assert bad.returncode == 1


def _symlinked_env(tmp_path: Path, host: str = "chiap04"):
    home = tmp_path / "home"
    skbin = home / ".skenv" / "bin"
    skbin.mkdir(parents=True, exist_ok=True)
    (skbin / "python").write_text("#!/bin/sh\n")
    (skbin / "python").chmod(0o755)
    real = tmp_path / "realbin"
    real.mkdir()
    (real / "pip").write_text('#!/bin/sh\necho "REALPIP $*"\n')
    (real / "pip").chmod(0o755)
    local = home / ".local" / "bin"
    local.mkdir(parents=True)
    (local / "pip").symlink_to(WRAPPER)
    env = {
        "PATH": f"{skbin}:{local}:{real}:/usr/bin:/bin",
        "HOME": str(home),
        "HOSTNAME": host,
    }
    return local / "pip", env


def test_wrapper_symlinked_as_pip_does_not_recurse(tmp_path):
    pip, env = _symlinked_env(tmp_path)
    r = subprocess.run(
        [str(pip), "--version"], env=env, capture_output=True, text=True, timeout=10
    )
    assert r.returncode == 0 and "REALPIP --version" in r.stdout


def test_wrapper_symlinked_still_blocks_install_editable(tmp_path):
    pip, env = _symlinked_env(tmp_path)
    r = subprocess.run(
        [str(pip), "install", "-e", "."], env=env, capture_output=True, text=True, timeout=10
    )
    assert r.returncode == 1 and "BLOCKED" in r.stderr


def test_wrapper_does_not_block_read_only_editable_flag(tmp_path):
    pip, env = _symlinked_env(tmp_path)
    r = subprocess.run(
        [str(pip), "list", "-e"], env=env, capture_output=True, text=True, timeout=10
    )
    assert r.returncode == 0 and "REALPIP list -e" in r.stdout
