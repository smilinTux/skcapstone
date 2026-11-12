- `AGENTS.md`: added a Reviewer (seat-seraph) section, an Other lifecycle
  seats section, and a "Reading the board without blowing your context"
  section, all derived from reading real closed review cards and the
  current code rather than guessed. Fixes the gap that left review card
  `b34ca6f9` flailing on invalid CLI syntax (`fleet get workers`, `coord
  kanban <id>`) for 17 claims and 18 releases with no verdict ever
  recorded. Also corrects the stale "record it LAST: anything written
  after a verdict supersedes it" line next to `coord verdict`; since PR
  #837 only a new outcome-shaped link, a `blocked_on` chain, or an
  `evidence_sha256` link invalidates a generation, not any link at all.
