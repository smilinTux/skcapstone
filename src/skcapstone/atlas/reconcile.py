"""The three-registry reconciler: declared vs desired vs actual (card 0e050100).

Three registries on this estate independently claim to know what runs where,
and none of them is authoritative:

1. **declared**  the skos capability registry. An installed pack's
   ``fleet_objects`` step names the objects that pack said should exist.
2. **desired**   the fleet store. Objects under ``objects/{cronjob,service}``
   plus the placements that address them to a node. This is the only
   desired-state in the architecture.
3. **actual**    systemd ``--user`` units on the node. What is really running.

Nothing joined them, so the estate accumulated the drift this module exists to
name: fleet placements that correspond to no workload, a node advertising a
capability while it has been Dead for two days, and units failing where nothing
declares them.

One thing lives in none of the three and is reported apart from the join:
a placement with no object behind it (:data:`PLACEMENT_WITHOUT_OBJECT`). It has
no spec, so it has no unit, so it cannot be joined in unit space without
inventing a name. The reconciler used to invent one. It does not any more.

Why this is legal for Atlas
---------------------------
Atlas is frozen. The standing governance position is that it gains no new
authority, and ``docs/ATLAS_CONSTITUTION.md`` charters it as read-only over apps
and infra. Reporting a delta grants no authority: it is the same posture Article
1 already puts Atlas into whenever the freeze flag is set ("observes and reports
but takes no action"), made permanent and made testable. So this module writes
nothing, actuates nothing, creates nothing (not even the directories it reads),
and does not read the coordination board, which ``docs/fleet/seat-charters.md``
records that Atlas provably does not read.

Two mechanisms keep that from being a promise. :func:`assert_read_only` refuses
any argv that is not an observation, at runtime, on every command this module
issues. And ``tests/test_atlas_reconcile.py`` parses every module in this package
and fails on any call to an actuation verb or a filesystem write. A frozen seat
with no test proving it is frozen is just a promise.

Why the readers are injected
----------------------------
The three readers are parameters, not imports of live system calls, following
``fleet/converge.py`` (injected ``runner`` and ``writer``) and
``fleet/actuation.py`` (injected ``Runner``). That is what lets the whole delta
engine be tested with no live systemd, no populated fleet store and no installed
skos. A test that needs the real box is not a test anyone can trust at 4am.

Every default reader degrades to empty rather than raising. An unreadable
registry is reported as an unreadable registry (see
:attr:`ReconcileReport.notes`); it never takes the observer down with it.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from ..fleet import store
from ..fleet.paths import FleetPaths

# ---------------------------------------------------------------------------
# Delta vocabulary
# ---------------------------------------------------------------------------

#: A pack declared the object and the fleet store never got it. The install
#: chain broke between ``fleet_objects`` and the store.
DECLARED_NOT_DESIRED = "declared_not_desired"

#: The fleet store wants it and the node has no unit for it. The noroc2027
#: skfleet-rotate shape: desired state that never reached the node.
DESIRED_NOT_ACTUAL = "desired_not_actual"

#: A unit is running that nothing in the fleet store asks for. Either hand
#: installed outside the chain, or left behind by something now deleted.
ACTUAL_NOT_DESIRED = "actual_not_desired"

#: The fleet store wants it under a runtime this pass cannot see. A Service
#: with ``runtime: docker`` names a container, not a systemd unit, so systemd
#: saying nothing about it is not evidence either way. Reported as its own
#: class rather than folded into :data:`DESIRED_NOT_ACTUAL`, because "absent"
#: and "not looked at" are different findings and an operator acts on them
#: differently.
DESIRED_UNOBSERVABLE = "desired_unobservable"

#: A placement exists and no object stands behind it (card 0e0a0600).
#:
#: This is its own class, not a flavour of :data:`DESIRED_NOT_ACTUAL`, because
#: it cannot be resolved inside the three-registry join at all. The join runs
#: in unit space, and a unit name comes from an object's spec. With no spec
#: there is nothing to project: the only way to put such a placement into unit
#: space is to invent ``<name>.service``, which is exactly what this module
#: used to do, and exactly what :func:`unit_for`'s own docstring warns against.
#: A reconciler whose job is reporting drift must not manufacture it.
#:
#: The estate's concrete case is ``placements/job/``, a deliberately
#: object-less advisory namespace: ``skharness`` fleet_dispatch writes one
#: record per dispatched coordination card as an audit trail ("placement is
#: advisory routing"), never creates an object, and ``job`` is not a fleet
#: object kind. Every such record is a scheduler decision keyed to the
#: coordination board, which ``docs/fleet/seat-charters.md`` records that Atlas
#: provably does not read. So it lives in none of the three registries this
#: module joins, and calling it "desired but not actual" is a category error,
#: not merely a wrong count. Reported with ``unit=""`` and no claim about
#: systemd either way.
PLACEMENT_WITHOUT_OBJECT = "placement_without_object"

DELTA_KINDS = (
    DECLARED_NOT_DESIRED,
    DESIRED_NOT_ACTUAL,
    DESIRED_UNOBSERVABLE,
    ACTUAL_NOT_DESIRED,
    PLACEMENT_WITHOUT_OBJECT,
)

#: Spec keys that declare what a workload runs. A Service spec carrying none of
#: them only references a unit something else must have installed: the fleet
#: store can want it forever and never be able to create it.
EXEC_KEYS = ("exec", "execStart", "command")

#: Runtimes whose workload is a systemd unit this pass can observe.
SYSTEMD_RUNTIMES = ("", "systemd-user", "systemd")

#: Object kinds that correspond to a workload on a node. ``node``, ``profile``
#: and ``operatorapp`` objects describe the estate, not something that runs, so
#: they are not reconciled against units.
WORKLOAD_KINDS = ("cronjob", "service")

#: Unit-name prefixes that belong to this estate.
#:
#: Scoping is not cosmetic. An unscoped ``actual`` reader reports every desktop
#: session unit (``gnome-terminal-server``, ``gvfs-*``, ``pipewire``) as running
#: with nothing declaring it, which is true and useless: hundreds of false
#: positives bury the handful of real ones and the report stops being read. The
#: same narrowing instinct as ``operator_seat/skos_adapter.py``'s deliberately
#: narrow GradingBacklog condition. The scope is a parameter, so an operator who
#: disagrees can widen it without editing this module.
ESTATE_UNIT_PREFIXES = (
    "anthropic-proxy",
    "atlas-",
    "beelama",
    "capauth-",
    "claude-code-api",
    "cloud9-",
    "comfyui",
    "hermes-",
    "jarvis-",
    "kokoro-",
    "livekit-",
    "lumina-",
    "nextcloud-",
    "ollama",
    "sk",
    "whisper",
    "wiki-",
)


class ReadOnlyViolationError(RuntimeError):
    """Raised when a command that is not pure observation reaches the guard."""


#: Binaries this module may invoke at all. Everything else is refused.
_READ_ONLY_BINARIES = frozenset({"systemctl", "journalctl", "systemd-analyze"})

#: The only ``systemctl`` subcommands that observe. Deliberately an allowlist:
#: a denylist of actuation verbs silently permits every verb added upstream
#: after this file was written.
_READ_ONLY_SYSTEMCTL = frozenset(
    {
        "cat",
        "is-active",
        "is-enabled",
        "is-failed",
        "list-timers",
        "list-unit-files",
        "list-units",
        "show",
        "show-environment",
        "status",
    }
)


def assert_read_only(cmd: Sequence[str]) -> None:
    """Refuse any argv that is not pure observation.

    This is the control, not the documentation of one. Every command this module
    issues passes through here first, so a future edit that reaches for an
    actuation verb fails closed at runtime instead of quietly widening Atlas's
    authority.

    Args:
        cmd: The argv about to be executed.

    Raises:
        ReadOnlyViolationError: The binary is not an observation tool, or the
            subcommand is not on the read-only allowlist.
    """
    argv = [str(token) for token in cmd]
    if not argv:
        raise ReadOnlyViolationError("empty command")
    binary = Path(argv[0]).name
    if binary not in _READ_ONLY_BINARIES:
        raise ReadOnlyViolationError(f"atlas may not invoke {binary!r}; observation binaries only")
    if binary != "systemctl":
        return
    for token in argv[1:]:
        if token.startswith("-"):
            continue
        if token in _READ_ONLY_SYSTEMCTL:
            return
        raise ReadOnlyViolationError(f"systemctl {token!r} is not a read-only subcommand")
    raise ReadOnlyViolationError("systemctl invoked with no subcommand")


# ---------------------------------------------------------------------------
# Typed rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DeclaredItem:
    """One object a pack said should exist.

    Attributes:
        kind: Normalized object kind ("cronjob", "service", "job").
        name: Object name as the pack declared it.
        pack: The skos pack that declared it.
        unit: The systemd unit this object projects onto.
    """

    kind: str
    name: str
    pack: str
    unit: str


@dataclass(frozen=True)
class DesiredItem:
    """One object the fleet store holds as desired state.

    Attributes:
        kind: Normalized object kind.
        name: Object name.
        node: Node the placement addresses it to, or "" when unplaced.
        unit: The systemd unit this object projects onto.
        generation: Spec generation, 0 when unknown.
        declares_exec: True when the spec carries one of :data:`EXEC_KEYS`.
            False means the object merely references a unit installed by
            something outside the fleet chain.
        runtime: The spec's declared runtime ("systemd-user" when unset).

    Every row here is backed by an object under ``objects/``. A placement with
    no object behind it is a :class:`PlacementItem`, never a DesiredItem: it
    has no spec, so it has no unit, and a row type that requires a unit would
    force one to be invented.
    """

    kind: str
    name: str
    node: str
    unit: str
    generation: int = 0
    declares_exec: bool = True
    runtime: str = "systemd-user"

    @property
    def observable(self) -> bool:
        """True when this pass can see the workload through systemd."""
        return self.runtime in SYSTEMD_RUNTIMES


@dataclass(frozen=True)
class PlacementItem:
    """One placement record with no object behind it.

    Deliberately carries no ``unit`` field. A unit name is a projection of a
    spec, and there is no spec; a field for it would only ever hold a guess.

    Attributes:
        kind: Normalized placement kind. ``job`` on this estate, which is not
            a fleet object kind at all (see :data:`PLACEMENT_WITHOUT_OBJECT`).
        name: Placement name. For ``job`` this is a coordination card id.
        node: Node the placement addresses, or "" when unset.
        generation: Placement generation, 0 when unknown.
    """

    kind: str
    name: str
    node: str
    generation: int = 0

    @property
    def workload_kind(self) -> bool:
        """True when the kind names something that could have had a unit."""
        return self.kind in WORKLOAD_KINDS


@dataclass(frozen=True)
class ActualItem:
    """One systemd unit observed on the node.

    Attributes:
        unit: Full unit name including its suffix.
        state: ActiveState as reported (active, failed, inactive, ...).
        load: LoadState as reported (loaded, not-found, ...).
    """

    unit: str
    state: str
    load: str = ""


@dataclass(frozen=True)
class Delta:
    """One disagreement between two registries.

    Attributes:
        kind: One of :data:`DELTA_KINDS`.
        unit: The unit name the three registries are joined on. Empty for
            :data:`PLACEMENT_WITHOUT_OBJECT`, which has no unit and claims none.
        name: Object name, or the unit stem when only systemd knows about it.
        object_kind: Normalized object kind, or "unit" when only systemd knows.
        node: Node the delta was observed for.
        state: Observed unit state, "" when there is no unit.
        declared: Whether any pack declared this object.
        has_object: False for a placement with no object behind it.
        declares_exec: False when the desired object only references a unit.
        detail: Human-readable explanation, including which registries hold it.
    """

    kind: str
    unit: str
    name: str
    object_kind: str
    node: str
    state: str = ""
    declared: bool = False
    has_object: bool = True
    declares_exec: bool = True
    detail: str = ""


@dataclass(frozen=True)
class ReconcileReport:
    """The delta between the three registries for one node.

    Attributes:
        node: The node the report covers.
        declared: What the skos capability registry says should exist.
        desired: What the fleet store holds as desired state for this node.
        actual: In-scope systemd units observed on this node.
        placements: Placement records with no object behind them. Kept apart
            from ``desired`` because they have no unit to join on.
        deltas: Every disagreement, ordered by class then unit then name.
        notes: Degraded reads and other caveats. A registry that could not be
            read is reported here, never silently treated as empty agreement.
    """

    node: str
    declared: list[DeclaredItem] = field(default_factory=list)
    desired: list[DesiredItem] = field(default_factory=list)
    actual: list[ActualItem] = field(default_factory=list)
    placements: list[PlacementItem] = field(default_factory=list)
    deltas: list[Delta] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def in_agreement(self) -> bool:
        """True when the three registries hold the same set of units."""
        return not self.deltas

    def by_kind(self, kind: str) -> list[Delta]:
        """Every delta of one class."""
        return [d for d in self.deltas if d.kind == kind]

    def summary(self) -> dict[str, int]:
        """Delta counts per class, for a dashboard tile or a brief."""
        return {kind: len(self.by_kind(kind)) for kind in DELTA_KINDS}

    def placements_without_object(self) -> list[PlacementItem]:
        """Placements that address an object the store does not hold."""
        return list(self.placements)

    def referenced_only(self) -> list[DesiredItem]:
        """Objects that name a unit but declare nothing that would create it.

        Every one of these is desired state the fleet chain cannot realise on
        its own: the unit has to arrive from outside, and when it does not, the
        store keeps wanting it with no way to act. That is a property of the
        object, not of any node, so it is reported once per object rather than
        as a per-node delta.
        """
        return [d for d in self.desired if not d.declares_exec]


DeclaredReader = Callable[[], Sequence[DeclaredItem]]
DesiredReader = Callable[[FleetPaths, str], Sequence[DesiredItem]]
ActualReader = Callable[[str], Sequence[ActualItem]]
PlacementReader = Callable[[FleetPaths, str], Sequence[PlacementItem]]
Runner = Callable[[list[str]], subprocess.CompletedProcess]


# ---------------------------------------------------------------------------
# Projection into unit space
# ---------------------------------------------------------------------------


def normalize_kind(kind: str) -> str:
    """Lowercase an object kind. CronJob, Cronjob and cronjob are one kind."""
    return (kind or "").strip().lower()


def unit_for(kind: str, name: str, spec: dict | None = None) -> str:
    """Project an object onto the systemd unit it corresponds to.

    All three registries are joined in unit space, because it is the only naming
    every one of them can express. A Service spec may name a unit that is not
    ``<name>.service``, and following ``spec.unit`` matters: without it a
    correctly installed service whose unit was renamed reads as missing, and the
    report invents drift that is not there.

    Args:
        kind: Object kind, any casing.
        name: Object name.
        spec: The object's ``spec`` block, when available.

    Returns:
        The unit name, including its suffix.
    """
    kind_l = normalize_kind(kind)
    spec = spec or {}
    explicit = spec.get("unit")
    if isinstance(explicit, str) and explicit.strip():
        explicit = explicit.strip()
        runtime = str(spec.get("runtime") or "systemd-user")
        if "." not in explicit and runtime in SYSTEMD_RUNTIMES:
            # systemctl itself reads a bare name as <name>.service; joining
            # on the bare name would invent drift for a correctly named unit.
            return f"{explicit}.service"
        return explicit
    if kind_l == "cronjob":
        return f"{name}.timer"
    return f"{name}.service"


def in_scope(
    rows: Iterable[ActualItem],
    *,
    prefixes: Sequence[str] = ESTATE_UNIT_PREFIXES,
) -> list[ActualItem]:
    """Keep only the units this estate is responsible for.

    Args:
        rows: Observed units.
        prefixes: Unit-name prefixes considered in scope.

    Returns:
        The in-scope subset, order preserved.
    """
    return [row for row in rows if row.unit.startswith(tuple(prefixes))]


# ---------------------------------------------------------------------------
# Default reader: declared (the skos capability registry)
# ---------------------------------------------------------------------------


def _skos_registry_path() -> Path | None:
    """Resolve ``<data root>/registry/installed.json`` without creating it.

    Mirrors ``skos.paths.data_root``'s precedence rather than calling it, for
    the same reason ``operator_seat/skos_adapter.py`` mirrors
    ``watchdog_home()``: the skos accessor calls ``ensure_tree()``, which mkdirs
    as a side effect. An observer that creates the store it is only supposed to
    look at manufactures the state it reports on.
    """
    env = os.environ.get("SK_DATA_ROOT", "").strip()
    if env:
        return Path(env).expanduser() / "registry" / "installed.json"
    profile = os.environ.get("SKOS_PROFILE", "local").strip().lower()
    if profile == "local":
        return Path.home() / "var" / "data" / "sk" / "registry" / "installed.json"
    if profile == "cluster":
        return Path("/var/data/sk/registry/installed.json")
    return None


def _skos_packs_dir() -> Path | None:
    """Locate the skos packs directory, or None when skos is not importable."""
    env = os.environ.get("SKOS_PACKS_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    try:
        import importlib.util

        spec = importlib.util.find_spec("skos")
    except Exception:
        return None
    if spec is None or not spec.origin:
        return None
    return Path(spec.origin).parent / "packs"


def _read_json(path: Path) -> dict | None:
    """Load one JSON object, or None when absent or unreadable."""
    try:
        if not path.is_file():
            return None
        return json.loads(path.read_text())
    except Exception:
        return None


def _pack_declared_objects(pack: str, packs_dir: Path | None) -> list[DeclaredItem]:
    """Every fleet object one pack's manifest declares."""
    if packs_dir is None:
        return []
    manifest = _read_json(packs_dir / pack / "skworld.module.json")
    if not manifest:
        return []
    out: list[DeclaredItem] = []
    steps = ((manifest.get("install") or {}).get("steps")) or []
    for step in steps:
        if not isinstance(step, dict) or step.get("kind") != "fleet_objects":
            continue
        for rel in step.get("objects") or []:
            payload = _read_json(packs_dir / pack / str(rel))
            if not payload:
                continue
            kind = normalize_kind(payload.get("kind", ""))
            name = str(payload.get("name", "")).strip()
            if not kind or not name:
                continue
            out.append(
                DeclaredItem(
                    kind=kind,
                    name=name,
                    pack=pack,
                    unit=unit_for(kind, name, payload.get("spec")),
                )
            )
    return out


