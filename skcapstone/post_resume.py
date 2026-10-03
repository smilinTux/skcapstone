"""
Post-resume self-test.

Laptop-fleet machines (notably the .41 laptop) suspend and resume. After a
resume the sovereign stack can be left in a bad state: the daemon may have
wedged, memory / skmem-pg connections may have dropped, the coordination board
may be unreadable, the comms transport may be dead, tokens may have expired, or
the wall clock may have skewed while the machine slept.

This module runs an automated, **read-only** self-test that verifies the stack
is healthy after a resume and reports a structured pass/fail result per check
plus an overall status. It is deliberately observational: a self-test observes,
it does not mutate. Any self-heal is left to explicit, conservative tooling
(e.g. ``skcapstone daemon start``); this module only reports and, when
explicitly enabled, alerts.

It REUSES the existing health machinery rather than reinventing it:

* :func:`skcapstone.doctor.run_diagnostics` - the canonical stack diagnostics
  (identity, memory, transport, sync, ...). Its :class:`~skcapstone.doctor.Check`
  results are filtered by category and folded into the self-test result.
* :func:`skcapstone.daemon.is_running` - daemon liveness (PID file + ``kill -0``).
* :class:`skcapstone.coordination.Board` - coordination board readability.
* :func:`skcapstone.notifications.notify` - the existing desktop / alert
  transport, used only when alerting is explicitly enabled and a critical
  check fails.

Resume-specific probes (clock skew, network / tailscale reachability) are
injectable so tests stay hermetic and so operators can point them at whatever
host makes sense for their fleet.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Optional

logger = logging.getLogger("skcapstone.post_resume")

CheckStatus = Literal["pass", "warn", "fail", "skip"]

# Doctor categories that, if failing, mean the resumed stack is broken.
DEFAULT_CRITICAL_CATEGORIES = frozenset({"identity", "memory", "transport"})
# Doctor categories worth surfacing but not fatal (a stale sync recovers).
DEFAULT_WARN_CATEGORIES = frozenset({"sync", "agent", "packages"})


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------


@dataclass
class SelfTestCheck:
    """Result of a single post-resume check.

    Attributes:
        name: Short check identifier (e.g. ``daemon``, ``memory``).
        status: ``pass`` / ``warn`` / ``fail`` / ``skip``.
        critical: Whether a failure of this check fails the overall self-test.
        detail: Human-readable detail (version, count, error message).
        category: Grouping, mirrors doctor categories where reused.
        fix: Suggested remediation, if any.
        duration_ms: Wall-clock time the check took, in milliseconds.
    """

    name: str
    status: CheckStatus
    critical: bool = True
    detail: str = ""
    category: str = "resume"
    fix: str = ""
    duration_ms: float = 0.0

    @property
    def ok(self) -> bool:
        """True when the check did not fail (pass / warn / skip)."""
        return self.status != "fail"

    @property
    def failed(self) -> bool:
        """True when the check failed."""
        return self.status == "fail"

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict."""
        return {
            "name": self.name,
            "status": self.status,
            "critical": self.critical,
            "detail": self.detail,
            "category": self.category,
            "fix": self.fix,
            "duration_ms": round(self.duration_ms, 1),
        }


