- Retire unused expired review offers when their reserved transient unit is
  stopped and failed, without requiring reset-failed. Preserve strict launched
  invocation, status, session and reservation proofs.
- Name withheld review backoff as pass-awaiting-acceptance, blocked or
  claim-ceiling. Coord gates reports pending PASS acceptance and names hashed
  dependency holds as blocked backoff; runtime admission decisions are unchanged.
