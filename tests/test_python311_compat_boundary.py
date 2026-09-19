"""Compat-lane wheel boundary must not poison live src/ egg-info."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMPAT = ROOT / "scripts" / "ci" / "run-python311-compat.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _egg_version(repo: Path, name: str = "poisonprobe") -> str | None:
    pkg_info = repo / "src" / f"{name}.egg-info" / "PKG-INFO"
    if not pkg_info.is_file():
        return None
    for line in pkg_info.read_text(encoding="utf-8").splitlines():
        if line.startswith("Version:"):
            return line.removeprefix("Version:").strip()
    return None


def _scm_project(tmp_path: Path) -> Path:
    """Minimal setuptools-scm package used to probe egg-info side effects."""
    repo = tmp_path / "probe"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Compat Boundary")
    _git(repo, "config", "user.email", "compat@example.invalid")
    (repo / "pyproject.toml").write_text(
        textwrap.dedent("""\
            [build-system]
            requires = ["setuptools>=68", "setuptools_scm>=8"]
            build-backend = "setuptools.build_meta"

            [project]
            name = "poisonprobe"
            dynamic = ["version"]

            [tool.setuptools.packages.find]
            where = ["src"]

            [tool.setuptools_scm]
            """),
        encoding="utf-8",
    )
    pkg = repo / "src" / "poisonprobe"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text('"""Probe package."""\n', encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-qm", "probe")
    return repo


def _isolated_compat_wheel(repo: Path, wheel_dir: Path, *, pretend: str) -> None:
    """Mirror scripts/ci/run-python311-compat.sh wheel isolation."""
    stage = wheel_dir / "stage"
    dist = wheel_dir / "dist"
    stage.mkdir(parents=True)
    dist.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    subprocess.run(["tar", "-x", "-C", str(stage)], input=archive, check=True)
    env = os.environ.copy()
    # Reason: archive has no .git; package-specific pretend matches the compat script.
    env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_POISONPROBE"] = pretend
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(dist),
            str(stage),
        ],
        cwd=stage,
        env=env,
        check=True,
    )


def test_compat_script_isolates_pip_wheel_off_live_src() -> None:
    text = COMPAT.read_text(encoding="utf-8")
    assert "git archive HEAD" in text
    assert "SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE" in text
    assert 'from importlib.metadata import version; print(version("skcapstone"))' in text
    assert 'pip wheel --no-deps --no-cache-dir --wheel-dir "$wheel_dir" "$stage"' in text
    assert 'pip wheel --no-deps --no-cache-dir --wheel-dir "$wheel_dir" .' not in text


def test_inplace_pip_wheel_can_write_poisoned_zero_version_egg_info(tmp_path: Path) -> None:
    """Control: in-tree pip wheel is what leaves Version: 0.0.0 under src/."""
    repo = _scm_project(tmp_path)
    dist = tmp_path / "inplace-dist"
    dist.mkdir()
    env = os.environ.copy()
    env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_POISONPROBE"] = "0.0.0"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(dist),
            str(repo),
        ],
        cwd=repo,
        env=env,
        check=True,
    )
    assert _egg_version(repo) == "0.0.0"


def test_compat_wheel_boundary_leaves_no_poisoned_egg_info(tmp_path: Path) -> None:
    """Isolated compat wheel leaves the live tree without Version: 0.0.0 egg-info."""
    repo = _scm_project(tmp_path)
    assert _egg_version(repo) is None

    _isolated_compat_wheel(repo, tmp_path / "isolated", pretend="0.0.0")

    assert _egg_version(repo) is None
    assert not (repo / "src" / "poisonprobe.egg-info").exists()
    assert list((tmp_path / "isolated" / "dist").glob("poisonprobe-0.0.0-*.whl"))
    # Staged build may carry egg-info; that must stay off the live checkout.
    assert (tmp_path / "isolated" / "stage" / "src" / "poisonprobe.egg-info").exists()
    assert _egg_version(tmp_path / "isolated" / "stage", name="poisonprobe") == "0.0.0"
