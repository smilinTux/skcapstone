# Review source custody restoration design

All 28 sources have readable modern bundles, so none may use the legacy route.
Twenty-seven latest native producer PASS records still name the selected bundle.
Their exact claims were released, and builder reconciliation rewrote successful
terminal statuses as authority-released blocked statuses, omitting source_artifact.
The remaining old source generation is superseded by a newer producer claim.

Restore proof-based reviewability without recreating an owner, claim, launch,
provider receipt, or verdict. A read-only verifier will require the latest exact
native claim and PASS, one subsequent exact release, immutable bundle/evidence,
and a node-authored authority-released terminal status with the original request,
unit, invocation and successful exit. Query that host strictly to prove death.
Reuse this proof in dispatch and acceptance so passing dispatch is not a dead end.

Only allow dispatch's source revision exception if folding without the exact
release event reproduces the review's pinned source revision. Other changes stay
refused and are reported for an exact binding repair or current-generation review.
No live store history is rewritten. New claims, changed outcomes, malformed or
missing bytes, unsuccessful exits, live units, and unreachable hosts fail closed.

Plan: commit this design, write failing regression tests, implement the shared
proof and two callers, run targeted and remote-review/acceptance tests plus Ruff
and Black, probe all cards read-only, report any remaining exact blockers, and
open an unmerged draft PR. No deployment is part of this task.

## Verified result

Read-only probes against the 28 reported reviews verified readable modern source
bundles for every card. Twenty-five pass the new release-only revision proof and
strict terminal observation on the original producer host. No card is eligible
for a legacy evidence downgrade. No live board or custody files were changed.

Three intentionally remain refused:

- a950ce42 / a0196e7c: later test_profile_recipe changes prevent exact replay.
- 10191291 / 3f9eaa98: candidate artifact/tree/producer links changed and the
  pinned revision is not reproduced at review creation.
- c264927d / 52f7c2a5: its old candidate was superseded by a newer producer claim
  and PASS_FOR_REVIEW. Restoring the old review would review the wrong generation.

Those sources are already unclaimed in backlog. Preserve that governed path for
fresh current-generation qualification and review rather than editing immutable
review bindings, removing valid test recipes, or fabricating old claims. Reuse
existing candidate bytes where valid; this change does not dispatch fresh builds.
The complete local per-card evidence and remaining decisions are reported in
~/reviews/review-source-custody-20261012.md.

Tests cover source resolution, acceptance terminal proof, a complete fresh remote
review through acceptance collection, release-only replay, latest claims/outcomes,
changed criteria/recipes, holds, altered artifacts/status/request/unit, qualified
terminal evidence, and actual strict SSH observer refusals for live, unreachable,
or differently invoked units. Existing independent review and test gates remain.
