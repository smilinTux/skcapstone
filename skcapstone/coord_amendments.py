"""Folded amendment helpers for coordination cards (cards e78fd954, 325a737f).

Birth facts (``core.json`` priority / acceptance_criteria) are write-once.
These helpers amend them the way ``coord describe`` amends the title: an
appended, writer-attributed event that the fold applies on read, so the
edit is reversible by re-applying and the original stays visible in
``core.json``.

- ``reprioritize`` rides the existing ``set_priority`` overlay action, which
  both folds (legacy ``fold_overlay`` and the CardStore fold via
  ``load_legacy_mutations``) already understand.
- ``amend_criteria`` is appended to the card's own store log. The required
  ``skcoord>=0.1.39`` fold applies it to both ``CardStore.fold`` and every
  legacy rollback selector through ``Board.get_task_views``;
  :func:`current_acceptance_criteria` delegates to that authoritative fold.
- ``void_card`` (card 325a737f) kills a mistakenly created card without
  completing it: a ``void`` audit event plus the archive mechanism, so no
  Joules are minted and the changelog stays clean.
"""

from __future__ import annotations

from pathlib import Path

from .card import CardEvent, CardEventLog
from .card_store import CardStore, card_store_write_enabled

VALID_PRIORITIES = ("critical", "high", "medium", "low")


def add_dependency(
    home: Path,
    task_id: str,
    dependency_id: str,
    agent: str = "",
    reason: str = "",
) -> bool:
    """Append an idempotent, attributed dependency amendment.

    The immutable task/core birth record remains unchanged. The SKCoord fold
    applies the added gate at read and claim time, including through the legacy
    projection used by the CardStore rollback switch.
    """
    from .card_store import add_dependency as append_dependency

    return append_dependency(
        Path(home).expanduser(), task_id, dependency_id, agent=agent, reason=reason
    )


def remove_dependency(
    home: Path,
    task_id: str,
    dependency_id: str,
    agent: str = "",
    reason: str = "",
) -> bool:
    """Append an attributed dependency removal as a reversible rollback."""
    from .card_store import remove_dependency as append_removal

    return append_removal(
        Path(home).expanduser(), task_id, dependency_id, agent=agent, reason=reason
    )


def reprioritize(home: Path, task_id: str, priority: str, agent: str = "") -> None:
    """Append a folded priority amendment for a card.

    Writes the sanctioned ``set_priority`` overlay event and, when CardStore
    dual-write is enabled, mirrors it as a store ``priority`` event (the same
    belt-and-suspenders pattern as ``coord describe``).

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        task_id: The card/task ID to amend.
        priority: New priority (one of :data:`VALID_PRIORITIES`).
        agent: Writer attribution (empty defaults to the host).

    Raises:
        ValueError: If ``priority`` is not a valid priority.
    """
    if priority not in VALID_PRIORITIES:
        raise ValueError(f"invalid priority '{priority}' (expected one of {VALID_PRIORITIES})")
    home = Path(home).expanduser()
    CardEventLog(home).append(
        CardEvent(card_id=task_id, action="set_priority", priority=priority, writer=agent)
    )
    if card_store_write_enabled():
        CardStore(home).append_event(task_id, "priority", agent or "mcp", priority=priority)


def amend_criteria(home: Path, task_id: str, criteria: list[str], agent: str = "") -> None:
    """Replace a card's folded acceptance criteria with an appended event.

    The write-once ``core.json`` keeps the original list; the fold applies
    the latest ``amend_criteria`` event on top. Reversed by amending again
    (the original list is always readable from ``core.json``).

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        task_id: The card/task ID to amend.
        criteria: The full replacement criteria list (last event wins).
        agent: Writer attribution (empty defaults to ``mcp`` in the store log).

    Raises:
        ValueError: If ``criteria`` is empty.
    """
    if not criteria:
        raise ValueError("at least one criterion is required")
    home = Path(home).expanduser()
    store = CardStore(home)
    if store.fold(task_id) is None:
        raise ValueError(f"CardStore card {task_id} has no foldable core")
    store.append_event(task_id, "amend_criteria", agent or "mcp", criteria=list(criteria))


def _base_acceptance_criteria(home: Path, task_id: str) -> list[str]:
    """Return the birth-fact criteria from core.json, else the legacy task file."""
    core = CardStore(home)._load_core(task_id)
    if core is not None:
        return list(core.get("acceptance_criteria", []) or [])
    from .coordination import Board

    for task in Board(home).load_tasks(include_archived=True):
        if task.id == task_id:
            return list(task.acceptance_criteria)
    return []


