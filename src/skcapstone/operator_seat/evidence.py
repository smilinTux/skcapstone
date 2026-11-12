"""Evidence freshness for operator actuation (ATLAS gate 5 hardening).

A proposal that wants to actuate must carry structured evidence: the artifact
or observation it rests on (``source``) and when that was observed
(``observed_at``, an ISO 8601 timestamp WITH a timezone)::

    {"evidence": {"source": "observation:skchat/BridgeAlive/bridge",
                  "observed_at": "2026-11-12T12:00:00Z"}}

The gate 5 fault-injection drill (scenario 9) showed the old free-text
``evidence_ref`` was accepted verbatim whatever its age: a fabricated reference
from 2019 reached the ledger and was acted on. Freshness is now checked before
anything is written or run, and it fails closed: missing, malformed, naive
(no timezone), future-dated or too old evidence is refused, and so is a bad
maximum age (non-numeric, non-positive, non-finite, or above
``MAX_ALLOWED_AGE_SECONDS``). The check cannot be switched off, not even by
passing an absurdly large age.

Age is always measured against the REAL clock at the decision point, never the
pass clock the observation may have been stamped with, and ``observed_at`` is the
time of the SOURCE data (adapters report it as ``source_observed_at``). An
observation whose source time is unknown carries no evidence and cannot actuate.

The legacy ``evidence_ref`` string is still passed through to the ledger for
audit, but it is never freshness evidence on its own.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

#: Default maximum evidence age. The proposer stamps evidence from the
#: observation the pass just made, so a healthy pass is seconds old; this only
#: has to cover a slow model call plus queueing.
DEFAULT_MAX_AGE_SECONDS = 600.0

#: How far ahead of the clock an ``observed_at`` may sit before it is refused as
#: future-dated (clock skew between nodes, never a licence).
MAX_FUTURE_SKEW_SECONDS = 60.0

#: The largest maximum age any caller may configure. One hour: a pass has a five
#: minute runtime budget, so evidence older than that stopped describing the
#: estate a long time ago, and a configurable age with no ceiling is a switch that
#: turns the check off. Anything larger is refused, never clamped, so a typo cannot
#: silently widen the window.
MAX_ALLOWED_AGE_SECONDS = 3600.0

#: ``ActionEvent.evidence_ref`` is capped at this many characters.
_LEDGER_REF_LIMIT = 1024


def parse_aware(value: Any) -> datetime | None:
    """Parse an aware ISO 8601 timestamp to UTC, or None when it is not one.

    Naive timestamps are not assumed to be UTC, and a timestamp whose UTC
    conversion overflows (year 1 with a positive offset) is simply not usable.
    """
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


_parse = parse_aware


def _valid_max_age(max_age_s: Any) -> bool:
    if not isinstance(max_age_s, (int, float)) or isinstance(max_age_s, bool):
        return False
    try:
        return math.isfinite(max_age_s) and 0 < max_age_s <= MAX_ALLOWED_AGE_SECONDS
    except OverflowError:  # math.isfinite(10**400)
        return False


def refusal(proposal: dict, now_iso: str, max_age_s: Any = DEFAULT_MAX_AGE_SECONDS) -> str | None:
    """Return why this proposal's evidence may not be acted on, or None when fresh.

    Args:
        proposal: The proposal dict; its ``evidence`` key carries the stamp.
        now_iso: The clock to measure against (ISO 8601). Callers pass the REAL
            clock at the decision point, never the pass clock the observation
            may have been stamped with.
        max_age_s: Oldest acceptable evidence, in seconds. Anything that is not
            a finite number in (0, ``MAX_ALLOWED_AGE_SECONDS``] refuses rather
            than disabling the check.
    """
    if not _valid_max_age(max_age_s):
        return f"evidence max age invalid: {max_age_s!r}"
    now = parse_aware(now_iso)
    if now is None:
        return "evidence unverifiable: the clock is not a timezone-aware timestamp"
    stamp = proposal.get("evidence")
    if not isinstance(stamp, dict):
        return "evidence missing: actuation needs evidence {source, observed_at}"
    source = stamp.get("source")
    if not isinstance(source, str) or not source.strip():
        return "evidence missing: no source artifact or observation id"
    if "observed_at" not in stamp:
        return "evidence missing: no observed_at timestamp"
    observed_at = stamp["observed_at"]
    observed = parse_aware(observed_at)
    if observed is None:
        return (
            f"evidence unparseable: observed_at {observed_at!r} is not a timezone-aware "
            "ISO 8601 timestamp"
        )
    try:
        age = (now - observed).total_seconds()
    except OverflowError:
        return f"evidence unparseable: observed_at {observed_at!r} is out of range"
    if age < -MAX_FUTURE_SKEW_SECONDS:
        return f"evidence timestamp is in the future: {observed_at} is {-age:.0f}s ahead"
    if age > max_age_s:
        return f"evidence stale: {observed_at} is {age:.0f}s old, max {max_age_s:g}s"
    return None


def ledger_ref(proposal: dict) -> str | None:
    """The audit string recorded on the ledger's first event.

    The structured stamp comes FIRST, rendered ``<source>@<observed_at>``, so the
    ledger always carries the time the action rested on; an explicit free-text
    ``evidence_ref`` follows after ``"; "``. When the two do not fit the ledger's
    field limit the explicit text is trimmed first, then the source name, and the
    timestamp is never cut.
    """
    explicit = proposal.get("evidence_ref")
    explicit = explicit if isinstance(explicit, str) and explicit else None
    stamp = proposal.get("evidence")
    source = stamp.get("source") if isinstance(stamp, dict) else None
    observed_at = stamp.get("observed_at") if isinstance(stamp, dict) else None
    if not isinstance(source, str) or not isinstance(observed_at, str):
        return explicit[:_LEDGER_REF_LIMIT] if explicit else None
    suffix = f"@{observed_at}"
    head = source[: max(0, _LEDGER_REF_LIMIT - len(suffix))] + suffix
    if explicit is None:
        return head
    room = _LEDGER_REF_LIMIT - len(head) - 2
    return f"{head}; {explicit[:room]}" if room > 0 else head


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "MAX_ALLOWED_AGE_SECONDS",
    "MAX_FUTURE_SKEW_SECONDS",
    "ledger_ref",
    "parse_aware",
    "refusal",
]
