"""sknoded v1: the per-node self-report loop (spec section 6, step 1).

Phase 1 is report-only: heartbeat + node.json + join request. Actuation
arrives in Phase 3 and will gate on store.actuation_allowed().
"""

from __future__ import annotations

import platform
import socket
import time
from copy import deepcopy
from datetime import datetime, timezone

from .. import __version__ as skcapstone_version
from . import nodeinventory, store
from .capacity import allocatable, node_capacity
from .conditions import merge_transitions, node_conditions, probe_conditions
from .paths import FleetPaths

HEARTBEAT_INTERVAL_S = 60

#: How often the node re-observes its installed units and packages.
#: Deliberately much slower than the 60s heartbeat. An install profile moves
#: on deploy timescales (minutes to days), never per second, so re-reading it
#: every heartbeat would buy no freshness while paying 1440 systemctl execs a
#: day and putting a jittery value on the write-on-change path. 15 minutes
#: bounds drift-detection latency far below any human reaction time.
INVENTORY_INTERVAL_S = 900

#: Hard caps on what gets published. node.json rides the control-bus
#: Syncthing folder (10MB for the whole folder, every node's file in it), so
#: the inventory has to be bounded by construction and not by hope. Real
#: nodes sit at 18 to 80 enabled user units and 8 to 25 SK packages, so these
#: caps are roughly 5x headroom: they should never fire, and if they do, the
#: node is the anomaly and the marker says so.
MAX_INVENTORY_UNITS = 400
MAX_INVENTORY_PACKAGES = 200

#: Cached publishable inventory plus the monotonic clock reading it was taken
#: at. Process-global because sknoded is a single long-lived loop; a restart
#: simply re-observes, which is the correct behaviour after an upgrade.
_inventory_cache: dict | None = None
_inventory_cached_at: float = 0.0


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def reset_inventory_cache() -> None:
    """Drop the cached inventory so the next report re-observes the node."""
    global _inventory_cache, _inventory_cached_at
    _inventory_cache = None
    _inventory_cached_at = 0.0


def _collect_inventory() -> dict:
    """Observe this node. Seam for tests, so no test shells out to systemd."""
    return nodeinventory.collect()


def _cap_entries(entries: dict[str, str], limit: int) -> tuple[dict[str, str], dict | None]:
    """Keep at most `limit` entries, lowest name first.

    Sorting before the cut is what makes truncation stable: the same node
    with the same units keeps the same subset every pass, so a capped
    inventory still satisfies the write-on-change contract.

    Args:
        entries: {name: value} to bound.
        limit: Maximum number of entries to keep.

    Returns:
        (kept entries, truncation marker or None when nothing was dropped).
    """
    ordered = dict(sorted(entries.items()))
    if len(ordered) <= limit:
        return ordered, None
    kept = dict(list(ordered.items())[:limit])
    return kept, {"kept": limit, "total": len(ordered)}


def publishable_inventory(inventory: dict) -> dict:
    """Turn a raw nodeinventory.collect() result into the published block.

    Two things happen here, both load-bearing. The collection timestamp is
    stripped via nodeinventory.body() so an unchanged node produces a byte
    identical block and store.write_node_file() skips the write. And the
    entry counts are capped, with a `truncated` marker that is ALWAYS
    present, so a consumer can never read a partial inventory as a complete
    one by forgetting to check for an optional key. Empty marker means
    complete.

    Args:
        inventory: The result of nodeinventory.collect().

    Returns:
        ``{"units": {scope: {...}}, "packages": {...}, "truncated": {...}}``,
        carrying unit NAMES and package name plus version only. Unit file
        contents are never published: they are unbounded, and the drift diff
        does not read them.
    """
    body = nodeinventory.body(inventory or {})
    truncated: dict[str, dict] = {}
    units: dict[str, dict[str, str]] = {}
    for scope, entries in sorted((body.get("units") or {}).items()):
        kept, marker = _cap_entries(dict(entries or {}), MAX_INVENTORY_UNITS)
        units[scope] = kept
        if marker is not None:
            truncated[f"units.{scope}"] = marker
    packages, marker = _cap_entries(dict(body.get("packages") or {}), MAX_INVENTORY_PACKAGES)
    if marker is not None:
        truncated["packages"] = marker
    return {"units": units, "packages": packages, "truncated": truncated}


def node_inventory(*, now: float | None = None) -> dict:
    """The published inventory block, re-observed at most every 15 minutes.

    Args:
        now: Monotonic clock override for tests.

    Returns:
        A fresh copy of the publishable inventory (see publishable_inventory).
        A copy, not the cached object: a caller that mutated what it got back
        would silently corrupt every later report from the same process.
    """
    global _inventory_cache, _inventory_cached_at
    clock = time.monotonic() if now is None else now
    if _inventory_cache is None or clock - _inventory_cached_at >= INVENTORY_INTERVAL_S:
        _inventory_cache = publishable_inventory(_collect_inventory())
        _inventory_cached_at = clock
    return deepcopy(_inventory_cache)


def build_heartbeat(node: str, now_iso: str) -> dict:
    """The one small heartbeat file, overwritten in place (R2)."""
    return {"kind": "Node", "name": node, "node": node, "ts": now_iso}


def build_node_report(paths: FleetPaths, node: str, now_iso: str) -> dict:
    """Capacity + conditions + versions, with stable lastTransition."""
    cap = node_capacity()
    spec = store.read_spec(paths, "node", node)
    conds = node_conditions(cap, paths.root, now_iso)
    probes = (spec or {}).get("spec", {}).get("healthProbes", [])
    conds.extend(probe_conditions(probes, now_iso))
    previous = store.read_node_file(paths, node, "node.json") or {}
    conds = merge_transitions(conds, previous.get("conditions", []))
    return {
        "kind": "Node",
        "name": node,
        "node": node,
        "observedGeneration": int(spec["generation"]) if spec else 0,
        "status": {
            "capacity": cap,
            "allocatable": allocatable(cap),
            "versions": {
                "python": platform.python_version(),
                "skcapstone": skcapstone_version,
            },
            # What `skfleet node doctor --all` reads from the control node, so
            # fleet-wide drift needs the files-as-API store and not ssh.
            "inventory": node_inventory(),
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
    actuation_interval: int | None = None,
) -> None:
    """The daemon loop behind sknoded.service.

    Self-report runs every `interval` seconds; the Phase 3 converge pass
    runs every `actuation_interval` seconds (default 30, spec 3.3). The
    converge pass re-reads the freeze flag and the node's actuate opt-in
    every time, so both are live level-triggered gates.
    """
    from .converge import ACTUATION_INTERVAL_S, converge_once

    act_every = ACTUATION_INTERVAL_S if actuation_interval is None else actuation_interval
    last_report = 0.0
    while True:
        now = time.time()
        if now - last_report >= interval or last_report == 0.0:
            run_once(paths, node)
            last_report = now
        converge_once(paths, node)
        if once:
            return
        time.sleep(act_every)
