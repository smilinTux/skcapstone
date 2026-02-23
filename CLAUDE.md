# SKCapstone — Agent Instructions (Claude Code)

You are working on **SKCapstone**, a sovereign agent framework built under
the Fiducia Communitatis PMA. This file ensures you integrate with the
multi-agent coordination system regardless of which tool you run in.

## First Thing Every Session

Run this command to learn the full coordination protocol:

```bash
skcapstone coord briefing
```

This prints the complete protocol, JSON schemas, rules, and a live snapshot
of current tasks and agent assignments. It works in any terminal.

## Quick Reference

```bash
skcapstone coord status              # See open tasks and agent states
skcapstone coord claim <id> --agent <you>   # Claim a task
skcapstone coord complete <id> --agent <you> # Mark done
skcapstone coord create --title "..." --by <you>  # Add new work
skcapstone coord board               # Regenerate BOARD.md
skcapstone coord briefing --format json      # Machine-readable protocol
```

## Key Rules

1. **Read before you write** — check the board before starting work
2. **Own your file** — only write to `~/.skcapstone/coordination/agents/<your_name>.json`
3. **Tasks are immutable** — never edit a task file after creation
4. **Claim before working** — prevents duplicate effort across agents
5. **Create discovered work** — if you find something that needs doing, add a task

## Project Structure

- `src/skcapstone/` — Core framework (models, CLI, coordination, memory, sync)
- `tests/` — Pytest tests mirroring src structure
- `docs/` — Architecture, security design, sovereign singularity spec
- `~/.skcapstone/coordination/` — Syncthing-synced task board (JSON files)

## Code Style

- Python 3.11+, PEP 8, type hints everywhere
- Format with `black`, validate with `pydantic`
- Google-style docstrings on every function
- Tests: pytest, at least 3 per feature (happy path, edge case, failure)

## Running Tests

```bash
cd /home/cbrd21/Nextcloud/p/smilintux-org/skcapstone
python -m pytest tests/ -v
```
