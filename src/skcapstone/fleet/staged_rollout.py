"""Staged rollout: deploy to one node, gate it, halt on the first failure.
Also rollback: return each node to whatever manifest it was running before
its last recorded change.

See ``docs/superpowers/specs/2026-09-16-nimble-factory-design.md`` A.4 and
``docs/superpowers/plans/2026-09-17-staged-rollout.md`` Tasks 2 and 3. This closes
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

Rollback (Task 3) is the reverse of the same mechanism, not a separate one:
``execute_rollback`` reuses ``DeployOutcome``, ``GateOutcome``, ``NodeResult``,
``default_gate_node``, the same halt-on-first-failure loop shape, and the same
``dry_run`` default, rather than inventing parallel types for "the same idea,
backwards". The one thing rollback cannot reuse is a caller-supplied target
manifest (``RolloutPlan.manifest``): each node's "previous" state is whatever
``rollout_history.record_deployment`` actually recorded for THAT node, looked
up at rollback time via ``rollout_history.previous_manifest`` (local node) or
the ssh equivalent (remote node). When there is no recorded previous manifest,
rollback refuses outright rather than reconstructing one from "current minus
one commit" or similar -- this estate's existing rollback practice is
hand-written evidence in ``card_events`` with no code behind it, which is
exactly the gap this refusal replaces with a fact.

Rollback re-runs the gate after every node, the same gate the forward path
uses, for the same reason the forward path pays that cost: this estate has
two documented cases of an unverified change going unnoticed for a long
time precisely because nothing checked after it landed (a release merged
and uninstalled for sixteen hours; a dispatcher timer active-but-not-enabled
for seven weeks). Skipping verification would save time on the rollback
path specifically, which is exactly the path most often run under pressure
with a fleet already known to be unhealthy -- the wrong place to trade away
the one thing that would have caught either incident. See
``execute_rollback``'s docstring for what happens when the gate itself
fails during a rollback.
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
from .rollout_history import previous_manifest, record_deployment

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

#: The rollback equivalent of ``_DEPLOY_STEPS``: ``git checkout <sha>``
#: (the previous manifest's own recorded revision) in place of ``git pull``
#: (which only ever moves forward). Everything after checkout is identical
#: to the forward path on purpose: reinstalling and reconverging is the
#: same operation either direction, only the source revision differs.
#: Formatted with ``repo=`` and ``git_sha=`` (the latter already
#: ``shlex.quote``-d by the caller) at call time.
_ROLLBACK_STEPS: tuple[tuple[str, str], ...] = (
    ("git_checkout", "git -C {repo} checkout {git_sha}"),
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

#: Mirrors ``_RECORD_SNIPPET``: reads THIS remote node's own previous
#: recorded manifest and prints it as one JSON line (``null`` when there is
#: none), so a controller can ask "what would rollback return you to"
#: without a second, parallel way to answer that question.
_PREVIOUS_MANIFEST_SNIPPET = (
    "import json, os; "
    "from skcapstone.fleet.rollout_history import previous_manifest; "
    "print(json.dumps(previous_manifest(os.path.expanduser('~'))))"
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
class RollbackPlan:
    """A rollback target: which nodes, in which order, each returned to
    whatever manifest ``rollout_history`` recorded before its own last
    change.

    Deliberately has no ``manifest`` field, unlike ``RolloutPlan``.
    Rollback's target is not one manifest chosen by the caller up front:
    it is looked up per node, from that node's own history, at rollback
    time (each node may have last changed at a different point, so "the
    previous manifest" can genuinely differ node to node). Baking a single
    manifest into the plan would let a rollback deploy today's guess of
    "the previous state" instead of what actually happened on each node,
    which is exactly the reconstruction failure ``record_deployment``
    exists to replace.
    """

    nodes: tuple[str, ...]


@dataclass(frozen=True)
class RollbackResult:
    """The outcome of one ``execute_rollback`` call. Same shape as
    ``RolloutResult`` and read the same way: ``halted_at`` names the node
    that stopped the rollback (None when every node completed), ``reason``
    says why, and ``remaining`` lists every node never attempted, in plan
    order, so a reader can see at a glance that they are provably
    untouched.
    """

    dry_run: bool
    completed: tuple[NodeResult, ...]
    halted_at: str | None
    reason: str | None
    remaining: tuple[str, ...]


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


def plan_rollback(nodes: list[str] | tuple[str, ...]) -> RollbackPlan:
    """Build a rollback plan visiting ``nodes`` in the exact order given.

    Args:
        nodes: Node names, in the order the rollback must visit them. Same
            determinism and validation as ``plan_rollout``. No ``manifest``
            argument: see ``RollbackPlan`` for why the target manifest is
            looked up per node at rollback time rather than chosen here.

    Raises:
        ValueError: A node name is not a safe path component (see
            ``plan_rollout``).
    """
    node_tuple = tuple(nodes)
    for name in node_tuple:
        if not valid_name(name):
            raise ValueError(f"not a valid node name: {name!r}")
    return RollbackPlan(nodes=node_tuple)


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
    node: str,
    *,
    steps: tuple[tuple[str, str], ...] = _DEPLOY_STEPS,
    repo: str,
    runner: Runner,
    wrap: Callable[[str], list[str]],
    extra_format: dict[str, str] | None = None,
) -> DeployOutcome:
    """Run ``steps`` in order, halting at the first that fails.

    ``steps`` defaults to the forward ``_DEPLOY_STEPS``; the rollback path
    passes ``_ROLLBACK_STEPS`` plus ``extra_format={"git_sha": ...}`` so the
    same halt-at-first-failure loop backs both directions rather than two
    near-identical copies of it.
    """
    format_kwargs = {"repo": repo, **(extra_format or {})}
    for step_name, template in steps:
        command = template.format(**format_kwargs)
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
# rollback: return a node to the manifest rollout_history recorded before
# its last change
# --------------------------------------------------------------------------


def _previous_manifest_local(node: str, home: Path) -> tuple[dict | None, str]:
    """This node's own previous manifest, read in-process (this IS the
    node ``previous_manifest`` is scoped to -- see ``rollout_history``'s
    ``self_node_name()`` scoping, no "which node" parameter exists to
    override it).
    """
    try:
        manifest = previous_manifest(home)
    except OSError as exc:
        return None, f"could not look up the previous manifest for {node}: {exc}"
    if manifest is None:
        return None, (
            f"no recorded previous manifest for {node}; rollback refuses to guess "
            "or reconstruct one"
        )
    return manifest, ""


def _previous_manifest_remote(node: str, runner: Runner) -> tuple[dict | None, str]:
    """The equivalent remote lookup, over ssh, using the same
    ``python3 -c`` shape ``_record_remote`` already uses to reach a node
    that cannot be queried in-process (``rollout_drift``/``rollout_history``
    are both scoped to THIS process's own node identity, never a remote
    one).
    """
    remote_command = f"python3 -c {shlex.quote(_PREVIOUS_MANIFEST_SNIPPET)}"
    try:
        result = runner(_ssh(node, remote_command))
    except Exception as exc:  # pragma: no cover - defensive, mirrors actuation.py
        return None, f"could not reach {node} to look up its previous manifest: {exc}"
    if result.returncode != 0:
        reason = (result.stderr or result.stdout or "previous_manifest lookup failed").strip()
        return None, f"could not look up the previous manifest on {node}: {reason}"
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return None, f"could not parse the previous manifest reported by {node}: {exc}"
    if payload is None:
        return None, (
            f"no recorded previous manifest for {node}; rollback refuses to guess "
            "or reconstruct one"
        )
    if not isinstance(payload, dict):
        return None, f"the previous manifest reported by {node} was not a JSON object"
    return payload, ""


def _lookup_previous_manifest(node: str, home: Path, runner: Runner) -> tuple[dict | None, str]:
    """The manifest rollback would return ``node`` to, or ``None`` with a
    reason naming the node when there is no recorded previous manifest to
    return to. Never a guess, never a reconstruction: only what
    ``rollout_history.record_deployment`` actually recorded.
    """
    if _is_local(node):
        return _previous_manifest_local(node, home)
    return _previous_manifest_remote(node, runner)


def default_rollback_deploy_node(
    node: str,
    manifest: dict,
    *,
    home: Path | str | None = None,
    remote_repo_root: str = DEFAULT_REMOTE_REPO_ROOT,
    local_repo_root: Path | None = None,
    runner: Runner = default_runner,
) -> DeployOutcome:
    """Return ``node`` to ``manifest`` (an already-resolved previous
    manifest, e.g. from ``previous_manifest``): record, then check out its
    ``git_sha`` and re-run the rest of the deploy steps.

    Mirrors ``default_deploy_node``'s shape deliberately: record happens
    before any shell step here too, for the identical reason (a failure
    mid-node must still leave a record of the state rollback was moving
    to), and local/remote both branch through ``_run_shell_steps`` rather
    than a second implementation of "run these steps in order and halt on
    the first failure".

    ``manifest`` is taken as already resolved rather than looked up here:
    the caller (``execute_rollback``) resolves it once via
    ``_lookup_previous_manifest`` and passes the exact same dict on to both
    this function and the gate, so a gate check is never run against a
    manifest other than the one that was actually checked out.
    """
    home_path = Path(home) if home is not None else Path.home()
    git_sha = manifest.get("git_sha")
    if not isinstance(git_sha, str) or not git_sha:
        return DeployOutcome(
            ok=False,
            step="previous_manifest",
            reason=f"recorded previous manifest for {node} has no usable git_sha field",
        )

    if _is_local(node):
        record_outcome = _record_local(node, manifest, home_path)
        if not record_outcome.ok:
            return record_outcome
        repo = str(local_repo_root or _default_local_repo_root())
        return _run_shell_steps(
            node,
            steps=_ROLLBACK_STEPS,
            repo=repo,
            runner=runner,
            wrap=lambda cmd: ["bash", "-lc", cmd],
            extra_format={"git_sha": shlex.quote(git_sha)},
        )

    record_outcome = _record_remote(node, manifest, runner)
    if not record_outcome.ok:
        return record_outcome
    return _run_shell_steps(
        node,
        steps=_ROLLBACK_STEPS,
        repo=remote_repo_root,
        runner=runner,
        wrap=lambda cmd: _ssh(node, cmd),
        extra_format={"git_sha": shlex.quote(git_sha)},
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


# --------------------------------------------------------------------------
# execute_rollback
# --------------------------------------------------------------------------


def execute_rollback(
    plan: RollbackPlan,
    *,
    dry_run: bool = True,
    home: Path | str | None = None,
    remote_repo_root: str = DEFAULT_REMOTE_REPO_ROOT,
    local_repo_root: Path | None = None,
    runner: Runner = default_runner,
    rollback: DeployFn | None = None,
    gate: GateFn | None = None,
) -> RollbackResult:
    """Roll ``plan`` back one node at a time, halting on the first failure.

    Gating decision (this task's one required design choice): rollback
    re-runs the same gate the forward path uses, after every node, exactly
    like ``execute_rollout``. Skipping it would leave a rolled-back node's
    state unverified, and this estate already has two documented cases of
    exactly that going unnoticed for a long time precisely because nothing
    checked after a change landed: a release that sat merged and
    uninstalled for sixteen hours, and a dispatcher timer that sat
    active-but-not-enabled for seven weeks. A rollback is remediation run
    under pressure on a fleet already known to be in a bad state, which
    makes verifying each node MORE important, not an acceptable place to
    cut, and the forward path already accepts this same time cost for the
    same reason. The counterargument that the gate itself might be the
    broken thing is real, but it does not argue for skipping verification
    silently: it argues for a human reading the halt reason and judging
    whether the gate or the rollback is at fault, which is exactly what
    happens here -- a gate failure during rollback halts (see below) with
    a reason that says "after rollback", so it is never mistaken for a
    forward-deploy failure, and ``gate`` remains overridable (the same
    dependency-injection point ``execute_rollout`` already exposes) for a
    caller who has deliberately decided, for one run, that the gate itself
    cannot be trusted.

    When the gate fails during a rollback: the node's rollback has already
    happened (recorded, checked out, reinstalled, reconverged) by the time
    the gate runs, and nothing here undoes that -- there is no
    rollback-of-a-rollback. What halts is only the ADVANCE to further
    nodes: the failing node is reported not ready and excluded from
    ``completed``, and every later node is left in ``remaining``, never
    touched at all, same as a forward gate failure.

    "No recorded previous manifest" is treated the same as any other
    halting failure: looked up first, per node, via
    ``_lookup_previous_manifest`` (never a guess, never reconstructed --
    see ``RollbackPlan``), and a lookup failure halts with a reason naming
    the node before ``rollback`` or ``gate`` is ever called for it.

    Args:
        plan: The nodes to roll back, from ``plan_rollback``.
        dry_run: Defaults to True, and mirrors ``execute_rollout``
            precisely: when true, NOTHING is called for any node, not even
            the previous-manifest lookup. A remote lookup is an ssh round
            trip, so performing it during a dry run would make a dry
            rollback do real network I/O, exactly the thing a dry run
            exists to avoid.
        home: The estate home to read/write node-scoped state under
            (defaults to the real ``$HOME``; tests must always pass a
            throwaway directory here).
        remote_repo_root: The checkout path used on a REMOTE node's own
            filesystem.
        local_repo_root: The checkout to compare THIS machine's installed
            state against, when a plan node happens to be this machine.
        runner: Injectable command runner (``actuation.Runner``); tests
            supply a fake so nothing here ever shells out for real.
        rollback: Overrides the default rollback step entirely (mainly for
            tests). Defaults to ``default_rollback_deploy_node``, called
            with the manifest this function already resolved for that
            node -- the same ``DeployFn`` shape ``execute_rollout`` uses.
        gate: Overrides the default gate step entirely (mainly for tests).
            Defaults to ``default_gate_node``, the identical function
            ``execute_rollout`` uses: rollback never invents a second
            notion of "healthy".

    Returns:
        A ``RollbackResult`` naming, on failure, which node halted the
        rollback and why, with every later node listed in ``remaining`` and
        never having been passed to ``rollback`` or ``gate`` at all.
    """
    home_path = Path(home) if home is not None else Path.home()

    def _default_rollback(node: str, manifest: dict) -> DeployOutcome:
        return default_rollback_deploy_node(
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

    rollback_fn = rollback or _default_rollback
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
                        f"dry run: would look up {node}'s recorded previous manifest and, "
                        "if one exists, record it, check out its git_sha, reinstall, copy "
                        f"{DISPATCHER_SCRIPT_NAME}, converge, then gate the node; not executed"
                    ),
                )
            )
        return RollbackResult(
            dry_run=True, completed=tuple(completed), halted_at=None, reason=None, remaining=()
        )

    for index, node in enumerate(nodes):
        manifest, lookup_reason = _lookup_previous_manifest(node, home_path, runner)
        if manifest is None:
            return RollbackResult(
                dry_run=False,
                completed=tuple(completed),
                halted_at=node,
                reason=lookup_reason,
                remaining=tuple(nodes[index + 1 :]),
            )

        rollback_outcome = rollback_fn(node, manifest)
        if not rollback_outcome.ok:
            reason = (
                f"rollback step '{rollback_outcome.step}' failed on {node}: "
                f"{rollback_outcome.reason}"
            )
            return RollbackResult(
                dry_run=False,
                completed=tuple(completed),
                halted_at=node,
                reason=reason,
                remaining=tuple(nodes[index + 1 :]),
            )

        gate_outcome = gate_fn(node, manifest)
        if not gate_outcome.ready:
            return RollbackResult(
                dry_run=False,
                completed=tuple(completed),
                halted_at=node,
                reason=f"gate failed on {node} after rollback: {gate_outcome.reason}",
                remaining=tuple(nodes[index + 1 :]),
            )

        completed.append(
            NodeResult(
                node=node,
                dry_run=False,
                deployed=True,
                ready=True,
                drift=gate_outcome.drift,
                detail=f"{node}: rolled back to manifest {manifest.get('revision', '?')} "
                "and gate passed",
            )
        )

    return RollbackResult(
        dry_run=False, completed=tuple(completed), halted_at=None, reason=None, remaining=()
    )
