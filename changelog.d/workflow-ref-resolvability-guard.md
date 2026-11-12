### Added
- `scripts/ci/workflow_refs.py`: a gate that proves every `uses:` ref in
  `.github/workflows/` is a full 40-hex sha and still resolves upstream. On
  2026-09-19 `docs / docs-check` twice stopped publishing a check run at all
  (once from a pin to a squash-merged PR head, once from a 12-char abbreviated
  sha). A workflow that cannot resolve its `uses:` dies before creating a job
  and so publishes no check run, which makes a required gate go ABSENT rather
  than red. It runs under `unit tests` and `shim-imports`, required contexts
  with no cross-repo `uses:` of their own, because a broken `docs-check` ref
  cannot be caught by `docs-check`.
- `CONTRIBUTING.md`: the observability invariant that generalises it. A green
  that can be produced by absence is not a green.
