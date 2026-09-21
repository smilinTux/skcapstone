### Fixed

- A card whose workspace cannot be materialized now yields its dispatch slot.
  The guard logged `WORKSPACE_BLOCKED` and continued without recording any
  backoff, and it runs before `coord claim`, so the same five cards were
  attempted 18 times each over three hours out of a pool of 35 while the fleet
  launched nothing. A dispatch-local cooldown (default 1h, overridable with
  `SKFLEET_WORKSPACE_COOLDOWN_SECONDS`) drops a recently blocked card from the
  pool so the rest get a turn. Every path fails open.
