"""The qualify prefix layout follows the pinned interpreter, not the caller.

ziowk01 runs ~/.skenv on Python 3.14 while the qualify prefix is built with the
pinned /usr/bin/python3.12. Every site-packages path was derived from
``sys.version_info``, so verify() looked under lib/python3.14 and reported
"site-packages is missing" on a correct prefix (2026-11-09).
"""

from __future__ import annotations

import json
import sys

import pytest

from skcapstone.fleet import qualified_runtime, qualify_env

PINNED = json.loads(qualify_env.MANIFEST.read_bytes())["python"]["version"]
PINNED_MINOR = ".".join(PINNED.split(".")[:2])
OTHER_MINOR = f"{sys.version_info.major}.{sys.version_info.minor + 2}"


@pytest.fixture
def other_python(tmp_path, monkeypatch):
    """Pin a Python other than the one running the tests (as on ziowk01)."""
    manifest = json.loads(qualify_env.MANIFEST.read_bytes())
    manifest["python"]["version"] = f"{OTHER_MINOR}.0"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(qualified_runtime, "_MANIFEST", path)
    return OTHER_MINOR


def test_pinned_minor_reads_the_manifest():
    assert qualified_runtime.pinned_python_minor() == PINNED_MINOR


def test_site_path_ignores_the_caller_interpreter(tmp_path, other_python):
    site = qualified_runtime.prefix_site(tmp_path)
    assert site == tmp_path / "lib" / f"python{other_python}" / "site-packages"
    assert qualify_env.site_packages(tmp_path) == site.parent


def test_missing_dependencies_finds_packages_under_a_different_caller(tmp_path, other_python):
    site = qualified_runtime.prefix_site(tmp_path)
    for name in qualified_runtime.REQUIRED_PACKAGES:
        (site / name).mkdir(parents=True)
    (site / "skcapstone/__init__.py").write_text("")
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/ruff").write_bytes(b"ruff")
    assert qualified_runtime.missing_dependencies(tmp_path) == []


def test_empty_prefix_still_reports_site_packages_missing(tmp_path, other_python):
    findings = qualify_env.verify(tmp_path, qualify_env.load_manifest())
    assert "site-packages is missing" in findings
