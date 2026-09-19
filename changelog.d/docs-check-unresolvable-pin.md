### Fixed

- **The `docs-check` gate had been absent, not passing, since 08:46Z.** #813
  pinned both `uses:@` and `standards-ref` to `155071f7`, which was PR
  sk-standards#61's HEAD commit on a feature branch. #61 was squash-merged, so
  sk-standards main got a different sha and the branch was deleted, leaving
  `155071f7` reachable from nothing (`compare/main...155071f7` reports
  `diverged`). GitHub could not resolve the reusable workflow, so the run
  failed *before creating any job* - `conclusion=failure` with
  `latest_check_runs_count=0`.

  A workflow that fails at startup publishes no check run, so `docs / docs-check`
  did not go red, it went **missing**: `gh pr checks` simply stopped listing it,
  and every merge to main from 08:46Z onward ran with no docs enforcement at
  all, tier 3 included. Both refs now point at `8a799322`, the squashed commit
  on sk-standards main that actually carries the `changelog.d/` widening.
