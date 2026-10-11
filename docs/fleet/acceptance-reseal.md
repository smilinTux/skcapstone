# Recovering an interrupted source acceptance

`coord acceptance-reseal` supports one boundary: steps 00 through 07 are
acknowledged, step 08 wrote and projected the source PASS exactly once but has
no acknowledgement, and no later original step or finished receipt exists.
It is not a general acceptance retry or a way to replace review evidence.

The caller must be `lumina-nor`. The command uses normal coord authorization
and serializes against acceptance finishing, then locks both cards in stable
ID order. It records an immutable `reseal.json` and a native
`acceptance_reseal` audit event on the source card. A lost reply can be retried
with the identical arguments. The command does not complete either card or
write a verdict. The normal acceptance cycle consumes the checkpoint and
writes only steps 09 onward in `resealed/`, retaining the original journal.

Before invoking it, independently review the card readbacks, original intents,
native PASS event and full source/review histories. All changes after the PASS
must be enumerated `lumina-nor` link corrections: clearing an invalid PR
binding or setting a stale commit alias to the already reviewed candidate.
No other writer, changed contract, claim, outcome or evidence is accepted.
The native event and its overlay projection must agree. Source report,
committed independent decision, candidate Git state and qualified test receipt
are revalidated with the existing validators. No test or review is invented.

Supply a private corrections JSON file, ordered by event position. Each row
has exactly these fields:

```json
[
  {
    "event_sha256": "<digest of the exact correction event>",
    "key": "commit",
    "before": "<stale commit alias>",
    "after": "<exact sealed candidate SHA>"
  }
]
```

For PR removal use key `pr`, its exact previous URL and an empty `after`.
Correction digests use `seraph_review_contracts._digest` on the native event,
or the validated `review_verdict._card_events` row when no native event exists.
Ordinary link events can have null event IDs; their full event digest is the
identity. For an event present in both lanes, use the native representation.

```sh
skcapstone coord acceptance-reseal REVIEW_CARD \
  --review-claim REVIEW_CLAIM --agent lumina-nor \
  --corrections /absolute/private/corrections.json \
  --expected-context-sha256 CONTEXT_BYTES_SHA256 \
  --expected-finish-sha256 FINISH_INTENT_BYTES_SHA256 \
  --expected-step-sha256 STEP_08_INTENT_BYTES_SHA256 \
  --expected-event-sha256 NATIVE_PASS_EVENT_DIGEST \
  --expected-source-revision SOURCE_SNAPSHOT_REVISION \
  --expected-review-revision REVIEW_SNAPSHOT_REVISION \
  --expected-source-history SOURCE_HISTORY_DIGEST \
  --expected-review-history REVIEW_HISTORY_DIGEST \
  --expected-claim-revision SOURCE_CLAIM
```

File hashes are SHA256 of the exact retained bytes. Snapshot revisions use
`production_review_finish.native_state`; history digests use
`historical_pr.history_revision`; the PASS event digest uses `_digest`.
These are expected preimages supplied by the operator, not values the mutation
refreshes automatically. Omitted or mismatched guards refuse without a board
write. The immutable receipt returned as JSON must match the retained receipt
and its native audit digest. Verify the folded card through `coord show --json`
and observe the next `SOURCE_REVIEW_ACCEPTANCE` result before claiming success.

Subsequent history drift refuses even if the folded links are unchanged.
Only the exact remaining continuation events are allowed. Interrupted receipt
creation or a lost continuation reply resumes idempotently; no old intent or
acknowledgement is rewritten. `record_guarded_link` remains unchanged.
