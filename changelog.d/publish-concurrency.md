### Fixed

- A pending PyPI environment approval no longer starves every later publish
  run. The workflow-level concurrency group was held by every job in the run,
  including `pypi-publish`, which waits on the `pypi` environment's required
  reviewers, so one unapproved deployment held the slot indefinitely. Run
  34092921687 (2026-09-07) sat in `waiting` for two weeks and 99 of the
  following 100 publish runs were cancelled without ever creating a job, with
  zero successes and no release tag since v0.15.167. The serialization moves to
  the `tag` job, which is the only step that needed it.
