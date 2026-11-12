- Require skcoord 0.1.85 so the governed canonical-successor check also accepts
  legacy card cores that omit optional `exit_gates`, `non_goals` or
  `spec_version`. Rollouts keep an already-satisfied 0.1.84, so the floor has
  to move for hosts to pick up the fix.
