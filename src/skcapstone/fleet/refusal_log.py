"""Make fail-closed gate refusals observable without changing the decision.

A gate that answers ``False``/``None`` with no reason leaves the operator with
only a generic state (2026-10-12/13: awaiting-test-admission, a skipped route
preflight, a source review that never opened). ``refuse`` records why, once per
(site, subject, reason), and returns ``False`` so a guard can ``return
refuse(...)`` in place of a bare ``return False``.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_LOGGED: set[tuple[str, str, str]] = set()
_LAST: dict[str, str] = {}
_MAX_SEEN = 4096


def reason_of(exc: BaseException) -> str:
    """Return a bounded ``Type: message`` reason for an exception."""
    return f"{type(exc).__name__}: {str(exc)[:240]}"


def refuse(site: str, subject: object, reason: str) -> bool:
    """Log one refusal per (site, subject, reason) and return False.

    Args:
        site: Stable gate name, e.g. ``source-review-candidate``.
        subject: Card or request id the gate refused.
        reason: Why it refused.

    Returns:
        Always False, the fail-closed answer every caller already returned.
    """
    subject_text = str(subject or "")
    _LAST[subject_text] = reason
    key = (site, subject_text, reason)
    if key in _LOGGED:
        return False
    if len(_LOGGED) >= _MAX_SEEN:
        _LOGGED.clear()
    _LOGGED.add(key)
    logger.warning("GATE_REFUSED|%s|%s|%s", site, subject_text[:16], reason)
    return False


def last_reason(subject: object, default: str = "") -> str:
    """Return the most recent refusal reason recorded for ``subject``."""
    return _LAST.get(str(subject or ""), default)


def note(subject: object, reason: str) -> None:
    """Record an expected deferral reason for ``subject`` without a warning."""
    _LAST[str(subject or "")] = reason
