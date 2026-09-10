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
skcapstone coord link <card_id> verdict PASS_FOR_REVIEW --agent <your_name>
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