@dataclass
class SelfTestReport:
    """Aggregate result of a post-resume self-test run.

    Attributes:
        checks: Per-check results.
        started_at: Unix timestamp when the run started.
        duration_ms: Total wall-clock duration in milliseconds.
        alerted: Whether an alert notification was emitted.
    """

    checks: list[SelfTestCheck] = field(default_factory=list)
    started_at: float = 0.0
    duration_ms: float = 0.0
    alerted: bool = False

    @property
    def critical_failures(self) -> list[SelfTestCheck]:
        """Checks that failed and are marked critical."""
        return [c for c in self.checks if c.failed and c.critical]

    @property
    def failures(self) -> list[SelfTestCheck]:
        """All failed checks (critical or not)."""
        return [c for c in self.checks if c.failed]

    @property
    def warnings(self) -> list[SelfTestCheck]:
        """All warn checks."""
        return [c for c in self.checks if c.status == "warn"]

    @property
    def passed(self) -> bool:
        """True when no critical check failed."""
        return len(self.critical_failures) == 0

    @property
    def status(self) -> CheckStatus:
        """Overall status: ``fail`` on any critical failure, else ``warn`` if
        any non-critical failure / warning, else ``pass``."""
        if self.critical_failures:
            return "fail"
        if self.failures or self.warnings:
            return "warn"
        return "pass"

    @property
    def exit_code(self) -> int:
        """0 when healthy (no critical failure), 1 otherwise."""
        return 0 if self.passed else 1

    def to_dict(self) -> dict:
        """Serialize to a JSON-friendly dict."""
        return {
            "status": self.status,
            "passed": self.passed,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 1),
            "alerted": self.alerted,
            "counts": {
                "total": len(self.checks),
                "passed": sum(1 for c in self.checks if c.status == "pass"),
                "warnings": len(self.warnings),
                "failures": len(self.failures),
                "critical_failures": len(self.critical_failures),
                "skipped": sum(1 for c in self.checks if c.status == "skip"),
            },
            "checks": [c.to_dict() for c in self.checks],
        }


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class SelfTestConfig:
    """Configuration for the post-resume self-test.

    Loaded from ``<home>/config/selftest.yaml`` when present; safe defaults
    otherwise. All thresholds are config-driven so operators can tune per
    fleet without code changes.

    Attributes:
        critical_categories: Doctor categories whose failure is fatal.
        warn_categories: Doctor categories surfaced as warnings.
        check_daemon: Include the daemon-liveness check.
        check_board: Include the coordination-board-readable check.
        check_clock: Include the clock-skew check.
        check_network: Include the network / tailscale reachability check.
        clock_skew_warn_seconds: Skew above this warns.
        clock_skew_fail_seconds: Skew above this fails (0 = never fatal).
        network_host: Host the default network probe checks (tailscale peer or
            hostname); informational only, the probe is injectable.
        alert_enabled: Opt-in. When True, a critical failure emits a
            notification via the existing alert transport.
    """

    critical_categories: frozenset[str] = DEFAULT_CRITICAL_CATEGORIES
    warn_categories: frozenset[str] = DEFAULT_WARN_CATEGORIES
    check_daemon: bool = True
    check_board: bool = True
    check_clock: bool = True
    check_network: bool = True
    clock_skew_warn_seconds: float = 120.0
    clock_skew_fail_seconds: float = 0.0
    network_host: str = ""
    alert_enabled: bool = False

    @classmethod
    def load(cls, home: Path) -> "SelfTestConfig":
        """Load config from ``<home>/config/selftest.yaml``, else defaults.

        Args:
            home: Agent home directory.

        Returns:
            A populated :class:`SelfTestConfig`. Unreadable / malformed config
            falls back to defaults (a self-test must never crash on config).
        """
        cfg = cls()
        path = Path(home).expanduser() / "config" / "selftest.yaml"
        if not path.exists():
            return cfg
        try:
            import yaml

            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception as exc:  # never let bad config break the self-test
            logger.debug("selftest.yaml unreadable (%s) - using defaults", exc)
            return cfg
        if not isinstance(data, dict):
            return cfg

        if isinstance(data.get("critical_categories"), list):
            cfg.critical_categories = frozenset(str(c) for c in data["critical_categories"])
        if isinstance(data.get("warn_categories"), list):
            cfg.warn_categories = frozenset(str(c) for c in data["warn_categories"])
        for flag in ("check_daemon", "check_board", "check_clock", "check_network"):
            if flag in data:
                setattr(cfg, flag, bool(data[flag]))
        for num in ("clock_skew_warn_seconds", "clock_skew_fail_seconds"):
            if num in data:
                try:
                    setattr(cfg, num, float(data[num]))
                except (TypeError, ValueError):
                    pass
        if "network_host" in data:
            cfg.network_host = str(data["network_host"])
        if "alert_enabled" in data:
            cfg.alert_enabled = bool(data["alert_enabled"])
        return cfg


