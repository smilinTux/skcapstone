# Terminal worker capacity

Fleet workers receive the host's shared `fleet-live/<host>.json` path through
the worker wrapper. On every normal or failed child exit, the wrapper takes an
exclusive lock, removes only its card, and atomically replaces that snapshot
before any later claim-release reconciliation. Sibling cards remain present.
The wrapper publishes only when `child.poll()` confirms termination. A signal,
preflight failure, missing child, or still-live process leaves the card
occupied.

A missing or malformed snapshot is not evidence that another worker stopped.
The terminal publisher starts from an empty card list for that write, while the
scheduler continues to reconcile CardStore state, heartbeat, process, and
transient-unit evidence. Ambiguous process evidence remains occupied and fails
closed.

Source-only cards are checked with credential-free `git ls-remote` before the
scheduler creates a workspace or claims the card. An absent exact ref reports
`reconstructability_blocked`. Operators must repair or publish the source ref;
they must not bypass the preflight with credentials embedded in repository
URLs.

Rollback is to revert the card's commit. This removes the wrapper snapshot
argument and source-ref preflight together. No service installation or runtime
configuration is changed by this source-only repair.
