# Task 1: The pure decision module

**Files:**
- Create: `src/skcapstone/fleet/claim_expiry.py`
- Test: `tests/fleet/test_claim_expiry.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces:
  - `ClaimObservation` dataclass: `card_id: str`, `owner: str`, `claim_revision: str | None`, `last_owner_event_at: float` (unix seconds, 0.0 if unknown)
  - `ExpiryVerdict` dataclass: `card_id: str`, `owner: str`, `claim_revision: str`, `idle_seconds: float`, `reclaimable: bool`, `reason: str`
  - `evaluate(observations: Iterable[ClaimObservation], *, now: float, ttl_seconds: float) -> list[ExpiryVerdict]`
  - `ttl_seconds_from_env(env: Mapping[str, str]) -> float`
  - `mode_from_env(env: Mapping[str, str]) -> str` returning one of `"off" | "report" | "enforce"`

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from skcapstone.fleet.claim_expiry import (
    ClaimObservation, evaluate, ttl_seconds_from_env, mode_from_env,
)

HOUR = 3600.0
NOW = 1_000_000.0

def obs(cid="c1", owner="jarvis", rev="r1", last=None):
    return ClaimObservation(card_id=cid, owner=owner, claim_revision=rev,
                            last_owner_event_at=NOW - 50 * HOUR if last is None else last)

def test_idle_beyond_ttl_is_reclaimable():
    v = evaluate([obs()], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is True
    assert v.idle_seconds == pytest.approx(50 * HOUR)

def test_idle_within_ttl_is_not_reclaimable():
    v = evaluate([obs(last=NOW - 10 * HOUR)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "within-ttl"

def test_exactly_at_ttl_is_not_reclaimable():
    """The boundary is exclusive, so a claim is never reclaimed a moment early."""
    v = evaluate([obs(last=NOW - 48 * HOUR)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False

def test_bare_session_owner_is_reclaimable_not_skipped():
    """The case the current reaper cannot reach at all: an owner that does not
    look like a fleet worker. 146 of the 349 held claims look like this."""
    for name in ("jarvis", "codex", "seraph", "lumina", "tank"):
        v = evaluate([obs(owner=name)], now=NOW, ttl_seconds=48 * HOUR)[0]
        assert v.reclaimable is True, name

def test_missing_claim_revision_is_never_reclaimable():
    """Without a revision there is no CAS fence, so a release could race a
    re-claim. Refuse rather than risk it."""
    v = evaluate([obs(rev=None)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "no-claim-revision"

def test_unknown_last_event_is_never_reclaimable():
    v = evaluate([obs(last=0.0)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "no-owner-activity"

def test_future_timestamp_is_never_reclaimable():
    """Clock skew across hosts must not manufacture a reclaim."""
    v = evaluate([obs(last=NOW + 5 * HOUR)], now=NOW, ttl_seconds=48 * HOUR)[0]
    assert v.reclaimable is False
    assert v.reason == "future-timestamp"

def test_ttl_from_env_default_is_48h():
    assert ttl_seconds_from_env({}) == 48 * HOUR

def test_ttl_from_env_override():
    assert ttl_seconds_from_env({"SKFLEET_CLAIM_TTL_H": "6"}) == 6 * HOUR

def test_ttl_from_env_rejects_garbage_and_nonpositive():
    for bad in ("0", "-1", "abc", ""):
        assert ttl_seconds_from_env({"SKFLEET_CLAIM_TTL_H": bad}) == 48 * HOUR

def test_mode_defaults_off_and_unknown_is_off():
    assert mode_from_env({}) == "off"
    assert mode_from_env({"SKFLEET_CLAIM_TTL_MODE": "banana"}) == "off"
    assert mode_from_env({"SKFLEET_CLAIM_TTL_MODE": "ENFORCE"}) == "enforce"
    assert mode_from_env({"SKFLEET_CLAIM_TTL_MODE": " report "}) == "report"
```

- [ ] **Step 2: Run the tests, verify they fail**

Run: `python3 -m pytest tests/fleet/test_claim_expiry.py -q`
Expected: collection error, no module named `claim_expiry`.

- [ ] **Step 3: Implement the module**

