"""``skcapstone fleet node drift`` (Task 4, spec A.6).

This is what gives Task 3's ``detect_drift`` a caller: before this command
existed, nothing in the codebase called it at all. Follows the exact
``node doctor`` / ``node stignore`` shape (report only by default,
``--strict`` gates on any finding, ``--json`` for machine consumption), and
the same "writes nothing, judges only THIS machine" discipline.

Most tests here monkeypatch ``deployment_manifest.build_manifest`` and
``rollout_drift.detect_drift`` directly: those two functions are already
covered in depth by test_deployment_manifest.py and test_rollout_drift.py,
so this file is about the CLI's OWN responsibilities -- resolving
--repo-root/--home, rendering text and --json, the --strict exit code, and
turning a build/detect failure into a clean CLI error instead of a
traceback -- not re-proving detect_drift's internal correctness.
``test_real_pipeline_end_to_end`` is the one exception: it exercises the
real functions, unmocked, against this real checkout, to prove the wiring
itself (not just the mocked call shape) actually works.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

REPO_ROOT = Path(__file__).resolve().parents[2]

sys.path.insert(0, str(REPO_ROOT / "scripts" / "fleet"))
import skfleet_readiness  # noqa: E402

from skcapstone.fleet import deployment_manifest, rollout_drift  # noqa: E402
from skcapstone.fleet.cli import fleet  # noqa: E402
from skcapstone.fleet.rollout_drift import Drift  # noqa: E402


def _env() -> dict:
    return {"SKFLEET_NODE": "node-under-test"}


@pytest.fixture(autouse=True)
def _no_live_systemctl(monkeypatch):
    """Every unit test must never hit live systemd; a stray real call is a
    bug in the test, not a fact about the real fleet. Matches the pattern
    already established in tests/fleet/test_rollout_drift.py.

    ``skfleet_readiness.subprocess`` is the same global ``subprocess``
    module every other module in the process shares (there is only ever
    one ``sys.modules["subprocess"]``), so patching its ``run`` intercepts
    every subprocess call anywhere -- including ``deployment_manifest``'s
    own ``git rev-parse`` for a real build_manifest() call. Only a
    systemctl show invocation is faked; anything else (git, in
    particular) is passed straight through to the real ``subprocess.run``.
    """
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:3] != ["systemctl", "--user", "show"]:
            return real_run(cmd, **kwargs)
        prop = cmd[cmd.index("-p") + 1]
        value = "not-found" if prop == "LoadState" else ""
        return subprocess.CompletedProcess(cmd, 0, stdout=value + "\n", stderr="")

    monkeypatch.setattr(skfleet_readiness.subprocess, "run", fake_run)


@pytest.fixture
def fake_manifest(monkeypatch):
    """Stub build_manifest, and record how the CLI called it."""
    calls: dict = {}

    def fake_build_manifest(repo_root, home):
        calls["build_manifest"] = (Path(repo_root), Path(home))
        return {"git_sha": "abc12345", "units": []}

    monkeypatch.setattr(deployment_manifest, "build_manifest", fake_build_manifest)
    return calls


# --------------------------------------------------------------- text ---


def test_reports_no_drift_when_detect_drift_returns_empty(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: [])

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "no drift" in result.output
    assert "abc12345" in result.output


def test_reports_each_drift_kind_distinctly(tmp_path, monkeypatch, fake_manifest):
    drifts = [
        Drift("unit:x.service", "missing", "deadbeef", None, "node-under-test"),
        Drift("unit:y.service", "changed", "deadbeef", "c0ffee", "node-under-test"),
        Drift(
            "unit_enablement:z.timer",
            "enablement_mismatch",
            "enabled",
            "disabled",
            "node-under-test",
        ),
    ]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "missing" in result.output
    assert "changed" in result.output
    assert "enablement_mismatch" in result.output
    assert "3 drift" in result.output


def test_missing_unit_and_dispatcher_are_summarized_not_listed_by_default(
    tmp_path, monkeypatch, fake_manifest
):
    """Task 5, Part 1: 'missing' on a unit or the dispatcher cannot be told
    apart from a legitimate role absence without a per-host role manifest,
    which this estate does not have. Default text output must not print
    either by name, but must still say how many and how to see them.
    """
    drifts = [
        Drift("unit:a.service", "missing", "deadbeef", None, "node-under-test"),
        Drift("unit:b.service", "missing", "deadbeef", None, "node-under-test"),
        Drift("dispatcher:skfleet-rotate.py", "missing", "deadbeef", None, "node-under-test"),
        Drift("unit:c.service", "changed", "deadbeef", "c0ffee", "node-under-test"),
    ]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "unit:a.service" not in result.output
    assert "unit:b.service" not in result.output
    assert "dispatcher:skfleet-rotate.py" not in result.output
    assert "unit:c.service" in result.output  # unambiguous, still listed by name
    assert "3 unit(s)/dispatcher script reported missing" in result.output
    assert "no per-host role manifest" in result.output
    # --json expands the suppressed findings. --strict does NOT: in this
    # codebase it sets a non-zero exit code (see atlas_cmd, config_cmd) and
    # its output is identical to the default, so the summary must not send
    # an operator there expecting names.
    assert "--json" in result.output
    assert "--json or --strict" not in result.output


def test_a_missing_git_sha_is_unambiguous_and_still_listed_by_default(
    tmp_path, monkeypatch, fake_manifest
):
    """A 'missing' git_sha (the installed distribution itself could not be
    read) is not a role question, unlike a missing unit, so it stays in
    the default listing rather than folding into the summary count.
    """
    drifts = [Drift("git_sha", "missing", "deadbeef", None, "node-under-test")]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "git_sha" in result.output
    assert "reported missing, not listed" not in result.output


def test_a_missing_script_file_is_unambiguous_and_listed_by_default(
    tmp_path, monkeypatch, fake_manifest
):
    """Unlike a missing unit (which a host's role may legitimately never
    install), a missing pyproject.toml script-files entry has no such
    excuse: pip installs every script-files entry into ~/.skenv/bin on
    every host regardless of role, so its absence is a fact about this
    host, not a role guess -- it must stay in the default listing by name.
    Measured live: skfleet_readiness.py is genuinely missing from
    chiap01's ~/.skenv/bin.
    """
    drifts = [
        Drift(
            "script:skfleet_readiness.py",
            "missing",
            "deadbeef",
            None,
            "node-under-test",
        )
    ]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "script:skfleet_readiness.py" in result.output
    assert "reported missing, not listed" not in result.output


def test_a_failed_unit_is_unambiguous_and_listed_by_default(tmp_path, monkeypatch, fake_manifest):
    """A 'failed' finding (the chiap08 incident: a unit stuck FAILED) is a
    fact about that unit, not a guess about role, so it stays in the
    default listing by name rather than folding into the missing summary.
    """
    drifts = [
        Drift(
            "unit_failed:skfleet-niobe-shadow.service",
            "failed",
            "not failed",
            "failed",
            "node-under-test",
        )
    ]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "unit_failed:skfleet-niobe-shadow.service" in result.output
    assert "reported missing, not listed" not in result.output


def test_all_missing_drift_still_shows_the_summary_with_no_unambiguous_line(
    tmp_path, monkeypatch, fake_manifest
):
    drifts = [Drift("unit:a.service", "missing", "deadbeef", None, "node-under-test")]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert "no unambiguous drift" in result.output
    assert "1 unit(s)/dispatcher script reported missing" in result.output


def test_strict_still_exits_one_on_missing_only_drift(tmp_path, monkeypatch, fake_manifest):
    """--strict must act on every finding, missing included, even though
    the default text rendering summarizes rather than lists them.
    """
    drifts = [Drift("unit:a.service", "missing", "deadbeef", None, "node-under-test")]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--strict", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 1


def test_json_lists_every_missing_drift_by_name_regardless_of_default_summary(
    tmp_path, monkeypatch, fake_manifest
):
    drifts = [Drift("unit:a.service", "missing", "deadbeef", None, "node-under-test")]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path), "--json"],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["drifts"] == [
        {"artifact": "unit:a.service", "kind": "missing", "expected": "deadbeef", "found": None}
    ]


# --------------------------------------------------------------- json ---


def test_json_output_carries_every_field(tmp_path, monkeypatch, fake_manifest):
    drifts = [Drift("git_sha", "changed", "deadbeef", "c0ffee00", "node-under-test")]
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: drifts)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path), "--json"],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["node"] == "node-under-test"
    assert payload["git_sha"] == "abc12345"
    assert payload["drifts"] == [
        {"artifact": "git_sha", "kind": "changed", "expected": "deadbeef", "found": "c0ffee00"}
    ]


# ------------------------------------------------------------ exit codes ---


def test_report_only_exits_zero_with_drift_by_default(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(
        rollout_drift,
        "detect_drift",
        lambda manifest, home, repo_root: [Drift("git_sha", "changed", "a", "b", "n")],
    )

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output


def test_strict_exits_one_when_drift_found(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(
        rollout_drift,
        "detect_drift",
        lambda manifest, home, repo_root: [Drift("git_sha", "changed", "a", "b", "n")],
    )

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--strict", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 1


def test_strict_exits_zero_when_no_drift(tmp_path, monkeypatch, fake_manifest):
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home, repo_root: [])

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--strict", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output


# ------------------------------------------------------------ resolution ---


def test_calls_build_manifest_and_detect_drift_with_resolved_paths(
    tmp_path, monkeypatch, fake_manifest
):
    home = tmp_path / "home"
    repo = tmp_path / "repo"
    seen: dict = {}

    def fake_detect_drift(manifest, home_, repo_root_):
        seen["manifest"] = manifest
        seen["home"] = Path(home_)
        seen["repo_root"] = Path(repo_root_)
        return []

    monkeypatch.setattr(rollout_drift, "detect_drift", fake_detect_drift)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(repo), "--home", str(home)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    assert seen["home"] == home
    assert seen["repo_root"] == repo
    assert fake_manifest["build_manifest"] == (repo, home)


def test_default_repo_root_uses_env_override(tmp_path, monkeypatch, fake_manifest):
    custom_repo = tmp_path / "custom-repo"
    monkeypatch.setenv("SKCAPSTONE_REPO_ROOT", str(custom_repo))
    seen: dict = {}
    monkeypatch.setattr(
        rollout_drift,
        "detect_drift",
        lambda manifest, home_, repo_root_: seen.update(repo_root=Path(repo_root_)) or [],
    )

    result = CliRunner().invoke(
        fleet, ["node", "drift", "--home", str(tmp_path / "home")], env=_env()
    )

    assert result.exit_code == 0, result.output
    assert seen["repo_root"] == custom_repo


# ------------------------------------------------------------- errors ---


def test_a_build_manifest_error_is_a_clean_cli_error_not_a_traceback(tmp_path, monkeypatch):
    def boom(repo_root, home):
        raise RuntimeError("could not resolve the git HEAD")

    monkeypatch.setattr(deployment_manifest, "build_manifest", boom)

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(tmp_path)],
        env=_env(),
    )

    assert result.exit_code != 0
    assert not isinstance(result.exception, RuntimeError)
    assert "could not resolve the git HEAD" in result.output


# ----------------------------------------------------------- zero writes ---


def test_writes_nothing(tmp_path, monkeypatch, fake_manifest):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(rollout_drift, "detect_drift", lambda manifest, home_, repo_root_: [])
    before = sorted(home.rglob("*"))

    CliRunner().invoke(
        fleet, ["node", "drift", "--repo-root", str(tmp_path), "--home", str(home)], env=_env()
    )
    CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(tmp_path), "--home", str(home), "--json"],
        env=_env(),
    )

    assert sorted(home.rglob("*")) == before


# --------------------------------------------------------- real pipeline ---


def test_real_pipeline_end_to_end(tmp_path):
    """No mocking of build_manifest/detect_drift: exercises the real Task
    1 + Task 3 pipeline through the CLI against an empty, throwaway host,
    to prove the wiring itself (not just a mocked call shape) works.
    """
    home = tmp_path / "home"

    result = CliRunner().invoke(
        fleet,
        ["node", "drift", "--repo-root", str(REPO_ROOT), "--home", str(home)],
        env=_env(),
    )

    assert result.exit_code == 0, result.output
    # An empty host has no dist-info at all: git_sha must read as missing.
    assert "git_sha" in result.output
    assert "missing" in result.output
