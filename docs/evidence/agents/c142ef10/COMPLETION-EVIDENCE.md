# Production producer brief

Card: c142ef10. Owner: codex-production-brief-c142ef10.
Claim: 99d240364d194796a1d031bf32871b31.
Base: 8817dac18b650f3b26e601cb24d292a7b20e4927.

The standalone production_worker_brief helper replaces the entire producer
brief when the parent integrates it after claiming a production source card.
Its stable prefix requires explicit AGENTS/TDD reads, exact ownership, scoped
authorization, existing isolated clone, verification triple, two-compaction
handoff, bounded reads, privacy and actual required tests. Commit and push
authorization comes only from the card. Missing permission for a required
candidate commit is BLOCKED, not permission inferred from this prompt.

The rendered Bash completion recipe requires a clean clone with a real .git
directory, named non-main branch, authorized-base ancestry and byte-exact
committed COMPLETION-EVIDENCE.md. It rechecks the native card's owner/claim,
copies evidence to a private uniquely named shared file, writes owner-authenticated
evidence/commit/branch links, then records typed PASS_FOR_REVIEW last. It never
commits, pushes, completes a producer card, releases its claim or deletes source.
The report does not need to contain its own impossible self-containing hash.
Required independent review and native completion remain controller-owned.

Meaningful validation executes the actual rendered recipe in temporary Git
repositories with a recording coordination CLI stand-in. Fifteen tests pass:
the positive case proves exact HEAD/tree/ref, committed/shared byte identity,
private file mode and typed verdict order. Negative cases reject dirty,
untracked, missing or symlink evidence, detached/main branches, linked worktrees,
unrelated base, wrong owner/claim and command-injection identities before any
board write. The recipe uses the common installed native CLI contract, without
optional CAS flags absent on remote nodes. Controller publication separately
validates exact claim/outcome and source custody after worker exit. The tests
caught and corrected Bash AND-list failures bypassing errexit in the first
draft. Formatting and lint pass.

Command: PYTHONPATH=src python -m pytest tests/fleet/test_production_brief.py -q.
No model call, source push, runtime install or real native mutation was performed
by the tests. The compact empty-context prompt is approximately 4.4KB; this is a
character measurement, not a measured tokenizer saving or throughput claim.
Parent callsite integration and independent exact review are still required.
Rollback is the source commit revert; existing legacy/reviewer briefs are not
modified by this isolated leaf.
