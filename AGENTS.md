# SKCapstone — Agent Onboarding (Universal)

This file provides instructions for ANY AI agent working on SKCapstone,
regardless of tool, IDE, or platform.

## 🔴 Orchestrating the fleet? Run FROM the estate you are controlling

If you are on `chiap08` (or any chi host) and about to run `skfleet`,
`skcapstone fleet`, or anything that touches fleet readiness/rollout: a
controller must run **inside** the estate it controls. `nor` (`noroc2027`)
and `chi` are disjoint Syncthing shares with separate CardStores, so running
a chi rollout from `noroc2027` reads every chi readiness verdict as
**MISSING**, not "unhealthy" — the rollout halts on node one as if it failed
a real check. Full rule, verified reach/versions, and the known-good
entrypoints (`skfleet rollout`, dry-run by default): see
[`docs/fleet/runbook-chiap08-controller.md`](docs/fleet/runbook-chiap08-controller.md).

## Step 1: Learn the Coordination Protocol

```bash
skcapstone coord briefing
```

This single command prints everything you need:
- The complete multi-agent coordination protocol
- JSON schemas for tasks and agent files
- CLI commands for all operations
- A live snapshot of current tasks and who's working on what

For machine-readable output:

```bash
skcapstone coord briefing --format json
```

## Step 2: Check In

```bash
skcapstone coord status
```

See what tasks are open, what's claimed, and which agents are active.

## Coordination Write Boundary

All worker verdict, evidence, status, claim, label, dependency, and lifecycle
writes must use `skcapstone coord`. Never create, append, rewrite, rename, or
delete CardStore JSONL directly. Use CLI reads for normal verification. Raw file
inspection is reserved for emergency operator diagnostics.

Good:

```bash
# A PASS that owes an independent review must bind the bytes the reviewer
# verifies. coord link has no field for a candidate, so it refuses this class.
skcapstone coord verdict <card_id> PASS_FOR_REVIEW \
  --candidate ~/.skcapstone/evidence/work/<card_id>/<file> \
  --commit $(git rev-parse HEAD) --tree $(git rev-parse HEAD^{tree}) \
  --ref refs/heads/<branch> --agent <your_name>
# Record it LAST: anything written after a verdict supersedes it.

# A terminal outcome (plain PASS, BLOCKED) has no candidate to bind:
skcapstone coord link <card_id> verdict PASS --agent <your_name>
skcapstone coord link <card_id> evidence <repo-relative-path> --agent <your_name>
skcapstone coord move <card_id> review --agent <your_name>
```

Bad: opening a file below `cards/<card_id>/events/` or
`coordination/card_events/` and writing JSONL yourself.

## Step 3: Claim Work

```bash
skcapstone coord claim <task_id> --agent <your_name>
```

## Step 4: Do the Work

Follow the project conventions:
- Python 3.11+, PEP 8, type hints, black formatting
- Pydantic for data models
- Pytest tests (happy path + edge case + failure case)
- Google-style docstrings on every function
- Max 500 lines per file

**Changelog: add a NEW file, never edit `CHANGELOG.md`.** Any PR touching `src/**`
or `pyproject.toml` must record a changelog entry or the `docs / docs-check`
tier-2 gate fails it. Record it as a new fragment:

```bash
cat > changelog.d/<your-branch-slug>.md <<'ENTRY'
- Card `<id>`: what changed, and what was observably wrong before.
ENTRY
```

One file per PR means two concurrent PRs never touch the same lines, so the
rebase conflict that a shared `CHANGELOG.md` guarantees cannot happen. Editing
`CHANGELOG.md` directly still satisfies the gate, but with other PRs open you
will be resolving a conflict. See `changelog.d/README.md`.

## Step 5: Complete

```bash
skcapstone coord complete <task_id> --agent <your_name>
```

## Step 6: Create Discovered Work

```bash
skcapstone coord create --title "What needs doing" --by <your_name>
```

## Directory Reference

| Path | Purpose |
|------|---------|
| `src/skcapstone/` | Core framework modules |
| `tests/` | Pytest test suite |
| `docs/` | Architecture and design docs |
| `~/.skcapstone/coordination/` | Syncthing-synced task board |
| `~/.skcapstone/coordination/tasks/` | Task JSON files (immutable) |
| `~/.skcapstone/coordination/agents/` | Agent status JSON files |

## How Sync Works

The `~/.skcapstone/` directory is synchronized via Syncthing across all
devices in the mesh. No SSH, no APIs, no cloud services — just encrypted
peer-to-peer file sync. Create a task here, it appears everywhere.