def skos_declared(
    *,
    registry_path: Path | None = None,
    packs_dir: Path | None = None,
) -> list[DeclaredItem]:
    """Read the declared registry: every fleet object an installed pack names.

    Args:
        registry_path: Override for ``registry/installed.json``.
        packs_dir: Override for the skos packs directory.

    Returns:
        Declared objects, sorted by unit. Empty when skos is absent or the
        registry was never initialized, which is itself a finding rather than
        an error.
    """
    path = registry_path or _skos_registry_path()
    if path is None:
        return []
    installed = _read_json(path)
    if not installed:
        return []
    packs = packs_dir if packs_dir is not None else _skos_packs_dir()
    out: list[DeclaredItem] = []
    for pack in sorted(installed):
        out.extend(_pack_declared_objects(str(pack), packs))
    return sorted(out, key=lambda item: item.unit)


# ---------------------------------------------------------------------------
# Default reader: desired (the fleet store)
# ---------------------------------------------------------------------------


def fleet_desired(paths: FleetPaths, node: str) -> list[DesiredItem]:
    """Read the desired registry: fleet objects and placements for one node.

    Objects carry no node of their own; a placement addresses them. An object
    with no placement is recorded with an empty node, meaning "desired but
    unplaced", and is reconciled on every node rather than vanishing from every
    report.

    Only objects are desired state. A placement with no object behind it is
    not promoted into a DesiredItem here, because doing so requires a unit name
    and there is no spec to derive one from; the old sweep called
    :func:`unit_for` with no spec and fabricated ``<name>.service`` for every
    ``placements/job/`` record, a third of this node's desired-not-actual
    count. Those records are read by :func:`fleet_placements_without_object`
    and reported as :data:`PLACEMENT_WITHOUT_OBJECT` instead.

    Args:
        paths: The fleet tree to read. Never created if absent.
        node: The node to scope placements to.

    Returns:
        Desired objects for this node, sorted by unit.
    """
    placed = _placements_by_key(paths)
    out: list[DesiredItem] = []
    for kind in WORKLOAD_KINDS:
        for payload in store.list_specs(paths, kind):
            name = str(payload.get("name", "")).strip()
            if not name:
                continue
            placement = placed.get((kind, name)) or {}
            spec = payload.get("spec") or {}
            if not isinstance(spec, dict):
                spec = {}
            out.append(
                DesiredItem(
                    kind=kind,
                    name=name,
                    node=str(placement.get("node", "")),
                    unit=unit_for(kind, name, spec),
                    generation=int(payload.get("generation", 0) or 0),
                    declares_exec=any(spec.get(k) for k in EXEC_KEYS),
                    runtime=str(spec.get("runtime") or "systemd-user"),
                )
            )
    scoped = [item for item in out if item.node in (node, "")]
    return sorted(scoped, key=lambda item: item.unit)


