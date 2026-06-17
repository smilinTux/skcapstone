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

## Agent Switching

`SKAGENT` is the primary env var for selecting the active agent across all SK*
packages (skcapstone, skmemory, skcomms, skchat). Legacy vars `SKCAPSTONE_AGENT`
and `SKMEMORY_AGENT` are still supported as fallbacks.

```bash
skswitch lumina               # Switch active agent for this shell session
skswitch opus                 # Sets SKAGENT + SKCAPSTONE_AGENT + SKMEMORY_AGENT
skswitch                      # Interactive picker (if multiple agents)
SKAGENT=jarvis claude         # One-shot override for a single command
```

`skswitch` is installed automatically by `scripts/install.sh` via the agent
picker (`sk-agent-picker.sh`). It validates the agent directory exists and
updates all three env vars in one shot.

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

## Installation

All SK* packages install into a dedicated virtualenv at `~/.skenv/`.

```bash
# Clone and install (creates ~/.skenv/ venv, installs all SK* packages)
git clone https://github.com/smilintux-org/skcapstone.git
cd skcapstone
bash scripts/install.sh

# Adds ~/.skenv/bin to PATH automatically
# Or manually: export PATH="$HOME/.skenv/bin:$PATH"
```

**`scripts/install.sh`** — Creates `~/.skenv/` if it does not exist, then installs all SK* packages into the venv.

**`scripts/dev-install.sh`** — Wrapper around `install.sh` for development workflows (editable installs, extra dev dependencies).

Do NOT use `pip install --user` or system pip for SK* packages. Always use the venv.

## Project Structure

- `src/skcapstone/` — Core framework (models, CLI, coordination, memory, sync)
- `tests/` — Pytest tests mirroring src structure
- `docs/` — Architecture, security design, sovereign singularity spec
- `~/.skcapstone/coordination/` — Syncthing-synced task board (JSON files)
- `cli/upgrade_cmd.py` — Upgrade command implementation
- `mcp_tools/did_tools.py` — DID identity MCP tools

## Code Style

- Python 3.11+, PEP 8, type hints everywhere
- Format with `black`, validate with `pydantic`
- Google-style docstrings on every function
- Tests: pytest, at least 3 per feature (happy path, edge case, failure)

## Running Tests

```bash
# Using venv python
~/.skenv/bin/python -m pytest tests/ -v
# Or activate first:
source ~/.skenv/bin/activate
pytest tests/ -v
```

## Identity (unified resolver)

skcapstone does **not** reimplement identity resolution — it delegates to the one
canonical resolver in CapAuth (epic `2b264064`):

```python
from capauth import resolve_agent_identity
ident = resolve_agent_identity()             # active agent via SKAGENT
# ident.capauth_uri  → capauth:<agent>@skworld.io   (wire)
# ident.fqid         → <agent>@<operator>.<realm>   (sovereign FQID, from cluster.json)
```

`skcapstone doctor` enforces the unified layer via the `identity:*` checks
(`doctor.py::_check_identity_consistency`): resolver importable, self-identity
resolves agent-aware (not the `local` floor), shared
`~/.skcapstone/identity/identity.json` is the **operator** (`role: operator`),
no `@capauth.local` placeholders anywhere, and every provisioned agent (one with
a CapAuth home) carries its own `identity/identity.json`. Run `skcapstone doctor`
after any identity change. (skos T6 `0bac4f62`; supersedes `b5fcf55d`.)

## Agent Capability Manifest (`skcapstone agent profile`)

`skcapstone agent profile [--agent NAME] [--json] [--init]` renders the unified
per-agent capability manifest — **soul** (active overlay), **model** profile
patterns, **MCP servers + exposed tools** (read from `<home>/config/<agent>-mcp.yaml`,
the same file the skchat Telegram tool-router uses), the **bridge curation**
(which of those tools / what voice policy the live bridge exposes), and installed
**skills**. `--init` writes a `profile.yaml` (a `bridge:` block: `tools: default|all|[list]`,
`voice_reply: voice|always|off`) into the agent home; `telegram_bridge.py` reads it
(env `SKC_BRIDGE_*` still wins when set). Code: `cli/agent_profile_cmd.py`.

## MCP Tools

### DID Tools (`mcp_tools/did_tools.py`)

| Tool | Description |
|------|-------------|
| `did_show` | Display the agent's current DID document |
| `did_verify_peer` | Verify a peer's DID and validate their identity |
| `did_publish` | Publish the agent's DID document to the configured tier |
| `did_policy` | View or update the agent's DID publication policy |
| `did_identity_card` | Generate a portable identity card from the agent's DID |
