"""Refuse card titles that are obviously an argv fragment, not a title.

Measured on the chi board 2026-09-19: 118 ``describe`` events had destroyed a
card's folded title, 42 of them that day alone, accelerating from 8 on
2026-09-11. Every one was written by ``mcp``, across six different nodes, so
this is a caller behaviour rather than one broken host.

Two shapes account for all of them:

* ``"x"`` / ``"y"`` -- a tool being poked with placeholder values.
* ``"--description"`` / ``"--desc"`` -- a CLI option NAME passed as the option
  VALUE, by a caller mixing up the CLI signature with the MCP one.

The cost was not cosmetic. ``_size_class_for`` resolves a card's size from its
title, and consults the canonical ``sk-*`` label only when the title is EMPTY.
A title that is present but carries no ``[S]``/``[M]``/``[L]``/``[XL]`` marker
takes the fail-closed branch, so ``_logical_route_for`` returns None and the
card is filtered out of the candidate scan before any lane is consulted, with
no log line at all. Thirteen cards in that state were the entire remaining
owned slice of three hosts, and the fleet fell from a full complement to two
workers while every seat sat free.

``core.json`` keeps the birth title, so inspecting it shows nothing wrong.
``authoritative_claimability`` overwrites the folded title from event state,
and the folded title is what the selector reads. That is why this was
invisible to the obvious check.

The guard is deliberately narrow. It rejects only what cannot be a real title,
because a validator that refuses legitimate input gets routed around, and the
write boundary it protects is the only thing standing between a typo and an
un-routable card.
"""

from __future__ import annotations

#: Placeholder values seen in real corruption events.
_PLACEHOLDERS = frozenset({"x", "y", "z", "foo", "bar", "test", "n/a", "none"})

MIN_TITLE_CHARS = 3


class DegenerateTitleError(ValueError):
    """A proposed title is an argv fragment or placeholder, not a title."""


def check_title(title: str | None) -> None:
    """Raise :class:`DegenerateTitleError` if ``title`` cannot be a real title.

    ``None`` is allowed: it means "do not change the title". An empty string is
    also allowed, because clearing a title is an explicit, documented action
    and the size resolver has a defined fallback for it.
    """
    if title is None:
        return
    candidate = title.strip()
    if not candidate:
        return
    if candidate.startswith("-"):
        raise DegenerateTitleError(
            f"refusing title {title!r}: it starts with '-', which means an option "
            "name was passed as the option value. Pass the title text itself."
        )
    if candidate.lower() in _PLACEHOLDERS:
        raise DegenerateTitleError(
            f"refusing title {title!r}: that is a placeholder, not a title. "
            "A card whose title loses its [S]/[M]/[L]/[XL] marker becomes "
            "un-routable and is dropped from dispatch with no log line."
        )
    if len(candidate) < MIN_TITLE_CHARS:
        raise DegenerateTitleError(
            f"refusing title {title!r}: shorter than {MIN_TITLE_CHARS} characters. "
            "Real card titles carry a size marker and a description."
        )
