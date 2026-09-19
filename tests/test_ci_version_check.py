"""Regression tests for the source checkout version gate."""
import json
import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check-version-consistency.sh"


def run_gate(tmp_path, version, package_version=None):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndynamic = ["version"]\n'
    )
    fake = tmp_path / "setuptools_scm.py"
    fake.write_text(f"def get_version(root='.'): return {version!r}\n")
    if package_version is not None:
        (tmp_path / "package.json").write_text(
            json.dumps({"version": package_version})
        )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    return subprocess.run(
        ["bash", str(SCRIPT)], cwd=tmp_path, env=env,
        text=True, capture_output=True, check=False,
    )


def test_dynamic_version_passes_without_package_manifest(tmp_path):
    result = run_gate(tmp_path, "1.2.3.dev4+gabc")
    assert result.returncode == 0, result.stderr
    assert "no package.json" in result.stdout


def test_genuine_manifest_mismatch_fails(tmp_path):
    result = run_gate(tmp_path, "1.2.3", "9.9.9")
    assert result.returncode != 0
    assert "Version mismatch" in result.stderr
