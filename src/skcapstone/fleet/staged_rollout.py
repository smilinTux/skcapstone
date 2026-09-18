"""Staged rollout: deploy to one node, gate it, halt on the first failure.

See ``docs/superpowers/specs/2026-09-16-nimble-factory-design.md`` A.4 and
``docs/superpowers/plans/2026-09-17-staged-rollout.md`` Task 2. This closes
the gap that A.1 measured by hand: today's manual roll of four hosts is the
worked example this module is built to reproduce mechanically. Rolling
chiap02 first surfaced a stale dispatcher copy before the same step ever
touched chiap01 or chiap03. Had all four gone at once, three hosts would
have been mid-change when the first problem appeared.

This is a human-invoked mechanism, not an autonomous actuator (A.4's own
words): ``execute_rollout`` is a function a person or a CLI command (Task
4) calls; nothing here schedules itself.

``dry_run`` defaults to True and, when true, calls neither ``deploy`` nor
``gate``: a dry run is a preview of the plan, not a live health check
dressed up as one, so it makes exactly zero network or filesystem-writing
calls, matching the same reasoning an earlier plan's backfill script used
for the same default.

The gate is never reinvented. A node passes when the existing readiness
gate (``scripts/fleet/skfleet_readiness.py``, read from its published,
Syncthing-synced verdict file) reports READY *and*
``rollout_drift.detect_drift`` reports no unambiguous finding, reusing
``cli.py``'s own ``_drift_is_role_ambiguous`` rule for what counts as
unambiguous rather than inventing a second notion of "healthy" (the estate
already has a documented case of three signals -- version strings, pip
metadata, and actual content -- disagreeing silently; a third
"healthy" would just be a fourth).

What "deploy to a node" means here, chosen deliberately: the four steps
Chef performed by hand on 2026-09-17, in the order that made them safe --
``git pull`` in the node's checkout, ``pip install -e .`` (the dispatcher
script imports ``GATED_EXIT_CODE`` from the installed package, so a newer
script against an older package fails at import and takes that host's
dispatcher down: package first, then script), copying
``scripts/fleet/skfleet-rotate.py`` to ``~/.local/bin``, then converging
(``skcapstone fleet sknoded --once``). ``record_deployment`` runs before
any of those four, never after: a failure mid-node must still leave a
previous-state record for Task 3's rollback to use.

Local versus remote, and why both exist: ``rollout_drift.detect_drift``
and ``rollout_history.record_deployment`` are both scoped by
``paths.self_node_name()``, the CURRENT process's own node identity, with
no "which node" parameter -- and ``cli.py``'s own ``node drift`` command
documents why: "grading a remote node from a local repo checkout would be
a confident wrong answer dressed up as a report, not a report." A
controller orchestrating several *other* machines therefore cannot call
either function in-process for a remote node; it has to reach that node
and run the equivalent check there. This module does that over ``ssh``,
reusing the exact CLI command ``cli.py`` already ships
(``skcapstone fleet node drift --json``) rather than inventing a second
way to ask the same question. When the target node IS this machine
(measured via ``self_node_name()``, mirroring ``node doctor``'s own "only
THIS node can be inventoried live" rule), no ssh is spent at all: the real
functions are called directly, in-process.

Every side-effecting call goes through an injectable ``Runner``
(``actuation.Runner``, the same type ``nodeinventory`` and ``actuation``
already use), so tests never shell out and every path here is exercised
without touching a real host.
"""

from __future__ import annotations

import base64
import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .actuation import Runner, default_runner
from .deployment_manifest import DISPATCHER_RELATIVE_PATH
from .paths import paths_for_home, self_node_name, valid_name
from .rollout_drift import Drift, detect_drift
from .rollout_history import record_deployment

#: The shared-checkout convention this fleet's rotate hosts actually use,
#: confirmed read-only against chiap01/02/03/04/08 (see
#: ``cli.py::_default_repo_root``). Deliberately not
#: ``install_backends._repos_root()`` (``~/clawd/skcapstone-repos``): that
#: is a developer-workstation convention that no rotate host carries.
REMOTE_REPO_ROOT_ENV = "SKCAPSTONE_REPO_ROOT"
DEFAULT_REMOTE_REPO_ROOT = "~/work/skcapstone"

DISPATCHER_SCRIPT_NAME = DISPATCHER_RELATIVE_PATH.name

