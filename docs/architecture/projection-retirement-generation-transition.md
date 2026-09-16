# Projection retirement generation transition

Card `abe011ea` is the SKCapstone source-only boundary for Mero's `abe011e9`
writer. Folded CardStore owner/status and immutable SKCoord generations are
ranked above the mutable `coordination/agents/<agent>.json` legacy cache.

## Proposed authority contract from Mero abe011e9

SKCoord writes each generation at
`coordination/agents/.generations/<agent>/<writer-id>/<uuid>.json`. The stable
writer ID identifies host and agent; a UUID keeps paths unique. Each record is
created exclusively with no-follow and file/directory fsync. Its canonical
bytes bind schema version, agent, writer, generation ID, predecessor SHA-256,
full `AgentFile` payload and payload SHA-256, timestamp, and exact CardStore
card/fold witnesses. Reads validate the serialization, hashes, identity, and
lineage. Competing or missing predecessors are ambiguous; mtime never selects a
winner.

The broker provides `append_generation`, `fold_generation_set`,
`reconcile_agent`, and `append_disposition`. Retirement and restore append
hash-bound tombstone/restoration generations and dispositions. They never move,
replace, or delete canonical, generation, or Syncthing conflict bytes. The
retirement or restore receipt exposes `generation_sha256` and
`disposition_sha256`; the CLI reports those hashes without a quarantine path.
Disk-backed seam tests exercise stale generation and disposition races in two
replicas, but the absent broker means they do not qualify its implementation.
A single-authority reconciler alone may publish the legacy cache after a clean
fold and recheck. The SKCapstone CLI must fail closed on broker absence/outage,
ambiguous lineage, conflict bytes, or stale digests. Its existing hash options
remain mandatory; their generation meaning needs the broker adapter before
activation.

For a claimed tuple, the exact folded card must be nonvoid and conflict-free,
owned by that agent, in `ready`, `doing`, or `review`, with the witnessed claim
revision. `current_task` additionally requires `doing` and membership among
claimed tasks. A completed tuple requires `done`, owner cleared, exact
completer, and an exact completion-event/prior-claim-revision witness. The
retirement target must be ownerless, voided, and archived. Any absent or
mismatched witness fails closed.

## Legacy consumer migration and rollback

First make the installed federation watcher match PR743's conflict-preserving
source behavior on every host. Inventory and disable raw canonical writers,
then bootstrap hashes of existing canonical and conflict bytes without
changing them. Introduce generation reads and dispositions; move consumers to
that authority before allowing retirement. Only one designated cache publisher
may serve remaining legacy readers, and only from an unambiguous fold. Compare
cache views to the generation/CardStore fold and record mismatches without
mtime selection. Do not run retirement or restore against a legacy-only fleet.

Rollback disables the publisher and retains all generations and dispositions.
It never overwrites a newer cache or re-enables last-writer-wins. A later clean
fold can republish while retaining unrelated liveness and host fields; an
ambiguous fold requires reconciliation. Until the broker and migration exist,
retirement/restore source commands refuse mutation and `abe011e9` stays FAIL.
