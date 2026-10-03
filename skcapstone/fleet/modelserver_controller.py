"""ModelController (Phase 6 step 2): read-time ModelServer rows.

Runs on the control-plane node, mirroring CronController's read-time
conventions. Read-time only: never writes status (sknoded-owned) and never
edits spec (operator-owned). No gateway wiring here (that is 6.2).
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .modelserver import ModelServerSpecError, modelserver_conditions, normalize_modelserver_spec
from .paths import FleetPaths


@dataclass(frozen=True)
class ModelServerRow:
    """One row of skfleet get modelservers (read-time derivation, nothing persisted)."""

    name: str
    node: str | None
    ports: list[int]
    serving: bool
    vram: float | None


def _status_for(merged: dict, target: str | None) -> dict | None:
    for st in merged.get("statuses", []):
        if target is None or st.get("node") == target:
            return st
    return None


def _serving(conditions: list[dict]) -> bool:
    for cond in conditions:
        if cond.get("type") == "Serving":
            return cond.get("status") == "True"
    return False


def modelserver_rows(paths: FleetPaths, now_iso: str) -> list[ModelServerRow]:
    """All ModelServers with observed ports/models/vram and Serving derivation.

    The observed dict (open_ports/loaded_models/vram_gb) comes from the
    status file this ModelServer's node has reported; a ModelServer with
    no observed status yet reads as not-serving with unknown vram.
    """
    rows: list[ModelServerRow] = []
    for payload in store.list_specs(paths, "modelserver"):
        name = payload["name"]
        if payload.get("spec", {}).get("deleted"):
            continue
        try:
            spec = normalize_modelserver_spec(payload.get("spec", {}))
        except ModelServerSpecError:
            continue
        target = spec["node"]
        merged = store.merged(paths, "modelserver", name) or {}
        status = _status_for(merged, target)
        observed = (status or {}).get("status", {})
        conditions = modelserver_conditions(spec, observed, now_iso)
        rows.append(
            ModelServerRow(
                name=name,
                node=target,
                ports=spec["ports"],
                serving=_serving(conditions),
                vram=observed.get("vram_gb"),
            )
        )
    return rows
