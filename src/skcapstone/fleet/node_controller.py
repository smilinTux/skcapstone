"""NodeController: derived node health and the cordon action (spec 5.1).

Runs on the control-plane node. It is the only component allowed to mark a
node schedulable or not; sknoded self-reports raw observations only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import store
from .paths import FleetPaths, valid_name

NOT_READY_AFTER_S = 180
DEAD_AFTER_S = 300

#: The taint effects the scheduler actually implements, so the setter cannot
#: mint one that reads like policy and enforces nothing. NoSchedule is the
#: hard filter in scheduler.feasible(); PreferNoSchedule is the soft-avoid
#: ranking in scheduler.select(). NoExecute is deliberately absent: nothing
#: in this fleet evicts an already-running workload, so accepting it would
#: write a taint an operator would reasonably read as "get off this box".
TAINT_EFFECTS: tuple[str, ...] = ("NoSchedule", "PreferNoSchedule")


@dataclass
class NodeView:
    """One row of the fleet inventory (skfleet nodes)."""

    name: str
    phase: str
    cordoned: bool = False
    labels: dict = field(default_factory=dict)
    taints: list = field(default_factory=list)
    capacity: dict = field(default_factory=dict)
    allocatable: dict = field(default_factory=dict)
    heartbeat_age_s: float | None = None
    conditions: list = field(default_factory=list)
    # The install profile this node is bound to (epic 3bbf39ea). "" means
    # unbound, which is the correct reading before every node is backfilled
    # and must never be an error: the doctor skips it, it does not fail.
    role: str = ""


def _heartbeat_age(paths: FleetPaths, node: str, now: datetime) -> float | None:
    beat = store.read_node_file(paths, node, "heartbeat.json")
    if not beat or "ts" not in beat:
        return None
    try:
        ts = datetime.strptime(beat["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (now - ts).total_seconds()


def _phase(age: float | None) -> str:
    if age is None or age > DEAD_AFTER_S:
        return "Dead"
    if age > NOT_READY_AFTER_S:
        return "NotReady"
    return "Ready"


def node_views(paths: FleetPaths, *, now: datetime | None = None) -> list[NodeView]:
    """All known nodes: admitted (from spec) plus Pending joiners."""
    now = now or datetime.now(timezone.utc)
    admitted = {s["name"]: s for s in store.list_specs(paths, "node")}
    names = set(admitted)
    if paths.status.exists():
        for node_dir in paths.status.iterdir():
            if node_dir.is_dir() and (node_dir / "join.json").exists():
                names.add(node_dir.name)
    views = []
    for name in sorted(names):
        report = store.read_node_file(paths, name, "node.json") or {}
        spec = admitted.get(name)
        age = _heartbeat_age(paths, name, now)
        views.append(
            NodeView(
                name=name,
                phase="Pending" if spec is None else _phase(age),
                cordoned=bool((spec or {}).get("spec", {}).get("cordoned")),
                labels=(spec or {}).get("labels", {}),
                taints=(spec or {}).get("spec", {}).get("taints", []),
                capacity=report.get("status", {}).get("capacity", {}),
                allocatable=(
                    report.get("status", {}).get("allocatable")
                    or report.get("status", {}).get("capacity", {})
                ),
                heartbeat_age_s=age,
                conditions=report.get("conditions", []),
                role=(spec or {}).get("spec", {}).get("role", "") or "",
            )
        )
    return views


def cordon(paths: FleetPaths, name: str, cordoned: bool, *, writer: store.Writer) -> dict:
    """Set or clear the cordon flag on a node spec (operator action)."""
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    new_spec = dict(current.get("spec", {}), cordoned=cordoned)
    return store.write_spec(
        paths, "node", name, new_spec, writer=writer, labels=current.get("labels", {})
    )


def set_role(paths: FleetPaths, name: str, role: str, *, writer: store.Writer) -> dict:
    """Bind a node to an install profile by name (operator action).

    Mirrors set_actuation exactly: read the current spec, overlay one field,
    rewrite through store.write_spec preserving labels. Every other spec
    field (taints, cordoned, address, identity, actuate) survives, and the
    generation bumps by exactly one.

    Validation here is only that the role is a safe name. Whether a profile
    object of that name actually exists is the doctor's question (card
    cd5ef08b), deliberately: binding must not require the manifest to have
    landed first, or the two cards deadlock on each other.

    Raises:
        LookupError: no node object of that name.
        ValueError: role is not a valid object name.
    """
    if not valid_name(role):
        raise ValueError(f"invalid role name: {role!r}")
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    new_spec = dict(current.get("spec", {}), role=role)
    return store.write_spec(
        paths, "node", name, new_spec, writer=writer, labels=current.get("labels", {})
    )


def set_labels(
    paths: FleetPaths,
    name: str,
    *,
    add: dict[str, str] | None = None,
    remove: tuple[str, ...] = (),
    writer: store.Writer,
) -> dict:
    """Merge labels on a node spec, leaving every other field alone.

    This exists because there was no way to change one label without risking
    the rest of the object. The only tool available was ``skfleet apply``,
    which REPLACES the whole spec from the supplied document: during the
    promotion drill (card ``4c32df6f``) a label-only apply silently dropped
    ``taints``, ``cordoned`` and ``address``, thereby un-cordoning the node,
    and exited 0. So the documented way to fix a label corrupted the spec it
    was fixing.

    Labels are load-bearing, not decoration. ``scheduler.feasible`` filters on
    them and never reads ``spec.role``, so a node's labels are what decides
    whether anything can be placed on it at all.

    Merge semantics, mirroring set_role: read, overlay, rewrite through
    store.write_spec preserving the rest of the spec. Adds win over existing
    values of the same key; removes drop a key entirely and are silent when
    the key was already absent, so the call is idempotent in both directions.

    Raises:
        LookupError: no node object of that name.
        ValueError: a key is in both add and remove, or a key/value is not a
            valid name.
    """
    add = dict(add or {})
    overlap = sorted(set(add) & set(remove))
    if overlap:
        raise ValueError(
            f"label key(s) in both --add and --remove: {overlap}; "
            "one call cannot both set and unset a key"
        )
    for key, value in add.items():
        if not valid_name(key):
            raise ValueError(f"invalid label key: {key!r}")
        if not valid_name(value):
            raise ValueError(f"invalid label value: {value!r}")
    for key in remove:
        if not valid_name(key):
            raise ValueError(f"invalid label key: {key!r}")

    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")

    labels = dict(current.get("labels", {}))
    labels.update(add)
    for key in remove:
        labels.pop(key, None)

    return store.write_spec(
        paths, "node", name, dict(current.get("spec", {})), writer=writer, labels=labels
    )


def set_taint(
    paths: FleetPaths,
    name: str,
    key: str,
    value: str,
    effect: str,
    *,
    writer: store.Writer,
) -> dict:
    """Add or replace one taint on a node spec (operator action).

    Mirrors set_role and set_actuation: read the current spec, overlay one
    field, rewrite through store.write_spec preserving labels, so every other
    spec field survives and the generation bumps by exactly one. The taint
    list is never hand-edited on disk, which would bypass both the generation
    bump and the SPE writer block.

    Idempotent on the key: re-tainting an existing key REPLACES that entry in
    place rather than appending a second one. Two entries sharing a key would
    make scheduler.feasible() depend on list order, which is not a property
    an operator can see or reason about.

    Write-on-change: setting a taint that is already exactly present returns
    the current payload untouched. A runbook that re-asserts the same taint
    on every suspend must not churn the generation, and this tree is a live
    Syncthing folder where every write fans out to the whole fleet.

    Args:
        key: Taint key, e.g. "travel".
        value: Taint value, matched exactly by a toleration that names one.
        effect: One of TAINT_EFFECTS.

    Returns:
        The full node payload as on disk.

    Raises:
        LookupError: no node object of that name.
        ValueError: unsafe key, or an effect the scheduler does not honor.
    """
    if not valid_name(key):
        raise ValueError(f"invalid taint key: {key!r}")
    if effect not in TAINT_EFFECTS:
        raise ValueError(
            f"invalid taint effect: {effect!r} (want one of {', '.join(TAINT_EFFECTS)})"
        )
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    entry = {"key": key, "value": value, "effect": effect}
    existing = list(current.get("spec", {}).get("taints", []) or [])
    taints = [t for t in existing if t.get("key") != key]
    replaced_at = next((i for i, t in enumerate(existing) if t.get("key") == key), None)
    # Keep a replaced taint where it was, so a re-taint never silently
    # reorders the list an operator is reading in `describe`.
    taints.insert(len(taints) if replaced_at is None else replaced_at, entry)
    if taints == existing:
        return current
    new_spec = dict(current.get("spec", {}), taints=taints)
    return store.write_spec(
        paths, "node", name, new_spec, writer=writer, labels=current.get("labels", {})
    )


def clear_taint(paths: FleetPaths, name: str, key: str, *, writer: store.Writer) -> dict:
    """Remove every taint with this key from a node spec (operator action).

    Write-on-change for the same reason set_taint is: clearing a key that is
    not there is the normal resume-path case for a runbook, and it must be a
    silent no-op rather than a generation bump or an error.

    Returns:
        The full node payload as on disk, unchanged when the key was absent.

    Raises:
        LookupError: no node object of that name.
    """
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    existing = list(current.get("spec", {}).get("taints", []) or [])
    taints = [t for t in existing if t.get("key") != key]
    if taints == existing:
        return current
    new_spec = dict(current.get("spec", {}), taints=taints)
    return store.write_spec(
        paths, "node", name, new_spec, writer=writer, labels=current.get("labels", {})
    )


def set_actuation(paths: FleetPaths, name: str, enabled: bool, *, writer: store.Writer) -> dict:
    """Toggle the per-node actuation opt-in (operator action, spec R4).

    Every node is born report-only; this is the single explicit lever that
    lets sknoded on that node actuate. Preserves all other spec fields.
    """
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    new_spec = dict(current.get("spec", {}), actuate=enabled)
    return store.write_spec(
        paths, "node", name, new_spec, writer=writer, labels=current.get("labels", {})
    )
