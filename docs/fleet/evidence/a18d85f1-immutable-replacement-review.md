# Card a18d85f1 immutable replacement review

Verdict: PASS

Reviewer identity: `pi-codex-a18d85f1`

Producer identity: `pi-codex-c84987dd`

Producer PR: <https://github.com/smilinTux/skcapstone/pull/243>

## Byte identity

The durable shared partition 01 report is 333781 bytes and recomputes to SHA-256 `e5e4ea5f48fbeafb8bf7aff8da510c0546bf7015c6f3b458accd8bccac38ad37`. The byte-addressed report, named replacement report, sidecar, report entry in the manifest, and links on card a18d85f1 agree. The two report paths contain identical bytes.

The durable shared bundle manifest is 3399486 bytes and recomputes to SHA-256 `7bd3b9c94a39bb1ac51fb64f068fdd6ae41a69c0eced5501c6d63f2e367a1bf3`. This agrees with the final manifest hash linked on cards c84987dd and a18d85f1.

The producer's published rebuild script was independently fetched from open PR 243 at commit `8d7ad3da4e8dbfaa3097452f6556a59f93ed0e0f`, parsed, and run against a scratch output directory. The fresh output necessarily differs because the script records a new capture timestamp and observes later append-only source growth. Review of the implementation confirms stable reads, per-line JSON parsing before snapshot publication, content-addressed snapshots, canonical JSON serialization, and no source-store write.

## Snapshot verification

The full bundle manifest contains 4259 source entries, including 3827 append-only JSONL entries. Every one of the 3827 JSONL snapshots was independently checked for:

* exact `cutoff_bytes` length
* exact `cutoff_json_lines` parsed line count
* successful JSON parse of every line
* equality of `snapshot_sha256`, `prefix_sha256`, and `exact_bytes_sha256`
* content-addressed snapshot filename equality with the recomputed SHA-256
* exact equality of the current source prefix through `cutoff_bytes`
* recorded source path and capture provenance fields for device, inode, mode, and capture mtime

All 3827 checks passed. Partition 01 cites 188 frozen source entries, including 113 JSONL snapshots. Each cited entry equals its corresponding full bundle manifest entry. All 113 cited snapshots passed the same byte, line, parse, hash, path, and prefix checks.

The source report and blocked review bytes also recompute to their declared hashes. Later filesystem metadata changes do not alter the reviewed identity because the report binds exact source prefixes and immutable snapshots. Provenance remains the recorded capture identity and is not treated as evidence of a verdict.

## Structural and evidence-store join

The embedded report preserves 42 selected cards, including 3 archived cards, from the complete partition selection. It retains structural status, terminal state, claims, dependencies, labels, and source files separately from `separate_evidence.events` and folded link annotations. The report expressly states that links are annotations and that no PASS is inferred from done, archive, completion, or links.

The report keeps all historical evidence visible, including the explicit PASS event and separate BLOCKED annotations for card 16bbc6fe. It does not collapse those outcomes into structural lifecycle state. Its three classifications are backed by frozen structural CardStore bytes, frozen per-card event bytes, separate evidence bytes or annotations, and dependency joins.

## Remedy support and authority

All three remedy proposals were checked against the frozen cited bytes:

1. Card 018bf488 retains c969dfb8 as the active dependency, preserves the completed 04b218cd dependency as non-authoritative, and routes missing evidence reconstruction to a stronger reviewer without inventing PASS authority.
2. Card 16bbc6fe preserves both the historical source-read-only PASS event and the later explicit BLOCKED correction. The proposal requests append-only governance amendment and fresh review rather than erasing either outcome.
3. Card 6e15b435 retains the live human gate 0a0a66ce and its explicit denial. The proposal permits no machine discharge and reserves approval or void authority to Chef.

The proposals apply no repair and preserve human authority and historical non-PASS outcomes.

## Scope

No repair application, lifecycle change, dependency change, deployment, restart, gateway change, configuration change, credential access, human signoff, merge, or live action was performed. The only repository change is this review evidence publication.
