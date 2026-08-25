# SKDASH-REL-01 release CI gate evidence

Card: `2f71f2fa`

## Defect and repair

The prior publish workflow started independently on each push to `main`. It
could create and publish a patch release before the CI workflow for the same
commit completed. Main SHA
`843ac88261f7268dfb67f11c4c628967565b66bc` demonstrated the defect when the
release completed before a Python 3.12 CI job reported failure.

The repaired workflow uses GitHub's native `workflow_run` event. Its tag job
runs only after the named CI workflow succeeds for `main`, checks out that
exact tested SHA, and refuses to tag it if a newer main commit has superseded
it. The same gated workflow checks out the exact new tag, builds it, and
publishes it. Manual dispatch, tag-push triggering, and the off-main ancestry
override are absent.

## Sensitivity and checks

`test_release_waits_for_successful_current_main_ci` failed against the prior
workflow because no CI completion trigger existed. It passes only when the
successful-CI condition, exact tested SHA, current-main guard, same-run build
path, and absence of manual dispatch and tag-push triggering are all present.

Source qualification on 2026-08-25:

- pre-repair sensitivity check: 1 failed as expected
- post-repair focused check: 1 passed
- complete test suite: 544 passed
- Ruff check and format check for the new test: passed
- YAML parse: passed

## Release qualification

The pull request must produce no tag because pull-request CI is not a
successful main workflow run. After merge, the exact main CI run must finish
successfully before the next patch tag is created. That same workflow must
then build the exact tag, pass `twine check`, and publish once.

The first qualification exposed that a tag pushed with `GITHUB_TOKEN` does not
trigger another workflow. It correctly created `v0.1.80` at
`5abf88032da9137c7eb1bc96947b89bbd0c17bf2`, but no build or publish run
started and PyPI returned 404 for version `0.1.80`. The tag remains immutable.
The follow-up repair removes reliance on a second workflow event.

A failed, cancelled, skipped, or superseded main CI run must produce no patch
tag. Qualification compares tag targets and workflow timestamps without
creating any tag by hand.

## Rollback

There is no data or runtime migration. If the new gate cannot operate safely,
the fail-closed rollback is a normal reviewed PR that removes the
`workflow_run` trigger while retaining the `v*` tag trigger. This pauses
automatic releases. Do not restore the direct-main trigger, dispatch the
workflow around CI, delete a published tag, or hand-create a replacement tag.

The pre-change source baseline is
`27c8f9ed4b10d8665daac724dcfed9972847f882`, tagged `v0.1.79`. Existing tags
and PyPI artifacts remain immutable during rollback.
