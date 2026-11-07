# AGENTS.md (skcapstone)

The sovereign agent runtime of the SK stack: daemon and consciousness loop, model router,
the `skcapstone coord` board, skscheduler, ITIL tools, and the `skcapstone` CLI plus
`skcapstone-mcp` server. Overview: [README.md](./README.md). Operations and architecture:
[SOP.md](./SOP.md) and [docs/ARCHITECTURE.md](./docs/ARCHITECTURE.md). This file follows
[AGENT_INSTRUCTIONS_STANDARD](https://github.com/smilinTux/sk-standards/blob/main/standards/AGENT_INSTRUCTIONS_STANDARD.md).
There is no `CLAUDE.md`: Claude Code reads this file, and a `CLAUDE.md` next to it would hide it.

## Source of truth and invariants

- **The coord board lives in `~/.skcapstone/coordination/`**, synced by Syncthing. CardStore
  JSONL under it is written only by `skcapstone coord` (or the `coord_*` MCP tools). Never
  create, append, rewrite, rename or delete it by hand; a failed coord call means record the
  error and stop, never fall back to raw writes. Details: [docs/COORD_AGENT_GUIDE.md](./docs/COORD_AGENT_GUIDE.md).
- **A card's title is its folded state, not `core.json`** (the birth record). Every title
  carries exactly one size marker `[S]`, `[M]`, `[L]` or `[XL]`; without one the dispatcher
  drops the card silently. `[M]` is the safe default.
- **Source card completion is two facts:** an independent reviewer's PASS on the exact commit
  with tests green on that commit, and that code merged on the target branch. An operator
  closes it with `skcapstone coord accept <card> --agent <operator> --repo-path <clone>`
  (`src/skcapstone/simple_accept.py`). A card with no review closes with `coord complete`.
- **No implicit agent.** `SKAGENT` is the primary selector; `SKCAPSTONE_AGENT` and
  `SKMEMORY_AGENT` are legacy fallbacks and must agree with it when set.
- **Identity is resolved by CapAuth, not reimplemented here:**
  `capauth.resolve_agent_identity()`. `skcapstone doctor` enforces the unified identity layer
  (`doctor.py::_check_identity_consistency`); run it after any identity change.
- **An agent can never approve an ITIL change.** The CAB guard in skcoord excludes the
  preparer's own vote and requires a human or owner/approver grant. Escalate; do not route
  around it (see the ITIL section of the coord guide).

## Commands

CI: `.github/workflows/ci.yml` (lint, shim-imports, build), `pytest.yml` (unit tests py3.11
and py3.12, the required checks), `providers.yml`, `secret-scan.yml` (gitleaks),
`skmemory-pin-moves.yml` and `docs-check.yml`. All must be green.

```bash
# install (dev: editable with all extras, as CI's test-env action does)
python -m pip install -c deploy/fleet-constraints.txt -e ".[all,dev]"
# end users: bash scripts/install.sh   (creates ~/.skenv and installs every SK package)
# test (CI shards this on py3.12; py3.11 runs scripts/ci/run-python311-compat.sh)
python -m pytest tests/ --strict-markers -m "not integration and not e2e"
# lint and format (CI pins black==26.5.1 ruff==0.15.4)
black --check src/ tests/
ruff check src/
# guard scripts (CI shim-imports job)
bash scripts/check-no-shim-imports.sh
python scripts/check-coord-write-guidance.py
python scripts/ci/workflow_refs.py --resolve
# build
python -m build && twine check dist/*
# docs gate (tiers 1,2,3)
python3 <path-to-sk-standards>/scripts/docs_check.py --repo .
```

Black and ruff use line length 99 (`pyproject.toml`). Black covers `src/` and `tests/`; ruff
only `src/`.

## Start here

1. [SOP.md](./SOP.md): overview, architecture, build, test, release, troubleshooting.
2. [docs/COORD_AGENT_GUIDE.md](./docs/COORD_AGENT_GUIDE.md): working a card as worker or
   reviewer, verdict recipes, cheap board reads. Read it before touching the board.
3. `src/skcapstone/cli/coord.py`: every `coord` verb (`show`, `edit`, `verdict`, `gates`, `accept`).
4. `src/skcapstone/review_verdict.py` and `review_admission.py`: what a review must record to close.
5. `changelog.d/README.md`: the changelog fragment format.

## Rules

- **Writing style:** no em dashes (U+2014) and no en dashes (U+2013) anywhere: code,
  comments, docstrings, docs, commits, PR bodies. Use commas, parentheses, colons or a new
  sentence. Hyphens are fine.
- **Branch and commit:** never commit to `main`. Branch, commit as soon as a fast check passes
  and before the full suite, then open a PR. Never leave edits uncommitted or files untracked
  in a checkout another session may share; prefer a `git worktree` per task.
- **Attribution:** a `Co-Authored-By` trailer names the agent that actually did the work, or
  is left out. Never one you cannot evidence.
- **Changelog:** any PR touching `src/**` or `pyproject.toml` needs an entry or docs-check
  tier 2 fails it. Add a new `changelog.d/<branch-slug>.md` fragment; do not edit
  `CHANGELOG.md` (concurrent PRs conflict on it).
- **Code style:** Python 3.11+, type hints, Pydantic models, Google-style docstrings. Tests in
  pytest covering happy path, edge case and failure.
- **Install:** SK packages go into `~/.skenv/` (CLIs in `~/.skenv/bin/`), never
  `pip install --user` or system pip.
- **Generated files:** `skcapstone context generate` and `refresh-context` write AGENTS.md /
  CLAUDE.md for an agent's own context, not for this repo; never run them at the repo root.
  `src/skcapstone/defaults/claude/CLAUDE.md` is a packaged template for a user's
  `~/.claude/`, not a repo instruction file.
- **Coord guidance is linted:** `scripts/check-coord-write-guidance.py` rejects any text in
  this file, the coord guide, `scripts/fleet`, `systemd` and the prompt builders that tells an
  agent to write CardStore JSONL directly.

## Privacy

This repo is public. Never put host names, LAN or tailnet IPs, estate names, personal names or
data, credentials, or any real memory, message, card or evidence content in code, docs, tests,
fixtures, commits or the site. Use placeholders (`<card_id>`, `<operator>`, `~/.skcapstone/...`).
Enforced by: `secret-scan.yml` (gitleaks, baseline-filtered) for credentials; everything else
is review only.

## Hazards

- **Board-wide reads blow your context.** Bare `coord status` is hundreds of KB and
  `coord kanban --json` over 10 MB. Read one card with `coord show <id>` (about 3 KB), a
  family with `coord status --parent <id>`, and claim refusals with `coord gates <id>`.
- **`coord describe` is a write**, renamed `coord edit` (alias still warns). `--title` takes
  the title text itself; a flag name or placeholder as the value is refused.
- **Review verdicts:** record with `coord link <id> verdict PASS|FAIL|BLOCKED ...` only. Any
  later link whose key contains `verdict`, `outcome`, `result`, `disposition` or
  `review_decision` wins the latest-timestamp race and breaks completion. A PASS also needs
  CI evidence links before `coord complete`. Recipes: the coord guide.
- **`skcapstone fleet` is not the task board** (it is the node/service control plane). Stay in
  `coord` for card work.
- **Run a fleet controller from inside the estate it controls.** Estates have disjoint
  Syncthing shares and CardStores, so a controller elsewhere reads every verdict as missing.
  See [docs/fleet/runbook-chiap08-controller.md](./docs/fleet/runbook-chiap08-controller.md).

## See also

- [skcoord](https://github.com/smilinTux/skcoord): CardStore, fold and ITIL internals behind `coord`.
- [capauth](https://github.com/smilinTux/capauth): identity resolution and PGP keys.
- [skmemory](https://github.com/smilinTux/skmemory): the memory pillar.
- [sk-standards](https://github.com/smilinTux/sk-standards): `SK_REPO_DOC_STANDARD`, `AGENT_INSTRUCTIONS_STANDARD`, `TESTING_AND_CI_STANDARD`.
