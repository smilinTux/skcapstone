"""Operator-qualified native test plans and independently checked raw receipts."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from . import production_builder
from .production_policy import _unique_object
from .production_resources import active_resource_units, local_worker_admission

PREFIX = Path.home() / ".skenv"
MAX_OUTPUT = 4 * 1024 * 1024
TEST_FILES = tuple(
    "tests/" + name + ".py"
    for name in (
        "test_scheduler_decision",
        "test_skfleet_pool_v2_authority",
        "test_skfleet_claimability",
        "test_skfleet_terminal_review_skip",
        "test_skfleet_review_metadata",
        "test_skfleet_review_claim_release",
        "test_skfleet_assigned_review_observation",
        "test_fleet_duplicate_admission",
        "test_skfleet_awaiting_gates",
        "test_skfleet_provisional_opener",
        "test_seraph_dispatcher_path",
        "test_provisional_verdict_producer",
    )
)
BINDING_KEYS = frozenset(
    {
        "source_card",
        "source_owner",
        "source_claim_revision",
        "source_head",
        "source_tree",
        "source_revision",
        "criteria_sha256",
    }
)


class TestEvidenceError(ValueError):
    """Missing, ambiguous, stale or untrusted native test evidence."""

    __test__ = False


def sha(raw: bytes) -> str:
    """Hash exact persisted bytes."""
    return hashlib.sha256(raw).hexdigest()


def private_dir(path: Path, *, create=False) -> None:
    """Require an owned private real directory, including existing directories."""
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise TestEvidenceError("test evidence directory is not private")


def read_private(path: Path, maximum=MAX_OUTPUT) -> bytes:
    """Bound reads of owned single-link regular files without following symlinks."""
    private_dir(path.parent)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_mode & 0o077
            or info.st_nlink != 1
        ):
            raise TestEvidenceError("test evidence file is not private")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise TestEvidenceError("test evidence exceeds read bound")
    return raw


def write_once(path: Path, value: dict) -> None:
    """Publish an immutable native record, refusing any existing destination."""
    private_dir(path.parent)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def read_json(path: Path) -> dict:
    """Parse bounded native evidence without ambiguous duplicate JSON keys."""
    value = json.loads(read_private(path), object_pairs_hook=_unique_object)
    if not isinstance(value, dict):
        raise TestEvidenceError("test evidence is not an object")
    return value


def check_binding(binding: dict) -> None:
    """Validate exact keys before constructing any evidence path."""
    if (
        set(binding) != BINDING_KEYS
        or any(not isinstance(v, str) or not v for v in binding.values())
        or not re.fullmatch(r"[0-9a-f]{8}", binding["source_card"])
        or any(
            not re.fullmatch(r"[0-9a-f]{40}", binding[k]) for k in ("source_head", "source_tree")
        )
        or not re.fullmatch(r"[0-9a-f]{64}", binding["criteria_sha256"])
    ):
        raise TestEvidenceError("invalid exact source binding")


def approved_checks() -> list[dict]:
    """The initial qualified trial profile has no model-controlled command text."""
    python = str(PREFIX / "bin/python")
    return [
        {
            "id": "pytest",
            "argv": [
                python,
                "-m",
                "pytest",
                "-q",
                "-p",
                "no:cacheprovider",
                "--junitxml=/output/pytest.xml",
                *TEST_FILES,
            ],
        },
        {"id": "compile", "argv": [python, "-m", "py_compile", "scripts/fleet/skfleet-rotate.py"]},
        {
            "id": "lint",
            "argv": [python, "-m", "ruff", "check", "tests/test_skfleet_terminal_review_skip.py"],
        },
        {"id": "changelog", "argv": [python, "scripts/changelog_fragments.py", "--check"]},
    ]


def seal_plan(
    home: Path,
    binding: dict,
    workspace: Path,
    policy: dict,
    qualified_by: str,
    qualification_sha256: str,
) -> Path:
    """Operator entrypoint only: seal the fixed profile after explicit qualification."""
    check_binding(binding)
    source_state(workspace, binding)
    directory = home / "fleet/test-plans"
    private_dir(directory, create=True)
    path = directory / (binding["source_card"] + "-" + binding["source_head"] + ".json")
    value = {
        "schema": "skfleet.native-test-plan/v1",
        "binding": binding,
        "checks": approved_checks(),
        "qualified_by": qualified_by,
        "qualification_sha256": qualification_sha256,
        "python_sha256": sha((PREFIX / "bin/python").read_bytes()),
        "host": socket.gethostname().split(".")[0].lower(),
        "policy_sha256": production_builder.digest(policy),
    }
    if (
        not qualified_by
        or not re.fullmatch(r"[0-9a-f]{64}", qualification_sha256)
        or value["host"] != policy["authority_host"]
    ):
        raise TestEvidenceError("operator qualification is incomplete")
    write_once(path, value)
    return path


def load_plan(home: Path, binding: dict) -> tuple[dict, Path, str]:
    """Read the operator-installed plan and bind its exact approved runtime/profile."""
    check_binding(binding)
    path = (
        home
        / "fleet/test-plans"
        / (binding["source_card"] + "-" + binding["source_head"] + ".json")
    )
    raw = read_private(path)
    plan = json.loads(raw, object_pairs_hook=_unique_object)
    required = {
        "schema",
        "binding",
        "checks",
        "qualified_by",
        "qualification_sha256",
        "python_sha256",
        "host",
        "policy_sha256",
    }
    if (
        not isinstance(plan, dict)
        or set(plan) != required
        or plan["schema"] != "skfleet.native-test-plan/v1"
        or plan["binding"] != binding
        or plan["checks"] != approved_checks()
        or not isinstance(plan["qualified_by"], str)
        or not plan["qualified_by"]
        or any(
            not re.fullmatch(r"[0-9a-f]{64}", str(plan[k]))
            for k in ("qualification_sha256", "python_sha256", "policy_sha256")
        )
        or plan["python_sha256"] != sha((PREFIX / "bin/python").read_bytes())
        or plan["host"] != socket.gethostname().split(".")[0].lower()
    ):
        raise TestEvidenceError("operator test plan is invalid or stale")
    return plan, path, sha(raw)


def source_state(workspace: Path, binding: dict) -> dict:
    """Check actual Git source, including ignored/untracked files and linked worktrees."""

    def git(*args):
        result = subprocess.run(
            ["/usr/bin/git", "-C", str(workspace), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
            env={
                "PATH": "/usr/bin",
                "HOME": "/nonexistent",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.fsmonitor",
                "GIT_CONFIG_VALUE_0": "false",
            },
        )
        return result.stdout.strip()

    state = {"head": git("rev-parse", "HEAD"), "tree": git("rev-parse", "HEAD^{tree}")}
    if (
        state != {"head": binding["source_head"], "tree": binding["source_tree"]}
        or git("status", "--porcelain", "--untracked-files=all", "--ignored")
        or git("ls-files", "--stage").find("160000 ") >= 0
    ):
        raise TestEvidenceError("test source is changed, dirty, or contains submodules")
    return state


def run_directory(home: Path, plan_sha: str) -> Path:
    """Return the single immutable attempt directory for the exact plan bytes."""
    return home / "fleet/test-runs" / plan_sha


def junit_counts(raw: bytes) -> dict:
    """Recompute strict per-file coverage from raw JUnit, never reported totals alone."""
    if b"<!DOCTYPE" in raw or b"<!ENTITY" in raw:
        raise TestEvidenceError("JUnit entities are forbidden")
    root = ElementTree.fromstring(raw)
    cases = list(root.iter("testcase"))
    counts = {name: 0 for name in TEST_FILES}
    identities = set()
    for case in cases:
        if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
            raise TestEvidenceError("required tests failed, errored or skipped")
        classname = case.get("classname", "")
        matching = [
            name
            for name in TEST_FILES
            if any(
                classname == prefix or classname.startswith(prefix + ".")
                for prefix in (Path(name).stem, "tests." + Path(name).stem)
            )
        ]
        if len(matching) != 1:
            raise TestEvidenceError("JUnit includes an unexpected test file")
        name = matching[0]
        identity = (classname, case.get("name"))
        if not identity[1] or identity in identities:
            raise TestEvidenceError("JUnit testcase identity is missing or duplicated")
        identities.add(identity)
        counts[name] += 1
    suites = list(root.iter("testsuite"))
    if (
        not suites
        or any(int(s.get(k, "-1")) != 0 for s in suites for k in ("failures", "errors", "skipped"))
        or sum(int(s.get("tests", "-1")) for s in suites) != len(cases)
        or min(counts.values()) < 1
        or len(cases) < 226
        or counts["tests/test_skfleet_terminal_review_skip.py"] < 11
    ):
        raise TestEvidenceError("JUnit coverage does not meet the qualified plan")
    return {"total": len(cases), "per_file": counts, "failures": 0, "errors": 0, "skipped": 0}


def validate_test_receipt(home: Path, binding: dict, workspace: Path) -> dict:
    """Fail closed with one public error type for any invalid underlying evidence."""
    try:
        return _validate_test_receipt(home, binding, workspace)
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        ElementTree.ParseError,
        subprocess.SubprocessError,
    ) as exc:
        raise TestEvidenceError(str(exc)) from exc


def _validate_test_receipt(home: Path, binding: dict, workspace: Path) -> dict:
    """Rehash real output, argv, JUnit, source and independently observed unit exit."""
    plan, _, plan_sha = load_plan(home, binding)
    directory = run_directory(home, plan_sha)
    launch = read_json(directory / "launch.json")
    receipt = read_json(directory / "receipt.json")
    terminal = read_json(directory / "terminal.json")
    expected = source_state(workspace, binding)
    unit = production_builder.unit_name(launch, 1)
    policy = launch.get("policy", {})
    if (
        launch.get("binding") != binding
        or launch.get("request_id") != plan_sha
        or launch.get("workspace") != str(workspace.resolve())
        or launch.get("unit") != unit
        or launch.get("attempt") != 1
        or production_builder.digest(policy) != plan["policy_sha256"]
        or launch.get("production")
        != {"resources": policy.get("node_quotas", {}).get(plan["host"])}
        or launch.get("service_argv")
        != service_argv(
            launch,
            home
            / "fleet/test-plans"
            / (binding["source_card"] + "-" + binding["source_head"] + ".json"),
            directory,
            workspace,
        )
        or receipt.get("schema") != "skfleet.native-test-receipt/v1"
        or receipt.get("binding") != binding
        or receipt.get("plan_sha256") != plan_sha
        or receipt.get("source_before") != expected
        or receipt.get("source_after") != expected
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("invocation", "")))
        or terminal.get("unit") != unit
        or terminal.get("InvocationID") != receipt["invocation"]
        or terminal.get("ActiveState") not in {"inactive", "failed"}
        or terminal.get("ExecMainCode") != "1"
        or terminal.get("ExecMainStatus") != "0"
        or terminal.get("ExecMainPID") != str(receipt.get("pid"))
        or terminal.get("receipt_sha256") != sha(read_private(directory / "receipt.json"))
    ):
        raise TestEvidenceError("native unit, source or receipt custody mismatch")
    results = receipt.get("checks")
    if not isinstance(results, list) or len(results) != len(plan["checks"]):
        raise TestEvidenceError("required check receipts are missing")
    from .production_test_worker import sandbox_command

    for check, result in zip(plan["checks"], results):
        actual = sandbox_command(workspace, directory / "output", check["argv"])
        if (
            result.get("id") != check["id"]
            or result.get("argv") != check["argv"]
            or result.get("sandbox_argv") != actual
            or type(result.get("exit_code")) is not int
            or result.get("exit_code") != 0
            or result.get("output_sha256") != sha(read_private(directory / (check["id"] + ".log")))
        ):
            raise TestEvidenceError("native command or raw output mismatch")
    raw = read_private(directory / "pytest.xml")
    counts = junit_counts(raw)
    if receipt.get("junit_sha256") != sha(raw) or receipt.get("counts") != counts:
        raise TestEvidenceError("native JUnit evidence mismatch")
    return {
        "plan_sha256": plan_sha,
        "receipt_sha256": sha(read_private(directory / "receipt.json")),
        "source_head": binding["source_head"],
        "checks": results,
        "counts": counts,
        "receipt_path": str(directory / "receipt.json"),
    }


def service_argv(request: dict, plan_path: Path, directory: Path, workspace: Path) -> list[str]:
    """Use only the installed trusted runner under the existing native service quota API."""
    command = [
        str(PREFIX / "bin/python"),
        "-I",
        "-m",
        "skcapstone.fleet.production_test_worker",
        str(plan_path),
        str(directory),
    ]
    return production_builder.service_command(request, 1, command, workspace)


def observe_terminal(directory: Path, launch: dict) -> bool:
    """Persist exact independently observed systemd terminal custody once."""
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            launch["unit"],
            "--property=LoadState,ActiveState,InvocationID,ExecMainPID,ExecMainCode,ExecMainStatus",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("ActiveState") not in {"inactive", "failed"}:
        return False
    receipt = read_json(directory / "receipt.json")
    if (
        values.get("LoadState") != "loaded"
        or values.get("InvocationID") != receipt.get("invocation")
        or values.get("ExecMainPID") != str(receipt.get("pid"))
    ):
        raise TestEvidenceError("test service exact terminal custody is unavailable")
    values.update(
        unit=launch["unit"], receipt_sha256=sha(read_private(directory / "receipt.json"))
    )
    write_once(directory / "terminal.json", values)
    return True


def run_or_read_tests(home: Path, binding: dict, workspace: Path, policy: dict) -> dict | None:
    """Launch once after caller proves source/reviewer termination, or verify real tests.

    Missing qualification and still-running services return None. A lost launch
    acknowledgement never starts another unit; native custody must resolve it.
    The caller owns exact current claim/revision checks before this launch.
    """
    try:
        plan, plan_path, plan_sha = load_plan(home, binding)
    except FileNotFoundError:
        return None
    if plan["policy_sha256"] != production_builder.digest(policy):
        raise TestEvidenceError("test quota policy changed after qualification")
    workspace = workspace.resolve()
    source_state(workspace, binding)
    root = home / "fleet/test-runs"
    private_dir(root, create=True)
    directory = run_directory(home, plan_sha)
    lock_fd = os.open(root / ".admission.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        private_dir(directory, create=True)
        if (directory / "launch.json").exists():
            launch = read_json(directory / "launch.json")
            if (directory / "terminal.json").exists() or observe_terminal(directory, launch):
                return validate_test_receipt(home, binding, workspace)
            return None
        ready, _ = local_worker_admission(policy, plan["host"], active_resource_units(home))
        if not ready:
            return None
        private_dir(directory / "output", create=True)
        request = {
            "card_id": binding["source_card"],
            "request_id": plan_sha,
            "attempt": 1,
            "binding": binding,
            "workspace": str(workspace),
            "policy": policy,
            "production": {"resources": policy["node_quotas"][plan["host"]]},
        }
        request["unit"] = production_builder.unit_name(request, 1)
        argv = service_argv(request, plan_path, directory, workspace)
        request["service_argv"] = argv
        write_once(directory / "launch.json", request)
        # Persist before Popen: interruption or lost acknowledgement cannot replay launch.
        subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return None
