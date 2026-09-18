# Task 3: A reporting CLI

**Files:**
- Create: `src/skcapstone/fleet/claim_expiry_cli.py`
- Test: `tests/fleet/test_claim_expiry_cli.py`

**Interfaces:**
- Consumes: `observe`, `evaluate`, `ttl_seconds_from_env`, `mode_from_env` from Tasks 1 and 2.
- Produces: `main(argv: list[str] | None = None) -> int`

This ships before any enforcement so phase 2 of the rollout has a tool. It never writes.

- [ ] **Step 1: Write the failing test**

```python
import json
from skcapstone.fleet import claim_expiry_cli

def test_reports_reclaimable_as_json(tmp_path, capsys, monkeypatch):
    from tests.fleet.test_claim_expiry_observe import _card, _iso
    _card(tmp_path, "aaaa1111", [
        {"action": "claim", "owner": "jarvis", "writer": "jarvis",
         "claim_revision": "rev1", "ts": _iso(-100)},
    ])
    _card(tmp_path, "bbbb2222", [
        {"action": "claim", "owner": "fresh", "writer": "fresh",
         "claim_revision": "rev2", "ts": _iso(-1)},
    ])
    rc = claim_expiry_cli.main(["--home", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    ids = {r["card_id"]: r for r in payload["verdicts"]}
    assert ids["aaaa1111"]["reclaimable"] is True
    assert ids["bbbb2222"]["reclaimable"] is False
    assert payload["mode"] == "off"
    assert payload["reclaimable_count"] == 1

def test_exit_code_is_zero_even_with_findings(tmp_path):
    """A report is not a failure. Nothing in CI should go red because the
    fleet has expired claims."""
    assert claim_expiry_cli.main(["--home", str(tmp_path), "--json"]) == 0
```

- [ ] **Step 2: Run, verify failure**

Run: `python3 -m pytest tests/fleet/test_claim_expiry_cli.py -q`

- [ ] **Step 3: Implement**

```python
"""Report which claims are past their idle deadline. Never writes."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from .claim_expiry import evaluate, mode_from_env, observe, ttl_seconds_from_env


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="skfleet-claim-expiry")
    ap.add_argument("--home", default=os.path.expanduser("~/.skcapstone"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--reclaimable-only", action="store_true")
    args = ap.parse_args(argv)

    ttl = ttl_seconds_from_env(os.environ)
    mode = mode_from_env(os.environ)
    verdicts = evaluate(observe(Path(args.home)), now=time.time(), ttl_seconds=ttl)
    shown = [v for v in verdicts if v.reclaimable] if args.reclaimable_only else verdicts

    if args.json:
        print(json.dumps({
            "mode": mode,
            "ttl_hours": ttl / 3600.0,
            "held_count": len(verdicts),
            "reclaimable_count": sum(1 for v in verdicts if v.reclaimable),
            "verdicts": [asdict(v) for v in shown],
        }, indent=2))
        return 0

    print(f"mode={mode} ttl={ttl / 3600.0:.1f}h held={len(verdicts)} "
          f"reclaimable={sum(1 for v in verdicts if v.reclaimable)}")
    for v in sorted(shown, key=lambda x: -x.idle_seconds):
        flag = "RECLAIM" if v.reclaimable else "hold   "
        print(f"  {flag} {v.card_id}  idle={v.idle_seconds / 3600.0:7.1f}h  "
              f"{v.owner[:34]:34} {v.reason}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 4: Run, verify pass**

- [ ] **Step 5: Commit**

```bash
black src/skcapstone/fleet/claim_expiry_cli.py tests/fleet/test_claim_expiry_cli.py
ruff check src/skcapstone/fleet/claim_expiry_cli.py
git add -A src/skcapstone/fleet/claim_expiry_cli.py tests/fleet/test_claim_expiry_cli.py
git commit -m "feat(fleet): read-only claim-expiry report"
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
