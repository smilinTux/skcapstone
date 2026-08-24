# SKCP-00F3C release recovery evidence

Card `a081d5ed` changes release to one append-only `release_claim` event that
folds owner removal and backlog atomically. Mutation order is bounded shared
board lock, then bounded per-card lock. The board lock rejects timeout after
five seconds. Legacy agent bytes are snapshotted and restored exactly if event
append fails. If restoration fails, a durable recovery JSONL record is written
and the operation fails closed.

Focused dependency, release, CLI, lifecycle, and concurrent-process coverage
passed: `20 passed in 0.74s`. Editable SKCoord and SKCapstone were refreshed in
the local `.skenv`; installed Kanban read-only JSON confirms all three legacy
cards fold `d0edbff1`. No live claim probe, implementation claim, production
deployment, service restart, candidate artifact edit, commit, or push occurred.

Rollback is `pip install skcoord==0.1.32 skcapstone==0.15.49`; the append-only
board records remain intact.
