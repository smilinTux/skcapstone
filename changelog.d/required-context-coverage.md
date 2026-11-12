### Added

- **`scripts/ci/required-checks.sh`: read a PR's mergeability by required
  CONTEXT, not by check count.** On 2026-09-19 `docs / docs-check` stopped
  publishing any check run at all (a reusable-workflow ref pinned to a
  squash-merged PR's head commit, so the workflow died before creating a job).
  A workflow that fails at startup produces no check run, so the gate did not
  go red, it went **absent** - `gh pr checks` simply stopped listing it. Two
  independent readers both saw "10 registered, 0 failing" and called it
  healthy, while main merged for 43 minutes with no docs enforcement at all.

  Counting checks cannot catch that. The script reads the required list from
  live branch protection (never a copy kept in-repo, which would drift the
  same way), reports `N/total green` plus an explicit `ABSENT` count, and
  exits non-zero when any required context published nothing - treating
  absence as its own failure mode, distinct from pending.
