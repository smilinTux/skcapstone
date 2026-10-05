"""Real temporary-file rollout regressions, without hosts or systemd mutation."""

import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skcapstone.fleet import deployment_manifest, paths, rollout_artifacts
from skcapstone.fleet.deployment_manifest import PER_HOST_ARTIFACTS, production_compatibility_shim
from skcapstone.fleet.paths import paths_for_home
from skcapstone.fleet.rollout_artifacts import install_artifact, install_units
from skcapstone.fleet.staged_rollout import _DEPLOY_STEPS, _ROLLBACK_STEPS, _remote_drift


@pytest.fixture
def layout(tmp_path):
    """Create a synthetic checkout and a production home."""
    repo, home = tmp_path / "repo", tmp_path / "home"
    (repo / "systemd/production").mkdir(parents=True)
    (repo / "systemd/sknoded.service").write_text(
        "[Service]\nEnvironmentFile=-%h/.config/sknoded/operator-http.env\nExecStart=main\n"
    )
    (repo / "systemd/skfleet-readiness.service").write_bytes(b"legacy readiness\n")
    (repo / "systemd/production/skfleet-readiness.service").write_bytes(
        b"main production readiness\n"
    )
    (repo / "systemd/skfleet-seat-cycle.timer").write_bytes(b"main timer\n")
    for relative in PER_HOST_ARTIFACTS:
        source = repo / relative
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"#!/usr/bin/env python3\n# deployed " + source.name.encode() + b"\n")
    fleet = paths_for_home(home).root
    fleet.mkdir(parents=True)
    (fleet / "production.json").write_text("{}")
    return repo, home


@pytest.mark.parametrize("name", ["skfleet-rotate.py", "skfleet-worker-wrapper.py"])
def test_production_shim_is_byte_identical_to_drift_contract(layout, name):
    """Delegate to the pip-installed script, preserving executable mode."""
    repo, home = layout
    install_artifact(repo, home, name)
    target = home / ".local/bin" / name
    assert target.read_bytes() == production_compatibility_shim(name, home)
    assert target.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize("production", [False, True])
def test_unpackaged_sweep_remains_a_full_copy(layout, production):
    """There is no native pip script for the sweep shim to delegate to."""
    repo, home = layout
    if not production:
        (paths_for_home(home).root / "production.json").unlink()
    install_artifact(repo, home, "skwork-sweep.py")
    assert (home / ".local/bin/skwork-sweep.py").read_bytes() == (
        repo / "scripts/fleet/skwork-sweep.py"
    ).read_bytes()


def test_legacy_dispatcher_remains_a_full_copy(layout):
    """Non-production hosts retain the checker's original full-copy contract."""
    repo, home = layout
    (paths_for_home(home).root / "production.json").unlink()
    install_artifact(repo, home, "skfleet-rotate.py")
    assert (home / ".local/bin/skfleet-rotate.py").read_bytes() == (
        repo / "scripts/fleet/skfleet-rotate.py"
    ).read_bytes()


@pytest.mark.parametrize("production", [False, True])
def test_unit_bytes_match_checker_selection_and_retain_preimages(layout, production):
    """Install timers too, with production precedence and durable old unit bytes."""
    repo, home = layout
    if not production:
        (paths_for_home(home).root / "production.json").unlink()
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    old = b"old readiness\n"
    (units / "skfleet-readiness.service").write_bytes(old)
    install_units(repo, home)
    prefix = "systemd/production" if production else "systemd"
    assert (units / "skfleet-readiness.service").read_bytes() == (
        repo / prefix / "skfleet-readiness.service"
    ).read_bytes()
    for name in ("sknoded.service", "skfleet-seat-cycle.timer"):
        assert (units / name).read_bytes() == (repo / "systemd" / name).read_bytes()
    backup = paths_for_home(home).root / "rollout-unit-preimages" / hashlib.sha256(old).hexdigest()
    assert (backup / "skfleet-readiness.service").read_bytes() == old


def test_host_worker_values_survive_main_unit_and_existing_env_wins(layout):
    """Do not overwrite operator settings or mix host values into shipped unit bytes."""
    repo, home = layout
    units = home / ".config/systemd/user"
    units.mkdir(parents=True)
    (units / "sknoded.service").write_text(
        '[Service]\nEnvironment="SKFLEET_PI=/opt/pi with spaces"\n'
        'Environment="SKFLEET_PI_CARDSTORE_GUARD=%h/.skenv/bin/guard.mjs"\n'
        "Environment=UNRELATED_SECRET=synthetic\n"
    )
    dropins = units / "sknoded.service.d"
    dropins.mkdir()
    (dropins / "20-policy.conf").write_text(
        "[Service]\nEnvironment=SKFLEET_AUTHORITY_HOST=chiap08\n"
    )
    environment = home / ".config/sknoded/operator-http.env"
    environment.parent.mkdir(parents=True)
    original = b"# existing operator gate\nSKFLEET_PI=/operator/pi\nOPERATOR_HTTP=0"
    environment.write_bytes(original)
    install_units(repo, home)
    first = environment.read_bytes()
    assert first.startswith(original + b"\n")
    assert b"/opt/pi with spaces" not in first
    assert b"UNRELATED_SECRET" not in first
    assignments = dict(item.split("=", 1) for item in shlex.split(first.decode(), comments=True))
    assert assignments["SKFLEET_PI"] == "/operator/pi"
    assert assignments["SKFLEET_PI_CARDSTORE_GUARD"] == str(home / ".skenv/bin/guard.mjs")
    assert assignments["SKFLEET_AUTHORITY_HOST"] == "chiap08"
    assert environment.stat().st_mode & 0o777 == 0o600
    assert (units / "sknoded.service").read_bytes() == (
        repo / "systemd/sknoded.service"
    ).read_bytes()
    install_units(repo, home)
    assert environment.read_bytes() == first


