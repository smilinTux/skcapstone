# Contributing to skdashboard

Thanks for helping build the SKWorld operator dashboard. This repo follows the SKWorld
[`SK_REPO_DOC_STANDARD`](https://github.com/smilinTux/sk-standards): docs and tests are
part of "done". Read [`SOP.md`](SOP.md) first, especially section 5, because how this
package is deployed is not how it looks.

## The one thing that surprises everyone

**`skdashboard` has no entry point of its own.** `pyproject.toml` declares no
`[project.scripts]`, and there is no `skdashboard` systemd unit. The running service is
`skcapstone-dashboard.service`, whose ExecStart is
`~/.skenv/bin/skcapstone dashboard --port 7778`, and the `skcapstone dashboard` CLI
resolves `skcapstone.dashboard` to this package through an alias shim that lives in
**skcapstone**, not here.

Two consequences for your change:

1. To see your change on a live seat you must reinstall **and restart the unit**. An
   editable install alone changes nothing already imported.
2. Do not add a console script or a unit file as a drive-by. Whether this package should
   own its own process is an open question, recorded under
   "Unverified / needs an operator pass" in `SOP.md`. Raise it as its own change.

## Setup

```bash
git clone https://github.com/smilinTux/skdashboard
cd skdashboard
python -m pip install -e .
python -m pip install ruff pytest
```

Full history matters: `setuptools_scm` derives the version from the git tag, so a
shallow clone produces a placeholder version and a broken build. Do not `--depth 1`.

## Branch model

- `main` is always releasable and protected. **Never push to `main` directly, and never
  push a tag**: a push to `main` cuts the next patch tag automatically and that tag
  publishes to PyPI.
- Branch per unit of work with a conventional prefix: `feat/<slug>`, `fix/<slug>`,
  `docs/<slug>`, `chore/<slug>`, `refactor/<slug>`, `ci/<slug>`.
- Open a PR against `main`. Keep branches focused.

## Commit convention

- Conventional Commits: `type(scope): summary`, for example
  `feat(dashboard): change.* validate/schedule/arm PEP routes`.
- When a change maps to a coordination card, reference the card id in the subject or
  body.
- Every commit ends with the trailer:

  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```

## Writing style (hard rule)

**Do not use em dashes or en dashes anywhere**: not in docs, not in code comments, not
in commit messages, not in PR bodies. Restructure with a comma, parentheses, a colon, or
a new sentence. Regular hyphens are always fine. This is enforced by review, and
`dashboard_economy.py` says so in its own module docstring.

## The green bar

Merge is blocked on CI (`.github/workflows/ci.yml`). Run the same thing locally before
opening a PR:

```bash
ruff check src/ tests/
python -m pytest tests/ -q
```

On a fleet seat, use the venv the service runs from:

```bash
~/.skenv/bin/python -m pytest tests/ -q
```

Rules:

- **A bug fix adds a regression test** that fails before the fix and passes after.
- **New routes get tests.** `tests/test_cm_p2_change_routes.py` is the reference pattern
  for a route suite; `tests/test_queue_gate_enforcement.py` is the reference for proving
  a gate actually denies.
- **Do not weaken the test install.** The `test` job installs the real dependencies
  (`skcapstone starlette pytest`) and only then lays this checkout on top with
  `--no-deps -e .`. It used to install with `--no-deps` alone, so `dashboard_kanban`'s
  module-scope `skcoord` import and the queue tests' `skcapstone` import failed at
  **collection**, and the kanban, ITIL and queue tests silently never ran while the job
  still looked like a test job. Do not reintroduce that.
- `tests/test_smoke.py` deliberately does not import `skdashboard.dashboard`. Keep it
  that way; it is the check that the package itself is intact.
- **The secret-scan gate runs the gitleaks binary over the full history.** If it goes
  red, a secret was added: rotate it and purge it. Do not narrow the scan.

## Documentation is part of the change

- **`CHANGELOG.md` is required** for anything touching `src/**` or `pyproject.toml`. The
  docs-check gate enforces it on a PR. Add your entry under `[Unreleased]`.
- **If you change a documented fact, update `SOP.md` in the same PR.** The
  `docs-evidence` block at the bottom of `SOP.md` is executable: the port, the loopback
  bind, the presence of `/api/doctor`, the absence of a console script, and the
  dependency direction are all pinned by a grep. If your change is legitimate, update
  both the prose and the check. If a check fails, the doc is now wrong, and that is the
  point.
- Adding a check to `docs-evidence` is welcome. Checks must be **hermetic** (repo-local,
  no network, no `systemctl`, no live service) and **cheap** (seconds). Prove it can
  fail: break the fact, confirm the command exits non-zero, restore.

## Code conventions

- Python 3.10 and up. `ruff` with `line-length = 99` and rule sets `E`, `W`, `F`, `I`
  (`E501` ignored); configuration lives in `pyproject.toml`.
- **Dependency direction is a rule, not a preference.** Coordination access goes through
  `skcoord` (`skcoord.card`, `skcoord.card_store`, `skcoord.coordination`, `skcoord.itil`,
  `skcoord.cmdb`). There must be no `skcapstone.coordination` or `skcapstone.card_store`
  import; a docs-evidence check greps for it. The richer agent, runtime, doctor, trust,
  and model panels reach into `skcapstone` through **lazy imports inside handlers**, which
  is what keeps the two packages free of an import-time cycle. Keep new skcapstone
  imports lazy.
- Optional siblings (`skharness`, `skjoule`) are lazy-imported and must degrade to a
  well-formed empty payload with an `errors` note. A missing optional dependency must
  never 500 a panel.
- New privileged routes go through `queue_authz.authorize_capability` (or
  `authorize_queue`). Do not hand-roll a check. Keep it fail-closed: any error, any
  unreachable PDP, any missing secret is a **deny**.
- Do not widen the uvicorn bind off `127.0.0.1`. Remote access is an operator decision
  made outside this repo, and today the bind is the access control (see `SECURITY.md`).
- New static assets go under `src/skdashboard/static/` and ship via
  `[tool.setuptools.package-data]`. There is no build step and no npm; keep it that way.

## Security issues

Do not open a public issue. Follow [`SECURITY.md`](SECURITY.md): GitHub private
vulnerability reporting, acknowledgement within 72 hours.

## Code of conduct

By participating you agree to [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md).
