"""Register the app adapters as fleet Operatorapp objects (R1.5).

Each app adapter describes itself via explain (conditions + actions). This module
turns that self-description into an Operatorapp spec and writes it to the fleet,
so every subapp Atlas operates is a first-class, listable, ratifiable object.

The registration writer is the autonomous operator seat (agent_seat=True): it may
create and refresh an Operatorapp but the store's human-only field guard blocks it
from ever writing ratifiedStandardActions. So registration PROPOSES standard
actions (derived from each adapter's standard+reversible actions); a human
ratifies which of them run auto-standard (see `skoperator apps ratify`). A refresh
preserves any existing human ratifications rather than blanking them.
"""

from __future__ import annotations

from typing import Callable

from ..fleet import operatorapp, store
from . import (
    skchat_adapter,
    skcode_adapter,
    skcomms_adapter,
    skdashboard_adapter,
    skgateway_adapter,
    skmemory_adapter,
    skos_adapter,
)

#: Per-app registration metadata: the explain fn plus the app's operator CLI and
#: home repos. The fleet itself is NOT here: it is the reference the apps plug
#: into, not an Operatorapp.
APP_REGISTRY: dict[str, dict] = {
    "skchat": {
        "explain": skchat_adapter.skchat_explain,
        "cli": "skchat operator",
        "repos": ["skchat"],
    },
    "skcode": {
        "explain": skcode_adapter.skcode_explain,
        "cli": "skcode-hostd operator",
        "repos": ["skharness"],
    },
    "skcomms": {
        "explain": skcomms_adapter.skcomms_explain,
        "cli": "skcomms operator",
        "repos": ["skcomms"],
    },
    "skdashboard": {
        "explain": skdashboard_adapter.skdashboard_explain,
        "cli": "skcapstone dashboard operator",
        "repos": ["skcapstone"],
    },
    "skgateway": {
        "explain": skgateway_adapter.skgateway_explain,
        "cli": "skgateway operator",
        "repos": ["skgateway"],
    },
    "skmemory": {
        "explain": skmemory_adapter.skmemory_explain,
        "cli": "skmemory operator",
        "repos": ["skmemory"],
    },
    "skos": {
        "explain": skos_adapter.skos_explain,
        "cli": "skos operator",
        "repos": ["skos"],
    },
}


def derive_operatorapp_spec(
    name: str,
    explain_payload: dict,
    *,
    cli: str | None = None,
    repos: list[str] | None = None,
) -> dict:
    """Derive a normalized Operatorapp spec from an adapter's explain output.

    proposedStandardActions is the set of actions the adapter declares BOTH
    standard and reversible (the ones eligible to run auto-standard once a human
    ratifies them). conditions is the adapter's condition names. This never sets
    ratifiedStandardActions: that is the human's field.
    """
    actions = explain_payload.get("actions", [])
    proposed = [a["name"] for a in actions if a.get("standard") and a.get("reversible")]
    return operatorapp.normalize_operatorapp_spec(
        {
            "name": name,
            "cli": cli,
            "repos": list(repos or []),
            "proposedStandardActions": proposed,
            "conditions": list(explain_payload.get("conditions", [])),
        }
    )


def _write_preserving_ratifications(paths, name: str, spec: dict, *, writer: store.Writer) -> None:
    """Write an Operatorapp spec, preserving any existing human ratifications.

    A refresh must never blank a human's ratifiedStandardActions (the store's
    human-only field guard would also reject the seat writing them), so the prior
    ratified list is carried over onto the fresh spec before the write.
    """
    existing = store.read_spec(paths, "operatorapp", name)
    prior = ((existing or {}).get("spec") or {}).get("ratifiedStandardActions", [])
    spec = dict(spec)
    spec["ratifiedStandardActions"] = list(prior)
    store.write_spec(paths, "operatorapp", name, spec, writer=writer)


def register_all(
    paths,
    *,
    writer: store.Writer,
    registry: dict[str, dict] | None = None,
    discovered: list[dict] | None = None,
) -> list[str]:
    """Write or refresh an Operatorapp object for every registered app adapter.

    Preserves any existing ratifiedStandardActions on a refresh (so re-registering
    never blanks a human's ratifications, and the store guard passes). Returns the
    names written, sorted.

    ``discovered`` is the optional set of pre-normalized Operatorapp specs from
    manifest-driven discovery (OPS0.3): each is registered ALONGSIDE the built-ins,
    but a discovered spec whose name matches a built-in id is skipped so the
    built-in adapter always keeps precedence (a manifest never overrides a
    built-in). With ``discovered`` absent (the default) this is byte-identical to
    the built-in-only registration.
    """
    registry = registry if registry is not None else APP_REGISTRY
    written: list[str] = []
    for name in sorted(registry):
        meta = registry[name]
        explain_fn: Callable[[], dict] = meta["explain"]
        spec = derive_operatorapp_spec(
            name, explain_fn(), cli=meta.get("cli"), repos=meta.get("repos")
        )
        _write_preserving_ratifications(paths, name, spec, writer=writer)
        written.append(name)
    for spec in discovered or []:
        name = spec.get("name")
        if not name or name in registry:
            continue  # a discovered id matching a built-in never overrides it
        _write_preserving_ratifications(paths, name, spec, writer=writer)
        written.append(name)
    return sorted(written)


def ratify(paths, app: str, action: str, *, writer: store.Writer) -> dict:
    """Human-ratify one proposed standard action for an app (adds it to the
    ratified list). The writer must be a human (agent_seat False), else the store
    guard rejects the write.

    Raises:
        ValueError: the app is not registered, or the action is not one of its
            proposed standard actions.
    """
    existing = store.read_spec(paths, "operatorapp", app)
    if existing is None:
        raise ValueError(f"unknown operatorapp {app!r} (register it first)")
    spec = operatorapp.normalize_operatorapp_spec(existing.get("spec", {}))
    if action not in spec["proposedStandardActions"]:
        raise ValueError(
            f"{action!r} is not a proposed standard action of {app!r} "
            f"(proposed: {spec['proposedStandardActions']})"
        )
    ratified = list(spec["ratifiedStandardActions"])
    if action not in ratified:
        ratified.append(action)
    spec["ratifiedStandardActions"] = ratified
    return store.write_spec(paths, "operatorapp", app, spec, writer=writer)


__all__ = [
    "APP_REGISTRY",
    "derive_operatorapp_spec",
    "register_all",
    "ratify",
]
