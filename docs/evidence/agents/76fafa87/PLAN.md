# Fiber usage evidence implementation plan

**Goal:** Make per-run usage and local request-budget exhaustion measurable
without changing worker admission, forwarding, permissions or quality gates.

**Architecture:** Extend the existing per-task gateway bridge. Inspect bounded
response bytes only after forwarding them, retain numeric usage aggregates,
and write one private controller-owned usage receipt when the bridge closes.
The worker and lifecycle interfaces remain unchanged.

**Spec:** Native card `76fafa87`, authorized by the 2026-09-30 optimization
goal and audit `46ce2629`. Rollout is tracked by `bd9a559a`.

**Constraints:** Standard library only. Preserve exact route and claim checks,
64 requests, 4 MiB request and 16 MiB response limits, and streaming bytes.
Never persist prompts, response text, raw headers, secrets or raw exceptions.
Missing usage is unavailable, not zero. Do not edit the active lifecycle tree.

## Implementation

- [ ] Add failing tests in the existing bridge test module for JSON usage,
  fragmented SSE forwarding, missing/malformed numeric fields, repeated
  cumulative SSE usage, receipt privacy/mode, and budget-refusal accounting.
- [ ] Add bounded aggregate extraction in `fiber_gateway_bridge.py`, using
  the final usage object per completed response and per-field coverage.
  Persist `usage.json` beside the trusted socket with exclusive creation and
  mode 0600. Do not alter upstream request bodies or downstream response bytes.
- [ ] Run the bridge module and existing gateway/sandbox boundary tests.
  Record exact results and inspect the complete diff for secret/content leaks.
- [ ] Commit only this card's source, tests, changelog and evidence locally.
  Obtain independent review through a different qualified provider family.
- [ ] Under the existing authority lock, install only the reviewed bridge
  bytes with per-host preimage hashes and backups. Roll back by restoring
  those exact bytes; retain usage evidence. Do not stop existing workers.
- [ ] Observe a real qualified run and verify its controller-owned receipt.
  Keep the broader rollout goal active until distributed workflow evidence
  proves the whole requested outcome.
