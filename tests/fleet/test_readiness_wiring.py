"""Enabled-aware role scoping for the readiness gate, plus its verdict file.

The readiness gate (`scripts/fleet/skfleet_readiness.py`) has never had a
caller: confirmed by two reviews weeks apart and again on 2026-09-17.
Run by hand against chiap08 today it reported NOT READY because
SKFLEET_GATEWAY_URL is unset for skfleet-rotate.service, on a host where
that role does not apply: chiap08 is a seat host, not a rotate host.

Measured against the real fleet (chiap08, chiap01, chiap02, chiap03) before
writing this fix: `systemctl --user is-enabled skfleet-rotate.service` and
`skfleet-rotate.timer` report the identical "static" / "disabled" pair on
all four hosts, so enablement state alone cannot tell a rotate host from a
seat host. The signal that actually differs is runtime activity:
skfleet-rotate.timer's ActiveState is "active" on chiap01/02/03 and
"inactive" on chiap08 (a oneshot .service itself is "inactive" between
runs on every host, active timer or not, so the timer -- not the service --
is what has to be asked). `unit_in_scope` below checks a unit's paired
timer's ActiveState and UnitFileState (systemd's own "enabled" vocabulary,
read via `show` rather than parsed from `is-enabled`'s exit code, matching
this module's existing `_systemctl_show_value` pattern) and treats either
"active" or "enabled"/"enabled-runtime" as in scope.

These tests never touch a real systemd instance: `subprocess.run` is
monkeypatched so systemctl calls return fixed fixture output, while a real
subprocess still runs for the module-import check (against a real stdlib
module), so the import-check half of the gate stays genuinely exercised.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "fleet"))

import skfleet_readiness  # noqa: E402
from skfleet_readiness import _run, unit_in_scope, write_verdict  # noqa: E402


def _fake_systemctl(responses):
    """Return a subprocess.run replacement that answers `systemctl --user show
    <unit> -p <prop> --value` from `responses[(unit, prop)]`, and otherwise
    (the module-import check, `python -c "..."`) calls the real subprocess.run.
    """
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "show"]:
            unit = cmd[3]
            prop = cmd[cmd.index("-p") + 1]
            value = responses.get((unit, prop), "")
            return subprocess.CompletedProcess(cmd, 0, stdout=value + "\n", stderr="")
        return real_run(cmd, **kwargs)

    return fake_run


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def _dispatcher(tmp_path: Path) -> Path:
    return _write(
        tmp_path / "fake-dispatcher.py",
        'GATEWAY = _required_lane_target("SKFLEET_GATEWAY_URL")\n',
    )


def _units_dir_with_real_module(tmp_path: Path) -> Path:
    """A units dir with one unit whose ExecStart names a real stdlib module,
    so the import-check half of the gate genuinely passes without mocking.
    """
    units_dir = tmp_path / "units"
    units_dir.mkdir()
    _write(
        units_dir / "skfleet-rotate.service",
        "[Service]\nExecStart=/usr/bin/python3 -m json\n",
    )
    return units_dir


def _units_dir_with_missing_module(tmp_path: Path) -> Path:
    units_dir = tmp_path / "units"
    units_dir.mkdir()
    _write(
        units_dir / "skfleet-rotate.service",
        "[Service]\nExecStart=/usr/bin/python3 -m skcapstone.nonexistent_entrypoint_nope\n",
    )
    return units_dir


# ---------------------------------------------------------------------------
# unit_in_scope
# ---------------------------------------------------------------------------


def test_unit_in_scope_false_when_timer_is_static_and_inactive(monkeypatch):
    """This is exactly chiap08's measured state for skfleet-rotate.timer."""
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "inactive",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
            }
        ),
    )
    in_scope, error, checked_unit = unit_in_scope("skfleet-rotate.service")
    assert error is None
    assert in_scope is False
    assert checked_unit == "skfleet-rotate.timer"


