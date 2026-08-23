# Fleet Control Plane, Phase 1: Implementation Plan (TDD, bite-sized)

Date: 2026-07-27
Author: Fable
Parent spec: `2026-07-27-skworld-fleet-control-plane-design.md` (rev 2)
Executes: revised Phase 1 (Cards 1.1, 1.2, 1.3, 1.4)

## Goal

Ship a live, truthful fleet inventory (Node kind, sknoded self-report,
NodeController, skfleet CLI) plus the substrate primitives every later phase
is born using: object store, events, freeze flag, writer-identity seam, and
join/admission self-enrollment.

## Architecture

All fleet state is JSON files under the Syncthing-shared `~/.skcapstone/fleet/`
tree, with exactly one writer per file fleet-wide (spec by the operator seat,
status by the owning node's sknoded). New code is a single Python package,
`skcapstone.fleet`, exposing a `skfleet` click CLI and an `sknoded` self-report
loop that reuses `skharness.autocode.autoscale` for capacity and
`skcapstone.doctor` for probes.

## Tech stack

- Python 3.11+, stdlib + click (already a skcapstone dependency), pytest.
- Repo: `/home/cbrd21/clawd/skcapstone-repos/skcapstone/` (src layout).
- Venv: `~/.skenv/` (all SK* packages). Test command from repo root:
  `~/.skenv/bin/python -m pytest tests/fleet/ -v`
- Reused libraries: `skcapstone.atomic_io.atomic_write_text`,
  `skharness.autocode.autoscale.resources`, `skcapstone.doctor.Check` and
  `skcapstone.doctor._check_sync_conflicts`.

## Global constraints (binding, copied from the spec)

1. Single-writer-per-FILE, fleet-wide: every file in the fleet tree has
   exactly one writer in the whole fleet, ever. Spec and status are separate
   files. A node NEVER writes another node's status subtree. sknoded refuses
   to write outside `status/$(self)/`. The scheduler never writes status;
   sknoded never writes placements or spec.
2. R2 flood discipline: write-on-change-else-skip for all status files
   (no-op writes suppressed); heartbeat is ONE small file per node,
   overwritten in place at a fixed low rate (default 60s); events are one
   bounded rotating file per node (size cap, one rotated sibling), rate-capped
   with a dedupe window; no per-event files, no fan-out, no broadcast.
3. Every spec file carries a `generation` (int, bumped on every spec write).
   Every status file carries `observedGeneration`. Staleness under eventual
   consistency is always detectable, never silent.
4. Dash ban: NEVER use em dashes or en dashes anywhere (code, docstrings,
   comments, docs, commit messages). Regular hyphens are fine.
5. Reuse autoscale.py and doctor as libraries: capacity math comes from
   `skharness.autocode.autoscale` (with a minimal same-shape fallback for a
   fresh box where skharness is not installed); condition probes reuse
   `skcapstone.doctor`.
6. Fleet files live under `~/.skcapstone/fleet/` (tests override via the
   `SKFLEET_ROOT` env var or by constructing `FleetPaths` directly).
7. New code lives in `skcapstone/fleet/`. In this repo's src layout that is
   `src/skcapstone/fleet/` on disk, import path `skcapstone.fleet`.
8. Repo conventions: type hints everywhere, Google-style docstrings, black
   formatting, pytest tests under `tests/` mirroring src. Commit messages end
   with the repo's standard `Co-Authored-By` trailer.

Card mapping: Tasks 1-6 are Card 1.1, Tasks 7-8 are Card 1.2, Tasks 9-10 are
Card 1.3, Task 11 is Card 1.4.

Shared test fixtures, created in Task 1 and used by every later task
(`tests/fleet/conftest.py`):

```python
"""Shared fixtures for fleet tests."""
from __future__ import annotations

import pytest

from skcapstone.fleet.paths import FleetPaths


@pytest.fixture
def paths(tmp_path) -> FleetPaths:
    """A throwaway fleet tree root."""
    return FleetPaths(root=tmp_path / "fleet")


@pytest.fixture
def operator():
    """The operator seat writer (spec owner)."""
    from skcapstone.fleet.store import Writer

    return Writer(role="operator", node="node-158", identity="capauth:chef@skworld.io")


@pytest.fixture
def noded41():
    """sknoded writer on node-41 (status owner for node-41 only)."""
    from skcapstone.fleet.store import Writer

    return Writer(role="sknoded", node="node-41", identity="")
```

---

## Task 1: Fleet tree layout and node identity (paths.py)

Card: 1.1. Everything else imports this, so it goes first.

Files:
- Create `src/skcapstone/fleet/__init__.py`
- Create `src/skcapstone/fleet/paths.py`
- Create `tests/fleet/__init__.py` (empty)
- Create `tests/fleet/conftest.py` (content above)
- Create `tests/fleet/test_paths.py`

Interfaces (produced):

```python
def valid_name(name: str) -> bool
@dataclass(frozen=True)
class FleetPaths:
    root: Path
    # properties: objects, placements, status  (all Path)
    def spec_path(self, kind: str, name: str) -> Path
    def placement_path(self, kind: str, name: str) -> Path
    def node_status_dir(self, node: str) -> Path
    def status_path(self, node: str, kind: str, name: str) -> Path
    def heartbeat_path(self, node: str) -> Path
    def node_report_path(self, node: str) -> Path
    def join_path(self, node: str) -> Path
    def events_path(self, node: str) -> Path
    def freeze_path(self) -> Path
def default_paths() -> FleetPaths     # SKFLEET_ROOT env or ~/.skcapstone/fleet
def self_node_name() -> str           # SKFLEET_NODE env or "node-" + hostname
```

Steps:

1. Write the failing test, `tests/fleet/test_paths.py`:

```python
"""Tests for the fleet tree layout helpers."""
from __future__ import annotations

from pathlib import Path

from skcapstone.fleet.paths import FleetPaths, default_paths, self_node_name, valid_name


def test_tree_layout(paths: FleetPaths) -> None:
    root = paths.root
    assert paths.spec_path("node", "node-41") == root / "objects" / "node" / "node-41.json"
    assert paths.placement_path("service", "skgateway") == root / "placements" / "service" / "skgateway.json"
    assert paths.status_path("node-41", "service", "skgateway") == root / "status" / "node-41" / "service" / "skgateway.json"
    assert paths.heartbeat_path("node-41") == root / "status" / "node-41" / "heartbeat.json"
    assert paths.node_report_path("node-41") == root / "status" / "node-41" / "node.json"
    assert paths.join_path("node-41") == root / "status" / "node-41" / "join.json"
    assert paths.events_path("node-41") == root / "status" / "node-41" / "events.jsonl"
    assert paths.freeze_path() == root / "objects" / "_freeze.json"


def test_default_paths_env_override(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SKFLEET_ROOT", str(tmp_path / "elsewhere"))
    assert default_paths().root == tmp_path / "elsewhere"
    monkeypatch.delenv("SKFLEET_ROOT")
    assert default_paths().root == Path("~/.skcapstone/fleet").expanduser()


def test_self_node_name(monkeypatch) -> None:
    monkeypatch.setenv("SKFLEET_NODE", "node-test")
    assert self_node_name() == "node-test"
    monkeypatch.delenv("SKFLEET_NODE")
    name = self_node_name()
    assert name.startswith("node-") and valid_name(name)


def test_valid_name_rejects_traversal() -> None:
    assert valid_name("skgateway")
    assert valid_name("node-41")
    assert not valid_name("../evil")
    assert not valid_name("a/b")
    assert not valid_name("")
    assert not valid_name("_freeze")
```

2. Run to fail:
   `~/.skenv/bin/python -m pytest tests/fleet/test_paths.py -v`
   Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet'`

3. Implement `src/skcapstone/fleet/__init__.py`:

```python
"""SKWorld fleet control plane substrate (spec rev 2, Phase 1)."""
```

   and `src/skcapstone/fleet/paths.py`:

```python
"""Fleet tree layout and node identity.

The fleet tree is a Syncthing-shared directory of JSON files. This module
is the single source of truth for where every file lives; nothing else in
the package builds fleet paths by hand.
"""
from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")


def valid_name(name: str) -> bool:
    """Return True when name is a safe kind/object/node name.

    Names must be lowercase alphanumeric with ._- separators and must not
    start with a separator or underscore, which blocks path traversal and
    reserves the underscore prefix (e.g. _freeze.json) for plane files.
    """
    return bool(_NAME_RE.match(name)) and "/" not in name and ".." not in name


@dataclass(frozen=True)
class FleetPaths:
    """All paths inside one fleet tree, derived from its root."""

    root: Path

    @property
    def objects(self) -> Path:
        return self.root / "objects"

    @property
    def placements(self) -> Path:
        return self.root / "placements"

    @property
    def status(self) -> Path:
        return self.root / "status"

    def spec_path(self, kind: str, name: str) -> Path:
        return self.objects / kind / f"{name}.json"

    def placement_path(self, kind: str, name: str) -> Path:
        return self.placements / kind / f"{name}.json"

    def node_status_dir(self, node: str) -> Path:
        return self.status / node

    def status_path(self, node: str, kind: str, name: str) -> Path:
        return self.node_status_dir(node) / kind / f"{name}.json"

    def heartbeat_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "heartbeat.json"

    def node_report_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "node.json"

    def join_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "join.json"

    def events_path(self, node: str) -> Path:
        return self.node_status_dir(node) / "events.jsonl"

    def freeze_path(self) -> Path:
        return self.objects / "_freeze.json"


def default_paths() -> FleetPaths:
    """The live fleet tree (SKFLEET_ROOT override for tests)."""
    root = os.environ.get("SKFLEET_ROOT", "~/.skcapstone/fleet")
    return FleetPaths(root=Path(root).expanduser())


def self_node_name() -> str:
    """This machine's node name (SKFLEET_NODE override, else hostname)."""
    env = os.environ.get("SKFLEET_NODE")
    if env:
        return env
    host = socket.gethostname().split(".")[0].lower()
    host = re.sub(r"[^a-z0-9-]", "-", host).strip("-") or "unknown"
    return f"node-{host}"
```

4. Run to pass: same command, 4 passed.
5. Commit: `feat(fleet): tree layout + node identity (paths.py)`

---

## Task 2: Spec store with generation and ownership guard (store.py part 1)

Card: 1.1.

Files:
- Create `src/skcapstone/fleet/store.py`
- Create `tests/fleet/test_store_spec.py`

Interfaces (produced; every later task uses these exact signatures):

```python
class OwnershipError(Exception)
@dataclass(frozen=True)
class Writer:
    role: str      # "operator" | "scheduler" | "sknoded" | "controller"
    node: str
    identity: str  # capauth identity string, "" until signing (Card 3.5)
def writer_identity() -> str
def write_spec(paths: FleetPaths, kind: str, name: str, spec: dict, *,
               writer: Writer, labels: dict | None = None) -> dict
def read_spec(paths: FleetPaths, kind: str, name: str) -> dict | None
def list_specs(paths: FleetPaths, kind: str) -> list[dict]
```

Consumes: Task 1 (`FleetPaths`, `valid_name`), `skcapstone.atomic_io`.

Steps:

1. Write the failing test, `tests/fleet/test_store_spec.py`:

```python
"""Tests for spec writes: generation, ownership, writer block."""
from __future__ import annotations

import pytest

from skcapstone.fleet import store


def test_write_spec_bumps_generation(paths, operator) -> None:
    first = store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    assert first["generation"] == 1
    assert first["kind"] == "Node"
    second = store.write_spec(paths, "node", "node-41", {"cordoned": True}, writer=operator)
    assert second["generation"] == 2
    on_disk = store.read_spec(paths, "node", "node-41")
    assert on_disk["spec"]["cordoned"] is True
    assert on_disk["generation"] == 2


def test_spec_carries_writer_identity_block(paths, operator) -> None:
    payload = store.write_spec(paths, "node", "node-41", {}, writer=operator)
    assert payload["writer"] == {
        "role": "operator",
        "node": "node-158",
        "identity": "capauth:chef@skworld.io",
        "signature": None,
    }


def test_non_operator_cannot_write_spec(paths, noded41) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_spec(paths, "node", "node-41", {}, writer=noded41)


def test_bad_names_rejected(paths, operator) -> None:
    with pytest.raises(store.OwnershipError):
        store.write_spec(paths, "node", "../evil", {}, writer=operator)
    with pytest.raises(store.OwnershipError):
        store.write_spec(paths, "no/kind", "x", {}, writer=operator)


def test_list_specs_sorted_and_empty(paths, operator) -> None:
    assert store.list_specs(paths, "service") == []
    store.write_spec(paths, "node", "node-b", {}, writer=operator)
    store.write_spec(paths, "node", "node-a", {}, writer=operator)
    assert [s["name"] for s in store.list_specs(paths, "node")] == ["node-a", "node-b"]
```

2. Run to fail. Expected:
   `ModuleNotFoundError: No module named 'skcapstone.fleet.store'` (then,
   after stubs, AttributeError / assertion failures).

3. Implement `src/skcapstone/fleet/store.py`:

```python
"""Fleet object store: spec, status, freeze, and the ownership guard.

Single-writer-per-file is the load-bearing invariant (spec 3.2). This
module is the only code allowed to touch fleet files, and it enforces
ownership at write time: operator role writes spec, sknoded writes only
its own node's status subtree, scheduler (Phase 2) writes placements.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_text
from .paths import FleetPaths, valid_name


class OwnershipError(Exception):
    """A writer attempted a write outside its ownership boundary."""


@dataclass(frozen=True)
class Writer:
    """Identity of a fleet writer (the seat, spec section 8).

    Attributes:
        role: One of operator, scheduler, sknoded, controller.
        node: Node name the writing process runs on.
        identity: capauth identity string; empty until signing (Card 3.5).
    """

    role: str
    node: str
    identity: str


def writer_identity() -> str:
    """Resolve this process's capauth identity, or "" when unavailable."""
    try:
        from capauth import resolve_agent_identity

        return resolve_agent_identity().capauth_uri
    except Exception:
        return ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _dump(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _writer_block(writer: Writer) -> dict:
    return {
        "role": writer.role,
        "node": writer.node,
        "identity": writer.identity,
        "signature": None,
    }


def write_spec(
    paths: FleetPaths,
    kind: str,
    name: str,
    spec: dict,
    *,
    writer: Writer,
    labels: dict | None = None,
) -> dict:
    """Write desired state for one object, bumping its generation.

    Only the operator seat may write spec (spec 3.2 ownership table).

    Returns:
        The full payload as written.
    Raises:
        OwnershipError: wrong role, or unsafe kind/name.
    """
    if writer.role != "operator":
        raise OwnershipError(f"role {writer.role!r} may not write spec files")
    if not (valid_name(kind) and valid_name(name)):
        raise OwnershipError(f"invalid kind/name: {kind!r}/{name!r}")
    path = paths.spec_path(kind, name)
    existing = _load(path) or {}
    payload = {
        "kind": kind.capitalize(),
        "name": name,
        "labels": labels if labels is not None else existing.get("labels", {}),
        "generation": int(existing.get("generation", 0)) + 1,
        "spec": spec,
        "writer": _writer_block(writer),
        "updatedAt": _now_iso(),
    }
    _dump(path, payload)
    return payload


def read_spec(paths: FleetPaths, kind: str, name: str) -> dict | None:
    """Read one spec file, or None when absent."""
    return _load(paths.spec_path(kind, name))


def list_specs(paths: FleetPaths, kind: str) -> list[dict]:
    """All specs of a kind, sorted by name. Zero objects cost nothing."""
    kind_dir = paths.objects / kind
    if not kind_dir.exists():
        return []
    out = []
    for p in sorted(kind_dir.glob("*.json")):
        payload = _load(p)
        if payload is not None:
            out.append(payload)
    return out
```

4. Run to pass: 5 passed.
5. Commit: `feat(fleet): spec store with generation + ownership guard`

---

## Task 3: Status store, write-on-change, node-owned files, merged read

Card: 1.1.

Files:
- Modify `src/skcapstone/fleet/store.py` (append functions)
- Create `tests/fleet/test_store_status.py`

Interfaces (produced):

```python
def write_status(paths: FleetPaths, kind: str, name: str, *, node: str,
                 status: dict, conditions: list[dict],
                 observed_generation: int, writer: Writer) -> bool
def read_status(paths: FleetPaths, kind: str, name: str, node: str) -> dict | None
def write_node_file(paths: FleetPaths, writer: Writer, filename: str,
                    payload: dict, *, if_changed: bool = True) -> bool
def read_node_file(paths: FleetPaths, node: str, filename: str) -> dict | None
def merged(paths: FleetPaths, kind: str, name: str) -> dict | None
```

`write_node_file` accepts only `heartbeat.json`, `node.json`, `join.json` and
writes them under `status/<writer.node>/` only. Both write functions return
False (and do not touch disk) when content, minus `updatedAt`, is unchanged.
`merged` returns `{"spec": ..., "placement": ..., "statuses": [...]}` where
each status gains `"stale": bool` (observedGeneration behind spec generation).

Steps:

1. Write the failing test, `tests/fleet/test_store_status.py`:

```python
"""Tests for status writes: ownership, write-on-change, merged read."""
from __future__ import annotations

import pytest

from skcapstone.fleet import store


def _write(paths, noded41, generation: int = 1, state: str = "active") -> bool:
    return store.write_status(
        paths, "service", "skgateway",
        node="node-41",
        status={"state": state},
        conditions=[{"type": "Ready", "status": "True", "reason": "UnitActive",
                     "message": "ok", "lastTransition": "2026-07-27T00:00:00Z"}],
        observed_generation=generation,
        writer=noded41,
    )


def test_status_ownership(paths, operator, noded41) -> None:
    with pytest.raises(store.OwnershipError):
        _write(paths, operator)          # operator may not write status
    other = store.Writer(role="sknoded", node="node-158", identity="")
    with pytest.raises(store.OwnershipError):
        store.write_status(paths, "service", "skgateway", node="node-41",
                           status={}, conditions=[], observed_generation=1,
                           writer=other)  # a node never writes another node's subtree


def test_write_on_change(paths, noded41) -> None:
    assert _write(paths, noded41) is True
    assert _write(paths, noded41) is False          # identical: no write
    assert _write(paths, noded41, state="failed") is True


def test_node_file_guard_and_change_detection(paths, noded41) -> None:
    assert store.write_node_file(paths, noded41, "node.json", {"a": 1}) is True
    assert store.write_node_file(paths, noded41, "node.json", {"a": 1}) is False
    assert store.write_node_file(paths, noded41, "heartbeat.json", {"ts": "x"},
                                 if_changed=False) is True
    with pytest.raises(store.OwnershipError):
        store.write_node_file(paths, noded41, "evil.json", {})
    assert store.read_node_file(paths, "node-41", "node.json")["a"] == 1
    written = paths.node_status_dir("node-41").rglob("*.json")
    assert all("node-41" in str(p) for p in written)


def test_merged_staleness(paths, operator, noded41) -> None:
    store.write_spec(paths, "service", "skgateway", {"unit": "skgateway.service"},
                     writer=operator)
    _write(paths, noded41, generation=1)
    m = store.merged(paths, "service", "skgateway")
    assert m["spec"]["generation"] == 1
    assert m["statuses"][0]["stale"] is False
    store.write_spec(paths, "service", "skgateway", {"unit": "skgateway.service",
                     "paused": True}, writer=operator)
    m = store.merged(paths, "service", "skgateway")
    assert m["statuses"][0]["stale"] is True        # observedGeneration 1 < generation 2
    assert store.merged(paths, "service", "missing") is None
```

2. Run to fail. Expected: `AttributeError: module 'skcapstone.fleet.store' has no attribute 'write_status'`

3. Implement (append to `store.py`):

```python
_NODE_FILES = {"heartbeat.json", "node.json", "join.json"}


def _changed(existing: dict | None, payload: dict) -> bool:
    if existing is None:
        return True
    strip = lambda d: {k: v for k, v in d.items() if k != "updatedAt"}  # noqa: E731
    return strip(existing) != strip(payload)


def write_status(
    paths: FleetPaths,
    kind: str,
    name: str,
    *,
    node: str,
    status: dict,
    conditions: list[dict],
    observed_generation: int,
    writer: Writer,
) -> bool:
    """Write observed state for one object on one node (write-on-change).

    Returns:
        True when a write happened, False when content was unchanged.
    Raises:
        OwnershipError: wrong role, or writer.node != node.
    """
    if writer.role != "sknoded":
        raise OwnershipError(f"role {writer.role!r} may not write status files")
    if writer.node != node:
        raise OwnershipError(f"{writer.node!r} may not write status for {node!r}")
    if not (valid_name(kind) and valid_name(name)):
        raise OwnershipError(f"invalid kind/name: {kind!r}/{name!r}")
    payload = {
        "kind": kind.capitalize(),
        "name": name,
        "node": node,
        "observedGeneration": observed_generation,
        "status": status,
        "conditions": conditions,
    }
    path = paths.status_path(node, kind, name)
    existing = _load(path)
    if not _changed(existing, payload):
        return False
    payload["updatedAt"] = _now_iso()
    _dump(path, payload)
    return True


def read_status(paths: FleetPaths, kind: str, name: str, node: str) -> dict | None:
    """Read one node's status file for an object, or None."""
    return _load(paths.status_path(node, kind, name))


def write_node_file(
    paths: FleetPaths,
    writer: Writer,
    filename: str,
    payload: dict,
    *,
    if_changed: bool = True,
) -> bool:
    """Write one of the node-owned singleton files (heartbeat/node/join).

    Only sknoded may call this, and only into its own subtree.
    """
    if writer.role != "sknoded":
        raise OwnershipError(f"role {writer.role!r} may not write node files")
    if filename not in _NODE_FILES:
        raise OwnershipError(f"not a node-owned file: {filename!r}")
    path = paths.node_status_dir(writer.node) / filename
    if if_changed and not _changed(_load(path), payload):
        return False
    body = dict(payload)
    body["updatedAt"] = _now_iso()
    _dump(path, body)
    return True


def read_node_file(paths: FleetPaths, node: str, filename: str) -> dict | None:
    """Read a node-owned singleton file, or None."""
    return _load(paths.node_status_dir(node) / filename)


def merged(paths: FleetPaths, kind: str, name: str) -> dict | None:
    """Assemble the object a reader sees: spec + placement + statuses.

    Each status gains a "stale" flag when its observedGeneration is behind
    the spec generation (spec 3.2: staleness detectable, never silent).
    """
    spec = read_spec(paths, kind, name)
    if spec is None:
        return None
    placement = _load(paths.placement_path(kind, name))
    statuses: list[dict] = []
    if paths.status.exists():
        for node_dir in sorted(p for p in paths.status.iterdir() if p.is_dir()):
            st = _load(node_dir / kind / f"{name}.json")
            if st is not None:
                st["stale"] = int(st.get("observedGeneration", 0)) < int(spec["generation"])
                statuses.append(st)
    return {"spec": spec, "placement": placement, "statuses": statuses}
```

4. Run to pass: `~/.skenv/bin/python -m pytest tests/fleet/ -v` (all tasks so far).
5. Commit: `feat(fleet): status store, write-on-change, merged read`

---

## Task 4: Per-node bounded event log (events.py)

Card: 1.1 (spec 3.5).

Files:
- Create `src/skcapstone/fleet/events.py`
- Create `tests/fleet/test_events.py`

Interfaces (produced):

```python
MAX_BYTES: int = 1_048_576
DEDUPE_WINDOW_S: float = 300.0
def emit(paths: FleetPaths, writer: Writer, *, kind: str, name: str,
         type: str, reason: str, message: str,
         now: float | None = None) -> bool
def read(paths: FleetPaths, node: str, *, kind: str | None = None,
         name: str | None = None, limit: int = 200) -> list[dict]
def reset_dedupe() -> None
```

`emit` appends one JSON line to `status/<writer.node>/events.jsonl` under an
exclusive flock on a `.events.lock` sidecar; rotates to `events.jsonl.1`
(overwrite) when the file reaches `MAX_BYTES`; returns False without writing
when the same (node, kind, name, type, reason) fired inside the dedupe
window. The `.events.lock` sidecar is excluded from sync (added to the
share's `.stignore`, noted in the Task 11 runbook). Only role `sknoded` or `controller` on its own
node may emit (single-writer-per-node for Syncthing purposes; same-node
processes serialize on the flock).

Steps:

1. Write the failing test, `tests/fleet/test_events.py`:

```python
"""Tests for the bounded per-node event log."""
from __future__ import annotations

import pytest

from skcapstone.fleet import events, store


@pytest.fixture(autouse=True)
def _fresh_dedupe():
    events.reset_dedupe()
    yield
    events.reset_dedupe()


def _emit(paths, noded41, *, reason="Started", now=1000.0) -> bool:
    return events.emit(paths, noded41, kind="service", name="skgateway",
                       type="Actuation", reason=reason, message="m", now=now)


def test_emit_appends_and_read_filters(paths, noded41) -> None:
    assert _emit(paths, noded41) is True
    assert _emit(paths, noded41, reason="Stopped", now=1001.0) is True
    all_events = events.read(paths, "node-41")
    assert [e["reason"] for e in all_events] == ["Started", "Stopped"]
    assert events.read(paths, "node-41", kind="service", name="other") == []
    assert all_events[0]["node"] == "node-41"


def test_dedupe_window(paths, noded41) -> None:
    assert _emit(paths, noded41, now=1000.0) is True
    assert _emit(paths, noded41, now=1100.0) is False       # inside window
    assert _emit(paths, noded41, now=1000.0 + 301.0) is True  # window passed


def test_rotation_bounded_to_two_files(paths, noded41, monkeypatch) -> None:
    monkeypatch.setattr(events, "MAX_BYTES", 200)
    for i in range(20):
        assert _emit(paths, noded41, reason=f"r{i}", now=1000.0 + i) is True
    live = paths.events_path("node-41")
    rotated = live.with_name("events.jsonl.1")
    assert live.exists() and rotated.exists()
    assert live.stat().st_size <= 400
    siblings = [p.name for p in live.parent.iterdir() if p.name.startswith("events.jsonl")]
    assert sorted(siblings) == ["events.jsonl", "events.jsonl.1"]   # two files, ever
    assert events.read(paths, "node-41", limit=5)[-1]["reason"] == "r19"


def test_emit_ownership(paths, operator) -> None:
    with pytest.raises(store.OwnershipError):
        events.emit(paths, operator, kind="service", name="x",
                    type="Actuation", reason="r", message="m")
```

2. Run to fail. Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet.events'`

3. Implement `src/skcapstone/fleet/events.py`:

```python
"""Append-only, bounded, per-node event log (spec 3.5).

One rotating JSONL file per node, never per object and never per event.
Events are causal history for the cognitive layer; they are observability,
not control flow: no controller may key a decision off this log.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import datetime, timezone

from .paths import FleetPaths
from .store import OwnershipError, Writer

MAX_BYTES = 1_048_576
DEDUPE_WINDOW_S = 300.0

_last_emit: dict[tuple[str, str, str, str, str], float] = {}


def reset_dedupe() -> None:
    """Clear the in-process dedupe memory (tests, daemon restart)."""
    _last_emit.clear()


def emit(
    paths: FleetPaths,
    writer: Writer,
    *,
    kind: str,
    name: str,
    type: str,
    reason: str,
    message: str,
    now: float | None = None,
) -> bool:
    """Append one event to this node's log; False when deduped.

    Flood-safe (R2): rate-capped by the dedupe window, bounded by size
    rotation, serialized by a local flock so same-node processes share
    one file while the single-writer-per-node invariant holds for sync.
    """
    if writer.role not in {"sknoded", "controller"}:
        raise OwnershipError(f"role {writer.role!r} may not emit events")
    ts = time.time() if now is None else now
    key = (writer.node, kind, name, type, reason)
    last = _last_emit.get(key)
    if last is not None and ts - last < DEDUPE_WINDOW_S:
        return False
    _last_emit[key] = ts
    line = json.dumps(
        {
            "ts": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "node": writer.node,
            "kind": kind,
            "name": name,
            "type": type,
            "reason": reason,
            "message": message,
            "count": 1,
        },
        sort_keys=True,
    )
    path = paths.events_path(writer.node)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(".events.lock")
    with open(lock_path, "w", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if path.exists() and path.stat().st_size >= MAX_BYTES:
            os.replace(path, path.with_name("events.jsonl.1"))
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return True


def read(
    paths: FleetPaths,
    node: str,
    *,
    kind: str | None = None,
    name: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Read events for a node, oldest first, filtered by kind/name."""
    out: list[dict] = []
    live = paths.events_path(node)
    for p in (live.with_name("events.jsonl.1"), live):
        if not p.exists():
            continue
        for raw in p.read_text(encoding="utf-8").splitlines():
            try:
                ev = json.loads(raw)
            except ValueError:
                continue
            if kind is not None and ev.get("kind") != kind:
                continue
            if name is not None and ev.get("name") != name:
                continue
            out.append(ev)
    return out[-limit:]
```

4. Run to pass.
5. Commit: `feat(fleet): bounded rotating per-node event log`

---

## Task 5: Freeze kill-switch primitive (store.py part 2)

Card: 1.1 (spec section 8, guardrail 2).

Files:
- Modify `src/skcapstone/fleet/store.py` (append functions)
- Create `tests/fleet/test_freeze.py`

Interfaces (produced):

```python
def is_frozen(paths: FleetPaths) -> bool
def set_frozen(paths: FleetPaths, frozen: bool, *, writer: Writer,
               reason: str = "") -> dict
def actuation_allowed(paths: FleetPaths) -> bool
```

`actuation_allowed` is the single helper every actuating component (scheduler
Phase 2, sknoded actuation Phase 3) must call before acting. Self-report is
NOT actuation and never checks it.

Steps:

1. Write the failing test, `tests/fleet/test_freeze.py`:

```python
"""Tests for the fleet-wide freeze kill-switch."""
from __future__ import annotations

import pytest

from skcapstone.fleet import store


def test_unfrozen_by_default(paths) -> None:
    assert store.is_frozen(paths) is False
    assert store.actuation_allowed(paths) is True


def test_freeze_round_trip(paths, operator) -> None:
    payload = store.set_frozen(paths, True, writer=operator, reason="incident drill")
    assert payload["frozen"] is True
    assert store.is_frozen(paths) is True
    assert store.actuation_allowed(paths) is False
    assert paths.freeze_path().exists()
    store.set_frozen(paths, False, writer=operator)
    assert store.is_frozen(paths) is False


def test_only_operator_may_toggle(paths, noded41) -> None:
    with pytest.raises(store.OwnershipError):
        store.set_frozen(paths, True, writer=noded41)


def test_garbage_freeze_file_fails_safe_frozen(paths) -> None:
    paths.freeze_path().parent.mkdir(parents=True, exist_ok=True)
    paths.freeze_path().write_text("not json")
    assert store.is_frozen(paths) is True     # unreadable flag = halt, not run
```

2. Run to fail. Expected: `AttributeError: module 'skcapstone.fleet.store' has no attribute 'is_frozen'`

3. Implement (append to `store.py`):

```python
def is_frozen(paths: FleetPaths) -> bool:
    """True when the fleet-wide kill-switch is on.

    An unreadable freeze file counts as frozen: when in doubt, halt
    actuation (running services are never touched by the flag itself).
    """
    path = paths.freeze_path()
    if not path.exists():
        return False
    payload = _load(path)
    if payload is None:
        return True
    return bool(payload.get("frozen"))


def set_frozen(
    paths: FleetPaths, frozen: bool, *, writer: Writer, reason: str = ""
) -> dict:
    """Toggle the kill-switch. Operator seat only (spec section 8)."""
    if writer.role != "operator":
        raise OwnershipError("only the operator seat may toggle freeze")
    payload = {
        "frozen": bool(frozen),
        "reason": reason,
        "writer": _writer_block(writer),
        "updatedAt": _now_iso(),
    }
    _dump(paths.freeze_path(), payload)
    return payload


def actuation_allowed(paths: FleetPaths) -> bool:
    """The one guard every actuating component checks before acting."""
    return not is_frozen(paths)
```

4. Run to pass.
5. Commit: `feat(fleet): freeze kill-switch primitive`

---

## Task 6: Single-node-mode invariant (right-sized complexity, spec 3.6)

Card: 1.1. Mostly tests; proves a 1-box fleet works and zero-object kinds
cost nothing. Locks the invariant so later phases cannot regress it silently.

Files:
- Create `tests/fleet/test_single_node.py`

Interfaces: consumes Tasks 1-5 only; produces no new code (any failure here
is fixed in the module that broke the invariant).

Steps:

1. Write the test, `tests/fleet/test_single_node.py`:

```python
"""Right-sized complexity invariant (spec 3.6): 1 box works, zero costs zero."""
from __future__ import annotations

from skcapstone.fleet import store

ALL_KINDS = ["node", "service", "cronjob", "agent", "modelserver", "config"]


def test_zero_object_kinds_cost_nothing(paths) -> None:
    for kind in ALL_KINDS:
        assert store.list_specs(paths, kind) == []
        assert store.merged(paths, kind, "anything") is None
    assert not paths.root.exists()      # reads created no directories at all


def test_one_box_fleet_is_complete(paths, operator) -> None:
    solo = store.Writer(role="sknoded", node="node-solo", identity="")
    store.write_spec(paths, "node", "node-solo", {"cordoned": False}, writer=operator)
    store.write_node_file(paths, solo, "heartbeat.json", {"ts": "t"}, if_changed=False)
    store.write_status(paths, "node", "node-solo", node="node-solo",
                       status={"capacity": {"cores": 4}}, conditions=[],
                       observed_generation=1, writer=solo)
    m = store.merged(paths, "node", "node-solo")
    assert m["spec"]["generation"] == 1
    assert m["statuses"][0]["stale"] is False
    # the whole tree is exactly the files this one node needs, nothing more
    files = sorted(str(p.relative_to(paths.root)) for p in paths.root.rglob("*") if p.is_file())
    assert files == [
        "objects/node/node-solo.json",
        "status/node-solo/heartbeat.json",
        "status/node-solo/node/node-solo.json",
    ]


def test_kinds_with_zero_objects_stay_no_op_after_use(paths, operator) -> None:
    store.write_spec(paths, "node", "node-solo", {}, writer=operator)
    assert store.list_specs(paths, "service") == []
    assert not (paths.objects / "service").exists()
    assert not paths.placements.exists()
```

2. Run: `~/.skenv/bin/python -m pytest tests/fleet/test_single_node.py -v`
   Expected: passes immediately if Tasks 1-5 are clean; any failure means a
   read path is creating directories or state it should not, and the fix goes
   into `store.py` (reads must never call mkdir).
3. Run the whole suite to confirm no regressions.
4. Commit: `test(fleet): single-node-mode invariant (right-sized complexity)`

---

## Task 7: Capacity probe and node conditions (capacity.py, conditions.py)

Card: 1.2. Reuses autoscale and doctor as libraries (global constraint 5).

Files:
- Create `src/skcapstone/fleet/capacity.py`
- Create `src/skcapstone/fleet/conditions.py`
- Create `tests/fleet/test_capacity_conditions.py`

Interfaces (produced):

```python
# capacity.py
def node_capacity() -> dict
# keys: cores int, ram_gb float, disk_gb float, gpu str | None, vram_gb float | None
def _fallback_resources() -> dict     # same keys as autoscale.resources()
def _gpu_info() -> dict | None        # {"name": str, "vram_gb": float}

# conditions.py
RAM_PRESSURE_GB: float = 2.0
DISK_PRESSURE_GB: float = 5.0
def node_conditions(capacity: dict, fleet_root: Path, now_iso: str) -> list[dict]
def merge_transitions(new: list[dict], old: list[dict]) -> list[dict]
```

Condition dicts follow spec section 4 exactly:
`{type, status: "True"|"False"|"Unknown", reason, message, lastTransition}`.
Types produced: `Ready`, `MemoryPressure`, `DiskPressure`, `SyncConflict`,
and `GPUAvailable` (only when a GPU is present). `merge_transitions` keeps
the old `lastTransition` when a condition's status did not change, which is
what makes node.json write-on-change actually skip writes.

Steps:

1. Write the failing test, `tests/fleet/test_capacity_conditions.py`:

```python
"""Tests for the capacity probe and condition derivation."""
from __future__ import annotations

from skcapstone.fleet import capacity, conditions

NOW = "2026-07-27T12:00:00Z"


def test_node_capacity_shape() -> None:
    cap = capacity.node_capacity()
    assert set(cap) == {"cores", "ram_gb", "disk_gb", "gpu", "vram_gb"}
    assert cap["cores"] >= 1 and cap["ram_gb"] > 0 and cap["disk_gb"] > 0


def test_fallback_matches_autoscale_shape() -> None:
    fb = capacity._fallback_resources()
    assert set(fb) == {"cores", "ram_gb", "disk_gb"}


def test_gpu_probe_absent_is_none(monkeypatch) -> None:
    import subprocess

    def boom(*a, **k):
        raise FileNotFoundError("nvidia-smi")

    monkeypatch.setattr(subprocess, "run", boom)
    assert capacity._gpu_info() is None


def _by_type(conds):
    return {c["type"]: c for c in conds}


def test_conditions_pressure_and_conflict(tmp_path) -> None:
    cap = {"cores": 4, "ram_gb": 1.0, "disk_gb": 100.0, "gpu": None, "vram_gb": None}
    conds = _by_type(conditions.node_conditions(cap, tmp_path, NOW))
    assert conds["Ready"]["status"] == "True"
    assert conds["MemoryPressure"]["status"] == "True"      # 1.0 < 2.0
    assert conds["DiskPressure"]["status"] == "False"
    assert conds["SyncConflict"]["status"] == "False"
    assert "GPUAvailable" not in conds
    (tmp_path / "x.sync-conflict-20260727").write_text("boom")
    conds = _by_type(conditions.node_conditions(cap, tmp_path, NOW))
    assert conds["SyncConflict"]["status"] == "True"


def test_gpu_condition_when_present(tmp_path) -> None:
    cap = {"cores": 4, "ram_gb": 8.0, "disk_gb": 100.0,
           "gpu": "RTX 5060 Ti", "vram_gb": 16.0}
    conds = _by_type(conditions.node_conditions(cap, tmp_path, NOW))
    assert conds["GPUAvailable"]["status"] == "True"


def test_merge_transitions_preserves_unchanged(tmp_path) -> None:
    old = [{"type": "Ready", "status": "True", "reason": "r", "message": "m",
            "lastTransition": "2026-07-26T00:00:00Z"}]
    new = [{"type": "Ready", "status": "True", "reason": "r", "message": "m",
            "lastTransition": NOW}]
    merged = conditions.merge_transitions(new, old)
    assert merged[0]["lastTransition"] == "2026-07-26T00:00:00Z"
    flipped = [{"type": "Ready", "status": "False", "reason": "r", "message": "m",
                "lastTransition": NOW}]
    assert conditions.merge_transitions(flipped, old)[0]["lastTransition"] == NOW
```

2. Run to fail. Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet.capacity'`

3. Implement `src/skcapstone/fleet/capacity.py`:

```python
"""Node capacity probe.

Reuses skharness.autocode.autoscale as a library (the single source of
capacity math fleet-wide, spec section 10). A same-shape fallback covers a
fresh box that does not have skharness installed yet (bootstrap, spec 9).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def _fallback_resources() -> dict:
    """Mirror autoscale.resources() keys without importing skharness."""
    ram_gb = 8.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                ram_gb = int(line.split()[1]) / 2**20
                break
    except OSError:
        pass
    try:
        disk_gb = shutil.disk_usage(Path.home()).free / 2**30
    except OSError:
        disk_gb = 20.0
    return {
        "cores": os.cpu_count() or 2,
        "ram_gb": round(ram_gb, 1),
        "disk_gb": round(disk_gb, 1),
    }


def _gpu_info() -> dict | None:
    """GPU name and VRAM via nvidia-smi, or None when absent."""
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    if out.returncode != 0 or not out.stdout.strip():
        return None
    first = out.stdout.strip().splitlines()[0]
    try:
        name, mem = (part.strip() for part in first.split(",", 1))
        return {"name": name, "vram_gb": round(float(mem) / 1024, 1)}
    except ValueError:
        return None


def node_capacity() -> dict:
    """Current host capacity for the node.json self-report."""
    try:
        from skharness.autocode.autoscale import resources

        base = resources()
    except Exception:
        base = _fallback_resources()
    gpu = _gpu_info()
    return {
        "cores": base["cores"],
        "ram_gb": base["ram_gb"],
        "disk_gb": base["disk_gb"],
        "gpu": gpu["name"] if gpu else None,
        "vram_gb": gpu["vram_gb"] if gpu else None,
    }
```

   and `src/skcapstone/fleet/conditions.py`:

```python
"""Node condition derivation (spec section 4 conventions).

Reuses skcapstone.doctor as a library for the sync-conflict probe (spec
3.4: a conflict file under the fleet tree is an ownership bug).
"""
from __future__ import annotations

from pathlib import Path

RAM_PRESSURE_GB = 2.0
DISK_PRESSURE_GB = 5.0


def _cond(type: str, active: bool, reason: str, message: str, now_iso: str) -> dict:
    return {
        "type": type,
        "status": "True" if active else "False",
        "reason": reason,
        "message": message,
        "lastTransition": now_iso,
    }


def node_conditions(capacity: dict, fleet_root: Path, now_iso: str) -> list[dict]:
    """Derive this node's conditions from a capacity snapshot."""
    from ..doctor import _check_sync_conflicts

    conds = [
        _cond("Ready", True, "SelfReport", "sknoded self-report alive", now_iso),
        _cond("MemoryPressure", float(capacity.get("ram_gb", 0.0)) < RAM_PRESSURE_GB,
              "FreeRam", f"{capacity.get('ram_gb')}GB available", now_iso),
        _cond("DiskPressure", float(capacity.get("disk_gb", 0.0)) < DISK_PRESSURE_GB,
              "FreeDisk", f"{capacity.get('disk_gb')}GB free", now_iso),
    ]
    check = _check_sync_conflicts(fleet_root)[0]
    conds.append(_cond("SyncConflict", not check.passed, "DoctorProbe",
                       check.detail, now_iso))
    if capacity.get("gpu"):
        conds.append(_cond("GPUAvailable", True, "NvidiaSmi",
                           str(capacity["gpu"]), now_iso))
    return conds


def merge_transitions(new: list[dict], old: list[dict]) -> list[dict]:
    """Keep old lastTransition when a condition's status is unchanged.

    Without this, every pass would stamp fresh timestamps and defeat the
    write-on-change discipline (R2).
    """
    prev = {c.get("type"): c for c in old}
    out = []
    for cond in new:
        before = prev.get(cond.get("type"))
        if before is not None and before.get("status") == cond.get("status"):
            cond = dict(cond, lastTransition=before.get("lastTransition"))
        out.append(cond)
    return out
```

4. Run to pass.
5. Commit: `feat(fleet): capacity probe (autoscale reuse) + node conditions (doctor reuse)`

---

## Task 8: sknoded v1 self-report + join request (sknoded.py) + systemd unit

Card: 1.2.

Files:
- Create `src/skcapstone/fleet/sknoded.py`
- Create `systemd/sknoded.service`
- Create `tests/fleet/test_sknoded.py`

Interfaces (produced):

```python
HEARTBEAT_INTERVAL_S: int = 60
def build_heartbeat(node: str, now_iso: str) -> dict
def build_node_report(paths: FleetPaths, node: str, now_iso: str) -> dict
def build_join_request(paths: FleetPaths, node: str, capacity: dict, now_iso: str) -> dict
def run_once(paths: FleetPaths, node: str) -> dict
# returns {"heartbeat": bool, "node": bool, "join": bool} (True = wrote)
def main_loop(paths: FleetPaths, node: str, *,
              interval: int = HEARTBEAT_INTERVAL_S, once: bool = False) -> None
```

`build_node_report` produces the section 4 status payload: capacity (Task 7),
conditions (with `merge_transitions` against the previous report so unchanged
passes are write-on-change no-ops), versions, and `observedGeneration` equal
to the node spec's generation (0 while unadmitted). `run_once` writes
heartbeat (always), node.json (on change), and a join request only when there
is no `objects/node/<self>.json` AND no existing join file. It writes through
`store.write_node_file` only, so the ownership guard makes it physically
unable to write outside `status/<self>/`.

Steps:

1. Write the failing test, `tests/fleet/test_sknoded.py`:

```python
"""Tests for sknoded v1: self-report + join request."""
from __future__ import annotations

import pytest

from skcapstone.fleet import sknoded, store

CAP = {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0, "gpu": None, "vram_gb": None}


@pytest.fixture(autouse=True)
def _fixed_capacity(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity", lambda: dict(CAP))


def test_first_run_writes_all_three(paths) -> None:
    result = sknoded.run_once(paths, "node-41")
    assert result == {"heartbeat": True, "node": True, "join": True}
    hb = store.read_node_file(paths, "node-41", "heartbeat.json")
    assert hb["name"] == "node-41" and "ts" in hb
    report = store.read_node_file(paths, "node-41", "node.json")
    assert report["status"]["capacity"]["cores"] == 4
    assert report["observedGeneration"] == 0           # unadmitted
    join = store.read_node_file(paths, "node-41", "join.json")
    assert join["name"] == "node-41" and join["capacity"]["ram_gb"] == 8.0


def test_second_run_is_write_on_change(paths) -> None:
    sknoded.run_once(paths, "node-41")
    result = sknoded.run_once(paths, "node-41")
    assert result["heartbeat"] is True     # heartbeat always beats
    assert result["node"] is False         # unchanged report skipped
    assert result["join"] is False         # join written once


def test_admitted_node_reports_generation_and_stops_joining(paths, operator) -> None:
    sknoded.run_once(paths, "node-41")
    store.write_spec(paths, "node", "node-41", {"cordoned": False}, writer=operator)
    result = sknoded.run_once(paths, "node-41")
    assert result["node"] is True          # observedGeneration 0 -> 1 changed
    assert store.read_node_file(paths, "node-41", "node.json")["observedGeneration"] == 1


def test_never_writes_outside_own_subtree(paths) -> None:
    sknoded.run_once(paths, "node-41")
    written = [p for p in paths.root.rglob("*") if p.is_file()]
    assert written and all(
        str(p).startswith(str(paths.node_status_dir("node-41"))) for p in written
    )
```

2. Run to fail. Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet.sknoded'`

3. Implement `src/skcapstone/fleet/sknoded.py`:

```python
"""sknoded v1: the per-node self-report loop (spec section 6, step 1).

Phase 1 is report-only: heartbeat + node.json + join request. Actuation
arrives in Phase 3 and will gate on store.actuation_allowed().
"""
from __future__ import annotations

import platform
import socket
import time
from datetime import datetime, timezone

from .. import __version__ as skcapstone_version
from . import store
from .capacity import node_capacity
from .conditions import merge_transitions, node_conditions
from .paths import FleetPaths

HEARTBEAT_INTERVAL_S = 60


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_heartbeat(node: str, now_iso: str) -> dict:
    """The one small heartbeat file, overwritten in place (R2)."""
    return {"kind": "Node", "name": node, "node": node, "ts": now_iso}


def build_node_report(paths: FleetPaths, node: str, now_iso: str) -> dict:
    """Capacity + conditions + versions, with stable lastTransition."""
    cap = node_capacity()
    conds = node_conditions(cap, paths.root, now_iso)
    previous = store.read_node_file(paths, node, "node.json") or {}
    conds = merge_transitions(conds, previous.get("conditions", []))
    spec = store.read_spec(paths, "node", node)
    return {
        "kind": "Node",
        "name": node,
        "node": node,
        "observedGeneration": int(spec["generation"]) if spec else 0,
        "status": {
            "capacity": cap,
            "versions": {
                "python": platform.python_version(),
                "skcapstone": skcapstone_version,
            },
        },
        "conditions": conds,
    }


def build_join_request(paths: FleetPaths, node: str, capacity: dict, now_iso: str) -> dict:
    """Join marker for admission (spec section 9)."""
    return {
        "name": node,
        "addresses": {"hostname": socket.gethostname()},
        "capacity": capacity,
        "identity": store.writer_identity(),
        "requestedAt": now_iso,
    }


def run_once(paths: FleetPaths, node: str) -> dict:
    """One self-report pass. Returns which files were actually written."""
    now_iso = _now_iso()
    writer = store.Writer(role="sknoded", node=node, identity=store.writer_identity())
    heartbeat = store.write_node_file(
        paths, writer, "heartbeat.json", build_heartbeat(node, now_iso), if_changed=False
    )
    report = build_node_report(paths, node, now_iso)
    node_written = store.write_node_file(paths, writer, "node.json", report)
    join_written = False
    unadmitted = store.read_spec(paths, "node", node) is None
    if unadmitted and store.read_node_file(paths, node, "join.json") is None:
        join = build_join_request(paths, node, report["status"]["capacity"], now_iso)
        join_written = store.write_node_file(paths, writer, "join.json", join, if_changed=False)
    return {"heartbeat": heartbeat, "node": node_written, "join": join_written}


def main_loop(
    paths: FleetPaths,
    node: str,
    *,
    interval: int = HEARTBEAT_INTERVAL_S,
    once: bool = False,
) -> None:
    """The daemon loop behind sknoded.service."""
    while True:
        run_once(paths, node)
        if once:
            return
        time.sleep(interval)
```

   Note: `from .. import __version__` must exist in `skcapstone/__init__.py`
   (it does; verify with `~/.skenv/bin/python -c "import skcapstone; print(skcapstone.__version__)"`,
   and if the attribute has another name, adapt this one import, not the tests).

   Create `systemd/sknoded.service`:

```ini
[Unit]
Description=SKWorld fleet node agent (self-report, Phase 1)
After=network.target

[Service]
ExecStart=%h/.skenv/bin/skfleet sknoded --interval 60
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
```

4. Run to pass.
5. Commit: `feat(fleet): sknoded v1 self-report + join request + systemd unit`

---

## Task 9: NodeController: phases, views, cordon (node_controller.py)

Card: 1.3.

Files:
- Create `src/skcapstone/fleet/node_controller.py`
- Create `tests/fleet/test_node_controller.py`

Interfaces (produced):

```python
NOT_READY_AFTER_S: int = 180
DEAD_AFTER_S: int = 300
@dataclass
class NodeView:
    name: str
    phase: str                 # "Ready" | "NotReady" | "Dead" | "Pending"
    cordoned: bool
    labels: dict
    taints: list
    capacity: dict
    heartbeat_age_s: float | None
    conditions: list
def node_views(paths: FleetPaths, *, now: datetime | None = None) -> list[NodeView]
def cordon(paths: FleetPaths, name: str, cordoned: bool, *, writer: Writer) -> dict
```

Phase rules: a node with a join request but no spec is `Pending`. An admitted
node is `Ready` when heartbeat age <= 180s, `NotReady` when <= 300s, `Dead`
beyond that (or when it has never beaten). `cordon` rewrites the node spec
through `store.write_spec` (generation bumps, single spec writer preserved).

Steps:

1. Write the failing test, `tests/fleet/test_node_controller.py`:

```python
"""Tests for NodeController phase derivation and cordon."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from skcapstone.fleet import node_controller, store

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)


def _beat(paths, node: str, age_s: float) -> None:
    writer = store.Writer(role="sknoded", node=node, identity="")
    ts = (NOW - timedelta(seconds=age_s)).strftime("%Y-%m-%dT%H:%M:%SZ")
    store.write_node_file(paths, writer, "heartbeat.json",
                          {"kind": "Node", "name": node, "node": node, "ts": ts},
                          if_changed=False)


def _admit(paths, operator, node: str) -> None:
    store.write_spec(paths, "node", node, {"cordoned": False, "taints": []},
                     writer=operator, labels={"tier": "test"})


def test_phases_from_heartbeat_age(paths, operator) -> None:
    for node, age in [("node-a", 10), ("node-b", 200), ("node-c", 400)]:
        _admit(paths, operator, node)
        _beat(paths, node, age)
    views = {v.name: v for v in node_controller.node_views(paths, now=NOW)}
    assert views["node-a"].phase == "Ready"
    assert views["node-b"].phase == "NotReady"
    assert views["node-c"].phase == "Dead"
    assert views["node-a"].heartbeat_age_s == 10.0


def test_never_beaten_is_dead_and_join_is_pending(paths, operator) -> None:
    _admit(paths, operator, "node-silent")
    joiner = store.Writer(role="sknoded", node="node-new", identity="")
    store.write_node_file(paths, joiner, "join.json", {"name": "node-new"},
                          if_changed=False)
    views = {v.name: v for v in node_controller.node_views(paths, now=NOW)}
    assert views["node-silent"].phase == "Dead"
    assert views["node-new"].phase == "Pending"


def test_cordon_round_trip(paths, operator) -> None:
    _admit(paths, operator, "node-a")
    _beat(paths, "node-a", 10)
    updated = node_controller.cordon(paths, "node-a", True, writer=operator)
    assert updated["spec"]["cordoned"] is True
    assert updated["generation"] == 2
    view = {v.name: v for v in node_controller.node_views(paths, now=NOW)}["node-a"]
    assert view.cordoned is True
    node_controller.cordon(paths, "node-a", False, writer=operator)
    assert store.read_spec(paths, "node", "node-a")["spec"]["cordoned"] is False
```

2. Run to fail. Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet.node_controller'`

3. Implement `src/skcapstone/fleet/node_controller.py`:

```python
"""NodeController: derived node health and the cordon action (spec 5.1).

Runs on the control-plane node. It is the only component allowed to mark a
node schedulable or not; sknoded self-reports raw observations only.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from . import store
from .paths import FleetPaths

NOT_READY_AFTER_S = 180
DEAD_AFTER_S = 300


@dataclass
class NodeView:
    """One row of the fleet inventory (skfleet nodes)."""

    name: str
    phase: str
    cordoned: bool = False
    labels: dict = field(default_factory=dict)
    taints: list = field(default_factory=list)
    capacity: dict = field(default_factory=dict)
    heartbeat_age_s: float | None = None
    conditions: list = field(default_factory=list)


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
                heartbeat_age_s=age,
                conditions=report.get("conditions", []),
            )
        )
    return views


def cordon(paths: FleetPaths, name: str, cordoned: bool, *, writer: store.Writer) -> dict:
    """Set or clear the cordon flag on a node spec (operator action)."""
    current = store.read_spec(paths, "node", name)
    if current is None:
        raise LookupError(f"no such node object: {name!r}")
    new_spec = dict(current.get("spec", {}), cordoned=cordoned)
    return store.write_spec(paths, "node", name, new_spec, writer=writer,
                            labels=current.get("labels", {}))
```

4. Run to pass.
5. Commit: `feat(fleet): NodeController phases + cordon`

---

## Task 10: skfleet CLI + explain seam (cli.py, explain.py)

Card: 1.3. Also wires the CLI into the skcapstone entry points.

Files:
- Create `src/skcapstone/fleet/explain.py`
- Create `src/skcapstone/fleet/cli.py`
- Modify `src/skcapstone/cli/__init__.py` (register the group, mirroring the
  existing pattern: add `from ..fleet.cli import register_fleet_commands` to
  the import block and `register_fleet_commands(main)` beside the other
  `register_*_commands(main)` calls at the bottom)
- Modify `pyproject.toml`: add `skfleet = "skcapstone.fleet.cli:main"` under
  `[project.scripts]`
- Create `tests/fleet/test_cli.py`

Interfaces (produced):

```python
# explain.py
KINDS: dict[str, dict]     # registry; "node" only in Phase 1, grows per phase
def explain(kind: str | None = None) -> dict
# cli.py
fleet: click.Group         # commands: nodes, describe, cordon, uncordon,
                           # explain, freeze, unfreeze, sknoded, admit (Task 11)
def register_fleet_commands(main: click.Group) -> None
def main() -> None         # console script entry (skfleet)
```

Steps:

1. Write the failing test, `tests/fleet/test_cli.py`:

```python
"""Tests for the skfleet CLI surface."""
from __future__ import annotations

import json

from click.testing import CliRunner

from skcapstone.fleet import sknoded, store
from skcapstone.fleet.cli import fleet
from skcapstone.fleet.explain import explain


def _env(paths) -> dict:
    return {"SKFLEET_ROOT": str(paths.root), "SKFLEET_NODE": "node-cli"}


def test_explain_registry() -> None:
    assert explain() == {"kinds": ["node"]}
    node = explain("node")
    assert node["kind"] == "Node"
    assert "Ready" in node["conditions"]
    assert any("cordon" in a for a in node["actions"])


def test_cli_nodes_and_describe(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0,
                                 "gpu": None, "vram_gb": None})
    sknoded.run_once(paths, "node-cli")
    store.write_spec(paths, "node", "node-cli", {"cordoned": False},
                     writer=operator, labels={"interactive": "true"})
    runner = CliRunner()
    out = runner.invoke(fleet, ["nodes"], env=_env(paths))
    assert out.exit_code == 0
    assert "node-cli" in out.output and "Ready" in out.output
    out = runner.invoke(fleet, ["describe", "node", "node-cli"], env=_env(paths))
    assert out.exit_code == 0
    payload = json.loads(out.output)
    assert payload["spec"]["name"] == "node-cli"


def test_cli_explain_json(paths) -> None:
    runner = CliRunner()
    out = runner.invoke(fleet, ["explain", "node", "--json"], env=_env(paths))
    assert out.exit_code == 0
    assert json.loads(out.output)["kind"] == "Node"


def test_cli_cordon_and_freeze(paths, operator) -> None:
    store.write_spec(paths, "node", "node-cli", {"cordoned": False}, writer=operator)
    runner = CliRunner()
    assert runner.invoke(fleet, ["cordon", "node-cli"], env=_env(paths)).exit_code == 0
    assert store.read_spec(paths, "node", "node-cli")["spec"]["cordoned"] is True
    assert runner.invoke(fleet, ["uncordon", "node-cli"], env=_env(paths)).exit_code == 0
    assert runner.invoke(fleet, ["freeze", "--reason", "drill"], env=_env(paths)).exit_code == 0
    assert store.is_frozen(paths) is True
    assert runner.invoke(fleet, ["unfreeze"], env=_env(paths)).exit_code == 0
    assert store.is_frozen(paths) is False
```

2. Run to fail. Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet.cli'`

3. Implement `src/skcapstone/fleet/explain.py`:

```python
"""Self-describing surface (spec section 8): kinds, fields, actions.

A fresh AI operator discovers the system from this registry at runtime
instead of via hardcoding. Phase 1 registers Node; each later phase adds
its kind here as part of shipping it.
"""
from __future__ import annotations

KINDS: dict[str, dict] = {
    "node": {
        "kind": "Node",
        "description": "A machine in the fleet.",
        "spec": {
            "labels": "label map used by selectors (exact match, AND)",
            "taints": "list of {key, value, effect: NoSchedule|PreferNoSchedule}",
            "cordoned": "bool; excluded from scheduling when true",
            "capacityOverrides": "optional manual capacity caps",
            "address": "LAN + tailscale addresses, ssh target",
        },
        "status": {
            "capacity": "cores, ram_gb, disk_gb, gpu, vram_gb (autoscale probe)",
            "conditions": "list of {type, status, reason, message, lastTransition}",
            "versions": "python + skcapstone versions on the node",
        },
        "conditions": {
            "Ready": "sknoded self-report is alive",
            "MemoryPressure": "free RAM below threshold",
            "DiskPressure": "free disk below threshold",
            "GPUAvailable": "a GPU is present and probed",
            "SyncConflict": "sync-conflict files under the fleet tree (ownership bug)",
        },
        "actions": [
            "skfleet nodes",
            "skfleet describe node <name>",
            "skfleet cordon <name>",
            "skfleet uncordon <name>",
            "skfleet admit <name>",
        ],
    },
}


def explain(kind: str | None = None) -> dict:
    """Describe registered kinds, or one kind in detail."""
    if kind is None:
        return {"kinds": sorted(KINDS)}
    if kind not in KINDS:
        raise KeyError(f"unknown kind: {kind!r} (known: {sorted(KINDS)})")
    return KINDS[kind]
```

   Implement `src/skcapstone/fleet/cli.py`:

```python
"""The skfleet CLI: fleet inventory, cordon, freeze, explain, sknoded.

Available standalone as `skfleet` and as `skcapstone fleet ...`.
"""
from __future__ import annotations

import json as jsonlib

import click

from . import node_controller, sknoded as sknoded_mod, store
from .explain import explain as explain_kind
from .paths import default_paths, self_node_name


def _operator() -> store.Writer:
    return store.Writer(role="operator", node=self_node_name(),
                        identity=store.writer_identity())


@click.group(name="fleet")
def fleet() -> None:
    """SKWorld fleet control plane (skfleet)."""


@fleet.command("nodes")
def nodes_cmd() -> None:
    """List all fleet nodes with phase, labels, and capacity."""
    for v in node_controller.node_views(default_paths()):
        labels = ",".join(f"{k}={val}" for k, val in sorted(v.labels.items()))
        cordoned = " CORDONED" if v.cordoned else ""
        age = "never" if v.heartbeat_age_s is None else f"{int(v.heartbeat_age_s)}s"
        click.echo(
            f"{v.name}\t{v.phase}{cordoned}\t[{labels}]\t"
            f"cores={v.capacity.get('cores', '?')} "
            f"ram={v.capacity.get('ram_gb', '?')}GB "
            f"disk={v.capacity.get('disk_gb', '?')}GB\tbeat={age}"
        )


@fleet.command("describe")
@click.argument("kind")
@click.argument("name")
def describe_cmd(kind: str, name: str) -> None:
    """Show the merged object (spec + placement + statuses) as JSON."""
    payload = store.merged(default_paths(), kind, name)
    if payload is None:
        raise click.ClickException(f"no such object: {kind}/{name}")
    click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))


@fleet.command("cordon")
@click.argument("name")
def cordon_cmd(name: str) -> None:
    """Mark a node unschedulable."""
    node_controller.cordon(default_paths(), name, True, writer=_operator())
    click.echo(f"{name} cordoned")


@fleet.command("uncordon")
@click.argument("name")
def uncordon_cmd(name: str) -> None:
    """Mark a node schedulable again."""
    node_controller.cordon(default_paths(), name, False, writer=_operator())
    click.echo(f"{name} uncordoned")


@fleet.command("explain")
@click.argument("kind", required=False)
@click.option("--json", "as_json", is_flag=True, help="Machine-readable output.")
def explain_cmd(kind: str | None, as_json: bool) -> None:
    """Describe the fleet object model (kinds, fields, conditions, actions)."""
    try:
        payload = explain_kind(kind)
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    if as_json:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))
    else:
        click.echo(jsonlib.dumps(payload, indent=2, sort_keys=True))


@fleet.command("freeze")
@click.option("--reason", default="", help="Why the fleet is frozen.")
def freeze_cmd(reason: str) -> None:
    """Halt ALL fleet actuation (services keep running). Kill-switch on."""
    store.set_frozen(default_paths(), True, writer=_operator(), reason=reason)
    click.echo("fleet FROZEN: actuation halted, services untouched")


@fleet.command("unfreeze")
def unfreeze_cmd() -> None:
    """Kill-switch off: actuation resumes."""
    store.set_frozen(default_paths(), False, writer=_operator())
    click.echo("fleet unfrozen")


@fleet.command("sknoded")
@click.option("--once", is_flag=True, help="One self-report pass, then exit.")
@click.option("--interval", default=sknoded_mod.HEARTBEAT_INTERVAL_S, show_default=True)
def sknoded_cmd(once: bool, interval: int) -> None:
    """Run the node agent self-report loop for this machine."""
    sknoded_mod.main_loop(default_paths(), self_node_name(),
                          interval=interval, once=once)


def register_fleet_commands(main: click.Group) -> None:
    """Register the fleet group on the skcapstone CLI."""
    main.add_command(fleet)


def main() -> None:
    """Console script entry point (skfleet)."""
    fleet()
```

   (The plain and `--json` outputs are identical for now; the flag is the
   stable machine contract, the plain form is free to grow tables later.)

   Wire up: in `src/skcapstone/cli/__init__.py` add
   `from ..fleet.cli import register_fleet_commands` with the other imports
   and `register_fleet_commands(main)` with the other register calls. In
   `pyproject.toml` add under `[project.scripts]`:
   `skfleet = "skcapstone.fleet.cli:main"`.

4. Run to pass, then verify the wiring:
   `~/.skenv/bin/pip install -e /home/cbrd21/clawd/skcapstone-repos/skcapstone`
   `~/.skenv/bin/skcapstone fleet --help` and `~/.skenv/bin/skfleet --help`
   both list nodes/describe/cordon/uncordon/explain/freeze/unfreeze/sknoded.
   Also run the full existing suite subset to prove no CLI regression:
   `~/.skenv/bin/python -m pytest tests/test_cli_status.py tests/fleet/ -v`
5. Commit: `feat(fleet): skfleet CLI + explain seam + entry points`

---

## Task 11: Self-enrollment and admission + cold-start runbook (admission.py)

Card: 1.4.

Files:
- Create `src/skcapstone/fleet/admission.py`
- Modify `src/skcapstone/fleet/cli.py` (add the `admit` command)
- Create `docs/runbooks/fleet-cold-start.md`
- Create `tests/fleet/test_admission.py`

Interfaces (produced):

```python
PRESETS: dict[str, dict]   # node-158 / node-41 / node-100 / node-local
def pending_joins(paths: FleetPaths) -> list[dict]
def admit(paths: FleetPaths, node: str, *, writer: Writer,
          labels: dict | None = None, taints: list | None = None,
          preset: bool = False, bootstrap: bool = False) -> dict
def auto_admit(paths: FleetPaths, trusted: set[str], *, writer: Writer) -> list[str]
```

`admit` is idempotent (an existing spec is returned untouched), requires a
join request unless `bootstrap=True` (first-node case, spec section 9), and
mints the spec via `store.write_spec` so generation and writer identity work
like every other spec write. `auto_admit` admits pending joiners whose
reported identity is in the trusted set (known-key policy).

Steps:

1. Write the failing test, `tests/fleet/test_admission.py`:

```python
"""Tests for self-enrollment and admission."""
from __future__ import annotations

import pytest

from skcapstone.fleet import admission, sknoded, store


@pytest.fixture(autouse=True)
def _fixed_capacity(monkeypatch):
    monkeypatch.setattr("skcapstone.fleet.sknoded.node_capacity",
                        lambda: {"cores": 4, "ram_gb": 8.0, "disk_gb": 50.0,
                                 "gpu": None, "vram_gb": None})


def test_join_then_admit_with_preset(paths, operator) -> None:
    sknoded.run_once(paths, "node-41")
    assert [j["name"] for j in admission.pending_joins(paths)] == ["node-41"]
    spec = admission.admit(paths, "node-41", writer=operator, preset=True)
    assert spec["labels"] == {"heavy-build": "true"}
    assert spec["generation"] == 1
    assert admission.pending_joins(paths) == []          # no longer pending
    # next sknoded pass observes its admission
    assert sknoded.run_once(paths, "node-41")["node"] is True
    assert store.read_node_file(paths, "node-41", "node.json")["observedGeneration"] == 1


def test_admit_requires_join_unless_bootstrap(paths, operator) -> None:
    with pytest.raises(LookupError):
        admission.admit(paths, "node-ghost", writer=operator)
    spec = admission.admit(paths, "node-158", writer=operator,
                           preset=True, bootstrap=True)
    assert spec["labels"]["control-plane"] == "true"


def test_admit_is_idempotent(paths, operator) -> None:
    sknoded.run_once(paths, "node-41")
    first = admission.admit(paths, "node-41", writer=operator, preset=True)
    again = admission.admit(paths, "node-41", writer=operator, preset=True)
    assert again["generation"] == first["generation"] == 1


def test_auto_admit_only_trusted(paths, operator, monkeypatch) -> None:
    monkeypatch.setattr("skcapstone.fleet.store.writer_identity",
                        lambda: "capauth:lumina@skworld.io")
    sknoded.run_once(paths, "node-41")
    assert admission.auto_admit(paths, {"capauth:other@x"}, writer=operator) == []
    admitted = admission.auto_admit(paths, {"capauth:lumina@skworld.io"},
                                    writer=operator)
    assert admitted == ["node-41"]
    assert store.read_spec(paths, "node", "node-41") is not None


def test_presets_cover_the_four_nodes() -> None:
    assert set(admission.PRESETS) == {"node-158", "node-41", "node-100", "node-local"}
    assert admission.PRESETS["node-100"]["taints"][0]["effect"] == "NoSchedule"
    assert admission.PRESETS["node-local"]["taints"][0]["effect"] == "PreferNoSchedule"
```

2. Run to fail. Expected: `ModuleNotFoundError: No module named 'skcapstone.fleet.admission'`

3. Implement `src/skcapstone/fleet/admission.py`:

```python
"""Self-enrollment and admission (spec section 9).

A fresh box self-reports a join request; admission mints its node object.
No hand-authored fleet files anywhere on the path from bare box to
managed fleet.
"""
from __future__ import annotations

from . import store
from .paths import FleetPaths

PRESETS: dict[str, dict] = {
    "node-158": {
        "labels": {"always-on": "true", "dev-primary": "true", "control-plane": "true"},
        "taints": [],
    },
    "node-41": {
        "labels": {"heavy-build": "true"},
        "taints": [],  # travel taint applied by runbook when the box travels
    },
    "node-100": {
        "labels": {"gpu": "true"},
        "taints": [{"key": "dedicated", "value": "model-serving", "effect": "NoSchedule"}],
    },
    "node-local": {
        "labels": {"interactive": "true"},
        "taints": [{"key": "interactive", "value": "true", "effect": "PreferNoSchedule"}],
    },
}


def pending_joins(paths: FleetPaths) -> list[dict]:
    """Join requests that do not yet have a node object, sorted by name."""
    out = []
    if not paths.status.exists():
        return out
    for node_dir in sorted(p for p in paths.status.iterdir() if p.is_dir()):
        join = store.read_node_file(paths, node_dir.name, "join.json")
        if join and store.read_spec(paths, "node", node_dir.name) is None:
            out.append(join)
    return out


def admit(
    paths: FleetPaths,
    node: str,
    *,
    writer: store.Writer,
    labels: dict | None = None,
    taints: list | None = None,
    preset: bool = False,
    bootstrap: bool = False,
) -> dict:
    """Mint the node object for a joiner (idempotent).

    Args:
        preset: pull labels/taints from PRESETS for the known four nodes.
        bootstrap: allow admitting without a join request (first node,
            spec section 9 cold-start step 3).
    Raises:
        LookupError: no join request and bootstrap not set.
    """
    existing = store.read_spec(paths, "node", node)
    if existing is not None:
        return existing
    join = store.read_node_file(paths, node, "join.json")
    if join is None and not bootstrap:
        raise LookupError(f"no join request for {node!r}; is sknoded running there?")
    if preset and node in PRESETS:
        labels = labels if labels is not None else PRESETS[node]["labels"]
        taints = taints if taints is not None else PRESETS[node]["taints"]
    spec = {
        "taints": taints or [],
        "cordoned": False,
        "address": (join or {}).get("addresses", {}),
        "identity": (join or {}).get("identity", ""),
    }
    return store.write_spec(paths, "node", node, spec, writer=writer,
                            labels=labels or {})


def auto_admit(paths: FleetPaths, trusted: set[str], *, writer: store.Writer) -> list[str]:
    """Admit pending joiners whose identity is already trusted (known-key)."""
    admitted = []
    for join in pending_joins(paths):
        identity = join.get("identity", "")
        if identity and identity in trusted:
            admit(paths, join["name"], writer=writer, preset=True)
            admitted.append(join["name"])
    return admitted
```

   Add to `src/skcapstone/fleet/cli.py` (after the sknoded command; also add
   `from . import admission` to the module imports):

```python
@fleet.command("admit")
@click.argument("name")
@click.option("--label", "labels", multiple=True, help="k=v, repeatable.")
@click.option("--preset", is_flag=True, help="Use the known-node preset labels/taints.")
@click.option("--bootstrap", is_flag=True, help="First node: admit without a join request.")
def admit_cmd(name: str, labels: tuple[str, ...], preset: bool, bootstrap: bool) -> None:
    """Admit a joining node, minting its node object."""
    label_map = dict(part.split("=", 1) for part in labels) if labels else None
    try:
        spec = admission.admit(default_paths(), name, writer=_operator(),
                               labels=label_map, preset=preset, bootstrap=bootstrap)
    except LookupError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"admitted {name} (generation {spec['generation']})")
```

   Create `docs/runbooks/fleet-cold-start.md`:

```markdown
# Fleet control-plane cold start (Phase 1)

From a single bare box to a managed fleet, no hand-authored fleet files.
Spec: skos/docs/specs/2026-07-27-skworld-fleet-control-plane-design.md,
section 9.

## First box (control plane, normally .158)

1. Install skcapstone into ~/.skenv (scripts/install.sh). Syncthing shares
   ~/.skcapstone as usual; the fleet tree lives at ~/.skcapstone/fleet/.
   Add `.events.lock` to the share's .stignore (event log lock sidecars).
2. Install and start the node agent:
   cp systemd/sknoded.service ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now sknoded
3. sknoded self-reports and writes a join request (status/<self>/join.json).
4. Admit yourself (first-node special case):
   skfleet admit --bootstrap --preset node-158
5. Verify: skfleet nodes shows node-158 Ready with labels
   always-on, dev-primary, control-plane.

## Every additional box (.41, .100, local)

1. Install skcapstone; let Syncthing sync ~/.skcapstone.
2. Start sknoded (same unit as above). The box appears in skfleet nodes as
   Pending within one sync interval.
3. From any operator seat: skfleet admit --preset node-41   (or node-100,
   node-local). Known-key auto-admit is available for rebuilds of trusted
   boxes via admission.auto_admit.
4. Verify: skfleet nodes shows the node Ready with the preset labels;
   node-100 must report gpu and vram_gb.

Note: the local box stays report-only in Phase 1 (no actuation exists yet
anywhere; sknoded only self-reports).

## Travel taint on .41

When .41 leaves the LAN (tailscale-only), record it on the node object:
skfleet describe node node-41 to view, then re-admit is NOT needed; edit
via cordon for full exclusion, or (Phase 2+) set the taint
travel=true:PreferNoSchedule with skfleet apply. Until preference scoring
lands (Card 2.1b), the taint is advisory and cordon is the operative tool.

## Kill-switch

skfleet freeze --reason "why"    halts all actuation fleet-wide
skfleet unfreeze                 resumes
Self-report and running services are never affected by freeze.

## Order of operations (why it works)

skcapstone daemon + Syncthing, then sknoded self-report, then admission,
then NodeController ticks, then (Phase 2) the scheduler, then (Phase 3+)
controllers become self-hosted as fleet objects themselves.
```

4. Run to pass: full suite `~/.skenv/bin/python -m pytest tests/fleet/ -v`
   (all 11 tasks green), plus a CLI smoke:
   `SKFLEET_ROOT=/tmp/fleet-demo SKFLEET_NODE=node-local ~/.skenv/bin/skfleet sknoded --once`
   `SKFLEET_ROOT=/tmp/fleet-demo ~/.skenv/bin/skfleet admit --bootstrap --preset node-local`
   `SKFLEET_ROOT=/tmp/fleet-demo ~/.skenv/bin/skfleet nodes`
5. Commit: `feat(fleet): self-enrollment + admission + cold-start runbook`

Live rollout (operator steps, after merge; not part of the test cycle):
follow the runbook on .158, then .41 (ssh cbrd21@100.86.156.5), .100, and
the local box; capture the 48h Syncthing churn baseline for the Card 1.2
acceptance (R2 gate) before Phase 3 work begins.

---

## Self-review

Spec coverage (revised Phase 1 cards -> tasks):
- Card 1.1 (store + conventions + events + freeze + identity seam +
  single-node test): Tasks 1, 2, 3, 4, 5, 6. Generation/observedGeneration
  in Tasks 2-3; ownership guards in Tasks 2, 3, 4; events bounded/rotating/
  deduped in Task 4; freeze primitive in Task 5; writer-identity block with
  `signature: None` seam in Task 2 (filled by Card 3.5); single-node-mode
  invariant in Task 6.
- Card 1.2 (sknoded self-report + join request): Tasks 7, 8. Capacity via
  autoscale-as-library with fallback (Task 7), conditions via doctor reuse
  (Task 7), heartbeat/node.json in-place write-on-change plus join request
  and the never-writes-outside-own-subtree test (Task 8). The 48h live churn
  baseline is an operator acceptance step recorded in Task 11's rollout note.
- Card 1.3 (NodeController + nodes/describe/cordon/explain): Tasks 9, 10.
  Ready/NotReady/Dead thresholds 180/300 (Task 9), Pending for joiners
  (Task 9), CLI + explain seam + entry-point wiring (Task 10). The
  conflict-file probe feeding the SyncConflict condition landed in Task 7.
- Card 1.4 (self-enrollment + admission + cold-start runbook, all four
  nodes): Task 11 (admit/preset/bootstrap/auto-admit, admit CLI, runbook
  with .158/.41/.100/local enrollment and travel-taint note).

Placeholder scan: no TODOs, no "add appropriate X", no elided bodies; every
test and implementation block above is complete runnable code. Two soft
points are called out explicitly rather than hidden: the `skcapstone`
version attribute check in Task 8 step 3, and the identical plain/`--json`
explain output in Task 10 (documented as intentional).

Type consistency: `FleetPaths` (Task 1) is the first parameter everywhere.
`Writer(role, node, identity)` defined in Task 2 is used unchanged by Tasks
3-11. `store.write_spec(paths, kind, name, spec, *, writer, labels)` is
called with that exact shape in Tasks 6, 9 (cordon), and 11 (admit).
`write_node_file(paths, writer, filename, payload, *, if_changed)` is used
by Tasks 6, 8, 9, 11. `node_capacity()` (Task 7) is monkeypatched at
`skcapstone.fleet.sknoded.node_capacity` in Tasks 8, 10, 11, which matches
the `from .capacity import node_capacity` import style in Task 8.
`run_once` returns `{"heartbeat", "node", "join"}` bools, asserted
identically in Tasks 8 and 11. Condition dicts use the section 4 shape in
Tasks 3, 7, 9, 10.

Deviation from the assessment: none in substance. One path note: the spec
says new code in `skcapstone/fleet/`; in this repo's src layout that is
`src/skcapstone/fleet/` on disk (import path `skcapstone.fleet`), and the
events lock sidecar (`.events.lock`) must be added to the Syncthing ignore
patterns (runbook step 1) so the flock file never syncs.
