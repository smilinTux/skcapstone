# Terminal worker capacity

Fleet workers receive the host's shared `fleet-live/<host>.json` path through
the worker wrapper. Every snapshot records the card, owner, and exact claim
revision for each resolved worker. On normal or failed child exit, the wrapper
removes only that exact generation and atomically replaces the snapshot.
Sibling cards and generations remain present.

The wrapper performs claim release and snapshot invalidation while holding one
CardStore card mutation lock. It verifies the exact current claim, appends an
idempotent exact-generation release, verifies its readback, and invalidates the
matching snapshot before unlocking. A newer claimant therefore cannot enter
between release and invalidation.

Capacity is released only when all of these facts agree:

- the child process is terminal and absent from `/proc`;
- the worker cgroup contains no process except the exiting wrapper;
- CardStore has the same current card, owner, and claim revision, or contains
  the idempotent exact release created by this transition; and
- the live snapshot contains exactly that generation.

The CardStore card lock fences this check against a concurrent next claim. A
signal, preflight failure, missing child, live process, occupied or unreadable
cgroup, stale claim generation, or incomplete snapshot leaves capacity
occupied. If CardStore release succeeds but snapshot invalidation fails, the
unchanged snapshot still advertises the seat as occupied and fails closed.

A missing, unreadable, or malformed snapshot is not evidence that a worker
stopped. The terminal publisher leaves the original bytes unchanged. A
malformed target generation also leaves every valid sibling occupancy intact.
The next normal fleet publication may replace the report only from current
CardStore, process, heartbeat, and transient-unit evidence.

Source-only cards are checked with credential-free `git ls-remote` before the
scheduler creates a workspace or claims the card. An absent exact ref reports
`reconstructability_blocked`. Operators must repair or publish the source ref;
they must not bypass the preflight with credentials embedded in repository
URLs.

The integration test runs the real wrapper terminal path, claims the same card
through the real coordination CLI as a new generation, and executes that
generation through the managed worker launch command.

Rollback is to revert the card's commit. This removes the wrapper snapshot
argument and source-ref preflight together. No service installation or runtime
configuration is changed by this source-only repair.