def test_unit_in_scope_true_when_timer_is_active(monkeypatch):
    """This is exactly chiap01/02/03's measured state for skfleet-rotate.timer:
    UnitFileState is "disabled" there too, but ActiveState is "active".
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "active",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
            }
        ),
    )
    in_scope, error, checked_unit = unit_in_scope("skfleet-rotate.service")
    assert error is None
    assert in_scope is True


def test_unit_in_scope_true_when_timer_is_boot_enabled_but_not_yet_active(monkeypatch):
    """A freshly enabled-at-boot host that hasn't ticked yet is still in scope."""
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "inactive",
                ("skfleet-rotate.timer", "UnitFileState"): "enabled",
            }
        ),
    )
    in_scope, error, _ = unit_in_scope("skfleet-rotate.service")
    assert error is None
    assert in_scope is True


def test_unit_in_scope_undetermined_when_unit_unknown(monkeypatch):
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl({("skfleet-rotate.timer", "LoadState"): "not-found"}),
    )
    in_scope, error, _ = unit_in_scope("skfleet-rotate.service")
    assert in_scope is None
    assert error is not None


# ---------------------------------------------------------------------------
# _run: enabled-aware scoping end to end
# ---------------------------------------------------------------------------


def test_run_skips_env_assertion_and_stays_ready_when_unit_out_of_scope(tmp_path, monkeypatch):
    """The chiap08 case: rotate's timer is not running here, so its missing
    SKFLEET_GATEWAY_URL must not fail the gate, and must be reported skipped,
    not silently passed.
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "inactive",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
            }
        ),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )

    assert exit_code == 0


def test_run_skip_line_names_the_var_and_says_skip_not_ok(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "inactive",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
            }
        ),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)

    _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )

    out = capsys.readouterr().out
    assert "SKIP" in out
    assert "SKFLEET_GATEWAY_URL" in out
    # The distinction this fix exists to make: "not checked" is never
    # spelled the same way as "checked and fine".
    assert "OK required env: SKFLEET_GATEWAY_URL" not in out
    assert "FAIL required env: SKFLEET_GATEWAY_URL" not in out


def test_run_still_fails_missing_env_when_unit_is_in_scope(tmp_path, monkeypatch):
    """The chiap01/02/03 case: rotate's timer IS running here, so a genuinely
    missing mandatory var must still fail the gate.
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "active",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
                ("skfleet-rotate.service", "Environment"): "SKFLEET_TARGET=5",
            }
        ),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )

    assert exit_code == 1


def test_run_fails_when_a_units_module_does_not_import(tmp_path, monkeypatch):
    """The niobe bug class, unaffected by the scoping change: a unit naming a
    module that does not import still fails the gate regardless of scope.
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "inactive",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
            }
        ),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_missing_module(tmp_path)

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )

    assert exit_code == 1


# ---------------------------------------------------------------------------
# The gateway-origin gate
# ---------------------------------------------------------------------------


def _fake_systemctl_with_gateway(value):
    """Like _fake_systemctl, but the rotate service carries the gateway URL.

    The drop-in file that supplies it is the hand-edited one, so the value
    lives in the service's parsed Environment, not in the unit file.
    """
    env = f"SKFLEET_TARGET=5 {value}" if value else "SKFLEET_TARGET=5"
    value_map = {
        ("skfleet-rotate.timer", "LoadState"): "loaded",
        ("skfleet-rotate.timer", "ActiveState"): "active",
        ("skfleet-rotate.timer", "UnitFileState"): "disabled",
        ("skfleet-rotate.service", "Environment"): env,
    }
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["systemctl", "--user", "show"]:
            unit = cmd[3]
            prop = cmd[cmd.index("-p") + 1]
            value = value_map.get((unit, prop), "")
            return subprocess.CompletedProcess(cmd, 0, stdout=value + "\n", stderr="")
        return real_run(cmd, **kwargs)

    return fake_run


def test_gateway_url_with_path_fails_readiness(tmp_path, monkeypatch, capsys):
    """AC4: a URL carrying a path must fail the gate with a line naming the
    variable, not just a raw exit code.
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl_with_gateway("SKFLEET_GATEWAY_URL=http://chiap01:18790/v1"),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "FAIL required env: SKFLEET_GATEWAY_URL" in out
    assert "/v1" in out


