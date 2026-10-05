"""Qualification must import the pinned source, never a host editable checkout."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import production_test_worker as worker
from skcapstone.fleet.production_test_plan import TestEvidenceError


def pythonpath(argv):
    at = argv.index("PYTHONPATH")
    assert argv[at - 1] == "--setenv"
    return argv[at + 1]


def test_simple_src_repository_keeps_existing_sandbox_contract(tmp_path):
    source = tmp_path / "source"
    (source / "src").mkdir(parents=True)
    argv = worker.sandbox_command(source, tmp_path / "output", ["python", "-V"])
    assert pythonpath(argv) == "/work/src:/qualified"
    assert "--clearenv" in argv and "--unshare-all" in argv
    assert argv[argv.index(str(source)) - 1] == "--ro-bind"


def test_monorepo_imports_only_candidate_source_roots(tmp_path, monkeypatch):
    source = tmp_path / "source"
    roots = [
        "packages/domain/src",
        "packages/connectors/hammertime/src",
        "services/api/src",
        "vendor/capauth/src",
    ]
    for relative in roots:
        (source / relative).mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/old-host-checkout/services/api/src")
    argv = worker.sandbox_command(source, tmp_path / "output", ["python", "-V"])
    assert set(pythonpath(argv).split(":")) == {
        "/work/src",
        "/qualified",
        *("/work/" + p for p in roots),
    }
    assert not any("old-host-checkout" in word for word in argv)


@pytest.mark.parametrize(
    "relative", ["packages/domain/src", "services/api/src", "vendor/capauth/src"]
)
def test_redirected_candidate_import_roots_refuse(tmp_path, relative):
    source = tmp_path / "source"
    path = source / relative
    path.parent.mkdir(parents=True)
    outside = tmp_path / "older-source"
    outside.mkdir()
    path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(TestEvidenceError, match="candidate Python source root is redirected"):
        worker.sandbox_command(source, tmp_path / "output", ["python", "-V"])


def test_candidate_import_roots_are_bounded(tmp_path):
    source = tmp_path / "source"
    for n in range(65):
        (source / "packages" / f"package{n}" / "src").mkdir(parents=True)
    with pytest.raises(TestEvidenceError, match="candidate Python source roots exceed bound"):
        worker.sandbox_command(source, tmp_path / "output", ["python", "-V"])


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_sandbox_executes_pinned_module_and_excludes_old_host_source(tmp_path, monkeypatch):
    source, output, older = (tmp_path / name for name in ("source", "output", "older"))
    pinned = source / "packages/domain/src"
    pinned.mkdir(parents=True)
    output.mkdir()
    older.mkdir()
    (pinned / "qualification_probe.py").write_text("VALUE='pinned264'\n")
    (older / "qualification_probe.py").write_text("VALUE='wrong-old-worktree'\n")
    monkeypatch.setenv("PYTHONPATH", str(older))
    monkeypatch.setattr(worker, "PREFIX", Path("/usr"))
    command = worker.sandbox_command(
        source,
        output,
        [
            "/usr/bin/python3",
            "-c",
            "import json,qualification_probe; "
            "print(json.dumps({'value':qualification_probe.VALUE,"
            "'file':qualification_probe.__file__}))",
        ],
    )
    result = subprocess.run(
        command, capture_output=True, text=True, timeout=10, env=dict(os.environ)
    )
    if result.returncode and (
        "Operation not permitted" in result.stderr
        or "Creating new namespace failed" in result.stderr
    ):
        pytest.skip("host does not permit bubblewrap namespaces")
    assert result.returncode == 0, result.stderr
    actual = json.loads(result.stdout)
    assert actual == dict(
        value="pinned264", file="/work/packages/domain/src/qualification_probe.py"
    )


@pytest.mark.parametrize("name", ["import:injection", "source with spaces"])
def test_candidate_paths_cannot_inject_pythonpath_entries(tmp_path, name):
    source = tmp_path / "source"
    (source / "packages" / name / "src").mkdir(parents=True)
    with pytest.raises(TestEvidenceError, match="candidate Python source root is unsafe"):
        worker.sandbox_command(source, tmp_path / "output", ["python", "-V"])


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bubblewrap unavailable")
def test_strict_asyncio_configuration_runs_in_sealed_sandbox(tmp_path, monkeypatch):
    # Mount the test runner independently of its host location; /tmp is sealed
    # over by the sandbox, and local test virtualenvs can live beneath it.
    monkeypatch.setattr(worker, "PREFIX", Path("/test-runtime"))
    source, output = tmp_path / "source", tmp_path / "output"
    source.mkdir()
    output.mkdir()
    (source / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\naddopts="--strict-config --strict-markers"\n'
        'asyncio_mode="strict"\nasyncio_default_fixture_loop_scope="function"\n'
    )
    (source / "test_async.py").write_text(
        "import asyncio,pytest\n@pytest.mark.asyncio\n"
        "async def test_native_async():\n    await asyncio.sleep(0)\n"
    )
    monkeypatch.setenv("PYTEST_PLUGINS", "untrusted_host_plugin")
    command = worker.sandbox_command(
        source, output, [str(worker.PREFIX / "bin/python"), "-m", "pytest", "-q"]
    )
    command[command.index("/test-runtime")] = sys.prefix
    # setup-python uses a shared libpython outside /usr; bind its qualified
    # library directory explicitly inside this relocated test runtime.
    boundary = command.index("--")
    command[boundary:boundary] = ["--setenv", "LD_LIBRARY_PATH", "/test-runtime/lib"]
    result = subprocess.run(command, capture_output=True, text=True, timeout=20)
    if result.returncode and (
        "Operation not permitted" in result.stderr
        or "Creating new namespace failed" in result.stderr
    ):
        pytest.skip("host does not permit bubblewrap namespaces")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert command[command.index("PYTEST_DISABLE_PLUGIN_AUTOLOAD") + 1] == "1"
    assert command[command.index("PYTEST_PLUGINS") + 1] == "pytest_asyncio.plugin"
    assert "untrusted_host_plugin" not in command


@pytest.mark.parametrize(
    "relative", ["pytest_asyncio/plugin.py", "pytest_asyncio-1.3.dist-info/METADATA"]
)
def test_runtime_fingerprint_binds_explicit_asyncio_plugin(qualified_runtime, relative):
    site = (
        qualified_runtime
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    path = site / relative
    path.parent.mkdir(exist_ok=True)
    path.write_text("qualified asyncio bytes\n")
    before = plan.runtime_fingerprint()
    path.write_text("changed asyncio bytes\n")
    assert plan.runtime_fingerprint() != before
