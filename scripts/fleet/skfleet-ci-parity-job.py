#!/usr/bin/env python3
"""Run one SKCapstone GitHub job in an isolated workflow-matching environment."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import tempfile
from pathlib import Path


def run(command: list[str], *, cwd: Path, env: dict[str, str] | None = None) -> None:
    """Run without exposing dependency or test output through the receipt process."""
    subprocess.run(command, cwd=cwd, env=env, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job", choices=("unit", "provider-cloud", "provider-docker", "build"))
    parser.add_argument("python", choices=("3.11", "3.12"))
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="skcapstone-ci-parity-") as directory:
        root = Path(directory)
        venv = root / "venv"
        run(["uv", "venv", "--python", args.python, str(venv)], cwd=repo)
        python = venv / "bin/python"
        env = {**os.environ, "PATH": f"{venv / 'bin'}{os.pathsep}{os.environ['PATH']}"}
        pi = shutil.which("pi", path=env["PATH"])
        if pi:
            pi_directory = str(Path(pi).parent)
            env["PATH"] = os.pathsep.join(
                item for item in env["PATH"].split(os.pathsep) if item != pi_directory
            )
        pip = ["uv", "pip", "install", "--python", str(python)]
        if args.job == "unit":
            run([*pip, "-e", ".[all,dev]"], cwd=repo)
            run([*pip, "--force-reinstall", "--no-deps", "skcoord==0.1.57"], cwd=repo)
            run(
                [
                    str(python),
                    "-m",
                    "pytest",
                    "-q",
                    "tests/test_cli_kanban.py",
                    "tests/test_coord_kanban_mcp.py",
                    "tests/test_coord_amend.py",
                ],
                cwd=repo,
                env=env,
            )
            run(
                [
                    *pip,
                    "--force-reinstall",
                    "--no-deps",
                    "skcoord@git+https://github.com/smilinTux/skcoord",
                    "skdashboard@git+https://github.com/smilinTux/skdashboard",
                    "skharness@git+https://github.com/smilinTux/skharness",
                ],
                cwd=repo,
            )
            run(
                [
                    str(python),
                    "-m",
                    "pytest",
                    "tests/",
                    "--strict-markers",
                    "-m",
                    "not integration and not e2e",
                    "--cov=skcapstone",
                    "--cov-report=term",
                ],
                cwd=repo,
                env=env,
            )
        elif args.job.startswith("provider-"):
            extra = args.job.removeprefix("provider-")
            run([*pip, "-e", f".[{extra},dev]"], cwd=repo)
            tests = ["tests/test_provider_smoke.py"]
            tests.insert(
                0,
                (
                    "tests/test_cloud_providers.py"
                    if extra == "cloud"
                    else "tests/test_docker_provider.py"
                ),
            )
            if extra == "cloud":
                tests.insert(1, "tests/test_cloud_provider.py")
            run(
                [
                    str(python),
                    "-m",
                    "pytest",
                    *tests,
                    "--strict-markers",
                    "-m",
                    "not integration and not e2e",
                ],
                cwd=repo,
                env=env,
            )
        else:
            run([*pip, "build", "twine"], cwd=repo)
            output = root / "dist"
            run([str(python), "-m", "build", "--outdir", str(output)], cwd=repo, env=env)
            run([str(venv / "bin/twine"), "check", *map(str, output.iterdir())], cwd=repo, env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
