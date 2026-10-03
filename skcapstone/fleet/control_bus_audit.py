"""Measure the fleet tree and enforce the control-bus scope contract.

The fleet store is being split out of the 19G sovereign Syncthing folder
into a scoped ``skfleet-control`` folder budgeted under 10MB (see
``docs/fleet/control-bus-folder.md``). A budget written in a document is a
sentence; this module is what keeps it honest.

Two things are enforced:

1. A byte budget over the whole tree.
2. A scope contract: the tree carries the five known path classes
   (``objects``, ``placements``, ``status``, ``decisions``, ``atlas``) and
   nothing else. Anything else that appears is named in the report.

A total alone would only tell us the budget broke after it broke, so the
report also names the two things that will actually spend it: the per-node
``events.jsonl`` (bounded per node, unbounded in the number of nodes) and
the ``node.json`` inventory block (unbounded per node). Both are surfaced
with the arithmetic that turns them into a budget failure.

Everything here is read-only. The audit is meant to run on any node,
including the one it is judging, so it must never write into the tree it
measures.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from .events import MAX_BYTES as EVENTS_MAX_BYTES
from .paths import FleetPaths
from .textformat import humanize_bytes

#: The five path classes the control bus is allowed to carry. Ordered as in
#: the design note so report output is stable and reviewable side by side.
KNOWN_CLASSES: tuple[str, ...] = ("objects", "placements", "status", "decisions", "atlas")

#: Default budget: 10MB, the number the design note commits to.
DEFAULT_BUDGET_BYTES = 10 * 1024 * 1024

#: Syncthing's own folder-root bookkeeping. These appear at the root of any
#: shared folder and are created by the transport, not by the fleet, so
#: failing the audit on them would make the gate cry wolf on every managed
#: node forever. They are still named in the report, just not counted as
#: scope violations.
TRANSPORT_MARKERS: frozenset[str] = frozenset({".stfolder", ".stignore", ".stversions"})

#: One live file plus at most one rotation, from events.emit.
EVENTS_CAP_PER_NODE = 2 * EVENTS_MAX_BYTES

_SIZE_UNITS = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3}
_SIZE_RE = re.compile(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMG]?B?)\s*$", re.IGNORECASE)


def parse_size(text: str) -> int:
    """Parse a human byte size such as ``'10MB'``, ``'512KB'`` or ``'4096'``.

    Args:
        text: The size string. A bare number is bytes.

    Returns:
        The size in bytes.

    Raises:
        ValueError: If the string is not a size, or is negative.
    """
    match = _SIZE_RE.match(text)
    if not match:
        raise ValueError(f"not a byte size: {text!r} (try 10MB, 512KB or a raw byte count)")
    number, unit = match.group(1), match.group(2).upper()
    if unit in ("", "B"):
        multiplier = 1
    else:
        multiplier = _SIZE_UNITS[unit if unit.endswith("B") else unit + "B"]
    value = int(float(number) * multiplier)
    if value <= 0:
        raise ValueError(f"budget must be positive, got {text!r}")
    return value


@dataclass(frozen=True)
class FileEntry:
    """One regular file in the fleet tree.

    Attributes:
        rel: Path relative to the fleet root, POSIX separators.
        size: Content bytes, what a transport would carry.
        disk_size: Allocated bytes, what the node actually spends. The fleet
            store is thousands of small JSON files, so a 300 byte spec costs
            a 4KB block and the two numbers diverge by roughly 5x.
    """

    rel: str
    size: int
    disk_size: int

    @property
    def path_class(self) -> str:
        """The top-level component of the path, or ``''`` for a root file."""
        head, sep, _ = self.rel.partition("/")
        return head if sep else ""


@dataclass(frozen=True)
class ClassUsage:
    """Bytes and file count for one top-level path class."""

    name: str
    size: int
    files: int
    known: bool


@dataclass(frozen=True)
class GrowthRisk:
    """A path pattern whose future size, not its current size, is the problem."""

    name: str
    size: int
    files: int
    detail: str


@dataclass(frozen=True)
class AuditReport:
    """The result of one read-only pass over a fleet tree."""

    root: Path
    budget: int
    total_bytes: int
    disk_bytes: int
    file_count: int
    largest: list[FileEntry]
    by_class: list[ClassUsage]
    out_of_scope: list[FileEntry]
    markers: list[str]
    risks: list[GrowthRisk]
    missing_classes: list[str] = field(default_factory=list)

    @property
    def over_budget(self) -> bool:
        """Whether the tree exceeds its budget, measured on disk.

        The budget is charged against allocated bytes, not content bytes.
        That is the number the design note budgets (its 368K is `du`
        output), it is what a joining node actually spends, and it is the
        one that grows when the fleet gains many small spec files.
        """
        return self.disk_bytes > self.budget

    @property
    def ok(self) -> bool:
        """True when the tree is inside budget and inside scope."""
        return not self.over_budget and not self.out_of_scope

    def as_dict(self) -> dict:
        """A JSON-safe view of the report."""
        return {
            "root": str(self.root),
            "budget": self.budget,
            "totalBytes": self.total_bytes,
            "diskBytes": self.disk_bytes,
            "fileCount": self.file_count,
            "overBudget": self.over_budget,
            "ok": self.ok,
            "byClass": [
                {"name": c.name, "bytes": c.size, "files": c.files, "known": c.known}
                for c in self.by_class
            ],
            "largest": [{"path": e.rel, "bytes": e.size} for e in self.largest],
            "outOfScope": [{"path": e.rel, "bytes": e.size} for e in self.out_of_scope],
            "transportMarkers": list(self.markers),
            "missingClasses": list(self.missing_classes),
            "growthRisks": [
                {"name": r.name, "bytes": r.size, "files": r.files, "detail": r.detail}
                for r in self.risks
            ],
        }


def walk(root: Path) -> list[FileEntry]:
    """List every regular file under root, relative to it, sorted by path.

    Symlinks are counted at their own size rather than followed: a symlink
    out of the tree is not part of the folder Syncthing would carry, and
    following one could double-count or loop.

    Args:
        root: The fleet tree root.

    Returns:
        Every regular file found, empty when root does not exist.
    """
    if not root.is_dir():
        return []
    entries: list[FileEntry] = []
    for path in root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        stat = path.stat()
        entries.append(
            FileEntry(
                rel=path.relative_to(root).as_posix(),
                size=stat.st_size,
                disk_size=_allocated(stat),
            )
        )
    return sorted(entries, key=lambda e: e.rel)


def _allocated(stat) -> int:
    """Bytes a stat result actually occupies, rounded up to a block if unknown."""
    blocks = getattr(stat, "st_blocks", None)
    if blocks is not None:
        return blocks * 512
    return -(-stat.st_size // 4096) * 4096


def dir_disk_bytes(root: Path) -> int:
    """Allocated bytes of the directories themselves, root included.

    Directory inodes are a real cost on a small tree (the live fleet store
    is 78 files and its directories are about a sixth of its `du` total),
    so the on-disk figure would not reconcile with `du -sh` without them.
    """
    if not root.is_dir():
        return 0
    total = _allocated(root.stat())
    for path in root.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            total += _allocated(path.stat())
    return total


def _class_usage(entries: list[FileEntry]) -> list[ClassUsage]:
    """Aggregate entries by top-level path class, known classes first.

    A Syncthing marker gets its own bucket rather than falling into the
    root bucket, so a stray root file is never hidden behind ``.stignore``.
    """
    totals: dict[str, list[int]] = {}
    for entry in entries:
        bucket = totals.setdefault(_marker_name(entry.rel) or entry.path_class, [0, 0])
        bucket[0] += entry.size
        bucket[1] += 1
    usage = [
        ClassUsage(name=name, size=totals[name][0], files=totals[name][1], known=True)
        for name in KNOWN_CLASSES
        if name in totals
    ]
    usage += [
        ClassUsage(name=name or "(root)", size=size, files=files, known=False)
        for name, (size, files) in sorted(totals.items())
        if name not in KNOWN_CLASSES
    ]
    return usage


def _marker_name(rel: str) -> str:
    """The Syncthing marker rel belongs to, or ``''`` if it belongs to none.

    Uses the first path component, not ``FileEntry.path_class``, because a
    marker can be a bare root file (``.stignore``) whose path class is the
    empty root bucket.
    """
    head, _, _ = rel.partition("/")
    return head if head in TRANSPORT_MARKERS else ""


def out_of_scope(entries: list[FileEntry]) -> list[FileEntry]:
    """Every file that is not inside one of the five known path classes.

    Args:
        entries: Files from :func:`walk`.

    Returns:
        The violating files, largest first, so the report leads with the
        one that costs the most.
    """
    bad = [e for e in entries if e.path_class not in KNOWN_CLASSES and not _marker_name(e.rel)]
    return sorted(bad, key=lambda e: (-e.size, e.rel))


def growth_risks(root: Path, entries: list[FileEntry], budget: int) -> list[GrowthRisk]:
    """Name the two files that will actually spend the budget.

    Reporting a total only tells us the budget broke after it broke. These
    two grow on their own: ``events.jsonl`` is capped per node but the node
    count is not capped, and the ``node.json`` inventory block grows with
    every unit and package installed on a node.

    Args:
        root: The fleet tree root, used to read node.json inventories.
        entries: Files from :func:`walk`.
        budget: The byte budget, used to state how many nodes break it.

    Returns:
        One risk per pattern, always both, even at zero bytes: a risk that
        disappears from the report when it happens to be empty is a risk
        nobody watches.
    """
    events = [
        e
        for e in entries
        if e.path_class == "status" and Path(e.rel).name.startswith("events.jsonl")
    ]
    nodes_to_blow = max(1, math.ceil(budget / EVENTS_CAP_PER_NODE))
    events_detail = (
        f"capped at {humanize_bytes(EVENTS_CAP_PER_NODE)} per node "
        f"({humanize_bytes(EVENTS_MAX_BYTES)} live plus one rotation), so "
        f"{nodes_to_blow} node(s) at the cap spend the whole "
        f"{humanize_bytes(budget)} budget"
    )

    reports = [e for e in entries if e.path_class == "status" and Path(e.rel).name == "node.json"]
    inventory_bytes = 0
    worst_node = ""
    worst_bytes = 0
    for entry in reports:
        size = _inventory_block_size(root / entry.rel)
        inventory_bytes += size
        if size > worst_bytes:
            worst_bytes, worst_node = size, Path(entry.rel).parent.name
    if worst_bytes:
        share = f"largest is {worst_node} at {humanize_bytes(worst_bytes)}"
    else:
        share = "no inventory published yet"
    inventory_detail = (
        f"the inventory block is unbounded: one entry per enabled unit and per "
        f"SK package on each node, republished on every heartbeat ({share})"
    )

    return [
        GrowthRisk(
            name="status/<node>/events.jsonl",
            size=sum(e.size for e in events),
            files=len(events),
            detail=events_detail,
        ),
        GrowthRisk(
            name="status/<node>/node.json inventory",
            size=inventory_bytes,
            files=len(reports),
            detail=inventory_detail,
        ),
    ]


def _inventory_block_size(path: Path) -> int:
    """Serialized size of a node report's inventory block, 0 if unreadable.

    An unparseable node report is a status-writer problem, not an audit
    failure, so it contributes nothing rather than raising.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0
    inventory = (payload.get("status") or {}).get("inventory")
    if inventory is None:
        return 0
    return len(json.dumps(inventory, sort_keys=True).encode("utf-8"))