def _placements_by_key(paths: FleetPaths) -> dict[tuple[str, str], dict]:
    """Every placement record, keyed by normalized (kind, name)."""
    placed: dict[tuple[str, str], dict] = {}
    for placement in store.list_placements(paths):
        kind = normalize_kind(placement.get("kind", ""))
        name = str(placement.get("name", "")).strip()
        if kind and name:
            placed[(kind, name)] = placement
    return placed


def fleet_placements_without_object(paths: FleetPaths, node: str) -> list[PlacementItem]:
    """Read every placement whose object the store does not hold.

    Carried through for every node, not only ``node``: such a placement is a
    property of the store, and scoping it away on the node it happens not to
    address would make it vanish from every node's report but one. ``node`` is
    accepted for reader-signature symmetry with :func:`fleet_desired`.

    Nothing here projects onto a unit. See :data:`PLACEMENT_WITHOUT_OBJECT`
    for why that is the point rather than an omission.

    Args:
        paths: The fleet tree to read. Never created if absent.
        node: The node being reported on. Unused for scoping, by design.

    Returns:
        Placement records with no object behind them, sorted by kind then name.
    """
    placed = _placements_by_key(paths)
    # "Object exists" means what it means to fleet_desired: a spec the store
    # can read. A conflict copy or an unparseable spec is not an object, and a
    # placement addressing one must not fall between the two readers.
    held: dict[str, set[str]] = {}
    for kind in sorted({kind for kind, _ in placed}):
        held[kind] = {
            str(payload.get("name", "")).strip() for payload in store.list_specs(paths, kind)
        }
    out: list[PlacementItem] = []
    for (kind, name), placement in sorted(placed.items()):
        if name in held.get(kind, set()):
            continue
        out.append(
            PlacementItem(
                kind=kind,
                name=name,
                node=str(placement.get("node", "")),
                generation=int(placement.get("placementGeneration", 0) or 0),
            )
        )
    return out


