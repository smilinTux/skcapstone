# ATLAS action-ledger custody

The ATLAS action ledger is durable governance evidence, not a cache. Physical
HONOR execution uses `~/.skcapstone/fleet/atlas/action-ledger/` and requires
CapAuth-signed lifecycle events. Each immutable intent binds the condition,
target, OperatorApp generation, ITIL change/authorization reference, CMDB CI,
verification contract, and typed rollback contract. Events are hash chained and
their signatures cover the actor, authorization reference inherited through the
intent, state, timestamp, detail, and previous hash.

## Retention

- Retain intent cores and event streams for at least seven years.
- Never prune an individual event from a stream. Archive the complete intent
  core and complete JSONL stream together.
- Terminal intents may move to yearly, read-only archives after 90 days. Active,
  failed, and escalated intents remain in the hot store.
- A retention job must run `ActionLedger.events(intent_id)` before and after a
  move. Any invalid chain or signature stops the entire archive batch.

## Replication and recovery

The fleet tree is replicated by the sovereign Syncthing mesh, while the normal
encrypted SKCapstone backup provides the independent third copy. Replication is
not verification: each peer verifies CapAuth signatures against its local trust
roster. Do not place private signing keys in the replicated ledger tree.

Recovery restores intent and event files as a pair, checks owner-only directory
and file permissions, then folds every stream. ATLAS remains frozen if a stream
is missing, truncated, unsigned, or fails its hash/signature checks. Never repair
a stream in place; preserve it as incident evidence and restore a known-good
complete copy.

## Rollback semantics

Rollback is explicit executable policy, not prose. A proposal eligible for
automatic rollback carries a typed `rollback.action` routed through the same
adapter boundary as forward actuation. Failed postcondition verification moves
the intent to `failed`; a proven rollback moves it to `rolled_back`. Missing or
failed rollback proof moves it to `escalated` and parks a human decision.
