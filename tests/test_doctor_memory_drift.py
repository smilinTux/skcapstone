"""Tests for the fleet-wide memory-drift and sync-timer doctor checks.

Covers ``doctor._every_agent_with_memory_home``, ``doctor._skmemory_health_json``,
``doctor._check_memory_drift``, and ``doctor._check_memory_sync_timer``.

Card: skmemory sqlite-only-orphans, 2026-10-16. A laptop's lumina agent home
carried 23,525 sqlite-only memories, undetected for six months, because
nothing in the estate's gating looked at skmemory's own drift signal
fleet-wide, and the skmemory-sync@<agent>.timer that would have exported
them was never actually installed. These checks close both gaps for
`skcapstone doctor`.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from skcapstone.doctor import (
    _check_memory_drift,
    _check_memory_sync_timer,
    _every_agent_with_memory_home,
    _skmemory_health_json,
)


def _by_name(checks):
    return {c.name: c for c in checks}


def _make_agents(tmp_path, names_with_memory, names_without_memory=(), register=True):
    """Build agent homes; by default every memory-bearing one is REGISTERED.

    Since 2026-10-27 the drift and timer checks cover the active agent plus
    the registered memory profiles, not every folder (see
    tests/test_doctor_agent_scope.py), so a fixture agent has to be
    registered to be in scope.
    """
    import json

    agents_dir = tmp_path / "agents"
    for name in names_with_memory:
        (agents_dir / name / "memory").mkdir(parents=True)
    for name in names_without_memory:
        (agents_dir / name).mkdir(parents=True)
    if register:
        (tmp_path / "config").mkdir(exist_ok=True)
        (tmp_path / "config" / "profile-registry.json").write_text(
            json.dumps(
                {
                    "profiles": [
                        {"profile_id": n, "memory_principal_id": f"memory:{n}"}
                        for n in names_with_memory
                    ]
                }
            )
        )
    return tmp_path


@pytest.fixture(autouse=True)
def _no_active_agent(monkeypatch):
    monkeypatch.delenv("SKAGENT", raising=False)
    # Fixture registries are lenient (no hashes). Treat every entry as
    # resolvable here; the strict resolver has its own tests below/elsewhere.
    from skcapstone import agent_scope as _scope

    monkeypatch.setattr(
        _scope,
        "resolvable_memory_profiles",
        lambda root: (_scope.registered_memory_profiles(root) or set(), {}),
    )


# ---------------------------------------------------------------------------
# _every_agent_with_memory_home
# ---------------------------------------------------------------------------


def test_discovers_only_agents_with_a_memory_dir(tmp_path):
    home = _make_agents(tmp_path, ["lumina", "opus"], ["template"])
    assert _every_agent_with_memory_home(home) == ["lumina", "opus"]


def test_no_agents_dir_returns_empty(tmp_path):
    assert _every_agent_with_memory_home(tmp_path) == []


def test_accepts_single_agent_home_shape(tmp_path):
    """home can be ~/.skcapstone/agents/<agent> itself, not just the shared
    root (mirrors _resolve_memory_dir's two accepted shapes)."""
    agents_dir = tmp_path / "agents"
    (agents_dir / "lumina" / "memory").mkdir(parents=True)
    (agents_dir / "opus" / "memory").mkdir(parents=True)
    single_home = agents_dir / "lumina"
    assert _every_agent_with_memory_home(single_home) == ["lumina", "opus"]


# ---------------------------------------------------------------------------
# _skmemory_health_json
# ---------------------------------------------------------------------------


def test_health_json_none_when_cli_missing():
    with patch("skcapstone.doctor.shutil.which", return_value=None):
        assert _skmemory_health_json("lumina") is None


def test_health_json_parses_stdout():
    class _Proc:
        stdout = '{"status": "ok"}'

    with (
        patch("skcapstone.doctor.shutil.which", return_value="/usr/bin/skmemory"),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()),
    ):
        assert _skmemory_health_json("lumina") == {"status": "ok"}


def test_health_json_none_on_bad_json():
    class _Proc:
        stdout = "not json"

    with (
        patch("skcapstone.doctor.shutil.which", return_value="/usr/bin/skmemory"),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()),
    ):
        assert _skmemory_health_json("lumina") is None


def test_health_json_none_on_timeout():
    import subprocess

    with (
        patch("skcapstone.doctor.shutil.which", return_value="/usr/bin/skmemory"),
        patch(
            "skcapstone.doctor.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="skmemory", timeout=30),
        ),
    ):
        assert _skmemory_health_json("lumina") is None


# ---------------------------------------------------------------------------
# _check_memory_drift
# ---------------------------------------------------------------------------


def test_drift_no_agents_is_empty(tmp_path):
    assert _check_memory_drift(tmp_path) == []


