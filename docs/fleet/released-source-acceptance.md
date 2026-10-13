# Released source acceptance

`production_review_finish.finish_pair` validates the retained reports, decision
and trusted tests, then drives the authority CLI through deterministic link and
completion steps. The reviewer completes first. Source links bind the exact
candidate and test receipt; source completion also locks and validates the
completed independent review.

A release can occur after the context, finish intent, or pending link intent was
recorded. `--released-source-binding` explicitly supplies the captured source
JSON to the guarded link and completion commands. It requires the normal exact
revision and claim guards; completion additionally requires the exact review
guards. Calls without this option retain their existing behavior.

Under the card mutation lock, the native handlers reuse
`released_source_matches`, including `production_admission._released_custody`.
The latest custody event must release exactly the captured producer and claim,
and the typed candidate, repository, head, tree and outcome must remain valid.
No replacement claim is created.

For a journal interrupted before release, a read-only CardStore view excludes
only that proven release event. The expected revision, or an already-written
link's post-write card digest, must match either the real current state or this
view. Every other event remains in the comparison. The transition payload,
outcome identity, projection and related-review checks remain strict. A new
outcome carrying the same PASS text is still a different outcome and refuses.
The native event and any existing journal are never rewritten.

New released-source acceptances use the full producer-link sequence so the
source retains the trusted test receipt. Existing journal step sequences remain
immutable. Lost link replies reconcile the existing event and projection; lost
completion replies reconcile the exact completed state. Neither writes a second
PASS or completion. A changed non-release history still requires its separate
governed recovery procedure.
