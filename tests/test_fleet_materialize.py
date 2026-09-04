import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from skcapstone.fleet.materialize import (
    UnitRenderError,
    render_service_unit,
    render_timer_unit,
)


def test_render_service_unit_is_oneshot_with_verbatim_execstart():
    spec = {
        "unit": "skfleet-rotate.service",
        "runtime": "systemd-user",
        "exec": "%h/.skenv/bin/python3 %h/.local/bin/skfleet-rotate.py --go",
        "type": "oneshot",
        "environment": {"SKFLEET_TARGET": "3"},
        "description": "SKWorld fleet rotation",
    }
    text = render_service_unit("skfleet-rotate", spec)
    # Specifiers are systemd's to expand at start time, not ours at write time.
    assert "%h/.skenv/bin/python3 %h/.local/bin/skfleet-rotate.py --go" in text
    assert "Type=oneshot" in text
    assert "Environment=SKFLEET_TARGET=3" in text
    assert "Description=SKWorld fleet rotation" in text
    # A rendered base unit must never carry a bare reset; that is drop-in grammar.
    assert "\nExecStart=\n" not in text


def test_render_service_unit_refuses_missing_exec():
    with pytest.raises(UnitRenderError):
        render_service_unit("x", {"unit": "x.service", "runtime": "systemd-user"})


def test_render_service_unit_refuses_non_systemd_runtime():
    spec = {"unit": "x", "runtime": "docker", "exec": "/bin/true"}
    with pytest.raises(UnitRenderError):
        render_service_unit("x", spec)


def test_render_timer_unit_maps_interval_schedule():
    # Exact text, not substrings. OnUnitActiveSec alone never fires for a
    # service that has never run, so an interval MUST render both triggers.
    assert render_timer_unit("skfleet-rotate", "5m") == (
        "# Managed by skcapstone fleet materialize. Edits are overwritten.\n"
        "# Fleet object: service/skfleet-rotate\n"
        "\n"
        "[Unit]\n"
        "Description=Run skfleet-rotate\n"
        "\n"
        "[Timer]\n"
        "OnActiveSec=5m\n"
        "OnUnitActiveSec=5m\n"
        "Persistent=true\n"
        "Unit=skfleet-rotate.service\n"
        "\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )


def test_render_timer_unit_maps_named_schedule():
    # Named schedules are OnCalendar only; the first-fire seed is an
    # interval-schedule concern and must not leak into calendar timers.
    text = render_timer_unit("j", "@hourly")
    timer_section = text.split("[Timer]\n", 1)[1].split("\n\n", 1)[0]
    assert timer_section == "OnCalendar=hourly\nPersistent=true\nUnit=j.service"


def test_render_timer_unit_refuses_bad_schedule():
    with pytest.raises(UnitRenderError):
        render_timer_unit("j", "every-so-often")


def _user_systemd_available() -> bool:
    """True when a systemd user manager answers on this session's bus."""
    if shutil.which("systemctl") is None:
        return False
    probe = subprocess.run(
        ["systemctl", "--user", "show", "--property=Version"],
        capture_output=True,
        text=True,
    )
    return probe.returncode == 0 and probe.stdout.startswith("Version=")


def _timer_property(unit: str, prop: str) -> str:
    out = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", prop, "--value"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return out.strip()


@pytest.mark.parametrize(
    ("schedule", "prop"),
    [("1m", "NextElapseUSecMonotonic"), ("@hourly", "NextElapseUSecRealtime")],
)
def test_rendered_timer_is_scheduled_by_real_systemd(schedule, prop):
    """Assert systemd's verdict on the rendered unit, not the unit text.

    The interval schedule used to render ``OnUnitActiveSec=`` alone. The text
    was exactly what was intended, and six tests asserting that text passed,
    yet the timer never fired: OnUnitActiveSec counts from the service's last
    activation, a never-run service has none, and systemd armed the timer
    with ``NextElapseUSecMonotonic=infinity`` while reporting enabled, active
    and Result=success. A text assertion structurally cannot see that; it
    checks what we wrote, not what systemd does with it. This test loads the
    rendered pair into the real user manager, starts the timer, and asks
    systemd when it will next elapse. Six text tests missed the bug; this one
    catches it. Skips when there is no user systemd (CI without a session
    bus).
    """
    if not _user_systemd_available():
        pytest.skip("no systemd user manager on this session")
    name = f"skfleet-test-{uuid.uuid4().hex[:12]}"
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = unit_dir / f"{name}.service"
    timer = unit_dir / f"{name}.timer"
    spec = {"runtime": "systemd-user", "exec": "/bin/true", "type": "oneshot"}
    try:
        service.write_text(render_service_unit(name, spec), encoding="utf-8")
        timer.write_text(render_timer_unit(name, schedule), encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
        subprocess.run(["systemctl", "--user", "start", timer.name], check=True)
        assert _timer_property(timer.name, "ActiveState") == "active"
        next_elapse = _timer_property(timer.name, prop)
        assert next_elapse not in (
            "",
            "infinity",
        ), f"{timer.name} is active but will never fire: {prop}={next_elapse!r}"
    finally:
        subprocess.run(["systemctl", "--user", "stop", timer.name], capture_output=True)
        for path in (timer, service):
            path.unlink(missing_ok=True)
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
