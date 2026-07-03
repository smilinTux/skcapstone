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

## Review path

1. Open a PR against `main` with a clear description and the compliance checklist from
   the doc standard where relevant.
2. CI (pytest + ruff + black) must be green.
3. At least one maintainer review. Security-sensitive changes (daemon HTTP surface,
   auth, secret handling, self-healing) get an extra security-focused pass.
4. Squash or rebase-merge once approved; keep `main` linear and releasable.
5. User-visible changes add a `CHANGELOG.md` entry (Keep-a-Changelog, under
   `[Unreleased]` until the next tagged release).

---

## Where to start

- `docs/ARCHITECTURE.md` — the full runtime reference.
- `SOP.md` — build / test / deploy / config / troubleshooting.
- `src/skcapstone/daemon.py`, `consciousness_loop.py`, `model_router.py` — the core.

Questions: open a discussion or issue on `smilinTux/skcapstone`.
