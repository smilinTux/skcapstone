"""Qualified toolchain installation and fail-closed observer regressions."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomllib
from packaging.requirements import Requirement

from skcapstone.fleet import production_test_plan as plan
from skcapstone.fleet import rollout_drift
from skcapstone.fleet.cli import _drift_is_role_ambiguous
from skcapstone.fleet.staged_rollout import _DEPLOY_STEPS, _ROLLBACK_STEPS

pytestmark = pytest.mark.usefixtures("sandbox_prerequisites")


def test_packaging_extra_covers_fingerprinted_tools_and_both_install_directions():
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    names = {
        Requirement(item).name
        for item in config["project"]["optional-dependencies"]["fleet-qualify"]
    }
    assert {"pytest", "pytest-asyncio", "ruff", "pluggy", "iniconfig", "packaging"} <= names
    assert set(plan.TOOL_PACKAGES) == {
        "pytest",
        "_pytest",
        "pytest_asyncio",
        "ruff",
        "pluggy",
        "iniconfig",
        "packaging",
    }
    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        command = dict(steps)["pip_install"]
        assert "pip install '.[fleet-qualify]'" in command


def test_missing_tools_probe_uses_the_target_prefix(tmp_path):
    from skcapstone.fleet.qualified_runtime import missing_dependencies

    site = (
        tmp_path
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site.mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin/ruff").write_text("synthetic executable")
    from skcapstone.fleet.qualified_runtime import REQUIRED_PACKAGES

    for name in REQUIRED_PACKAGES:
        (site / name).mkdir()
    (site / "skcapstone/__init__.py").write_text("# Packaged fixture.\n")
    assert missing_dependencies(tmp_path) == []
    (site / "pytest_asyncio").rmdir()
    assert missing_dependencies(tmp_path) == ["pytest_asyncio"]
    (tmp_path / "bin/ruff").unlink()
    assert missing_dependencies(tmp_path) == ["pytest_asyncio", "ruff executable"]


@pytest.mark.parametrize("module", ["pytest_asyncio", "skharness"])
def test_production_drift_and_rollout_gate_cannot_hide_missing_tools(
    tmp_path, monkeypatch, module
):
    home = tmp_path / "home"
    policy = home / ".skcapstone/fleet/production.json"
    policy.parent.mkdir(parents=True)
    policy.write_text("{}")
    observer = SimpleNamespace(
        unit_in_scope=lambda _: (False, None, "fixture"),
        _systemctl_show_value=lambda *args: ("inactive", None),
    )
    monkeypatch.setattr(rollout_drift, "_load_readiness_module", lambda _: observer)
    monkeypatch.setattr(rollout_drift, "_script_files", lambda _: [])
    drifts = rollout_drift.detect_drift({"units": []}, home, tmp_path)
    missing = [d for d in drifts if d.artifact == "qualified-runtime:" + module]
    assert len(missing) == 1 and missing[0].kind == "missing"
    assert not _drift_is_role_ambiguous(missing[0])


@pytest.mark.parametrize("missing", [True, False])
@pytest.mark.parametrize("module", ["pytest_asyncio", "skharness"])
def test_production_readiness_reports_the_actual_interpreter_missing_plugin(
    tmp_path, monkeypatch, capsys, missing, module
):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/fleet"))
    import skfleet_readiness as readiness

    source = tmp_path / "dispatcher.py"
    source.write_text("SKFLEET_PRODUCTION_POLICY_V1 = True\n")
    units = tmp_path / "units"
    units.mkdir()
    (units / "fixture.service").write_text("ExecStart=/usr/bin/python3 -m json\n")
    monkeypatch.setenv("SKFLEET_PRODUCTION_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setenv("SKFLEET_AUTHORITY_HOST", "fixture")
    monkeypatch.setattr(readiness, "production_environment_error", lambda *args: None)
    monkeypatch.setattr(readiness, "qualification_sandbox_error", lambda *args: None)
    calls = []

    def imports(modules, interpreter):
        calls.append((modules, interpreter))
        return {name: not missing or name != module for name in modules}

    monkeypatch.setattr(readiness, "check_module_imports", imports)
    assert readiness._run(source, units, "/fixture/python", None) == int(missing)
    assert ("FAIL qualified-runtime dependency: " + module in capsys.readouterr().out) == missing
    assert any(
        module in modules and interpreter == "/fixture/python" for modules, interpreter in calls
    )


def test_controller_extra_supports_python_311_and_matches_312_source_ci():
    """The declared controller is resolvable on both fleet Python floors."""
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    items = [Requirement(x) for x in config["project"]["optional-dependencies"]["fleet-qualify"]]
    for version, pin in [("3.11", "2.19.5"), ("3.12", "2.21.3")]:
        matches = [
            r
            for r in items
            if r.name == "ansible-core"
            and (r.marker is None or r.marker.evaluate({"python_version": version}))
        ]
        assert len(matches) == 1 and str(matches[0].specifier) == "==" + pin


def test_sealed_runtime_requires_controller_and_prefix_contained_core(tmp_path):
    """Editable imports outside the sealed prefix cannot satisfy readiness."""
    from skcapstone.fleet.qualified_runtime import missing_dependencies

    missing = missing_dependencies(tmp_path)
    assert "ansible" in missing
    assert "skcapstone" in missing
    site = (
        tmp_path
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    (site / "skcapstone").mkdir(parents=True)
    assert "skcapstone" in missing_dependencies(tmp_path)
    (site / "skcapstone/__init__.py").write_text("# Packaged fixture.\n")
    assert "skcapstone" not in missing_dependencies(tmp_path)


def test_checkout_symlink_cannot_satisfy_sealed_core_requirement(tmp_path):
    """A prefix entry pointing outside the read-only prefix is still unavailable."""
    from skcapstone.fleet.qualified_runtime import missing_dependencies

    site = (
        tmp_path
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site.mkdir(parents=True)
    checkout = tmp_path.parent / ("outside-" + tmp_path.name) / "skcapstone"
    checkout.mkdir(parents=True)
    (checkout / "__init__.py").write_text("# Outside the sandbox runtime.\n")
    (site / "skcapstone").symlink_to(checkout, target_is_directory=True)
    assert "skcapstone" in missing_dependencies(tmp_path)


def test_dashboard_dependency_is_pinned_in_the_governed_install_extra():
    """Rollout and rollback can install the dashboard's private sibling."""
    root = Path(__file__).resolve().parents[2]
    config = tomllib.loads((root / "pyproject.toml").read_text())
    requirements = [
        Requirement(item) for item in config["project"]["optional-dependencies"]["fleet-qualify"]
    ]
    matches = [item for item in requirements if item.name == "skharness"]
    assert len(matches) == 1
    assert matches[0].url == (
        "git+https://github.com/smilinTux/skharness.git@"
        "7409a1ab28f9c8fed87cd40226f4c031ca9e3f6e"
    )


@pytest.mark.parametrize("placement", ["absent", "editable", "packaged"])
def test_dashboard_dependency_must_be_available_inside_the_sealed_prefix(tmp_path, placement):
    """Host-only editable imports cannot qualify the dashboard economy tests."""
    from skcapstone.fleet.qualified_runtime import missing_dependencies

    site = (
        tmp_path
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages"
    )
    site.mkdir(parents=True)
    if placement == "editable":
        outside = tmp_path.parent / ("dashboard-sibling-" + tmp_path.name)
        outside.mkdir()
        (outside / "__init__.py").write_text("# Outside the sealed runtime.\n")
        (site / "skharness").symlink_to(outside, target_is_directory=True)
    elif placement == "packaged":
        (site / "skharness").mkdir()
        (site / "skharness/__init__.py").write_text("# Packaged sibling.\n")
    assert ("skharness" in missing_dependencies(tmp_path)) == (placement != "packaged")
