"""Focused local CI preflight tests."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from skcapstone.fleet import pr_preflight
from skcapstone.fleet.pr_preflight import PreflightError, run_preflight, write_receipt


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "remote", "add", "origin", "https://example.invalid/org/repo.git")
    (repo / "src").mkdir()
    (repo / "tests").mkdir()
    (repo / "src/app.py").write_text("VALUE = 1\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "base")
    base = git(repo, "rev-parse", "HEAD")
    (repo / "src/app.py").write_text("VALUE = 2\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "candidate")
    standards = tmp_path / "standards/scripts"
    standards.mkdir(parents=True)
    (standards / "docs_check.py").write_text("raise SystemExit(0)\n")
    fake = tmp_path / "gitleaks-8.28.0"
    fake.write_text("#!/bin/sh\necho 8.28.0\n")
    fake.chmod(0o755)
    monkeypatch.setenv("SK_STANDARDS_HOME", str(standards.parent))
    monkeypatch.setenv("SKCAPSTONE_GITLEAKS_8_28_BIN", str(fake))
    return repo, base


def test_pass_receipt_binds_exact_head_paths_and_all_checks(candidate: tuple[Path, str]) -> None:
    repo, base = candidate
    commands: list[tuple[str, ...]] = []

    def runner(command: tuple[str, ...], _cwd: Path) -> int:
        commands.append(command)
        return 0

    receipt = run_preflight(repo, base=base, expected_paths=["src/app.py"], runner=runner)
    assert receipt.state == "PASS"
    assert receipt.head == git(repo, "rev-parse", "HEAD")
    assert receipt.paths == ("src/app.py",)
    assert [check.name for check in receipt.checks] == [
        "scope/diff",
        "lint/black-26.5.1",
        "lint/ruff-0.15.4",
        "docs/changelog",
        "secret/gitleaks-8.28.0",
        "imports/shims",
        "unit/python-current",
    ]
    assert any("--redact" in command for command in commands)
    assert len(receipt.digest) == 64


def test_failure_stops_later_checks(candidate: tuple[Path, str]) -> None:
    repo, base = candidate
    calls = 0

    def runner(_command: tuple[str, ...], _cwd: Path) -> int:
        nonlocal calls
        calls += 1
        return 1 if calls == 2 else 0

    receipt = run_preflight(repo, base=base, runner=runner)
    assert receipt.state == "FAIL"
    assert [check.name for check in receipt.checks] == ["scope/diff", "lint/black-26.5.1"]


def test_path_drift_and_dirty_tree_fail_closed(candidate: tuple[Path, str]) -> None:
    repo, base = candidate
    with pytest.raises(PreflightError, match="path set"):
        run_preflight(repo, base=base, expected_paths=["wrong.py"], runner=lambda *_: 0)
    (repo / "untracked").write_text("drift")
    with pytest.raises(PreflightError, match="dirty"):
        run_preflight(repo, base=base, runner=lambda *_: 0)


def test_receipt_is_canonical_immutable_and_contains_no_check_output(
    candidate: tuple[Path, str], tmp_path: Path
) -> None:
    repo, base = candidate
    receipt = run_preflight(repo, base=base, runner=lambda *_: 0)
    path = tmp_path / "receipt.json"
    write_receipt(receipt, path)
    data = json.loads(path.read_text())
    assert data["digest"] == receipt.digest
    assert all(
        set(item) == {"name", "environment", "status", "conclusion", "exit_code", "elapsed_ms"}
        for item in data["checks"]
    )
    assert path.stat().st_mode & 0o777 == 0o444
    with pytest.raises(FileExistsError):
        write_receipt(receipt, path)


def test_pytest_matches_clean_ci_without_host_global_pi(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, object] = {}

    def fake_run(*_args: object, **kwargs: object) -> object:
        seen.update(kwargs)
        return type("Result", (), {"returncode": 0})()

    monkeypatch.setenv("PATH", "/tools:/opt/pi/bin:/usr/bin")
    monkeypatch.setattr(pr_preflight.shutil, "which", lambda _name: "/opt/pi/bin/pi")
    monkeypatch.setattr(pr_preflight.subprocess, "run", fake_run)
    assert pr_preflight._run(("python", "-m", "pytest", "tests/"), tmp_path) == 0
    assert seen["env"]["PATH"] == "/tools:/usr/bin"  # type: ignore[index]


def test_gitleaks_version_requires_exact_equality(candidate: tuple[Path, str]) -> None:
    repo, base = candidate
    fake = Path(pr_preflight.os.environ["SKCAPSTONE_GITLEAKS_8_28_BIN"])
    fake.write_text("#!/bin/sh\necho 18.28.0-malicious\n")
    with pytest.raises(PreflightError, match="exact workflow version"):
        run_preflight(repo, base=base, runner=lambda *_: 0)
