"""Controller-owned test service launch and independently validated raw receipts."""

from __future__ import annotations

import fcntl
import os
import re
import subprocess
from pathlib import Path
from xml.etree import ElementTree

from . import production_builder
from . import production_test_plan as test_plan
from .production_admission import (
    AdmissionError,
    finalize_successful_launch,
    reserve_launch,
    reserved_command,
)
from .production_test_plan import (
    BINDING_KEYS as BINDING_KEYS,
)
from .production_test_plan import (
    MAX_OUTPUT as MAX_OUTPUT,
)
from .production_test_plan import (
    PREFIX as PREFIX,
)
from .production_test_plan import (
    TEST_FILES as TEST_FILES,
)
from .production_test_plan import (
    TestEvidenceError as TestEvidenceError,
)
from .production_test_plan import (
    approved_checks as approved_checks,
)
from .production_test_plan import (
    check_binding as check_binding,
)
from .production_test_plan import (
    junit_counts as junit_counts,
)
from .production_test_plan import (
    load_plan as load_plan,
)
from .production_test_plan import (
    private_dir as private_dir,
)
from .production_test_plan import (
    read_json as read_json,
)
from .production_test_plan import (
    read_private as read_private,
)
from .production_test_plan import (
    run_directory as run_directory,
)
from .production_test_plan import (
    seal_plan as seal_plan,
)
from .production_test_plan import (
    sha as sha,
)
from .production_test_plan import (
    source_state as source_state,
)
from .production_test_plan import (
    write_once as write_once,
)

_SERVICE_PROCESSES = {}


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
    plan, plan_path, plan_sha = load_plan(home, binding, allow_completed=True)
    directory = run_directory(home, plan_sha)
    launch = read_json(directory / "launch.json")
    receipt = read_json(directory / "receipt.json")
    terminal = read_json(directory / "terminal.json")
    expected = source_state(workspace, binding)
    from . import production_test_composite as composite

    if plan.get("profile"):
        composite.validate_source(plan["profile"], workspace)
    unit = production_builder.unit_name(launch, 1)
    policy = launch.get("policy", {})
    expected_argv = service_argv(
        launch,
        plan_path,
        directory,
        workspace,
    )
    if launch.get("admission_protocol") == 1:
        expected_argv = reserved_command(
            home,
            policy,
            plan["host"],
            unit,
            _admission_binding(binding, plan_sha),
            expected_argv,
        )
    if (
        launch.get("binding") != binding
        or launch.get("request_id") != plan_sha
        or launch.get("workspace") != str(workspace.resolve())
        or launch.get("unit") != unit
        or launch.get("attempt") != 1
        or test_plan.execution_policy_fingerprint(policy) != plan["policy_sha256"]
        or launch.get("production")
        != {"resources": policy.get("node_quotas", {}).get(plan["host"])}
        or launch.get("service_argv") != expected_argv
        or receipt.get("schema") != "skfleet.native-test-receipt/v1"
        or receipt.get("binding") != binding
        or receipt.get("plan_sha256") != plan_sha
        or receipt.get("source_before") != expected
        or receipt.get("source_after") != expected
        or receipt.get("failure") is not None
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("invocation", "")))
        or terminal.get("unit") != unit
        or terminal.get("InvocationID") != receipt["invocation"]
        or terminal.get("LoadState") != "loaded"
        or terminal.get("ActiveState") != "active"
        or terminal.get("SubState") != "exited"
        or not terminal_cgroup(terminal)
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
        actual = sandbox_command(
            workspace, directory / "output", check["argv"], plan.get("profile")
        )
        if (
            result.get("id") != check["id"]
            or result.get("argv") != check["argv"]
            or result.get("sandbox_argv") != actual
            or type(result.get("exit_code")) is not int
            or result.get("exit_code") != 0
            or result.get("output_sha256") != sha(read_private(directory / (check["id"] + ".log")))
        ):
            raise TestEvidenceError("native command or raw output mismatch")
    reports = {
        name: read_private(directory / name)
        for name in composite.report_names(plan.get("profile"))
    }
    selection = None

    profile = plan.get("profile")
    if composite.python_selection(profile):
        selection_raw = read_private(directory / "selection.json")
        if receipt.get("selection_sha256") != sha(selection_raw):
            raise TestEvidenceError("native selection evidence changed")
        selection = read_json(directory / "selection.json")
    report_digest, counts = composite.evidence(reports, profile, selection)
    if receipt.get("junit_sha256") != report_digest or receipt.get("counts") != counts:
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
    argv = production_builder.service_command(request, 1, command, workspace)
    position = argv.index("--")
    argv[position:position] = ["--property=RemainAfterExit=yes", "--property=Type=exec"]
    return argv


def terminal_cgroup(values: dict) -> bool:
    """Require an exited main process and either empty or removed owned cgroup."""
    if values.get("MainPID") != "0":
        return False
    group, tasks = values.get("ControlGroup"), values.get("TasksCurrent")
    return (group == "" and tasks == "[not set]") or (
        isinstance(group, str) and group.startswith("/") and tasks == "0"
    )