# Injectable probe signatures.
DiagnosticsFn = Callable[[Path], "object"]  # -> DiagnosticReport
DaemonAliveFn = Callable[[Path], bool]
BoardReaderFn = Callable[[Path], int]  # -> task count (raises on unreadable)
# clock probe returns observed skew in seconds (abs), or None if unknown.
ClockProbeFn = Callable[[], Optional[float]]
# network probe returns (reachable, detail).
NetworkProbeFn = Callable[[str], "tuple[bool, str]"]
Notifier = Callable[[str, str, str], bool]


# ---------------------------------------------------------------------------
# Default probes (real implementations)
# ---------------------------------------------------------------------------


def _default_daemon_alive(home: Path) -> bool:
    """Default daemon-liveness probe - reuses ``daemon.is_running``."""
    from .daemon import is_running

    return is_running(home)


def _default_board_reader(home: Path) -> int:
    """Default board probe - read the coordination board, return task count.

    Raises on an unreadable board (which the caller turns into a fail).
    """
    from .coordination import Board

    board = Board(Path(home).expanduser())
    return len(board.load_tasks(include_archived=False))


def _default_clock_probe() -> Optional[float]:
    """Default clock-skew probe.

    Read-only and dependency-free: it measures the drift between the monotonic
    clock and the wall clock across a short sleep. A large discrepancy is a
    signal the wall clock jumped (as it can right after resume). It cannot
    detect absolute NTP skew on its own; operators wanting true NTP skew should
    inject a probe that queries their time source. Returns skew in seconds, or
    None if it could not be measured.
    """
    try:
        wall_before = time.time()
        mono_before = time.monotonic()
        time.sleep(0.05)
        wall_after = time.time()
        mono_after = time.monotonic()
        wall_delta = wall_after - wall_before
        mono_delta = mono_after - mono_before
        return abs(wall_delta - mono_delta)
    except Exception as exc:
        logger.debug("clock probe failed: %s", exc)
        return None


