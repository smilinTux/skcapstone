"""CMDB network reconcile units, timer and runner (gate 4, card 075a82ce)."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "scripts" / "skcapstone-cmdb-network"
LAUNCHER = ROOT / "src" / "skcapstone" / "data" / "cmdb-network-apply.sh"
UNITS = (
    "skcapstone-cmdb-reconcile-network.service",
    "skcapstone-cmdb-reconcile-network.timer",
    "skcapstone-cmdb-reconcile-shadow.service",
)
# Legacy local timer, installed by the skbrain pack schedule (not in this repo).
LEGACY_CALENDAR = "*-*-* 00/3:17:00"
JITTER_MIN = 2
RUN_MIN = 5  # local runs take about 2 minutes; keep generous headroom


def _unit(name: str) -> str:
    return (ROOT / "src" / "skcapstone" / "data" / "systemd" / name).read_text()


@pytest.mark.parametrize("name", UNITS)
def test_packaged_and_repo_unit_copies_match(name):
    packaged = ROOT / "src" / "skcapstone" / "data" / "systemd" / name
    assert packaged.read_bytes() == (ROOT / "systemd" / name).read_bytes()


def _start_minutes(calendar: str) -> set[int]:
    match = re.fullmatch(r"\*-\*-\* (\d+)/3:(\d+):00", calendar)
    assert match, calendar
    first, minute = int(match.group(1)), int(match.group(2))
    return {(hour * 60 + minute) for hour in range(first, 24, 3)}


def test_network_timer_never_overlaps_the_legacy_local_timer():
    text = _unit("skcapstone-cmdb-reconcile-network.timer")
    calendar = re.search(r"^OnCalendar=(.+)$", text, re.M).group(1)
    network, legacy = _start_minutes(calendar), _start_minutes(LEGACY_CALENDAR)
    assert len(network) == 8
    window = JITTER_MIN + RUN_MIN
    for start in network:
        for other in legacy:
            gap = min((start - other) % 1440, (other - start) % 1440)
            assert gap > window, (calendar, start, other)


def test_network_timer_targets_the_guarded_apply_unit_without_catch_up():
    text = _unit("skcapstone-cmdb-reconcile-network.timer")
    assert "Unit=skcapstone-cmdb-reconcile-network.service" in text
    assert "Persistent=false" in text


def test_apply_unit_still_requires_the_owner_launcher():
    text = _unit("skcapstone-cmdb-reconcile-network.service")
    assert "ConditionPathExists=%h/.config/skcapstone/cmdb-network-apply" in text
    assert "ExecStart=%h/.config/skcapstone/cmdb-network-apply" in text


def test_shadow_unit_is_write_free_and_needs_targets():
    text = _unit("skcapstone-cmdb-reconcile-shadow.service")
    assert "ExecStart=%h/.skenv/bin/skcapstone-cmdb-network shadow" in text
    assert "ConditionPathExists=%h/.config/skcapstone/cmdb-network-targets" in text
    assert "--apply" not in text.split("[Service]")[1].replace("without --apply", "")


def test_launcher_runs_the_apply_mode_only():
    text = LAUNCHER.read_text()
    assert 'exec "$HOME/.skenv/bin/skcapstone-cmdb-network" apply' in text


def test_runner_is_shipped_as_a_script_file():
    assert '"scripts/skcapstone-cmdb-network"' in (ROOT / "pyproject.toml").read_text()
    assert os.access(RUNNER, os.X_OK)


def _run(tmp_path: Path, mode: str, targets: str | None, perm: int = 0o600):
    stub = tmp_path / "stub"
    stub.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@"\n')
    stub.chmod(0o755)
    path = tmp_path / "targets"
    if targets is not None:
        path.write_text(targets)
        path.chmod(perm)
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "SKCAPSTONE_CMDB_NETWORK_TARGETS": str(path),
        "SKCAPSTONE_BIN": "skcapstone",
        "SK_CRON_RUN": str(stub),
        "SKCAPSTONE_CMDB_NETWORK_LOCK": str(tmp_path / "lock"),
    }
    return subprocess.run(
        [str(RUNNER), mode], env=env, capture_output=True, text=True, check=False
    )


TARGETS = "# fleet\nnoroc2027=skvault://ssh/noroc2027\n\n192.168.0.100=skvault://ssh/ollama\n"


def test_runner_apply_builds_the_exact_reconcile_command(tmp_path):
    result = _run(tmp_path, "apply", TARGETS)
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == [
        "skcapstone-cmdb-reconcile-network",
        "skcapstone",
        "cmdb",
        "reconcile",
        "--network",
        "--apply",
        "--record-run",
        "--credential",
        "noroc2027=skvault://ssh/noroc2027",
        "--credential",
        "192.168.0.100=skvault://ssh/ollama",
    ]


def test_runner_shadow_never_applies(tmp_path):
    result = _run(tmp_path, "shadow", TARGETS)
    assert result.returncode == 0, result.stderr
    argv = result.stdout.split()
    assert argv[0] == "skcapstone-cmdb-reconcile-shadow"
    assert "--apply" not in argv
    assert "--record-run" in argv


@pytest.mark.parametrize(
    ("targets", "perm", "reason"),
    [
        (None, 0o600, "missing"),
        (TARGETS, 0o620, "writable"),
        ("noroc2027=hunter2\n", 0o600, "malformed"),
        ("noroc2027=skvault://a\nnoroc2027=skvault://b\n", 0o600, "duplicate"),
        ("# nothing\n", 0o600, "no target"),
    ],
)
def test_runner_refuses_unsafe_or_bad_targets(tmp_path, targets, perm, reason):
    result = _run(tmp_path, "apply", targets, perm)
    assert result.returncode == 78
    assert reason in result.stderr
    assert result.stdout == ""


def test_runner_requires_an_explicit_mode(tmp_path):
    assert _run(tmp_path, "", TARGETS).returncode == 64
