# ATLAS governed action lifecycle

ATLAS records every actionable proposal as an immutable `ActionIntent` core and
an append-only, hash-chained event stream. The ledger is durable evidence; it
does not replace CapAuth, OperatorApp ratification, CMDB, or ITIL authorization.

```text
observed -> diagnosed -> proposed -> authorized -> executing -> verified
                                                    |
                                                    +-> failed -> rolled_back
                                                               -> escalated
```

Skipped, repeated, and post-terminal transitions fail closed. Each intent binds:

- the owning application, typed target, action, and triggering condition fingerprint;
- the action-catalog generation used when planning;
- optional authoritative ITIL change and CMDB CI identifiers;
- explicit verification and rollback definitions.

The stable `ai-<digest>` identifier is derived from those bindings. A persistent
condition therefore folds into one lifecycle instead of creating a new action on
every ATLAS pass. Changing the action, target, catalog generation, ITIL change,
or CI produces a different intent.

## Storage contract

`ActionLedger(root)` writes:

```text
root/
  intents/ai-….json       immutable intent core
  events/ai-….jsonl       append-only lifecycle events
  .lock                   cross-process serialization lock
```

Events carry contiguous sequence numbers, the previous event hash, and their own
SHA-256 hash. Reads validate the entire chain and reject corruption, truncation
gaps, path-like IDs, unsupported fields, and unknown schemas. Files are opened
without following symlinks and event appends are flushed and fsynced before the
transition is reported.

The operator loop accepts an injected `lifecycle_ledger`. It creates and advances
the intent through observation, diagnosis, and proposal before authorization.
Immediately before physical work it records authorization and execution. A
successful postcondition check records `verified`; an actuation or verification
failure records `failed` and then `escalated`, linked to the parked decision when
available. Outcomes expose `intent_id` so skcoord/ITIL and dashboard projections
can link back to the authoritative lifecycle.

## Invariants

- Never infer authorization from ledger state. `authorized` is written only after
  the deterministic policy/ratification gate has admitted execution.
- Never mutate an intent core or an old event. Corrections are new lifecycle
  evidence or a new intent with changed governance bindings.
- Never reuse an intent after a terminal state. A newly observed operational
  episode needs a new condition fingerprint/evidence identity.
- Never put credentials, command output, or private evidence in `detail`; store a
  bounded reference to a separately governed artifact.
