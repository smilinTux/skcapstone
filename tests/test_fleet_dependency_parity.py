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
REQUIREMENTS = ROOT / "deploy" / "fleet-requirements.in"
# Every SK runtime package the fleet installs from a release (2026-11-14 adds the
# rest of the set; skchat-sovereign, sksecurity, skbrain and skvault are held
# back, see the comment in deploy/fleet-constraints.txt).
SK_RUNTIME = (
    "skcoord",
    "skmemory",
    "cloud9",
    "skwhisper",
    "capauth",
    "skseed",
    "skdashboard",
    "skharness",
    "skskills",
    "skcomms",
)


def _pins():
    """The SK pins: the required entries of the lock (deploy/fleet-requirements.in)."""
    return dp.load_constraints(REQUIREMENTS)


def _lock():
    return dp.load_constraints(CONSTRAINTS)


def _project_requirements():
    """Base AND optional dependencies.

    CI and rollout both install with `-c deploy/fleet-constraints.txt` against
    `.[all,dev]`, so an optional dependency is just as much "what production
    runs" as a base one. Reading only `project.dependencies` made the constraints
    file unable to pin an optional dep at all: adding `skwhisper==0.6.2` (which
    the `consciousness` and `all` extras depend on) failed
    `test_every_pyproject_sk_dependency_is_constrained` as "a package pyproject
    does not depend on", which was simply untrue.
    """
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    reqs = [Requirement(r) for r in config["project"]["dependencies"]]
    for extra_reqs in (config["project"].get("optional-dependencies") or {}).values():
        for raw in extra_reqs:
            # Skip direct-reference requirements (`pkg @ git+...`); they are not
            # index-resolvable and are never pinned here.
            if "@" in raw and "://" in raw:
                continue
            reqs.append(Requirement(raw))
    return reqs


def test_constraints_are_exact_pins_for_every_sk_runtime_dependency():
    pins = _pins()
    for name in SK_RUNTIME:
        assert canonicalize_name(name) in pins, f"{name} missing from fleet-requirements.in"


def test_lock_carries_every_required_pin_at_the_same_version():
    """The lock is generated from the requirements file; a hand edit of one
    without rerunning scripts/fleet-lock.sh must not pass."""
    lock = _lock()
    for name, want in _pins().items():
        assert lock.get(name) == want, (
            f"{name}=={want} in fleet-requirements.in but the lock has {lock.get(name)}; "
            "run scripts/fleet-lock.sh"
        )


def test_lock_is_fully_resolved():
    """Every transitive dependency of the SK set is in the lock, not just the SK pins."""
    lock = _lock()
    assert len(lock) > len(_pins()) * 5
    for name in ("click", "typer", "rich", "fastapi", "huggingface-hub", "cryptography"):
        assert name in lock, f"{name} missing from the lock"
    assert "skcapstone" not in lock, "skcapstone is installed from the checkout, never locked"


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


def test_parse_evaluates_markers_and_rejects_double_pins():
    text = (
        'numpy==2.5.2 ; python_version < "3.14"\n'
        'numpy==2.5.4 ; python_version >= "3.14"\n'
        "colorama==0.4.6 ; sys_platform == 'win32'\n"
    )
    pins = dp.parse_constraints(text, {"python_version": "3.12", "sys_platform": "linux"})
    assert pins == {"numpy": Version("2.5.2")}
    pins = dp.parse_constraints(text, {"python_version": "3.14", "sys_platform": "linux"})
    assert pins == {"numpy": Version("2.5.4")}
    with pytest.raises(ValueError, match="pinned twice"):
        dp.parse_constraints("a==1\na==2\n")


def test_optional_lock_entries_may_be_absent_but_never_differ(monkeypatch):
    pins = {"skcoord": Version("0.1.97"), "twine": Version("7.0.0"), "torch": Version("2.13")}
    installed = {"skcoord": Version("0.1.97"), "twine": Version("6.2.0")}
    monkeypatch.setattr(dp, "installed_version", lambda n: installed.get(n))
    drift = dp.find_drift(pins, required={"skcoord"})
    assert drift == ["twine: installed 6.2.0, constraints pin 7.0.0"]  # torch absent is fine
    installed.pop("skcoord")
    assert any("skcoord: installed MISSING" in d for d in dp.find_drift(pins, {"skcoord"}))
    # Without a requirements file (pre-lock revision) every entry is required.
    assert any("torch" in d for d in dp.find_drift(pins, None))


def test_write_installed_lists_only_what_the_host_has(tmp_path, monkeypatch):
    lock = tmp_path / "fleet-constraints.txt"
    lock.write_text("skcapstone==1\ntwine==7.0.0\ntorch==2.13.0\n")
    (tmp_path / "fleet-requirements.in").write_text("# none\n")
    have = {"twine": Version("6.2.0"), "skcapstone": Version("1")}
    monkeypatch.setattr(dp, "installed_version", lambda n: have.get(n))
    out = tmp_path / "cache" / "installed.txt"
    assert dp.main(["--constraints", str(lock), "--write-installed", str(out)]) == 0
    assert out.read_text() == "twine==7.0.0\n"


def test_rollout_installs_with_constraints_then_verifies_parity():
    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        names = [n for n, _ in steps]
        assert names.index("pip_install") + 1 == names.index("verify_dependency_parity")
        # pip check right after parity: a conflicting set halts on the canary.
        assert names.index("verify_dependency_parity") + 1 == names.index("verify_pip_check")
        assert dict(steps)["verify_pip_check"] == "~/.skenv/bin/python -m pip check"
        install = dict(steps)["pip_install"]
        assert "-r deploy/fleet-requirements.in" in install
        assert "--write-installed ~/.cache/skcapstone-rollout/installed-lock.txt" in install
        assert "-r ~/.cache/skcapstone-rollout/installed-lock.txt" in install
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

    It was also deliberately UNPINNED at first, because the index carried only
    0.1.0 while every host ran 0.6.2 from git, so a pin would either have been
    unresolvable or would have downgraded the fleet. smilinTux/skwhisper#1 added
    the missing publish workflow and a PyPI Trusted Publisher was configured, so
    the fleet release reached the index and the pin now matches what the fleet
    runs. These tests moved with that: the pin is now REQUIRED, and required to
    be >= what the hosts run.
    """

    def test_skwhisper_is_tracked_by_version_check(self):
        from skcapstone.version_check import ECOSYSTEM_PACKAGES

        assert "skwhisper" in ECOSYSTEM_PACKAGES

    def test_skwhisper_is_pinned_to_what_the_fleet_runs(self):
        """Now that the fleet release is indexed, the pin must not go backwards.

        A pin below what the hosts run would make the rollout DOWNGRADE every
        node's subconscious layer, which is the failure this guards against.
        """
        from pathlib import Path

        from packaging.version import Version

        root = Path(__file__).resolve().parents[1]
        text = (root / "deploy" / "fleet-constraints.txt").read_text(encoding="utf-8")
        pins = [
            line.strip()
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        pin = next((p for p in pins if p.startswith("skwhisper==")), None)
        assert pin, "skwhisper must be pinned in fleet-constraints.txt"
        assert Version(pin.split("==", 1)[1]) >= Version(
            "0.6.4"
        ), f"{pin} is below the 0.6.4 upgraded fleet version: rollout would downgrade it"

    def test_skmemory_pin_matches_what_the_fleet_runs(self):
        """The pin is what rollout installs; a stale pin reverts a deployed fix."""
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        text = (root / "deploy" / "fleet-constraints.txt").read_text(encoding="utf-8")
        assert "skmemory==0.11.172" in text