def stop_retained(directory: Path, launch: dict) -> None:
    """Stop only the exact recorded retained invocation; a lost stop ack is safe."""
    terminal = read_json(directory / "terminal.json")
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            launch["unit"],
            "--property=LoadState,ActiveState,SubState,InvocationID,MainPID,ControlGroup,TasksCurrent",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("LoadState") == "not-found":
        process = _SERVICE_PROCESSES.pop(launch["unit"], None)
        if process is not None:
            process.wait(timeout=5)
        return
    if (
        values.get("LoadState") != "loaded"
        or values.get("InvocationID") != terminal["InvocationID"]
        or (values.get("ActiveState"), values.get("SubState"))
        not in {("active", "exited"), ("failed", "failed"), ("inactive", "dead")}
        or not terminal_cgroup(values)
    ):
        raise TestEvidenceError("refusing stop without exact retained invocation")
    if values.get("ActiveState") != "inactive":
        subprocess.run(
            ["/usr/bin/systemctl", "--user", "stop", launch["unit"]],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
    process = _SERVICE_PROCESSES.pop(launch["unit"], None)
    if process is not None:
        process.wait(timeout=5)


def observe_terminal(directory: Path, launch: dict) -> bool:
    """Persist exact independently observed systemd terminal custody once."""
    result = subprocess.run(
        [
            "/usr/bin/systemctl",
            "--user",
            "show",
            launch["unit"],
            "--property=LoadState,ActiveState,SubState,InvocationID,ExecMainPID,ExecMainCode,"
            "ExecMainStatus,MainPID,ControlGroup,TasksCurrent",
            "--no-pager",
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=True,
    )
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if values.get("ActiveState") in {"activating", "deactivating"} or (
        values.get("ActiveState") == "active" and values.get("SubState") != "exited"
    ):
        return False
    receipt = (
        read_json(directory / "receipt.json") if (directory / "receipt.json").exists() else {}
    )
    if (
        values.get("LoadState") != "loaded"
        or not re.fullmatch(r"[0-9a-f]{32}", values.get("InvocationID", ""))
        or not re.fullmatch(r"[1-9][0-9]*", values.get("ExecMainPID", ""))
        or (
            receipt
            and (
                values.get("InvocationID") != receipt.get("invocation")
                or values.get("ExecMainPID") != str(receipt.get("pid"))
            )
        )
        or (not receipt and values.get("ExecMainStatus") == "0")
        or (values.get("ActiveState"), values.get("SubState"))
        not in {("active", "exited"), ("failed", "failed")}
        or not terminal_cgroup(values)
    ):
        raise TestEvidenceError("test service exact terminal custody is unavailable")
    values.update(
        unit=launch["unit"],
        receipt_sha256=(sha(read_private(directory / "receipt.json")) if receipt else ""),
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
        plan, plan_path, plan_sha = load_plan(home, binding, allow_completed=True)
    except FileNotFoundError:
        return None
    if plan["policy_sha256"] != test_plan.execution_policy_fingerprint(policy):
        raise TestEvidenceError("test quota policy changed after qualification")
    workspace = workspace.resolve()
    source_state(workspace, binding)
    root = home / "fleet/test-runs"
    private_dir(root, create=True)
    directory = run_directory(home, plan_sha)
    private_dir(directory, create=True)
    lock_fd = os.open(directory / ".run.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if load_plan(home, binding, allow_completed=True)[2] != plan_sha:
            raise TestEvidenceError("test plan generation changed before launch")
        private_dir(directory, create=True)
        if (directory / "launch.json").exists():
            launch = read_json(directory / "launch.json")
            if (directory / "terminal.json").exists() or observe_terminal(directory, launch):
                terminal = read_json(directory / "terminal.json")
                if terminal.get("ExecMainStatus") == "0":
                    from skcoord.card_store import CardStore

                    from ..seraph_review_cardstore import card_revision

                    card = CardStore(home).fold(binding["source_card"])
                    if card is None:
                        raise TestEvidenceError("test source claim is unavailable")
                    finalize_successful_launch(
                        home,
                        policy,
                        plan["host"],
                        launch["unit"],
                        _admission_binding(binding, plan_sha),
                        service_argv(launch, plan_path, directory, workspace),
                        invocation=terminal["InvocationID"],
                        expected_card_revision=card_revision(card),
                    )
                stop_retained(directory, launch)
                return validate_test_receipt(home, binding, workspace)
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
        try:
            argv = reserve_launch(
                home,
                policy,
                plan["host"],
                request["unit"],
                _admission_binding(binding, plan_sha),
                argv,
            )
        except AdmissionError:
            return None
        request["service_argv"] = argv
        request["admission_protocol"] = 1
        write_once(directory / "launch.json", request)
        # Persist before Popen: interruption or lost acknowledgement cannot replay launch.
        from .production_admission import start_reserved

        _SERVICE_PROCESSES[request["unit"]] = start_reserved(
            home,
            plan["host"],
            argv,
            lambda command: subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            ),
        )
        return None


def _admission_binding(binding: dict, plan_sha: str) -> dict:
    """Bind resource intent and receipt verification to the same exact source."""
    return {
        "card_id": binding["source_card"],
        "owner": binding["source_owner"],
        "claim_revision": binding["source_claim_revision"],
        "plan_sha256": plan_sha,
    }
