# 2026-09-23 Builder binding compatibility (card ca220923)

## Problem

The legacy `link_review_work` card shape records workspace bindings as
folded-card links named **exactly** `repository`, `base_ref`, and
`base_revision`, alongside `meta.repository`, `meta.base_ref`, and
`meta.base_revision`. Older review work cards were authored in that link
shape only; `builder_dispatch` currently reads `meta` only, so cards in the
legacy link shape cannot dispatch.

## Required contract (corrected)

`src/skcapstone/fleet/builder_dispatch.py` must accept any of:

- meta only (current behaviour, preserved)
- links only: folded-card links `repository`, `base_ref`, `base_revision`
- both, when the normalized values of meta and links agree

When both exist and the normalized values **disagree**, fail closed with
`BuilderDispatchError` whose message is exactly:

```
source binding conflict: repository
```

(substituting `base_ref` or `base_revision` for that field).

Rules that must stay:

1. `link_source_card` and `link_head_revision` are NOT workspace aliases.
   They never fill the three binding fields, and they never count towards
   "both exist".
2. Malformed sources (e.g. an unreadable or wrong-type value) are rejected.
3. Post-offer amendments: after an offer is recorded, the card is
   revalidated against the CURRENT folded card (including links), so a
   link amendment after the offer blocks materialization and claim.

## Source-candidate review vs installed-runtime qualification

This card binds the candidate source commit for review:

- commit: 1684e15d8b87beb6b03b799c087765a05cde7e0a
- tree: (recorded at commit time)
- ref: work/ca220923-builder-binding

The installed runtime (e.g. a deployed package) is a separate qualification
track; this work does not install, restart, or dispatch into the runtime.

## Focused tests

The test file must include (TDD RED first):

1. `test_link_only_binding_dispatches` -- links-only card dispatches.
2. `test_matching_dual_source_dispatches` -- meta + links agree, dispatches.
3. `test_repository_conflict_blocks` -- differing `repository` values.
4. `test_base_ref_conflict_blocks` -- differing `base_ref` values.
5. `test_base_revision_conflict_blocks` -- differing `base_revision` values.
6. `test_decline_reason_invalid_source` -- malformed/invalid source is
   rejected with a clear error.
7. `test_amendment_after_offer_blocks` -- a folded-card link amendment
   made after the offer blocks materialization and claim.
8. `test_legacy_link_review_work_link_shape_dispatches` -- legacy canonical
   link shape (links with exactly `repository`, `base_ref`,
   `base_revision`) dispatches.

The legacy canonical link shape is links with `repository`, `base_ref`, and
`base_revision` only.