def audit(
    paths: FleetPaths,
    *,
    budget: int = DEFAULT_BUDGET_BYTES,
    top: int = 10,
) -> AuditReport:
    """Measure a fleet tree against the control-bus scope contract.

    Args:
        paths: The fleet tree to measure.
        budget: Byte budget for the whole tree.
        top: How many of the largest files to name.

    Returns:
        The report. Read-only: nothing under ``paths.root`` is written.
    """
    entries = walk(paths.root)
    usage = _class_usage(entries)
    present = {c.name for c in usage if c.known}
    return AuditReport(
        root=paths.root,
        budget=budget,
        total_bytes=sum(e.size for e in entries),
        disk_bytes=sum(e.disk_size for e in entries) + dir_disk_bytes(paths.root),
        file_count=len(entries),
        largest=sorted(entries, key=lambda e: (-e.size, e.rel))[:top],
        by_class=usage,
        out_of_scope=out_of_scope(entries),
        markers=sorted({m for e in entries if (m := _marker_name(e.rel))}),
        risks=growth_risks(paths.root, entries, budget),
        missing_classes=[c for c in KNOWN_CLASSES if c not in present],
    )


def render(report: AuditReport) -> str:
    """Render a report as operator-facing text.

    Args:
        report: A report from :func:`audit`.

    Returns:
        The full report body, no trailing newline.
    """
    pct = (report.disk_bytes / report.budget * 100) if report.budget else 0.0
    lines = [
        f"control-bus audit: {report.root}",
        f"{humanize_bytes(report.disk_bytes)} on disk in {report.file_count} file(s), "
        f"budget {humanize_bytes(report.budget)} ({pct:.1f}% used)",
        f"({humanize_bytes(report.total_bytes)} of content: the fleet store is many "
        "small JSON files, so most of the cost is block allocation)",
        "",
        "by path class (content bytes)",
    ]
    if not report.by_class:
        lines.append("  (empty tree)")
    for usage in report.by_class:
        if usage.known:
            tag = ""
        elif usage.name in TRANSPORT_MARKERS:
            tag = "   transport marker"
        else:
            tag = "   OUT OF SCOPE"
        lines.append(
            f"  {usage.name:<14}{humanize_bytes(usage.size):>10}  {usage.files:>4} file(s){tag}"
        )
    if report.missing_classes:
        lines.append(f"  (absent: {', '.join(report.missing_classes)})")

    lines += ["", f"largest {len(report.largest)} file(s)"]
    for entry in report.largest:
        lines.append(f"  {humanize_bytes(entry.size):>10}  {entry.rel}")
    if not report.largest:
        lines.append("  (none)")

    lines += ["", "growth risks"]
    for risk in report.risks:
        lines.append(f"  {risk.name}: {humanize_bytes(risk.size)} now across {risk.files} file(s)")
        lines.append(f"    {risk.detail}")

    lines.append("")
    if report.out_of_scope:
        lines.append(f"OUT OF SCOPE: {len(report.out_of_scope)} file(s) outside {KNOWN_CLASSES}")
        for entry in report.out_of_scope:
            lines.append(f"  {humanize_bytes(entry.size):>10}  {entry.rel}")
    else:
        lines.append("out of scope: none")

    if report.over_budget:
        over = report.disk_bytes - report.budget
        lines.append(
            f"OVER BUDGET by {humanize_bytes(over)}: "
            f"{humanize_bytes(report.disk_bytes)} on disk of {humanize_bytes(report.budget)}"
        )
    elif report.ok:
        lines.append(
            f"OK: {humanize_bytes(report.disk_bytes)} of {humanize_bytes(report.budget)}, "
            "no out-of-scope paths"
        )
    return "\n".join(lines)


