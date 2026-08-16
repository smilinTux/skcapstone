"""Maps a required package/unit to the repo installer that provides it."""
from __future__ import annotations

import fnmatch

UNSUPPORTED = "unsupported"

# (glob, backend_id) checked in order; first match wins.
_UNIT_RULES: list[tuple[str, str]] = [
    ("capauth-authz*", "capauth-authz"),
    ("skcomms*", "skcomms"),
    ("skmemory-*@*", "agent"),
    ("skwhisper@*", "agent"),
    ("cloud9-daemon@*", "agent"),
    ("skchat*", "skchat"),
    ("livekit-server*", "skchat"),
    ("jarvis-heartbeat*", "skchat"),
    ("skcapstone*", "core"),
    ("sknoded*", "core"),
    ("skgateway*", "core"),
]

_TIER: dict[str, int] = {
    "packages": 1,
    "capauth-authz": 2,
    "skcomms": 3,
    "core": 4,
    "skchat": 5,
    "agent": 6,
    UNSUPPORTED: 9,
}


def tier_of(backend_id: str) -> int:
    """Return the install tier for a backend ID.

    Tiers determine install order: lower tiers install first.
    Unsupported units have tier 9 (last).

    Args:
        backend_id: The backend identifier.

    Returns:
        The tier number; 9 if backend_id is not recognized.
    """
    return _TIER.get(backend_id, 9)


def resolve(name: str, kind: str) -> str:
    """Resolve a required unit or package to its owning backend.

    Packages always resolve to the "packages" backend. Units are matched
    against glob patterns in order; the first match wins. Unmatched units
    resolve to UNSUPPORTED.

    Args:
        name: The unit or package name.
        kind: The kind: "unit" or "package".

    Returns:
        The backend ID (e.g., "core", "skchat", "agent") or UNSUPPORTED.
    """
    if kind == "package":
        return "packages"
    for glob, backend in _UNIT_RULES:
        if fnmatch.fnmatch(name, glob):
            return backend
    return UNSUPPORTED
