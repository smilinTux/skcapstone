- ATLAS gate 5 hardening: the three weak spots the fault-injection drill documented
  are fixed and are now PASS criteria of `scripts/atlas_p32_fault_injection_drill.py`
  (24 scenarios, scenario 22 is new).
  - Stale evidence is refused. A proposal that actuates must carry structured
    `evidence: {source, observed_at}`; missing, unparseable, naive, future-dated or
    older than the new `skoperator run --max-evidence-age` (default 600 s) is refused
    before any ITIL change, ledger intent or actuator call, and the check fails closed
    (a bad maximum age refuses, it never disables). The proposer stamps evidence from
    the observation, not from the model reply, and the ledger records `source@time`.
  - A mid-pass freeze is reported at the decision point: the pass result carries
    `frozen`, `frozen_mid_pass` and `freeze_state`, each outcome carries its own
    `freeze_state`, and a freeze refusal no longer feeds the circuit breaker (loop and
    dispatcher).
  - The outcome word `applied` is gone: `verified` only after the post-action
    re-observation ran and passed, otherwise `performed, unverified`.
