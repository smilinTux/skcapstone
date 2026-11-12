- **`not-abandoned` was written on every worker exit, so the ledger said "these
  all finished cleanly" about cards that had recorded nothing at all.**
  `abandon_reason` exists to keep a durable finish from being filed next to the
  releases nobody can explain; `not-abandoned` is the one member of the closed
  vocabulary that asserts success, and `coord release-claim --help` defines it as
  "the release follows a durable finish, not a stoppage". The wrapper's
  `finalize_worker_exit` path hardcoded it, so a worker that was killed, timed
  out, or exited without writing a verdict filed the same claim of success as one
  that passed. The collapse the field exists to prevent happened anyway, in the
  opposite direction: every stoppage became indistinguishable from a finish.

  Measured on chi, 2026-09-19. The eight cards frozen at the claim ceiling on
  chiap01 carry well over a hundred releases between them, every one labelled
  `not-abandoned`, and between them not one verdict, one PASS, one candidate
  commit or one piece of evidence. Card 63d0474d alone holds 14 claims, 14
  `not-abandoned` releases and zero outcome events. An investigation that trusted
  the field began from the conclusion that these workers had finished cleanly and
  were being re-dispatched; the ledger says they never finished.

  The wrapper now claims `not-abandoned` only when THIS claim generation actually
  recorded a terminal verdict, in either store one can land in (a native
  `verdict`/`blocked` event in the per-card shard, or an outcome-bearing link in
  the coordination overlay), and otherwise writes `unspecified`, whose stated
  purpose is to say the cause is not known. It fails closed in both directions: a
  claim whose own opening timestamp cannot be found, or a store that will not
  read, reports "not known" rather than crediting a verdict from another
  generation or another card. Nothing infers an outcome that was never written.
