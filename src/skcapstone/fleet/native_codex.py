"""Retained-owner Codex execution: fleet isolation owns the sandbox boundary.

The coding container shares networking for the approved provider transport.
Required candidate tests remain in the separate sealed host-side test executor.
This explicit operator route does not replace automatic Pi/SKGateway dispatch.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from . import production_admission as admission
from . import production_test_profile as profile
from .production_policy import require_destination

GUARD = """import os,pathlib,sys
uid,gid=map(int,sys.argv[1:3])
status=pathlib.Path('/proc/self/status').read_text()
def field(key):
    rows=(line for line in status.splitlines() if line.startswith(key+':'))
    return next(rows).split(':',1)[1].strip()
valid=(pathlib.Path('/proc/self/uid_map').read_text().split()==['0',str(uid),'1']
       and pathlib.Path('/proc/self/gid_map').read_text().split()==['0',str(gid),'1']
       and bool(os.statvfs('/').f_flag & os.ST_RDONLY)
       and not bool(os.statvfs('/work').f_flag & os.ST_RDONLY)
       and field('NoNewPrivs')=='1' and int(field('CapEff'),16)==0)
if not valid: sys.exit('Codex requires the enforced fleet outer sandbox')
os.execv(sys.argv[3],sys.argv[3:])
"""


def _path(path: Path, *, directory: bool = False, private: bool = False) -> Path:
    """Refuse redirected, foreign-owned or world-writable operator inputs.

    Installed runtime files may use the operator's shared group. Workspace and
    authentication inputs must additionally deny every group and other access.
    """
    path = Path(path)
    try:
        resolved = path.resolve(strict=True)
        metadata = path.stat()
    except OSError as exc:
        raise ValueError("Codex sandbox input is unavailable") from exc
    if not path.is_absolute() or resolved != path:
        raise ValueError("Codex sandbox input is redirected")
    correct_kind = stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)
    if not correct_kind or metadata.st_uid != os.getuid() or metadata.st_mode & 0o002:
        raise ValueError("Codex sandbox input ownership or permissions differ")
    if private and metadata.st_mode & 0o077:
        raise ValueError("Codex authentication input must remain private")
    return path


def sandbox_command(
    workspace: Path, executable: Path, auth: Path, *, model: str, owner: str
) -> list[str]:
    """Build one outer container; there is no unsandboxed host fallback.

    Authentication is the one explicit read-only credential input, never copied
    into the workspace. The worker can read it; protected tasks are not admitted.
    No host home, coordination store, user bus or private packet is mounted.
    """
    workspace = _path(workspace, directory=True, private=True)
    executable = _path(executable)
    auth = _path(auth, private=True)
    if executable.name != "codex" or executable.parent.name != "bin":
        raise ValueError("Codex must be the installed native bin/codex executable")
    runtime = _path(executable.parent.parent, directory=True)
    if not os.access(executable, os.X_OK) or not re.fullmatch(r"[A-Za-z0-9_.-]+", model):
        raise ValueError("Codex executable or explicit model is invalid")
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", owner):
        raise ValueError("Codex retained owner is invalid")
    command = [
        "/usr/bin/bwrap",
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--uid",
        "0",
        "--gid",
        "0",
        "--cap-drop",
        "ALL",
        "--clearenv",
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for directory in ("/lib", "/lib64"):
        if Path(directory).exists():
            command += ["--ro-bind", directory, directory]
    for source in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs"):
        if Path(source).exists():
            command += ["--ro-bind", str(Path(source).resolve()), source]
    command += [
        "--ro-bind",
        str(runtime),
        "/opt/skfleet-codex",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
        "--dir",
        "/tmp/codex",
        "--ro-bind",
        str(auth),
        "/tmp/codex/auth.json",
        "--bind",
        str(workspace),
        "/work",
        "--chdir",
        "/work",
        "--setenv",
        "HOME",
        "/tmp",
        "--setenv",
        "CODEX_HOME",
        "/tmp/codex",
        "--setenv",
        "PATH",
        "/opt/skfleet-codex/bin:/opt/skfleet-codex/codex-path:/usr/bin:/bin",
        "--setenv",
        "LANG",
        "C.UTF-8",
        "--setenv",
        "SKAGENT",
        owner,
        "--setenv",
        "SKCAPSTONE_AGENT",
        owner,
        "--setenv",
        "GIT_CONFIG_NOSYSTEM",
        "1",
        "--setenv",
        "GIT_CONFIG_GLOBAL",
        "/dev/null",
        "--setenv",
        "GIT_AUTHOR_NAME",
        owner,
        "--setenv",
        "GIT_COMMITTER_NAME",
        owner,
        "--setenv",
        "GIT_AUTHOR_EMAIL",
        owner + "@skfleet.local",
        "--setenv",
        "GIT_COMMITTER_EMAIL",
        owner + "@skfleet.local",
        "--remount-ro",
        "/",
        "--",
        "/usr/bin/python3",
        "-I",
        "-c",
        GUARD,
        str(os.getuid()),
        str(os.getgid()),
        "/opt/skfleet-codex/bin/codex",
        "exec",
        "--ignore-user-config",
        "--model",
        model,
        "--sandbox",
        "danger-full-access",
        "-c",
        'approval_policy="never"',
        "-c",
        'model_reasoning_effort="high"',
        "--cd",
        "/work",
        "--json",
        "--output-last-message",
        "/work/NATIVE-FINAL.md",
        "-",
    ]
    return command


def require_native_unit(home: Path, policy: dict, unit: str, binding: dict) -> dict:
    """Prove this exact process owns a consumed native admission and test profile."""
    marker = os.environ.get(admission.MARKER, "")
    invocation = os.environ.get("INVOCATION_ID", "")
    if not re.fullmatch(r"[0-9a-f]{64}", marker) or not re.fullmatch(r"[0-9a-f]{32}", invocation):
        raise ValueError("Codex requires its native admitted unit")
    state = admission.unit_state(unit, terminal=True)
    if (
        state.get("MainPID") != str(os.getpid())
        or state.get("InvocationID") != invocation
        or state.get(admission.MARKER) != marker
        or (state.get("ActiveState"), state.get("SubState")) != ("active", "running")
    ):
        raise ValueError("Codex requires its exact native admitted unit process")
    host = os.uname().nodename.split(".")[0]
    limits = require_destination(policy, host)
    if state.get("MemoryMax") != str(limits["memory_max_bytes"]):
        raise ValueError("Codex native resource allowance changed")
    root = Path(home) / "fleet/resource-admission" / host / marker
    intent = admission.read_json(root / "intent.json")
    start = admission.read_json(root / "start.json")
    if (
        admission._reservation_id(intent) != marker
        or intent["binding"] != binding
        or intent["unit"] != unit
        or intent["resources"] != limits
        or start
        != {
            "schema": "skfleet.resource-start/v1",
            "reservation_id": marker,
            "binding": binding,
            "argv_sha256": intent["argv_sha256"],
        }
    ):
        raise ValueError("Codex native admission custody changed")
    with admission.card_mutation_lock(home, binding["card_id"]):
        admission._require_claim(home, binding)
        core = admission.CardStore(home).fold(binding["card_id"]).model_dump(mode="json")
        labels = {str(label).lower() for label in core["labels"]}
        if not {"source-only", "codex-only", "dispatch-approved"} <= labels or labels & {
            "private",
            "protected-data",
            "sensitive",
            "human-gate",
            "do-not-claim",
        }:
            raise ValueError("Codex operator route is restricted to authorized public source work")
        _path(Path(home) / "agents" / binding["owner"], directory=True, private=True)
        profile.preflight(home, core, labels, policy)
        return core


def require_source(workspace: Path, core: dict) -> None:
    """Retain the exact registered repository and source ancestor, including partial work."""
    expected = profile.contract(core)
    bases = {
        section.get("base_revision")
        for section in (core["meta"], core["links"])
        if section.get("base_revision")
    }
    if len(bases) != 1 or not re.fullmatch(r"[0-9a-f]{40}", next(iter(bases))):
        raise ValueError("Codex card lacks an exact source base")
    environment = {
        "PATH": "/usr/bin:/bin",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    command = [
        "/usr/bin/git",
        "-C",
        str(workspace),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=/dev/null",
    ]
    origin = subprocess.run(
        [*command, "remote", "get-url", "origin"],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
    )
    ancestor = subprocess.run(
        [*command, "merge-base", "--is-ancestor", next(iter(bases)), "HEAD"],
        env=environment,
        capture_output=True,
        timeout=10,
    )
    if origin.returncode or origin.stdout.strip() != expected["repository"] or ancestor.returncode:
        raise ValueError("Codex workspace differs from the registered source binding")


def run(
    home: Path,
    policy: dict,
    unit: str,
    binding: dict,
    workspace: Path,
    executable: Path,
    auth: Path,
    *,
    model: str,
    prompt,
    stdout,
) -> int:
    """Execute from the admitted worker process, retaining outer isolation on error."""
    command = sandbox_command(workspace, executable, auth, model=model, owner=binding["owner"])
    core = require_native_unit(home, policy, unit, binding)
    require_source(workspace, core)
    return subprocess.run(
        command, stdin=prompt, stdout=stdout, stderr=subprocess.STDOUT, check=False
    ).returncode
