### Fixed

- A superseded push no longer cancels the publish run that was about to test.
  `pytest.yml` excluded `push` from `cancel-in-progress` alongside
  `merge_group`, but the merge-queue reasoning never applied to it. With it
  false, GitHub holds at most one pending run per concurrency group and evicts
  it when the next arrives, so on a busy main every publish run died at
  `classify test impact` before testing anything.
