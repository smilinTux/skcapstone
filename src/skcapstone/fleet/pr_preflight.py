"""Run the local, redacted equivalent of SKCapstone pull request CI."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Sequence


class PreflightError(RuntimeError):
    """The candidate cannot be handed to GitHub review."""


@dataclass(frozen=True)
class CheckResult:
    """One redacted local check result."""

    name: str
    exit_code: int
    elapsed_ms: int


@dataclass(frozen=True)
class PreflightReceipt:
    """Terminal local CI evidence bound to one exact Git candidate."""

    schema: str
    repository: str
    base: str
    head: str
    tree: str
    paths: tuple[str, ...]
    diff_sha256: str
    checks: tuple[CheckResult, ...]
    state: str
    digest: str


Runner = Callable[[Sequence[str], Path], int]


def _run(command: Sequence[str], cwd: Path) -> int:
    """Run a check while withholding output that may contain a secret."""
    return subprocess.run(
        command, cwd=cwd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    ).returncode


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def _commands(repo: Path, base: str, head: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    """Return commands mirrored from SKCapstone's checked-in workflows."""
    standards = Path(os.environ.get("SK_STANDARDS_HOME", str(repo.parent / "sk-standards")))
    docs_check = standards / "scripts/docs_check.py"
    gitleaks = os.environ.get("SKCAPSTONE_GITLEAKS_8_28_BIN", "gitleaks")
    if not docs_check.is_file():
        raise PreflightError("SK Standards docs_check.py is unavailable")
    resolved = shutil.which(gitleaks) if os.sep not in gitleaks else gitleaks
    if not resolved:
        raise PreflightError("gitleaks 8.28.0 binary is unavailable")
    version = subprocess.run(
        [resolved, "version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if "8.28.0" not in version:
        raise PreflightError("gitleaks binary must be exact workflow version 8.28.0")
    return (
        ("diff", ("git", "diff", "--check", f"{base}...{head}")),
        ("black", ("uvx", "--from", "black==26.5.1", "black", "--check", "src/", "tests/")),
        ("ruff", ("uvx", "--from", "ruff==0.15.4", "ruff", "check", "src/")),
        (
            "docs",
            (
                "python",
                str(docs_check),
                "--repo",
                str(repo),
                "--base-ref",
                base,
                "--tier",
                "1",
                "--tier",
                "2",
                "--tier",
                "3",
            ),
        ),
        (
            "gitleaks",
            (
                resolved,
                "detect",
                "--source",
                ".",
                "--config",
                ".gitleaks.toml",
                "--baseline-path",
                ".gitleaks-baseline.json",
                "--log-opts",
                head,
                "--redact",
                "--no-banner",
                "--exit-code",
                "1",
            ),
        ),
        ("shim-imports", ("bash", "scripts/check-no-shim-imports.sh")),
        (
            "tests",
            (
                "python",
                "-m",
                "pytest",
                "tests/",
                "--strict-markers",
                "-m",
                "not integration and not e2e",
            ),
        ),
    )


def run_preflight(
    repo: Path,
    *,
    base: str,
    head: str = "HEAD",
    expected_paths: Sequence[str] = (),
    runner: Runner = _run,
) -> PreflightReceipt:
    """Run all checks and return a terminal exact-head receipt."""
    repo = repo.resolve()
    resolved_base = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
    resolved_head = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
    tree = _git(repo, "rev-parse", f"{resolved_head}^{{tree}}")
    if _git(repo, "status", "--porcelain"):
        raise PreflightError("candidate worktree is dirty")
    diff = subprocess.run(
        ["git", "diff", "--full-index", "--binary", f"{resolved_base}...{resolved_head}"],
        cwd=repo,
        check=True,
        capture_output=True,
    ).stdout
    paths = tuple(
        line
        for line in _git(
            repo, "diff", "--name-only", f"{resolved_base}...{resolved_head}"
        ).splitlines()
        if line
    )
    if expected_paths and paths != tuple(expected_paths):
        raise PreflightError("candidate path set differs from the exact expected path set")

    results: list[CheckResult] = []
    for name, command in _commands(repo, resolved_base, resolved_head):
        started = time.monotonic()
        code = runner(command, repo)
        results.append(CheckResult(name, code, round((time.monotonic() - started) * 1000)))
        if code:
            break
    state = (
        "PASS" if len(results) == 7 and all(item.exit_code == 0 for item in results) else "FAIL"
    )
    unsigned = {
        "schema": "skfleet.local-ci-preflight/v1",
        "repository": _git(repo, "config", "--get", "remote.origin.url"),
        "base": resolved_base,
        "head": resolved_head,
        "tree": tree,
        "paths": paths,
        "diff_sha256": hashlib.sha256(diff).hexdigest(),
        "checks": [asdict(item) for item in results],
        "state": state,
    }
    digest = hashlib.sha256(
        json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return PreflightReceipt(
        schema=str(unsigned["schema"]),
        repository=str(unsigned["repository"]),
        base=resolved_base,
        head=resolved_head,
        tree=tree,
        paths=paths,
        diff_sha256=str(unsigned["diff_sha256"]),
        checks=tuple(results),
        state=state,
        digest=digest,
    )


def write_receipt(receipt: PreflightReceipt, path: Path) -> None:
    """Create one immutable canonical receipt without check output."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(asdict(receipt), handle, sort_keys=True, separators=(",", ":"))
        handle.write("\n")


def main() -> int:
    """Run local PR CI and write its redacted receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--expected-path", action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = run_preflight(
            args.repo, base=args.base, head=args.head, expected_paths=args.expected_path
        )
        write_receipt(receipt, args.output)
    except (OSError, subprocess.SubprocessError, PreflightError) as exc:
        print(f"local CI preflight: BLOCKED: {exc}")
        return 2
    print(f"local CI preflight: {receipt.state} sha256={receipt.digest}")
    return 0 if receipt.state == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
