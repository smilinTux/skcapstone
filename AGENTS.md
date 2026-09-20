# SKCapstone — Agent Onboarding (Universal)

This file provides instructions for ANY AI agent working on SKCapstone,
regardless of tool, IDE, or platform.

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
`coordination/card_events/` and writing JSONL yourself. An agent that did
exactly this left 54 unreadable lines that every fold on every host paid for
until they were cleaned up. `skcapstone coord` is the only write boundary,
with no exception for "just this once."

`core.json` is **not** the card's title. It keeps the birth title write-once;
the *folded* title (what the dispatcher actually reads) comes from event
state, and `authoritative_claimability` overwrites it from the latest
`describe` event. Checking `core.json` and concluding a title is healthy is a
trap: read the folded card, not the birth record.

### `coord describe`: titles are content, not CLI flags

`--title`/`title` take the title text itself, never a flag name and never a
placeholder. 118 real corruption events, 42 in one day, were exactly these
two mistakes:

Good (CLI):

```bash
skcapstone coord describe <card_id> --title "[M] Fix login retry backoff" --agent <your_name>
```

Good (MCP, same event, same effect):

```json
{"tool": "coord_describe", "task_id": "<card_id>", "title": "[M] Fix login retry backoff", "agent": "<your_name>"}
```

Bad, an option NAME typed as the option VALUE (the caller confused the CLI
signature with the MCP one and put `--description` *inside* the title field
instead of the actual text):

```json
{"tool": "coord_describe", "task_id": "<card_id>", "title": "--description", "agent": "<your_name>"}
```

This is refused (`refusing title '--description': it starts with '-'...`),
not written, but only because the guard exists. Put the real title text in
the field; never a flag name.

Bad, a placeholder from testing/poking the tool, also refused:

```json
{"tool": "coord_describe", "task_id": "<card_id>", "title": "x", "agent": "<your_name>"}
```

**Every title needs exactly one size marker: `[S]`, `[M]`, `[L]`, or
`[XL]`.** A title with none is silently un-routable: the dispatcher drops the
card from the candidate scan with no log line, no error, nothing. `[M]` is
the safe default when you're unsure. `[L]` fails closed on any estate that
has no L-class provider admitted, so don't reach for it out of habit.

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
