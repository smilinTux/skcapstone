"""OperatorappController (R1.4 step 2): read-time Operatorapp rows.

Runs on the control-plane node, mirroring ConfigController's read-time
conventions. Read-time only: never writes spec (operator-owned) and never
writes status. This is the registration/ratification audit surface: which apps
are registered, and which have proposed standard actions still awaiting a human's
ratification.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import store
from .operatorapp import (
    OperatorappSpecError,
    normalize_operatorapp_spec,
    operatorapp_conditions,
)
from .paths import FleetPaths


@dataclass(frozen=True)
class OperatorappRow:
    """One row of skfleet get operatorapps (read-time derivation, nothing persisted)."""

    name: str
    cli: str | None
    repos: tuple[str, ...]
    proposed_count: int
    ratified_count: int
    proposals_ratified: bool


def operatorapp_rows(paths: FleetPaths, now_iso: str) -> list[OperatorappRow]:
    """All Operatorapps with proposal/ratification counts and condition derivation."""
    rows: list[OperatorappRow] = []
    for payload in store.list_specs(paths, "operatorapp"):
        name = payload["name"]
        if payload.get("spec", {}).get("deleted"):
            continue
        try:
            spec = normalize_operatorapp_spec(payload.get("spec", {}))
        except OperatorappSpecError:
            continue
        conds = {
            c["type"]: c["status"] for c in operatorapp_conditions(spec, {}, now_iso)
        }
        rows.append(
            OperatorappRow(
                name=name,
                cli=spec["cli"],
                repos=tuple(spec["repos"]),
                proposed_count=len(spec["proposedStandardActions"]),
                ratified_count=len(spec["ratifiedStandardActions"]),
                proposals_ratified=conds.get("ProposalsRatified") == "True",
            )
        )
    return rows
