# Completion Evidence — Card e37a5a0d

**Title:** `[SKFLEET][M] Retire interrupted remote review generations safely`
**Owner:** `pi-deepseek-builder-node-chiap02-e37a5a0d`
**Claim revision:** `d4dfc2590f1e40ef942a378462f992cb`
**Authorized base:** `35047872f8324cda2963aca2666235ec32a177d1`
**Repository:** https://github.com/smilinTux/skcapstone

## Problem

PR #960 introduced prestart retirement. `production_admission._transaction`
creates `fleet/resource-admission/<host>/.lock` (a private regular file) for
every production admission transaction. `review_retire._admission_inventory`
iterated that directory and classified any non-directory entry as
`resource admission inventory is ambiguous`, so every chiap03 review prestart
retirement was refused as soon as an admission transaction had run.

## Change

- `src/skcapstone/fleet/production_admission.py`: extracted the lock-shape
  predicate used by `_transaction` into `_private_lock_stat(info)`. The
  behavior for a lock opened during a transaction is unchanged (still requires
  a regular file, owned by the current uid, no group/other bits, single link).
- `src/skcapstone/fleet/review_retire.py`: `_admission_inventory` now
  recognizes the exact `.lock` basename. A safe private lock (per
  `_private_lock_stat`) is skipped and does not enter the inventory digest. A
  `.lock` that is a symlink, non-regular, foreign-owned, group/world-readable,
  or multiply linked still fails closed with
  `resource admission lock is unsafe`. All other malformed inventory entries
  keep the existing fail-closed validation.
- `tests/fleet/test_review_retire.py`: added
  `test_prestart_inventory_ignores_private_admission_lock` and
  `test_prestart_inventory_rejects_unsafe_admission_lock`.
- `changelog.d/admission-lock-20261006.md`: changelog fragment.

## Criteria mapping

1. Only the private production admission lock is excluded from reservation
   inventory; unsafe locks and matching intents still fail closed.
   — Implemented in `_admission_inventory`; covered by the two new tests plus
   the existing `test_prestart_retirement_refuses_matching_resource_intent`.
2. A launched review with stale running status is retired only with exact
   request, launch and release binding, absent/failed terminal unit, no
   matching tmux session or admission intent, and archived terminal evidence.
   — Pre-existing `retire`/`retire_prestart` logic retained; covered by the
   existing tests in the TDD file.
3. When a transient unit has been collected, journal start and completion
   records must match the exact unit and invocation; no review packet or
   terminal custody may exist. — Covered by the existing `retire` validation
   and TDD tests.
4. No status or request pointer is cleared until check-only validates; receipts
   and coord events make retirement independently verifiable. — Unchanged
   check-only-then-apply flow; `already-retired` recovery covered by TDD.
5. Focused retirement and admission tests pass. — See commands below.

## Commands run

```text
$ python -m pytest tests/fleet/test_review_retire.py -q
7 passed in 0.30s

$ python -m pytest tests/fleet/test_production_admission.py tests/fleet/test_admission.py -q
33 passed in 0.46s
```

## Limitations / rollback

- Scope limited to the private lock exclusion in the reservation inventory; no
  other admission semantics changed.
- Rollback: revert this commit; the previous fail-closed behavior returns.
- The auto-merge PR criterion (#5) is handled by the fleet PR process; this
  workspace records the committed candidate and its evidence.
