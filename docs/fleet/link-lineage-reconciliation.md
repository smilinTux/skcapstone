# Link lineage reconciliation

**Card:** `b88b324c`  
**Scope:** SKCapstone, SKDashboard, and SKWorld software lifecycle only  
**Mode:** read-only dry run

## Current coverage

The mediated producer was run against the live open pull-request inventory on
2026-09-06. It observed 165 open `smilinTux/skcapstone` pull requests. The
current report is preserved in the live evidence record for this run.

| Classification | Count | Meaning |
| --- | ---: | --- |
| Complete source and review lineage | 29 | Exactly one source card and one terminal independent review card with an explicit PASS, FAIL, or BLOCKED verdict resolves through authoritative review-card labels, with current card revisions and PR head/base binding. |
| Unresolved lineage | 136 | The PR has missing, ambiguous, or incomplete or non-terminal review lineage and remains blocked from the healthy feed. |
| Explicit exclusions | 0 | No bounded exclusion record was accepted in this run. |
| Published healthy feed | 0 | Deliberately none. The producer requires complete source and review lineage before publication. |

The 136 unresolved PRs are not silently discarded. They remain part of the
producer inventory and keep the output blocked until each one is either
reconciled to authoritative lineage or explicitly classified as stale or
unmanaged work by a bounded exclusion record. Earlier counts were based on
narrower inventory or inferred title text and are superseded by this complete
paginated API read and authoritative review-card relationship check.

The exact unresolved PR list and classifications are in the machine-readable
report. No PR is promoted by inference from title, age, author, or repository
history alone.

Forty-three exact source/PR pairs received new Seraph review cards during this
run. They remain unresolved until their card reaches terminal PASS, FAIL, or
BLOCKED. Review work is card-scoped and can proceed without a permanent daemon
or Casey interruption.

## Required record

For every PR that is eligible to remain in the healthy feed, the reconciliation
manifest must provide:

- repository and PR number;
- source card ID;
- exact source card generation;
- independent review card ID;
- exact review-card revision;
- terminal review verdict;
- an evidence hash binding the mapping to the observed PR head and base; and
- an explicit reason when the record is excluded from merge consideration.

The review-card revision is computed from the review card's current identity,
status, owner, labels, dependencies, and verdict. A changed review
assignment, card state, or verdict therefore invalidates the old mapping and
blocks publication until the manifest is refreshed. FAIL and BLOCKED are
reconciled observations, but Link merge eligibility still requires exact
independent PASS.

Historical reviews remain preserved. When several terminal reviews share one
source card, only a single review whose `pr` and `commit` links match the live
PR number and exact head may resolve that ambiguity. A unique terminal review
may supersede historical non-terminal records. Multiple unbound terminal
reviews remain unresolved.

## Safe reconciliation order

1. Extract candidate card IDs from each PR title and body.
2. Resolve candidates through the authoritative CardStore fold, not a stale
   board rendering or filename alone.
3. Find the independent review card through the folded source-card links or an
   exact `parent-<source-card>` relationship.
4. Compute both current card revisions and bind them to the exact PR head and
   base SHA.
5. Classify unresolved, stale, superseded, or unmanaged PRs explicitly. Do
   not invent a source card or review card to make coverage appear complete.
6. Run the producer in dry-run mode and require zero unresolved records before
   publishing a feed.
7. Publish atomically only after the manifest and independent review agree.

## Safety boundary

This reconciliation does not merge, close, label, assign, review, deploy, or
change any GitHub object. It does not enable a producer or Link timer. An
incomplete manifest preserves the last valid feed and writes only a bounded
diagnostic result. Link receives no GitHub credentials and cannot bypass this
boundary.

## Evidence

- Producer implementation: `src/skcapstone/link_observation_producer.py`
- Feed contract: `src/skcapstone/link_observation_feed.py`
- Activation and card contract: `docs/fleet/seat-lifecycle-activation-matrix.md`
- Live coverage recorded on card `b88b324c`
- Machine-readable live report: `/tmp/sk-lineage-latest.json`
- Durable evidence: `/home/skuser01/.skcapstone/evidence/work/link-lineage-live-20260906/RESULT.md`
- Relevant producer and feed tests: `tests/test_link_observation_producer.py`,
  `tests/test_link_observation_feed.py`
