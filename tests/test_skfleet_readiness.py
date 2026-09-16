import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from skfleet_readiness import (
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
    src = (Path(__file__).resolve().parents[1]
           / "scripts" / "fleet" / "skfleet-rotate.py").read_text(encoding="utf-8")
    names = required_env(src)
    assert "SKFLEET_GATEWAY_URL" in names
    assert "SKFLEET_TARGET" in names
    assert "SKFLEET_QWEN_TARGET" not in names   # has a default


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
