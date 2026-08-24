# SKCP-00F3E partial recovery and read safety evidence

Card: `50e36b06`

## Result

This correction closes three final board-runtime review findings.

When a new target claim becomes durable and the paired bumped-card demotion
fails before append, the board now snapshots the target CardStore card and uses
the known claim revision to compensate it. The compensation releases the
durable target claim, restores a prior owner if there was one, and restores the
prior column and order before the raw legacy agent bytes are restored. The
recovery record retains every primary and compensation transition ID. If that
compensation cannot reach the captured CardStore state, the operation remains
fail-closed and emits recovery evidence.

A durable completion append that reports an error now continues through the
normal post-lock Joule-mint path. Completion first detects an already completed
task across the agent projection and returns it without another CardStore event
or mint. The first durable completion therefore mints once, and a retry mints
nothing.

CardStore core reads, event enumeration, card ID listing, and CardEventLog
reads now pin directories with no-follow descriptors. Each source file is
checked before and after open as a regular single-link file. Symlinked and
hardlinked external core, event, directory, and overlay content are rejected.

## Verification

The exact final-review regression suite passed:

```text
36 passed in 1.17s
```

It includes a durable target claim plus pre-append demote failure, a durable
complete-after-error mint followed by a no-mint retry, and CardStore and
CardEventLog external symlink and hardlink read probes.

Broader coordination and relevant SKCapstone coverage passed:

```text
192 passed in 4.28s
```

Full SKCoord verification passed:

```text
406 passed in 3.79s
ruff for SKCoord and SKCapstone: All checks passed
git diff --check: passed
```

No SKCapstone source changed for this narrow correction. The previously
qualified bounded SKCapstone suite remains applicable:

```text
6405 passed, 38 skipped, 554 warnings in 357.85s
```

The local `.skenv` editable activation was refreshed with:

```text
/home/skuser01/.skenv/bin/python -m pip install -e /home/skuser01/work/skcoord -e /home/skuser01/work/skcapstone
```

Read-only installed Kanban confirmed `d0edbff1` folds into `d12b8951`,
`94cbf19a`, and `f0c63c2a`. F4 `90a02b0e` remained not done during this check.
A temporary installed fixture passed `coord parity --check` with one matched
card, zero mismatches, zero missing cards, and zero open drift. Its read-only
`coord reconcile-agents` result was clean with one card and one agent.

## Rollback and preservation

The CardStore rollback remains executable without rewriting history:

```text
SKCOORD_CARD_STORE=0 skcapstone coord kanban --json
```

That read-only mode still folds all three `d0edbff1` dependencies. Candidate
manifest SHA256 remains
`88b78aece092291535177414f159925ce997083c7c51134ed184495c8b9318d3`.

No candidate artifact, implementation claim, production deployment, service
restart, commit, or push occurred.