def test_drift_unknown_when_health_unavailable(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])
    with patch("skcapstone.doctor._skmemory_health_json", return_value=None):
        checks = _check_memory_drift(home)
    check = _by_name(checks)["memory:drift:lumina"]
    assert check.unknown is True
    assert check.passed is True  # unknown counts toward neither pass nor fail


def test_drift_clean_passes(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])
    healthy = {
        "status": "ok",
        "primary": {"ok": True, "sync": {"sqlite_only": 0, "flat_only": 0, "in_sync": True}},
    }
    with patch("skcapstone.doctor._skmemory_health_json", return_value=healthy):
        checks = _check_memory_drift(home)
    check = _by_name(checks)["memory:drift:lumina"]
    assert check.passed is True
    assert check.category == "memory"


def test_drift_sqlite_only_fails(tmp_path):
    """The exact shape of the 2026-10-16 incident: sqlite_only > 0."""
    home = _make_agents(tmp_path, ["lumina"])
    drifted = {
        "status": "degraded",
        "primary": {"ok": True, "sync": {"sqlite_only": 23525, "flat_only": 0, "in_sync": False}},
    }
    with patch("skcapstone.doctor._skmemory_health_json", return_value=drifted):
        checks = _check_memory_drift(home)
    check = _by_name(checks)["memory:drift:lumina"]
    assert check.passed is False
    assert "sqlite_only=23525" in check.detail
    assert "skmemory sync" in check.fix


def test_drift_pg_only_fails(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])
    red = {
        "status": "red",
        "primary": {"ok": True, "sync": {"sqlite_only": 0, "flat_only": 0, "in_sync": True}},
        "vector": {"pg_only": 5},
    }
    with patch("skcapstone.doctor._skmemory_health_json", return_value=red):
        checks = _check_memory_drift(home)
    check = _by_name(checks)["memory:drift:lumina"]
    assert check.passed is False
    assert "pg_only=5" in check.detail


def test_drift_checks_every_agent_independently(tmp_path):
    home = _make_agents(tmp_path, ["lumina", "opus"])
    healthy_sync = {"sqlite_only": 0, "flat_only": 0}
    drifted_sync = {"sqlite_only": 7, "flat_only": 0}
    healthy = {"status": "ok", "primary": {"ok": True, "sync": healthy_sync}}
    drifted = {"status": "degraded", "primary": {"ok": True, "sync": drifted_sync}}

    def fake_health(agent, timeout=None):
        return healthy if agent == "opus" else drifted

    with patch("skcapstone.doctor._skmemory_health_json", side_effect=fake_health):
        checks = _check_memory_drift(home)
    by_name = _by_name(checks)
    assert by_name["memory:drift:lumina"].passed is False
    assert by_name["memory:drift:opus"].passed is True


# ---------------------------------------------------------------------------
# _check_memory_sync_timer
# ---------------------------------------------------------------------------


def test_timer_check_degrades_non_linux(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])
    with patch("skcapstone.doctor.platform.system", return_value="Darwin"):
        assert _check_memory_sync_timer(home) == []


def test_timer_check_degrades_no_systemd(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=False),
    ):
        assert _check_memory_sync_timer(home) == []


def test_timer_enabled_passes(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])

    class _Proc:
        stdout = "enabled\n"

    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()),
    ):
        checks = _check_memory_sync_timer(home)
    check = _by_name(checks)["memory:sync_timer:lumina"]
    assert check.passed is True
    assert check.category == "systemd"


def test_timer_disabled_fails_with_fix(tmp_path):
    """The exact gap that caused the 2026-10-16 incident: the unit shipped
    in-repo since v0.9.8 but nothing ever enabled it."""
    home = _make_agents(tmp_path, ["lumina"])

    class _Proc:
        stdout = "disabled\n"

    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()),
    ):
        checks = _check_memory_sync_timer(home)
    check = _by_name(checks)["memory:sync_timer:lumina"]
    assert check.passed is False
    assert "skmemory-sync@lumina.timer" in check.fix


def test_timer_query_failure_is_unknown_not_failed(tmp_path):
    home = _make_agents(tmp_path, ["lumina"])
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.doctor.subprocess.run", side_effect=OSError("no systemctl")),
    ):
        checks = _check_memory_sync_timer(home)
    check = _by_name(checks)["memory:sync_timer:lumina"]
    assert check.unknown is True
    assert check.passed is True


def test_timer_check_covers_every_agent(tmp_path):
    home = _make_agents(tmp_path, ["lumina", "opus"])

    class _Proc:
        stdout = "enabled\n"

    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.doctor.subprocess.run", return_value=_Proc()),
    ):
        checks = _check_memory_sync_timer(home)
    names = {c.name for c in checks}
    assert "memory:sync_timer:lumina" in names
    assert "memory:sync_timer:opus" in names
