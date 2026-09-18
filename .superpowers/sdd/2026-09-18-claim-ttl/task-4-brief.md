# Task 4: Wire the second release path into the reaper

**Files:**
- Modify: `scripts/fleet/skfleet-rotate.py` (inside `reap_dead_claims()`, at its END, after the existing loop completes)
- Test: `tests/fleet/test_claim_expiry_reaper.py`

**Interfaces:**
- Consumes: everything from Tasks 1 to 3.
- Produces: no new public interface.

**This is the task that must not break anything.** Read the constraints again before starting.

- [ ] **Step 1: Read the existing function**

Read `scripts/fleet/skfleet-rotate.py` around lines 4156 to 4330. Note that `DRY` is `"--go" not in sys.argv`, and that the function already shells out to `skcapstone coord release-claim` around line 4282. Reuse that exact invocation style; do not invent a second way to release.

- [ ] **Step 2: Write the failing test**

Test the new helper in isolation. `skfleet-rotate.py` is a flat script, so import the helper by loading the file via `importlib.util.spec_from_file_location` with `sys.argv` stubbed to avoid the module-scope `--go` block executing.

```python
def test_expiry_path_is_off_by_default(monkeypatch):
    monkeypatch.delenv("SKFLEET_CLAIM_TTL_MODE", raising=False)
    calls = []
    from skcapstone.fleet import claim_expiry
    # helper under test releases nothing when mode is off
    released = _run_expiry_path(observations=[...], runner=calls.append)
    assert released == 0 and calls == []

def test_report_mode_logs_but_releases_nothing(monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "report")
    calls = []
    released = _run_expiry_path(observations=[...], runner=calls.append)
    assert released == 0 and calls == []

def test_enforce_mode_releases_with_cas_fence(monkeypatch):
    monkeypatch.setenv("SKFLEET_CLAIM_TTL_MODE", "enforce")
    calls = []
    _run_expiry_path(observations=[...], runner=calls.append)
    assert len(calls) == 1
    cmd = calls[0]
    assert "release-claim" in cmd
    assert "--expected-claim-revision" in cmd   # CAS fence present
    assert "--owner" in cmd

def test_enforce_skips_a_card_the_absence_path_already_released(monkeypatch):
    """The two paths must never both release the same generation."""
```

- [ ] **Step 3: Implement**

Add a module-level helper near `reap_dead_claims()`, then call it on the last line of `reap_dead_claims()`. Structure:

```python
def _expire_idle_claims():
    """Second, independent release path: reclaim a claim whose OWNER has
    stopped touching the card.

    Deliberately does NOT consult _parse_worker_owner(): that gate only
    recognizes pi-<lane>-<host>-<cid> shaped owners and skips everything
    else before any liveness logic, which is why 146 of the 349 claims held
    on chi (jarvis 104, codex 28, seraph 5) are unreachable by the existing
    path. Nor does it require cross-host authority: an absolute idle
    deadline needs no host to prove absence, which is the point.
    """
    from skcapstone.fleet.claim_expiry import (
        evaluate, mode_from_env, observe, ttl_seconds_from_env,
    )

    mode = mode_from_env(os.environ)
    if mode == "off":
        return 0
    verdicts = [v for v in evaluate(observe(HOME), now=time.time(),
                                    ttl_seconds=ttl_seconds_from_env(os.environ))
                if v.reclaimable]
    if mode == "report":
        for v in verdicts:
            log(d, "CLAIM_TTL_WOULD_RECLAIM|%s|%s|%s|idle=%.1fh"
                   % (HOST, v.card_id, v.owner, v.idle_seconds / 3600.0))
        return 0
    released = 0
    for v in verdicts:
        # same invocation the absence path uses; the CAS fence makes a
        # racing re-claim a refusal rather than a theft
        ...
        released += 1
    return released
```

Use the same `HOME`, `log`, `d`, `HOST` names the surrounding script already uses. Verify each exists before referencing it.

- [ ] **Step 4: Prove the existing path is unchanged**

Run the existing reaper tests. Then confirm by inspection that no line inside the original loop was modified: `git diff` on the function must show only an addition at the end plus the new helper.

- [ ] **Step 5: Run the full fleet suite**

Run: `python3 -m pytest tests/fleet/ -q`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(fleet): reclaim claims idle beyond TTL, default off"
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
