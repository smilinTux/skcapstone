"""Fleet child processes must never run persistent SKMemory hooks."""

from __future__ import annotations

import ast
from pathlib import Path

from skcapstone.fleet import builder_dispatch, native_codex, production_cursor_sandbox

ROOT = Path(__file__).resolve().parents[2]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _rotate_launch_command() -> list[str]:
    """Load the rotation command builder without executing the dispatcher."""
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_worker_launch_command"
    )
    namespace = {
        "os": __import__("os"),
        "worker_resource_properties": lambda *_args: [],
        "PRODUCTION_POLICY": None,
        "HOST": "chiap08",
    }
    exec(compile(ast.Module(body=[function], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_worker_launch_command"](
        "skfleet-worker-codex-deadbeef.service", "/work", ["pi", "-p", "task"]
    )


def _assert_env_assignment(command: list[str]) -> None:
    """Assert the exact child environment disables SKMemory hooks."""
    assert "SKMEMORY_HOOKS=off" in command


def test_rotation_systemd_child_disables_memory_hooks() -> None:
    """Transient rotation services carry the isolation into the real child."""
    assert "--setenv=SKMEMORY_HOOKS=off" in _rotate_launch_command()


def test_pi_builder_clean_environment_disables_memory_hooks(monkeypatch) -> None:
    """The env-clearing Pi builder explicitly restores the isolation switch."""
    monkeypatch.setattr(builder_dispatch.shutil, "which", lambda _name: "/usr/bin/pi")
    monkeypatch.setattr(builder_dispatch, "_guard_path", lambda: "/guard.mjs")
    request = {
        "card_id": "deadbeef",
        "logical_route": "sk-m",
        "operator_role": "",
    }
    command = builder_dispatch.worker_command(request, "pi-builder", "revision", Path("/work"))
    _assert_env_assignment(command)


def test_native_codex_clearenv_disables_memory_hooks(tmp_path: Path) -> None:
    """The native Codex bwrap environment restores only the disabled value."""
    workspace = tmp_path / "work"
    runtime = tmp_path / "runtime"
    executable = runtime / "bin" / "codex"
    auth = tmp_path / "auth.json"
    workspace.mkdir(mode=0o700)
    executable.parent.mkdir(parents=True)
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    command = native_codex.sandbox_command(
        workspace, executable, auth, model="gpt-6.1-sol", owner="retained-owner"
    )
    index = command.index("SKMEMORY_HOOKS")
    assert command[index - 1 : index + 2] == ["--setenv", "SKMEMORY_HOOKS", "off"]


def test_cursor_clearenv_disables_memory_hooks(tmp_path: Path) -> None:
    """The Cursor sandbox carries the same disabled hook value after clearenv."""
    workspace = tmp_path / "work"
    runtime = tmp_path / "runtime"
    executable = runtime / "cursor-agent"
    auth = tmp_path / "auth.json"
    config = tmp_path / "config"
    workspace.mkdir(mode=0o700)
    (workspace / ".git").mkdir()
    runtime.mkdir()
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o700)
    (runtime / "node").write_text("", encoding="utf-8")
    (runtime / "index.js").write_text("", encoding="utf-8")
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    config.mkdir(mode=0o700)
    (config / "empty").mkdir()
    command = production_cursor_sandbox.sandbox_command(
        workspace,
        executable,
        auth,
        config,
        model="composer-2.5",
        owner="pi-cursor-builder-chiap08-deadbeef",
        card="deadbeef",
        claim="revision",
        prompt="task",
    )
    index = command.index("SKMEMORY_HOOKS")
    assert command[index - 1 : index + 2] == ["--setenv", "SKMEMORY_HOOKS", "off"]
