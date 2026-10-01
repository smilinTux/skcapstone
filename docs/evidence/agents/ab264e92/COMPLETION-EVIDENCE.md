# Production source-only reviewer brief

Card: ab264e92
Owner: codex-source-review-brief-ab264e92

## Source and scope

This isolated source leaf starts at reviewed checkout repair
4d2a00dc08039c37a3cb00e5bd18fba757499617, then composes reviewed private staging
eb49a9801d85df55589a4376899dc805fa3b2253 as the identical prerequisite commit
4eb2ef93a9c3c3e6d28b584bfd614f0251c41430. This leaf's additive diff starts at
4eb2ef93a9c3c3e6d28b584bfd614f0251c41430. Blocked guarded-CLI parity changes are
not included. Local source/evidence commits are explicitly authorized.

## Acceptance evidence

- `scripts/fleet/skfleet-rotate.py` selects the dedicated source-only reviewer
  brief in the actual post-claim path. Production producers and hosted/legacy
  reviews keep their existing selections. Invalid review bindings refuse launch
  and retain custody.
- `src/skcapstone/fleet/production_brief.py` uses the existing source-only
  applicability contract and unchanged native completion validator. It derives
  reviewed commit/tree through Git, generates the decision JSON without a
  containing-commit self-hash, binds schema and report_sha256, and verifies committed report/decision bytes and
  evidence-only changes before handoff. The private directory-descriptor staging
  recipe is shared with the reviewed producer recipe without changing its bytes.
- The selected source-only brief does not inherit the legacy six-CI publication
  recipe. It prohibits invented checks, PR/CI links, self-completion, claim
  release, unsolicited mail and source changes. PASS publishes report/digest,
  actual verdict, then the existing six-field applicability receipt. FAIL and
  structured BLOCKED do not publish a PASS applicability receipt.
- Every authority link requires the existing expected source/claim revisions,
  transition ID and JSON readback. The returned source revision feeds the next
  write. A source/claim change between writes or missing guarded CLI support
  refuses handoff; no legacy write fallback is present.
- `tests/fleet/test_production_source_review_brief.py` executes the real
  post-claim selection and both rendered shell recipes against Git, then checks
  actual native fixture events with the unchanged validator. Twenty-two cases
  cover normal PASS, FAIL/BLOCKED, preserved producer/hosted/legacy selection,
  malformed or conflicting source bindings, self-review, hosted binding refusal,
  bad decision hashes, impossible self-hashes, source edits, changed claims,
  producer identity and candidate evidence digests, guarded inter-write races
  and missing authority guard support.
- `docs/fleet/production-pi-workers.md` records concise operational lessons from
  invalid review3054d5f1, including the observed cost, invalid-history retention,
  pending rollout and independent verification requirements.

## Actual validation

Initial focused suite: 17 passed, zero skipped, 6.14 seconds. After adding the
shared decision schema and guarded authority publication, all twenty-two new
cases passed in the final integration run below.

Integration command:

```text
python -m pytest tests/fleet/test_production_source_review_brief.py tests/fleet/test_production_brief.py tests/fleet/test_production_dispatch.py tests/test_review_verdict.py tests/test_skfleet_workspace_materialization.py tests/fleet/test_source_bundle.py -q
```

Final actual result: 254 passed, zero skipped, 19.03 seconds. Exact log retained
under this card's operator evidence directory as `TESTS-GUARDED.log`. The prior
249-check run is retained separately as `TESTS.log`. Ruff and Black checks
passed on the changed package module and new tests. `git diff --check` passed.
Independent review of this exact candidate remains pending; no new model call
was made by this source leaf.

## Limits and rollback

This is a source-only repair, not deployment or acceptance of the failed review.
No live workspace, invalid review artifact, trial verdict, runtime or service was
changed. Review3054d5f1 remains invalid history; source89508f83 remains gated.

The prompt and worker-side recipe do not guarantee truthful model behavior.
Deterministic external acceptance verification of committed source bindings,
evidence-only changes and actual tests remains a separate prerequisite. The
existing native completion validator does not inspect REVIEW-DECISION.json.
Authority guarded native publication is required. Worker-node CLI parity remains
separate and is not assumed by this authority-only review recipe. Existing unsafe evidence
directories refuse staging and require an attributable operator repair.

Rollback restores the prior source commit. Preserve all worktrees, native event
history and immutable evidence. No production rollback was needed or attempted.
