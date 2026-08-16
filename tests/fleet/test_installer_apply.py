"""Tests for installer.apply() function."""
from skcapstone.fleet.installer import apply, InstallPlan, InstallStep
from skcapstone.fleet.install_backends import UNSUPPORTED


def _plan(*steps):
    return InstallPlan(steps=list(steps))


def test_dry_run_calls_backends_with_dry_run_and_reports_would_write():
    calls = []
    backends = {"core": lambda names, **kw: (calls.append(("core", names, kw)) or ("would-write", ""))}
    step = InstallStep("sknoded.service", "unit", 4, "core")
    results = apply(_plan(step), backends, dry_run=True)
    assert results[0].status == "would-write"
    assert calls[0][2]["dry_run"] is True


def test_backend_failure_isolates_and_skips_same_backend_dependents():
    def boom(names, **kw):
        return ("failed", "exit 1")

    backends = {"core": boom}
    s1 = InstallStep("sknoded.service", "unit", 4, "core")
    s2 = InstallStep("skgateway.service", "unit", 4, "core")
    results = apply(_plan(s1, s2), backends)
    assert results[0].status == "failed"
    assert results[1].status == "skipped"  # same backend, after the failure


def test_unsupported_backend_is_needs_manual_no_call():
    called = []
    backends = {"packages": lambda names, **kw: called.append(names) or ("ok", "")}
    step = InstallStep("made-up.service", "unit", 9, UNSUPPORTED)
    results = apply(_plan(step), backends)
    assert results[0].status == "needs_manual"
    assert called == []
