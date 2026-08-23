"""Scratch-fleet drill harness: rehearse the control-seat promotion safely.

The fleet has one control seat (.158). That SPOF is accepted, and its
mitigation is a warm replica plus a *drilled* promotion runbook
(``docs/fleet/adr-node-role-model.md``, card ``591d2b1a``). A runbook nobody
has executed is a hope, not a mitigation, so this module is what makes the
drill cheap enough to run repeatedly.

It stands up a throwaway fleet tree populated enough that ``skfleet nodes``,
``skfleet get profiles``, ``skfleet services`` and ``skfleet node doctor``
all return something real, kills the control seat by aging its heartbeat past
:data:`~skcapstone.fleet.node_controller.DEAD_AFTER_S`, promotes the standby
inside that tree, and deletes itself afterwards.

Safety is the whole point, not a feature
----------------------------------------
Production is the fleet tree inside the sovereign home
(:data:`~skcapstone.fleet.paths.SOVEREIGN_HOME`), a LIVE Syncthing folder
shared to three other machines. A drill that could write there would
propagate damage fleet-wide within seconds, so every refusal here is
structural:

1. **Resolved-path containment.** Every candidate root goes through
   ``Path.resolve()`` BEFORE it is judged, and the judgement is made on the
   resolved path. ``..`` segments and symlinks therefore cannot walk a drill
   into the sovereign tree: they are collapsed away before the check runs.
2. **Ownership marker.** An existing directory is only usable when it carries
   the marker file this harness itself wrote. The harness can consequently
   never adopt, populate or delete a directory somebody else created, which
   is the case that turns a typo into data loss.
3. **No ambient target.** ``SKFLEET_ROOT`` is never read as the drill target.
   An operator with that variable exported (the normal state on a control
   node) would otherwise aim the drill at production simply by omitting an
   argument. The root is always an explicit parameter, and the CLI marks it
   ``required=True`` so the omission is an error rather than a default. The
   variable is only ever *written*, into a child environment via
   :meth:`DrillFleet.env`, and never into ``os.environ``.

Nothing in this module consults :func:`skcapstone.fleet.paths.default_paths`.
That is deliberate and worth keeping that way: it is the single function
whose return value is production.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import node_controller, store
from .node_controller import DEAD_AFTER_S, NOT_READY_AFTER_S
from .paths import SOVEREIGN_HOME, FleetPaths, valid_name

# SOVEREIGN_HOME is imported rather than spelled out here on purpose. paths.py
# is the module that owns where fleet state lives, and card 59f78375's audit
# enforces that no other module in this package names that location: a second
# hardcoded copy is how a relocated root silently leaves state behind. The
# forbidden prefix is deliberately the sovereign HOME and not the fleet folder
# alone, so a drill cannot be aimed at a live sibling either (agents/, trust/,
# coordination/ are all just as shared).

#: Written into every root this harness creates, and required to exist
#: before it will populate or delete one. The marker is the difference
#: between "a directory I made" and "a directory that was already there".
MARKER_NAME = ".skfleet-drill"
MARKER_KIND = "skfleet-drill-scratch-root"

#: Default node names. The ``drill-`` infix keeps them impossible to confuse
#: with real fleet nodes in any output an operator reads.
CONTROL_NODE = "node-drill-control"
STANDBY_NODE = "node-drill-standby"
WORKER_NODE = "node-drill-worker"

#: Profile names, matching the shipped manifests in deploy/fleet-objects so
#: the drill rehearses the real role vocabulary. The specs below are inlined
#: rather than loaded from disk on purpose: manifest lookup consults
#: SKFLEET_PROFILE_MANIFESTS and the fleet tree, which would make a drill's
#: content depend on the ambient environment of whoever ran it.
CONTROL_PROFILE = "control"
STANDBY_PROFILE = "builder-standby"
WORKER_PROFILE = "worker-gpu"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


class UnsafeDrillRootError(RuntimeError):
    """The harness refused a root. Always structural, never advisory.

    Raised for any root that resolves inside the sovereign tree, for a root
    that exists without this harness's marker, and for degenerate targets
    (the filesystem root, ``$HOME``, an empty string).
    """


class DrillPreconditionError(RuntimeError):
    """A drill step was run out of order, e.g. promote before the seat died."""


@dataclass(frozen=True)
class DrillStep:
    """One executed runbook step, with the revert that undoes it.

    Card ``0afa9ffb`` requires a documented revert on every promotion step.
    Carrying it on the result rather than in prose means the drill cannot
    record a step whose revert nobody wrote down.

    Attributes:
        action: What was done, in runbook terms.
        detail: The concrete effect, for the drill transcript.
        revert: The command an operator runs to undo this step.
    """

    action: str
    detail: str
    revert: str

    def as_dict(self) -> dict:
        """Machine-readable form, for ``--json`` output and assertions."""
        return {"action": self.action, "detail": self.detail, "revert": self.revert}


def _real_home() -> Path:
    """The invoking user's home from the password database, NOT ``$HOME``.

    ``os.path.expanduser`` prefers the ``HOME`` environment variable, so every
    guard built on it moves when ``HOME`` moves. Drilled (card ``4c32df6f``,
    gap G0): running the drill under a rewritten ``HOME`` made the guard
    compute a different forbidden prefix, and it ACCEPTED the real production
    tree as a drill root. No write was performed, but the refusal that is the
    entire point of the guard did not fire.

    A guard whose definition of "production" is supplied by the caller is not
    a guard. The password database is not settable by the process it is
    protecting against, which is the property that matters here.
    """
    try:
        import pwd

        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, ImportError, AttributeError):
        # Non-POSIX, or a uid with no passwd entry (some containers). Falling
        # back to $HOME is weaker, and is still better than crashing the
        # guard: a guard that raises is a guard that gets removed.
        return Path(os.path.expanduser("~"))


def sovereign_home() -> Path:
    """The resolved live sovereign tree, i.e. the one place drills may not go.

    Resolved rather than merely expanded so that a symlinked sovereign home
    is compared against on its real path. Otherwise a candidate root that
    resolves through the symlink would compare unequal to the forbidden
    prefix and pass.

    The leading ``~`` is expanded against :func:`_real_home` rather than
    ``$HOME`` so the forbidden prefix cannot be relocated by the environment.
    """
    raw = str(SOVEREIGN_HOME)
    if raw == "~" or raw.startswith("~/"):
        raw = str(_real_home()) + raw[1:]
        return Path(raw).resolve()
    return Path(raw).expanduser().resolve()


def resolve_drill_root(root: Path | str | None) -> Path:
    """Resolve a candidate drill root, or refuse it.

    Order matters: resolution happens first and the containment test runs on
    the resolved path, so ``..`` traversal and symlinks are already collapsed
    when the decision is made. Checking the string the caller passed would be
    trivially defeated by a root spelled ``/tmp/safe/../../<sovereign home>``.

    Args:
        root: Candidate directory. May not be None or blank: an implicit
            target is exactly the failure mode this harness exists to
            prevent.

    Returns:
        The fully resolved, accepted root.

    Raises:
        UnsafeDrillRootError: The root is blank, is the filesystem root, is ``$HOME``
            itself, or resolves to or inside the sovereign tree.
    """
    if root is None or not str(root).strip():
        raise UnsafeDrillRootError(
            "a drill root must be passed explicitly; there is deliberately no "
            "default and SKFLEET_ROOT is never consulted, because an implicit "
            "target on a control node means production"
        )
    resolved = Path(root).expanduser().resolve()
    if resolved.parent == resolved:
        raise UnsafeDrillRootError(f"refusing the filesystem root as a drill root: {resolved}")
    if resolved == Path.home().resolve():
        raise UnsafeDrillRootError(f"refusing $HOME as a drill root: {resolved}")
    forbidden = sovereign_home()
    if resolved == forbidden or forbidden in resolved.parents:
        raise UnsafeDrillRootError(
            f"refusing drill root {resolved}: it resolves inside the live "
            f"sovereign tree {forbidden}, which is a shared Syncthing folder. "
            "A write there reaches every other node in the fleet."
        )
    return resolved


def marker_path(root: Path) -> Path:
    """Path of the ownership marker inside a drill root."""
    return root / MARKER_NAME


def read_marker(root: Path) -> dict | None:
    """The marker payload for a root, or None when it is not ours.

    A corrupt or foreign marker reads as None rather than raising, because
    every caller's next move is the same either way: refuse to touch it.
    """
    try:
        payload = json.loads(marker_path(root).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("kind") != MARKER_KIND:
        return None
    return payload


def _write_marker(root: Path, *, now: datetime) -> dict:
    payload = {
        "kind": MARKER_KIND,
        "createdAt": now.strftime(_TS_FMT),
        "createdByPid": os.getpid(),
        "warning": "throwaway drill tree; skfleet drill teardown deletes it entirely",
    }
    marker_path(root).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def claim_root(root: Path | str | None, *, now: datetime | None = None) -> Path:
    """Resolve, guard and take ownership of a root for a fresh drill.

    An existing directory is only claimable when it already carries this
    harness's marker, in which case its contents are wiped and the drill
    starts clean. A directory without the marker is refused outright: the
    harness must not be able to adopt a tree it did not create, since that is
    the path by which a mistyped root becomes deleted data.

    Args:
        root: Candidate directory, resolved and guarded by
            :func:`resolve_drill_root`.
        now: Marker timestamp override, for tests.

    Returns:
        The resolved, empty, marked root.

    Raises:
        UnsafeDrillRootError: The root fails :func:`resolve_drill_root`, exists
            without a valid marker, or exists as a non-directory.
    """
    resolved = resolve_drill_root(root)
    now = now or datetime.now(timezone.utc)
    if resolved.exists():
        if not resolved.is_dir():
            raise UnsafeDrillRootError(f"refusing drill root {resolved}: not a directory")
        if read_marker(resolved) is None:
            raise UnsafeDrillRootError(
                f"refusing drill root {resolved}: it already exists and carries no "
                f"{MARKER_NAME} marker, so this harness did not create it. Pick a "
                "path that does not exist, or delete that one by hand first."
            )
        shutil.rmtree(resolved)
    resolved.mkdir(parents=True)
    _write_marker(resolved, now=now)
    return resolved


def require_owned_root(root: Path | str | None) -> Path:
    """Resolve and guard a root that must already be one of ours.

    Used by every operation on an existing drill tree, teardown above all.
    The guard runs again on each call rather than trusting a root captured
    earlier, so a :class:`DrillFleet` whose ``root`` was mutated after
    construction still cannot reach production.

    Raises:
        UnsafeDrillRootError: Fails :func:`resolve_drill_root`, does not exist, or
            carries no valid marker.
    """
    resolved = resolve_drill_root(root)
    if not resolved.is_dir():
        raise UnsafeDrillRootError(f"no such drill root: {resolved}")
    if read_marker(resolved) is None:
        raise UnsafeDrillRootError(
            f"refusing to operate on {resolved}: no {MARKER_NAME} marker, so this "
            "harness did not create it"
        )
    return resolved


def _iso(moment: datetime) -> str:
    return moment.strftime(_TS_FMT)


def _profile_specs() -> dict[str, dict]:
    """The three profile specs the drill tree carries.

    Small on purpose. They only have to be *valid* under
    ``profiles.normalize_profile_spec`` and to differ from each other in the
    two fields that carry real consequence (``stateTier`` and
    ``capauthIdentityClass``), because those are what the promotion actually
    moves between nodes.

    The validator requires every ``required`` name to appear in ``allowed``
    too, so these lists are written that way rather than auto-widened here: a
    drill built on a spec the real validator would reject is not a drill.
    """
    return {
        CONTROL_PROFILE: {
            "description": "DRILL control seat: full replica, runs the control loops.",
            "stateTier": "full-replica",
            "capauthIdentityClass": "operator",
            "units": {
                "required": ["skgateway.service"],
                "allowed": ["skgateway.service", "skoperator.timer", "syncthing.service"],
                "mustNot": [],
            },
            "packages": {
                "required": ["skcapstone"],
                "allowed": ["skcapstone", "skos"],
                "mustNot": [],
            },
            "syncFolders": ["skfleet-control"],
        },
        STANDBY_PROFILE: {
            "description": (
                "DRILL warm replica and promotion target: holds the state, "
                "runs none of the control loops."
            ),
            "stateTier": "full-replica",
            "capauthIdentityClass": "agent",
            "units": {
                "required": ["syncthing.service"],
                "allowed": ["syncthing.service"],
                # The mustNot list is what makes the standby a warm replica
                # rather than a hot mirror, so the drill must carry it: a
                # promotion that did not have to lift this prohibition would
                # be rehearsing the wrong thing.
                "mustNot": ["skgateway.service", "skoperator.timer"],
            },
            "packages": {
                "required": ["skcapstone"],
                "allowed": ["skcapstone", "skos"],
                "mustNot": [],
            },
            "syncFolders": ["skfleet-control"],
        },
        WORKER_PROFILE: {
            "description": "DRILL GPU worker: runs workloads, holds no sovereign state.",
            "stateTier": "none",
            "capauthIdentityClass": "worker",
            "units": {
                "required": ["skmodel.service"],
                "allowed": ["skmodel.service"],
                "mustNot": [],
            },
            "packages": {"required": [], "allowed": [], "mustNot": ["skvault"]},
            "syncFolders": [],
        },
    }


def _inventory(units: list[str], packages: list[str], *, now: datetime) -> dict:
    """A node.json inventory block in the shape ``nodeinventory.collect`` emits."""
    return {
        "units": {"user": {name: "enabled" for name in units}},
        "packages": {name: "0.0.0-drill" for name in packages},
        "collectedAt": _iso(now),
    }


@dataclass
class DrillFleet:
    """A live handle on one scratch fleet tree.

    Attributes:
        root: The resolved scratch root. Re-guarded on every operation.
        control: Node name playing the control seat.
        standby: Node name playing the warm replica and promotion target.
        worker: Node name playing a stateless worker.
    """

    root: Path
    control: str = CONTROL_NODE
    standby: str = STANDBY_NODE
    worker: str = WORKER_NODE

    @property
    def paths(self) -> FleetPaths:
        """Fleet paths rooted at the scratch tree, never at production."""
        return FleetPaths(root=self.root)

    @property
    def nodes(self) -> tuple[str, str, str]:
        """The three drill node names, control first."""
        return (self.control, self.standby, self.worker)

    def env(self, base: Mapping[str, str] | None = None) -> dict[str, str]:
        """A child environment pointed at this drill tree.

        Returns a NEW dict and never touches ``os.environ``. Mutating the
        ambient environment would leave a process whose later, unrelated
        ``skfleet`` calls silently target a tree that is about to be deleted.
        ``SKFLEET_ROOT`` is overwritten unconditionally rather than defaulted,
        so an exported production value cannot survive into the drill.

        Args:
            base: Environment to copy. Defaults to the current one.

        Returns:
            The copied environment with ``SKFLEET_ROOT`` and ``SKFLEET_NODE``
            set for this drill.
        """
        child = dict(os.environ if base is None else base)
        child["SKFLEET_ROOT"] = str(self.root)
        child["SKFLEET_NODE"] = self.control
        return child

    def _operator(self) -> store.Writer:
        return store.Writer(role="operator", node=self.control, identity="capauth:drill@scratch")

    def _noded(self, node: str) -> store.Writer:
        return store.Writer(role="sknoded", node=node, identity="")

    def node_phases(self, *, now: datetime | None = None) -> dict[str, str]:
        """Each node's derived phase, as ``skfleet nodes`` would show it."""
        paths = FleetPaths(root=require_owned_root(self.root))
        return {v.name: v.phase for v in node_controller.node_views(paths, now=now)}

    def beat(self, node: str, *, age_s: float = 0.0, now: datetime | None = None) -> str:
        """Publish a heartbeat for a node, optionally backdated.

        Backdating rather than sleeping is what makes the death of the
        control seat testable: phase is a pure function of heartbeat age, so
        an aged timestamp is indistinguishable from a node that really went
        away, and the drill runs in milliseconds instead of five minutes.

        Args:
            node: Node whose heartbeat to write.
            age_s: How old the heartbeat should appear, in seconds.
            now: Reference time, defaults to now.

        Returns:
            The timestamp written.
        """
        paths = FleetPaths(root=require_owned_root(self.root))
        now = now or datetime.now(timezone.utc)
        ts = _iso(now - timedelta(seconds=age_s))
        store.write_node_file(paths, self._noded(node), "heartbeat.json", {"node": node, "ts": ts})
        return ts

    def kill_control(self, *, now: datetime | None = None) -> DrillStep:
        """Simulate the control seat going away, by aging its heartbeat.

        The heartbeat is backdated well past
        :data:`~skcapstone.fleet.node_controller.DEAD_AFTER_S`, not merely to
        it, so the drill does not depend on how long the assertions take to
        run afterwards.

        Returns:
            The step, whose revert restores a fresh heartbeat.
        """
        now = now or datetime.now(timezone.utc)
        age = DEAD_AFTER_S * 2
        ts = self.beat(self.control, age_s=age, now=now)
        return DrillStep(
            action="simulate control-seat loss",
            detail=(
                f"{self.control} heartbeat backdated to {ts} ({age}s old, "
                f"past DEAD_AFTER_S={DEAD_AFTER_S}) so its phase derives as Dead"
            ),
            revert=f"drill.beat({self.control!r}) to publish a fresh heartbeat",
        )

    def promote(self, *, now: datetime | None = None, force: bool = False) -> list[DrillStep]:
        """Run the promotion runbook inside the scratch tree.

        The precondition is checked rather than assumed: promoting while the
        old seat is still alive is the split-brain the fleet's single-writer
        ownership guard exists to prevent, so rehearsing it without noticing
        would teach the wrong reflex. ``force`` exists only so that failure
        mode can itself be drilled.

        Args:
            now: Reference time for the phase check.
            force: Promote even when the control seat is not Dead.

        Returns:
            One :class:`DrillStep` per runbook step, in execution order.

        Raises:
            DrillPreconditionError: The control seat is not Dead and force is
                not set.
        """
        paths = FleetPaths(root=require_owned_root(self.root))
        phases = self.node_phases(now=now)
        phase = phases.get(self.control, "Unknown")
        if phase != "Dead" and not force:
            raise DrillPreconditionError(
                f"refusing to promote while {self.control} is {phase}: two live "
                "control seats is a split brain, not a failover. Run kill_control() "
                "first, or pass force=True to drill this refusal itself."
            )
        operator = self._operator()
        steps = [
            DrillStep(
                action="cordon the lost seat",
                detail=f"{self.control} spec.cordoned = True",
                revert=f"skfleet uncordon {self.control}",
            ),
            DrillStep(
                action="taint the lost seat",
                detail=f"{self.control} taint control-seat=lost:NoSchedule",
                revert=f"skfleet untaint {self.control} control-seat",
            ),
            DrillStep(
                action="promote the warm replica",
                detail=f"{self.standby} spec.role {STANDBY_PROFILE} -> {CONTROL_PROFILE}",
                revert=f"skfleet set-role {self.standby} {STANDBY_PROFILE}",
            ),
        ]
        node_controller.cordon(paths, self.control, True, writer=operator)
        node_controller.set_taint(
            paths, self.control, "control-seat", "lost", "NoSchedule", writer=operator
        )
        node_controller.set_role(paths, self.standby, CONTROL_PROFILE, writer=operator)
        return steps

    def revert_promotion(self) -> list[DrillStep]:
        """Undo :meth:`promote` inside the scratch tree, step for step.

        Present so the reverts card ``0afa9ffb`` asks for are executable and
        not merely documented. A revert nobody has run is the same kind of
        claim as a runbook nobody has drilled.
        """
        paths = FleetPaths(root=require_owned_root(self.root))
        operator = self._operator()
        node_controller.set_role(paths, self.standby, STANDBY_PROFILE, writer=operator)
        node_controller.clear_taint(paths, self.control, "control-seat", writer=operator)
        node_controller.cordon(paths, self.control, False, writer=operator)
        return [
            DrillStep(
                action="demote the replica",
                detail=f"{self.standby} spec.role back to {STANDBY_PROFILE}",
                revert="re-run promote()",
            ),
            DrillStep(
                action="untaint the recovered seat",
                detail=f"{self.control} taint control-seat cleared",
                revert=f"skfleet taint {self.control} control-seat=lost:NoSchedule",
            ),
            DrillStep(
                action="uncordon the recovered seat",
                detail=f"{self.control} spec.cordoned = False",
                revert=f"skfleet cordon {self.control}",
            ),
        ]

    def role_of(self, node: str) -> str:
        """The install profile a node is currently bound to, or ``""``."""
        paths = FleetPaths(root=require_owned_root(self.root))
        payload = store.read_spec(paths, "node", node) or {}
        return (payload.get("spec") or {}).get("role", "") or ""

    def teardown(self) -> Path:
        """Delete the whole scratch tree.

        The guard runs one more time immediately before the recursive delete,
        deliberately duplicating the check :meth:`create` already passed. This
        is the single most destructive call in the module and it is the one
        place where trusting a value captured earlier would be unrecoverable.

        Returns:
            The root that was removed.

        Raises:
            UnsafeDrillRootError: The root is not a marked drill tree.
        """
        resolved = require_owned_root(self.root)
        shutil.rmtree(resolved)
        return resolved


