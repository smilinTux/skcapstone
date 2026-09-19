- **A channel with right of first refusal had become the owner of an entire card
  class.** PR #635 (2026-09-11) removed every `builder_dispatch.eligible()` card
  from every regular host's lanes *unconditionally*, so a path built to feed one
  idle standby box its first four cards withheld the whole class from the rest of
  the fleet, including cards it had already permanently failed and cards it could
  never write a request for at all. Measured on the chi estate 2026-09-19, one
  chiap08 rotation tick: 14-16 of a 19-20 card pool parked on `node-ziowk01`'s
  four slots (`builders-at-capacity: node-ziowk01=4/4` logged 13 times in that
  single tick), `026a08d9` withheld while terminal-`failed` at the attempt
  ceiling, `23554ec7` withheld while its binding makes `offer()` raise before it
  writes anything, and then `SELECTION_EMPTY|chiap08|reason=builder-path-withheld
  pool=19 owned=0` while `owner_free` advertised 54 free local seats across five
  hosts. `node-ziowk01`'s lifetime record at that point was 2 completed out of 41
  dispatched. The local lanes were never incapable of this work:
  `_source_workspace_spec` and `_materialize_worker_workspace` produce the same
  pinned exact checkout (and verify ancestry, which the remote materializer does
  not), and source-only card `9968f114` was running in chiap01's codex lane
  throughout. A card is now withheld from the local lanes only while the builder
  path *actively holds* it, which `builder_dispatch.held_card_ids()` reads from
  the dispatch tree as exactly two situations: a non-terminal status for the
  current request generation, or an offer still inside its lease with no answer
  yet. Terminal, unplaceable, or at-capacity means the card goes to a local lane;
  capacity is data, not a veto. This opens no double-claim window, because it adds
  no claim path: the CardStore fence remains the sole authority, a card the
  builder has claimed is no longer READY and so is never in the pool, and the only
  window a release could widen (offer to remote claim) is precisely what
  `held_card_ids()` reports as held. Holding is keyed on the dispatch tree rather
  than on `_ready_builders()`, so a node demoted or cordoned mid-flight still
  holds the card its worker is running. Every failure is towards withholding: an
  unreadable record holds its card, and an unreadable tree raises so the rotation
  falls back to the full PR #635 withhold for that tick. The split is logged as
  `BUILDER_WITHHOLD|<host>|withheld=N|returned=M`, one
  `BUILDER_RELEASED_TO_LANE` line per released card, and `builder_returned=` on
  the `SELECTION_EMPTY` diagnostic.