#: The four deploy steps, in the load-bearing order described in the module
#: docstring: package before script. Formatted with ``repo=`` (the remote
#: checkout path) at call time.
_DEPLOY_STEPS: tuple[tuple[str, str], ...] = (
    ("git_pull", "git -C {repo} pull"),
    ("pip_install", "cd {repo} && pip install -e ."),
    (
        "copy_dispatcher",
        "cp {repo}/scripts/fleet/"
        + DISPATCHER_SCRIPT_NAME
        + " ~/.local/bin/"
        + DISPATCHER_SCRIPT_NAME,
    ),
    ("converge", "skcapstone fleet sknoded --once"),
)

_RECORD_SNIPPET = (
    "import base64, json, os, sys; "
    "from skcapstone.fleet.rollout_history import record_deployment; "
    "record_deployment(os.path.expanduser('~'), json.loads(base64.b64decode(sys.argv[1])))"
)


@dataclass(frozen=True)
class RolloutPlan:
    """A rollout target: which nodes, in which order, running which manifest.

    ``nodes`` is a tuple (not a set) precisely because order is the point:
    ``plan_rollout`` preserves the caller's given order verbatim, so the
    same input always produces the same visiting order.
    """

    nodes: tuple[str, ...]
    manifest: dict


@dataclass(frozen=True)
class DeployOutcome:
    """The result of deploying to one node.

    ``step`` names which of the four deploy steps failed (None on success),
    so a halted rollout can say exactly where it stopped, not just that it
    stopped.
    """

    ok: bool
    step: str | None
    reason: str | None


@dataclass(frozen=True)
class GateOutcome:
    """The result of gating one node: readiness plus drift, nothing else.

    ``drift`` carries only the UNAMBIGUOUS findings (see
    ``_drift_is_role_ambiguous``); a role-ambiguous ``missing`` finding is
    filtered out before this is constructed, so a caller inspecting
    ``drift`` never has to re-apply that filter itself.
    """

    ready: bool
    drift: tuple[Drift, ...]
    reason: str


@dataclass(frozen=True)
class NodeResult:
    """What happened for one node, whether deployed for real or previewed."""

    node: str
    dry_run: bool
    deployed: bool
    ready: bool
    drift: tuple[Drift, ...]
    detail: str


@dataclass(frozen=True)
class RolloutResult:
    """The outcome of one ``execute_rollout`` call.

    ``halted_at`` names the node that stopped the rollout (None when every
    node completed); ``reason`` says why. ``remaining`` lists every node
    never attempted, in plan order, so a reader can see at a glance that
    they are provably untouched: they are simply absent from ``completed``
    and absent from any deploy/gate call the injected fakes recorded.
    """

    dry_run: bool
    completed: tuple[NodeResult, ...]
    halted_at: str | None
    reason: str | None
    remaining: tuple[str, ...]


def plan_rollout(nodes: list[str] | tuple[str, ...], manifest: dict) -> RolloutPlan:
    """Build a rollout plan visiting ``nodes`` in the exact order given.

    Args:
        nodes: Node names, in the order the rollout must visit them. Not
            deduplicated or reordered: determinism means the same input
            always yields the same plan, not that the caller's choice of
            order gets second-guessed here.
        manifest: The deployment manifest every node in this rollout should
            end up running (built by ``deployment_manifest.build_manifest``
            and passed in by the caller, e.g. Task 4's CLI command).

    Raises:
        ValueError: A node name is not a safe path component (reused from
            ``paths.valid_name``, since every node name here ends up inside
            a filesystem path -- rejecting an unsafe one here is cheaper
            than discovering it three layers down inside ``paths_for_home``).
    """
    node_tuple = tuple(nodes)
    for name in node_tuple:
        if not valid_name(name):
            raise ValueError(f"not a valid node name: {name!r}")
    return RolloutPlan(nodes=node_tuple, manifest=dict(manifest))


# --------------------------------------------------------------------------
# node identity: local versus remote
# --------------------------------------------------------------------------


def _bare_self_host() -> str:
    """This process's own node name, without the ``node-`` prefix.

    Matches the bare host names a rollout plan is built from (e.g.
    ``chiap01``), so a target node can be compared against "am I this
    node" without either side having to know about the other's prefix
    convention.
    """
    current = self_node_name()
    if current.startswith("node-"):
        return current[len("node-") :]
    return current


def _is_local(node: str) -> bool:
    """True when ``node`` names the machine this process runs on.

    Mirrors ``cli.py``'s ``node doctor``: "Only THIS node can be
    inventoried live... naming another node and grading it against local
    state produces a confident wrong answer." The same rule applies here to
    deploying and gating: a remote node is reached over ssh, never assumed
    to share this process's local filesystem.
    """
    return node == _bare_self_host()


