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
