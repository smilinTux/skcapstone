"""Regression coverage for the fleet service console-script path."""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROTATE = ROOT / "scripts" / "fleet" / "skfleet-rotate.py"


def _load_helper():
    tree = ast.parse(ROTATE.read_text(encoding="utf-8"))
    node = next(
        item
        for item in tree.body
        if isinstance(item, ast.FunctionDef) and item.name == "_ensure_runtime_console_path"
    )
    namespace = {"os": os, "sys": sys}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ROTATE), "exec"), namespace)
    return namespace["_ensure_runtime_console_path"]


def test_runtime_python_bin_resolves_skcapstone_without_service_path(tmp_path: Path) -> None:
    runtime_bin = tmp_path / "venv" / "bin"
    runtime_bin.mkdir(parents=True)
    python = runtime_bin / "python3"
    skcapstone = runtime_bin / "skcapstone"
    skcapstone.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    skcapstone.chmod(0o755)
    environ = {"PATH": "/usr/bin:/bin"}

    resolved = _load_helper()(str(python), environ)

    assert resolved == str(skcapstone)
    assert environ["PATH"].split(os.pathsep)[0] == str(runtime_bin)
    assert subprocess.run(["skcapstone"], env=environ, check=False).returncode == 0


def test_runtime_python_bin_is_not_duplicated() -> None:
    environ = {"PATH": "/opt/venv/bin:/usr/bin"}

    _load_helper()("/opt/venv/bin/python3", environ)

    assert environ["PATH"] == "/opt/venv/bin:/usr/bin"
