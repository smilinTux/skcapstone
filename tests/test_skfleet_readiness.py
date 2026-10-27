import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "fleet"))

from skfleet_readiness import (
    check_module_imports,
    dispatcher_imports,
    parse_systemd_environment,
    required_env,
    unit_modules,
    unit_python,
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


# ── the dispatcher SCRIPT, not just its env ──────────────────────────────────
#
# unit_modules() only recognises `python -m <module>` units, so
# skfleet-rotate.service, whose ExecStart names a script path, had no import
# check at all. Measured 2026-09-21: chiap02 crashed at import every cycle for
# hours (capauth 0.3.1 against the fleet 0.3.9+) while this gate reported only
# that its env vars were set, and 11 cards sat owned by that host.


def test_dispatcher_imports_collects_plain_and_from_imports():
    source = "import os\nimport json\nfrom skcapstone.card_store import CardStore\n"
    assert dispatcher_imports(source) == ["os", "json", "skcapstone.card_store"]


def test_dispatcher_imports_keeps_dotted_paths_and_ignores_aliases():
    source = "import importlib.util\nimport numpy as np\n"
    assert dispatcher_imports(source) == ["importlib.util", "numpy"]


def test_dispatcher_imports_collapses_duplicates_in_first_seen_order():
    assert dispatcher_imports("import os\nimport json\nimport os\n") == ["os", "json"]


def test_dispatcher_imports_ignores_imports_inside_a_function():
    """A guarded import is not startup-critical and is frequently deliberate."""
    source = "import os\n\n\ndef helper():\n    import pytest\n    return pytest\n"
    assert dispatcher_imports(source) == ["os"]


def test_dispatcher_imports_skips_relative_imports():
    """The dispatcher is a script, not a package: a relative import cannot be
    resolved standalone and must not be reported as a failure."""
    source = "from . import sibling\nfrom .pkg import thing\nimport os\n"
    assert dispatcher_imports(source) == ["os"]


def test_dispatcher_imports_returns_nothing_for_unparseable_source():
    assert dispatcher_imports("def (((:\n") == []


def test_dispatcher_imports_covers_the_real_rotate_script():
    """Guards the wiring: the check must reach the real dispatcher."""
    rotate = Path(__file__).resolve().parents[1] / "scripts" / "fleet" / "skfleet-rotate.py"
    modules = dispatcher_imports(rotate.read_text(encoding="utf-8"))
    assert (
        "skcapstone.card_store" in modules
    ), "skcapstone.card_store is the exact import that was failing on chiap02"


# ── unit ExecStart resolved the way systemd runs it (2026-10-27) ─────────────
#
# The chi auto-rollout halted at chiap01 every cycle from 2026-10-25: the gate
# import-checked sklipsync.service (%h/...venv/bin/python, own WorkingDirectory)
# and skcall-join@.service (%h/.skenv-skcall) under ~/.skenv instead.

from skfleet_readiness import _run, expand_specifiers, unit_exec_targets  # noqa: E402

HOME = "/home/fixture"


def test_percent_h_interpreter_is_the_units_own_venv():
    unit = (
        "[Service]\nWorkingDirectory=%h/src/lipsync\n"
        "ExecStart=%h/bench/venv/bin/python -m sklipsync serve\n"
    )
    [t] = unit_exec_targets(unit, "sklipsync.service", HOME)
    assert t.module == "sklipsync"
    assert t.interpreter == HOME + "/bench/venv/bin/python"
    assert t.cwd == HOME + "/src/lipsync"
    assert t.unresolved is None
    assert unit_python(unit, "sklipsync.service", HOME) == HOME + "/bench/venv/bin/python"


def test_template_unit_resolves_interpreter_despite_instance_args():
    unit = (
        "[Service]\nWorkingDirectory=%h\nEnvironmentFile=-%t/skcall/join-%i.env\n"
        "ExecStart=%h/.skenv-skcall/bin/python -m skcall.cli join --room %i --wait 600\n"
    )
    [t] = unit_exec_targets(unit, "skcall-join@.service", HOME)
    assert (t.module, t.interpreter, t.cwd, t.unresolved) == (
        "skcall.cli",
        HOME + "/.skenv-skcall/bin/python",
        HOME,
        None,
    )


def test_interpreter_needing_the_instance_is_skipped_not_failed():
    unit = "[Service]\nExecStart=/opt/%i/bin/python -m pkg\n"
    [t] = unit_exec_targets(unit, "x@.service", HOME)
    assert t.unresolved and "%i" in t.unresolved
    # An instantiated unit knows its instance.
    [t] = unit_exec_targets(unit, "x@blue.service", HOME)
    assert t.interpreter == "/opt/blue/bin/python" and t.unresolved is None


def test_env_var_interpreter_and_env_wrapper_and_shell_wrapper(tmp_path):
    envfile = tmp_path / "e.env"
    envfile.write_text("# c\nexport VENV=/srv/venv\nPYTHONPATH='/srv/lib'\n")
    unit = (
        '[Service]\nEnvironment="A=1" PATH=/usr/bin:/bin\n'
        "EnvironmentFile=%s\n" % envfile + "ExecStart=${VENV}/bin/python3 -u -X dev -m one.mod\n"
        "ExecStart=/usr/bin/env FOO=1 python3 -m two.mod\n"
        "ExecStart=/bin/bash -lc 'cd ~/app && exec ~/v/bin/python -m three.mod --x'\n"
        "ExecStart=/usr/bin/node -m not_python\n"
        "ExecStart=$UNDEFINED/bin/python -m four.mod\n"
    )
    t = {x.module: x for x in unit_exec_targets(unit, "w.service", HOME)}
    assert set(t) == {"one.mod", "two.mod", "three.mod", "four.mod"}
    assert t["one.mod"].interpreter == "/srv/venv/bin/python3"
    assert t["one.mod"].env == {"PYTHONPATH": "/srv/lib"}
    import shutil

    assert t["two.mod"].interpreter == shutil.which("python3", path="/usr/bin:/bin")
    assert t["three.mod"].interpreter == HOME + "/v/bin/python"
    assert t["three.mod"].cwd == HOME + "/app"
    assert t["four.mod"].unresolved and "$UNDEFINED" in t["four.mod"].unresolved


def test_bare_python_without_path_falls_back_to_gate_interpreter():
    [t] = unit_exec_targets("ExecStart=python3 -m a.b", "u.service", HOME)
    assert t.interpreter is None and t.unresolved is None


def test_expand_specifiers_reports_unknown():
    out, missing = expand_specifiers("%h/%i/%%/%Z", "a@.service", HOME)
    assert out.startswith(HOME + "/%i/%/") and missing == ["%i", "%Z"]


def _gate(tmp_path, unit_text, name="svc.service"):
    src = tmp_path / "dispatcher.py"
    src.write_text("import json\n")
    units = tmp_path / "units"
    units.mkdir(exist_ok=True)
    (units / name).write_text(unit_text)
    return _run(src, units, "/nonexistent/gate/python", None)


def test_gate_passes_a_module_only_importable_from_the_units_venv_and_cwd(tmp_path, capsys):
    """End to end: a package that exists only in the unit's WorkingDirectory,
    under the unit's own interpreter, while --python-bin does not even exist.
    Under the old gate this was the chiap01 halt."""
    pkgroot = tmp_path / "src"
    (pkgroot / "onlyhere_pkg").mkdir(parents=True)
    (pkgroot / "onlyhere_pkg" / "__init__.py").write_text("")
    # dispatcher imports json under --python-bin, which does not exist: that
    # FAILs, so judge only the unit line.
    _gate(
        tmp_path,
        "[Service]\nWorkingDirectory=%s\nExecStart=%s -m onlyhere_pkg\n"
        % (pkgroot, sys.executable),
    )
    out = capsys.readouterr().out
    assert (
        "OK unit svc.service: module onlyhere_pkg imports under %s (cwd %s)"
        % (sys.executable, pkgroot)
        in out
    )


def test_gate_still_fails_a_module_missing_from_the_units_venv(tmp_path, capsys):
    _gate(tmp_path, "[Service]\nExecStart=%s -m definitely_absent_pkg_xyz\n" % sys.executable)
    assert (
        "FAIL unit svc.service: module definitely_absent_pkg_xyz does not import"
        in capsys.readouterr().out
    )


def test_gate_skips_a_non_python_unit_and_an_unknowable_interpreter(tmp_path, capsys):
    _gate(tmp_path, "[Service]\nExecStart=/usr/bin/node server.js -m x\n", "node.service")
    _gate(tmp_path, "[Service]\nExecStart=/opt/%i/bin/python -m pkg\n", "t@.service")
    out = capsys.readouterr().out
    assert "node.service" not in out
    assert "SKIP unit t@.service: module pkg not checked" in out