def current_acceptance_criteria(home: Path, task_id: str) -> list[str]:
    """Return a card's acceptance criteria from the authoritative store fold.

    Cards mirrored into the store delegate directly to ``CardStore.fold``.
    Cards without a store core fall back to the legacy task birth facts.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        task_id: The card/task ID to fold.

    Returns:
        list[str]: The current (amended) acceptance criteria.
    """
    home = Path(home).expanduser()
    card = CardStore(home).fold(task_id)
    if card is not None:
        return list(card.acceptance_criteria)
    return _base_acceptance_criteria(home, task_id)


def void_record(home: Path, task_id: str) -> dict | None:
    """Return the latest void event for a card, or None if never voided.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        task_id: The card/task ID to inspect.

    Returns:
        dict | None: The void event (writer, ts, reason), latest winning.
    """
    record = None
    for event in CardStore(Path(home).expanduser())._read_events(task_id):
        if event.get("action") == "void":
            record = event
    return record


def is_voided(home: Path, task_id: str) -> bool:
    """True when the card has a void event in its store log."""
    return void_record(home, task_id) is not None


def void_card(
    home: Path,
    task_id: str,
    reason: str,
    agent: str = "",
    force_terminal: bool = False,
) -> None:
    """Void a mistakenly created card without completing it (card 325a737f).

    Appends a writer-attributed ``void`` event (the audit record, with the
    reason) to the card's store log, then archives the card so it leaves the
    active board in both the legacy and store-served projections. It never
    goes through ``Board.complete_task``, so no Joules are minted
    (``_mint_joules_for_task`` only fires on completion) and the card never
    appears in ``coord changelog`` output (the changelog reads non-archived
    done tasks only). The card stays on disk and remains foldable with
    ``include_archived=True`` for audit.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        task_id: The card/task ID to void.
        reason: Why the card is being voided (required for audit).
        agent: Writer attribution (empty defaults to ``mcp``/host).

    A COMPLETED card is refused by default, because voiding it does nothing.

    The mechanism, measured rather than assumed: ``CardStore.fold`` has no
    handler for the ``void`` action at all. It handles ``archive`` and
    ``reopen`` and ignores ``void`` entirely. Voiding is therefore expressed by
    archiving the card off the active board, never by its folded status, and
    ``is_voided`` is a separate audit lookup. On a card that has already
    completed there is nothing left to archive off the board, so the whole
    operation becomes a no-op that still reports success.

    Measured 2026-08-27: three cards marked complete while their own verdict read
    BLOCKED (2c35d28b, ae993252, e5d5748f) were voided. All three commands
    reported success. All three still folded to done, each carrying one void
    event that no reader of the folded state will ever see.

    ``force_terminal`` records the void anyway, as an audit trace only. The fold
    still reports the card done, and the caller is expected to say so.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        task_id: The card/task ID to void.
        reason: Why the card is being voided (required for audit).
        agent: Writer attribution (empty defaults to ``mcp``/host).
        force_terminal: Record an audit-only void on an already-completed card.

    Raises:
        ValueError: If ``reason`` is empty, the card is already voided, or the
            card is already complete and ``force_terminal`` is not set.
    """
    if not reason:
        raise ValueError("a void reason is required")
    home = Path(home).expanduser()
    if is_voided(home, task_id):
        raise ValueError(f"card {task_id} is already voided")
    if not force_terminal:
        from skcoord.card import Column

        folded = CardStore(home).fold(task_id)
        if folded is not None and folded.status == Column.DONE:
            raise ValueError(
                f"card {task_id} is already complete; voiding it would append an "
                "event the fold discards, and the card would still read done. "
                "Supersede it with a replacement card, or pass force_terminal to "
                "record an audit-only void that does NOT change the folded state."
            )
    CardStore(home).append_event(task_id, "void", agent or "mcp", reason=reason)

    from .coordination import Board

    board = Board(home)
    board.archive_task(task_id, by=agent or "void")

    legacy_archived = task_id in board.archived_ids()
    store = CardStore(home)
    events = store._read_events(task_id)
    void_positions = [index for index, event in enumerate(events) if event.get("action") == "void"]
    store_archived = bool(void_positions) and any(
        event.get("action") == "archive" for event in events[void_positions[-1] + 1 :]
    )
    if not legacy_archived or not store_archived:
        missing = []
        if not legacy_archived:
            missing.append("legacy board")
        if not store_archived:
            missing.append("CardStore")
        raise RuntimeError(
            f"void for {task_id} was only partially applied; archive missing from "
            + " and ".join(missing)
        )
