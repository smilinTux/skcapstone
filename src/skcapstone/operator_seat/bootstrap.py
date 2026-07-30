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
from . import kedb_seeds, registration


def bootstrap_operator(paths, *, writer: store.Writer, home) -> dict:
    """Register every app adapter as an Operatorapp and seed the operator KEDB.

    Idempotent and safe-by-default: only registration objects and missing KEDB
    entries are written, and human ratifications are preserved. Never actuates.

    Args:
        paths: The fleet paths (where Operatorapp objects live).
        writer: The autonomous operator seat writer (``agent_seat=True``). The
            store guard blocks it from writing ``ratifiedStandardActions``.
        home: The ITIL home root used to seed the KEDB (``SHARED_ROOT``).

    Returns:
        ``{"registered": [names...], "seeded": [ids...]}`` where ``registered``
        is every app registered/refreshed this run (sorted) and ``seeded`` is
        only the KEDB ids newly created this run (empty once already seeded).
    """
    registered = registration.register_all(paths, writer=writer)
    seeded = kedb_seeds.seed_operator_kedb(home)
    return {"registered": registered, "seeded": seeded}


__all__ = ["bootstrap_operator"]
