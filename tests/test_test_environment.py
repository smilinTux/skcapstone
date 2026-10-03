"""Test installers cannot select a production runtime or inherit shell startup."""

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "scripts/ci/test_environment.py"
check = runpy.run_path(str(CHECK))["check_environment"]


@pytest.mark.parametrize("target", ["prefix", "executable", "VIRTUAL_ENV", "alias", "registered"])
def test_production_environment_refused(tmp_path, monkeypatch, target):
    """Neither interpreter aliases nor an independent activation bypass the guard."""
    monkeypatch.delenv("BASH_ENV", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    production = tmp_path / ".skenv"
    production.mkdir()
    if target == "prefix":
        monkeypatch.setattr(sys, "prefix", str(production))
    elif target == "executable":
        monkeypatch.setattr(sys, "executable", str(production / "bin/python"))
    elif target == "alias":
        alias = tmp_path / "innocent-name"
        alias.symlink_to(production, target_is_directory=True)
        monkeypatch.setenv("VIRTUAL_ENV", str(alias))
    elif target == "registered":
        production = tmp_path / "live-runtime"
        monkeypatch.setenv("SKCAPSTONE_PRODUCTION_VENVS", str(production))
        monkeypatch.setenv("VIRTUAL_ENV", str(production))
    else:
        monkeypatch.setenv("VIRTUAL_ENV", str(production))
    with pytest.raises(RuntimeError, match="production environment"):
        check()


def test_safe_environment_and_shell_hook_refusal(monkeypatch):
    """A disposable interpreter works, but a sourced shell hook is refused."""
    monkeypatch.delenv("BASH_ENV", raising=False)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    check()
    monkeypatch.setenv("BASH_ENV", "/tmp/startup-hook")
    with pytest.raises(RuntimeError, match="BASH_ENV"):
        check()


def test_compat_refuses_production_before_build(tmp_path):
    """Exercise the real script without invoking pip or modifying an environment."""
    env = dict(os.environ, PYTHON_BIN=sys.executable, VIRTUAL_ENV=str(tmp_path / ".skenv"))
    env.pop("BASH_ENV", None)
    result = subprocess.run(
        [str(ROOT / "scripts/ci/run-python311-compat.sh"), "base", "head"],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode != 0
    assert "Refusing test installation in production environment" in result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("script", ["ci/run-python311-compat.sh", "e2e-test.sh", "ci-check.sh"])
def test_entrypoint_does_not_source_bash_env(tmp_path, script):
    """A poison startup file must never execute, even before the guard runs."""
    marker = tmp_path / "sourced"
    hook = tmp_path / "hook"
    hook.write_text(f"touch '{marker}'\n")
    env = dict(
        os.environ,
        BASH_ENV=str(hook),
        PYTHON_BIN=sys.executable,
        VIRTUAL_ENV=str(tmp_path / ".skenv"),
    )
    result = subprocess.run(
        [str(ROOT / "scripts" / script), "base", "head"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not marker.exists()
