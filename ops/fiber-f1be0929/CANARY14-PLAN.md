# Temporary 14-worker canary

Card: 2c5ac908. Parent: 3ee76566. Human approval: 2026-09-30.

Change only the existing authority admission ceiling from live 12 to 14.
Keep DeepSeek 3, GLM 5, Codex 2, conservative unknown occupancy, host limits,
75-second launch spacing, source/claim checks and repeated-failure HOLD.
No competing dispatcher, mass restart, forced claim handoff or automatic merge.

1. Test the exact 14/15 boundary, including pending reservations, and unchanged
   provider budgets, placement, unknown occupancy and spacing.
2. Obtain independent cross-provider review of exact source and tests.
3. Under the existing authority lock, hash-fence the live 12 module, preserve
   its exact bytes and atomically install the reviewed candidate. Rehearse
   rollback with scratch files before touching live state.
4. Recheck the live census and provider completions. Admit only qualified,
   source-bound canary requests, respecting existing ownership and partial work.
5. Require real accepted implementation and independent review with recorded
   placement, gateway errors and host pressure before retaining 14. Unit tests
   alone do not complete this card or demonstrate productive throughput.

Rollback: hold new admission, acquire the authority lock, restore the exact
backed-up live 12-worker module and verify its hash. Leave running sessions,
claims and reservations untouched; let excess occupancy drain. Never restore
the historical nine-worker snapshot or restart legacy launchers.
