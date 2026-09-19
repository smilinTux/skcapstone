- Added `merge_group:` triggers to `ci.yml`, `pytest.yml`, `secret-scan.yml`,
  and `docs-check.yml` — the prerequisite for a GitHub merge queue. Does not
  enable a queue or change branch protection. `pytest.yml` treats
  `merge_group` identically to `pull_request` (same 2-version matrix, same
  py3.12-full/py3.11-compat-lane split) so per-queue-run CI cost matches a
  PR's; a latent bug where the changelog gate silently passed everything
  under `merge_group` was found and fixed upstream in sk-standards.