def test_unknown_artifact_and_missing_unit_tree_fail_closed(layout):
    """A bad artifact or checkout cannot silently succeed."""
    repo, home = layout
    with pytest.raises(ValueError, match="undeclared"):
        install_artifact(repo, home, "other.py")
    for source in (repo / "systemd").glob("*.*"):
        source.unlink()
    with pytest.raises(RuntimeError, match="no shipped"):
        install_units(repo, home)


def test_remote_gate_explicitly_uses_expanded_requested_checkout():
    """Changing working directory alone did not override the CLI default checkout."""
    calls = []

    def runner(argv):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, '{"drifts": []}', "")

    assert _remote_drift("chiap03", "~/deploy/skcapstone", runner, "9533b254") == ([], "")
    command = shlex.split(" ".join(calls[0][2:]))[2]
    assert command.startswith("cd ~/deploy/skcapstone && ")
    assert '--repo-root "$PWD"' in command
    assert "--expect-git-sha 9533b254" in command


def test_remote_shell_checks_deploy_checkout_instead_of_cli_default(tmp_path):
    """Exercise tilde/PWD expansion and a CLI whose default remains the work checkout."""
    home = tmp_path / "remote-home"
    deployed = home / "deploy/skcapstone"
    legacy = home / "work/skcapstone"
    deployed.mkdir(parents=True)
    legacy.mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cli = bin_dir / "skcapstone"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "args = sys.argv\n"
        "root = (args[args.index('--repo-root') + 1] if '--repo-root' in args\n"
        "        else os.path.expanduser('~/work/skcapstone'))\n"
        "print(json.dumps({'drifts': [], 'checked_repo': root}))\n"
    )
    cli.chmod(0o755)
    observed = []

    def runner(argv):
        command = shlex.split(" ".join(argv[2:]))[2]
        result = subprocess.run(
            ["bash", "-c", command],
            env={"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=15,
        )
        observed.append(json.loads(result.stdout)["checked_repo"])
        return result

    assert _remote_drift("chiap03", "~/deploy/skcapstone", runner, "9533b254") == ([], "")
    assert observed == [str(deployed)]


def test_unit_install_reloads_definitions_without_enable_start_or_restart():
    """Rollout does not turn a stopped destination consumer on as a side effect."""
    for steps in (_DEPLOY_STEPS, _ROLLBACK_STEPS):
        names = [name for name, _ in steps]
        assert names.index("pip_install") < names.index("install_units") < names.index("converge")
        command = dict(steps)["install_units"]
        assert "systemctl --user daemon-reload" in command
        assert all(action not in command for action in (" enable", " start", " restart"))


def test_rollback_retains_tool_before_checkout_without_importing_new_manifest_symbol():
    """The previous main predates rollout_artifacts; reset must not remove the installer."""
    names = [name for name, _ in _ROLLBACK_STEPS]
    assert names.index("preserve_rollout_tool") < names.index("git_checkout")
    commands = dict(_ROLLBACK_STEPS)
    for name in ("copy_skfleet_rotate", "copy_skfleet_worker_wrapper", "install_units"):
        assert "~/.cache/skcapstone-rollout/rollout_artifacts.py" in commands[name]


def test_cached_installer_runs_when_target_package_has_no_rollout_artifacts(layout, tmp_path):
    """Execute the retained tool against the older manifest/path API on disk."""
    repo, home = layout
    package = tmp_path / "old-package/skcapstone"
    fleet = package / "fleet"
    fleet.mkdir(parents=True)
    (package / "__init__.py").touch()
    (fleet / "__init__.py").touch()
    for module in (deployment_manifest, paths):
        shutil.copyfile(module.__file__, fleet / Path(module.__file__).name)
    assert not (fleet / "rollout_artifacts.py").exists()
    assert b"unit_source_path" not in (fleet / "deployment_manifest.py").read_bytes()
    tool = tmp_path / "cached-rollout_artifacts.py"
    shutil.copyfile(rollout_artifacts.__file__, tool)
    environment = {
        key: value for key, value in os.environ.items() if key not in {"BASH_ENV", "VIRTUAL_ENV"}
    }
    environment.update(HOME=str(home), PYTHONPATH=str(package.parent))
    result = subprocess.run(
        [sys.executable, str(tool), str(repo), "units"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert (home / ".config/systemd/user/sknoded.service").read_bytes() == (
        repo / "systemd/sknoded.service"
    ).read_bytes()


@pytest.mark.parametrize("production", [False, True])
def test_fresh_sknoded_install_persists_production_context(tmp_path, production):
    """Fresh nodes receive policy bindings without a historical host environment."""
    repo = Path(__file__).resolve().parents[2]
    home = tmp_path / "home"
    fleet = paths_for_home(home).root
    fleet.mkdir(parents=True)
    if production:
        (fleet / "production.json").write_text("{}")
    install_units(repo, home)
    unit = home / ".config/systemd/user/sknoded.service"
    expected = rollout_artifacts.unit_source_path(repo, "sknoded.service", production)
    assert unit.read_bytes() == expected.read_bytes()
    assert unit.stat().st_mode & 0o777 == 0o644
    context = {
        line.removeprefix("Environment=")
        for line in unit.read_text().splitlines()
        if line.startswith("Environment=SKFLEET_")
    }
    wanted = {
        "SKFLEET_PRODUCTION_POLICY=%h/.skcapstone/fleet/production.json",
        "SKFLEET_AUTHORITY_HOST=chiap08",
    }
    assert context == (wanted if production else set())
    assert not (home / ".config/sknoded/operator-http.env").exists()
