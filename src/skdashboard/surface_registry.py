"""Fleet surface registry: maps ItemRef (surface, item_id) to CardStore shadow-card ids.

Generalized routes such as ``/api/suggest/{surface}/{id}`` and
``/api/queue/{surface}/{id}`` need a single, pure place to translate a fleet
surface reference into the shadow-card id that the existing per-card
suggestion/queue machinery already understands.

Shadow-card id conventions (see ``skcapstone.agent_run.ensure_card``):
    - GTD next-actions materialize as ``gtd-<id>``.
    - ITIL records already carry their own prefix (``inc-``/``prb-``/``chg-``)
      as part of the record id, so no extra prefix is added.
    - Coord tasks use their raw task id with no prefix.
    - Chat threads use ``thr-<id>``.
    - Security findings use ``sec-<id>``.

This module is intentionally standard-library only and has no dependency on
skcapstone or skdashboard internals, so it can be imported from route
handlers, background jobs, or tests without pulling in the rest of the
stack.
"""

from __future__ import annotations

KNOWN_SURFACES: frozenset[str] = frozenset({"coord", "gtd", "itil", "chat", "security"})
"""The set of fleet surfaces the suggestion engine knows how to route."""

SURFACE_PREFIX: dict[str, str] = {
    "coord": "",
    "gtd": "gtd-",
    "itil": "",
    "chat": "thr-",
    "security": "sec-",
}
"""Maps a surface name to the shadow-card id prefix used for that surface.

An empty string means the shadow-card id is the raw item id unchanged
(coord tasks, and ITIL records whose id already carries its own
``inc-``/``prb-``/``chg-`` prefix).
"""


def is_known_surface(surface: str) -> bool:
    """Report whether ``surface`` is a recognized fleet surface.

    Args:
        surface: The surface name to check (e.g. "coord", "gtd").

    Returns:
        True if ``surface`` is in ``KNOWN_SURFACES``, False otherwise.
    """
    return surface in KNOWN_SURFACES


def resolve_card_id(surface: str, item_id: str) -> str | None:
    """Resolve a fleet ItemRef to its CardStore shadow-card id.

    Args:
        surface: The fleet surface the item belongs to (e.g. "gtd", "coord").
        item_id: The surface-native item id (e.g. a GTD next-action id, a
            coord task id, or an ITIL record id already carrying its own
            ``inc-``/``prb-``/``chg-`` prefix).

    Returns:
        The shadow-card id (prefix + item_id) that
        ``skcapstone.agent_run.ensure_card`` and the suggestion/queue
        machinery expect, or None if ``surface`` is not known or ``item_id``
        is empty/blank. If ``item_id`` already starts with the surface's
        prefix, the prefix is not applied a second time (idempotent). For
        surfaces with an empty prefix (coord, itil), the stripped item_id is
        returned unchanged.
    """
    if not is_known_surface(surface):
        return None

    stripped = item_id.strip()
    if not stripped:
        return None

    prefix = SURFACE_PREFIX[surface]
    if not prefix:
        return stripped
    if stripped.startswith(prefix):
        return stripped
    return f"{prefix}{stripped}"


def parse_card_id(card_id: str) -> tuple[str, str]:
    """Infer the fleet surface and item id from a shadow-card id.

    This is a best-effort inverse of :func:`resolve_card_id`. Because coord
    and ITIL surfaces carry no distinguishing prefix, any card id that does
    not match a known prefix is classified as "coord", and ITIL ids
    (``inc-``/``prb-``/``chg-``) are classified as "itil" but returned with
    their full id intact as ``item_id`` (the prefix is part of the ITIL
    record id, not a routing artifact).

    Args:
        card_id: A shadow-card id, e.g. "gtd-abc", "thr-x", "inc-1".

    Returns:
        A (surface, item_id) tuple. Examples: "gtd-abc" -> ("gtd", "abc");
        "thr-x" -> ("chat", "x"); "sec-x" -> ("security", "x");
        "inc-1"/"prb-1"/"chg-1" -> ("itil", "inc-1") etc. (item_id equals
        the full card_id for itil); anything else -> ("coord", card_id).
    """
    if card_id.startswith("gtd-"):
        return "gtd", card_id[len("gtd-") :]
    if card_id.startswith("thr-"):
        return "chat", card_id[len("thr-") :]
    if card_id.startswith("sec-"):
        return "security", card_id[len("sec-") :]
    if card_id.startswith(("inc-", "prb-", "chg-")):
        return "itil", card_id
    return "coord", card_id
