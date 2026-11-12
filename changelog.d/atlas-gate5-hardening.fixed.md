- ATLAS gate 5 hardening: the weak spots the fault-injection drill documented, and the
  holes the independent review of the first fix found, are fixed and are PASS criteria of
  `scripts/atlas_p32_fault_injection_drill.py` (26 scenarios; 22 to 24 are new).
  - Stale evidence is refused, and judged against the source. A proposal that actuates
    must carry structured `evidence: {source, observed_at}` where `observed_at` is when
    the SOURCE data was true (adapters report `source_observed_at`; the envelope never
    invents one; a probe-less observation cannot actuate). Age is measured on the real
    clock at the decision point, never the pass clock, against
    `skoperator run --max-evidence-age` (default 600 s, capped at 3600 s). Missing,
    unparseable, naive, future-dated, stale, or a bad or oversized max age (including
    `10**400`) is refused before any ITIL change, ledger intent or actuator call. The
    ledger records `source@time` first, then any free-text `evidence_ref`.
  - A leftover PROPOSED intent can no longer be actuated later. Any closed gate refuses
    before anything is written; an intent written and not actuated in its pass is voided
    (new `PROPOSED -> ESCALATED` ledger edge); the dispatcher re-observes the condition
    before acting and requires fresh stored evidence for auto-approved standard changes
    (human-approved changes skip the age limit, never the re-observation).
  - A mid-pass freeze is reported as what happened: `frozen`, `frozen_mid_pass`,
    `freeze_state`, `freeze_summary` and per-outcome `freeze_state` and `freeze_after`;
    the brief no longer says Atlas stood down when actions already ran. A freeze refusal
    does not feed the circuit breaker, a real failure coinciding with a freeze does.
  - The outcome word `applied` is gone: `verified` only after the post-action
    re-observation ran and passed, otherwise `performed, unverified`.
