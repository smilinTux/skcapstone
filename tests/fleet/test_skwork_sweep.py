"""Exit-code regressions for the read-only work-durability sweep."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "fleet" / "skwork-sweep.py"
SERVICE = Path(__file__).parents[2] / "systemd" / "skwork-sweep.service"
SPEC = importlib.util.spec_from_file_location("skwork_sweep_under_test", SCRIPT)
assert SPEC and SPEC.loader
skwork_sweep = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(skwork_sweep)


def _run(monkeypatch: pytest.MonkeyPatch, findings: list[dict], *args: str) -> int:
    """Run the CLI entry point with synthetic scan results."""
    monkeypatch.setattr(skwork_sweep, "scan", lambda roots: findings)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), *args])
    return skwork_sweep.main()


def test_default_findings_exit_one(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep findings actionable for interactive and strict callers by default."""
    findings = [
        {"repo": "dirty", "branch": "main", "issues": ["modified=1"], "untracked_sample": []}
    ]
    assert _run(monkeypatch, findings) == 1


def test_report_only_findings_exit_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let reporting services succeed after delivering non-empty reports."""
    findings = [
        {"repo": "dirty", "branch": "main", "issues": ["modified=1"], "untracked_sample": []}
    ]
    assert _run(monkeypatch, findings, "--report-only") == 0


@pytest.mark.parametrize("args", [(), ("--report-only",)])
def test_clean_exit_zero(monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]) -> None:
    """A clean scan succeeds in either exit-code mode."""
    assert _run(monkeypatch, [], *args) == 0


def test_report_only_does_not_swallow_runtime_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Operational failures still escape main and produce a nonzero process exit."""

    def fail_scan(roots: list[str]) -> list[dict]:
        raise RuntimeError("synthetic scan failure")

    monkeypatch.setattr(skwork_sweep, "scan", fail_scan)
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--report-only"])
    with pytest.raises(RuntimeError, match="synthetic scan failure"):
        skwork_sweep.main()


def test_service_uses_report_only_runtime_command() -> None:
    """The tracked chi oneshot reports findings without failing the unit."""
    assert (
        "ExecStart=/usr/bin/python3 %h/.local/bin/skwork-sweep.py "
        "--skmail --to jarvis --report-only"
    ) in SERVICE.read_text()