# --- recommended .stignore -------------------------------------------------
#
# The design note is explicit that the sovereign folder's .stignore is what
# keeps private keys off worker nodes, and equally explicit that an ignore
# rule which can skip objects/_freeze.json breaks the kill switch: is_frozen()
# treats an unreadable freeze file as frozen, so a transport that can partly
# write it is fail-safe, but one that can skip it is not. So the control-bus
# ruleset ignores nothing inside the five known classes, and stignore_ignores
# below exists so that property is asserted rather than eyeballed.

_STIGNORE_HEADER = """// skfleet-control recommended ignore ruleset.
// Emitted by: skfleet control-bus audit --stignore
//
// Syncthing matches top down, first match wins, so the keep rules come
// first. Nothing inside the five known path classes is ever ignored:
// objects/_freeze.json is the human kill switch and a transport that can
// skip it is not fail-safe.
"""


def stignore_body() -> str:
    """Return a recommended ``.stignore`` body for the control-bus folder.

    Returns:
        The file body, ending in a newline. It excludes nothing inside
        ``objects``, ``placements``, ``status``, ``decisions`` or ``atlas``.
    """
    lines = [_STIGNORE_HEADER]
    lines.append("// Keep: the five path classes the control bus carries.")
    for name in KNOWN_CLASSES:
        # Both the directory itself and its contents, because an ignored
        # parent directory is not descended into.
        lines.append(f"!/{name}")
        lines.append(f"!/{name}/**")
    lines.append("")
    lines.append("// Keep: Syncthing's own folder marker.")
    lines.append("!/.stfolder")
    lines.append("!/.stfolder/**")
    lines.append("")
    lines.append("// Ignore everything else at the folder root. A single `*` does not")
    lines.append("// cross a `/`, so this matches root entries only, and the keep rules")
    lines.append("// above already claimed the ones the control bus needs.")
    lines.append("/*")
    return "\n".join(lines) + "\n"