def _default_local_repo_root() -> Path:
    """This machine's own checkout, matching ``cli.py::_default_repo_root``.

    Duplicated rather than imported: importing from ``cli.py`` at module
    load time would create a cycle once Task 4 makes ``cli.py`` import this
    module. The two are kept in sync by using the same environment variable
    name and the same fallback path, not by sharing a Python reference.
    """
    env = os.environ.get(REMOTE_REPO_ROOT_ENV)
    return Path(env).expanduser() if env else Path.home() / "work" / "skcapstone"


def _ssh(node: str, remote_command: str) -> list[str]:
    return ["ssh", node, "bash", "-lc", remote_command]


# --------------------------------------------------------------------------
# deploy
# --------------------------------------------------------------------------


def _record_local(node: str, manifest: dict, home: Path) -> DeployOutcome:
    try:
        record_deployment(home, manifest)
    except OSError as exc:
        return DeployOutcome(ok=False, step="record", reason=str(exc))
    return DeployOutcome(ok=True, step=None, reason=None)


def _record_remote(node: str, manifest: dict, runner: Runner) -> DeployOutcome:
    encoded = base64.b64encode(json.dumps(manifest, sort_keys=True).encode()).decode()
    remote_command = f"python3 -c {shlex.quote(_RECORD_SNIPPET)} {shlex.quote(encoded)}"
    try:
        result = runner(_ssh(node, remote_command))
    except Exception as exc:  # pragma: no cover - defensive, mirrors actuation.py
        return DeployOutcome(ok=False, step="record", reason=str(exc))
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "record_deployment failed").strip()
        return DeployOutcome(ok=False, step="record", reason=reason)
    return DeployOutcome(ok=True, step=None, reason=None)


def _run_shell_steps(
    node: str, *, repo: str, runner: Runner, wrap: Callable[[str], list[str]]
) -> DeployOutcome:
    for step_name, template in _DEPLOY_STEPS:
        command = template.format(repo=repo)
        try:
            result = runner(wrap(command))
        except Exception as exc:  # pragma: no cover - defensive, mirrors actuation.py
            return DeployOutcome(ok=False, step=step_name, reason=str(exc))
        if result.returncode != 0:
            reason = (
                result.stderr or result.stdout or f"{step_name} exited {result.returncode}"
            ).strip()
            return DeployOutcome(ok=False, step=step_name, reason=reason)
    return DeployOutcome(ok=True, step=None, reason=None)


def default_deploy_node(
    node: str,
    manifest: dict,
    *,
    home: Path | str | None = None,
    remote_repo_root: str = DEFAULT_REMOTE_REPO_ROOT,
    local_repo_root: Path | None = None,
    runner: Runner = default_runner,
) -> DeployOutcome:
    """Deploy ``manifest`` to ``node``: record, then the four shell steps.

    Local node: every step runs in-process (a direct, real call to
    ``record_deployment``, then plain local shell commands). Remote node:
    every step is the ssh-wrapped equivalent, the record step first,
    exactly as required -- a failure mid-node must still leave a
    previous-state record.
    """
    home_path = Path(home) if home is not None else Path.home()

    if _is_local(node):
        record_outcome = _record_local(node, manifest, home_path)
        if not record_outcome.ok:
            return record_outcome
        repo = str(local_repo_root or _default_local_repo_root())
        return _run_shell_steps(
            node, repo=repo, runner=runner, wrap=lambda cmd: ["bash", "-lc", cmd]
        )

    record_outcome = _record_remote(node, manifest, runner)
    if not record_outcome.ok:
        return record_outcome
    return _run_shell_steps(
        node, repo=remote_repo_root, runner=runner, wrap=lambda cmd: _ssh(node, cmd)
    )


# --------------------------------------------------------------------------
# gate: readiness verdict plus detect_drift, never a third notion
# --------------------------------------------------------------------------


def _readiness_verdict(node: str, home: Path) -> tuple[bool | None, str]:
    """This node's published readiness verdict, read from the synced file.

    Never ssh: the readiness gate already publishes continuously (its own
    systemd timer, see ``systemd/skfleet-readiness.service``) to
    ``status/node-<host>/readiness/verdict.json`` under the one Syncthing
    folder every host shares, so the controller's own copy is current
    without reaching out. An absent or unreadable verdict is reported as
    "not ready" with a reason, never as an unlabelled False: unknown is
    never ready, the same rule the gate itself is built on.
    """
    path = paths_for_home(home).status_path(f"node-{node}", "readiness", "verdict")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, f"no readiness verdict published for {node} (expected at {path})"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"could not read readiness verdict for {node}: {exc}"
    ready = payload.get("ready")
    if not isinstance(ready, bool):
        return None, f"readiness verdict for {node} has no boolean 'ready' field"
    return ready, "" if ready else f"readiness gate reports NOT READY for {node}"


