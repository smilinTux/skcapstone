"""Tests for the systemd-runtime and store-integrity doctor/preflight checks.

Covers ``doctor._check_systemd_runtime`` and ``doctor._check_store_integrity``
plus ``preflight.PreflightChecker.check_systemd``:

- systemd absent (non-Linux / no user manager) degrades gracefully, never FAILs
- healthy installed units + no failed units + no drift all pass
- a failed unit is reported
- a stale (drifted) installed unit is reported
- corrupt store files (card core, event log, gtd list, itil core) are reported
- absent stores pass (nothing to corrupt)
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from skcapstone.doctor import (
    _check_store_integrity,
    _check_systemd_runtime,
    _systemd_unit_files,
    run_diagnostics,
)
from skcapstone.preflight import PreflightChecker


def _by_name(checks):
    return {c.name: c for c in checks}


# ---------------------------------------------------------------------------
# _check_systemd_runtime: availability gate / graceful degrade
# ---------------------------------------------------------------------------


def test_systemd_absent_non_linux_degrades(tmp_path):
    """On a non-Linux host the family is a single informational pass."""
    with patch("skcapstone.doctor.platform.system", return_value="Darwin"):
        checks = _check_systemd_runtime(tmp_path)
    assert len(checks) == 1
    only = checks[0]
    assert only.name == "systemd:available"
    assert only.passed is True  # degraded, NOT failed
    assert only.category == "systemd"
    assert "skipping" in only.detail.lower()


def test_systemd_unavailable_on_linux_degrades(tmp_path):
    """No systemctl --user session (containers/CI) degrades, does not FAIL."""
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=False),
    ):
        checks = _check_systemd_runtime(tmp_path)
    assert len(checks) == 1
    assert checks[0].name == "systemd:available"
    assert checks[0].passed is True
    # A degraded family must never contribute a failure.
    assert all(c.passed for c in checks)


# ---------------------------------------------------------------------------
# _check_systemd_runtime: Linux + available
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_systemd(tmp_path):
    """A fake bundled + installed unit tree with one matching unit."""
    bundled = tmp_path / "bundled"
    user_dir = tmp_path / "user"
    bundled.mkdir()
    user_dir.mkdir()
    content = b"[Unit]\nDescription=fake\n"
    (bundled / "skcapstone.service").write_bytes(content)
    (user_dir / "skcapstone.service").write_bytes(content)
    return bundled, user_dir


def test_systemd_healthy_all_pass(fake_systemd):
    """Installed unit, no failed units, byte-identical to packaged -> all pass."""
    bundled, user_dir = fake_systemd
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.systemd.SYSTEMD_USER_DIR", user_dir),
        patch("skcapstone.systemd.BUNDLED_DIR", bundled),
        patch("skcapstone.doctor._failed_sk_units", return_value=([], "")),
    ):
        checks = _check_systemd_runtime(Path("/nonexistent"))
    by = _by_name(checks)
    assert by["systemd:available"].passed
    assert by["systemd:units-installed"].passed
    assert "skcapstone.service" in by["systemd:units-installed"].detail
    assert by["systemd:failed-units"].passed
    assert by["systemd:unit-drift"].passed
    assert all(c.passed for c in checks)


def test_systemd_failed_unit_reported(fake_systemd):
    """A failed template instance is surfaced as a failing check."""
    bundled, user_dir = fake_systemd
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.systemd.SYSTEMD_USER_DIR", user_dir),
        patch("skcapstone.systemd.BUNDLED_DIR", bundled),
        patch(
            "skcapstone.doctor._failed_sk_units",
            return_value=(["skcapstone@lumina.service"], ""),
        ),
    ):
        checks = _check_systemd_runtime(Path("/nonexistent"))
    failed = _by_name(checks)["systemd:failed-units"]
    assert failed.passed is False
    assert "skcapstone@lumina.service" in failed.detail
    assert failed.fix  # actionable remediation present


def test_systemd_unit_drift_reported(fake_systemd):
    """An installed unit that differs from the packaged copy is flagged stale."""
    bundled, user_dir = fake_systemd
    # Make the installed unit drift from the packaged one.
    (user_dir / "skcapstone.service").write_bytes(b"[Unit]\nDescription=STALE\n")
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.systemd.SYSTEMD_USER_DIR", user_dir),
        patch("skcapstone.systemd.BUNDLED_DIR", bundled),
        patch("skcapstone.doctor._failed_sk_units", return_value=([], "")),
    ):
        checks = _check_systemd_runtime(Path("/nonexistent"))
    drift = _by_name(checks)["systemd:unit-drift"]
    assert drift.passed is False
    assert "skcapstone.service" in drift.detail


def test_systemd_no_units_installed_is_pass(tmp_path):
    """Zero installed units is an informational pass, not a failure."""
    empty_user = tmp_path / "user"
    empty_user.mkdir()
    bundled = tmp_path / "bundled"
    bundled.mkdir()
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.systemd.SYSTEMD_USER_DIR", empty_user),
        patch("skcapstone.systemd.BUNDLED_DIR", bundled),
        patch("skcapstone.doctor._failed_sk_units", return_value=([], "")),
    ):
        checks = _check_systemd_runtime(Path("/nonexistent"))
    by = _by_name(checks)
    assert by["systemd:units-installed"].passed
    assert "none installed" in by["systemd:units-installed"].detail
    assert all(c.passed for c in checks)


def test_systemd_unit_files_filters_foreign(tmp_path):
    """_systemd_unit_files returns only SK* service/timer files."""
    d = tmp_path / "user"
    d.mkdir()
    (d / "skcapstone.service").write_text("x")
    (d / "skcomms-heartbeat.timer").write_text("x")
    (d / "unrelated.service").write_text("x")
    (d / "skcapstone.socket").write_text("x")  # not a .service/.timer
    names = _systemd_unit_files(d)
    assert names == ["skcapstone.service", "skcomms-heartbeat.timer"]


def test_systemd_unit_files_missing_dir(tmp_path):
    """Missing unit dir yields an empty list (no crash)."""
    assert _systemd_unit_files(tmp_path / "nope") == []


# ---------------------------------------------------------------------------
# _check_store_integrity
# ---------------------------------------------------------------------------


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_store_integrity_absent_stores_pass(tmp_path):
    """No stores on disk -> every store check passes."""
    checks = _check_store_integrity(tmp_path)
    by = _by_name(checks)
    assert set(by) == {"store:cards", "store:gtd", "store:itil"}
    assert all(c.passed for c in checks)


def test_store_integrity_healthy(tmp_path):
    """Valid card / gtd / itil stores all parse and pass."""
    home = tmp_path
    _write(home / "cards" / "abc" / "core.json", json.dumps({"id": "abc"}))
    _write(
        home / "cards" / "abc" / "events" / "lumina@h.jsonl",
        json.dumps({"action": "create"}) + "\n" + json.dumps({"action": "move"}) + "\n",
    )
    _write(home / "coordination" / "archive" / "h.jsonl", json.dumps({"id": "abc"}) + "\n")
    _write(home / "coordination" / "gtd" / "inbox.json", "[]")
    _write(
        home / "coordination" / "itil" / "incidents" / "i1" / "core.json",
        json.dumps({"id": "i1"}),
    )
    _write(
        home / "coordination" / "itil" / "incidents" / "i1" / "events" / "a@h.jsonl",
        json.dumps({"action": "create"}) + "\n",
    )
    checks = _check_store_integrity(home)
    assert all(c.passed for c in checks), _by_name(checks)


def test_store_integrity_corrupt_card_core(tmp_path):
    """A malformed card core.json is reported by store:cards."""
    _write(tmp_path / "cards" / "abc" / "core.json", "{not json")
    checks = _check_store_integrity(tmp_path)
    cards = _by_name(checks)["store:cards"]
    assert cards.passed is False
    assert "core.json" in cards.detail
    # Other stores unaffected.
    assert _by_name(checks)["store:gtd"].passed
    assert _by_name(checks)["store:itil"].passed


def test_store_integrity_corrupt_event_line(tmp_path):
    """A bad JSONL line in an append-only event log is reported."""
    _write(tmp_path / "cards" / "abc" / "core.json", json.dumps({"id": "abc"}))
    _write(
        tmp_path / "cards" / "abc" / "events" / "a@h.jsonl",
        json.dumps({"ok": 1}) + "\n" + "{broken\n",
    )
    checks = _check_store_integrity(tmp_path)
    assert _by_name(checks)["store:cards"].passed is False


def test_store_integrity_corrupt_gtd(tmp_path):
    """A malformed GTD list file is reported by store:gtd only."""
    _write(tmp_path / "coordination" / "gtd" / "inbox.json", "[[[")
    checks = _check_store_integrity(tmp_path)
    assert _by_name(checks)["store:gtd"].passed is False
    assert _by_name(checks)["store:cards"].passed
    assert _by_name(checks)["store:itil"].passed


def test_store_integrity_corrupt_itil_core(tmp_path):
    """A malformed ITIL core.json is reported by store:itil only."""
    _write(
        tmp_path / "coordination" / "itil" / "changes" / "c1" / "core.json",
        "not-json",
    )
    checks = _check_store_integrity(tmp_path)
    assert _by_name(checks)["store:itil"].passed is False
    assert _by_name(checks)["store:cards"].passed
    assert _by_name(checks)["store:gtd"].passed


# ---------------------------------------------------------------------------
# Integration: the new categories are wired into run_diagnostics
# ---------------------------------------------------------------------------


def test_run_diagnostics_includes_systemd_and_store(tmp_path):
    """run_diagnostics surfaces the systemd + store categories."""
    # Force systemd unavailable so the result is deterministic in any env.
    with (
        patch("skcapstone.doctor.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=False),
    ):
        report = run_diagnostics(tmp_path)
    categories = {c.category for c in report.checks}
    assert "systemd" in categories
    assert "store" in categories


# ---------------------------------------------------------------------------
# preflight.PreflightChecker.check_systemd
# ---------------------------------------------------------------------------


def test_preflight_systemd_non_linux_ok(tmp_path):
    """check_systemd degrades to ok (non-critical) off Linux."""
    with patch("skcapstone.preflight.platform.system", return_value="Windows"):
        res = PreflightChecker(tmp_path).check_systemd()
    assert res.status == "ok"
    assert res.critical is False


def test_preflight_systemd_unavailable_ok(tmp_path):
    """No systemctl --user session degrades to ok, never fails startup."""
    with (
        patch("skcapstone.preflight.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=False),
    ):
        res = PreflightChecker(tmp_path).check_systemd()
    assert res.status == "ok"
    assert res.critical is False


def test_preflight_systemd_failed_units_warn(tmp_path):
    """A failed SK* unit surfaces as a non-critical warn."""

    class _Proc:
        returncode = 0
        stdout = "skcapstone@lumina.service loaded failed failed Foo\n"
        stderr = ""

    with (
        patch("skcapstone.preflight.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.preflight.subprocess.run", return_value=_Proc()),
    ):
        res = PreflightChecker(tmp_path).check_systemd()
    assert res.status == "warn"
    assert res.critical is False
    assert "skcapstone@lumina.service" in res.message


def test_preflight_systemd_clean_ok(tmp_path):
    """No failed SK* units -> ok."""

    class _Proc:
        returncode = 0
        stdout = "some-other.service loaded failed failed Bar\n"
        stderr = ""

    with (
        patch("skcapstone.preflight.platform.system", return_value="Linux"),
        patch("skcapstone.systemd.systemd_available", return_value=True),
        patch("skcapstone.preflight.subprocess.run", return_value=_Proc()),
    ):
        res = PreflightChecker(tmp_path).check_systemd()
    assert res.status == "ok"
