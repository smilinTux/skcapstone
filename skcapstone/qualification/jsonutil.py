"""Strict JSON parsing helpers for qualification trust boundaries."""

from __future__ import annotations

import json

SENSITIVE_JSON_KEYS = frozenset(
    {"password", "secret", "apikey", "accesstoken", "clientsecret", "refreshtoken"}
)


class StrictJsonError(ValueError):
    """Raised when JSON is malformed or contains duplicate object members."""


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build one object while rejecting last-key-wins ambiguity."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise StrictJsonError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def strict_json_loads(value: str | bytes) -> object:
    """Parse JSON with recursive duplicate-member rejection."""
    try:
        return json.loads(value, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise StrictJsonError("invalid JSON") from exc
