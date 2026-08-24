"""Bounded non-secret scope parsing for the control-plane Now projection."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

ROLES = frozenset({"operator", "project-manager", "architect"})
SILOS = frozenset(
    {
        "portfolio",
        "flow",
        "itil",
        "delivery",
        "architecture",
        "fleet",
        "ai",
        "economy",
        "governance",
        "legal",
        "corpus",
        "operator",
    }
)
TRUTH_STATES = frozenset(
    {
        "current",
        "stale",
        "partial",
        "unavailable",
        "unreachable",
        "unknown",
        "not_applicable",
    }
)
ALLOWED_KEYS = frozenset(
    {"role", "scope", "window", "baseline", "service", "selected_silo", "truth", "saved_view"}
)
PROTECTED_KEYS = frozenset({"tenant_id", "matter_id"})
_SAVED_VIEW = re.compile(r"^sv-[0-9a-f]{32}$")
_MAX_VALUE_LENGTH = 128


class ScopeQueryError(ValueError):
    """A query cannot be represented by the current read-only projection."""


class ProtectedScopeDenied(ScopeQueryError):
    """A protected scope was requested without an owner-policy projection."""


@dataclass(frozen=True)
class NowScope:
    role: str
    scope: str = "estate"
    window: str = "latest"
    baseline: str = "none"
    service: str = "all"
    selected_silo: str = ""
    truth: str = ""
    saved_view: str = ""

    def as_dict(self) -> dict[str, str]:
        values = {
            "role": self.role,
            "scope": self.scope,
            "window": self.window,
            "baseline": self.baseline,
            "service": self.service,
        }
        for key in ("selected_silo", "truth", "saved_view"):
            value = getattr(self, key)
            if value:
                values[key] = value
        return values


def parse_now_scope(query) -> NowScope:
    """Validate one exact V1 scope before any estate adapter is called."""

    pairs = list(query.multi_items())
    counts = Counter(key for key, _value in pairs)
    if any(key in PROTECTED_KEYS for key in counts):
        raise ProtectedScopeDenied("protected scope is not available")
    unknown = set(counts) - ALLOWED_KEYS
    if unknown:
        raise ScopeQueryError("query contains unsupported fields")
    if any(count != 1 for count in counts.values()):
        raise ScopeQueryError("query fields must appear once")
    if any(not value or len(value) > _MAX_VALUE_LENGTH for _key, value in pairs):
        raise ScopeQueryError("query values are invalid")

    values = dict(pairs)
    role = values.get("role", "operator")
    if role not in ROLES:
        raise ScopeQueryError("role is unsupported")
    exact = {"scope": "estate", "window": "latest", "baseline": "none", "service": "all"}
    if any(values.get(key, expected) != expected for key, expected in exact.items()):
        raise ScopeQueryError("scope is unsupported by the current projection")
    selected_silo = values.get("selected_silo", "")
    if selected_silo and selected_silo not in SILOS:
        raise ScopeQueryError("selected silo is unsupported")
    truth = values.get("truth", "")
    if truth and truth not in TRUTH_STATES:
        raise ScopeQueryError("truth filter is unsupported")
    saved_view = values.get("saved_view", "")
    if saved_view and not _SAVED_VIEW.fullmatch(saved_view):
        raise ScopeQueryError("saved view is invalid")
    return NowScope(
        role=role,
        selected_silo=selected_silo,
        truth=truth,
        saved_view=saved_view,
    )
