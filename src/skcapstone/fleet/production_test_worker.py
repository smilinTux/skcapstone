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
from xml.etree import ElementTree

from . import production_test_node as node
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


def candidate_pythonpath(workspace: Path) -> str:
    """Resolve bounded monorepo imports inside the existing read-only source mount."""
    roots = sorted(
        {
            path
            for pattern in ("packages/**/src", "services/*/src", "vendor/*/src")
            for path in workspace.glob(pattern)
        }
    )
    if len(roots) > 64:
        raise TestEvidenceError("candidate Python source roots exceed bound")
    paths = ["/work/src"]
    for path in roots:
        relative = path.relative_to(workspace)
        if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in relative.parts):
            raise TestEvidenceError("candidate Python source root is unsafe")
        if not path.is_dir() or path.resolve() != workspace.resolve() / relative:
            raise TestEvidenceError("candidate Python source root is redirected")
        paths.append("/work/" + relative.as_posix())
    return ":".join(paths)


def sandbox_command(
    workspace: Path, output: Path, argv: list[str], profile: dict | None = None
) -> list[str]:
    """Expose only source/runtime read-only plus private tmp and one output directory."""
    command = [
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
        candidate_pythonpath(workspace),
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
        "PYTEST_PLUGINS",
        "pytest_asyncio.plugin",
        "--setenv",
        "GIT_CONFIG_NOSYSTEM",
        "1",
        "--setenv",
        "GIT_CONFIG_GLOBAL",
        "/dev/null",
        "--",
        *argv,
    ]
    if node.is_node(profile):
        artifact = node.artifact_path(profile["node_environment"])
        source_position = command.index(str(workspace)) - 1
        # Clean candidates have no ignored node_modules directories. Build only
        # mountpoints privately, overlay every source entry read-only, then seal.
        mounts = ["--tmpfs", "/work"]
        for relative, excluded in (("", {"apps", ".git"}), ("apps", {"web"}), ("apps/web", set())):
            source = workspace / relative
            if source.is_symlink() or not source.is_dir():
                raise TestEvidenceError("Node source mount parent is redirected")
            target = "/work" + ("/" + relative if relative else "")
            mounts.extend(["--dir", target])
            for entry in sorted(source.iterdir()):
                if entry.name in excluded:
                    continue
                if entry.is_symlink():
                    raise TestEvidenceError("Node source mount entry is redirected")
                mounts.extend(["--ro-bind", str(entry), target + "/" + entry.name])
        command[source_position : source_position + 3] = mounts
        position = command.index("--tmpfs")
        position = command.index("--tmpfs", position + 1)
        dependencies = [
            "--ro-bind",
            str(artifact / "node_modules"),
            "/work/node_modules",
            "--ro-bind",
            str(artifact / "apps/web/node_modules"),
            "/work/apps/web/node_modules",
        ]
        if argv == node.checks(profile["recipe"])[1]["argv"]:
            dependencies.extend(
                [
                    "--bind",
                    str(output / "tsconfig.tsbuildinfo"),
                    "/work/apps/web/tsconfig.tsbuildinfo",
                ]
            )
        command[position:position] = [*dependencies, "--remount-ro", "/work"]
        command[command.index("--chdir") + 1] = "/work/apps/web"
    return command


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
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT, MAX_OUTPUT))
    receipt = {
        "schema": "skfleet.native-test-receipt/v1",
        "binding": binding,
        "plan_sha256": plan_sha,
        "invocation": invocation,
        "pid": os.getpid(),
        "checks": [],
        "counts": None,
        "junit_sha256": None,
        "failure": None,
    }
    timeout = launch["production"]["resources"]["runtime_max_seconds"]
    try:
        receipt["source_before"] = source_state(workspace, binding)
        profile = plan.get("profile")
        if node.is_node(profile):
            node.validate_environment(profile["node_environment"], workspace)
        for check in plan["checks"]:
            if node.is_node(profile) and check["id"] == "typecheck":
                # Earlier candidate tests cannot seed an incremental cache that
                # suppresses the independent typecheck. Existing output fails closed.
                fd = os.open(
                    directory / "output/tsconfig.tsbuildinfo",
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
                os.close(fd)
            argv = sandbox_command(workspace, directory / "output", check["argv"], profile)
            log = directory / (check["id"] + ".log")
            row = {**check, "sandbox_argv": argv, "exit_code": None, "output_sha256": None}
            receipt["checks"].append(row)
            try:
                row["exit_code"] = capture(argv, log, timeout)
            finally:
                if log.exists():
                    row["output_sha256"] = sha(read_private(log))
            if row["exit_code"] != 0:
                break
        receipt["source_after"] = source_state(workspace, binding)
        load_plan(home, binding)
        if node.is_node(profile):
            node.validate_environment(profile["node_environment"], workspace)
        name = "vitest.xml" if node.is_node(profile) else "pytest.xml"
        raw = read_private(directory / "output" / name)
        fd = os.open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
        receipt["junit_sha256"] = sha(raw)
        receipt["counts"] = junit_counts(raw, plan.get("profile"))
    except (OSError, ValueError, subprocess.SubprocessError, ElementTree.ParseError) as exc:
        receipt["failure"] = {"type": type(exc).__name__, "message": str(exc)[:1000]}
    write_once(directory / "receipt.json", receipt)
    return int(
        receipt["failure"] is not None
        or receipt["counts"] is None
        or len(receipt["checks"]) != len(plan["checks"])
        or any(row["exit_code"] for row in receipt["checks"])
    )


if __name__ == "__main__":
    sys.exit(execute(Path(sys.argv[1]), Path(sys.argv[2])))