# ---------------------------------------------------------------------------
# Default reader: actual (systemd user units)
# ---------------------------------------------------------------------------


def default_runner(cmd: list[str]) -> subprocess.CompletedProcess:
    """Run one observation command, captured, bounded, never check=True.

    Mirrors ``fleet/actuation.py``'s runner, with the read-only guard in front:
    this runner physically cannot be handed an actuation argv.
    """
    assert_read_only(cmd)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)


def parse_unit_listing(text: str) -> list[ActualItem]:
    """Parse ``systemctl list-units --no-legend`` output into typed rows."""
    out: list[ActualItem] = []
    for line in (text or "").splitlines():
        fields = line.strip().lstrip("*● ").split()
        if len(fields) < 3:
            continue
        unit = fields[0]
        if "." not in unit:
            continue
        out.append(ActualItem(unit=unit, load=fields[1], state=fields[2]))
    return out


def systemd_actual(
    node: str,
    *,
    runner: Runner = default_runner,
    prefixes: Sequence[str] = ESTATE_UNIT_PREFIXES,
) -> list[ActualItem]:
    """Read the actual registry: in-scope systemd ``--user`` units.

    Observation only. The single command issued is a listing, checked against
    :func:`assert_read_only` before it is handed to the runner, so an injected
    runner is never given anything but an observation.

    Args:
        node: The node being observed. Present for reader-signature symmetry;
            systemd is always local to the caller.
        runner: Injected command runner, so tests need no live systemd.
        prefixes: Unit-name prefixes considered in scope.

    Returns:
        In-scope units, sorted by unit name. Empty when systemd is unavailable,
        which degrades to "nothing observed", never to an exception.
    """
    cmd = ["systemctl", "--user", "list-units", "--all", "--no-legend", "--no-pager"]
    assert_read_only(cmd)
    try:
        completed = runner(cmd)
    except Exception:
        return []
    if completed.returncode != 0:
        return []
    rows = parse_unit_listing(completed.stdout or "")
    return sorted(in_scope(rows, prefixes=prefixes), key=lambda item: item.unit)