def _stignore_patterns(body: str) -> list[tuple[bool, re.Pattern[str]]]:
    """Compile a .stignore body into (keep, regex) pairs in file order."""
    compiled: list[tuple[bool, re.Pattern[str]]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line or line.startswith("//"):
            continue
        # Strip Syncthing's inline flag prefixes, e.g. (?d), (?i).
        while line.startswith("(?"):
            line = line[line.index(")") + 1 :]
        keep = line.startswith("!")
        if keep:
            line = line[1:]
        anchored = line.startswith("/")
        line = line.lstrip("/")
        parts: list[str] = []
        i = 0
        while i < len(line):
            if line.startswith("**", i):
                parts.append(".*")
                i += 2
            elif line[i] == "*":
                parts.append("[^/]*")
                i += 1
            elif line[i] == "?":
                parts.append("[^/]")
                i += 1
            else:
                parts.append(re.escape(line[i]))
                i += 1
        prefix = "" if anchored else r"(?:.*/)?"
        compiled.append((keep, re.compile(f"^{prefix}{''.join(parts)}$")))
    return compiled


def stignore_ignores(body: str, rel_path: str) -> bool:
    """Whether a .stignore body would ignore a path, first match wins.

    This is a deliberately small model of Syncthing's matcher: enough to
    self-check the ruleset :func:`stignore_body` emits (anchoring, ``!``
    negation, ``*`` which does not cross ``/``, ``**`` which does), not a
    drop-in replacement for it. A path is ignored when it matches, or when
    any ancestor directory is ignored, because Syncthing does not descend
    into an ignored directory.

    Args:
        body: A ``.stignore`` file body.
        rel_path: A path relative to the folder root, POSIX separators.

    Returns:
        True when the path would not be synced.
    """
    patterns = _stignore_patterns(body)
    parts = rel_path.strip("/").split("/")
    for depth in range(1, len(parts) + 1):
        candidate = "/".join(parts[:depth])
        for keep, regex in patterns:
            if regex.match(candidate):
                if keep:
                    break  # this prefix is kept, test the next one down
                return True
    return False