def _unambiguous_drift(drifts: list[Drift]) -> tuple[Drift, ...]:
    """Filter out role-ambiguous findings, reusing cli.py's own rule.

    Lazily imported to avoid a module-load-time cycle once Task 4 makes
    ``cli.py`` import this module; by the time this is actually called,
    both modules are already fully loaded.
    """
    from .cli import _drift_is_role_ambiguous

    return tuple(d for d in drifts if not _drift_is_role_ambiguous(d))


def _gate_local(
    node: str, manifest: dict, home: Path, local_repo_root: Path | None
) -> GateOutcome:
    ready, reason = _readiness_verdict(node, home)
    repo_root = local_repo_root or _default_local_repo_root()
    try:
        drifts = detect_drift(manifest, home, repo_root)
    except (OSError, RuntimeError) as exc:
        return GateOutcome(
            ready=False, drift=(), reason=f"could not compute drift for {node}: {exc}"
        )
    unambiguous = _unambiguous_drift(drifts)
    if ready is not True:
        return GateOutcome(ready=False, drift=unambiguous, reason=reason)
    if unambiguous:
        names = ", ".join(f"{d.kind}:{d.artifact}" for d in unambiguous)
        return GateOutcome(
            ready=False, drift=unambiguous, reason=f"unambiguous drift on {node}: {names}"
        )
    return GateOutcome(
        ready=True, drift=(), reason=f"{node}: readiness READY, no unambiguous drift"
    )


def _remote_drift(
    node: str, remote_repo_root: str, runner: Runner
) -> tuple[list[Drift] | None, str]:
    """``skcapstone fleet node drift --json`` over ssh: the same command a
    human would run, reused rather than a second way to ask this.
    """
    remote_command = f"cd {remote_repo_root} && skcapstone fleet node drift --json"
    try:
        result = runner(_ssh(node, remote_command))
    except Exception as exc:  # pragma: no cover - defensive, mirrors actuation.py
        return None, f"could not reach {node} to check drift: {exc}"
    if result.returncode != 0:
        reason = (
            result.stderr or result.stdout or f"node drift exited {result.returncode}"
        ).strip()
        return None, f"drift check failed on {node}: {reason}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"could not parse drift output from {node}: {exc}"
    drifts = [
        Drift(entry["artifact"], entry["kind"], entry["expected"], entry.get("found"), node)
        for entry in payload.get("drifts", [])
    ]
    return drifts, ""


def _gate_remote(node: str, home: Path, remote_repo_root: str, runner: Runner) -> GateOutcome:
    ready, reason = _readiness_verdict(node, home)
    drifts, drift_reason = _remote_drift(node, remote_repo_root, runner)
    if drifts is None:
        return GateOutcome(ready=False, drift=(), reason=drift_reason)
    unambiguous = _unambiguous_drift(drifts)
    if ready is not True:
        return GateOutcome(ready=False, drift=unambiguous, reason=reason)
    if unambiguous:
        names = ", ".join(f"{d.kind}:{d.artifact}" for d in unambiguous)
        return GateOutcome(
            ready=False, drift=unambiguous, reason=f"unambiguous drift on {node}: {names}"
        )
    return GateOutcome(
        ready=True, drift=(), reason=f"{node}: readiness READY, no unambiguous drift"
    )


def default_gate_node(
    node: str,
    manifest: dict,
    *,
    home: Path | str | None = None,
    remote_repo_root: str = DEFAULT_REMOTE_REPO_ROOT,
    local_repo_root: Path | None = None,
    runner: Runner = default_runner,
) -> GateOutcome:
    """Gate ``node``: the existing readiness verdict plus ``detect_drift``.

    A node passes when readiness reports READY and drift reports no
    unambiguous finding. Nothing else. Local node: both checks run
    in-process. Remote node: readiness is read from the synced verdict
    file (no ssh needed, it already publishes continuously); drift is
    checked over ssh via the same ``fleet node drift --json`` command a
    human would run by hand.
    """
    home_path = Path(home) if home is not None else Path.home()
    if _is_local(node):
        return _gate_local(node, manifest, home_path, local_repo_root)
    return _gate_remote(node, home_path, remote_repo_root, runner)


