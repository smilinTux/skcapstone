"""The registry delta as an operator surface for ``skcapstone atlas eyes``.

:mod:`skcapstone.atlas.reconcile` computes the disagreement between the three
registries (declared, desired, actual). This module turns that report into the
few lines an operator reads at a glance, and into the JSON block ``atlas eyes
--json`` carries alongside the lane assessment.

The render leads with what is wrong. Full agreement is one line; a thousand
units in agreement is still one line. What gets space is the drift, ordered by
how much it should worry the reader:

1. Referenced-only objects: Service objects that name a unit but declare no
   exec. The fleet chain cannot create these, so when the unit is missing the
   store wants it forever with no way to act.
2. Desired but absent on this node, by object kind.
3. Desired under a runtime this pass cannot observe.
4. Placements with no object behind them. A workload kind whose object is gone
   is real drift; ``placements/job/`` is an advisory routing namespace that
   never had objects and is provenance, not junk. Neither has a unit, and the
   surface says so instead of inventing one (card 0e0a0600).
5. Running with no fleet object behind it, failed units first.
6. Registries that could not be read at all.

Why this is legal for Atlas
---------------------------
Same answer as the reconciler: Atlas is frozen and gains no authority from
this. A rendered delta is a description of the estate. Nothing here writes,
actuates, or reads the coordination board, and ``tests/test_atlas_reconcile.py``
and ``tests/test_atlas_delta.py`` hold this module to that in code rather than
in prose.
"""

from __future__ import annotations

from dataclasses import asdict

from ..fleet.paths import FleetPaths, self_node_name
from . import reconcile as rc

#: How many names a single finding lists inline before deferring to ``--json``.
#: Enough that every orphan on this estate is named, few enough that the
#: sixty-odd hand-installed units do not become a wall: a wall of names is how a
#: surface stops being read.
INLINE_LIMIT = 24


def observe(
    paths: FleetPaths,
    node: str | None = None,
    **readers,
) -> rc.ReconcileReport:
    """Run the three-registry reconciler for one node, observation only.

    Args:
        paths: The fleet tree to read. Never created if absent.
        node: Node to reconcile. Defaults to this machine's fleet node name.
        **readers: Passed through to :func:`reconcile.reconcile` so a test can
            inject readers and never touch a live registry.

    Returns:
        The :class:`reconcile.ReconcileReport` for the node.
    """
    return rc.reconcile(paths, node or self_node_name(), **readers)


def to_dict(report: rc.ReconcileReport) -> dict:
    """The report as plain JSON-ready data, for ``atlas eyes --json``."""
    return {
        "schema": "skatlas.delta/v1",
        "node": report.node,
        "in_agreement": report.in_agreement,
        "summary": report.summary(),
        "counts": {
            "declared": len(report.declared),
            "desired": len(report.desired),
            "actual": len(report.actual),
            "placements_without_object": len(report.placements_without_object()),
            "referenced_only": len(report.referenced_only()),
        },
        "deltas": [asdict(d) for d in report.deltas],
        "placements_without_object": [asdict(d) for d in report.placements_without_object()],
        "referenced_only": [asdict(d) for d in report.referenced_only()],
        "declared": [asdict(d) for d in report.declared],
        "desired": [asdict(d) for d in report.desired],
        "actual": [asdict(d) for d in report.actual],
        "notes": list(report.notes),
    }


def _names(items: list[str], limit: int = INLINE_LIMIT) -> str:
    """Join names inline, deferring the tail to ``--json`` past the limit."""
    if len(items) <= limit:
        return " ".join(items)
    return " ".join(items[:limit]) + f" (+{len(items) - limit} more, see --json)"


def _plural(n: int, noun: str, plural: str | None = None) -> str:
    return f"{n} {noun if n == 1 else (plural or noun + 's')}"


def _placements_without_object(report: rc.ReconcileReport, lines: list[str]) -> None:
    rows = report.placements_without_object()
    if not rows:
        return
    by_kind: dict[str, list[rc.PlacementItem]] = {}
    for item in rows:
        by_kind.setdefault(item.kind, []).append(item)
    for kind, items in sorted(by_kind.items()):
        by_node: dict[str, int] = {}
        for item in items:
            by_node[item.node or "unplaced"] = by_node.get(item.node or "unplaced", 0) + 1
        where = ", ".join(f"{n} -> {node}" for node, n in sorted(by_node.items()))
        if items[0].workload_kind:
            # The object was deleted and its placement was not. Real drift.
            lines.append(
                f" !  {_plural(len(items), kind + ' placement')} with no object: "
                f"objects/{kind}/<name>.json is gone ({where})"
            )
        else:
            # Not an object kind at all: advisory routing, kept as provenance.
            lines.append(
                f" ~  {_plural(len(items), kind + ' placement')} with no object: "
                f"{kind!r} is not a fleet object kind, advisory routing only ({where})"
            )
        lines.append("    no unit is derived for these and no claim is made about systemd")
        lines.append(f"    {_names(sorted(i.name for i in items))}")


