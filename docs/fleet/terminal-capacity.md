# Terminal worker capacity

Fleet workers receive the host's shared `fleet-live/<host>.json` path through
the worker wrapper. Every snapshot records the card, owner, and exact claim
revision for each resolved worker. On normal or failed child exit, the wrapper
removes only that exact generation and atomically replaces the snapshot.
Sibling cards and generations remain present.

Capacity is released only when all of these facts agree:

- the child process is terminal and absent from `/proc`;
- the worker cgroup contains no process except the exiting wrapper;
- CardStore contains an exact release for the same card, owner, and claim
  revision, with no current or conflicting claim; and
- the live snapshot contains exactly that generation.

The CardStore card lock fences this check against a concurrent next claim. A
signal, preflight failure, missing child, live process, occupied or unreadable
cgroup, stale claim generation, or incomplete snapshot leaves capacity
occupied.

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

Rollback is to revert the card's commit. This removes the wrapper snapshot
argument and source-ref preflight together. No service installation or runtime
configuration is changed by this source-only repair.
