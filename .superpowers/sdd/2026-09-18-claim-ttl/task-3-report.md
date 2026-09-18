# Task 3 report: read-only claim-expiry report CLI

## Summary

Implemented `src/skcapstone/fleet/claim_expiry_cli.py` per the brief's Step 3
code sample, essentially unchanged (argparse with `--home`, `--json`,
`--reclaimable-only`; always exits 0; never writes). Test file
`tests/fleet/test_claim_expiry_cli.py` implements the brief's two required
tests plus four more: `--reclaimable-only` filters shown rows but keeps
fleet-wide counts, text mode is distinguishable from JSON mode, a run never
creates or modifies anything under `--home` (the read-only guarantee, checked
directly rather than assumed), and mode is read from
`SKFLEET_CLAIM_TTL_MODE`.

## Commits

- `431cb7c9` feat(fleet): read-only claim-expiry report CLI
- `13389050` test(fleet): exempt claim_expiry_cli.py's ~/.skcapstone default

## What the brief got wrong, and what I did about it

1. **The sibling-test import worked, contrary to the "may not resolve"
   warning.** `from tests.fleet.test_claim_expiry_observe import _card, _iso`
   resolves cleanly under this repo's pytest config
   (`pythonpath = ["src"]`, `testpaths = ["tests"]`, and `tests/__init__.py` +
   `tests/fleet/__init__.py` both exist, so pytest's rootdir-package
   insertion puts the worktree root on `sys.path`). Verified with a bare
   `python3 -c` import and then by running the observe test file standalone
   (7 passed) before writing anything. Used the sibling import as the brief
   specified rather than duplicating the helpers.

2. **The brief's file list did not mention `tests/fleet/test_root_relocation.py`,
   and the full fleet suite failed against it.** That test enforces that any
   new `~/.skcapstone`-referencing module in `src/skcapstone/fleet/` is on a
   named, justified exemption list (`_SKCAPSTONE_PATH_EXEMPT`), because a
   hardcoded path there could silently leave state behind when
   `SKFLEET_ROOT` is relocated. `claim_expiry_cli.py`'s default `--home` is
   `~/.skcapstone` (the CardStore root, not the fleet tree), so it tripped
   the guardrail. Fixed by adding a one-line justified entry, following the
   test's own documented process and the precedent of the existing
   `bounded_artifact_discovery.py` entry (also coordination state, not fleet
   state). This is a genuinely new file with the same shape as that
   precedent, not a workaround.

## Test counts

- `tests/fleet/test_claim_expiry_cli.py`: 6 passed
- Full `tests/fleet/` suite (1422 tests) after the exemption fix: 1422 passed,
  0 failed, 14 warnings (pre-existing pgpy self-sig/revocation warnings,
  unrelated to this change)
- Before the exemption fix: 1421 passed, 1 failed
  (`test_any_other_skcapstone_path_is_a_named_exemption`)

## Mutation check

Broke the `--reclaimable-only` filter condition by inverting it
(`if v.reclaimable` to `if not v.reclaimable`) in
`src/skcapstone/fleet/claim_expiry_cli.py`, line 32:

```
shown = [v for v in verdicts if not v.reclaimable] if args.reclaimable_only else verdicts
```

Ran the targeted test file against the mutant:

```
$ python3 -m pytest tests/fleet/test_claim_expiry_cli.py -q
collected 6 items

tests/fleet/test_claim_expiry_cli.py ..F...                              [100%]

=================================== FAILURES ===================================
_____________________ test_reclaimable_only_filters_output _____________________
tests/fleet/test_claim_expiry_cli.py:84: in test_reclaimable_only_filters_output
    assert ids == {"aaaa1111"}
E   AssertionError: assert {'bbbb2222'} == {'aaaa1111'}
E
E     Extra items in the left set:
E     'bbbb2222'
E     Extra items in the right set:
E     'aaaa1111'
E     Use -v to get more diff
=========================== short test summary info ============================
FAILED tests/fleet/test_claim_expiry_cli.py::test_reclaimable_only_filters_output
========================= 1 failed, 5 passed in 0.89s ==========================
```

Restored the original file (verified via `git status --short` showing no diff
against the committed version) and re-ran clean:

```
$ python3 -m pytest tests/fleet/test_claim_expiry_cli.py -q
collected 6 items

tests/fleet/test_claim_expiry_cli.py ......                              [100%]

============================== 6 passed in 0.63s ===============================
```

## Formatting / linting

`black` and `ruff check` clean on both `src/skcapstone/fleet/claim_expiry_cli.py`
and `tests/fleet/test_claim_expiry_cli.py` (plus `test_root_relocation.py`
after the exemption edit). Checked for em/en dashes with
`grep -nP '[\x{2013}\x{2014}]'` on every file I touched: none found.

## Scope discipline

Did not touch `reap_dead_claims()`, `_parse_worker_owner()`, or
`pyproject.toml` (Task 5's console-script entry point is intentionally left
undone). `if __name__ == "__main__": raise SystemExit(main())` guard is
present and `main(argv)` is directly callable.

## Uncommitted, out of scope

`.superpowers/sdd/2026-09-18-claim-ttl/progress.md` shows as modified in
`git status` but predates this session (Task 1+2 controller notes) and was
left untouched.
