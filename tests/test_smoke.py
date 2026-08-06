"""Light, self-contained smoke tests for skdashboard.

Intentionally does NOT import skdashboard.dashboard (which reaches into the heavy
skcapstone runtime); CI installs skdashboard --no-deps. This guards that the
package imports, is versioned, and ships its modules + static assets.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import skdashboard


def test_version():
    assert skdashboard.__version__


def test_static_assets_present():
    static = Path(skdashboard.__file__).parent / "static"
    assert static.is_dir()
    # the key pages the :7778 routes serve
    for page in ("overview.html", "board.html", "models.html", "cmdb.html"):
        assert (static / page).is_file(), f"missing static/{page}"


def test_dashboard_modules_present():
    pkg_dir = Path(skdashboard.__file__).parent
    for mod in (
        "dashboard",
        "dashboard_kanban",
        "dashboard_itil",
        "dashboard_cmdb",
        "dashboard_assistant",
        "dashboard_overview",
        "skdashboard_manifest",
    ):
        assert (pkg_dir / f"{mod}.py").is_file(), f"missing {mod}.py"
        # module is at least importable as a spec (syntax valid) without executing it
        spec = importlib.util.spec_from_file_location(mod, pkg_dir / f"{mod}.py")
        assert spec is not None