# --------------------------------------------------------------------------
# execute_rollout
# --------------------------------------------------------------------------

DeployFn = Callable[[str, dict], DeployOutcome]
GateFn = Callable[[str, dict], GateOutcome]


def execute_rollout(
    plan: RolloutPlan,
    *,
    dry_run: bool = True,
    home: Path | str | None = None,
    remote_repo_root: str = DEFAULT_REMOTE_REPO_ROOT,
    local_repo_root: Path | None = None,
    runner: Runner = default_runner,
    deploy: DeployFn | None = None,
    gate: GateFn | None = None,
) -> RolloutResult:
    """Roll ``plan`` out one node at a time, halting on the first failure.

    Args:
        plan: The nodes and manifest to roll out, from ``plan_rollout``.
        dry_run: Defaults to True. When true, neither ``deploy`` nor
            ``gate`` is called for any node: this is a preview of the plan,
            not a live check, so it makes zero network or filesystem-
            writing calls. A rollout tool whose default is to act is the
            wrong default for a first version.
        home: The estate home to read/write node-scoped state under
            (defaults to the real ``$HOME``; tests must always pass a
            throwaway directory here).
        remote_repo_root: The checkout path used on a REMOTE node's own
            filesystem (see the module docstring for why this differs from
            a developer-workstation convention).
        local_repo_root: The checkout to compare THIS machine's installed
            state against, when a plan node happens to be this machine.
        runner: Injectable command runner (``actuation.Runner``); tests
            supply a fake so nothing here ever shells out for real.
        deploy: Overrides the default deploy step entirely (mainly for
            tests). Defaults to ``default_deploy_node``.
        gate: Overrides the default gate step entirely (mainly for tests).
            Defaults to ``default_gate_node``.

    Returns:
        A ``RolloutResult`` naming, on failure, which node halted the
        rollout and why, with every later node listed in ``remaining`` and
        never having been passed to ``deploy`` or ``gate`` at all.
    """
    home_path = Path(home) if home is not None else Path.home()

    def _default_deploy(node: str, manifest: dict) -> DeployOutcome:
        return default_deploy_node(
            node,
            manifest,
            home=home_path,
            remote_repo_root=remote_repo_root,
            local_repo_root=local_repo_root,
            runner=runner,
        )

    def _default_gate(node: str, manifest: dict) -> GateOutcome:
        return default_gate_node(
            node,
            manifest,
            home=home_path,
            remote_repo_root=remote_repo_root,
            local_repo_root=local_repo_root,
            runner=runner,
        )

    deploy_fn = deploy or _default_deploy
    gate_fn = gate or _default_gate

    nodes = plan.nodes
    completed: list[NodeResult] = []

    if dry_run:
        for node in nodes:
            completed.append(
                NodeResult(
                    node=node,
                    dry_run=True,
                    deployed=False,
                    ready=False,
                    drift=(),
                    detail=(
                        f"dry run: would record deployment, then git pull, pip install, "
                        f"copy {DISPATCHER_SCRIPT_NAME}, and converge on {node}; not executed"
                    ),
                )
            )
        return RolloutResult(
            dry_run=True, completed=tuple(completed), halted_at=None, reason=None, remaining=()
        )

    for index, node in enumerate(nodes):
        deploy_outcome = deploy_fn(node, plan.manifest)
        if not deploy_outcome.ok:
            reason = (
                f"deploy step '{deploy_outcome.step}' failed on {node}: {deploy_outcome.reason}"
            )
            return RolloutResult(
                dry_run=False,
                completed=tuple(completed),
                halted_at=node,
                reason=reason,
                remaining=tuple(nodes[index + 1 :]),
            )

        gate_outcome = gate_fn(node, plan.manifest)
        if not gate_outcome.ready:
            return RolloutResult(
                dry_run=False,
                completed=tuple(completed),
                halted_at=node,
                reason=f"gate failed on {node} after deploy: {gate_outcome.reason}",
                remaining=tuple(nodes[index + 1 :]),
            )

        completed.append(
            NodeResult(
                node=node,
                dry_run=False,
                deployed=True,
                ready=True,
                drift=gate_outcome.drift,
                detail=f"{node}: deployed and gate passed",
            )
        )

    return RolloutResult(
        dry_run=False, completed=tuple(completed), halted_at=None, reason=None, remaining=()
    )
