"""Tests for the post-resume self-test (skcapstone.post_resume).

All underlying probes are injected so the suite is fully hermetic - no real
daemon, database, coordination board, clock source, or network is needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from skcapstone.post_resume import (
    SelfTestCheck,
    SelfTestConfig,
    SelfTestReport,
    run_post_resume_selftest,
)

# ---------------------------------------------------------------------------
# Fakes: stand-ins for doctor.Check / DiagnosticReport
# ---------------------------------------------------------------------------


@dataclass
class FakeCheck:
    """Mimics skcapstone.doctor.Check (the attributes post_resume reads)."""

    name: str
    category: str
    passed: bool
    detail: str = ""
    description: str = ""
    fix: str = ""


@dataclass
class FakeReport:
    """Mimics skcapstone.doctor.DiagnosticReport."""

    checks: list


def _healthy_diag(home: Path) -> FakeReport:
    """A diagnostics report where every relevant category passes."""
    return FakeReport(
        checks=[
            FakeCheck("identity:self", "identity", True, "resolves"),
            FakeCheck("memory:index", "memory", True, "reachable"),
            FakeCheck("transport:socket", "transport", True, "alive"),
            FakeCheck("sync:conflicts", "sync", True, "clean"),
            # A category outside critical/warn - must be ignored entirely:
            FakeCheck("harness:env", "harness", False, "irrelevant post-resume"),
        ]
    )


def _memory_broken_diag(home: Path) -> FakeReport:
    """Same as healthy but the (critical) memory check fails."""
    return FakeReport(
        checks=[
            FakeCheck("identity:self", "identity", True, "resolves"),
            FakeCheck("memory:index", "memory", False, "skmem-pg connection refused"),
            FakeCheck("transport:socket", "transport", True, "alive"),
            FakeCheck("sync:conflicts", "sync", True, "clean"),
        ]
    )


def _sync_broken_diag(home: Path) -> FakeReport:
    """A non-critical (warn) category fails; nothing critical."""
    return FakeReport(
        checks=[
            FakeCheck("identity:self", "identity", True, "resolves"),
            FakeCheck("memory:index", "memory", True, "reachable"),
            FakeCheck("transport:socket", "transport", True, "alive"),
            FakeCheck("sync:conflicts", "sync", False, "12 conflict files"),
        ]
    )


# Convenience injected probes for the "all healthy" baseline.
def _daemon_up(home: Path) -> bool:
    return True


def _daemon_down(home: Path) -> bool:
    return False


def _board_ok(home: Path) -> int:
    return 7


def _board_unreadable(home: Path) -> int:
    raise OSError("coordination dir gone")


def _clock_ok() -> float:
    return 0.01


def _clock_none() -> float:
    return None


def _net_up(host: str):
    return True, "tailscale up"


def _net_down(host: str):
    return False, "tailscale backend stopped"


def _all_healthy(home: Path, cfg=None, **overrides) -> SelfTestReport:
    """Run with every probe injected to a healthy value unless overridden."""
    kwargs = dict(
        diagnostics_fn=_healthy_diag,
        daemon_alive_fn=_daemon_up,
        board_reader_fn=_board_ok,
        clock_probe_fn=_clock_ok,
        network_probe_fn=_net_up,
    )
    kwargs.update(overrides)
    return run_post_resume_selftest(home, cfg, **kwargs)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestAllHealthy:
    def test_all_checks_pass_exit_zero(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path)
        assert report.passed is True
        assert report.status == "pass"
        assert report.exit_code == 0
        assert report.failures == []
        assert report.critical_failures == []

    def test_expected_checks_present(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path)
        names = {c.name for c in report.checks}
        # daemon + coordination + clock + network + the folded doctor checks
        assert "daemon" in names
        assert "coordination_board" in names
        assert "clock_skew" in names
        assert "network" in names
        assert "memory:index" in names
        assert "identity:self" in names

    def test_irrelevant_category_ignored(self, tmp_path: Path) -> None:
        """A failing doctor check outside critical/warn categories is dropped."""
        report = _all_healthy(tmp_path)
        names = {c.name for c in report.checks}
        assert "harness:env" not in names

    def test_to_dict_roundtrip(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path)
        d = report.to_dict()
        assert d["passed"] is True
        assert d["exit_code"] == 0
        assert d["counts"]["failures"] == 0
        assert d["counts"]["critical_failures"] == 0
        assert isinstance(d["checks"], list) and d["checks"]


# ---------------------------------------------------------------------------
# Critical failures -> overall fail + non-zero exit
# ---------------------------------------------------------------------------


class TestDaemonDown:
    def test_daemon_down_fails(self, tmp_path: Path) -> None:
        # fail-before/pass-after: same suite, only the daemon probe flips.
        healthy = _all_healthy(tmp_path)
        assert healthy.passed is True

        broken = _all_healthy(tmp_path, daemon_alive_fn=_daemon_down)
        assert broken.passed is False
        assert broken.status == "fail"
        assert broken.exit_code == 1
        daemon = next(c for c in broken.checks if c.name == "daemon")
        assert daemon.status == "fail"
        assert daemon.critical is True
        assert daemon.fix  # actionable fix present


class TestMemoryBackendDown:
    def test_critical_doctor_category_fails_overall(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path, diagnostics_fn=_memory_broken_diag)
        assert report.passed is False
        assert report.exit_code == 1
        mem = next(c for c in report.checks if c.name == "memory:index")
        assert mem.status == "fail"
        assert mem.critical is True


class TestBoardUnreadable:
    def test_board_unreadable_fails(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path, board_reader_fn=_board_unreadable)
        assert report.passed is False
        assert report.exit_code == 1
        board = next(c for c in report.checks if c.name == "coordination_board")
        assert board.status == "fail"
        assert "unreadable" in board.detail


class TestDiagnosticsRaises:
    def test_diagnostics_exception_is_a_failure(self, tmp_path: Path) -> None:
        def _boom(home: Path):
            raise RuntimeError("doctor exploded")

        report = _all_healthy(tmp_path, diagnostics_fn=_boom)
        assert report.passed is False
        diag = next(c for c in report.checks if c.name == "diagnostics")
        assert diag.status == "fail"


# ---------------------------------------------------------------------------
# Non-critical failures -> warn, still exit 0
# ---------------------------------------------------------------------------


class TestNonCriticalWarnings:
    def test_sync_failure_warns_not_fails(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path, diagnostics_fn=_sync_broken_diag)
        assert report.passed is True  # no critical failure
        assert report.exit_code == 0
        assert report.status == "warn"
        sync = next(c for c in report.checks if c.name == "sync:conflicts")
        assert sync.status == "warn"
        assert sync.critical is False

    def test_network_down_warns_not_fails(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path, network_probe_fn=_net_down)
        assert report.passed is True
        assert report.exit_code == 0
        net = next(c for c in report.checks if c.name == "network")
        assert net.status == "warn"
        assert net.critical is False

    def test_clock_not_measurable_is_skip(self, tmp_path: Path) -> None:
        report = _all_healthy(tmp_path, clock_probe_fn=_clock_none)
        assert report.passed is True
        clock = next(c for c in report.checks if c.name == "clock_skew")
        assert clock.status == "skip"


# ---------------------------------------------------------------------------
# Clock skew thresholds
# ---------------------------------------------------------------------------


class TestClockSkew:
    def test_skew_above_warn_threshold_warns(self, tmp_path: Path) -> None:
        cfg = SelfTestConfig(clock_skew_warn_seconds=60.0)
        report = _all_healthy(tmp_path, cfg=cfg, clock_probe_fn=lambda: 90.0)
        clock = next(c for c in report.checks if c.name == "clock_skew")
        assert clock.status == "warn"
        assert report.passed is True  # warn is non-critical by default

    def test_skew_above_fail_threshold_fails_when_clock_critical(self, tmp_path: Path) -> None:
        cfg = SelfTestConfig(
            clock_skew_warn_seconds=60.0,
            clock_skew_fail_seconds=300.0,
            critical_categories=frozenset({"identity", "memory", "transport", "clock"}),
        )
        report = _all_healthy(tmp_path, cfg=cfg, clock_probe_fn=lambda: 600.0)
        clock = next(c for c in report.checks if c.name == "clock_skew")
        assert clock.status == "fail"
        assert clock.critical is True
        assert report.passed is False
        assert report.exit_code == 1

    def test_fail_threshold_zero_never_fatal(self, tmp_path: Path) -> None:
        cfg = SelfTestConfig(clock_skew_warn_seconds=60.0, clock_skew_fail_seconds=0.0)
        report = _all_healthy(tmp_path, cfg=cfg, clock_probe_fn=lambda: 9999.0)
        clock = next(c for c in report.checks if c.name == "clock_skew")
        assert clock.status == "warn"  # only warns, never fails
        assert report.passed is True


# ---------------------------------------------------------------------------
# Alerting: only when enabled AND on critical failure
# ---------------------------------------------------------------------------


class _SpyNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, title: str, body: str, urgency: str) -> bool:
        self.calls.append((title, body, urgency))
        return True


class TestAlerting:
    def test_no_alert_when_disabled_even_on_failure(self, tmp_path: Path) -> None:
        spy = _SpyNotifier()
        cfg = SelfTestConfig(alert_enabled=False)
        report = _all_healthy(tmp_path, cfg=cfg, daemon_alive_fn=_daemon_down, notifier=spy)
        assert report.passed is False
        assert spy.calls == []
        assert report.alerted is False

    def test_no_alert_when_enabled_but_healthy(self, tmp_path: Path) -> None:
        spy = _SpyNotifier()
        cfg = SelfTestConfig(alert_enabled=True)
        report = _all_healthy(tmp_path, cfg=cfg, notifier=spy)
        assert report.passed is True
        assert spy.calls == []
        assert report.alerted is False

    def test_alert_fires_when_enabled_and_critical_failure(self, tmp_path: Path) -> None:
        spy = _SpyNotifier()
        cfg = SelfTestConfig(alert_enabled=True)
        report = _all_healthy(tmp_path, cfg=cfg, daemon_alive_fn=_daemon_down, notifier=spy)
        assert report.passed is False
        assert len(spy.calls) == 1
        title, body, urgency = spy.calls[0]
        assert "FAILED" in title
        assert urgency == "critical"
        assert report.alerted is True

    def test_alert_not_fired_on_noncritical_warning(self, tmp_path: Path) -> None:
        spy = _SpyNotifier()
        cfg = SelfTestConfig(alert_enabled=True)
        report = _all_healthy(tmp_path, cfg=cfg, network_probe_fn=_net_down, notifier=spy)
        assert report.passed is True  # only a warn
        assert spy.calls == []

    def test_alert_failure_does_not_break_selftest(self, tmp_path: Path) -> None:
        def _bad_notify(title, body, urgency):
            raise RuntimeError("dbus down")

        cfg = SelfTestConfig(alert_enabled=True)
        report = _all_healthy(
            tmp_path, cfg=cfg, daemon_alive_fn=_daemon_down, notifier=_bad_notify
        )
        assert report.passed is False  # still reports the failure
        assert report.alerted is False  # alert failed silently


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class TestConfig:
    def test_defaults_when_no_file(self, tmp_path: Path) -> None:
        cfg = SelfTestConfig.load(tmp_path)
        assert cfg.alert_enabled is False
        assert "memory" in cfg.critical_categories
        assert cfg.check_daemon is True

    def test_loads_from_yaml(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "selftest.yaml").write_text(
            "alert_enabled: true\n"
            "check_network: false\n"
            "clock_skew_warn_seconds: 45\n"
            "critical_categories: [memory, transport]\n",
            encoding="utf-8",
        )
        cfg = SelfTestConfig.load(tmp_path)
        assert cfg.alert_enabled is True
        assert cfg.check_network is False
        assert cfg.clock_skew_warn_seconds == 45.0
        assert cfg.critical_categories == frozenset({"memory", "transport"})

    def test_malformed_yaml_falls_back_to_defaults(self, tmp_path: Path) -> None:
        cfg_dir = tmp_path / "config"
        cfg_dir.mkdir()
        (cfg_dir / "selftest.yaml").write_text(":::not yaml:::\n[", encoding="utf-8")
        cfg = SelfTestConfig.load(tmp_path)
        assert cfg.alert_enabled is False  # defaults intact

    def test_disabling_checks_removes_them(self, tmp_path: Path) -> None:
        cfg = SelfTestConfig(check_clock=False, check_network=False)
        report = _all_healthy(tmp_path, cfg=cfg)
        names = {c.name for c in report.checks}
        assert "clock_skew" not in names
        assert "network" not in names
        assert "daemon" in names


# ---------------------------------------------------------------------------
# SelfTestCheck / report helpers
# ---------------------------------------------------------------------------


class TestModels:
    def test_check_ok_property(self) -> None:
        assert SelfTestCheck("x", "pass").ok is True
        assert SelfTestCheck("x", "warn").ok is True
        assert SelfTestCheck("x", "skip").ok is True
        assert SelfTestCheck("x", "fail").ok is False

    def test_report_status_precedence(self) -> None:
        r = SelfTestReport(
            checks=[
                SelfTestCheck("a", "pass"),
                SelfTestCheck("b", "warn", critical=False),
            ]
        )
        assert r.status == "warn"
        assert r.passed is True

        r.checks.append(SelfTestCheck("c", "fail", critical=True))
        assert r.status == "fail"
        assert r.passed is False
