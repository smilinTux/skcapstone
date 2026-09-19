# Contributing to SKCapstone

Thanks for helping build the sovereign agent runtime. This guide covers the branch
model, commit convention, the test gate, and the review path. By contributing you agree
your work is licensed under the repo's **GPL-3.0-or-later** license.

---

## Branch model

- `main` is the always-releasable trunk. Do **not** commit WIP directly to `main`.
- Branch per unit of work, prefixed by type:
  - `feat/<slug>` — new capability
  - `fix/<slug>` — bug fix
  - `docs/<slug>` — documentation
  - `refactor/<slug>` / `chore/<slug>` / `security/<slug>`
- Rebase (or merge) on the latest `main` before opening a PR. Keep PRs focused.

---

## Commit convention

- Short imperative subject, optionally scoped: `daemon: bind API to loopback only`.
- Explain the *why* in the body when it isn't obvious.
- **Every commit MUST end with the `Co-Authored-By` trailer** identifying the AI
  collaborator, e.g.:

  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  ```

- Never commit secrets. API keys and PGP material are environment-sourced (see
  `SECURITY.md`); `.env.example` documents variable names only.

---

## Test gate (required before merge)

The green-bar gate is **pytest** (config in `pyproject.toml`).

```bash
pip install -e ".[dev]"
pytest                       # default unit run — MUST be green
ruff check src tests         # lint — MUST pass
black --check src tests      # format — MUST pass (line-length 99)
```

- `integration`- and `e2e`-marked tests are excluded from the default run; run them
  explicitly when your change touches cross-component or live-daemon behavior
  (`pytest -m integration`, `pytest -m e2e`).
- New behavior needs a test. Prefer test-driven changes (write the failing test first).
- A PR that reds the default `pytest` run, `ruff`, or `black` cannot merge.

---

## Honest-claims gate (docs & release)

Per the smilinTux [SK Repo Doc Standard](https://github.com/smilinTux/sk-standards),
before changing `README.md`, `SOP.md`, `SECURITY.md`, or `CHANGELOG.md`:

- No capability/security claim without in-repo evidence (a self-report command, a test,
  or cited code).
- Scope every claim to its exact surface; do not claim ecosystem-wide properties for a
  single module.
- Forbidden crypto words: "quantum-proof", "unbreakable", "quantum-safe",
  "CNSA 2.0", "FIPS 206", "Falcon". Use "quantum-resistant" / "post-quantum" and cite the
  FIPS number — though note skcapstone itself holds no key material and makes no crypto
  claim (identity/crypto is delegated to capauth).

---

## Observability invariant (read this before adding any check)

**A green that can be produced by absence is not a green.**

Any signal a consumer treats as a pass MUST be able to distinguish *observed and
healthy* from *not observed*. If the two states are indistinguishable downstream,
the signal is not a gate, it is decoration, and it will certify whatever it
stopped looking at.

This is not theoretical. On 2026-09-19 the same defect landed three times in one
day in three disguises, and **not one of them reported red**. They reported
absent, skipped, or truncated, and every consumer rendered that as green:

| Signal | What it did | What it looked like |
|---|---|---|
| `docs / docs-check` | unresolvable `uses:` ref, so the workflow died before creating a job and published **no check run at all** | not failing, so healthy |
| a `skipif`-gated test | gated on a binary no runner has, so it can never fail the build | a green test |
| `gh pr list --limit 60` | returned exactly the limit | a complete list |

The rules that follow from it, all of which CI now enforces:

1. **Absence is its own outcome, never a pass.** Give it a distinct exit path.
   `scripts/ci/required-checks.sh` is the reference implementation: it reports
   `ABSENT` separately from pending and `exit 2`s when branch protection is
   unreadable rather than assuming a list.
2. **"I could not look" must never render as "I looked and it was fine."**
   A guard that cannot reach its evidence exits non-zero. See
   `scripts/ci/workflow_refs.py` exit 2.
3. **Report what you could not read.** A summary computed from partial input
   must say so (`CardStore.dropped`, skcoord #126).
4. **A count is not an identity.** Never conclude from "N checks, 0 failing";
   ask which *required contexts* are present and green. Counting cannot see a
   missing row.
5. **A number in prose with no assertion behind it is a defect.** Put it in
   `docs/fleet/SETTINGS-REGISTRY.md` or back it with a `docs-evidence` check.
6. **A guard that has never been observed failing is a guess.** Every gate in
   this repo ships a negative control that deliberately breaks it and proves it
   goes red (`docs_check.py --self-test`, `workflow_refs.py --self-test`).
7. **A guard cannot be its own witness.** Never let the only check on X live
   inside X. The workflow-ref guard runs in `unit tests` and `shim-imports`,
   two required contexts with no cross-repo `uses:` of their own, precisely
   because a broken `docs-check` ref cannot be caught by `docs-check`.

---

## Review path

1. Open a PR against `main` with a clear description and the compliance checklist from
   the doc standard where relevant.
2. CI (pytest + ruff + black) must be green.
3. At least one maintainer review. Security-sensitive changes (daemon HTTP surface,
   auth, secret handling, self-healing) get an extra security-focused pass.
4. Squash or rebase-merge once approved; keep `main` linear and releasable.
5. User-visible changes add a changelog entry. **Add a new
   `changelog.d/<slug>.md` fragment rather than editing `CHANGELOG.md`.** One file
   per PR means two concurrent PRs never touch the same lines, so the rebase
   conflict a single shared changelog guarantees becomes structurally impossible;
   fragments are folded in by `python scripts/changelog_fragments.py`. Write the
   fragment exactly as you would the `## Unreleased` bullet, Keep-a-Changelog
   style. Editing `CHANGELOG.md` directly still satisfies the gate and is still
   right for a release-assembly commit. Full workflow: `changelog.d/README.md`.

---

## Where to start

- `docs/ARCHITECTURE.md` — the full runtime reference.
- `SOP.md` — build / test / deploy / config / troubleshooting.
- `src/skcapstone/daemon.py`, `consciousness_loop.py`, `model_router.py` — the core.

Questions: open a discussion or issue on `smilinTux/skcapstone`.
