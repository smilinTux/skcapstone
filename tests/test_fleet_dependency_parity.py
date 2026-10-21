"""CI must test what production runs; rollout must refuse dependency drift."""

from __future__ import annotations

import ast
import importlib
import sys
from pathlib import Path

import pytest
import tomllib
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from skcapstone.fleet import dependency_parity as dp
from skcapstone.fleet.staged_rollout import _DEPLOY_STEPS, _ROLLBACK_STEPS

ROOT = Path(__file__).resolve().parents[1]
CONSTRAINTS = ROOT / "deploy" / "fleet-constraints.txt"
# skskills is intentionally unpinned: production runs a git build the CI index lacks.
SK_RUNTIME = ("skcoord", "skmemory", "cloud9")


def _pins():
    return dp.load_constraints(CONSTRAINTS)


def _project_requirements():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    return [Requirement(r) for r in config["project"]["dependencies"]]


def test_constraints_are_exact_pins_for_every_sk_runtime_dependency():
    pins = _pins()
    for name in SK_RUNTIME:
        assert canonicalize_name(name) in pins, f"{name} missing from fleet-constraints.txt"


def test_pyproject_floor_never_exceeds_production_pin():
    """A floor above production would break hosts while CI (using the pin) passes."""
    pins = _pins()
    for req in _project_requirements():
        pin = pins.get(canonicalize_name(req.name))
        if pin is not None:
            assert req.specifier.contains(pin, prereleases=True), (
                f"pyproject requires {req} but production pins {pin}; "
                "release upstream and bump deploy/fleet-constraints.txt in the same PR"
            )


def test_every_pyproject_sk_dependency_is_constrained():
    names = {canonicalize_name(r.name) for r in _project_requirements()}
    assert {canonicalize_name(n) for n in SK_RUNTIME} <= names
    assert set(_pins()) <= names, "constraints pin a package pyproject does not depend on"


def test_ci_installs_with_the_constraints_file():
    # pytest.yml provisions its environment through the shared test-env action.
    for path in (
        ".github/actions/test-env/action.yml",
        ".github/workflows/providers.yml",
    ):
        text = (ROOT / path).read_text()
        assert "-c deploy/fleet-constraints.txt" in text, path
    for path in (".github/workflows/pytest.yml", ".github/actions/test-env/action.yml"):
        assert (
            "skcoord @ git+" not in (ROOT / path).read_text()
        ), "CI must not overlay an unreleased skcoord; test the version production runs"


def _skcoord_imports():
    found = set()
    for base in (ROOT / "src", ROOT / "scripts" / "fleet"):
        for path in base.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module
                    and (node.module == "skcoord" or node.module.startswith("skcoord."))
                ):
                    for alias in node.names:
                        found.add((node.module, alias.name, str(path.relative_to(ROOT))))
    return sorted(found)


def test_code_only_imports_skcoord_symbols_the_pinned_version_provides():
    installed = dp.installed_version("skcoord")
    pin = _pins()[canonicalize_name("skcoord")]
    if installed != pin:
        pytest.skip(f"skcoord {installed} installed, constraints pin {pin}")
    missing = []
    for module, name, where in _skcoord_imports():
        try:
            mod = importlib.import_module(module)
        except ImportError:
            missing.append(f"{where}: module {module} not in skcoord {pin}")
            continue
        if name != "*" and not hasattr(mod, name):
            try:
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                missing.append(f"{where}: {module}.{name} not in skcoord {pin}")
    assert not missing, "\n".join(missing)


def test_parse_rejects_ranges_and_reports_drift(monkeypatch):
    with pytest.raises(ValueError):
        dp.parse_constraints("skcoord>=0.1.86\n")
    pins = dp.parse_constraints("# c\nskcoord==0.1.87  # note\n")
    monkeypatch.setattr(dp, "installed_version", lambda n: Version("0.1.86"))
    drift = dp.find_drift(pins)
    assert drift and "installed 0.1.86" in drift[0] and "0.1.87" in drift[0]
    monkeypatch.setattr(dp, "installed_version", lambda n: None)
    assert "MISSING" in dp.find_drift(pins)[0]


def test_main_exit_codes(tmp_path, monkeypatch, capsys):
    good = tmp_path / "c.txt"
    good.write_text("skcoord==0.1.87\n")
    monkeypatch.setattr(dp, "installed_version", lambda n: Version("0.1.87"))
    assert dp.main(["--constraints", str(good)]) == 0
    monkeypatch.setattr(dp, "installed_version", lambda n: Version("0.1.86"))
    assert dp.main(["--constraints", str(good)]) == 3
    assert "DEPENDENCY_DRIFT" in capsys.readouterr().err
    assert dp.main(["--constraints", str(tmp_path / "absent.txt")]) == 2


def test_rollout_installs_with_constraints_then_verifies_parity():
    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        names = [n for n, _ in steps]
        assert names.index("pip_install") + 1 == names.index("verify_dependency_parity")
        table = dict(steps)
        assert "-c deploy/fleet-constraints.txt" in table["pip_install"]
        assert "dependency_parity.py" in table["verify_dependency_parity"]
        assert "{repo}/deploy/fleet-constraints.txt" in table["verify_dependency_parity"]


def test_parity_script_runs_by_path_without_package_import():
    import subprocess

    script = ROOT / "src" / "skcapstone" / "fleet" / "dependency_parity.py"
    result = subprocess.run(
        [sys.executable, str(script), "--constraints", str(CONSTRAINTS)],
        capture_output=True,
        text=True,
    )
    assert result.returncode in (0, 3), result.stderr
    if result.returncode == 3:
        assert "DEPENDENCY_DRIFT" in result.stderr


class TestSkwhisperTracked:
    """skwhisper must be reported, and must never be pinned below what hosts run.

    Added 2026-10-20: skwhisper was absent from ECOSYSTEM_PACKAGES, so a node
    whose subconscious daemon had been off for 17 days reported nothing at all.
    It is deliberately NOT in fleet-constraints.txt, because the index carries
    only 0.1.0 while every host runs 0.6.2 from git: a pin would either make
    resolution impossible or downgrade the fleet.
    """

    def test_skwhisper_is_tracked_by_version_check(self):
        from skcapstone.version_check import ECOSYSTEM_PACKAGES

        assert "skwhisper" in ECOSYSTEM_PACKAGES

    def test_skwhisper_is_not_pinned_while_index_lags(self):
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        text = (root / "deploy" / "fleet-constraints.txt").read_text(encoding="utf-8")
        pins = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        assert not any(p.startswith("skwhisper==") for p in pins), (
            "skwhisper pinned in fleet-constraints.txt: only do this once 0.6.2+ "
            "is on the index, or the rollout will downgrade or fail to resolve"
        )
        assert "skwhisper" in text, "the reason skwhisper is unpinned must stay documented"

    def test_skmemory_pin_matches_what_the_fleet_runs(self):
        """The pin is what rollout installs; a stale pin reverts a deployed fix."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        text = (root / "deploy" / "fleet-constraints.txt").read_text(encoding="utf-8")
        assert "skmemory==0.11.67" in text