def create(
    root: Path | str | None,
    *,
    control: str = CONTROL_NODE,
    standby: str = STANDBY_NODE,
    worker: str = WORKER_NODE,
    now: datetime | None = None,
    seed_drift: bool = True,
) -> DrillFleet:
    """Stand up a populated scratch fleet tree and return a handle on it.

    The tree carries three profiles, three admitted nodes bound to them, a
    published inventory and fresh heartbeat per node, one Service object with
    a placement and an observed status. That is the minimum that makes
    ``skfleet nodes``, ``get profiles``, ``services`` and ``node doctor``
    return something an operator can read, which is what a rehearsal needs:
    empty output cannot be told apart from a broken drill.

    Args:
        root: Where to build it. Guarded by :func:`claim_root`, so it must be
            outside the sovereign tree and must not already exist unmarked.
        control: Node name for the control seat.
        standby: Node name for the warm replica.
        worker: Node name for the stateless worker.
        now: Reference time for heartbeats and inventories.
        seed_drift: Give the worker one unexpected unit, so ``node doctor``
            produces a real finding. A harness that could only ever emit a
            clean report would not prove the doctor was consulted at all.

    Returns:
        A :class:`DrillFleet` handle.

    Raises:
        UnsafeDrillRootError: The root is unsafe or not ours.
        ValueError: A node name is not a valid fleet object name.
    """
    for name in (control, standby, worker):
        if not valid_name(name):
            raise ValueError(f"invalid drill node name: {name!r}")
    resolved = claim_root(root, now=now)
    fleet = DrillFleet(root=resolved, control=control, standby=standby, worker=worker)
    now = now or datetime.now(timezone.utc)
    paths = fleet.paths
    operator = fleet._operator()

    for profile_name, spec in _profile_specs().items():
        store.write_spec(
            paths, "profile", profile_name, spec, writer=operator, labels={"drill": "true"}
        )

    plan = (
        (control, CONTROL_PROFILE, {"cores": 8, "ram_gb": 32, "disk_gb": 500}),
        (standby, STANDBY_PROFILE, {"cores": 4, "ram_gb": 16, "disk_gb": 250}),
        (worker, WORKER_PROFILE, {"cores": 16, "ram_gb": 64, "disk_gb": 1000}),
    )
    inventories = {
        control: (["skgateway.service", "syncthing.service"], ["skcapstone"]),
        standby: (["syncthing.service"], ["skcapstone"]),
        worker: (
            ["skmodel.service"] + (["rogue-drill.service"] if seed_drift else []),
            [],
        ),
    }
    for node, role, capacity in plan:
        store.write_spec(
            paths,
            "node",
            node,
            {"role": role, "cordoned": False, "address": f"127.0.0.1  # {node} (drill)"},
            writer=operator,
            labels={"drill": "true", "role": role},
        )
        units, packages = inventories[node]
        noded = fleet._noded(node)
        store.write_node_file(
            paths,
            noded,
            "node.json",
            {
                "node": node,
                "status": {
                    "capacity": capacity,
                    "allocatable": capacity,
                    "inventory": _inventory(units, packages, now=now),
                },
                "conditions": [
                    {"type": "Ready", "status": "True", "reason": "DrillSeeded"},
                ],
            },
        )
        store.write_node_file(paths, noded, "join.json", {"node": node, "requestedAt": _iso(now)})
        fleet.beat(node, age_s=0.0, now=now)

    # One Service, placed on the control seat: the promotion drill is about
    # what happens to the things the lost seat was carrying, so an empty
    # placement table would rehearse the easy half of the problem.
    store.write_spec(
        paths,
        "service",
        "drill-gateway",
        {"unit": "skgateway.service", "runtime": "systemd-user", "failover": "manual"},
        writer=operator,
        labels={"drill": "true"},
    )
    store.write_placement(
        paths,
        "service",
        "drill-gateway",
        node=control,
        reason="drill seed: the control seat carries the gateway",
        writer=store.Writer(role="scheduler", node=control, identity=""),
    )
    store.write_status(
        paths,
        "service",
        "drill-gateway",
        node=control,
        status={"state": "active", "ready": True},
        conditions=[{"type": "Ready", "status": "True", "reason": "DrillSeeded"}],
        observed_generation=1,
        writer=fleet._noded(control),
    )
    return fleet


def summary(fleet: DrillFleet, *, now: datetime | None = None) -> dict:
    """A machine-readable snapshot of a drill tree, for CLI and assertions.

    Returns:
        Root, marker, per-node phase, per-node bound role, and the two
        thresholds that decide phase, so a reader can check the arithmetic
        rather than trust the verdict.
    """
    resolved = require_owned_root(fleet.root)
    return {
        "root": str(resolved),
        "marker": read_marker(resolved),
        "control": fleet.control,
        "standby": fleet.standby,
        "worker": fleet.worker,
        "phases": fleet.node_phases(now=now),
        "roles": {node: fleet.role_of(node) for node in fleet.nodes},
        "thresholds": {"notReadyAfterS": NOT_READY_AFTER_S, "deadAfterS": DEAD_AFTER_S},
    }


def attach(root: Path | str | None, **names: str) -> DrillFleet:
    """Reopen an existing drill tree by root, for a second CLI invocation.

    Args:
        root: The scratch root, re-guarded by :func:`require_owned_root`.
        **names: Optional ``control``/``standby``/``worker`` overrides.

    Raises:
        UnsafeDrillRootError: The root is unsafe, missing, or not ours.
    """
    return DrillFleet(root=require_owned_root(root), **names)
