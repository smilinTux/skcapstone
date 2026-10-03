"""Self-enrollment and admission (spec section 9).

A fresh box self-reports a join request; admission mints its node object.
No hand-authored fleet files anywhere on the path from bare box to
managed fleet.
"""

from __future__ import annotations

import os

from . import store
from .paths import FleetPaths

PRESETS: dict[str, dict] = {
    # Keyed by the LIVE node name, which paths.self_node_name() derives from
    # the hostname. The control node is `node-noroc2027`, NOT `node-158`:
    # the old LAN-address keys never matched anything, so `admit --preset`
    # silently applied nothing on the control box. Fixed by rekeying to the
    # real names, with ALIASES below preserving the address-style spellings
    # for anyone (or any runbook) still typing them.
    "node-noroc2027": {
        "labels": {"always-on": "true", "dev-primary": "true", "control-plane": "true"},
        "role": "control",
        "taints": [],
    },
    "node-41": {
        "labels": {"heavy-build": "true"},
        "role": "builder-standby",
        # Born untainted. The travel taint is an operator action, not a
        # preset: `skfleet taint node-41 travel=true:NoSchedule` when the
        # laptop leaves, `skfleet untaint node-41 travel` when it is back.
        # See docs/fleet/travel-taint-runbook.md.
        "taints": [],
    },
    "node-ollama": {
        # Keyed by the LIVE name, which is `node-ollama`, NOT `node-100`. The
        # address-style key never matched anything, so `admit --preset` on the
        # GPU box silently applied no labels, no role and no taint. This is the
        # same defect that was fixed for the control node (node-158 ->
        # node-noroc2027) and it survived there because only one of the two
        # dead keys was rekeyed. `node-100` lives on as an alias below.
        # NOT gpu=true on .41: the GPU box in this fleet is .100.
        "labels": {"gpu": "true"},
        "role": "worker-gpu",
        "taints": [{"key": "dedicated", "value": "model-serving", "effect": "NoSchedule"}],
    },
    "node-local": {
        "labels": {"interactive": "true"},
        "role": "",
        "taints": [{"key": "interactive", "value": "true", "effect": "PreferNoSchedule"}],
    },
}

#: Old address-style keys kept working, so a runbook that says
#: `skfleet admit node-158 --preset` still does what it reads like it does.
PRESET_ALIASES: dict[str, str] = {
    "node-158": "node-noroc2027",
    "node-100": "node-ollama",
}


def resolve_preset(node: str) -> dict | None:
    """The preset for a node name, following aliases. None when unknown."""
    return PRESETS.get(PRESET_ALIASES.get(node, node))


class RoleRequiredError(ValueError):
    """A node was admitted with no role while the role gate is on."""


def role_gate_on() -> bool:
    """True when admission must refuse a role-less node.

    Off by default, and deliberately so. Turning it on before every live node
    carries a role would refuse to admit a box that is otherwise healthy, and
    an epic that makes the fleet harder to join has failed at its own job.
    Flip it once the backfill is done (card fdd17a01), the same shadow-then-
    enforce shape used for signing and the profile gate.
    """
    return os.environ.get("SKFLEET_REQUIRE_ROLE", "").strip().lower() in {"1", "true", "on"}


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
    role: str | None = None,
    preset: bool = False,
    bootstrap: bool = False,
) -> dict:
    """Mint the node object for a joiner (idempotent).

    Args:
        role: install profile to bind (epic 3bbf39ea). Explicit value wins
            over the preset; omitted and unpreset means unbound, which is a
            legitimate state the doctor reports as a skip.
        preset: pull labels/taints/role from PRESETS for the known nodes,
            following PRESET_ALIASES.
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
    if preset:
        chosen = resolve_preset(node)
        if chosen is not None:
            labels = labels if labels is not None else chosen["labels"]
            taints = taints if taints is not None else chosen["taints"]
            role = role if role is not None else chosen.get("role", "")
    if not role and role_gate_on():
        raise RoleRequiredError(
            f"{node!r} has no role and SKFLEET_REQUIRE_ROLE is on; pass --role "
            "<profile> or --preset so a fresh box cannot join role-less"
        )
    spec = {
        "taints": taints or [],
        "cordoned": False,
        "address": (join or {}).get("addresses", {}),
        "identity": (join or {}).get("identity", ""),
        "role": role or "",
    }
    return store.write_spec(paths, "node", node, spec, writer=writer, labels=labels or {})


def auto_admit(paths: FleetPaths, trusted: set[str], *, writer: store.Writer) -> list[str]:
    """Admit pending joiners whose identity is already trusted (known-key)."""
    admitted = []
    for join in pending_joins(paths):
        identity = join.get("identity", "")
        if identity and identity in trusted:
            admit(paths, join["name"], writer=writer, preset=True)
            admitted.append(join["name"])
    return admitted
