import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from skfleet_readiness import (
    unit_python,
    required_env,
    unit_modules,
    check_module_imports,
    parse_systemd_environment,
)


def test_required_env_includes_a_var_with_no_default():
    src = 'TARGET=_required_lane_target("SKFLEET_TARGET")'
    assert "SKFLEET_TARGET" in required_env(src)


def test_required_env_excludes_a_var_with_a_default():
    src = 'Q=_required_lane_target("SKFLEET_QWEN_TARGET", default="6")'
    assert "SKFLEET_QWEN_TARGET" not in required_env(src)


def test_required_env_handles_a_call_split_across_lines():
    """CODEX_PHYSICAL_LIMIT is written this way in the real dispatcher."""
    src = 'C=_required_lane_target(\n    "SKFLEET_CODEX_PHYSICAL_LIMIT",\n    default="4",\n)'
    assert "SKFLEET_CODEX_PHYSICAL_LIMIT" not in required_env(src)


def test_required_env_picks_up_raise_systemexit_form():
    src = 'raise SystemExit("SKFLEET_GATEWAY_URL is required")'
    assert "SKFLEET_GATEWAY_URL" in required_env(src)


def test_unit_modules_extracts_the_module_path():
    unit = "ExecStart=%h/.skenv/bin/python3 -m skcapstone.niobe_live_entrypoint --home %h"
    assert unit_modules(unit) == ["skcapstone.niobe_live_entrypoint"]


def test_unit_modules_returns_empty_for_a_unit_with_no_module():
    assert unit_modules("ExecStart=/bin/echo hello") == []


def test_unit_modules_deduplicates():
    unit = "ExecStart=python3 -m a.b\nExecStart=python3 -m a.b"
    assert unit_modules(unit) == ["a.b"]


def test_check_module_imports_reports_a_real_module_true(tmp_path):
    import sys

    result = check_module_imports(["json"], sys.executable)
    assert result == {"json": True}


def test_check_module_imports_reports_a_missing_module_false(tmp_path):
    """This is the niobe bug class: a unit naming a module that is not installed."""
    import sys

    result = check_module_imports(["skcapstone.seat_shadow_entrypoint_nope"], sys.executable)
    assert result == {"skcapstone.seat_shadow_entrypoint_nope": False}


def test_required_env_on_the_real_dispatcher_finds_the_var_that_broke_the_deploy():
    """Regression: SKFLEET_GATEWAY_URL is exactly what took chiap01 down."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
    ).read_text(encoding="utf-8")
    names = required_env(src)
    assert "SKFLEET_GATEWAY_URL" in names
    assert "SKFLEET_TARGET" in names
    assert "SKFLEET_QWEN_TARGET" not in names  # has a default


def test_parse_systemd_environment_normal_multi_var_line():
    raw = "SKFLEET_TARGET=3 SKFLEET_GLM_TARGET=2 SKFLEET_GATEWAY_URL=http://gateway.local:8080"
    result = parse_systemd_environment(raw)
    assert result == {
        "SKFLEET_TARGET": "3",
        "SKFLEET_GLM_TARGET": "2",
        "SKFLEET_GATEWAY_URL": "http://gateway.local:8080",
    }


def test_parse_systemd_environment_empty_result():
    assert parse_systemd_environment("") == {}
    assert parse_systemd_environment("   \n") == {}


def test_parse_systemd_environment_value_containing_equals_splits_on_first_only():
    raw = "SKFLEET_GATEWAY_URL=http://gateway.local:8080/v1?token=abc=def"
    result = parse_systemd_environment(raw)
    assert result == {"SKFLEET_GATEWAY_URL": "http://gateway.local:8080/v1?token=abc=def"}


def test_unit_python_reads_the_interpreter_the_unit_declares():
    """A unit is entitled to its own virtualenv, declared in ExecStart.

    Regression: readiness checked EVERY unit's imports against one
    interpreter (--python-bin). On chiap04, hermes-gateway.service runs
    /home/skuser01/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main
    and had been active since 2026-08-27, yet readiness reported "module
    hermes_cli.main does not import" because it tested ~/.skenv/bin/python3.
    Under the unit's own interpreter the module imports fine. That false
    failure blocked the deploy gate for the entire host, and the gate is
    what a staged rollout consults before proceeding.
    """
    unit = (
        "[Service]\n"
        "ExecStart=/home/skuser01/.hermes/hermes-agent/venv/bin/python "
        "-m hermes_cli.main gateway run --replace\n"
    )
    assert unit_python(unit) == "/home/skuser01/.hermes/hermes-agent/venv/bin/python"


def test_unit_python_returns_none_when_no_absolute_interpreter_is_named():
    """Then the caller falls back to the interpreter it was given, which is
    the old behaviour and stays correct for units that do not declare one."""
    assert unit_python("[Service]\nExecStart=/usr/bin/true\n") is None
    assert unit_python("[Service]\nExecStart=skcapstone fleet sknoded --once\n") is None
    assert unit_python("[Unit]\nDescription=no ExecStart at all\n") is None


def test_unit_python_strips_systemd_exec_prefixes():
    """systemd allows -, @, +, ! and : prefixes on ExecStart; they are not
    part of the executable path."""
    for prefix in ("-", "@", "+", "!", "-@"):
        unit = f"[Service]\nExecStart={prefix}/opt/venv/bin/python3 -m pkg.mod\n"
        assert unit_python(unit) == "/opt/venv/bin/python3", prefix


def test_unit_python_ignores_a_non_python_executable():
    """Only an interpreter is a useful answer here; a unit that runs a binary
    has no module to import under it."""
    assert unit_python("[Service]\nExecStart=/usr/bin/node server.js\n") is None
    assert unit_python("[Service]\nExecStart=/usr/local/bin/skfleet-rotate.py --go\n") is None
