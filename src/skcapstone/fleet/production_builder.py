"""Production builder policy and resource-bounded native service custody."""

from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import subprocess
from pathlib import Path

from . import production_routes, scheduler, store
from .production_policy import load_production_policy, require_destination


def policy() -> dict | None:
    """Require the shared policy and its explicit authority in production mode."""
    path = os.environ.get("SKFLEET_PRODUCTION_POLICY")
    if path is None:
        return None
    authority = os.environ.get("SKFLEET_AUTHORITY_HOST", "")
    if not path or not authority:
        raise ValueError("production builder policy authority is missing")
    return load_production_policy(Path(path), host=authority)


def digest(value: dict) -> str:
    """Bind only validated canonical policy content, never a caller path."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def node_binding(paths, node: str, value: dict) -> dict:
    """Resolve a node's explicit host identity and qualified worker resources."""
    spec = store.read_spec(paths, "node", node) or {}
    host = (spec.get("labels") or {}).get("host")
    limits = require_destination(value, host)
    if not isinstance(host, str) or not isinstance(limits, dict):
        raise ValueError("production builder node lacks qualified worker resources")
    return {
        "authority": value["authority_host"],
        "policy_sha256": digest(value),
        "host": host,
        "resources": dict(limits),
    }


def workload(card: str, binding: dict):
    """Use real per-worker resource requests in the existing scheduler."""
    limits = binding["resources"]
    return scheduler.Workload(
        "job",
        card,
        requests={
            "cores": limits["cpu_quota_percent"] / 100,
            "ram_gb": limits["memory_max_bytes"] / (1024**3),
        },
    )


def ready_nodes(paths, views: list, value: dict, card: str) -> list:
    """Retain native readiness, taints and resource checks without count caps."""
    result = []
    for view in views:
        try:
            binding = node_binding(paths, view.name, value)
        except ValueError:
            continue
        if scheduler.feasible(view, workload(card, binding)) is None:
            result.append(view)
    return result


def route_binding(
    value: dict, card: str, route: str, labels: list[str], *, family=None, model=None
) -> dict:
    """Select the smallest qualified route from actual card and gateway requirements."""
    eligible = [
        row
        for row in production_routes.candidates(value, route, labels)
        if family in (None, row["family"])
        and ("qwen-first" not in labels or row["family"] == "qwen")
        and model in (None, row["model"])
    ]
    if not eligible:
        raise production_routes.RouteUnavailableError(
            "production card has no currently qualified gateway route"
        )
    sizes = {"S": 0, "M": 1, "L": 2, "XL": 3}
    smallest = min(sizes[row["size_class"]] for row in eligible)
    eligible = [row for row in eligible if sizes[row["size_class"]] == smallest]
    return eligible[int(hashlib.sha256(card.encode()).hexdigest(), 16) % len(eligible)]


def validate_request(paths, node: str, request: dict, *, local: bool = False) -> dict | None:
    """Refuse stale policy, node, route or resource bindings before a launch."""
    value = policy()
    bound = request.get("production")
    if value is None:
        if bound is not None:
            raise ValueError("production request requires production policy")
        return None
    expected = node_binding(paths, node, value)
    if not isinstance(bound, dict) or any(
        bound.get(key) != item for key, item in expected.items()
    ):
        raise ValueError("production request policy changed")
    expected.update(
        route_binding(
            value,
            request["card_id"],
            request["logical_route"],
            request["labels"],
            family=bound.get("family"),
            model=bound.get("model"),
        )
    )
    if bound != expected:
        raise ValueError("production request policy changed")
    if local and expected["host"] != socket.gethostname().split(".")[0].lower():
        raise ValueError("production request belongs to another execution host")
    return expected


def unit_name(request: dict, attempt: int) -> str:
    """Give every request attempt its own non-reusable service identity."""
    card, token = request["card_id"], request["request_id"]
    if not re.fullmatch(r"[0-9a-f]{8}", card) or not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("production service request identity invalid")
    if type(attempt) is not int or attempt <= 0:
        raise ValueError("production service attempt invalid")
    return f"skfleet-builder-{card}-{token}-{attempt}.service"


