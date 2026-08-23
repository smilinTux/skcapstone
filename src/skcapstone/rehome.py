"""Rehome-safe card descriptions: fold-native path-prefix rewrite (card a680158b).

Learning: a repository move (``DAVE AI`` -> ``DAVE-AI``) stranded dozens of card
descriptions pointing at the old absolute path, and the stragglers survived
unnoticed for days because fixing them meant hand-editing write-once task
files. Cards should not carry fragile absolute paths, and when one slips
through anyway a repo move should be ONE command, not a sweep of hand edits.

This module implements ``coord rehome OLD_PREFIX NEW_PREFIX``: for every card
whose folded description still mentions ``OLD_PREFIX``, it appends a single
``describe`` event carrying the rewritten description. That is the established
fold pattern (same write path as ``coord describe``):

- ``core.json`` stays write-once - the rewrite is attributed, never destructive.
- It lands on both sanctioned append-only paths (the kanban overlay and, when
  mirroring is enabled, the card's own store log), so legacy and CardStore
  folds converge on the same text.
- It is reversible by swapping the arguments and idempotent on a re-run (the
  second run finds no remaining occurrences).

New cards should reference repo-relative paths (see ``coord create --desc``
help) so a rehome is needed rarely, not never.
"""

from __future__ import annotations

from pathlib import Path

DEFAULT_WRITER = "coord-rehome"


def find_rehome_matches(home: Path, old_prefix: str) -> list[dict]:
    """Fold the board and list every card whose description mentions ``old_prefix``.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        old_prefix: Path prefix to look for (matched literally, anywhere in
            the folded description).

    Returns:
        list[dict]: One entry per match: ``{"id", "description"}`` where
        ``description`` is the CURRENT folded description (replacement is
        computed by the caller). Empty when the board has no cards or none
        match.
    """
    from .card import KanbanBoard

    matches: list[dict] = []
    for card in KanbanBoard(Path(home).expanduser()).cards(include_archived=True):
        if old_prefix and old_prefix in card.description:
            matches.append({"id": card.id, "description": card.description})
    return matches


def rehome_descriptions(
    home: Path,
    old_prefix: str,
    new_prefix: str,
    agent: str = "",
    dry_run: bool = False,
) -> dict:
    """Rewrite ``old_prefix`` -> ``new_prefix`` across folded card descriptions.

    For each matching card, appends one attributed ``describe`` event with the
    rewritten description (overlay log plus CardStore mirror when enabled) -
    exactly the write path of ``coord describe``. ``dry_run`` reports the
    matches without appending anything.

    Args:
        home: Shared skcapstone root (``~/.skcapstone``).
        old_prefix: Path prefix to replace (literal, all occurrences).
        new_prefix: Replacement path prefix.
        agent: Writer attribution; defaults to ``coord-rehome``.
        dry_run: When True, compute the report without writing events.

    Returns:
        dict: ``{"matched": n, "rewritten": n, "dry_run": bool, "cards": [ids]}``.

    Raises:
        ValueError: When ``old_prefix`` is empty (would match every card).
    """
    from .card import CardEvent, CardEventLog
    from .card_store import card_store_write_enabled, mirror_coord_describe

    if not old_prefix:
        raise ValueError("old_prefix must not be empty")

    home_path = Path(home).expanduser()
    writer = agent or DEFAULT_WRITER
    matches = find_rehome_matches(home_path, old_prefix)

    rewritten = 0
    for match in matches:
        new_description = match["description"].replace(old_prefix, new_prefix)
        if dry_run:
            continue
        CardEventLog(home_path).append(
            CardEvent(
                card_id=match["id"],
                action="describe",
                description=new_description,
                writer=writer,
            )
        )
        if card_store_write_enabled():
            mirror_coord_describe(home_path, match["id"], writer, description=new_description)
        rewritten += 1

    return {
        "matched": len(matches),
        "rewritten": rewritten if not dry_run else 0,
        "dry_run": dry_run,
        "cards": [m["id"] for m in matches],
    }
