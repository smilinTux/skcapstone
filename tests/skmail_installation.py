"""Run mail regression tests against a guarded, disposable wheel installation."""

import importlib.util
import os
import runpy
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def skmail_installation(tmp_path_factory):
    """Build offline outside the checkout and install into a new test venv."""
    guard = ROOT / "scripts/ci/test_environment.py"
    runpy.run_path(str(guard))["check_environment"]()
    work = tmp_path_factory.mktemp("skmail-install")
    stage = work / "project"
    shutil.copytree(
        ROOT,
        stage,
        ignore=shutil.ignore_patterns(
            ".git",
            ".venv",
            ".skenv",
            "*.egg-info",
            "__pycache__",
            "build",
            "dist",
            ".pytest_cache",
            ".ruff_cache",
            "node_modules",
        ),
    )
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("ENV", None)
    env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE"] = "0.0.0+skmailtest"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--no-index",
            "--no-cache-dir",
            "--wheel-dir",
            str(work),
            str(stage),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    target = work / "venv"
    venv.EnvBuilder(with_pip=True).create(target)
    python = target / "bin/python"
    env["VIRTUAL_ENV"] = str(target)
    subprocess.run([str(python), str(guard)], env=env, check=True, capture_output=True)
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--no-index",
            str(next(work.glob("*.whl"))),
        ],
        env=env,
        check=True,
        capture_output=True,
    )
    package = Path(
        subprocess.check_output(
            [str(python), "-I", "-c", "import skcapstone.fleet.skmail as m; print(m.__file__)"],
            env=env,
            text=True,
        ).strip()
    ).parent
    assert target in package.parents
    return {"bin": target / "bin", "python": python, "package": package, "work": work}


@pytest.fixture(autouse=True)
def _installed_skmail_tests(request, monkeypatch):
    """Select installed commands and writer bytes for existing mail tests."""
    if not request.path.name.startswith("test_skmail_"):
        return
    install = request.getfixturevalue("skmail_installation")
    monkeypatch.setenv("PATH", f"{install['bin']}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("PYTHONPATH", raising=False)
    if hasattr(request.module, "SCRIPT"):
        monkeypatch.setattr(request.module, "SCRIPT", install["bin"] / "skmail")
    if hasattr(request.module, "MODULE") and "writer" in str(request.module.PATH):
        spec = importlib.util.spec_from_file_location(
            "installed_skmail_writer", install["package"] / "skmail_writer.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(request.module, "MODULE", module)
