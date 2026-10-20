"""Regression tests for the source checkout version gate."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "check-version-consistency.sh"


def run_gate(tmp_path, version, package_version=None, pyproject=None, scm_source=None):
    (tmp_path / "pyproject.toml").write_text(
        pyproject if pyproject is not None else '[project]\ndynamic = ["version"]\n'
    )
    fake = tmp_path / "setuptools_scm.py"
    fake.write_text(
        scm_source
        if scm_source is not None
        else f"def get_version(root='.'): return {version!r}\n"
    )
    if package_version is not None:
        (tmp_path / "package.json").write_text(json.dumps({"version": package_version}))
    env = os.environ.copy()
    env["PYTHONPATH"] = str(tmp_path)
    env["PYTHON"] = sys.executable
    return subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_dynamic_version_passes_without_package_manifest(tmp_path):
    result = run_gate(tmp_path, "1.2.3.dev4+gabc")
    assert result.returncode == 0, result.stderr
    assert "no package.json" in result.stdout


def test_genuine_manifest_mismatch_fails(tmp_path):
    result = run_gate(tmp_path, "1.2.3", "9.9.9")
    assert result.returncode != 0
    assert "Version mismatch" in result.stderr


def test_static_version_without_dynamic_declaration_fails(tmp_path):
    result = run_gate(tmp_path, "1.2.3", pyproject='[project]\nversion = "1.2.3"\n')
    assert result.returncode != 0
    assert "dynamic version" in result.stderr


def test_unresolvable_scm_version_fails(tmp_path):
    result = run_gate(
        tmp_path,
        "",
        scm_source="def get_version(root='.'):\n    raise LookupError('no git metadata')\n",
    )
    assert result.returncode != 0
    assert "unable to resolve setuptools-scm version" in result.stderr


def test_empty_scm_version_fails(tmp_path):
    result = run_gate(tmp_path, "")
    assert result.returncode != 0
    assert "empty version" in result.stderr


def test_matching_manifest_passes(tmp_path):
    result = run_gate(tmp_path, "1.2.3", "1.2.3")
    assert result.returncode == 0, result.stderr
    assert "== package.json" in result.stdout


def test_real_repo_resolves_a_version():
    """Exercise the real setuptools-scm against this checkout, not a fake."""
    pytest.importorskip("setuptools_scm")
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=ROOT,
        env={**os.environ, "PYTHON": sys.executable},
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "setuptools-scm generated version" in result.stdout
