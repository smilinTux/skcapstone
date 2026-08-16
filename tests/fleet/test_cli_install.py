"""The `skfleet install` CLI verb (task 8, epic 2026-08-16-skfleet-install).

Wires installer.run_install through a real CLI surface: --role/--check/
--apply/--dry-run/--enable/--start/--only/--json. installer.run_install is
stubbed in every test here (its own behavior is covered by
test_installer_*.py); this file only proves the CLI wiring: flag parsing,
role resolution, JSON/table rendering, and exit codes.
"""

from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.fleet import store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.installer import ActuationNotAllowed, Frozen, ProfileNotApplied


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


# --------------------------------------------------------------- shape ---


def test_cli_install_check_json_exit_and_shape(paths, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.fleet.cli.installer.run_install",
        lambda *a, **k: {"role": "control", "mode": "check", "results": [], "ok": True},
    )
    result = CliRunner().invoke(
        fleet, ["install", "--role", "control", "--check", "--json"], env=_env(paths)
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["mode"] == "check"
    assert payload["role"] == "control"


def test_check_is_the_default_mode(paths, monkeypatch) -> None:
    captured = {}

    def fake_run_install(paths_, role, *, mode, **kw):
        captured["mode"] = mode
        return {"role": role, "mode": mode, "results": [], "ok": True}

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", fake_run_install)
    result = CliRunner().invoke(fleet, ["install", "--role", "control", "--json"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "check"


def test_apply_flag_selects_apply_mode(paths, monkeypatch) -> None:
    captured = {}

    def fake_run_install(paths_, role, *, mode, **kw):
        captured["mode"] = mode
        return {"role": role, "mode": mode, "results": [], "ok": True}

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", fake_run_install)
    result = CliRunner().invoke(
        fleet, ["install", "--role", "control", "--apply", "--json"], env=_env(paths)
    )
    assert result.exit_code == 0, result.output
    assert captured["mode"] == "apply"


def test_flags_pass_through_to_run_install(paths, monkeypatch) -> None:
    captured = {}

    def fake_run_install(paths_, role, **kw):
        captured.update(kw)
        return {"role": role, "mode": kw["mode"], "results": [], "ok": True}

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", fake_run_install)
    result = CliRunner().invoke(
        fleet,
        [
            "install",
            "--role",
            "control",
            "--apply",
            "--dry-run",
            "--enable",
            "--start",
            "--only",
            "skcapstone",
            "--only",
            "sknoded.service",
            "--json",
        ],
        env=_env(paths),
    )
    assert result.exit_code == 0, result.output
    assert captured["dry_run"] is True
    assert captured["enable"] is True
    assert captured["start"] is True
    assert captured["only"] == ["skcapstone", "sknoded.service"]
    assert captured["backends"]  # default_backends() registry, non-empty


def test_only_defaults_to_none_when_omitted(paths, monkeypatch) -> None:
    captured = {}

    def fake_run_install(paths_, role, **kw):
        captured.update(kw)
        return {"role": role, "mode": kw["mode"], "results": [], "ok": True}

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", fake_run_install)
    result = CliRunner().invoke(fleet, ["install", "--role", "control", "--json"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert captured["only"] is None


# ------------------------------------------------------------- exit code ---


def test_exit_code_is_zero_when_ok(paths, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.fleet.cli.installer.run_install",
        lambda *a, **k: {"role": "control", "mode": "check", "results": [], "ok": True},
    )
    result = CliRunner().invoke(fleet, ["install", "--role", "control"], env=_env(paths))
    assert result.exit_code == 0, result.output


def test_exit_code_is_one_when_not_ok(paths, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.fleet.cli.installer.run_install",
        lambda *a, **k: {
            "role": "control",
            "mode": "check",
            "results": [{"grade": "error", "category": "missing_required_units", "name": "x"}],
            "ok": False,
        },
    )
    result = CliRunner().invoke(fleet, ["install", "--role", "control"], env=_env(paths))
    assert result.exit_code == 1, result.output


# ------------------------------------------------------------ table view ---


def test_human_table_shows_check_findings(paths, monkeypatch) -> None:
    monkeypatch.setattr(
        "skcapstone.fleet.cli.installer.run_install",
        lambda *a, **k: {
            "role": "control",
            "mode": "check",
            "results": [
                {
                    "grade": "error",
                    "category": "missing_required_units",
                    "name": "skcapstone.service",
                }
            ],
            "ok": False,
        },
    )
    result = CliRunner().invoke(fleet, ["install", "--role", "control"], env=_env(paths))
    assert "skcapstone.service" in result.output
    assert "control" in result.output


# --------------------------------------------------------- role resolution ---


def test_role_resolves_from_node_spec_when_omitted(paths, operator, monkeypatch) -> None:
    store.write_spec(paths, "node", "node-cli", {"role": "worker-gpu"}, writer=operator)
    captured = {}

    def fake_run_install(paths_, role, **kw):
        captured["role"] = role
        return {"role": role, "mode": kw["mode"], "results": [], "ok": True}

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", fake_run_install)
    result = CliRunner().invoke(fleet, ["install", "--json"], env=_env(paths))
    assert result.exit_code == 0, result.output
    assert captured["role"] == "worker-gpu"


def test_missing_role_with_no_bound_role_is_a_clear_error(paths, operator) -> None:
    store.write_spec(paths, "node", "node-cli", {"cordoned": False}, writer=operator)
    result = CliRunner().invoke(fleet, ["install"], env=_env(paths))
    assert result.exit_code != 0
    assert "no spec.role set" in result.output


def test_missing_role_with_no_node_object_is_a_clear_error(paths) -> None:
    result = CliRunner().invoke(fleet, ["install"], env=_env(paths))
    assert result.exit_code != 0
    assert "no such node object" in result.output or "no spec.role set" in result.output


# ------------------------------------------------------- gate exceptions ---


def test_profile_not_applied_is_a_clean_error_not_a_traceback(paths, monkeypatch) -> None:
    def raiser(*a, **k):
        raise ProfileNotApplied("control")

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", raiser)
    result = CliRunner().invoke(fleet, ["install", "--role", "control"], env=_env(paths))
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "control" in result.output


def test_frozen_is_a_clean_error_not_a_traceback(paths, monkeypatch) -> None:
    def raiser(*a, **k):
        raise Frozen("control")

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", raiser)
    result = CliRunner().invoke(
        fleet, ["install", "--role", "control", "--apply"], env=_env(paths)
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "frozen" in result.output.lower() or "FROZEN" in result.output


def test_actuation_not_allowed_is_a_clean_error_not_a_traceback(paths, monkeypatch) -> None:
    def raiser(*a, **k):
        raise ActuationNotAllowed("control")

    monkeypatch.setattr("skcapstone.fleet.cli.installer.run_install", raiser)
    result = CliRunner().invoke(
        fleet, ["install", "--role", "control", "--apply"], env=_env(paths)
    )
    assert result.exit_code != 0
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert "actuation" in result.output.lower()
