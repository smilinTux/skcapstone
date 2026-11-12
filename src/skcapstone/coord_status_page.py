"""Bounded, machine-readable pages for ``coord status --format json``.

The cursor is opaque and scope-bound: it carries the last card id returned, the
page size and a digest of the filters it was minted for. A SHA-256 checksum
detects corruption and casual edits. It is NOT a MAC (there is no secret), so it
is not a security boundary; every failure to decode raises ``ValueError``
rather than silently restarting from the top of the board.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Sequence
from typing import Any

CURSOR_VERSION = 1
MAX_CURSOR_CHARS = 4096


def status_scope(tags: Sequence[str], parent: str | None, status: str | None) -> str:
    """Stable digest of the filters a cursor is valid for."""
    body = json.dumps(
        {"tag": sorted(t.lower() for t in tags), "parent": parent, "status": status},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(body).hexdigest()[:32]


def encode_cursor(after: str, limit: int, scope: str) -> str:
    body = json.dumps(
        {"after": after, "limit": limit, "scope": scope, "v": CURSOR_VERSION},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    cursor = base64.urlsafe_b64encode(body + hashlib.sha256(body).digest()).decode().rstrip("=")
    if len(cursor) > MAX_CURSOR_CHARS:
        raise ValueError("status cursor exceeds its encoded-size contract")
    return cursor


def decode_cursor(cursor: Any, limit: int, scope: str) -> str:
    """Return the ``after`` card id, or raise ValueError (malformed, tampered, stale)."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_CHARS:
        raise ValueError("status cursor is malformed")
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, digest = raw[:-32], raw[-32:]
        if digest != hashlib.sha256(body).digest():
            raise ValueError("checksum mismatch")
        payload = json.loads(body)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError("status cursor is malformed") from exc
    if (
        not isinstance(payload, dict)
        or set(payload) != {"after", "limit", "scope", "v"}
        or payload["v"] != CURSOR_VERSION
        or not isinstance(payload["after"], str)
        or not payload["after"]
        or isinstance(payload["limit"], bool)
        or payload["limit"] != limit
        or payload["scope"] != scope
    ):
        raise ValueError("status cursor is stale or minted for a different scope or limit")
    return payload["after"]


def build_page(
    rows: list[dict[str, Any]],
    *,
    limit: int | None,
    cursor: str | None,
    scope: str,
) -> dict[str, Any]:
    """Slice id-sorted ``rows`` into one page. ``limit`` must be >= 1 or None."""
    if limit is not None and (isinstance(limit, bool) or limit < 1):
        raise ValueError("--limit must be >= 1")
    if cursor is not None and limit is None:
        raise ValueError("--cursor requires --limit")
    rows = sorted(rows, key=lambda r: r["id"])
    if cursor is not None:
        after = decode_cursor(cursor, limit, scope)  # type: ignore[arg-type]
        rows = [r for r in rows if r["id"] > after]
    page = rows if limit is None else rows[:limit]
    has_more = limit is not None and len(rows) > limit
    next_cursor = encode_cursor(page[-1]["id"], limit, scope) if has_more else None  # type: ignore[arg-type]
    return {"cards": page, "has_more": has_more, "next_cursor": next_cursor}
