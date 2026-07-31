"""Idempotent operator startup bootstrap: register app adapters + seed the KEDB.

The natural hook is the start of every ``skoperator run`` tick (unless
``--no-bootstrap``). Its job is to make sure the fleet always reflects reality on
its own: the Operatorapp set is never missing or stale, and every adapter
``kedb_ref`` resolves to a real runbook entry, without anyone remembering to run
``skoperator apps register`` by hand.

Both underlying calls are safe and idempotent by construction, so running this
every tick is cheap and can never clobber:

  * ``registration.register_all`` writes or refreshes one Operatorapp per adapter
    and PRESERVES any existing human ratifications on refresh. The store's
    human-only field guard blocks the seat writer (``agent_seat=True``) from ever
    writing ``ratifiedStandardActions``, so a ratification made between ticks
    survives untouched.
  * ``kedb_seeds.seed_operator_kedb`` is create-or-skip: an existing KEDB entry
    with a given id is left exactly as it is, never duplicated or overwritten.

Bootstrap therefore only ever WRITES Operatorapp registration objects plus any
missing KEDB entries. It never actuates anything: no fleet act verb, no restart,
no purge. It is purely the registration + knowledge-base half of the operator,
which is human-safe by the same guards the manual subcommands rely on.
"""

from __future__ import annotations

from ..fleet import store
from . import discovery, kedb_seeds, registration


def bootstrap_operator(paths, *, writer: store.Writer, home) -> dict:
    """Register every app adapter as an Operatorapp and seed the operator KEDB.

    Idempotent and safe-by-default: only registration objects and missing KEDB
    entries are written, and human ratifications are preserved. Never actuates.

    Manifest-driven discovery (OPS0.3) runs FIRST, gated behind
    ``SKOPERATOR_MANIFEST_DISCOVERY`` (default OFF): when on, verified non-built-in
    capability-pack manifests register ALONGSIDE the built-in seven (a manifest
    never overrides a built-in). When off, ``discovery`` yields nothing and this is
    byte-identical to the built-in-only bootstrap. Discovery only widens what Atlas
    registers/proposes; it introduces no auto-ratification and no actuation.

    Args:
        paths: The fleet paths (where Operatorapp objects live).
        writer: The autonomous operator seat writer (``agent_seat=True``). The
            store guard blocks it from writing ``ratifiedStandardActions``.
        home: The ITIL home root used to seed the KEDB (``SHARED_ROOT``).

    Returns:
        ``{"registered": [names...], "seeded": [ids...], "discovered": [names...]}``
        where ``registered`` is every app registered/refreshed this run (sorted),
        ``seeded`` is only the KEDB ids newly created this run (empty once already
        seeded), and ``discovered`` names the manifest-driven apps registered this
        run (empty when discovery is off).
    """
    discovered_specs = discovery.discover_operatorapp_specs()
    registered = registration.register_all(paths, writer=writer, discovered=discovered_specs)
    seeded = kedb_seeds.seed_operator_kedb(home)
    return {
        "registered": registered,
        "seeded": seeded,
        "discovered": sorted(s["name"] for s in discovered_specs if s.get("name")),
    }


__all__ = ["bootstrap_operator"]
