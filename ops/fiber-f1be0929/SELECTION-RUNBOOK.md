# Existing-selector bridge

Card `16c3cd09`, parent `3ee76566`.

The existing controller calls selection under its existing authority lock,
before refill. There is no new service or timer. Install `fiber_selection.py`
and an unchanged copy of `scripts/fleet/skfleet-backlog-promoter.py` beside
`fiber_control.py`. Preserve the previous controller bytes and hashes first.

Selection is disabled until an operator publishes a protected
`~/.config/skfleet-fiber/selection.json`. Its parent must be operator-owned
and not group/world writable. The exact fields are:

- `schema`: `skfleet.selection/v1`
- `cards`: at most 32 explicit card IDs mapped to their native
  `fiber_admission.card_fingerprint` hashes
- `repository`: one exact approved source repository URL
- `qualified_at`, `expires_at`: finite Unix timestamps, at most 24 hours apart
- `evidence_sha256`: hash of retained operator scope-qualification evidence
- optional `test_environment`: the existing qualified Python selector,
  validated unchanged by the worker contract

This is explicit scope approval, not permission to scan arbitrary backlog.
Only explicitly scoped SKLegal source-only S/M implementation cards enter this bridge.
The W7 crew workspaces do not imply a literal w7 card label: existing regular
crew cards lack that label. Both sklegal and source-only labels are required.
An optional tdd_custody input link is permitted and fingerprinted, while
candidate, evidence, outcome and unknown links still require the separate
preserved-work continuation path. GLM-only
cards retain that family; other breadth cards use DeepSeek. Codex-only cards,
standing-seat cards, ownership, partial work, non-source evidence links,
changed contracts, unverified dependencies, and Chef's existing promoter
refusals remain excluded. A dependency must be done with an explicit PASS.
Independent review and inactive-owner handoff retain their existing paths;
this module does not claim to automate those paths.

At most two live qualification receipts are staged. All original refill,
claim, runtime, source, provider, estate capacity and launch-spacing gates
still apply. The currently approved temporary canary ceiling is 14, with
rollback to 12; this selector change grants no additional capacity.

Expired receipts remain as evidence without consuming the live selection
budget. They are not automatically overwritten or renewed. Malformed scope
fails closed through existing fault accounting. A removed scope prevents new
selection but does not revoke already-qualified receipts: to revoke those,
HOLD admission and preserve/remove their exact active receipt and queued
request using the existing refill rollback procedure. Never reset run history.

## Verification and rollback

Run the full existing fleet suite, the new selection tests, and an independent
cross-provider review of exact source bytes. Test actual controller describe
and verify functions, dependency closure, expiry during checks, preserved
output, repeated selection, no-clobber, bounds, and authority-lock ordering.

Install under the existing authority lock, helpers first and controller last.
No scope file means the new hook remains disabled. Run a native cycle and
verify it reports selection disabled without changing claims or launching.
Only activate a genuinely reviewed scope after fresh capacity and host checks.

Rollback: HOLD new admission under the same lock, preserve any active scope,
qualified receipts and queued requests, then atomically restore the exact
previous controller. Unused helpers can remain for audit. No worker is killed,
no claim is transferred, and no competing timer is re-enabled. Never discard
evidence or silently convert a qualified request to a manual request.
