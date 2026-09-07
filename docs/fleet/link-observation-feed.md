# Link observation feed

The Link seat reads the mediated feed at:

```text
~/.skcapstone/coordination/link-observations.json
```

The feed is input, not authority. A producer with the required external
connector access creates it. Link receives no GitHub credential, shell
credential, merge capability, deployment capability, or CardStore mutation
tool.

## Required envelope

The JSON envelope uses schema `skfleet.link-observation-feed/v1` and contains:

- `source_revision`: producer-side source or inventory revision
- `observed_at`: timezone-aware ISO-8601 timestamp
- `producer`: identity, host, session, and workspace of the mediated producer
- `records`: exact `PullRequestObservation` fields plus review-card identity
- `reviewer_candidates`: exact `ReviewerIdentity` fields
- `evidence_sha256`: SHA-256 of the canonical envelope payload excluding the
  schema and this hash

Link rejects missing, malformed, future-dated, stale, or hash-mismatched
feeds. The freshness window is 15 minutes. Rejected input is recorded as a
bounded no-op in the Link health log.

For each valid record Link may append a revision-fenced advisory handoff to
`coordination/seat-cycles/link.handoffs.jsonl`. Niobe remains the only seat
allowed to validate and execute a handoff. A recommendation never claims a
card, creates a review, merges, deploys, or releases anything.