```python
"""Decide which card claims are past their idle deadline.

Pure: no I/O, no clock, no environment beyond an explicitly passed mapping.
Every refusal is named in ``ExpiryVerdict.reason`` so the reaper's report
mode can explain itself without re-deriving anything.

The deadline is measured from the last event the OWNER wrote on the card it
holds, not from the claim timestamp. A worker doing real work writes move,
describe, evidence and verdict events continuously; one that has written
nothing for the whole TTL is dead or making no progress, and in both cases
the card should return to the pool. Measured on chi 2026-09-18, the 349
held claims have a minimum owner-idle of 30.9 hours, so a 48 hour deadline
separates them from live work with room to spare.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

#: Hours of owner inactivity before a claim may be reclaimed. Deliberately
#: generous: the failure is asymmetric. Too long leaves a card stuck, which
#: is the status quo; too short steals a card from a worker mid-run.
DEFAULT_TTL_HOURS = 48.0

_MODES = ("off", "report", "enforce")


@dataclass(frozen=True)
class ClaimObservation:
    """One held claim, as read from the CardStore fold plus its event log."""

    card_id: str
    owner: str
    claim_revision: str | None
    last_owner_event_at: float


@dataclass(frozen=True)
class ExpiryVerdict:
    card_id: str
    owner: str
    claim_revision: str
    idle_seconds: float
    reclaimable: bool
    reason: str


def ttl_seconds_from_env(env: Mapping[str, str]) -> float:
    raw = str(env.get("SKFLEET_CLAIM_TTL_H", "")).strip()
    try:
        hours = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TTL_HOURS * 3600.0
    if hours <= 0:
        return DEFAULT_TTL_HOURS * 3600.0
    return hours * 3600.0


def mode_from_env(env: Mapping[str, str]) -> str:
    raw = str(env.get("SKFLEET_CLAIM_TTL_MODE", "")).strip().lower()
    return raw if raw in _MODES else "off"


def evaluate(
    observations: Iterable[ClaimObservation], *, now: float, ttl_seconds: float
) -> list[ExpiryVerdict]:
    """Verdict per observation. Never raises on malformed input."""
    out: list[ExpiryVerdict] = []
    for o in observations:
        idle = now - float(o.last_owner_event_at or 0.0)
        reason = ""
        ok = False
        if not o.claim_revision:
            reason = "no-claim-revision"
        elif not o.last_owner_event_at:
            reason = "no-owner-activity"
        elif o.last_owner_event_at > now:
            reason = "future-timestamp"
        elif idle <= ttl_seconds:
            reason = "within-ttl"
        else:
            reason = "idle-beyond-ttl"
            ok = True
        out.append(
            ExpiryVerdict(
                card_id=o.card_id,
                owner=o.owner,
                claim_revision=o.claim_revision or "",
                idle_seconds=idle,
                reclaimable=ok,
                reason=reason,
            )
        )
    return out
```

- [ ] **Step 4: Run the tests, verify they pass**

Run: `python3 -m pytest tests/fleet/test_claim_expiry.py -q`
Expected: all pass.

- [ ] **Step 5: Format, lint, commit**

```bash
black src/skcapstone/fleet/claim_expiry.py tests/fleet/test_claim_expiry.py
ruff check src/skcapstone/fleet/claim_expiry.py
git add src/skcapstone/fleet/claim_expiry.py tests/fleet/test_claim_expiry.py
git commit -m "feat(fleet): pure claim-expiry decision module"
```

---

## Global Constraints (binding)

- **No new event field and no CardStore schema change.** The deadline is derived from existing events, so the mechanism applies retroactively to the 349 claims already held. A `claim_expires_at` field was considered and rejected: it would only help claims written after a fleet-wide deploy.
- **Never modify `reap_dead_claims()`'s existing gates.** Add a separate path. The absence-proof path must release exactly what it released before, with the same gates, or criterion 5 of the spec fails.
- **The new path must NOT call `_parse_worker_owner()`.** That function rejects any owner not shaped `pi-<lane>-<host>-<cid>` and accounts for the 146 largest-held claims. Routing the new path through it reproduces the bug.
- **Default is OFF.** `SKFLEET_CLAIM_TTL_MODE` defaults to `off`. Modes: `off`, `report`, `enforce`. Nothing reclaims until a human sets `enforce`.
- **TTL default 48 hours**, via `SKFLEET_CLAIM_TTL_H`. Reclaiming from a live worker is strictly worse than leaving a card stuck.
- Never write a long typographic dash (em or en) in code, comments, docstrings, or commit messages. Hyphens are fine.
- Every commit names the agent that did the work. Never add a `Co-Authored-By` you cannot evidence.
