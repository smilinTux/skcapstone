"""Exit-code regressions for the read-only work-durability sweep."""

import importlib.util
import subprocess
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


def _git(*args: str, cwd: Path) -> None:
    """Run a real git command for repository-level sweep regressions."""
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def test_unborn_repository_is_legitimate(tmp_path: Path) -> None:
    """Do not treat an initialized main branch without a first commit as broken."""
    repo = tmp_path / "unborn"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)

    assert skwork_sweep.scan([str(repo)]) == []


def test_repository_without_upstream_is_a_finding(tmp_path: Path) -> None:
    """Keep a committed feature branch without an upstream as a normal finding."""
    repo = tmp_path / "missing-upstream"
    repo.mkdir()
    _git("init", "--initial-branch=feature", cwd=repo)
    _git("config", "user.name", "Sweep Test", cwd=repo)
    _git("config", "user.email", "sweep@example.invalid", cwd=repo)
    (repo / "tracked.txt").write_text("tracked\n")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)

    assert skwork_sweep.scan([str(repo)]) == [
        {
            "repo": "missing-upstream",
            "path": str(repo),
            "branch": "feature",
            "issues": ["branch has no upstream"],
            "untracked_sample": [],
        }
    ]


def test_detached_head_is_legitimate(tmp_path: Path) -> None:
    """Continue scanning a valid detached checkout without inventing an issue."""
    repo = tmp_path / "detached"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    _git("config", "user.name", "Sweep Test", cwd=repo)
    _git("config", "user.email", "sweep@example.invalid", cwd=repo)
    (repo / "tracked.txt").write_text("tracked\n")
    _git("add", "tracked.txt", cwd=repo)
    _git("commit", "-m", "initial", cwd=repo)
    _git("checkout", "--detach", cwd=repo)

    assert skwork_sweep.scan([str(repo)]) == []


def test_detached_head_still_requires_valid_revision(tmp_path: Path) -> None:
    """Reject status-1 symbolic-ref results whose HEAD cannot be resolved."""
    repo = tmp_path / "invalid-detached"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    (repo / ".git" / "HEAD").write_text("0" * 40 + "\n")

    with pytest.raises(RuntimeError, match=r"rev-parse --verify HEAD\^\{commit\}"):
        skwork_sweep.git_branch(str(repo))


def test_corrupt_repository_fails_report_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Propagate a real git operational error through report-only mode."""
    repo = tmp_path / "corrupt"
    repo.mkdir()
    _git("init", "--initial-branch=main", cwd=repo)
    (repo / ".git" / "HEAD").write_text("not a valid symbolic ref\n")
    monkeypatch.setattr(sys, "argv", [str(SCRIPT), "--roots", str(repo), "--report-only"])

    with pytest.raises(RuntimeError, match="git .*status --porcelain"):
        skwork_sweep.main()


def test_service_uses_report_only_runtime_command() -> None:
    """The tracked chi oneshot reports findings without failing the unit."""
    assert (
        "ExecStart=/usr/bin/python3 %h/.local/bin/skwork-sweep.py "
        "--skmail --to jarvis --report-only"
    ) in SERVICE.read_text()
