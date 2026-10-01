# Fresh replacement review context

Card: 6f0d481a. Base: 772f4cba9fcfde15893b466eb52a298d4500a63d.

The actual production trial spent 248.2 seconds on twelve guarded completion
steps. A review snapshot loaded the full legacy board history four times.
Replacement lineage now uses the fresh CardStore already owned by that
snapshot. No store is retained between gateway reads or native operations.
All source, predecessor, authorization and retained-artifact checks remain.

Validation: the new regression failed before the change (four history loads
instead of one). The focused replacement, report digest, publisher and guarded
review-work suites passed: 81 tests in 4.45 seconds. The regression also writes
a real legacy-only predecessor mutation between reads and proves that the
same gateway instance rejects it on the next read. Ruff, Black and git diff
--check passed.

One read-only live comparison of d570f5bf returned identical snapshot hashes
and revisions: installed 4.3777 seconds / four history loads, candidate 1.8547
seconds / one load. Receipt LIVE-READ-BENCHMARK-1790861253001721969.json is held
under this card's private evidence. This is one snapshot sample, not a measured
post-install whole-cycle throughput result.

Runtime scope: review_replacement.py and seraph_review_cardstore.py. No board
mutation, source migration, installation or push. Rollback restores those two
exact preimages; existing claims, evidence and card history stay intact.
