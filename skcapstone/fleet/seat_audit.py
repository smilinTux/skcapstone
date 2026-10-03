"""Detect a second operator seat writing to the fleet store (drill gap G2).

The fleet store is a Syncthing folder shared by every node, and the control
plane assumes exactly ONE operator seat writes specs at a time. During a
promotion that assumption is briefly false by construction, which is why the
promotion runbook has a two-seat window at all.

The existing detector is the Syncthing conflict file, and the drill (card
``4c32df6f``) measured what it actually catches:

===========================================  ==================  ==============
scenario                                     writes              conflict files
===========================================  ==================  ==============
two seats writing inside one sync interval   2                   1
two seats with a sync between the writes     10                  **0**
===========================================  ==================  ==============

So it is a COLLISION detector, not a PRESENCE detector. The interleaved case is
the likely one, since a 368K folder converges in seconds while the operator
timer runs every 15 minutes, and the runbook's own advice to "wait one full
timer cycle" names precisely the interval that guarantees no collision is
raised. A quiet conflict directory is therefore not evidence of a single
writer.

This module reads the ``writer`` block that every spec already carries and
reports how many distinct seats are represented. It catches the interleaved
case that ``find`` cannot.

**Its limit, stated plainly because it decides how much the result is worth.**
This is a CURRENT-STATE audit. ``store.write_spec`` emits no event, so there is
no write history anywhere in the tree: a second seat that wrote an object and
was later overwritten by the first leaves nothing at all behind. A clean audit
means "no second seat is represented in the objects as they stand now", not
"no second seat has been writing". Closing that gap needs an event on
``write_spec`` (card ``27aa2d4d``), not a better reader.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import store
from .paths import FleetPaths

#: Roles whose writes represent an operator seat.
#:
#: ``store.write_spec`` already refuses any role but ``operator``, so this
#: filter is defence in depth rather than the primary control: a non-operator
#: writer block can still ARRIVE without being created here, over Syncthing
#: from a node running older code or by a hand edit. Counting those as seats
#: would report a normal fleet as a two-seat emergency, and a signal that
#: cries wolf stops being read.
SEAT_ROLES = frozenset({"operator"})


@dataclass(frozen=True)
class SeatAudit:
    """Which nodes hold operator-seat writes in the store right now."""

    #: node name -> object references ("kind/name") it wrote, sorted.
    by_node: dict[str, list[str]] = field(default_factory=dict)
    #: Objects carrying no writer block at all (pre-provenance, or hand-made).
    unattributed: list[str] = field(default_factory=list)

    @property
    def seats(self) -> list[str]:
        """Distinct operator-seat nodes, sorted."""
        return sorted(self.by_node)

    @property
    def ok(self) -> bool:
        """True when at most one operator seat is represented."""
        return len(self.by_node) <= 1

    def summary(self) -> str:
        if not self.by_node:
            return "no operator-seat writes found"
        if self.ok:
            node = self.seats[0]
            return f"one operator seat: {node} ({len(self.by_node[node])} object(s))"
        parts = ", ".join(f"{n} ({len(self.by_node[n])})" for n in self.seats)
        return f"{len(self.by_node)} operator seats represented: {parts}"


def _kinds_present(paths: FleetPaths) -> list[str]:
    """Object kinds that exist in this tree, sorted. Empty when there are none."""
    if not paths.objects.exists():
        return []
    return sorted(p.name for p in paths.objects.iterdir() if p.is_dir())


def audit_seats(paths: FleetPaths, *, kinds: tuple[str, ...] | None = None) -> SeatAudit:
    """Group every spec in the store by the operator seat that wrote it.

    Read-only. Conflict copies are skipped by ``store.list_specs`` itself, so
    a ``.sync-conflict-`` sibling cannot inflate the seat count: this audit
    reports on the objects the fleet actually obeys.

    Args:
        kinds: restrict to these object kinds. Defaults to every kind present.
    """
    by_node: dict[str, list[str]] = {}
    unattributed: list[str] = []

    for kind in sorted(kinds if kinds is not None else _kinds_present(paths)):
        for payload in store.list_specs(paths, kind):
            ref = f"{kind}/{payload.get('name', '?')}"
            writer = payload.get("writer") or {}
            role = writer.get("role", "")
            node = writer.get("node", "")
            if not writer or not node:
                unattributed.append(ref)
                continue
            if role not in SEAT_ROLES:
                continue
            by_node.setdefault(node, []).append(ref)

    return SeatAudit(
        by_node={n: sorted(refs) for n, refs in sorted(by_node.items())},
        unattributed=sorted(unattributed),
    )
