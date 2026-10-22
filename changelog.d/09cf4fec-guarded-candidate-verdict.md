- Card `09cf4fec`: add optional exact-source and claim-generation guards plus an idempotency key to `coord verdict`. A controller can refuse stale or superseded candidate handoffs under the existing card lock without changing legacy callers or granting completion authority.
- Card `09cf4fec`: expose Chef's existing independent review opener through
  `coord review-work` with exact source and claim guards. Preserve typed
  candidate metadata, original criteria and idempotent recovery; no claim,
  launch, completion, merge or deployment is authorized by this command.
- Card `09cf4fec`: pin the controller's expected candidate digest before the
  native verdict write. Connect explicitly scoped terminal receipts to one
  opposite-provider review through the existing dispatcher and bounded Git
  bundle transport. Preserve typed evidence-only review proposals without
  treating them as test acceptance or source completion.
- Card `09cf4fec`: add opt-in guarded `coord link` writes with native idempotent
  transitions and a same-identity projection for existing review gates. Add
  exact claim/source guards to native completion, including an optional locked
  completed-review binding for source closure. Legacy callers remain unchanged.
- Card `09cf4fec`: validate controller-private task-qualified test plans and
  receipts against the exact source head, task contract, commands, retained
  logs, execution identity and explicit skip allowances. Missing evidence
  remains waiting, not PASS. This validator does not execute or certify tests.
- Connect exact independent PASS proposals and controller-private required-test
  receipts to guarded native review/source completion. Retain immutable step
  intents, recover lost native replies without duplicate receipts, recheck both
  live terminal units before mutation, and record applicability last. These
  checks do not execute tests or manufacture hosted CI evidence.