def _referenced_only(report: rc.ReconcileReport, lines: list[str]) -> None:
    refs = report.referenced_only()
    if not refs:
        return
    by_kind: dict[str, list[rc.DesiredItem]] = {}
    for item in refs:
        by_kind.setdefault(item.kind, []).append(item)
    actual_units = {a.unit for a in report.actual}
    for kind, items in sorted(by_kind.items()):
        if kind == "cronjob":
            # A CronJob's command is its declaration; a CronJob without one is
            # malformed rather than a reference, and rare enough to list plainly.
            lines.append(
                f" !! {_plural(len(items), kind + ' object')} declare no command: "
                f"{_names(sorted(i.name for i in items))}"
            )
            continue
        lines.append(
            f" !! {_plural(len(items), kind + ' object')} reference a unit but declare no "
            f"exec ({'/'.join(rc.EXEC_KEYS)}):"
        )
        lines.append("    the fleet chain cannot create any of these; something outside it must")
        observable = [i for i in items if i.observable]
        absent = sorted(
            (i for i in observable if i.unit not in actual_units), key=lambda i: i.name
        )
        for item in absent:
            where = "unplaced" if not item.node else item.node
            lines.append(f"    {item.name} -> {item.unit}  not on this node ({where})")
        present = sorted(i.name for i in observable if i.unit in actual_units)
        if present:
            lines.append(f"    unit present for: {_names(present)}")
        unobservable = sorted(i.name for i in items if not i.observable)
        if unobservable:
            lines.append(f"    not observable via systemd: {_names(unobservable)}")


def _desired_not_actual(report: rc.ReconcileReport, lines: list[str]) -> None:
    # Referenced-only objects already had their own section.
    rows = [d for d in report.by_kind(rc.DESIRED_NOT_ACTUAL) if d.declares_exec]
    if not rows:
        return
    by_kind: dict[str, list[str]] = {}
    for d in rows:
        by_kind.setdefault(d.object_kind, []).append(d.unit)
    for kind, units in sorted(by_kind.items()):
        lines.append(f" !  {_plural(len(units), kind)} desired, no unit on this node:")
        lines.append(f"    {_names(sorted(units))}")


def _unobservable(report: rc.ReconcileReport, lines: list[str]) -> None:
    rows = report.by_kind(rc.DESIRED_UNOBSERVABLE)
    if not rows:
        return
    lines.append(
        f" ?  {_plural(len(rows), 'desired object')} under a runtime this pass cannot "
        f"observe: {_names(sorted(f'{d.name} ({d.unit})' for d in rows))}"
    )


def _actual_not_desired(report: rc.ReconcileReport, lines: list[str]) -> None:
    rows = report.by_kind(rc.ACTUAL_NOT_DESIRED)
    if not rows:
        return
    failed = sorted(d.unit for d in rows if d.state == "failed")
    rest = sorted(d.unit for d in rows if d.state != "failed")
    lines.append(f" -  {_plural(len(rows), 'unit')} on this node that no fleet object asks for")
    if failed:
        lines.append(f"    FAILED: {_names(failed)}")
    if rest:
        lines.append(f"    {_names(rest)}")


def render(report: rc.ReconcileReport) -> str:
    """Render the delta for a human, worst finding first.

    Args:
        report: The reconciler's output for one node.

    Returns:
        A short block of text. One line when the registries agree.
    """
    counts = (
        f"declared {len(report.declared)} / desired {len(report.desired)} / "
        f"actual {len(report.actual)}"
    )
    lines = [f"REGISTRY DELTA  {report.node}  ({counts})"]
    if report.in_agreement and not report.notes:
        lines.append(" ok  the three registries agree on this node")
        return "\n".join(lines)

    _referenced_only(report, lines)
    _desired_not_actual(report, lines)
    _unobservable(report, lines)
    _placements_without_object(report, lines)
    _actual_not_desired(report, lines)
    if not report.declared and not any("declared" in n for n in report.notes):
        lines.append(" -  declared registry is empty: no installed skos pack names a fleet object")
    for note in report.notes:
        lines.append(f" ?  {note}")
    if report.in_agreement:
        lines.append(" ok  no delta between the three registries")
    return "\n".join(lines)


__all__ = ["INLINE_LIMIT", "observe", "render", "to_dict"]
