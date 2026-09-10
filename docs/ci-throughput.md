# Governed CI throughput

Card `9c71f24a` removes duplicate pull-request execution without weakening the
declared Python contract. Python 3.12 is the primary runtime. A pull request
runs the complete deterministic suite there once. Python 3.11 builds and
installs the wheel, compiles all source, checks dependencies, imports core
schemas, runs fixed compatibility tests, and runs every changed test file.
The publish workflow calls this workflow on every push to `main`. That call,
the weekly schedule, and release workflow calls run the full suite on every
advertised Python version from 3.10 through 3.14. Pytest has no separate main
push trigger because that would duplicate the publish call's complete matrix.

## Measured baseline

Measurements use GitHub Actions UTC timestamps and logs from successful
pull-request runs on 2026-09-08. Run `34281618144` for PR 541 completed Python
3.11 in 13:24 and Python 3.12 in 12:57. Run `34280790025` for PR 542 completed
both lanes in 13:33 of runner time. Each runtime selected 7,710 tests and
reported 7,673 passed, 38 skipped, and 33 deselected. All four measured pip
cache restores were exact primary-key hits, for a sampled hit rate of 100
percent.

The full pytest step consumed 9:45 to 10:43 per runtime and is the actual
critical path. Installation consumed about two minutes. Duplicate full-suite
execution therefore spends about 10.2 runner-minutes per PR without improving
primary-runtime defect detection. The new lane is expected to remove about 40
percent of pytest workflow runner time. On an uncongested runner, wall time
remains bounded by the Python 3.12 full suite. When Python 3.11 queues behind
other work, the shorter lane removes that duplicated suite from tail latency.
The candidate PR is the authoritative canary for actual timing.

## Documentation-only policy

`scripts/ci/classify-test-impact.sh` prints the exact base, head, count, paths,
and decision. It returns documentation-only only when at least one changed path
is under `docs/` or is a root Markdown file and every changed path meets that
rule. Source, tests, fixtures, workflows, packaging, generated contracts,
deletions outside those paths, and unknown paths fail closed into Python jobs.
Required checks still run and record the decision when Python execution is
omitted.

Dependency consistency is checked immediately after resolving SKCapstone and
all extras. It intentionally precedes exact sibling source overlays, which use
`--no-deps` to prevent sibling repositories from changing the environment under
test. The later import and compatibility tests verify those exact overlays.

## Required-check migration

No live protection changes belong in this source card. Existing check names
remain unchanged, so the initial merge needs no protection change. After the
candidate is green, a repository administrator performs this fail-closed
sequence:

1. Save the current branch-protection JSON and required check names.
2. Prove this exact head reports successful Python 3.11 and 3.12 unit checks.
3. Add any replacement required names while retaining every existing name.
4. Prove old and replacement checks together on one exact head.
5. Remove old names in one update, read back the result, or restore saved JSON.

There is no missing-check interval. The initial change preserves both current
required names. Future changes add replacements before removing old names.

## Failure and rollback

No test command masks failure. Scheduled failures must create attributable
repair work through the normal Link observation path, while exact heads that
already passed their required PR checks retain their evidence. Rollback is one
revert of this workflow, both scripts, and its tests. The preserved required
check names keep rollback compatible with existing protection.