def _default_network_probe(host: str) -> "tuple[bool, str]":
    """Default network / tailscale reachability probe.

    Best-effort and read-only: if the ``tailscale`` binary is present it runs
    ``tailscale status`` (which does not mutate anything) and reports whether
    the backend is up. If tailscale is absent, the check is reported as
    unavailable (skip) by returning ``(True, ...)`` with a note - a laptop
    without tailscale is not a resume failure.

    Args:
        host: Optional host hint (informational).

    Returns:
        Tuple of (reachable, detail).
    """
    import shutil
    import subprocess

    if not shutil.which("tailscale"):
        return True, "tailscale not installed (skipping network probe)"
    try:
        result = subprocess.run(
            ["tailscale", "status", "--peers=false"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"tailscale status failed: {exc}"
    out = (result.stdout + result.stderr).strip().lower()
    if result.returncode != 0 or "stopped" in out or "logged out" in out:
        first = (result.stdout or result.stderr).strip().splitlines()
        return False, first[0] if first else "tailscale backend not up"
    hint = f" (host {host})" if host else ""
    return True, f"tailscale up{hint}"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def _timed(fn: Callable[[], SelfTestCheck]) -> SelfTestCheck:
    """Run a check builder, timing it and stamping ``duration_ms``."""
    start = time.monotonic()
    check = fn()
    check.duration_ms = (time.monotonic() - start) * 1000.0
    return check


def run_post_resume_selftest(
    home: Path,
    config: Optional[SelfTestConfig] = None,
    *,
    diagnostics_fn: Optional[DiagnosticsFn] = None,
    daemon_alive_fn: Optional[DaemonAliveFn] = None,
    board_reader_fn: Optional[BoardReaderFn] = None,
    clock_probe_fn: Optional[ClockProbeFn] = None,
    network_probe_fn: Optional[NetworkProbeFn] = None,
    notifier: Optional[Notifier] = None,
) -> SelfTestReport:
    """Run the post-resume self-test and return a structured report.

    Reuses :func:`skcapstone.doctor.run_diagnostics` for the stack checks and
    layers resume-specific probes (daemon liveness, coordination board, clock
    skew, network reachability) on top. Every check is read-only.

    All external dependencies are injectable so the whole suite can be exercised
    hermetically in tests. When a dependency is not injected, its real
    implementation is used.

    Args:
        home: Agent home directory (``~/.skcapstone`` or a per-agent home).
        config: Self-test config. Loaded from ``<home>/config/selftest.yaml``
            when omitted.
        diagnostics_fn: Override for ``doctor.run_diagnostics``.
        daemon_alive_fn: Override for the daemon-liveness probe.
        board_reader_fn: Override for the coordination-board probe.
        clock_probe_fn: Override for the clock-skew probe.
        network_probe_fn: Override for the network-reachability probe.
        notifier: Override for the alert notifier
            (``skcapstone.notifications.notify``).

    Returns:
        A :class:`SelfTestReport`. An alert is emitted only when
        ``config.alert_enabled`` is True **and** the overall result is a
        critical failure.
    """
    home = Path(home).expanduser()
    cfg = config or SelfTestConfig.load(home)
    report = SelfTestReport(started_at=time.time())
    run_start = time.monotonic()

    # --- Daemon liveness ------------------------------------------------
    if cfg.check_daemon:
        alive_fn = daemon_alive_fn or _default_daemon_alive

        def _daemon_check() -> SelfTestCheck:
            try:
                alive = alive_fn(home)
            except Exception as exc:
                return SelfTestCheck(
                    "daemon",
                    "fail",
                    critical=True,
                    detail=f"liveness probe error: {exc}",
                    category="daemon",
                    fix="skcapstone daemon start",
                )
            if alive:
                return SelfTestCheck(
                    "daemon",
                    "pass",
                    critical=True,
                    detail="daemon process alive",
                    category="daemon",
                )
            return SelfTestCheck(
                "daemon",
                "fail",
                critical=True,
                detail="daemon not running (no live PID)",
                category="daemon",
                fix="skcapstone daemon start",
            )

        report.checks.append(_timed(_daemon_check))

    # --- Stack diagnostics (reuse doctor) -------------------------------
    diag_fn = diagnostics_fn
    if diag_fn is None:
        from .doctor import run_diagnostics as diag_fn  # type: ignore[assignment]

    def _diagnostics_check() -> list[SelfTestCheck]:
        try:
            report_obj = diag_fn(home)
            doctor_checks = list(getattr(report_obj, "checks", []))
        except Exception as exc:
            return [
                SelfTestCheck(
                    "diagnostics",
                    "fail",
                    critical=True,
                    detail=f"run_diagnostics raised: {exc}",
                    category="diagnostics",
                )
            ]
        out: list[SelfTestCheck] = []
        for c in doctor_checks:
            category = getattr(c, "category", "general")
            is_critical = category in cfg.critical_categories
            in_warn = category in cfg.warn_categories
            # Only fold in categories the operator cares about post-resume.
            if not is_critical and not in_warn:
                continue
            passed = bool(getattr(c, "passed", False))
            if passed:
                status: CheckStatus = "pass"
            else:
                status = "fail" if is_critical else "warn"
            out.append(
                SelfTestCheck(
                    name=getattr(c, "name", "check"),
                    status=status,
                    critical=is_critical,
                    detail=getattr(c, "detail", "") or getattr(c, "description", ""),
                    category=category,
                    fix=getattr(c, "fix", ""),
                )
            )
        return out

    diag_start = time.monotonic()
    diag_checks = _diagnostics_check()
    diag_elapsed = (time.monotonic() - diag_start) * 1000.0
    # Attribute the diagnostics wall-time to the first folded check so timing
    # is visible without double-counting per sub-check.
    if diag_checks:
        diag_checks[0].duration_ms = diag_elapsed
    report.checks.extend(diag_checks)

    # --- Coordination board readable ------------------------------------
    if cfg.check_board:
        read_board = board_reader_fn or _default_board_reader

        def _board_check() -> SelfTestCheck:
            try:
                count = read_board(home)
            except Exception as exc:
                return SelfTestCheck(
                    "coordination_board",
                    "fail",
                    critical=True,
                    detail=f"board unreadable: {exc}",
                    category="coordination",
                )
            return SelfTestCheck(
                "coordination_board",
                "pass",
                critical=True,
                detail=f"board readable ({count} task(s))",
                category="coordination",
            )

        report.checks.append(_timed(_board_check))

    # --- Clock skew (resume-specific, non-critical by default) ----------
    if cfg.check_clock:
        clock_fn = clock_probe_fn or _default_clock_probe

        def _clock_check() -> SelfTestCheck:
            try:
                skew = clock_fn()
            except Exception as exc:
                return SelfTestCheck(
                    "clock_skew",
                    "warn",
                    critical=False,
                    detail=f"clock probe error: {exc}",
                    category="clock",
                )
            if skew is None:
                return SelfTestCheck(
                    "clock_skew",
                    "skip",
                    critical=False,
                    detail="clock skew not measurable",
                    category="clock",
                )
            fail_thresh = cfg.clock_skew_fail_seconds
            if fail_thresh > 0 and skew >= fail_thresh:
                return SelfTestCheck(
                    "clock_skew",
                    "fail",
                    critical="clock" in cfg.critical_categories,
                    detail=f"clock skew {skew:.1f}s >= {fail_thresh:.0f}s",
                    category="clock",
                    fix="resync time (e.g. sudo systemctl restart systemd-timesyncd)",
                )
            if skew >= cfg.clock_skew_warn_seconds:
                return SelfTestCheck(
                    "clock_skew",
                    "warn",
                    critical=False,
                    detail=f"clock skew {skew:.1f}s >= {cfg.clock_skew_warn_seconds:.0f}s",
                    category="clock",
                    fix="resync time (e.g. sudo systemctl restart systemd-timesyncd)",
                )
            return SelfTestCheck(
                "clock_skew",
                "pass",
                critical=False,
                detail=f"clock skew {skew:.2f}s",
                category="clock",
            )

        report.checks.append(_timed(_clock_check))

    # --- Network / tailscale reachability (resume-specific) -------------
    if cfg.check_network:
        net_fn = network_probe_fn or _default_network_probe

        def _network_check() -> SelfTestCheck:
            try:
                reachable, detail = net_fn(cfg.network_host)
            except Exception as exc:
                return SelfTestCheck(
                    "network",
                    "warn",
                    critical=False,
                    detail=f"network probe error: {exc}",
                    category="network",
                )
            if reachable:
                return SelfTestCheck(
                    "network",
                    "pass",
                    critical=False,
                    detail=detail,
                    category="network",
                )
            return SelfTestCheck(
                "network",
                "warn",
                critical=False,
                detail=detail,
                category="network",
                fix="check tailscale / network (sudo tailscale up)",
            )

        report.checks.append(_timed(_network_check))

    report.duration_ms = (time.monotonic() - run_start) * 1000.0

    # --- Alert (opt-in, only on critical failure) -----------------------
    if cfg.alert_enabled and not report.passed:
        notify = notifier
        if notify is None:
            from .notifications import notify as notify  # type: ignore
        failed = report.critical_failures
        detail = ", ".join(f"{c.name} ({c.detail})" for c in failed)
        title = "Post-resume self-test FAILED"
        body = f"{len(failed)} critical check(s) failed after resume: {detail}"
        try:
            notify(title, body, "critical")
            report.alerted = True
        except Exception as exc:  # an alert must never break the self-test
            logger.debug("post-resume alert failed: %s", exc)

    return report
