# Task 2: Read the fleet's held claims

**Files:**
- Modify: `src/skcapstone/fleet/claim_expiry.py`
- Test: `tests/fleet/test_claim_expiry_observe.py`

**Interfaces:**
- Consumes: `ClaimObservation` from Task 1.
- Produces: `observe(home: Path) -> list[ClaimObservation]`

- [ ] **Step 1: Write the failing test**

Build a real CardStore in `tmp_path` rather than mocking, so the test pins the actual on-disk shape.

```python
import json, time
from pathlib import Path
import pytest
from skcapstone.fleet.claim_expiry import observe

def _card(home: Path, cid: str, events: list[dict]) -> None:
    d = home / "cards" / cid / "events"
    d.mkdir(parents=True, exist_ok=True)
    with (d / "0001.jsonl").open("w", encoding="utf-8") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")

def _iso(offset_h: float) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=offset_h)).isoformat()

def test_observe_reports_held_claim_with_owner_activity(tmp_path):
    _card(tmp_path, "aaaa1111", [
        {"action": "claim", "owner": "jarvis", "writer": "jarvis",
         "claim_revision": "rev1", "ts": _iso(-100)},
        {"action": "move", "writer": "jarvis", "ts": _iso(-60)},
    ])
    got = {o.card_id: o for o in observe(tmp_path)}
    assert "aaaa1111" in got
    o = got["aaaa1111"]
    assert o.owner == "jarvis"
    assert o.claim_revision == "rev1"
    # last OWNER event is the move at -60h, not the claim at -100h
    assert o.last_owner_event_at == pytest.approx(time.time() - 60 * 3600, abs=120)

def test_observe_ignores_a_released_claim(tmp_path):
    _card(tmp_path, "bbbb2222", [
        {"action": "claim", "owner": "jarvis", "writer": "jarvis",
         "claim_revision": "rev1", "ts": _iso(-100)},
        {"action": "release_claim", "released_owner": "jarvis", "writer": "mero",
         "expected_claim_revision": "rev1", "ts": _iso(-90)},
    ])
    assert [o.card_id for o in observe(tmp_path)] == []

def test_observe_ignores_a_completed_card(tmp_path):
    _card(tmp_path, "cccc3333", [
        {"action": "claim", "owner": "jarvis", "writer": "jarvis",
         "claim_revision": "rev1", "ts": _iso(-100)},
        {"action": "complete", "writer": "jarvis", "ts": _iso(-90)},
    ])
    assert [o.card_id for o in observe(tmp_path)] == []

def test_observe_uses_the_latest_claim_generation(tmp_path):
    """A re-claim by the same owner supersedes the first. The revision
    reported must be the newest, or the CAS fence targets a dead generation."""
    _card(tmp_path, "dddd4444", [
        {"action": "claim", "owner": "w1", "writer": "w1",
         "claim_revision": "old", "ts": _iso(-100)},
        {"action": "claim", "owner": "w1", "writer": "w1",
         "claim_revision": "new", "ts": _iso(-99)},
    ])
    o = observe(tmp_path)[0]
    assert o.claim_revision == "new"

def test_observe_survives_a_corrupt_line(tmp_path):
    d = tmp_path / "cards" / "eeee5555" / "events"
    d.mkdir(parents=True)
    (d / "0001.jsonl").write_text(
        "not json\n"
        + json.dumps({"action": "claim", "owner": "w2", "writer": "w2",
                      "claim_revision": "r", "ts": _iso(-100)}) + "\n",
        encoding="utf-8")
    assert observe(tmp_path)[0].owner == "w2"

def test_observe_on_missing_tree_returns_empty(tmp_path):
    assert observe(tmp_path / "nope") == []
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m pytest tests/fleet/test_claim_expiry_observe.py -q`
Expected: ImportError on `observe`.

- [ ] **Step 3: Implement `observe`**

Prefer the real fold. Import `CardStore` from `skcoord.card_store` inside the function so the module stays importable without skcoord present, and fall back to a local replay when the fold is unavailable. Append to `claim_expiry.py`:

```python
def _parse_ts(value: object) -> float:
    from datetime import datetime

    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


_TERMINAL = {"complete", "void", "archive"}
_RELEASE = {"release_claim", "unassign"}


def observe(home) -> list[ClaimObservation]:
    """Every claim the store currently reports as held, with owner idleness.

    Reads the event log directly rather than only the fold, because the
    deadline needs the last event the OWNER wrote, which the fold does not
    retain. Ownership itself is decided by replaying the claim/release pairs
    in sequence order, which is what the fold does: counting claim events
    against release events overcounts, since a worker re-claiming a card it
    already holds writes a second claim that one release settles.
    """
    from pathlib import Path

    root = Path(home) / "cards"
    if not root.is_dir():
        return []

    out: list[ClaimObservation] = []
    for card_dir in sorted(root.iterdir()):
        events_dir = card_dir / "events"
        if not events_dir.is_dir():
            continue
        events: list[dict] = []
        try:
            names = sorted(events_dir.iterdir())
        except OSError:
            continue
        for fn in names:
            try:
                fh = fn.open(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            with fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(e, dict):
                        events.append(e)
        if not events:
            continue
        events.sort(key=lambda e: (e.get("seq") or 0, _parse_ts(e.get("ts"))))

        owner: str | None = None
        revision: str | None = None
        terminal = False
        for e in events:
            action = e.get("action")
            if action in _TERMINAL:
                terminal = True
                break
            if action == "claim" and e.get("owner"):
                owner = str(e["owner"])
                revision = e.get("claim_revision") or revision
            elif action in _RELEASE:
                owner = None
                revision = None
        if terminal or not owner:
            continue

        last_owner = 0.0
        for e in events:
            if e.get("writer") == owner or e.get("owner") == owner:
                t = _parse_ts(e.get("ts"))
                if t > last_owner:
                    last_owner = t
        out.append(
            ClaimObservation(
                card_id=card_dir.name,
                owner=owner,
                claim_revision=revision,
                last_owner_event_at=last_owner,
            )
        )
    return out
```

Add `import json` to the module's imports.

- [ ] **Step 4: Run, verify pass**

Run: `python3 -m pytest tests/fleet/test_claim_expiry_observe.py tests/fleet/test_claim_expiry.py -q`
Expected: all pass.

- [ ] **Step 5: Validate against the real store, read-only**

The fold on chiap01 reports 349 held claims. Run locally against a COPY only if one is available; otherwise record that validation happens at deploy time. Never write during this step.

- [ ] **Step 6: Commit**

```bash
black src/skcapstone/fleet/claim_expiry.py tests/fleet/
ruff check src/skcapstone/fleet/claim_expiry.py
git add -A src/skcapstone/fleet/claim_expiry.py tests/fleet/test_claim_expiry_observe.py
git commit -m "feat(fleet): observe held claims and owner idleness"
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
