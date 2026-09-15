from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from importlib.metadata import version as pkg_version
from pathlib import Path

from setuptools import build_meta

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "skfleet-niobe-live.env.example"
PACKAGE_PATH = f"skcapstone/data/systemd/{TEMPLATE}"

# Keep the packaging build out of the live checkout. In-tree build_sdist writes
# src/skcapstone.egg-info; pytest sets pythonpath=["src"], so Version: 0.0.0
# there shadows installed metadata for later tests (PR731 / 890c731d failure).
_IGNORE = shutil.ignore_patterns(
    ".git",
    ".github",
    ".venv",
    ".skenv",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    "__pycache__",
    "*.egg-info",
    "build",
    "dist",
    "htmlcov",
    "evidence",
    "node_modules",
)


def test_niobe_environment_template_ships_in_wheel_and_sdist(tmp_path: Path) -> None:
    """Built distributions preserve the exact host-neutral template bytes.

    Builds in an isolated staging tree with a pretended SCM version so the live
    checkout's ``src/`` is never given a ``0.0.0`` egg-info that pytest would
    publish on ``sys.path``.
    """
    staging = tmp_path / "project"
    outdir = tmp_path / "dist"
    outdir.mkdir()
    shutil.copytree(ROOT, staging, ignore=_IGNORE, symlinks=True)

    pretend = pkg_version("skcapstone")
    env = os.environ.copy()
    env["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE"] = pretend

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(outdir),
            str(staging),
        ],
        cwd=staging,
        env=env,
        check=True,
    )

    previous = Path.cwd()
    previous_pretend = os.environ.get("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE")
    os.environ["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE"] = pretend
    try:
        os.chdir(staging)
        build_meta.build_sdist(str(outdir), config_settings={})
    finally:
        os.chdir(previous)
        if previous_pretend is None:
            os.environ.pop("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE", None)
        else:
            os.environ["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SKCAPSTONE"] = previous_pretend

    expected = (ROOT / "systemd" / TEMPLATE).read_bytes()

    wheel = next(outdir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read(PACKAGE_PATH) == expected

    sdist = next(outdir.glob("*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        member = next(item for item in archive.getmembers() if item.name.endswith(PACKAGE_PATH))
        extracted = archive.extractfile(member)
        assert extracted is not None
        assert extracted.read() == expected

    assert not (ROOT / "src" / "skcapstone.egg-info").exists()
