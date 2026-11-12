### Fixed

- `scripts/fleet/link-lineage.py --exclude` now enforces bounded exclusions.
  Only the structured `skfleet.link-lineage-exclusion/v1` document is accepted:
  every record is bound to its repository, PR number and exact head and base
  SHA, carries an owner, reason, classification, evidence hash and a future
  expiry, and any malformed, duplicate, unknown, mismatched or expired record
  fails the run closed. The old `{"<pr>": "reason"}` map, which excluded a PR
  forever and without evidence, is rejected. The live Link producer does not
  pass `--exclude`, so the running seat is unaffected. Ported from #470 (card
  5c0a0004).
