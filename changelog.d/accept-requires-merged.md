### Changed

- `skcapstone coord accept` now refuses unless the reviewed code is on the target branch (default `origin/main`): `merged_commit` (card link or `--merged-commit`) must be an ancestor of `--target-ref` in `--repo-path`, and either the reviewed head is too or the squash patch-id matches. Test logs with skipped tests are refused unless `--allow-skips`, and `--producer` overrides an unknown producer identity (recorded as `producer_identity_override`; producer equal to reviewer is still refused).
