"""Tests for the read-only node inventory collector (card 39e8a061).

Two properties matter more than coverage here: the module must never grow an
actuation verb, and it must degrade to empty rather than raise, because the
drift report it feeds runs on every node.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from skcapstone.fleet import nodeinventory

USER_UNITS_STDOUT = """\
comfyui.service                enabled enabled
f5-tts.service                 enabled enabled
skai-beellama.service          enabled enabled
syncthing.service              enabled enabled
gpg-agent.socket               enabled enabled
skai-beellama-restart.timer    enabled enabled
"""

SYSTEM_UNITS_STDOUT = """\
ollama.service                 enabled enabled
mxbai-arc.service              enabled enabled
tailscaled.service             enabled enabled
"""


def _proc(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr="")


def _fake_runner(stdout_by_scope: dict[str, str], returncode: int = 0):
    """A runner that answers per systemd scope and records what it was asked."""
    calls: list[list[str]] = []

    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        calls.append(cmd)
        scope = "user" if "--user" in cmd else "system"
        return _proc(stdout_by_scope.get(scope, ""), returncode)

    run.calls = calls  # type: ignore[attr-defined]
    return run


# ----------------------------------------------------------- happy path ---


def test_enabled_user_units_parsed_and_sorted() -> None:
    runner = _fake_runner({"user": USER_UNITS_STDOUT})
    units = nodeinventory.enabled_units(user=True, runner=runner)
    assert units["comfyui.service"] == "enabled"
    assert units["skai-beellama-restart.timer"] == "enabled"
    assert list(units) == sorted(units)
    assert len(units) == 6


def test_user_scope_uses_the_user_flag() -> None:
    runner = _fake_runner({"user": USER_UNITS_STDOUT})
    nodeinventory.enabled_units(user=True, runner=runner)
    assert runner.calls == [
        ["systemctl", "--user", "list-unit-files", "--state=enabled", "--no-legend"]
    ]


def test_system_scope_omits_the_user_flag() -> None:
    runner = _fake_runner({"system": SYSTEM_UNITS_STDOUT})
    units = nodeinventory.enabled_units(user=False, runner=runner)
    assert "--user" not in runner.calls[0]
    assert units == {
        "mxbai-arc.service": "enabled",
        "ollama.service": "enabled",
        "tailscaled.service": "enabled",
    }


def test_collect_shape_and_determinism() -> None:
    runner = _fake_runner({"user": USER_UNITS_STDOUT, "system": SYSTEM_UNITS_STDOUT})
    first = nodeinventory.collect(runner=runner, now_iso="2026-08-14T00:00:00Z")
    assert set(first) == {"units", "packages", "collectedAt"}
    assert first["collectedAt"] == "2026-08-14T00:00:00Z"
    # System scope is behind the flag, and an uncollected scope is absent
    # rather than empty, so the two cases can never be confused.
    assert set(first["units"]) == {"user"}
    second = nodeinventory.collect(runner=runner, include_system=True, now_iso="x")
    assert set(second["units"]) == {"user", "system"}


def test_body_drops_the_timestamp_so_write_on_change_stays_quiet() -> None:
    """store.write_node_file() is write-on-change: a moving timestamp would
    rewrite node.json every 60s and flood the control-bus folder."""
    runner = _fake_runner({"user": USER_UNITS_STDOUT})
    a = nodeinventory.collect(runner=runner, now_iso="2026-08-14T00:00:00Z")
    b = nodeinventory.collect(runner=runner, now_iso="2026-08-14T23:59:59Z")
    assert a != b
    assert nodeinventory.body(a) == nodeinventory.body(b)
    assert "collectedAt" not in nodeinventory.body(a)


# ------------------------------------------------------------- degrades ---


def test_non_zero_exit_degrades_to_empty() -> None:
    runner = _fake_runner({"user": USER_UNITS_STDOUT}, returncode=1)
    assert nodeinventory.enabled_units(user=True, runner=runner) == {}


def test_empty_output_is_an_empty_inventory_not_an_error() -> None:
    runner = _fake_runner({"user": ""})
    assert nodeinventory.enabled_units(user=True, runner=runner) == {}


def test_runner_raising_degrades_to_empty() -> None:
    def boom(cmd: list[str]) -> subprocess.CompletedProcess:
        raise OSError("systemctl not found")

    assert nodeinventory.enabled_units(user=True, runner=boom) == {}


def test_collect_survives_a_dead_runner() -> None:
    def boom(cmd: list[str]) -> subprocess.CompletedProcess:
        raise OSError("no systemd here")

    out = nodeinventory.collect(runner=boom, include_system=True, now_iso="t")
    assert out["units"] == {"user": {}, "system": {}}


def test_malformed_lines_are_skipped_not_guessed_at() -> None:
    runner = _fake_runner({"user": "garbage\n\nok.service enabled enabled\n   \n"})
    assert nodeinventory.enabled_units(user=True, runner=runner) == {"ok.service": "enabled"}


# ------------------------------------------------------------- packages ---


@pytest.mark.parametrize(
    "name,expected",
    [
        ("skcapstone", True),
        ("skmemory", True),
        ("SKChat", True),
        ("capauth", True),
        ("cloud9", True),
        ("pytest", False),
        ("click", False),
        ("scipy", False),
    ],
)
def test_sk_namespace_membership(name: str, expected: bool) -> None:
    assert nodeinventory.is_sk_package(name) is expected


def test_sk_packages_finds_the_installed_ecosystem() -> None:
    packages = nodeinventory.sk_packages()
    assert "skcapstone" in packages
    assert packages["skcapstone"]
    assert list(packages) == sorted(packages)
    assert all(nodeinventory.is_sk_package(name) for name in packages)


# --------------------------------------------------------- zero actuation ---


def test_module_contains_no_actuation_verb() -> None:
    """The inventory observes and never touches. This test is the guard that
    keeps it that way as the module grows."""
    source = Path(nodeinventory.__file__).read_text(encoding="utf-8")
    commands = [
        line
        for line in source.splitlines()
        if '"systemctl"' in line or "'systemctl'" in line or '"pip"' in line
    ]
    for line in commands:
        for verb in ("start", "stop", "restart", "enable", "disable", "install", "kill"):
            assert f'"{verb}"' not in line, f"actuation verb {verb!r} in: {line.strip()}"


def test_no_bare_subprocess_call_outside_the_runner() -> None:
    source = Path(nodeinventory.__file__).read_text(encoding="utf-8")
    assert "subprocess.run" not in source
    assert "os.system" not in source
    assert "popen" not in source.lower()
