"""Rollout history: the manifest a node was actually running, before now.

See ``docs/superpowers/specs/2026-09-16-nimble-factory-design.md``, section
A.6, and ``.superpowers/sdd/2026-09-17-staged-rollout/task-1-brief.md``.

Rollback needs a previous state, and today nothing records one:
``lifecycle_seats.rollback_lifecycle_seats`` rolls back its own config files
only, and the wheel-level rollback convention actually used in practice is
hand-written evidence in ``card_events`` with no code behind it at all. So
"go back to the previous state" is a reconstruction, not a fact, and a
rollout built on top of that reconstruction inherits the gap. This module
closes it: every call to :func:`record_deployment` appends the manifest now
in force to a per-node history, so :func:`previous_manifest` can answer
"what was running before this" by reading a record, not by guessing.

Three design choices carried over deliberately from the modules this one
sits beside:

Node-scoped path
    The sovereign home is ONE Syncthing folder replicated across five
    hosts. An unscoped history file would let five hosts overwrite each
    other's writes and the record would be worthless. This follows the
    exact shape Task 2 of phase 1 used for the readiness verdict
    (``<sovereign home>/fleet/status/node-<host>/readiness/verdict.json``,
    see ``systemd/skfleet-readiness.service``): history lives at
    ``<sovereign home>/fleet/status/node-<host>/rollout/history.jsonl``,
    resolved via :func:`skcapstone.fleet.paths.paths_for_home` (so this
    module never re-derives the sovereign home's own name) and scoped by
    :func:`skcapstone.fleet.paths.self_node_name`, the same node-identity
    function every other fleet module already uses, rather than a second,
    independent notion of "this node".

Append-only, but not CardStore's shape
    ``skcoord.card_store.CardStore`` is the established append-only event
    log in this estate (O_APPEND file descriptors, ``flock``, a SHA-256
    prev-hash chain across events, raising on any malformed line so a
    reader never silently treats a corrupt event as absent). This module
    diverges from that on purpose. CardStore solves multi-writer,
    multi-host concurrent append onto the SAME file; a rollout history file
    is single-writer (this node's own deploy path) and single-host (the
    node-scoped path above already rules out cross-host collision), so
    there is no concurrent-append hazard to buy CardStore's locking and
    hash-chain machinery for. What this module does keep from the
    surrounding fleet code is the write mechanism: the same read-whole,
    append-in-memory, ``tmp`` + ``os.replace`` atomic write that
    ``lifecycle_seats._atomic_write`` and
    ``deployment_manifest.write_manifest`` already use, so a reader here
    never observes a half-written file either. "Append-only" is enforced by
    never truncating or rewriting bytes already on disk: each write is the
    previous file's exact bytes plus one new line, replaced into place
    atomically.

Corruption costs an entry, never the file, and never silently
    A prior incident in this estate is on record: a guard that could not
    parse a file silently skipped it and reported green over files it had
    never actually scanned. This module refuses to repeat that shape. A
    line that fails to parse as a JSON object is skipped by
    :func:`previous_manifest` (and any future reader built on
    :func:`_valid_entries`) rather than raising and losing every entry
    around it, but the skip is never silent: it is logged at WARNING with
    the file and line number, and the corrupt bytes are left exactly where
    they were, sitting in the file, undiscarded, so an operator can go
    look at the line the log points at.

Rollback entries are marked, and previous_manifest ignores them
    A rollback IS a deployment (it changes what the node runs), so
    :func:`record_deployment` records it too, via ``kind="rollback"`` --
    otherwise a second rollback would have no trail to read at all.
    But if that entry were then treated the same as a forward deploy, the
    history would oscillate: after ``[v1, v2-bad]``, rolling back to v1
    appends it, giving ``[v1, v2-bad, v1]``; a second rollback naively
    reading the second-to-last entry would resolve to v2-bad, the exact
    manifest the first rollback escaped. This is reachable on the
    documented recovery path, not by misuse: a rollback that halts partway
    through a multi-node plan and is then re-run over the same node list
    re-rolls-back every node already completed, including this one. A
    rollback entry therefore carries an internal marker
    (``__rollout_kind``) and :func:`previous_manifest` skips marked
    entries entirely when resolving what to roll back to, so the answer
    always comes from the forward-deploy trail, never from what a rollback
    itself last wrote.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

from .paths import paths_for_home, self_node_name

logger = logging.getLogger(__name__)

#: Internal marker key added to a recorded entry when ``kind="rollback"``.
#: Not a manifest field: ``deployment_manifest.build_manifest`` never
#: produces this key, so its presence unambiguously means "this entry is
#: what a rollback recorded", not "this is a new forward deployment".
_KIND_KEY = "__rollout_kind"


def _history_path(home: Path | str) -> Path:
    """This node's rollout history file, under the estate home.

    Args:
        home: The user's home directory, the same meaning ``home`` carries
            in ``deployment_manifest.build_manifest`` and
            ``rollout_drift.detect_drift``.

    Returns:
        The node-scoped history file inside the fleet tree's status
        directory, built via :func:`paths_for_home` so the sovereign home's
        own name is never re-typed here.
    """
    fleet = paths_for_home(home)
    return fleet.node_status_dir(self_node_name()) / "rollout" / "history.jsonl"


def _canonical_json_line(manifest: dict[str, Any]) -> bytes:
    return (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    """Write ``payload`` to ``path`` atomically: tmp file in the same
    directory, then ``os.replace``, matching
    ``skcapstone.lifecycle_seats._atomic_write``.

    A reader (``previous_manifest``, or an operator's own inspection) never
    observes a partially written file: the temp file is either fully
    written and fsynced, or the rename has not happened yet and the
    previous file (or nothing) is still what a reader sees.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def record_deployment(home: Path | str, manifest: dict[str, Any], *, kind: str = "deploy") -> None:
    """Append ``manifest`` as the newest entry in this node's rollout history.

    Args:
        home: The user's home directory; see :func:`_history_path`.
        manifest: The deployment manifest now in force on this node (as
            built by ``deployment_manifest.build_manifest``, though any
            JSON-serializable dict is accepted; this module does not
            interpret the manifest's fields, only records it).
        kind: ``"deploy"`` (the default) for a forward deployment, or
            ``"rollback"`` when this call is recording what a rollback
            just returned the node to. Only ``"rollback"`` is written to
            disk as a marker (see :func:`previous_manifest` for why a
            plain forward-deploy entry stays exactly the manifest dict it
            always was, with no added key).

    The existing file's bytes are never truncated or rewritten: the new
    write is exactly the old bytes plus one new line, replaced into place
    atomically. That is what makes the history append-only rather than
    merely "a file that happens to grow".
    """
    path = _history_path(home)
    entry: dict[str, Any] = dict(manifest)
    if kind == "rollback":
        entry[_KIND_KEY] = kind
    existing = path.read_bytes() if path.exists() else b""
    if existing and not existing.endswith(b"\n"):
        existing += b"\n"
    _atomic_write(path, existing + _canonical_json_line(entry))


def _valid_entries(path: Path) -> list[dict[str, Any]]:
    """Every entry in ``path`` that parses as a JSON object, in file order.

    A line that fails to parse, or that parses to something other than a
    JSON object, is skipped rather than raised on, so one corrupt entry
    costs only itself, not every entry around it. The skip is logged at
    WARNING with the file and line number so it is discoverable, never
    silent, and the corrupt bytes are left in the file untouched -- this
    function only reads, it never writes.
    """
    if not path.exists():
        return []
    entries: list[dict[str, Any]] = []
    raw = path.read_text(encoding="utf-8")
    for line_number, line in enumerate(raw.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            logger.warning(
                "rollout_history: skipping corrupt (unparseable) entry at %s:%d",
                path,
                line_number,
            )
            continue
        if not isinstance(entry, dict):
            logger.warning(
                "rollout_history: skipping corrupt (non-object) entry at %s:%d",
                path,
                line_number,
            )
            continue
        entries.append(entry)
    return entries


def _is_rollback_entry(entry: dict[str, Any]) -> bool:
    return entry.get(_KIND_KEY) == "rollback"


def previous_manifest(home: Path | str) -> dict[str, Any] | None:
    """The manifest that was in force on this node before its most recent
    recorded forward deployment.

    Args:
        home: The user's home directory; see :func:`_history_path`.

    Returns:
        The second-to-last entry among this node's forward-deploy entries,
        or ``None`` when there is no such entry: the deploy trail has no
        records yet, has exactly one, or the entry immediately before the
        latest one was itself corrupt (skipped, per :func:`_valid_entries`,
        rather than treated as though it were readable).

    Entries recorded by a rollback (``kind="rollback"``, see
    :func:`record_deployment`) are skipped entirely, not merely left in
    place: they never count as either "the current entry" or "the previous
    entry" here. Without that skip, this function would oscillate --
    rolling back to v1 after ``[v1, v2-bad]`` appends v1, and a second
    rollback reading the second-to-last entry of ``[v1, v2-bad, v1]``
    would resolve to v2-bad, the manifest the first rollback escaped. See
    the module docstring's "Rollback entries are marked" section.
    """
    entries = [
        entry for entry in _valid_entries(_history_path(home)) if not _is_rollback_entry(entry)
    ]
    if len(entries) < 2:
        return None
    return entries[-2]
