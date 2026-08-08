"""Single-source-of-truth guarantees for the package version.

The version is declared exactly once, by the **git tag**. ``pyproject.toml``
marks the version ``dynamic`` and setuptools-scm derives it from that tag, so a
release cannot carry a version no tag corresponds to. Everything else
(``skcapstone.__version__``, the CLI ``--version`` string, the ``version``
command) derives from that one source via installed package metadata, so the
values can never drift apart.

Regression: previously ``__version__`` was a second hardcoded literal in
``src/skcapstone/__init__.py`` (``0.13.0``) that had already drifted from the
``pyproject.toml`` declaration (``0.15.0``).
"""

from __future__ import annotations

import re
from importlib.metadata import version as _pkg_version
from pathlib import Path

import pytest
from click.testing import CliRunner

import skcapstone
from skcapstone.cli import main

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - Python 3.10
    tomllib = None


def _pyproject_path() -> Path:
    return Path(skcapstone.__file__).resolve().parents[2] / "pyproject.toml"


def test_version_matches_installed_metadata():
    """``__version__`` traces to installed package metadata, not a literal."""
    assert skcapstone.__version__ == _pkg_version("skcapstone")


@pytest.mark.skipif(tomllib is None, reason="tomllib requires Python 3.11+")
def test_pyproject_declares_the_version_dynamic():
    """pyproject must NOT carry a static version that could drift from the tag.

    The old contract was a literal ``[project].version``. That is exactly what
    made publish-on-main impossible: the tag job cuts vX.Y.Z+1 while the build
    kept rebuilding the pinned string, which PyPI rejects as already existing.
    """
    pyproject = _pyproject_path()
    if not pyproject.is_file():
        pytest.skip("pyproject.toml not present in installed layout")
    with pyproject.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    assert "version" not in project, "a static version reintroduces tag/build drift"
    assert "version" in project.get("dynamic", []), "version must be dynamic (setuptools-scm)"


def test_cli_version_matches_dunder():
    """CLI ``--version`` reports exactly ``__version__`` (no independent literal)."""
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0
    assert skcapstone.__version__ in result.output
    # And it is a real semver-ish string, not the unknown fallback sentinel.
    assert result.output.strip().endswith(skcapstone.__version__)
    assert "0.0.0+unknown" not in result.output


def test_version_command_matches_dunder():
    """The ``version`` subcommand reports the same single-sourced value."""
    result = CliRunner().invoke(main, ["version"])
    assert result.exit_code == 0
    assert skcapstone.__version__ in result.output


def test_no_hardcoded_version_literal_in_init():
    """__init__ must not reassign __version__ to a bare string literal."""
    init_src = Path(skcapstone.__file__).read_text()
    # A literal assignment like ``__version__ = "1.2.3"`` reintroduces drift.
    assert not re.search(r'__version__\s*=\s*["\']', init_src), (
        "__version__ must derive from metadata/pyproject, not a string literal"
    )


def test_resolve_version_fallback_uses_the_git_tag(monkeypatch):
    """With no installed metadata, resolution falls back to setuptools-scm.

    Still single-source: setuptools-scm reads the same git tag the build does,
    so the fallback cannot disagree with a real release.
    """
    import importlib.metadata as md
    from importlib.metadata import PackageNotFoundError

    def _not_found(_name):
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(md, "version", _not_found)

    pytest.importorskip("setuptools_scm", reason="scm fallback needs setuptools-scm")
    resolved = skcapstone._resolve_version()
    assert resolved and resolved != "0.0.0+unknown", (
        "fallback must derive a real version from the git tag"
    )
    assert re.match(r"^\d+\.\d+", resolved), f"not a version-looking string: {resolved}"