def test_gateway_url_bare_origin_passes_readiness(tmp_path, monkeypatch, capsys):
    """AC4: the same URL with the path stripped must pass the gate.
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl_with_gateway("SKFLEET_GATEWAY_URL=http://chiap01:18790"),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )
    out = capsys.readouterr().out

    assert exit_code == 0
    assert "OK required env: SKFLEET_GATEWAY_URL" in out


def test_gateway_url_with_query_or_fragment_fails_readiness(tmp_path, monkeypatch):
    """AC2: a query or a fragment, like a path, disqualifies the value.
    """
    for bad in ("http://chiap01:18790?a=1", "http://chiap01:18790#frag"):
        monkeypatch.setattr(
            skfleet_readiness.subprocess,
            "run",
            _fake_systemctl_with_gateway(f"SKFLEET_GATEWAY_URL={bad}"),
        )
        rotate_script = _dispatcher(tmp_path)
        units_dir = _units_dir_with_real_module(tmp_path)

        exit_code = _run(
            rotate_script,
            units_dir,
            sys.executable,
            env_from_unit=None,
            env_from_systemd="skfleet-rotate.service",
        )
        assert exit_code == 1


def test_gateway_url_missing_still_fails_as_before(tmp_path, monkeypatch, capsys):
    """The pre-existing missing-var path must be unchanged: the gate still
    fails when the unit is in scope and the variable is simply absent.
    """
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl_with_gateway(None),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
    )
    out = capsys.readouterr().out

    assert exit_code == 1
    assert "FAIL required env: SKFLEET_GATEWAY_URL" in out


# ---------------------------------------------------------------------------
# The verdict file
# ---------------------------------------------------------------------------


def test_write_verdict_is_parseable_json_with_ready_and_lines(tmp_path):
    verdict_path = tmp_path / "status" / "node-test" / "readiness" / "verdict.json"

    write_verdict(verdict_path, ok=True, lines=["OK required env: SKFLEET_TARGET is set"])

    loaded = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert loaded["ready"] is True
    assert loaded["lines"] == ["OK required env: SKFLEET_TARGET is set"]
    assert "checked_at" in loaded


def test_write_verdict_creates_parent_directories(tmp_path):
    verdict_path = tmp_path / "does" / "not" / "exist" / "yet" / "verdict.json"

    write_verdict(verdict_path, ok=False, lines=["FAIL something"])

    assert verdict_path.exists()


def test_run_writes_the_verdict_when_a_verdict_path_is_given(tmp_path, monkeypatch):
    monkeypatch.setattr(
        skfleet_readiness.subprocess,
        "run",
        _fake_systemctl(
            {
                ("skfleet-rotate.timer", "LoadState"): "loaded",
                ("skfleet-rotate.timer", "ActiveState"): "inactive",
                ("skfleet-rotate.timer", "UnitFileState"): "disabled",
            }
        ),
    )
    rotate_script = _dispatcher(tmp_path)
    units_dir = _units_dir_with_real_module(tmp_path)
    verdict_path = tmp_path / "verdict.json"

    exit_code = _run(
        rotate_script,
        units_dir,
        sys.executable,
        env_from_unit=None,
        env_from_systemd="skfleet-rotate.service",
        verdict_path=verdict_path,
    )

    assert exit_code == 0
    loaded = json.loads(verdict_path.read_text(encoding="utf-8"))
    assert loaded["ready"] is True
    assert any("SKIP" in line for line in loaded["lines"])