def service_command(request: dict, attempt: int, command: list[str], workspace: Path) -> list[str]:
    """Apply per-worker quotas to the actual service, without a worker ceiling."""
    limits = request["production"]["resources"]
    return [
        "/usr/bin/systemd-run",
        "--user",
        "--quiet",
        "--wait",
        "--pipe",
        "--unit=" + unit_name(request, attempt),
        "--working-directory=" + str(workspace),
        "--property=CPUQuota=" + str(limits["cpu_quota_percent"]) + "%",
        "--property=MemoryMax=" + str(limits["memory_max_bytes"]),
        "--property=TasksMax=" + str(limits["tasks_max"]),
        "--property=RuntimeMaxSec=" + str(limits["runtime_max_seconds"]),
        "--property=KillMode=control-group",
        "--property=UMask=0077",
        "--",
        *command,
    ]


def service_state(status: dict, process=None) -> tuple[bool | None, int | None]:
    """Unknown unit or invocation custody never authorizes releasing a claim."""
    unit = status.get("unit")
    try:
        if unit != unit_name(status, status.get("attempt")):
            return None, None
    except (KeyError, ValueError):
        return None, None
    if not isinstance(unit, str) or not re.fullmatch(
        r"skfleet-builder-[0-9a-f]{8}-[0-9a-f]{64}-[1-9][0-9]*\.service", unit
    ):
        return None, None
    try:
        result = subprocess.run(
            [
                "systemctl",
                "--user",
                "show",
                unit,
                "--property=LoadState,ActiveState,InvocationID,ExecMainStatus",
                "--no-pager",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    if result.returncode:
        return None, None
    values = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    invocation = values.get("InvocationID", "")
    expected = status.get("invocation")
    if expected and invocation and invocation != expected:
        return None, None
    if values.get("LoadState") == "loaded" and values.get("ActiveState") in {
        "active",
        "activating",
        "deactivating",
    }:
        if re.fullmatch(r"[0-9a-f]{32}", invocation):
            status["invocation"] = invocation
        return True, None
    code = process.poll() if process is not None else None
    if code is not None and code >= 0:
        # This exact systemd-run --wait process has observed its service exit.
        return False, int(code)
    if expected and invocation == expected and values.get("ActiveState") in {"inactive", "failed"}:
        try:
            return False, int(values["ExecMainStatus"])
        except (KeyError, ValueError):
            pass
    return None, None


def review_worker_path(home: Path, inherited: str) -> str:
    """Prefer the installed user tools over system binaries in a clean worker."""
    return os.pathsep.join((str(home / ".skenv/bin"), str(home / ".local/bin"), inherited))


def review_wrapper_path() -> str:
    """Resolve the wrapper installed beside this process before consulting PATH."""
    import shutil
    import sys

    name = "skfleet-worker-wrapper.py"
    venv_path = Path(sys.executable).parent / name
    if venv_path.is_file():
        return str(venv_path)

    search_path = os.environ.get("PATH", os.defpath)
    wrapper = shutil.which(name, path=search_path)
    if wrapper and Path(wrapper).is_file():
        return wrapper

    searched = [str(venv_path)]
    searched.extend(
        os.path.abspath(os.path.join(directory or os.curdir, name))
        for directory in search_path.split(os.pathsep)
    )
    raise ValueError("managed review wrapper unavailable; tried: " + ", ".join(searched))


def launch_review(paths, home, request, card, handoff, workspace, *, launcher=None):
    """Launch the existing production wrapper under destination-native custody."""
    import shutil
    import sys
    import time

    from ..seat_runtime import append_review_launch_receipt
    from . import builder_dispatch, review_dispatch
    from .production_admission import MARKER, _digest, reserve_launch, start_reserved, unit_state
    from .production_brief import production_source_review_brief
    from .production_receipts import persist_production_snapshot
    from .production_review_finish import read_json
    from .worker_git import identity, preflight

    owner, claim = request["reviewer"], card.meta["_claim_revision"]
    bound = request["production"]
    host, lane = bound["host"], bound["family"]
    unit = "skfleet-worker-" + lane + "-" + card.id + ".service"
    worker = os.environ.get("SKFLEET_PI") or shutil.which("pi")
    wrapper = review_wrapper_path()
    if not worker:
        raise ValueError("managed review worker runtime unavailable: pi not found")
    brief = production_source_review_brief(
        card_id=card.id,
        owner=owner,
        claim_revision=claim,
        workspace=str(workspace),
        source_head=request["source"]["head"],
        core=card.model_dump(mode="json"),
        labels=card.labels,
    )
    child = [
        "/usr/bin/env",
        "-i",
        "HOME=" + str(Path.home()),
        "PATH=" + review_worker_path(Path.home(), os.environ.get("PATH", "/usr/bin:/bin")),
        "LANG=C.UTF-8",
        "SKCAPSTONE_HOME=" + str(home),
        "SKAGENT=" + owner,
        "SKCAPSTONE_AGENT=" + owner,
        "SKFLEET_CARD_ID=" + card.id,
        "SKFLEET_CLAIM_REVISION=" + claim,
        "SKFLEET_SESSION_ID=" + owner,
        "SKFLEET_WORKSPACE=" + str(workspace),
        *(key + "=" + value for key, value in identity(owner).items()),
        worker,
        "--no-approve",
        "--extension",
        builder_dispatch._guard_path(),
        "--name",
        owner,
        "--provider",
        "skgateway",
        "--model",
        bound["model"],
        "--thinking",
        "off",
        "--no-context-files",
        "--no-skills",
        "--tools",
        builder_dispatch._WORKER_TOOLS,
        "-p",
        brief,
    ]
    preflight(child, workspace, owner)
    observed = production_routes.snapshot(request["policy"])
    route = dict(
        logical_route=request["logical_route"],
        provider="skgateway",
        capacity_domains=[bound["capacity_domain"]],
        model_or_bucket=bound["model"],
        production_snapshot=persist_production_snapshot(home, observed),
    )
    logs = paths.root / "review-logs"
    logs.mkdir(parents=True, exist_ok=True, mode=0o700)
    inner = [
        sys.executable,
        wrapper,
        "--card",
        card.id,
        "--owner",
        owner,
        "--claim-revision",
        claim,
        "--host",
        host,
        "--lane",
        lane,
        "--model",
        bound["model"],
        "--logical-route",
        request["logical_route"],
        "--provider",
        "skgateway",
        "--capacity-domain",
        bound["capacity_domain"],
        "--source-repository",
        request["repository"],
        "--source-base-revision",
        request["source"]["head"],
        "--stdout",
        str(logs / (request["request_id"] + ".log")),
        "--evidence-dir",
        str(Path(home) / "evidence/worker-exits"),
        "--live-snapshot",
        str(paths.root / "live" / (host + ".json")),
        "--session",
        owner,
        "--worker-executable",
        worker,
        "--",
        *review_child_command(home, owner, card.id, claim, child),
    ]
    command = service_command(request, 1, inner, workspace)
    command = [("--unit=" + unit) if arg.startswith("--unit=") else arg for arg in command]
    command.insert(1, "--setenv=SKFLEET_REVIEW_REQUEST=" + request["request_id"])
    command.insert(1, "--setenv=SKFLEET_REVIEW_NODE=" + request["node"])
    command.insert(
        1, "--setenv=SKFLEET_PRODUCTION_POLICY=" + os.environ["SKFLEET_PRODUCTION_POLICY"]
    )
    command.insert(1, "--setenv=SKFLEET_AUTHORITY_HOST=" + request["production"]["authority"])
    command.insert(1, "--setenv=SKCAPSTONE_HOME=" + str(home))
    binding = dict(
        card_id=card.id,
        owner=owner,
        claim_revision=claim,
        request_id=request["request_id"],
        request_sha256=digest(request),
        policy_sha256=bound["policy_sha256"],
        work_kind="review",
    )
    review_dispatch.validate_request(paths, home, request["node"], request, claimed=True)
    if _review_card(home, card.id).meta.get("_claim_revision") != claim:
        raise ValueError("review claim changed before reservation")
    production_routes.preflight(request["policy"], bound)
    argv = reserve_launch(home, request["policy"], host, unit, binding, command)
    reservation = next(
        arg.split("=", 2)[2] for arg in argv if arg.startswith("--setenv=" + MARKER + "=")
    )
    intent = read_json(
        Path(home) / "fleet/resource-admission" / host / reservation / "intent.json"
    )
    execution = dict(
        request_id=request["request_id"],
        request_sha256=digest(request),
        authority=bound["authority"],
        node=request["node"],
        host=host,
        policy_sha256=bound["policy_sha256"],
        work_kind="review",
        unit=unit,
        admission_id=reservation,
        admission_sha256=_digest(intent),
        invocation="",
    )
    # Publish custody before spawning. A crash or any exception hereafter holds
    # the exact claim and intent; duplicate consumption never retries it.
    pending = dict(
        owner=owner,
        claim_revision=claim,
        claim_released=False,
        execution=execution,
        admission=intent,
        route_snapshot=observed,
        workspace=str(workspace),
        command=command,
    )
    builder_dispatch._write_status(paths, request["node"], request, "admission-pending", **pending)

    def spawn(command):
        if launcher is not None:
            return launcher(command, workspace)
        return subprocess.Popen(
            command,
            cwd=workspace,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    process = start_reserved(home, host, argv, spawn)
    builder_dispatch._PROCESSES[request["request_id"]] = process
    # A successful Popen is not a service acknowledgment.
    for _ in range(20):
        state = unit_state(unit)
        if (
            state.get("Id") == unit
            and state.get("LoadState") == "loaded"
            and state.get(MARKER) == reservation
            and re.fullmatch(r"[0-9a-f]{32}", state.get("InvocationID", ""))
            and int(state.get("MemoryMax", "0")) == bound["resources"]["memory_max_bytes"]
        ):
            break
        time.sleep(0.05)
    else:
        raise ValueError("native review service acknowledgment pending")
    execution["invocation"] = state["InvocationID"]
    pending["execution"] = execution
    pending["acknowledgment"] = {
        k: state[k] for k in ("Id", "LoadState", MARKER, "InvocationID", "MemoryMax")
    }
    status = builder_dispatch._write_status(paths, request["node"], request, "running", **pending)
    append_review_launch_receipt(
        home,
        handoff,
        actor=owner,
        claim_revision=claim,
        launched=True,
        route_identity=route,
        execution=execution,
    )
    return status


def _review_card(home, card):
    """Read exact final claim through the native fold."""
    from skcoord.card_store import CardStore

    return CardStore(home).fold(card)


def review_child_command(home, owner, card, claim, child):
    """Retain the wrapper's attributed startup heartbeat without claiming progress."""
    import shlex

    heartbeat = """import json,os,sys,time
from pathlib import Path
home,owner,card,claim=sys.argv[1:]
root=Path(home)/'fleet/beats';root.mkdir(parents=True,exist_ok=True,mode=0o700)
path=root/(owner+'.json');temporary=root/(owner+'.tmp')
while True:
    row=dict(owner=owner,card_id=card,claim_revision=claim,session_id=owner,
             pid=os.getppid(),invocation_id=os.environ.get('INVOCATION_ID',''),
             emitter='wrapper',disposition='RUNNING',proves='shell-liveness',beat_at=time.time())
    with open(temporary,'w') as stream: json.dump(row,stream)
    os.chmod(temporary,0o600);os.replace(temporary,path)
    time.sleep(30)
"""
    beat = ["/usr/bin/python3", "-c", heartbeat, str(home), owner, card, claim]
    shell = (
        shlex.join(beat) + " </dev/null >/dev/null 2>&1 & beat=$!; "
        'trap \'kill "$beat" 2>/dev/null || true; wait "$beat" 2>/dev/null || true\' EXIT; '
        + shlex.join(child)
        + '; rc=$?; exit "$rc"'
    )
    return ["/bin/bash", "-c", shell]
