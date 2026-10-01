# Native invalid-review replacement

Parent: 9b230773. Source base: b7c0bea695729a7ea8a455d7eafada929fff4c5a.

Extend the existing guarded opener with one native operator authorization,
stored as an attributable event on the invalid predecessor. The event binds
the unchanged producer snapshot, claim and typed candidate; predecessor native
fold and event-history digests; its attributed invalidation and retained report
and decision; and exact stopped reviewer unit invocation. It changes neither
producer metadata nor predecessor history, owner or claim.

The existing canonical review identity gains an optional authorization digest.
Attempt zero retains its existing identity. Exactly one replacement is allowed
for that original source generation; replays resume the same card. The opener
and reconciliation helper validate the authorization before using the attempt.
No caller-supplied entropy can authorize a second review.

A shared current_review_attempt boundary lets the separate acceptance bridge
reject the invalid predecessor and any stale attempt. It also permits the
snapshot reader to exclude only that exact authorized predecessor before
parsing its malformed legacy PR link. Other malformed or unresolved siblings
still fail closed. Later predecessor or producer mutations invalidate use.

The new coord review-replace command requires the existing operator identity
and native mutation authority. It opens review work, never launches workers,
releases claims, completes cards, changes a producer verdict or approves source.
The operator creates the actual authorization only after source qualification.

Tests cover default identity, native false-CI refusal, wrong/live invocation,
source/claim/artifact/history drift, caller-forged attempts, simultaneous calls,
lost acknowledgement, exactly one replacement, and predecessor preservation.
Installation, production authorization and external deterministic acceptance
remain separate. Local source/evidence commits and independent review are
authorized; pushes and live mutations are outside this source task.
