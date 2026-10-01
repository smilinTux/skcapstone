# Private production handoff staging

Card: b189af7e

Authorized scope: source-only correction based on commit
37011682fcc2351fada0f455c7db0a1c39e1ab6f. Local commits are authorized;
installation and the live trial remain parent responsibilities.

## Changes and acceptance

- `src/skcapstone/fleet/production_brief.py` opens every evidence directory
  component with `O_DIRECTORY | O_NOFOLLOW`, creates missing directories with
  mode 0700, and requires the exact card directory to be owned by the current
  user and mode 0700. It creates a unique mode 0600 candidate through the held
  directory descriptor and copies the previously verified committed report.
- Existing unsafe directories are refused, never silently chmodded. Symlink
  components are refused before staging candidate bytes or board writes.
- The redundant `evidence` link is removed. Required `commit_sha` and `branch`
  links remain because native `coord_completion._commit_evidence_problem`
  requires them for repository-labeled cards. Review generation and source
  transport consume the typed verdict candidate fields. The external verifier
  reads the committed report rather than the removed link.
- Tests execute the rendered recipe against real Git and a mediated native CLI
  stand-in, including permissive umasks, existing unsafe/private directories,
  symlink components, dirty or uncommitted evidence, wrong owner/claim, detached
  or main branches, linked worktrees, and unrelated source bases.

## Validation

Focused test command:
`python -m pytest tests/fleet/test_production_brief.py -q`

Actual result: 26 passed, zero skipped, 2.42 seconds.

Integration command: `python -m pytest tests/fleet/test_production_brief.py
tests/fleet/test_production_builder.py tests/fleet/test_production_exit.py
tests/fleet/test_builder_dispatch.py -q`.

Actual result: 138 passed, zero skipped, 8.22 seconds. Ruff, Black check and
`git diff --check` passed. Independent review remains pending and will be
recorded separately against the exact candidate commit.

## Limitations and rollback

This leaf deliberately preserves the existing typed-verdict behavior. It does
not claim atomic board metadata publication: authority guarded CLI support is
not yet present on the execution nodes. Guarded parity is a separate source
prerequisite. The read-before-write owner check does not close that race.

No live worker candidate, trial verdict, configuration, runtime or service was
modified. An existing unsafe evidence directory requires an attributable
operator repair; the worker refuses it. Rollback is the prior source commit,
with existing evidence and native event history preserved.
