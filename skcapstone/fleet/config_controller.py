"""ConfigController (Phase 7 step 2): read-time Config rows.

Runs on the control-plane node, mirroring AgentController's read-time
conventions. Read-time only: never writes status (sknoded-owned) and never
edits spec (operator-owned). No secret distribution here: this is the audit
surface only (presence/drift/rotation-age), never secret material.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .config_object import ConfigSpecError, config_conditions, normalize_config_spec
from .paths import FleetPaths


@dataclass(frozen=True)
class ConfigRow:
    """One row of skfleet get configs (read-time derivation, nothing persisted)."""

    name: str
    node: str | None
    secrets_present: bool
    drift: bool
    rotation_overdue: bool


def _first_status(merged: dict) -> dict | None:
    statuses = merged.get("statuses", [])
    return statuses[0] if statuses else None


def config_rows(paths: FleetPaths, now_iso: str) -> list[ConfigRow]:
    """All Configs with observed presence/drift/age and condition derivation.

    The observed dict (present_secrets/file_hashes/oldest_secret_age_days)
    comes from status.observed on whichever node has reported for this
    Config; a Config with no observed status yet reads as its conditions
    dictate from an empty observed dict (secrets required but absent,
    files required but unhashed, never rotation-overdue).
    """
    rows: list[ConfigRow] = []
    for payload in store.list_specs(paths, "config"):
        name = payload["name"]
        if payload.get("spec", {}).get("deleted"):
            continue
        try:
            spec = normalize_config_spec(payload.get("spec", {}))
        except ConfigSpecError:
            continue
        merged = store.merged(paths, "config", name) or {}
        status = _first_status(merged)
        observed = ((status or {}).get("status") or {}).get("observed") or {}
        conds = {c["type"]: c["status"] for c in config_conditions(spec, observed, now_iso)}
        rows.append(
            ConfigRow(
                name=name,
                node=(status or {}).get("node"),
                secrets_present=conds.get("SecretPresent") == "True",
                drift=conds.get("ConfigDrift") == "True",
                rotation_overdue=conds.get("RotationOverdue") == "True",
            )
        )
    return rows
