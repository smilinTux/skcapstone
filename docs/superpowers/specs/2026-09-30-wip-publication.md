# Guarded WIP publication

Card: 9897319a. Parent: 3ee76566. Human approval: 2026-09-30.

Follow the existing lifecycle: Niobe dispatches, Link recommends independent
review, Seraph reviews the exact candidate, and a mediated operator connector
publishes source for CI. The existing fiber dispatcher remains the only launch
authority. Do not enable the legacy Codex-only Niobe entrypoint or Link merge
automation. Existing source/review artifacts and inactive claims survive.

This increment adds the missing narrow draft-publication transport, not a new
scheduler. It accepts an operator-qualified exact candidate, not worker text.
The default Forgejo client keeps its existing review-only write permission.
A separate WIP client can create a private SKLegal PR only, with exact `WIP: `
prefix, deterministic immutable head branch and `main` base. It cannot submit
reviews, merge, modify a PR, delete anything or deploy. Credentials stay in the
existing operator secret boundary and never enter model workspaces.

Before any publication, verify the exact source and independent review bytes,
source-only diff, all ancestors being published, the current base, and CI
workflow effects. Reuse an existing matching PR without creating duplicates.
Refuse conflicting heads, bases, titles or lineage. A transport timeout is an
unknown result; the next invocation reconciles open PRs before another create.
CI is observed at the exact candidate SHA. Missing, pending or failed checks
are never success. A source review is not hosted CI or acceptance.

Inactive claims require fresh process-generation absence, preserved workspace
evidence and native exact-claim-revision release/readback. Never enable the
legacy inactivity-only expiry mechanism. Ambiguous legacy owners stay parked.
This transport increment does not implement or activate that handoff adapter.

Keep the human-approved estate ceiling 12, rollback 9, provider caps
DeepSeek 3 / GLM 5 / Codex 2, 75-second launch spacing, Kimi fence, and existing
failure holds. No automatic merge, deployment, protected Matter egress or legal
actions. Infrastructure commits are local-only review snapshots. No timer is
enabled until independent review and a real bounded canary pass.
