# Canonical production review implementation plan

Card `c1a30126`, owner `codex-canonical-review-c1a30126`, claim
`35c5f46f32fe40208c55b44593bdc82e`. Base
`090a71d29217aeb6f18498335d2d3e2f0cf3f3f4`; dependency `c1a30124` is DONE.

The authorized bounded design keeps ordinary legacy review creation unchanged.
Production selection reuses exact retained-source custody, resolves the native
current review identity before directory guards, and captures source/claim
revision guards. Creation calls `coord review-work` as Link and verifies native
metadata, source binding and lineage before admitting the review. No additional
scheduler, checkout path, release or installation belongs to this candidate.

1. Pin the qualified guarded CLI modules to reviewed coordination source
   `3b36c0333161e9e03c6d38695bf5ed388c9db6c1` and installed byte hashes.
2. Add failing dynamic coverage for canonical creation, legacy tombstones,
   retained claim, exact checkout/brief/acceptance lineage and refusal paths.
3. Patch only production branches in the existing opener and reuse custody.
4. Run focused opener/custody/source/reviewer tests, compile and whitespace
   checks. Preserve any historical dependency-pin failure as a limitation.
5. Record completion evidence, local commit and private verified bundle, then
   hand the exact delta to root for independent review and installation.
