# Review report digest compatibility

Card: `8b3ec3ea`. Base: `396cb58f6c592d2f2e6013f08af52c75e3b63e87`.

The installed production review brief publishes `review_evidence_sha256` through
guarded native links. The snapshot reader omitted that key, so acceptance saw a
missing report digest despite retained committed report and decision artifacts.
The reader now resolves that published key alongside both historical aliases.
The existing binding function still rejects distinct values across links and
metadata. No claim, generation, replacement, or acceptance guard changes.

Trace: `production_source_review_brief` and `link_cycle` publish the newly
recognized key; the dispatcher and review verdict reader already recognize it.
`production_acceptance.collect` compares the snapshot digest to the committed
decision's report digest. No writer or acceptance change is needed.

Validation: the meaningful prepatch run produced 6 failures and 3 passes. After
the fix, 80 tests passed in 4.76 seconds with zero skips across report digest,
review publication, replacement, and guarded review opening tests. Tests cover
all aliases, conflicting links/metadata, and digest changes affecting snapshot
revision. Logs are retained under the private card evidence directory.

Only the snapshot reader, regression tests, this evidence, and changelog change.
This source candidate does not install anything or mutate live review artifacts
or cards. Rollback restores the exact reader preimage. Production deployment and
actual trial acceptance remain the controller's responsibility. Independent
review and any read-only live comparison are retained separately with exact
candidate and artifact hashes.
