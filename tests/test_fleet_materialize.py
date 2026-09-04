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
    text = render_timer_unit("skfleet-rotate", "5m")
    assert "OnUnitActiveSec=5m" in text
    assert "Unit=skfleet-rotate.service" in text
    assert "WantedBy=timers.target" in text


def test_render_timer_unit_maps_named_schedule():
    assert "OnCalendar=hourly" in render_timer_unit("j", "@hourly")


def test_render_timer_unit_refuses_bad_schedule():
    with pytest.raises(UnitRenderError):
        render_timer_unit("j", "every-so-often")
