from __future__ import annotations

import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

from setuptools import build_meta

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = "skfleet-niobe-live.env.example"
PACKAGE_PATH = f"skcapstone/data/systemd/{TEMPLATE}"


def test_niobe_environment_template_ships_in_wheel_and_sdist(tmp_path: Path) -> None:
    """Built distributions preserve the exact host-neutral template bytes.

    Uses pip wheel + setuptools.build_meta rather than ``python -m build`` so the
    CI Python 3.11 compatibility lane's local ``./build`` directory cannot shadow
    the pypa build frontend (which is also not a declared dependency there).
    """
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-cache-dir",
            "--wheel-dir",
            str(tmp_path),
            str(ROOT),
        ],
        cwd=ROOT,
        check=True,
    )
    previous = Path.cwd()
    try:
        os.chdir(ROOT)
        build_meta.build_sdist(str(tmp_path), config_settings={})
    finally:
        os.chdir(previous)

    expected = (ROOT / "systemd" / TEMPLATE).read_bytes()

    wheel = next(tmp_path.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        assert archive.read(PACKAGE_PATH) == expected

    sdist = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(sdist, "r:gz") as archive:
        member = next(item for item in archive.getmembers() if item.name.endswith(PACKAGE_PATH))
        extracted = archive.extractfile(member)
        assert extracted is not None
        assert extracted.read() == expected
