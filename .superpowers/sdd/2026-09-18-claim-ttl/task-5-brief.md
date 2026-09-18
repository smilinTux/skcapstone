# Task 5: Package the entry point

**Files:**
- Modify: `pyproject.toml`
- Test: `tests/fleet/test_claim_expiry_packaging.py`

Three hand-maintained lists have each caused the same class of failure in this repo (`script-files`, `ALL_UNITS`, `install.sh`). A new CLI that is not packaged is a mechanism nothing can call.

- [ ] **Step 1: Write the failing test**

```python
def test_claim_expiry_cli_has_a_console_script():
    import tomllib, pathlib
    data = tomllib.loads(pathlib.Path("pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"].get("scripts", {})
    assert any("claim_expiry_cli" in v for v in scripts.values()), scripts
```

- [ ] **Step 2: Run, verify failure**
- [ ] **Step 3: Add the console script to `pyproject.toml`**
- [ ] **Step 4: Run, verify pass**
- [ ] **Step 5: Commit**

---

## Global Constraints (binding)

- **No new event field and no CardStore schema change.** The deadline is derived from existing events, so the mechanism applies retroactively to the 349 claims already held. A `claim_expires_at` field was considered and rejected: it would only help claims written after a fleet-wide deploy.
- **Never modify `reap_dead_claims()`'s existing gates.** Add a separate path. The absence-proof path must release exactly what it released before, with the same gates, or criterion 5 of the spec fails.
- **The new path must NOT call `_parse_worker_owner()`.** That function rejects any owner not shaped `pi-<lane>-<host>-<cid>` and accounts for the 146 largest-held claims. Routing the new path through it reproduces the bug.
- **Default is OFF.** `SKFLEET_CLAIM_TTL_MODE` defaults to `off`. Modes: `off`, `report`, `enforce`. Nothing reclaims until a human sets `enforce`.
- **TTL default 48 hours**, via `SKFLEET_CLAIM_TTL_H`. Reclaiming from a live worker is strictly worse than leaving a card stuck.
- Never write a long typographic dash (em or en) in code, comments, docstrings, or commit messages. Hyphens are fine.
- Every commit names the agent that did the work. Never add a `Co-Authored-By` you cannot evidence.