# ---------------------------------------------------------------------------
# The reconciler
# ---------------------------------------------------------------------------


def _safe(reader, label: str, notes: list[str], *args):
    """Call one reader, degrading a failure to empty plus a recorded note."""
    try:
        return list(reader(*args))
    except Exception as exc:
        notes.append(f"{label} registry unreadable: {type(exc).__name__}: {exc}")
        return []


def compute_deltas(
    declared: Sequence[DeclaredItem],
    desired: Sequence[DesiredItem],
    actual: Sequence[ActualItem],
    node: str,
    placements: Sequence[PlacementItem] = (),
) -> list[Delta]:
    """Join the three registries in unit space and name every disagreement.

    Pure: no I/O, no clock, no environment. Everything the caller needs to
    reproduce a report is in the arguments.

    Args:
        declared: What packs said should exist.
        desired: What the fleet store holds for this node.
        actual: What systemd has on this node.
        node: The node the report covers.
        placements: Placement records with no object behind them. These never
            enter the unit-space join; each becomes one
            :data:`PLACEMENT_WITHOUT_OBJECT` delta with an empty unit.

    Returns:
        Deltas ordered by class then unit then name.
    """
    declared_by_unit = {item.unit: item for item in declared}
    desired_by_unit = {item.unit: item for item in desired}
    actual_by_unit = {item.unit: item for item in actual}

    out: list[Delta] = []

    for unit in sorted(set(declared_by_unit) - set(desired_by_unit)):
        item = declared_by_unit[unit]
        out.append(
            Delta(
                kind=DECLARED_NOT_DESIRED,
                unit=unit,
                name=item.name,
                object_kind=item.kind,
                node=node,
                declared=True,
                detail=(
                    f"pack {item.pack!r} declares {item.kind}/{item.name}, "
                    "but the fleet store holds no such object: the install chain "
                    "stopped before fleet_objects reached the store"
                ),
            )
        )

    for unit in sorted(set(desired_by_unit) - set(actual_by_unit)):
        item = desired_by_unit[unit]
        was_declared = unit in declared_by_unit
        if not item.observable:
            kind = DESIRED_UNOBSERVABLE
            why = (
                f"runtime {item.runtime!r} is not a systemd unit; this pass observes "
                "systemd only and cannot say whether it runs"
            )
        elif was_declared:
            kind = DESIRED_NOT_ACTUAL
            why = (
                f"declared by pack {declared_by_unit[unit].pack!r} and present in the "
                "fleet store, but the node has no unit for it: the install never "
                "reached this node"
            )
        elif not item.declares_exec:
            kind = DESIRED_NOT_ACTUAL
            why = (
                "in the fleet store, undeclared by any pack, and the node has no unit "
                f"named {unit!r}: the object only references that unit, so nothing in "
                "the fleet chain can create it"
            )
        else:
            kind = DESIRED_NOT_ACTUAL
            why = (
                "in the fleet store but undeclared by any pack and absent from "
                "systemd: desired state that never reached this node"
            )
        out.append(
            Delta(
                kind=kind,
                unit=unit,
                name=item.name,
                object_kind=item.kind,
                node=item.node or node,
                declared=was_declared,
                declares_exec=item.declares_exec,
                detail=why,
            )
        )

    for unit in sorted(set(actual_by_unit) - set(desired_by_unit)):
        item = actual_by_unit[unit]
        out.append(
            Delta(
                kind=ACTUAL_NOT_DESIRED,
                unit=unit,
                name=unit.rsplit(".", 1)[0],
                object_kind="unit",
                node=node,
                state=item.state,
                declared=unit in declared_by_unit,
                detail=(
                    f"unit is {item.state} on this node but no fleet object asks "
                    "for it: installed outside the chain, or left behind by a "
                    "deleted object"
                ),
            )
        )

    declared_keys = {(item.kind, item.name) for item in declared}
    for item in placements:
        where = item.node or node
        if item.workload_kind:
            why = (
                f"placements/{item.kind}/{item.name} addresses {where!r} but "
                f"objects/{item.kind}/{item.name}.json does not exist: the object it "
                "routed is gone. No unit name can be derived without a spec, so this "
                "pass makes no claim about systemd"
            )
        else:
            why = (
                f"placements/{item.kind}/{item.name} addresses {where!r} but "
                f"{item.kind!r} is not a fleet object kind: an advisory routing "
                "record, not desired state. No unit name can be derived without a "
                "spec, so this pass makes no claim about systemd"
            )
        out.append(
            Delta(
                kind=PLACEMENT_WITHOUT_OBJECT,
                unit="",
                name=item.name,
                object_kind=item.kind,
                node=where,
                declared=(item.kind, item.name) in declared_keys,
                has_object=False,
                declares_exec=False,
                detail=why,
            )
        )

    order = {kind: index for index, kind in enumerate(DELTA_KINDS)}
    return sorted(out, key=lambda d: (order.get(d.kind, len(order)), d.unit, d.name))


