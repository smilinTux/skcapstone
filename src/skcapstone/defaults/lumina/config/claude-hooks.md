# Claude Code Hooks — Auto-Save Memory

## Setup

Run `skmemory register` to install hooks automatically.

This registers three hooks in `~/.claude/settings.json`:

### PreCompact
- **When**: Before Claude Code compacts conversation context
- **Does**: Saves a snapshot and journal entry to skmemory
- **Script**: `skmemory/hooks/pre-compact-save.sh`

### SessionEnd
- **When**: When a Claude Code session ends (logout, clear, exit)
- **Does**: Saves session-end snapshot and journal entry
- **Script**: `skmemory/hooks/session-end-save.sh`

### SessionStart (compact)
- **When**: After context compaction completes
- **Does**: Re-injects memory context (recent memories, seeds, journal) into the new context
- **Script**: `skmemory/hooks/post-compact-reinject.sh`

## How It Works

All hooks are agent-aware via `$SKCAPSTONE_AGENT` env var:
- `SKCAPSTONE_AGENT=lumina claude` → hooks save to Lumina's memory
- `SKCAPSTONE_AGENT=opus claude` → hooks save to Opus's memory
- Default (no env var): saves to `opus` agent

## Manual Verification

```bash
# Check hooks are registered
cat ~/.claude/settings.json | jq '.hooks'

# Test pre-compact hook
echo '{"session_id":"test","trigger":"manual","cwd":"."}' | /path/to/pre-compact-save.sh

# Re-register if needed
skmemory register
```
