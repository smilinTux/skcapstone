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

## Settings: one declaration per fact

**A number in prose with no assertion behind it is a defect.** That is the whole rule.
It exists because a doc once contradicted *itself* — a table said 32, a paragraph three
paragraphs up said 4 — and nothing detected it, because nothing checks prose against
config. Three readers in a row reasoned from the stale half.

Every fleet and gateway setting is registered in
[`docs/fleet/SETTINGS-REGISTRY.md`](docs/fleet/SETTINGS-REGISTRY.md). Adding one:

1. **A default that ships with the code** → a named constant in `src/` or `scripts/`,
   one row in registry §1, and a **paired** tier-3 assertion in `SOP.md` that extracts
   the number from the code *and* from the registry and compares them. Not a `grep -q`
   for a literal: a literal grep passes when both sides are edited to the same wrong
   value, and fails to notice when only the doc moves.
2. **A per-node value** → a node spec label (`skfleet label <node> <key>=<value>`).
   Not a constant, not a systemd drop-in, not a new file.
3. **A per-host runtime value** → a systemd `Environment=`, plus a dated row in
   registry §2 carrying the command that re-measures it.
4. **A gateway pool ceiling** → the gateway YAML on the owning host, with a dated
   rationale comment naming who measured what, plus a dated row in registry §2.

Anything CI cannot reach (another host's YAML, a systemd unit, a deployed artifact) is
recorded as a **dated measurement with a re-measure command**, never as a standing
claim, and is stated in the registry and **nowhere else**. A second copy is the thing
that rots; tier 3 asserts the single-declaration property for the values that have
already drifted once.

Writing the assertion: it goes in the `<!-- docs-evidence -->` block at the bottom of
`SOP.md`, as `- name:` / `run:` (a one-line shell command, run from the repo root, exit
0 = still true). Prove it can fail before you open the PR — break the config, watch it
go red, put it back. An assertion nobody has seen fail is an assertion nobody has
tested. To forbid a superseded value in prose, use `scripts/docs/prose_grep.sh`, which
strips `SOP.md`'s own evidence block so the check does not match itself.

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