def reconcile(
    paths: FleetPaths,
    node: str,
    *,
    declared_reader: DeclaredReader = skos_declared,
    desired_reader: DesiredReader = fleet_desired,
    actual_reader: ActualReader = systemd_actual,
    placement_reader: PlacementReader = fleet_placements_without_object,
) -> ReconcileReport:
    """Report the delta between declared, desired and actual for one node.

    Writes nothing and actuates nothing. Atlas gains no authority from calling
    it: the result is a description, and acting on that description is somebody
    else's seat.

    Args:
        paths: The fleet tree to read. Never created if absent.
        node: The node to reconcile.
        declared_reader: Reads the skos capability registry.
        desired_reader: Reads the fleet store.
        actual_reader: Reads systemd units.
        placement_reader: Reads placements with no object behind them.

    Returns:
        A :class:`ReconcileReport` whose ``deltas`` is empty exactly when the
        three registries agree and no placement lacks its object.
    """
    notes: list[str] = []
    declared = _safe(declared_reader, "declared", notes)
    desired = _safe(desired_reader, "desired", notes, paths, node)
    actual = _safe(actual_reader, "actual", notes, node)
    placements = _safe(placement_reader, "placements", notes, paths, node)
    return ReconcileReport(
        node=node,
        declared=declared,
        desired=desired,
        actual=actual,
        placements=placements,
        deltas=compute_deltas(declared, desired, actual, node, placements),
        notes=notes,
    )
