# c1c1e001 cycle snapshot and finalization evidence

## Scope and source

- Source card: `c1c1e001`
- Producer: `codex-c1c1e001`
- Repository: `https://github.com/smilinTux/skcapstone.git`
- Base ref: `main`
- Exact base revision: `723e6a989b2c5deafcee26f2514739f9dfd7c2e0`
- Worktree: fresh isolated task worktree, never the installed runtime tree
- Runtime and service mutation: none

## Immutable failure reproduction

A read-only journal query for `skfleet-seraph.service` from
`2026-09-11 23:01:00` through `23:36:00` local time returned exactly seven
`skfleet-rotate.py --go` failures at the configured 240 second timeout. The
filtered start, timeout, and failed-result rows have SHA-256:

`9152690822ff0b80d65e77254ed2026b62ba763b2ebb405e891a4adc613e3796`

The read-only `db1d2d07` worker journal shows that terminal processing reached
the exact-generation release path and then raised `TimeoutError: timed out
acquiring board mutation lock`. The filtered queue failure, finalizer stack,
lock failure, and unit result rows have SHA-256:

`9f296802099dba903a8a051184b58bd0acf62d72129c32f1fcf282997b737e3f`

Existing immutable worker exit receipts were not changed:

- `db1d2d07-0736c6587f2c90e0.json`: `2770dc39601af82e3efa451e8e697ec91320a94282f3e3d2771ce9eca749971f`
- `db1d2d07-e4cb10ce727f9b52.json`: `8b53749eedd5d9209e8e36edb6f69f2c9e4fca75b12bc5b4e356a14c48d56050`

## Root cause and repair

Each rotation repeatedly walked the same CardStore cores and native event
streams for legacy selection, dependency scoring, reaping, review discovery,
and POOL_V2. Concurrent Seraph, Niobe, and host rotations multiplied the same
shared filesystem work. One cycle now captures a bounded immutable CardStore
snapshot, logs its generation, card count, and byte count, and reuses detached
values for equivalent selector scans. Every pre-claim admission check still
bypasses the snapshot and reads live state, so stale input cannot grant a
claim.

The worker wrapper already wrote its terminal exit receipt before finalizing
capacity. A board-lock timeout after that point could still escape and lose the
release attempt. It now writes an immutable, deterministic retry request that
binds card, owner, and claim revision. Later worker starts reconcile at most
eight pending requests through the existing Board compare-and-swap release and
write a separately hashed completion receipt. A deferred release keeps the
owner projection and capacity occupied.

## Claim-to-projection publication race

At the requested read-only audit, card `6dd138ad` was doing under
`pi-codex-review-chiap03-6dd138ad` at claim revision
`258082bd2d6a4dc49c22577dc619e79b`. The `chiap03` fleet-live report carried
that exact tuple while the synced owner projection was still idle. Their bytes
at observation time hashed to:

- fleet-live report: `d9a6c6f35b0a70ab05cbe283a37e239d487367b272534cd1c6e341c6d711be14`
- idle owner projection: `5ea899fbce82a09144f35d6b091af441674e5705d2df45cacc5bf81da4a15cab`

Snapshotting does not remove cross-host publication ordering. The existing
fresh fleet-live fence does cover it: the reaper checks the running card set
before any release path. A focused regression uses the exact observed card,
owner, and generation and proves that no release is attempted while the owner
projection is idle but the worker is live. The live card and worker were never
mutated.

## Acceptance checks

- Focused snapshot, finalizer, claimability, POOL_V2, and seat checks: 132 passed.
- Worker exit, startup, Seraph, Niobe, and seat integration checks: 156 passed.
- Broad fleet regression: 814 passed, 1 environment-dependent failure. The
  unrelated Pi baseline test expected 162 direct MCP tools, while the installed
  Pi runtime exposed the newer MCP proxy tool set. The failure reproduces alone.
- Focused reaper, review opener, and scheduler regression after the publication
  race test: 73 passed.
- Final affected acceptance set after all repairs: 332 passed.
- Ruff on changed Python modules and tests: passed.
- Black check on changed Python modules and tests: passed.
- `git diff --check`: passed.

## Rollback

Revert the source commit. No migration, runtime installation, service change,
worker termination, card mutation, or data rewrite is required. Existing retry
requests are immutable evidence; an older runtime ignores their dedicated
directory. Any claim they name remains protected by its original exact
generation fence.
