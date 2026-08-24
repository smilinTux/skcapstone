# SKCP-00F3D board mutation hardening evidence

Card: `8ab522ee`

## Result

SKCoord now uses one documented local mutation order: board lock, sorted
affected card locks, then a lifecycle-only lock when needed. Claim locks both
the requested card and any card displaced from `current_task`. Claim, complete,
release, stale-release, and lifecycle transition use that order. Joule minting
runs after the completion locks have been released.

CardStore claim events receive a `claim_revision`. A `release_claim` event
names `released_owner` and `expected_claim_revision`; the fold applies it only
when both still match. A stale cross-host release therefore keeps the newer
owner and records a deterministic fold conflict. Local `flock` is documented
as same-filesystem coordination only, not distributed locking.

Agent projection snapshots and recovery records use no-follow directory
descriptors, regular single-link checks, fsync, atomic replacement, and parent
directory fsync. CardStore core and event files, lifecycle overlay event files,
and raw task mutation inputs use descriptor-pinned no-follow opens with
post-open regular-file and link-count checks. Temporary raw-byte compensation
files are fsynced, compared to their opened inode, replaced atomically, then
the parent directory is fsynced.

Every claim, complete, release, and label mirror has a deterministic transition
ID. If an append writes and then reports an error, the caller treats it as
successful only when the exact event is present and the CardStore fold equals
the requested owner and column. A missing or conflicting event produces a
durable recovery record with those IDs before exact legacy-byte restoration.
Different-owner cross-host claims now fold as explicit `claim_conflicts` and
block completion. Claim and complete mirror failures propagate rather than
being silently logged. Staged label additions and removals mirror as CardStore
events in store and dual modes. A label mirror error restores the exact task
bytes and compensates any earlier label events from the same mutation.

## Verification

Focused hardening coverage passed:

```text
153 passed in 2.88s
```

This includes eight-process dependency and release idempotency, forced
claim/release and complete/release races, same-owner different-card release,
write-then-error claim, complete, release, and remove-label behavior, raw-byte
compensation, recovery failure, stale release preconditions, cross-host claim
conflict, staged label parity in modes `1`, `0`, and `dual`, Joule reentry,
lock timeout, invalid IDs, and open-time symlink and hardlink rejection.

Full SKCoord verification passed:

```text
402 passed in 4.32s
ruff for SKCoord and SKCapstone: All checks passed
git diff --check: passed
```

Full SKCapstone verification was run under a 900-second bound after the
previous executor detached a test child. The exact suspected blueprint test
also passed under a 90-second bound. The bounded full result was:

```text
6405 passed, 38 skipped, 554 warnings in 357.85s
```

The warnings were existing Pydantic and PGP deprecation or verification
warnings. No test failed.

The local `.skenv` was refreshed with:

```text
/home/skuser01/.skenv/bin/python -m pip install -e /home/skuser01/work/skcoord -e /home/skuser01/work/skcapstone
```

Installed imports resolve to the editable source directories. Read-only
installed Kanban confirms `d0edbff1` folds into `d12b8951`, `94cbf19a`, and
`f0c63c2a`; F4 `90a02b0e` and this correction card were still not done during
the check. The rollback command below yields the same three folded
dependencies. A separate installed temporary fixture passed `coord parity
--check` with `checked=1`, `matched=1`, `mismatches=0`, `missing=0`, and zero
open drift. Its read-only `coord reconcile-agents` audit returned `clean: true`
with one card and one agent.

The shared live board's read-only global parity command remains outside this
card's scope: it reported 125 pre-existing mismatches, 270 missing cards, and
open drift 10 before this card was completed. No migrate or reconcile mutation
was authorized or run. This does not affect the local isolated parity proof or
the clean live lifecycle audit.

## Rollback and preservation

Runtime rollback is executable without rewriting history:

```text
SKCOORD_CARD_STORE=0 skcapstone coord kanban --json
```

That read-only rollback mode still folds all three `d0edbff1` dependencies.
The local editable activation can be repeated with the documented editable
install command after a qualified source rollback. Candidate manifest SHA256
remains
`88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`.

No original candidate artifact, implementation source, production service,
live implementation claim, deployment, restart, commit, or push was changed.
