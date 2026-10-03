"""Trusted host-side executor; candidate code only runs inside a sealed bwrap."""

from __future__ import annotations

import os
import re
import resource
import selectors
import subprocess
import sys
import time
from pathlib import Path

from .production_tests import (
    MAX_OUTPUT,
    PREFIX,
    TestEvidenceError,
    junit_counts,
    load_plan,
    read_json,
    read_private,
    sha,
    source_state,
    write_once,
)


def sandbox_command(workspace: Path, output: Path, argv: list[str]) -> list[str]:
    """Expose only source/runtime read-only plus private tmp and one output directory."""
    return [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
        "--symlink",
        "usr/bin",
        "/bin",
        "--symlink",
        "usr/lib",
        "/lib",
        "--symlink",
        "usr/lib64",
        "/lib64",
        "--ro-bind",
        str(PREFIX),
        str(PREFIX),
        "--ro-bind",
        str(workspace),
        "/work",
        "--tmpfs",
        "/tmp",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--bind",
        str(output),
        "/output",
        "--chdir",
        "/work",
        "--setenv",
        "PATH",
        str(PREFIX / "bin") + ":/usr/bin",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "PYTHONPATH",
        "/work/src",
        "--setenv",
        "PYTHONPYCACHEPREFIX",
        "/tmp/pycache",
        "--setenv",
        "RUFF_CACHE_DIR",
        "/tmp/ruff-cache",
        "--setenv",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD",
        "1",
        "--setenv",
        "GIT_CONFIG_NOSYSTEM",
        "1",
        "--setenv",
        "GIT_CONFIG_GLOBAL",
        "/dev/null",
        "--",
        *argv,
    ]


def capture(argv: list[str], path: Path, timeout: int) -> int:
    """Drain one child into a bounded host file, killing/reaping on overflow or timeout."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output:
        with subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            close_fds=True,
        ) as child:
            selector = selectors.DefaultSelector()
            selector.register(child.stdout, selectors.EVENT_READ)
            total, deadline = 0, time.monotonic() + timeout
            try:
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise TestEvidenceError("native check exceeded runtime bound")
                    for key, _ in selector.select(
                        timeout=min(1, max(0, deadline - time.monotonic()))
                    ):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT:
                            raise TestEvidenceError("native check exceeded output bound")
                        output.write(chunk)
                return child.wait(timeout=max(0.01, deadline - time.monotonic()))
            finally:
                selector.close()
                if child.poll() is None:
                    child.kill()
                child.wait()


def execute(plan_path: Path, directory: Path) -> int:
    """Execute an installed plan under the exact native service invocation."""
    launch = read_json(directory / "launch.json")
    binding = launch["binding"]
    home = plan_path.parent.parent.parent
    plan, expected_path, plan_sha = load_plan(home, binding)
    if (
        expected_path != plan_path
        or directory != home / "fleet/test-runs" / plan_sha
        or launch["request_id"] != plan_sha
    ):
        raise TestEvidenceError("executor plan path or launch does not match")
    invocation = os.environ.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise TestEvidenceError("executor requires native systemd invocation")
    os.umask(0o077)
    workspace = Path(launch["workspace"])
    before = source_state(workspace, binding)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    receipt = {
        "schema": "skfleet.native-test-receipt/v1",
        "binding": binding,
        "plan_sha256": plan_sha,
        "invocation": invocation,
        "pid": os.getpid(),
        "source_before": before,
        "checks": [],
    }
    timeout = launch["production"]["resources"]["runtime_max_seconds"]
    for check in plan["checks"]:
        argv = sandbox_command(workspace, directory / "output", check["argv"])
        log = directory / (check["id"] + ".log")
        code = capture(argv, log, timeout)
        receipt["checks"].append(
            {
                **check,
                "sandbox_argv": argv,
                "exit_code": code,
                "output_sha256": sha(read_private(log)),
            }
        )
        if code != 0:
            break
    receipt["source_after"] = source_state(workspace, binding)
    # Recheck the qualified dependency bytes after every sandbox command completed.
    load_plan(home, binding)
    # Sandbox-generated JUnit is copied to immutable host custody only after exit.
    raw = read_private(directory / "output/pytest.xml")
    fd = os.open(
        directory / "pytest.xml", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    with os.fdopen(fd, "wb") as output:
        output.write(raw)
    receipt["junit_sha256"] = sha(raw)
    try:
        receipt["counts"] = junit_counts(raw)
    except (ValueError, TestEvidenceError):
        receipt["counts"] = None
    write_once(directory / "receipt.json", receipt)
    return int(
        receipt["counts"] is None
        or len(receipt["checks"]) != len(plan["checks"])
        or any(row["exit_code"] for row in receipt["checks"])
    )


if __name__ == "__main__":
    sys.exit(execute(Path(sys.argv[1]), Path(sys.argv[2])))
