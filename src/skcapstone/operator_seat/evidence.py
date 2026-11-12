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
maximum age. There is no way to switch the check off.

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

#: How far ahead of the pass clock an ``observed_at`` may sit before it is
#: refused as future-dated (clock skew between nodes, never a licence).
MAX_FUTURE_SKEW_SECONDS = 60.0

#: ``ActionEvent.evidence_ref`` is capped at this many characters.
_LEDGER_REF_LIMIT = 1024


def _parse(value: str) -> datetime | None:
    """Parse an aware ISO 8601 timestamp to UTC, or None when it is not one."""
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(timezone.utc)


def _valid_max_age(max_age_s: Any) -> bool:
    return (
        isinstance(max_age_s, (int, float))
        and not isinstance(max_age_s, bool)
        and math.isfinite(max_age_s)
        and max_age_s > 0
    )


def refusal(proposal: dict, now_iso: str, max_age_s: Any = DEFAULT_MAX_AGE_SECONDS) -> str | None:
    """Return why this proposal's evidence may not be acted on, or None when fresh.

    Args:
        proposal: The proposal dict; its ``evidence`` key carries the stamp.
        now_iso: The pass clock (ISO 8601). Age is measured against it, so a
            pass is deterministic and testable.
        max_age_s: Oldest acceptable evidence, in seconds. Anything that is not
            a finite positive number refuses rather than disabling the check.
    """
    if not _valid_max_age(max_age_s):
        return f"evidence max age invalid: {max_age_s!r}"
    now = _parse(now_iso) if isinstance(now_iso, str) else None
    if now is None:
        return "evidence unverifiable: the pass clock is not a timezone-aware timestamp"
    stamp = proposal.get("evidence")
    if not isinstance(stamp, dict):
        return "evidence missing: actuation needs evidence {source, observed_at}"
    source = stamp.get("source")
    if not isinstance(source, str) or not source.strip():
        return "evidence missing: no source artifact or observation id"
    if "observed_at" not in stamp:
        return "evidence missing: no observed_at timestamp"
    observed_at = stamp["observed_at"]
    observed = _parse(observed_at) if isinstance(observed_at, str) else None
    if observed is None:
        return (
            f"evidence unparseable: observed_at {observed_at!r} is not a timezone-aware "
            "ISO 8601 timestamp"
        )
    age = (now - observed).total_seconds()
    if age < -MAX_FUTURE_SKEW_SECONDS:
        return f"evidence timestamp is in the future: {observed_at} is {-age:.0f}s ahead"
    if age > max_age_s:
        return f"evidence stale: {observed_at} is {age:.0f}s old, max {max_age_s:g}s"
    return None


def ledger_ref(proposal: dict) -> str | None:
    """The audit string recorded on the ledger's first event.

    An explicit ``evidence_ref`` wins. Otherwise the structured stamp is
    rendered as ``<source>@<observed_at>`` so the ledger carries the time the
    action rested on, trimmed (source first) to the ledger field limit.
    """
    explicit = proposal.get("evidence_ref")
    if isinstance(explicit, str) and explicit:
        return explicit[:_LEDGER_REF_LIMIT]
    stamp = proposal.get("evidence")
    if not isinstance(stamp, dict):
        return None
    source, observed_at = stamp.get("source"), stamp.get("observed_at")
    if not isinstance(source, str) or not isinstance(observed_at, str):
        return None
    suffix = f"@{observed_at}"
    return source[: max(0, _LEDGER_REF_LIMIT - len(suffix))] + suffix


__all__ = ["DEFAULT_MAX_AGE_SECONDS", "MAX_FUTURE_SKEW_SECONDS", "ledger_ref", "refusal"]
