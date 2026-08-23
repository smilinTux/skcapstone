# Fleet Control Plane, Phase 2: Implementation Plan (TDD, bite-sized)

Date: 2026-07-28
Author: Fable
Parent spec: `2026-07-27-skworld-fleet-control-plane-design.md` (rev 2)
Executes: Phase 2 (Cards 2.1, 2.2, 2.3; Card 2.1b listed as a gated stub)
Prior plan: `2026-07-27-fleet-phase1-implementation-plan.md` (merged; all
interfaces referenced below were verified against the merged code, not the
plan text)

## Goal

Ship capacity-aware, fleet-wide placement: a stateless scheduler v1 (filter
plus least-loaded) writing auditable placement records, the autopilot harness
re-homed onto it so a run never targets a dead, cordoned, or traveling node,
and a `skfleet placements` view with a one-line reason per decision.

## Architecture

The scheduler is a pure function over Phase 1 `NodeView` objects plus a
workload's requirements; its only write surface is `placements/<kind>/
<name>.json` through a new `store.write_placement` helper (single-writer:
scheduler role only, on the control-plane node). The autopilot harness in the
skharness repo gains a thin dispatch gate: before the swarm phase, each
selected card is placed by the scheduler and only cards placed on THIS node
proceed; the coord claim (made node-scoped in this phase) remains the
authoritative execution gate, so a stale placement can never double-run a
card. Everything is level-triggered and idempotent: re-running with unchanged
inputs writes nothing.

## Tech stack

- Python 3.11+, stdlib + click, pytest. Same venv and conventions as Phase 1.
- Repo 1 (Cards 2.1, 2.3): `/home/cbrd21/clawd/skcapstone-repos/skcapstone/`
  (src layout, package `skcapstone.fleet`). Test command from repo root:
  `~/.skenv/bin/python -m pytest tests/fleet/ -v`
- Repo 2 (Card 2.2): `/home/cbrd21/clawd/skcapstone-repos/skharness/`
  (package `skharness.autocode`). Test command from repo root:
  `~/.skenv/bin/python -m pytest tests/test_autopilot_fleet_dispatch.py tests/test_autopilot_fleet_gate.py tests/test_autopilot_orchestrator.py -v`
  skcapstone is an OPTIONAL sibling for skharness (not a declared dependency);
  tests that import it carry the existing `needs_skcapstone` marker, which
  `tests/conftest.py` auto-skips when the sibling is absent.
- Reused Phase 1 code (real, merged signatures, verified):
  `FleetPaths`, `paths.placement_path(kind, name)`, `default_paths()`,
  `self_node_name()`, `store.Writer(role, node, identity)`,
  `store.write_spec(paths, kind, name, spec, *, writer, labels=None)`,
  `store.read_spec`, `store.merged`, `store.is_frozen`,
  `store.actuation_allowed(paths)`, `store.OwnershipError`,
  `events.emit(paths, writer, *, kind, name, type, reason, message, now=None)`,
  `node_controller.NodeView`, `node_controller.node_views(paths, *, now=None)`,
  `capacity.node_capacity()`, `sknoded.build_node_report`, `sknoded.run_once`.

## Global constraints (binding, copied from the spec)

1. Single-writer-per-FILE, fleet-wide: every file in the fleet tree has
   exactly one writer in the whole fleet, ever. The scheduler NEVER writes
   status. sknoded NEVER writes placements or spec. The scheduler writes only
   `placements/**` (spec 3.2 ownership table) and runs on the control-plane
   node; a non-control-plane node may QUERY the scheduler functions but never
   persists placement files.
2. Every spec file carries a `generation`; every status file carries
   `observedGeneration`; every placement file carries a `placementGeneration`
   bumped on change. Staleness under eventual consistency is always
   detectable, never silent.
3. Level-triggered, idempotent reconcile: a decision on stale state is
   corrected next pass; an idempotent re-run with unchanged inputs produces
   no placement churn (write-on-change, R2 flood discipline).
4. Headroom comes from Node status: `allocatable` is autoscale.py output
   surfaced in node.json by sknoded. The scheduler consumes it; it never
   probes hosts itself. autoscale remains the single source of capacity math.
5. Double-execution safety does not rest on the scheduler: Jobs are guarded
   by atomic coord claims (spec 5.3, R1). Placement is routing; claim is the
   execution gate.
6. The scheduler checks the freeze flag and writes no placements while frozen
   (spec section 8, guardrail 2; `store.actuation_allowed`).
7. Scheduler v1 scope (spec section 7): filter is Ready, not cordoned,
   selector match, `NoSchedule` taints tolerated, headroom. Selection is
   least-loaded with one deterministic tiebreak (lexicographic node name).
   NO PreferNoSchedule scoring, NO affinity bonuses (Card 2.1b, gated).
   A `PreferNoSchedule` taint is advisory only: recorded in the placement
   reason, not acted on.
8. Dash ban: NEVER use em dashes or en dashes anywhere (code, docstrings,
   comments, docs, commit messages). Regular hyphens are fine.
9. New skcapstone-side code lives in `skcapstone/fleet/`; on disk that is
   `src/skcapstone/fleet/`, tests under `tests/fleet/`. Card 2.2 code lives
   in the skharness repo under `src/skharness/autocode/` (the JobController
   is the existing autopilot harness there; spec section 10 says its dispatch
   node-selection is the ONE true replacement in this epic).
10. Repo conventions: type hints everywhere, Google-style docstrings, black
    formatting, commits end with the standard `Co-Authored-By` trailer.

Card mapping: Tasks 1-4 are Card 2.1, Tasks 5-6 are Card 2.2 (skharness),
Task 7 is Card 2.3. Card 2.1b is a listed stub only (see the note after
Task 4).

Shared fixture addition, made in Task 1 (appended to the existing
`tests/fleet/conftest.py`, which already provides `paths`, `operator`,
`noded41`):

```python
@pytest.fixture
def scheduler_writer():
    """The scheduler seat (placement owner, runs on the control-plane node)."""
    from skcapstone.fleet.store import Writer

    return Writer(role="scheduler", node="node-158", identity="")
```

---

## Task 1: Placement store primitives + scheduler event seat (store.py, events.py)

Card: 2.1. The store grows the placement write path that the scheduler (and
nothing else) uses. Checked against the merged `store.py`: no placement
writer exists today; `merged()` already reads `paths.placement_path(kind,
name)` via `_load`, so records written here appear in `skfleet describe`
with zero extra work. Also: the merged `events.emit` guard is
`if writer.role not in {"sknoded", "controller"}`, which would block the
scheduler from logging decisions (needed by Card 2.3); this task adds the
`scheduler` role to that set. Spec 3.5 explicitly allows control-plane
processes on .158 to append through the store library under the local flock.

Files:
- Modify `src/skcapstone/fleet/store.py` (append three functions)
- Modify `src/skcapstone/fleet/events.py` (one line: allowed roles)
- Modify `tests/fleet/conftest.py` (append the `scheduler_writer` fixture)
- Create `tests/fleet/test_store_placement.py`

Interfaces (produced):

```python
def write_placement(paths: FleetPaths, kind: str, name: str, *,
                    node: str, reason: str, writer: Writer) -> tuple[dict, bool]
def read_placement(paths: FleetPaths, kind: str, name: str) -> dict | None
def list_placements(paths: FleetPaths, kind: str | None = None) -> list[dict]
```

Consumes (real Phase 1 code): `FleetPaths.placement_path`, `valid_name`,
`Writer`, `_load`, `_dump`, `_writer_block`, `_now_iso` (all already in
`store.py`).

Steps:

1. Append the fixture above to `tests/fleet/conftest.py`, then write the
   failing test, `tests/fleet/test_store_placement.py`:

```python
"""Tests for placement writes: scheduler ownership, generation, write-on-change."""
from __future__ import annotations

import pytest

from skcapstone.fleet import events, store


def test_write_placement_bumps_generation_on_change(paths, scheduler_writer) -> None:
    first, changed = store.write_placement(paths, "job", "card-1", node="node-158",
                                           reason="least-loaded", writer=scheduler_writer)
    assert changed is True
    assert first["placementGeneration"] == 1
    assert first["node"] == "node-158"
    assert first["kind"] == "Job"
    moved, changed = store.write_placement(paths, "job", "card-1", node="node-41",
                                           reason="least-loaded", writer=scheduler_writer)
    assert changed is True and moved["placementGeneration"] == 2
    assert store.read_placement(paths, "job", "card-1")["node"] == "node-41"


def test_write_placement_idempotent(paths, scheduler_writer) -> None:
    store.write_placement(paths, "job", "card-1", node="node-158",
                          reason="r", writer=scheduler_writer)
    again, changed = store.write_placement(paths, "job", "card-1", node="node-158",
                                           reason="r", writer=scheduler_writer)
    assert changed is False
    assert again["placementGeneration"] == 1          # unchanged input: zero churn


def test_only_scheduler_writes_placements(paths, operator, noded41) -> None:
    for writer in (operator, noded41):
        with pytest.raises(store.OwnershipError):
            store.write_placement(paths, "job", "card-1", node="node-158",
                                  reason="r", writer=writer)


def test_bad_names_rejected(paths, scheduler_writer) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_placement(paths, "job", "../evil", node="node-158",
                              reason="r", writer=scheduler_writer)


def test_list_placements_sorted_and_filtered(paths, scheduler_writer) -> None:
    assert store.list_placements(paths) == []
    store.write_placement(paths, "job", "card-b", node="node-158", reason="r",
                          writer=scheduler_writer)
    store.write_placement(paths, "job", "card-a", node="node-41", reason="r",
                          writer=scheduler_writer)
    store.write_placement(paths, "service", "skgateway", node="node-158", reason="r",
                          writer=scheduler_writer)
    assert [(p["kind"], p["name"]) for p in store.list_placements(paths)] == [
        ("Job", "card-a"), ("Job", "card-b"), ("Service", "skgateway")]
    assert [p["name"] for p in store.list_placements(paths, "job")] == [
        "card-a", "card-b"]


def test_merged_includes_placement(paths, operator, scheduler_writer) -> None:
    store.write_spec(paths, "service", "skgateway", {"unit": "u"}, writer=operator)
    store.write_placement(paths, "service", "skgateway", node="node-158",
                          reason="r", writer=scheduler_writer)
    assert store.merged(paths, "service", "skgateway")["placement"]["node"] == "node-158"


def test_scheduler_may_emit_events(paths, scheduler_writer, operator) -> None:
    events.reset_dedupe()
    assert events.emit(paths, scheduler_writer, kind="job", name="card-1",
                       type="Placement", reason="Placed", message="m",
                       now=1000.0) is True
    with pytest.raises(store.OwnershipError):
        events.emit(paths, operator, kind="job", name="card-1",
                    type="Placement", reason="Placed", message="m", now=1001.0)
    events.reset_dedupe()
```

2. Run to fail:
   `~/.skenv/bin/python -m pytest tests/fleet/test_store_placement.py -v`
   Expected: `AttributeError: module 'skcapstone.fleet.store' has no attribute 'write_placement'`

3. Implement. In `src/skcapstone/fleet/events.py` change the guard line in
   `emit` from

```python
    if writer.role not in {"sknoded", "controller"}:
```

   to

```python
    if writer.role not in {"sknoded", "controller", "scheduler"}:
```

   Append to `src/skcapstone/fleet/store.py`:

```python
def write_placement(
    paths: FleetPaths,
    kind: str,
    name: str,
    *,
    node: str,
    reason: str,
    writer: Writer,
) -> tuple[dict, bool]:
    """Write the scheduler's decision for one object (write-on-change).

    Only the scheduler role may write placements (spec 3.2 ownership table);
    it never writes status and never touches spec. placementGeneration bumps
    only when the decision content (node or reason) changed, so an idempotent
    re-run produces zero churn (R2).

    Returns:
        (payload as on disk, True when a write happened).
    Raises:
        OwnershipError: wrong role, or unsafe kind/name.
    """
    if writer.role != "scheduler":
        raise OwnershipError(f"role {writer.role!r} may not write placements")
    if not (valid_name(kind) and valid_name(name)):
        raise OwnershipError(f"invalid kind/name: {kind!r}/{name!r}")
    path = paths.placement_path(kind, name)
    existing = _load(path)
    if (existing is not None and existing.get("node") == node
            and existing.get("reason") == reason):
        return existing, False
    payload = {
        "kind": kind.capitalize(),
        "name": name,
        "node": node,
        "reason": reason,
        "placementGeneration": int((existing or {}).get("placementGeneration", 0)) + 1,
        "writer": _writer_block(writer),
        "updatedAt": _now_iso(),
    }
    _dump(path, payload)
    return payload, True


def read_placement(paths: FleetPaths, kind: str, name: str) -> dict | None:
    """Read one placement record, or None when absent."""
    return _load(paths.placement_path(kind, name))


def list_placements(paths: FleetPaths, kind: str | None = None) -> list[dict]:
    """All placement records, sorted by (kind, name). Zero objects cost nothing."""
    if not paths.placements.exists():
        return []
    kinds = ([kind] if kind is not None
             else sorted(p.name for p in paths.placements.iterdir() if p.is_dir()))
    out: list[dict] = []
    for k in kinds:
        kind_dir = paths.placements / k
        if not kind_dir.exists():
            continue
        for p in sorted(kind_dir.glob("*.json")):
            payload = _load(p)
            if payload is not None:
                out.append(payload)
    return out
```

4. Run to pass, then the whole fleet suite (`tests/fleet/`) to confirm no
   regression (the events role change must not break `test_emit_ownership`,
   which uses the operator role and still raises).
5. Commit: `feat(fleet): placement store (scheduler-only writes) + scheduler event seat`

---

## Task 2: Allocatable headroom in node reports and NodeView

Card: 2.1 (prerequisite). Spec 5.1 puts both `capacity` and `allocatable`
(capacity minus reserves) in Node status; the merged Phase 1 `sknoded.
build_node_report` ships only `capacity`. This task adds `allocatable`
(computed from the same autoscale-backed `node_capacity()` snapshot, keeping
autoscale the single source of capacity math) and surfaces it on `NodeView`
with a capacity fallback so a mixed-version fleet (a node still writing
pre-Phase-2 node.json) stays schedulable during rollout.

Files:
- Modify `src/skcapstone/fleet/capacity.py` (append reserves + one function)
- Modify `src/skcapstone/fleet/sknoded.py` (one import, one status key)
- Modify `src/skcapstone/fleet/node_controller.py` (one NodeView field, one
  populate line)
- Create `tests/fleet/test_allocatable.py`

Interfaces (produced):

```python
# capacity.py
RESERVE_CORES: int = 1
RESERVE_RAM_GB: float = 1.0
RESERVE_DISK_GB: float = 5.0
def allocatable(capacity: dict) -> dict   # {"cores", "ram_gb", "disk_gb"}

# node_controller.py (NodeView gains one field, default preserved)
@dataclass
class NodeView:
    ...
    allocatable: dict = field(default_factory=dict)
```

Consumes (real code): `capacity.node_capacity()` returns
`{"cores", "ram_gb", "disk_gb", "gpu", "vram_gb"}`; `sknoded.
build_node_report` builds `status={"capacity": cap, "versions": {...}}`;
`node_views` populates NodeView rows from `report.get("status", {})`.

Steps:

1. Write the failing test, `tests/fleet/test_allocatable.py`:

```python
"""Tests for allocatable headroom (capacity minus reserves, spec 5.1)."""
from __future__ import annotations

from skcapstone.fleet import capacity, node_controller, sknoded, store

CAP = {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None}


def test_allocatable_subtracts_reserves() -> None:
    assert capacity.allocatable(CAP) == {"cores": 7, "ram_gb": 15.0, "disk_gb": 95.0}


def test_allocatable_floors() -> None:
    tiny = {"cores": 1, "ram_gb": 0.5, "disk_gb": 2.0, "gpu": None, "vram_gb": None}
    assert capacity.allocatable(tiny) == {"cores": 1, "ram_gb": 0.0, "disk_gb": 0.0}


def test_node_report_carries_allocatable(paths, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    sknoded.run_once(paths, "node-41")
    report = store.read_node_file(paths, "node-41", "node.json")
    assert report["status"]["allocatable"] == {"cores": 7, "ram_gb": 15.0,
                                               "disk_gb": 95.0}


def test_node_view_allocatable_with_capacity_fallback(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))
    sknoded.run_once(paths, "node-41")
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    view = {v.name: v for v in node_controller.node_views(paths)}["node-41"]
    assert view.allocatable == {"cores": 7, "ram_gb": 15.0, "disk_gb": 95.0}
    # a pre-Phase-2 node.json (no allocatable key) falls back to capacity
    noded_old = store.Writer(role="sknoded", node="node-old", identity="")
    store.write_node_file(paths, noded_old, "node.json",
                          {"kind": "Node", "name": "node-old", "node": "node-old",
                           "observedGeneration": 1, "conditions": [],
                           "status": {"capacity": {"cores": 2, "ram_gb": 4.0,
                                                   "disk_gb": 10.0}}})
    store.write_spec(paths, "node", "node-old", {}, writer=operator)
    view = {v.name: v for v in node_controller.node_views(paths)}["node-old"]
    assert view.allocatable == {"cores": 2, "ram_gb": 4.0, "disk_gb": 10.0}
```

2. Run to fail. Expected:
   `AttributeError: module 'skcapstone.fleet.capacity' has no attribute 'allocatable'`

3. Implement. Append to `src/skcapstone/fleet/capacity.py`:

```python
RESERVE_CORES = 1
RESERVE_RAM_GB = 1.0
RESERVE_DISK_GB = 5.0


def allocatable(capacity: dict) -> dict:
    """Schedulable headroom: capacity minus fixed host reserves (spec 5.1).

    Mirrors the autoscale discipline (leave the host a core and some RAM)
    so the fleet scheduler and the local worker pool agree on what is spare.
    """
    return {
        "cores": max(1, int(capacity.get("cores", 1)) - RESERVE_CORES),
        "ram_gb": round(max(0.0, float(capacity.get("ram_gb", 0.0)) - RESERVE_RAM_GB), 1),
        "disk_gb": round(max(0.0, float(capacity.get("disk_gb", 0.0)) - RESERVE_DISK_GB), 1),
    }
```

   In `src/skcapstone/fleet/sknoded.py` change the import line

```python
from .capacity import node_capacity
```

   to

```python
from .capacity import allocatable, node_capacity
```

   and in `build_node_report` change the status block to

```python
        "status": {
            "capacity": cap,
            "allocatable": allocatable(cap),
            "versions": {
                "python": platform.python_version(),
                "skcapstone": skcapstone_version,
            },
        },
```

   In `src/skcapstone/fleet/node_controller.py` add to `NodeView`, directly
   after the `capacity` field:

```python
    allocatable: dict = field(default_factory=dict)
```

   and in `node_views`, where the row is built, add after the
   `capacity=...` argument:

```python
                allocatable=(report.get("status", {}).get("allocatable")
                             or report.get("status", {}).get("capacity", {})),
```

4. Run to pass, then the whole `tests/fleet/` suite (the Phase 1
   `test_sknoded.py` write-on-change assertions still hold: the new key is
   deterministic from the same fixed capacity, so a repeat pass is still a
   no-op; `test_single_node.py` still passes because no new files appear).
5. Commit: `feat(fleet): allocatable headroom in node report + NodeView (capacity fallback)`

---

## Task 3: Scheduler filter (scheduler.py part 1)

Card: 2.1. The filter half of spec section 7 step 1: Ready (per
NodeController phase), not cordoned, `nodeSelector` exact-match label AND
(same semantics as autopilot `--tag`), all `NoSchedule` taints tolerated,
allocatable headroom for the requested resources. `PreferNoSchedule` passes
the filter (advisory in v1).

Files:
- Create `src/skcapstone/fleet/scheduler.py`
- Create `tests/fleet/test_scheduler_filter.py`

Interfaces (produced):

```python
DEFAULT_REQUESTS: dict = {"cores": 1, "ram_gb": 2.0}
@dataclass(frozen=True)
class Workload:
    kind: str
    name: str
    node_selector: dict = field(default_factory=dict)
    tolerations: tuple = ()          # of {"key": str, "value": str (optional)}
    requests: dict = field(default_factory=lambda: dict(DEFAULT_REQUESTS))
def feasible(view: NodeView, workload: Workload) -> str | None
# None = node passes every filter; else the exclusion reason (pinned strings)
```

Consumes (real code): `node_controller.NodeView` (fields `name`, `phase`,
`cordoned`, `labels`, `taints`, `allocatable` from Task 2).

Steps:

1. Write the failing test, `tests/fleet/test_scheduler_filter.py`:

```python
"""Tests for scheduler v1 filtering (Ready, cordon, selector, taints, headroom)."""
from __future__ import annotations

from skcapstone.fleet.node_controller import NodeView
from skcapstone.fleet.scheduler import Workload, feasible

ALLOC = {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0}


def _view(name="node-a", phase="Ready", cordoned=False, labels=None,
          taints=None, alloc=None) -> NodeView:
    return NodeView(name=name, phase=phase, cordoned=cordoned,
                    labels=labels or {}, taints=taints or [],
                    capacity=dict(ALLOC), allocatable=alloc or dict(ALLOC))


def _wl(**kw) -> Workload:
    return Workload(kind="job", name="card-1", **kw)


def test_ready_and_cordon_filters() -> None:
    assert feasible(_view(), _wl()) is None
    assert feasible(_view(phase="NotReady"), _wl()) == "not Ready (phase=NotReady)"
    assert feasible(_view(phase="Dead"), _wl()) == "not Ready (phase=Dead)"
    assert feasible(_view(phase="Pending"), _wl()) == "not Ready (phase=Pending)"
    assert feasible(_view(cordoned=True), _wl()) == "cordoned"


def test_node_selector_exact_match_and() -> None:
    view = _view(labels={"heavy-build": "true", "tier": "core"})
    assert feasible(view, _wl(node_selector={"heavy-build": "true"})) is None
    assert feasible(view, _wl(node_selector={"heavy-build": "true",
                                             "tier": "core"})) is None
    assert feasible(view, _wl(node_selector={"gpu": "true"})) == \
        "selector mismatch (gpu=true)"
    assert feasible(view, _wl(node_selector={"tier": "edge"})) == \
        "selector mismatch (tier=edge)"


def test_noschedule_taints_require_toleration() -> None:
    tainted = _view(taints=[{"key": "dedicated", "value": "model-serving",
                             "effect": "NoSchedule"}])
    assert feasible(tainted, _wl()) == \
        "untolerated NoSchedule taint dedicated=model-serving"
    exact = _wl(tolerations=({"key": "dedicated", "value": "model-serving"},))
    assert feasible(tainted, exact) is None
    key_only = _wl(tolerations=({"key": "dedicated"},))
    assert feasible(tainted, key_only) is None   # key-only tolerates any value
    wrong = _wl(tolerations=({"key": "dedicated", "value": "other"},))
    assert feasible(tainted, wrong) == \
        "untolerated NoSchedule taint dedicated=model-serving"


def test_prefernoschedule_is_advisory_in_v1() -> None:
    travel = _view(taints=[{"key": "travel", "value": "true",
                            "effect": "PreferNoSchedule"}])
    assert feasible(travel, _wl()) is None       # correctness filters only (spec 7)


def test_headroom_filter() -> None:
    small = _view(alloc={"cores": 1, "ram_gb": 1.0, "disk_gb": 50.0})
    assert feasible(small, _wl()) == \
        "insufficient headroom (need cores>=1, ram_gb>=2.0)"
    assert feasible(small, _wl(requests={"cores": 1, "ram_gb": 0.5})) is None
```

2. Run to fail. Expected:
   `ModuleNotFoundError: No module named 'skcapstone.fleet.scheduler'`

3. Implement `src/skcapstone/fleet/scheduler.py`:

```python
"""Scheduler v1: filter + least-loaded placement (spec section 7).

Stateless and idempotent: every pass recomputes from Node views and a
workload's requirements, and placement writes are write-on-change. No
preference or affinity scoring in v1 (deferred, gated Card 2.1b); a
PreferNoSchedule taint is advisory only, recorded in the placement reason.
The scheduler writes ONLY placements: never status, never spec.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import events, store
from .node_controller import NodeView, node_views
from .paths import FleetPaths

DEFAULT_REQUESTS: dict = {"cores": 1, "ram_gb": 2.0}


@dataclass(frozen=True)
class Workload:
    """What the scheduler needs to know about one schedulable unit.

    Attributes:
        kind: Fleet kind ("job" for autopilot cards, "service" later).
        name: Object name (a card id for jobs).
        node_selector: Exact-match label AND map (autopilot --tag semantics).
        tolerations: Tuple of {"key", optional "value"}; key-only tolerates
            any value of that taint key.
        requests: Requested resources checked against Node allocatable.
    """

    kind: str
    name: str
    node_selector: dict = field(default_factory=dict)
    tolerations: tuple = ()
    requests: dict = field(default_factory=lambda: dict(DEFAULT_REQUESTS))


def _tolerated(taint: dict, tolerations: tuple) -> bool:
    for tol in tolerations:
        if tol.get("key") != taint.get("key"):
            continue
        if "value" not in tol or tol.get("value") == taint.get("value"):
            return True
    return False


def feasible(view: NodeView, workload: Workload) -> str | None:
    """None when the node passes every v1 filter, else the exclusion reason.

    Filters (spec section 7 step 1): Ready phase, not cordoned, selector
    match, all NoSchedule taints tolerated, allocatable headroom.
    """
    if view.phase != "Ready":
        return f"not Ready (phase={view.phase})"
    if view.cordoned:
        return "cordoned"
    for key, value in sorted(workload.node_selector.items()):
        if view.labels.get(key) != value:
            return f"selector mismatch ({key}={value})"
    for taint in view.taints:
        if (taint.get("effect") == "NoSchedule"
                and not _tolerated(taint, workload.tolerations)):
            return (f"untolerated NoSchedule taint "
                    f"{taint.get('key')}={taint.get('value')}")
    need_cores = float(workload.requests.get("cores", 0))
    need_ram = float(workload.requests.get("ram_gb", 0.0))
    if (float(view.allocatable.get("cores", 0)) < need_cores
            or float(view.allocatable.get("ram_gb", 0.0)) < need_ram):
        return f"insufficient headroom (need cores>={need_cores:g}, ram_gb>={need_ram})"
    return None
```

4. Run to pass.
5. Commit: `feat(fleet): scheduler v1 filter (ready/cordon/selector/taints/headroom)`

---

## Task 4: Scheduler select + place with the pinned decision table (scheduler.py part 2)

Card: 2.1. The selection half: least-loaded survivor = most allocatable
headroom (RAM first, then cores; RAM is the binding resource on this fleet
and the same axis autoscale's recommended() keys on), single deterministic
tiebreak by lexicographic node name. `place()` is the write path: it honors
freeze, writes through `store.write_placement` (write-on-change), and emits
one Placement event per CHANGED decision (the Card 2.3 audit trail). The
table-driven test below is the pinned decision table the Card 2.3 acceptance
refers back to.

Files:
- Modify `src/skcapstone/fleet/scheduler.py` (append)
- Create `tests/fleet/test_scheduler_place.py`

Interfaces (produced):

```python
@dataclass(frozen=True)
class Decision:
    node: str | None
    reason: str
    excluded: dict = field(default_factory=dict)   # node name -> filter reason
def select(views: list[NodeView], workload: Workload) -> Decision
def place(paths: FleetPaths, workload: Workload, *, writer: Writer,
          views: list[NodeView] | None = None) -> dict | None
```

Consumes: Task 1 (`store.write_placement`, `store.read_placement`,
`events.emit` with the scheduler role), Task 3 (`feasible`, `Workload`),
real Phase 1 `store.actuation_allowed(paths)` and
`node_controller.node_views(paths)`.

Steps:

1. Write the failing test, `tests/fleet/test_scheduler_place.py`:

```python
"""Table-driven placement decisions (Card 2.1 acceptance, pinned table)."""
from __future__ import annotations

import pytest

from skcapstone.fleet import events, scheduler, store
from skcapstone.fleet.node_controller import NodeView


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _views() -> list[NodeView]:
    return [
        NodeView(name="node-158", phase="Ready",
                 labels={"always-on": "true", "control-plane": "true"},
                 allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase="Ready", labels={"heavy-build": "true"},
                 allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0}),
        NodeView(name="node-100", phase="Ready", labels={"gpu": "true"},
                 taints=[{"key": "dedicated", "value": "model-serving",
                          "effect": "NoSchedule"}],
                 allocatable={"cores": 11, "ram_gb": 20.0, "disk_gb": 300.0}),
        NodeView(name="node-local", phase="Ready", labels={"interactive": "true"},
                 taints=[{"key": "interactive", "value": "true",
                          "effect": "PreferNoSchedule"}],
                 allocatable={"cores": 3, "ram_gb": 6.0, "disk_gb": 40.0}),
    ]


# The PINNED table (Card 2.3 acceptance: shown reasons must match this).
TABLE = [
    ({}, "node-41", "least-loaded: node-41"),
    ({"node_selector": {"gpu": "true"},
      "tolerations": ({"key": "dedicated"},)}, "node-100",
     "least-loaded: node-100"),
    ({"node_selector": {"heavy-build": "true"}}, "node-41",
     "least-loaded: node-41"),
    ({"node_selector": {"gpu": "true"}}, None, "unschedulable"),
]


@pytest.mark.parametrize("kw,expected,fragment", TABLE)
def test_decision_table(kw, expected, fragment) -> None:
    decision = scheduler.select(_views(), scheduler.Workload(kind="job", name="c", **kw))
    assert decision.node == expected
    assert fragment in decision.reason


def test_least_loaded_and_deterministic_tiebreak() -> None:
    a = NodeView(name="node-b", phase="Ready",
                 allocatable={"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0})
    b = NodeView(name="node-a", phase="Ready",
                 allocatable={"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0})
    wl = scheduler.Workload(kind="job", name="c")
    assert scheduler.select([a, b], wl).node == "node-a"   # lexicographic tiebreak
    assert scheduler.select([b, a], wl).node == "node-a"   # input order irrelevant
    bigger = NodeView(name="node-z", phase="Ready",
                      allocatable={"cores": 4, "ram_gb": 9.0, "disk_gb": 50.0})
    assert scheduler.select([a, b, bigger], wl).node == "node-z"


def test_cordon_excludes_with_recorded_reason() -> None:
    views = _views()
    views[1] = NodeView(name="node-41", phase="Ready", cordoned=True,
                        labels={"heavy-build": "true"},
                        allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0})
    decision = scheduler.select(views, scheduler.Workload(kind="job", name="c"))
    assert decision.node == "node-158"          # next-most headroom survivor
    assert decision.excluded["node-41"] == "cordoned"


def test_advisory_prefer_noschedule_recorded_in_reason() -> None:
    views = [NodeView(name="node-local", phase="Ready",
                      taints=[{"key": "interactive", "value": "true",
                               "effect": "PreferNoSchedule"}],
                      allocatable={"cores": 3, "ram_gb": 6.0, "disk_gb": 40.0})]
    decision = scheduler.select(views, scheduler.Workload(kind="job", name="c"))
    assert decision.node == "node-local"
    assert "advisory: PreferNoSchedule taint interactive=true" in decision.reason


def test_place_writes_once_and_honors_freeze(paths, operator) -> None:
    sched = store.Writer(role="scheduler", node="node-158", identity="")
    wl = scheduler.Workload(kind="job", name="card-1")
    placement = scheduler.place(paths, wl, writer=sched, views=_views())
    assert placement["node"] == "node-41"
    assert placement["placementGeneration"] == 1
    again = scheduler.place(paths, wl, writer=sched, views=_views())
    assert again["placementGeneration"] == 1    # idempotent re-run: no churn
    store.set_frozen(paths, True, writer=operator, reason="drill")
    assert scheduler.place(paths, scheduler.Workload(kind="job", name="card-2"),
                           writer=sched, views=_views()) is None
    assert store.read_placement(paths, "job", "card-2") is None   # frozen: no writes


def test_place_unschedulable_writes_nothing(paths) -> None:
    sched = store.Writer(role="scheduler", node="node-158", identity="")
    wl = scheduler.Workload(kind="job", name="card-3",
                            node_selector={"nonexistent": "true"})
    assert scheduler.place(paths, wl, writer=sched, views=_views()) is None
    assert store.read_placement(paths, "job", "card-3") is None
```

2. Run to fail. Expected:
   `AttributeError: module 'skcapstone.fleet.scheduler' has no attribute 'select'`

3. Implement (append to `src/skcapstone/fleet/scheduler.py`):

```python
@dataclass(frozen=True)
class Decision:
    """One scheduling decision with its full audit trail.

    Attributes:
        node: Chosen node name, or None when unschedulable.
        reason: One-line human reason (surfaced by skfleet placements).
        excluded: Per-node filter reason for every excluded candidate.
    """

    node: str | None
    reason: str
    excluded: dict = field(default_factory=dict)


def _headroom_key(view: NodeView) -> tuple:
    """Sort key: most allocatable RAM, then cores, then lexicographic name."""
    return (-float(view.allocatable.get("ram_gb", 0.0)),
            -float(view.allocatable.get("cores", 0)),
            view.name)


def select(views: list[NodeView], workload: Workload) -> Decision:
    """Filter (Task 3) then pick the least-loaded survivor (spec 7 step 2).

    v1 has no preference scoring: a PreferNoSchedule taint on the chosen
    node is recorded in the reason, never acted on.
    """
    excluded: dict = {}
    candidates: list[NodeView] = []
    for view in views:
        why = feasible(view, workload)
        if why is None:
            candidates.append(view)
        else:
            excluded[view.name] = why
    if not candidates:
        detail = "; ".join(f"{n}: {w}" for n, w in sorted(excluded.items()))
        return Decision(node=None, reason=f"unschedulable ({detail})",
                        excluded=excluded)
    chosen = sorted(candidates, key=_headroom_key)[0]
    reason = (f"least-loaded: {chosen.name} allocatable "
              f"ram={chosen.allocatable.get('ram_gb')}GB "
              f"cores={chosen.allocatable.get('cores')} "
              f"of {len(candidates)} candidate(s)")
    for taint in chosen.taints:
        if taint.get("effect") == "PreferNoSchedule":
            reason += (f"; advisory: PreferNoSchedule taint "
                       f"{taint.get('key')}={taint.get('value')} not scored in v1")
    return Decision(node=chosen.name, reason=reason, excluded=excluded)


def place(
    paths: FleetPaths,
    workload: Workload,
    *,
    writer: store.Writer,
    views: list[NodeView] | None = None,
) -> dict | None:
    """Decide and record one placement (level-triggered, idempotent).

    Honors the freeze flag: a frozen tree gets no placement writes (spec
    section 8, guardrail 2). Emits one Placement event per CHANGED decision
    (the Card 2.3 audit trail; unchanged re-runs stay silent, R2).

    Returns:
        The placement payload as on disk, or None when frozen or
        unschedulable (nothing is written in either case).
    """
    if not store.actuation_allowed(paths):
        return None
    views = node_views(paths) if views is None else views
    decision = select(views, workload)
    if decision.node is None:
        return None
    payload, changed = store.write_placement(
        paths, workload.kind, workload.name,
        node=decision.node, reason=decision.reason, writer=writer)
    if changed:
        events.emit(paths, writer, kind=workload.kind, name=workload.name,
                    type="Placement", reason="Placed", message=decision.reason)
    return payload
```

4. Run to pass, then the whole `tests/fleet/` suite.
5. Commit: `feat(fleet): scheduler v1 select + place (least-loaded, freeze-safe, pinned table)`

### Card 2.1b (DEFERRED, v1.1): preference + affinity scoring. STUB ONLY.

Not a task in this plan. When the gate opens (a carded incident or a measured
bad placement demonstrating placement contention, per spec 3.6 right-sized
complexity), a v1.1 card adds a `score(candidates, workload) -> list` step
between filter and tiebreak inside `select`: `PreferNoSchedule` ordering
(travel-tainted node-41 avoided but usable, interactive-tainted node-local a
last resort) and label-affinity bonuses. The seam already exists: `select`
is the only function that ranks, `feasible` stays untouched, and the pinned
decision table gains rows rather than changing existing ones. Do not build
any of this now.

---

## Task 5: Fleet dispatch seam in skharness (fleet_dispatch.py)

Card: 2.2. CROSS-REPO: this task and Task 6 live in
`/home/cbrd21/clawd/skcapstone-repos/skharness/`.

What was actually found in the merged skharness code (read, not assumed):
there is NO literal ".158 + .41" node list in the engine. The "static
dual-node dispatch" is a convention living outside the code path: the
`autopilot-daily` scheduler job is pinned to one node
(`nodes: [noroc2027]` in `~/.skcapstone/config/jobs.yaml`, comment:
"single-node dispatch (no cross-node double-exec)"), and cross-node
distribution is done by hand-assigned per-node tags via the `--tag` scope
(`skharness/src/skharness/autocode/orchestrator.py`, `phase0_assess`
docstring: "``only_tag`` -- only unblocked tasks carrying this tag (--tag,
e.g. a per-node assignment tag or ``autopilot``)"). The real integration
point is therefore `run_once` in `orchestrator.py`: after `phase1_triage`
produces `selected` and before `phase2_swarm` runs it, a placement gate
partitions `selected` into cards placed HERE versus elsewhere. That replaces
the convention with a scheduler query, which is exactly the spec's "the
autopilot dispatcher becomes the first scheduler client".

This task builds the seam module (pure, injectable, testable without a live
fleet); Task 6 wires it into `run_once` and hardens the claim gate.

Design points:
- Soft dependency: skcapstone is an OPTIONAL sibling of skharness (per
  `pyproject.toml` markers and `tests/conftest.py` auto-skip). All
  `skcapstone.fleet` imports are lazy inside functions; when the import
  fails or the fleet tree has no admitted nodes, the gate is inert and
  everything runs locally (current behavior, and the spec 3.6 one-box
  invariant).
- Single-writer discipline: `select` is pure and every node computes the
  same answer from the same synced views; only a run on the control-plane
  node (label `control-plane=true` on its own NodeView) PERSISTS placement
  records via `scheduler.place`. Any other node queries only.
- Card requirements: a card opts into a nodeSelector with tags
  `node:<key>[=<value>]` (bare `node:heavy-build` means
  `heavy-build=true`), mirroring the existing `repo:<name>` /
  `quality:<mode>` tag vocabulary. Jobs carry no tolerations in v1, so the
  NoSchedule-tainted node-100 never receives builds. Requests default to
  `scheduler.DEFAULT_REQUESTS`.
- Freeze: a frozen fleet places nothing new, so every card is skipped with
  reason "fleet frozen"; running builds are never touched (freeze semantics,
  spec section 8).

Files:
- Create `src/skharness/autocode/fleet_dispatch.py`
- Create `tests/test_autopilot_fleet_dispatch.py`

Interfaces (produced):

```python
NODE_TAG_PREFIX: str = "node:"
@dataclass(frozen=True)
class DispatchDecision:
    ref: str
    node: str | None
    reason: str
def card_selector(tags: list[str]) -> dict
def self_node() -> str
def claim_agent_name() -> str
def default_placer() -> Callable[[WorkItem], DispatchDecision] | None
def partition_local(selected, *, placer, self_node) -> tuple[list, list[tuple]]
# selected is the run's list of (WorkItem, executor) pairs;
# returns (kept pairs, skipped [(WorkItem, DispatchDecision), ...])
```

Consumes (real code): `skharness.autocode.types.WorkItem(kind, ref, source,
repo, payload)`; skcapstone side (lazy): `scheduler.Workload`,
`scheduler.select`, `scheduler.place`, `node_controller.node_views`,
`paths.default_paths`, `paths.self_node_name`, `store.Writer`,
`store.writer_identity`, `store.is_frozen`.

Steps:

1. Write the failing test, `tests/test_autopilot_fleet_dispatch.py`:

```python
"""Fleet-aware dispatch seam: card selectors, partition, unmanaged fallback."""
import pytest

from skharness.autocode import fleet_dispatch as fd
from skharness.autocode.types import WorkItem


def _item(ref, tags=None):
    return WorkItem(kind="engineering", ref=ref, source="coord", repo="skos",
                    payload={"tags": tags or []})


def test_card_selector_from_node_tags():
    assert fd.card_selector([]) == {}
    assert fd.card_selector(["repo:skos", "node:heavy-build"]) == {"heavy-build": "true"}
    assert fd.card_selector(["node:tier=core", "node:gpu"]) == {"tier": "core",
                                                                "gpu": "true"}


def test_partition_local_none_placer_keeps_everything():
    selected = [(_item("t-1"), object()), (_item("t-2"), object())]
    kept, skipped = fd.partition_local(selected, placer=None, self_node="node-158")
    assert kept == selected and skipped == []


def test_partition_local_splits_by_placement():
    selected = [(_item("t-1"), object()), (_item("t-2", ["node:heavy-build"]), object())]

    def placer(item):
        node = "node-41" if "node:heavy-build" in item.payload["tags"] else "node-158"
        return fd.DispatchDecision(ref=item.ref, node=node, reason="least-loaded")

    kept, skipped = fd.partition_local(selected, placer=placer, self_node="node-158")
    assert [it.ref for it, _ in kept] == ["t-1"]
    assert [(it.ref, d.node) for it, d in skipped] == [("t-2", "node-41")]


def test_partition_local_unschedulable_is_skipped_with_reason():
    selected = [(_item("t-1"), object())]

    def placer(item):
        return fd.DispatchDecision(ref=item.ref, node=None,
                                   reason="unschedulable (all filtered)")

    kept, skipped = fd.partition_local(selected, placer=placer, self_node="node-158")
    assert kept == [] and skipped[0][1].reason.startswith("unschedulable")


@pytest.mark.needs_skcapstone
def test_default_placer_unmanaged_tree_returns_none(monkeypatch, tmp_path):
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    assert fd.default_placer() is None          # no admitted nodes: run local (3.6)


@pytest.mark.needs_skcapstone
def test_default_placer_places_and_persists_on_control_plane(monkeypatch, tmp_path):
    from skcapstone.fleet import events, sknoded, store
    from skcapstone.fleet.paths import FleetPaths

    events.reset_dedupe()
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0,
                                 "gpu": None, "vram_gb": None})
    paths = FleetPaths(root=tmp_path / "fleet")
    operator = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False, "taints": []},
                     writer=operator, labels={"control-plane": "true"})
    sknoded.run_once(paths, "node-158")         # observe the admission
    placer = fd.default_placer()
    assert placer is not None
    decision = placer(_item("t-1"))
    assert decision.node == "node-158"
    assert decision.reason.startswith("least-loaded: node-158")
    # control-plane runs persist the audit record; others would only query
    assert store.read_placement(paths, "job", "t-1")["node"] == "node-158"
    events.reset_dedupe()


@pytest.mark.needs_skcapstone
def test_default_placer_frozen_skips_everything(monkeypatch, tmp_path):
    from skcapstone.fleet import sknoded, store
    from skcapstone.fleet.paths import FleetPaths

    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet"))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 8, "ram_gb": 16.0, "disk_gb": 100.0,
                                 "gpu": None, "vram_gb": None})
    paths = FleetPaths(root=tmp_path / "fleet")
    operator = store.Writer(role="operator", node="node-158", identity="")
    sknoded.run_once(paths, "node-158")
    store.write_spec(paths, "node", "node-158", {"cordoned": False}, writer=operator)
    store.set_frozen(paths, True, writer=operator, reason="drill")
    placer = fd.default_placer()
    decision = placer(_item("t-1"))
    assert decision.node is None and "frozen" in decision.reason
```

2. Run to fail (from the skharness repo root):
   `~/.skenv/bin/python -m pytest tests/test_autopilot_fleet_dispatch.py -v`
   Expected: `ModuleNotFoundError: No module named 'skharness.autocode.fleet_dispatch'`

3. Implement `src/skharness/autocode/fleet_dispatch.py`:

```python
"""Fleet-aware dispatch gate for autopilot (fleet design spec, Card 2.2).

Replaces the static single-node-by-convention dispatch (jobs.yaml node
pinning plus hand-assigned per-node tags): before the swarm phase, each
selected card is placed by the fleet scheduler v1 (filter + least-loaded)
and only cards placed on THIS node proceed. The coord claim remains the
authoritative execution gate; placement is advisory routing, so a stale
placement can never double-run a card (the claim is atomic).

Soft dependency: skcapstone is an optional sibling. When it is not
importable, or the fleet tree has no admitted nodes, the gate is inert and
everything runs locally, preserving one-box behavior (spec 3.6).

Single-writer discipline: select() is pure and every node computes the same
answer from the same synced views; only a run on the control-plane node
(label control-plane=true) PERSISTS placement records. Other nodes query.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .types import WorkItem

NODE_TAG_PREFIX = "node:"


@dataclass(frozen=True)
class DispatchDecision:
    """Where one card should run, and why."""

    ref: str
    node: str | None
    reason: str


def card_selector(tags: list[str]) -> dict:
    """Map ``node:<key>[=<value>]`` card tags to a fleet nodeSelector.

    Same exact-match AND semantics as autopilot ``--tag`` selection; a bare
    ``node:<key>`` means ``<key>=true`` (e.g. ``node:heavy-build``).
    """
    selector: dict = {}
    for tag in tags:
        if not tag.startswith(NODE_TAG_PREFIX):
            continue
        body = tag[len(NODE_TAG_PREFIX):]
        key, _, value = body.partition("=")
        if key:
            selector[key] = value or "true"
    return selector


def self_node() -> str:
    """This machine's fleet node name, or "local" without the fleet package."""
    try:
        from skcapstone.fleet.paths import self_node_name

        return self_node_name()
    except Exception:
        return "local"


def claim_agent_name() -> str:
    """Node-scoped claim identity so the coord claim gate distinguishes nodes.

    With cross-node dispatch, two nodes claiming as the same agent name
    would not conflict (claim_task allows re-claim by the same name), so the
    claimer must be per-node: autopilot-<node>. Falls back to the legacy
    "autopilot" on a box without the fleet package.
    """
    try:
        from skcapstone.fleet.paths import self_node_name

        return f"autopilot-{self_node_name()}"
    except Exception:
        return "autopilot"


def default_placer() -> Callable[[WorkItem], DispatchDecision] | None:
    """Build the live placer from the fleet tree, or None when unmanaged.

    None means "no fleet": the caller keeps every card local (the exact
    pre-Phase-2 behavior, and the spec 3.6 one-box invariant).
    """
    try:
        from skcapstone.fleet import scheduler as fsched
        from skcapstone.fleet import store
        from skcapstone.fleet.node_controller import node_views
        from skcapstone.fleet.paths import default_paths, self_node_name
    except Exception:
        return None                                  # no fleet substrate installed
    paths = default_paths()
    views = [v for v in node_views(paths) if v.phase != "Pending"]
    if not views:
        return None                                  # unmanaged tree: run local
    me = self_node_name()
    is_control_plane = any(v.name == me and v.labels.get("control-plane") == "true"
                           for v in views)
    frozen = store.is_frozen(paths)
    writer = store.Writer(role="scheduler", node=me,
                          identity=store.writer_identity())

    def _place(item: WorkItem) -> DispatchDecision:
        if frozen:
            return DispatchDecision(ref=item.ref, node=None,
                                    reason="fleet frozen: no new placements")
        workload = fsched.Workload(
            kind="job", name=item.ref,
            node_selector=card_selector(item.payload.get("tags") or []))
        decision = fsched.select(views, workload)
        if decision.node is not None and is_control_plane:
            fsched.place(paths, workload, writer=writer, views=views)  # audit record
        return DispatchDecision(ref=item.ref, node=decision.node,
                                reason=decision.reason)

    return _place


def partition_local(selected, *, placer, self_node: str) -> tuple[list, list[tuple]]:
    """Split (item, executor) pairs into (run here, skipped elsewhere).

    A None placer keeps everything (gate inert). Skipped entries carry the
    full DispatchDecision so the run journal records where and why.
    """
    if placer is None:
        return list(selected), []
    kept: list = []
    skipped: list[tuple] = []
    for item, ex in selected:
        decision = placer(item)
        if decision.node == self_node:
            kept.append((item, ex))
        else:
            skipped.append((item, decision))
    return kept, skipped
```

4. Run to pass.
5. Commit: `feat(autocode): fleet dispatch seam (scheduler query, node tags, soft dep)`

---

## Task 6: Wire the gate into run_once + node-scoped claim + claim-race guard

Card: 2.2, still in the skharness repo. Two changes, one test cycle:

(a) `run_once` gains the placement gate: live runs partition `selected`
through `fleet_dispatch.partition_local` right before `phase2_swarm`;
off-node cards are journaled as `off-node` and returned in the report. A
new `fleet_dispatch: bool` config flag (default True) plus the injectable
`placer` parameter keep it testable and reversible without code edits.

(b) The claim gate becomes node-safe. Reality check (read from the merged
code): `Board.claim_task(agent_name, task_id)` in
`skcapstone/src/skcapstone/coordination.py` raises ValueError only when the
card is claimed by a DIFFERENT agent name; `EngineeringExecutor.claim`
(engineering.py line 160) claims as the literal "autopilot" on every node,
so two nodes racing on a stale placement would BOTH succeed. That was safe
only while dispatch was single-node by convention. Fix: the claimer becomes
node-scoped (`autopilot-<node>`, from `fleet_dispatch.claim_agent_name`),
a lost race raises the new typed `ClaimRaced`, and `phase2_swarm` records
the loser as a skip instead of crashing the worker pool.

Files:
- Modify `src/skharness/autocode/types.py` (add `ClaimRaced`)
- Modify `src/skharness/autocode/engineering.py` (`__init__` gains
  `agent_name`; `claim` and the `complete_task` call use it; ValueError from
  a lost claim becomes `ClaimRaced`)
- Modify `src/skharness/autocode/orchestrator.py` (release both claim names
  in `phase0_assess`; `run_once` gains `placer=None` and the gate;
  `phase2_swarm._process` catches `ClaimRaced`)
- Modify `src/skharness/autocode/config.py` (`Config.fleet_dispatch: bool =
  True`, loaded from yaml key `fleet_dispatch`)
- Modify `tests/conftest.py` (hermetic fleet root so existing live-run tests
  never consult the real `~/.skcapstone/fleet`)
- Modify `tests/test_autopilot_orchestrator.py` (one assertion:
  `release_stale_claims` is now called per claim name)
- Create `tests/test_autopilot_fleet_gate.py`

Interfaces (produced/changed):

```python
# types.py
class ClaimRaced(Exception): ...
# engineering.py
class EngineeringExecutor:
    def __init__(self, config, board, journal, digest=None, *,
                 agent_name: str | None = None) -> None
    # self.agent_name defaults to fleet_dispatch.claim_agent_name()
# orchestrator.py
def run_once(*, board, harness, config, tasks_dir=None, run_id=None,
             dry_run=None, ledger=None, deepdive_proposals=None,
             executors=None, task=None, tasks=None, tag=None,
             placer=None) -> dict
# the return dict gains "off_node": [{"ref", "node", "reason"}, ...]
```

Steps:

1. Add the hermetic-fleet fixture to `tests/conftest.py` (append; the file
   already has `_allow_empty_store`):

```python
@pytest.fixture(autouse=True)
def _hermetic_fleet(monkeypatch, tmp_path):
    """Point the fleet dispatch gate at an empty tree so orchestrator tests
    never consult the live ~/.skcapstone/fleet. The gate stays inert (no
    admitted nodes) unless a test builds its own tree under this root or
    overrides SKFLEET_ROOT itself."""
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "fleet-hermetic"))
```

   Then write the failing test, `tests/test_autopilot_fleet_gate.py`:

```python
"""run_once fleet gate + claim-race guard (Card 2.2 acceptance)."""
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from skharness.autocode import fleet_dispatch as fd
from skharness.autocode import orchestrator as orch
from skharness.autocode.config import Caps
from skharness.autocode.types import ClaimRaced, GateResult, Verdict, WorkItem


def _write_task(d, tid, tags=None):
    t = {"id": tid, "title": tid, "description": "d",
         "tags": tags or ["repo:skos"], "acceptance_criteria": ["works"],
         "dependencies": [], "status": "open"}
    (d / f"{tid}.json").write_text(json.dumps(t))


def _board(unblocked):
    b = MagicMock()
    b.unblocked_task_ids.return_value = set(unblocked)
    return b


def _config(**kw):
    base = dict(enabled=True, dry_run=False, caps=Caps(), repo_map={"skos": object()},
                fleet_dispatch=True, cleanup_after_run="off")
    base.update(kw)
    return SimpleNamespace(**base)


class _RunExec:
    kind = "engineering"

    def __init__(self):
        self.ran = []

    def selectable(self, item):
        return True

    def run(self, item, harness):
        self.ran.append(item.ref)
        return GateResult(score=5, passed=True, notes="ok", artifact="pr#1")

    def finalize(self, item, result):
        pass

    def escalate(self, item, reason):
        raise AssertionError("escalate not expected in these tests")


def _placer_from_views(views):
    """A real scheduler query over synthetic views (no live fleet needed)."""
    from skcapstone.fleet import scheduler as fsched

    def placer(item):
        wl = fsched.Workload(kind="job", name=item.ref,
                             node_selector=fd.card_selector(
                                 item.payload.get("tags") or []))
        d = fsched.select(views, wl)
        return fd.DispatchDecision(ref=item.ref, node=d.node, reason=d.reason)

    return placer


@pytest.mark.needs_skcapstone
@pytest.mark.parametrize("phase,cordoned", [("Dead", False), ("Ready", True)])
def test_dead_or_cordoned_heavy_node_routes_builds_here(tmp_path, monkeypatch,
                                                        phase, cordoned):
    """Card 2.2 acceptance: with node-41 cordoned or Dead, a run places all
    schedulable builds on node-158 with no config edit; a heavy-build card
    (selector only matches node-41) is skipped, never run on the wrong node."""
    from skcapstone.fleet.node_controller import NodeView

    views = [
        NodeView(name="node-158", phase="Ready",
                 allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase=phase, cordoned=cordoned,
                 labels={"heavy-build": "true"},
                 allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0}),
    ]
    _write_task(tmp_path, "t-plain")
    _write_task(tmp_path, "t-heavy", tags=["repo:skos", "node:heavy-build"])
    ex = _RunExec()
    board = _board(["t-plain", "t-heavy"])
    harness = SimpleNamespace(name="h",
                              assess=lambda b: Verdict(verdict="valid", reason=""))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", dry_run=False,
                        executors={"engineering": ex},
                        placer=_placer_from_views(views))
    assert ex.ran == ["t-plain"]
    assert out["selected"] == ["t-plain"]
    assert [(o["ref"], o["node"]) for o in out["off_node"]] == [("t-heavy", None)]


@pytest.mark.needs_skcapstone
def test_heavy_build_card_lands_on_heavy_node(tmp_path, monkeypatch):
    """With both nodes Ready, the heavy-build selector card is placed on
    node-41 (filtering, not preference) and skipped by the node-158 run."""
    from skcapstone.fleet.node_controller import NodeView

    views = [
        NodeView(name="node-158", phase="Ready",
                 allocatable={"cores": 7, "ram_gb": 24.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase="Ready", labels={"heavy-build": "true"},
                 allocatable={"cores": 15, "ram_gb": 12.0, "disk_gb": 200.0}),
    ]
    _write_task(tmp_path, "t-plain")
    _write_task(tmp_path, "t-heavy", tags=["repo:skos", "node:heavy-build"])
    ex = _RunExec()
    board = _board(["t-plain", "t-heavy"])
    harness = SimpleNamespace(name="h",
                              assess=lambda b: Verdict(verdict="valid", reason=""))
    monkeypatch.setenv("SKFLEET_NODE", "node-158")
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", dry_run=False,
                        executors={"engineering": ex},
                        placer=_placer_from_views(views))
    assert ex.ran == ["t-plain"]
    assert [(o["ref"], o["node"]) for o in out["off_node"]] == [("t-heavy", "node-41")]


def test_no_placer_and_no_fleet_keeps_current_behavior(tmp_path):
    """Gate inert (hermetic empty fleet root): everything runs locally."""
    _write_task(tmp_path, "t-1")
    ex = _RunExec()
    board = _board(["t-1"])
    harness = SimpleNamespace(name="h",
                              assess=lambda b: Verdict(verdict="valid", reason=""))
    out = orch.run_once(board=board, harness=harness, config=_config(),
                        tasks_dir=tmp_path, run_id="r1", dry_run=False,
                        executors={"engineering": ex})
    assert ex.ran == ["t-1"] and out["off_node"] == []


def test_lost_claim_raises_claim_raced():
    """The coord claim stays the execution gate: a card already claimed by
    another node's autopilot raises ClaimRaced instead of double-running."""
    from skharness.autocode.engineering import EngineeringExecutor

    board = MagicMock()
    board.claim_task.side_effect = ValueError(
        "Task t-1 already claimed by autopilot-node-41")
    ex = EngineeringExecutor(_config(), board, MagicMock(),
                             agent_name="autopilot-node-158")
    item = WorkItem(kind="engineering", ref="t-1", source="coord", repo="skos",
                    payload={"tags": ["repo:skos"]})
    with pytest.raises(ClaimRaced):
        ex.claim(item)
    board.claim_task.assert_called_once_with("autopilot-node-158", "t-1")


def test_swarm_records_claim_race_as_skip(monkeypatch):
    """A lost race is journaled as a skip: no escalation, no crash, no build."""
    class _Racing(_RunExec):
        def run(self, item, harness):
            raise ClaimRaced("t-1: already claimed by autopilot-node-41")

    monkeypatch.setattr(orch.journal, "write_run", lambda *a, **k: None)
    item = WorkItem(kind="engineering", ref="t-1", source="coord", repo="skos",
                    payload={"tags": []})
    decisions = []
    state = orch.phase2_swarm([(item, _Racing())], harness=MagicMock(),
                              board=MagicMock(), caps=Caps(max_concurrent=1),
                              ledger=orch.CapLedger(Caps()), decisions=decisions,
                              run_id="r1", enabled=True)
    assert state["t-1"]["state"] == "claim-raced"
    assert decisions == []
```

2. Run to fail. Expected first failure:
   `ImportError: cannot import name 'ClaimRaced' from 'skharness.autocode.types'`

3. Implement, smallest diffs first.

   `src/skharness/autocode/types.py`, append:

```python
class ClaimRaced(Exception):
    """The coord claim was won by another runtime (stale-placement guard).

    Raised instead of executing when claim_task reports the card claimed by a
    different agent name; the swarm records a skip, never a double-run.
    """
```

   `src/skharness/autocode/config.py`: add to the `Config` dataclass fields

```python
    fleet_dispatch: bool = True           # consult the fleet scheduler before swarm
```

   and in `Config.load(...)`, with the other kwargs:

```python
            fleet_dispatch=bool(raw.get("fleet_dispatch", True)),
```

   `src/skharness/autocode/engineering.py`: extend the constructor

```python
    def __init__(self, config, board, journal, digest=None, *,
                 agent_name: str | None = None) -> None:
        self.config = config
        self.board = board
        self.journal = journal
        self.digest = digest
        from .fleet_dispatch import claim_agent_name
        self.agent_name = agent_name or claim_agent_name()
        # Per-build LLM usage, keyed by item.ref, accrued across rounds in run()
        # and settled into the SKJoule wallet at finalize() on a twin-gate pass.
        self._build_usage: dict = {}   # item.ref -> joules.BuildUsage
```

   replace the body of `claim` (line 156):

```python
    def claim(self, item: WorkItem) -> None:
        """Claim the coord task before any work (a second runtime cannot double-
        execute), then record the lease start so a crash is reclaimable. The
        claimer is node-scoped (autopilot-<node>) so a stale fleet placement
        loses the race loudly (ClaimRaced) instead of double-running."""
        from .types import ClaimRaced
        with _BOARD_LOCK:                   # shared agent file (read-modify-write)
            try:
                self.board.claim_task(self.agent_name, item.ref)
            except ValueError as exc:
                raise ClaimRaced(f"{item.ref}: {exc}") from exc
        self.journal.record_claim(item.ref, claimed_at=_now_iso())
```

   and change the `complete_task` call (line 434) from
   `self.board.complete_task("autopilot", item.ref)` to
   `self.board.complete_task(self.agent_name, item.ref)`.
   (`DirectExecutor` in `direct.py` subclasses `EngineeringExecutor` and
   shares `claim`, so it is covered with no edit.)

   `src/skharness/autocode/orchestrator.py`:

   a. Import the seam with the other relative imports:
      `from . import fleet_dispatch`.

   b. In `phase0_assess`, replace

```python
    if not dry_run:
        board.release_stale_claims("autopilot", 3600)
```

      with

```python
    if not dry_run:
        for agent in sorted({"autopilot", fleet_dispatch.claim_agent_name()}):
            board.release_stale_claims(agent, 3600)
```

   c. `run_once` signature gains `placer=None` (after `tag`). In the body,
      after the `task`/`tasks`/`tag` filtering of `selected` and before the
      `if not dry:` swarm block, add:

```python
    off_node: list[tuple] = []
    if not dry:
        if placer is None and getattr(config, "fleet_dispatch", True):
            placer = fleet_dispatch.default_placer()
        selected, off_node = fleet_dispatch.partition_local(
            selected, placer=placer, self_node=fleet_dispatch.self_node())
        for item, decision in off_node:
            state[item.ref] = {"state": "off-node", "node": decision.node,
                               "reason": decision.reason}
```

      and extend the final return dict with:

```python
            "off_node": [{"ref": it.ref, "node": d.node, "reason": d.reason}
                         for it, d in off_node],
```

   d. In `phase2_swarm._process`, wrap the isolated build call:

```python
        try:
            result = ex.run(item, harness)          # ISOLATED build -- unlocked
        except ClaimRaced as exc:
            with _lock:
                state[item.ref] = {"state": "claim-raced", "detail": str(exc)}
                journal.write_run(run_id, {"run_id": run_id, "phase": "swarm",
                                           "items": dict(state)})
            return
```

      (add `ClaimRaced` to the existing `from .types import ...` line).

   e. Update the one stale assertion in
      `tests/test_autopilot_orchestrator.py::test_phase0_reclaims_then_computes_unblocked`:
      replace
      `board.release_stale_claims.assert_called_once_with("autopilot", 3600)`
      with

```python
    calls = [c.args for c in board.release_stale_claims.call_args_list]
    assert ("autopilot", 3600) in calls          # legacy name still reclaimed
```

4. Run to pass:
   `~/.skenv/bin/python -m pytest tests/test_autopilot_fleet_gate.py tests/test_autopilot_fleet_dispatch.py tests/test_autopilot_orchestrator.py -v`
   then the full skharness suite to prove no regression.
5. Commit: `feat(autocode): dispatch via fleet scheduler + node-scoped claim gate (ClaimRaced)`

Rollout note (operator steps, after both repos merge; not part of the test
cycle): the daily run stays where it is (`nodes: [noroc2027]` in jobs.yaml).
On .158 that run now writes `placements/job/*` (control-plane) and skips
cards placed elsewhere; a `.41` run picks up its own placements the same way
(add a `.41` scheduler entry when wanted; the claim gate makes overlap safe).
Ensure each sknoded systemd unit sets `Environment=SKFLEET_NODE=node-<name>`
so runtime node names match the admitted node objects. Reversal is one
config line: `fleet_dispatch: false` in autopilot.yaml.

---

## Task 7: `skfleet placements` + placement audit trail (cli.py)

Card: 2.3. The read path: every placement visible with its one-line reason
(which is exactly the `Decision.reason` string pinned by the Task 4 table),
and every CHANGED decision already logged to the per-node event log by
`scheduler.place` (Task 4). This task adds the CLI surface and the test that
ties the shown reasons to the pinned table.

Files:
- Modify `src/skcapstone/fleet/cli.py` (add the `placements` command)
- Create `tests/fleet/test_cli_placements.py`

Interfaces (produced):

```python
# cli.py gains:
@fleet.command("placements")     # options: --kind, --json
```

Consumes: Task 1 (`store.list_placements`), Task 4 (`scheduler.place` reason
strings + Placement events), real Phase 1 `default_paths`, the existing
`fleet` click group.

Steps:

1. Write the failing test, `tests/fleet/test_cli_placements.py`:

```python
"""Tests for skfleet placements (Card 2.3): visible decisions with reasons."""
from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from skcapstone.fleet import events, scheduler, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.node_controller import NodeView


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _views() -> list[NodeView]:
    return [
        NodeView(name="node-158", phase="Ready",
                 allocatable={"cores": 7, "ram_gb": 12.0, "disk_gb": 100.0}),
        NodeView(name="node-41", phase="Ready", labels={"heavy-build": "true"},
                 allocatable={"cores": 15, "ram_gb": 24.0, "disk_gb": 200.0}),
    ]


def _place_two(paths) -> None:
    sched = store.Writer(role="scheduler", node="node-158", identity="")
    scheduler.place(paths, scheduler.Workload(kind="job", name="card-1"),
                    writer=sched, views=_views())
    scheduler.place(paths, scheduler.Workload(kind="job", name="card-2",
                                              node_selector={"heavy-build": "true"}),
                    writer=sched, views=_views())


def test_placements_listing_shows_node_and_reason(paths) -> None:
    runner = CliRunner()
    env = {"SKFLEET_ROOT": str(paths.root)}
    assert "no placements" in runner.invoke(fleet, ["placements"], env=env).output
    _place_two(paths)
    out = runner.invoke(fleet, ["placements"], env=env)
    assert out.exit_code == 0
    assert "job/card-1" in out.output and "-> node-41" in out.output
    assert "least-loaded: node-41" in out.output      # the reason column
    payload = json.loads(runner.invoke(fleet, ["placements", "--json"],
                                       env=env).output)
    assert [p["name"] for p in payload] == ["card-1", "card-2"]
    # reasons match the pinned scheduler test table (Task 4 TABLE rows 1 and 3)
    assert all(p["reason"].startswith("least-loaded: node-41") for p in payload)
    only_jobs = json.loads(runner.invoke(fleet, ["placements", "--kind", "job",
                                                 "--json"], env=env).output)
    assert len(only_jobs) == 2


def test_every_placement_decision_is_logged(paths) -> None:
    _place_two(paths)
    for name in ("card-1", "card-2"):
        logged = events.read(paths, "node-158", kind="job", name=name)
        assert logged, f"no Placement event for {name}"
        assert logged[-1]["type"] == "Placement"
        assert logged[-1]["reason"] == "Placed"
        assert logged[-1]["message"].startswith("least-loaded: node-41")
```

2. Run to fail. Expected: click exits with code 2 and
   `Error: No such command 'placements'.` (the first assertion on
   "no placements" fails).

3. Implement. In `src/skcapstone/fleet/cli.py`, after the `describe`
   command, add:

```python
@fleet.command("placements")
@click.option("--kind", "kind", default=None,
              help="Filter by kind (e.g. job, service).")
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def placements_cmd(kind: str | None, as_json: bool) -> None:
    """Show current placements with the scheduler's reason for each decision."""
    records = store.list_placements(default_paths(), kind)
    if as_json:
        click.echo(jsonlib.dumps(records, indent=2, sort_keys=True))
        return
    if not records:
        click.echo("no placements")
        return
    for r in records:
        click.echo(f"{r['kind'].lower()}/{r['name']}\t-> {r['node']}\t"
                   f"gen={r['placementGeneration']}\t{r['reason']}")
```

   (No new imports needed: `store`, `default_paths`, `jsonlib`, and `click`
   are already imported by the merged `cli.py`.)

4. Run to pass, then the full `tests/fleet/` suite, then the wiring smoke:
   `~/.skenv/bin/pip install -e /home/cbrd21/clawd/skcapstone-repos/skcapstone`
   `~/.skenv/bin/skfleet placements --help`
5. Commit: `feat(fleet): skfleet placements view + audited decisions (Card 2.3)`

---

## Self-review

Card coverage (Phase 2 cards -> tasks):
- Card 2.1 (scheduler v1, descoped): Tasks 1-4. Placement single-writer +
  placementGeneration + write-on-change in Task 1; headroom source
  (allocatable from autoscale-backed Node status, capacity fallback for
  mixed-version fleets) in Task 2; the five filters with pinned exclusion
  reasons in Task 3; least-loaded selection, deterministic tiebreak,
  advisory PreferNoSchedule, freeze, idempotent re-run, and the pinned
  decision table in Task 4. Acceptance items from the spec map one to one:
  gpu selector lands on node-100 only with a toleration (TABLE rows 2 and
  4), cordon excludes (test_cordon_excludes_with_recorded_reason),
  least-loaded wins plus deterministic tiebreak
  (test_least_loaded_and_deterministic_tiebreak), frozen tree produces no
  writes and idempotent re-run produces no churn
  (test_place_writes_once_and_honors_freeze).
- Card 2.1b: listed as a gated stub after Task 4, deliberately not detailed.
- Card 2.2 (autopilot dispatch via scheduler, cross-repo skharness):
  Tasks 5-6. The real integration point (found by reading, documented in
  Task 5): there is no literal static node list in the engine; dispatch is
  single-node by convention (jobs.yaml `nodes: [noroc2027]` pin + per-node
  assignment tags in the `--tag` scope), so the replacement is a placement
  gate in `orchestrator.run_once` between `phase1_triage` and
  `phase2_swarm`. Acceptance: cordoned-or-Dead node-41 routes all builds to
  node-158 with no config edit (parametrized test in Task 6), heavy-build
  selector lands on node-41 when both are Ready (filtering, not
  preference), and the claim-race pair of tests proves a stale placement
  cannot double-run (node-scoped claimer + ClaimRaced skip). autoscale
  numbers: the scheduler consumes autoscale output via node.json
  allocatable (Task 2); the local pool sizing in `phase2_swarm` keeps
  calling the same `autoscale` library directly, so there is one source of
  capacity math and nothing to change there.
- Card 2.3 (`skfleet placements` + audit trail): Task 7, with the event log
  write landing in Task 4's `place` (one Placement event per changed
  decision) and the CLI test asserting the shown reasons match the Task 4
  pinned table strings.

Placeholder scan: no TODOs, no "add appropriate X", no elided bodies; every
test and implementation block is complete runnable code. Two soft points are
called out rather than hidden: the Task 6 rollout note (SKFLEET_NODE must be
set in the sknoded unit so runtime names match admitted objects), and the
`explain.py` registry deliberately gains nothing this phase (Phase 2 adds no
new KIND: jobs stay coord cards, and the scheduler is a component, not a
kind; the registry grows again in Phase 3 with Service).

Type and signature consistency, checked against the MERGED Phase 1 code (not
the Phase 1 plan text): `Writer(role, node, identity)` and
`store.write_spec(paths, kind, name, spec, *, writer, labels=None)` are used
with their exact real shapes in Tasks 1, 2, 5; `store.actuation_allowed
(paths)` and `store.is_frozen(paths)` exist verbatim in the merged store.py
and are consumed in Tasks 4-5; `events.emit(paths, writer, *, kind, name,
type, reason, message, now=None)` matches the merged events.py (only its
role set changes, one line, Task 1); `NodeView` gains one defaulted field so
every existing construction site (node_views, Phase 1 tests) is untouched;
`paths.placement_path(kind, name)` already existed and `merged()` already
reads it, so Task 1 needed a writer, not a reader rework. New store helper
added: `write_placement` (plus `read_placement` / `list_placements`); it did
not exist in the merged store.py (checked before writing this plan). On the
skharness side, `Board.claim_task(agent_name, task_id)`, `GateResult(score,
passed, notes, artifact)`, `WorkItem(kind, ref, source, repo, payload)`,
`run_once` and `phase2_swarm` keyword signatures, and the optional-sibling
`needs_skcapstone` marker were all read from the merged code and are used
with those exact shapes.

Reality-forced adjustments versus the spec text, stated explicitly:
1. "Headroom from Node status allocatable" required adding `allocatable` to
   node.json first (Phase 1 shipped only `capacity`); Task 2 does it with a
   capacity fallback for rollout.
2. "Replace the static dual-node list" is really "replace a convention":
   the gate lands in run_once because no such list exists in code.
3. The coord claim was only cross-node-safe by that same convention
   (claim_task permits re-claim under an identical agent name), so Card 2.2
   adds the node-scoped claimer; without it the claim-race acceptance test
   cannot pass honestly.
4. The scheduler event seat (`scheduler` in the events.emit role set) was
   needed for the Card 2.3 audit trail; spec 3.5 already sanctions
   control-plane processes appending under the local flock.
5. Non-control-plane runs query the scheduler purely and never persist
   placements, preserving the single-writer rule while letting any node
   compute identical decisions from synced views.
