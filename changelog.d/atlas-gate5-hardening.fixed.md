- ATLAS gate 5 hardening: the weak spots the fault-injection drill documented, and the
  holes the independent review of the first fix found, are fixed and are PASS criteria of
  `scripts/atlas_p32_fault_injection_drill.py` (28 scenarios; 22 to 26 are new).
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
  - Review round 3: an escalated proposal now gets a CAB-gated change (the ITIL change type
    follows the disposition, the `auto-normal` tag is stripped), so `run_dispatch_pass`
    can no longer run it without a human; the age rule never voids a pending human decision
    (the loop records the lane on the intent); the dispatcher decides auto-approval from the
    fold's `auto-approved` timeline entry (so `auto-normal` counts), re-checks the target and
    action binding, and treats `Unknown` as "do not act" with a fresh-source-time re-observation.
    The end freeze state is reported as it is, naive clocks are refused, discovered observers
    cannot supply a source time, and skos reads its digest once. Drill scenario 25 is new.
  - Review round 4: "human-approved" is now derived from the CAB votes, not inferred from a
    missing `auto-approved` marker (a standard change that goes approved, scheduled,
    unscheduled folds approved with no marker and used to read as human); the loop always
    drafts its own change tagged `op-bind:<app>|<action>|<target>` and never trusts a
    proposal-supplied change id, the dispatcher refuses a change not bound to the intent,
    and a run spends the approval (`implementing`, then `deployed` or `failed`); cooldown
    and an open circuit at dispatch are a refusal (nothing recorded, nothing rolled back);
    and a stale or hardened catalog refuses without writing AUTHORIZED, after the lane and
    approval checks. Drill scenario 26 is new.
