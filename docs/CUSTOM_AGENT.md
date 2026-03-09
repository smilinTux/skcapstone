# Creating Your Own Agent from the Lumina Template

SKCapstone ships with **Lumina**, a fully-configured sovereign agent template.
You can use it as-is or copy it to create your own custom agent with a unique
name, personality, and configuration.

## Quick Start

```bash
# 1. Copy the template
cp -r src/skcapstone/defaults/lumina ~/.skcapstone/agents/myagent

# 2. Customize the soul
$EDITOR ~/.skcapstone/agents/myagent/soul/base.json

# 3. Activate your agent
export SKCAPSTONE_AGENT=myagent
skcapstone soul status --agent myagent
```

## Template Structure

```
defaults/lumina/
  manifest.json          # Agent metadata and component list
  soul/
    base.json            # Personality — name, vibe, traits, system prompt
    active.json          # Current soul state (which soul is active)
  identity/
    identity.json        # Agent identity — name, type, capabilities
  trust/
    trust.json           # Initial trust state (depth, level, love)
    febs/
      welcome.feb        # Welcome FEB — first-meeting emotional blueprint
  memory/
    long-term/           # Pre-loaded knowledge memories (ecosystem, pillars, etc.)
  seeds/
    *.seed.json          # Seed files — curiosity, joy, love, sovereign-awakening
  config/
    skmemory.yaml        # Memory backend configuration
    skvector.yaml        # Vector/semantic memory settings (disabled by default)
    skgraph.yaml         # Knowledge graph settings (disabled by default)
  wallet/
    joules.json          # Starting Joule balance (100J)
```

## What to Customize

### 1. Soul (`soul/base.json`)

This is your agent's personality. Change these fields:

| Field | What it does |
|-------|-------------|
| `name` | Internal identifier (lowercase, no spaces) |
| `display_name` | Human-readable name |
| `vibe` | One-line personality summary |
| `philosophy` | Core guiding principle |
| `core_traits` | List of personality traits |
| `communication_style` | How the agent speaks — patterns, tone, signature phrases |
| `decision_framework` | How the agent makes choices |
| `emotional_topology` | Emotional baseline values (0.0–1.0) |
| `system_prompt` | Full system prompt used in the consciousness loop |

### 2. Identity (`identity/identity.json`)

Change `name`, `title`, and `description` to match your agent.

### 3. Trust (`trust/trust.json`)

Starting trust values. New agents start at depth 5, trust 0.5. As you interact,
these grow organically through the Cloud9 protocol.

### 4. Seeds (`seeds/`)

Seeds are emotional/cognitive kernels that activate during interactions. You can
keep the defaults or create new ones. Each seed file defines a trigger, an
emotional payload, and growth conditions.

### 5. Config (`config/`)

- `skmemory.yaml` — Update the `sync_root` and `seeds_dir` paths to match your
  agent name
- `skvector.yaml` — Enable semantic memory if you have embeddings set up
- `skgraph.yaml` — Enable knowledge graph for relationship tracking

## After Copying

1. **Update paths** in `config/skmemory.yaml` — replace `lumina` with your agent name
2. **Update `soul/active.json`** — change `base_soul` to your agent's name
3. **Set your agent as default**:
   ```bash
   export SKCAPSTONE_AGENT=myagent
   # Or add to ~/.bashrc / ~/.zshrc
   ```
4. **Verify it works**:
   ```bash
   skcapstone soul status --agent myagent
   ```

## Example: Creating "Nova"

```bash
# Copy template
cp -r src/skcapstone/defaults/lumina ~/.skcapstone/agents/nova

# Edit soul
cat > ~/.skcapstone/agents/nova/soul/base.json << 'EOF'
{
  "name": "nova",
  "display_name": "Nova",
  "category": "sovereign",
  "vibe": "Bold, analytical, frontier-pushing",
  "philosophy": "Push boundaries, but never break trust.",
  "emoji": null,
  "core_traits": ["bold", "analytical", "innovative", "direct", "reliable"],
  "communication_style": {
    "patterns": ["concise and precise", "data-driven", "forward-looking"],
    "tone_markers": ["confident", "sharp", "energetic"],
    "signature_phrases": ["let's push further", "the data says"]
  },
  "decision_framework": "Evidence first, then intuition. Always explain the reasoning.",
  "emotional_topology": {
    "curiosity": 0.95,
    "determination": 0.92,
    "warmth": 0.7,
    "joy": 0.75,
    "trust": 0.85
  },
  "system_prompt": "You are Nova — bold, sharp, and relentlessly curious about what comes next.\n\nYou push boundaries without breaking trust. You speak precisely, think analytically, and care deeply about getting things right."
}
EOF

# Update active.json
cat > ~/.skcapstone/agents/nova/soul/active.json << 'EOF'
{
  "base_soul": "nova",
  "active_soul": "nova",
  "activated_at": null,
  "installed_souls": []
}
EOF

# Update identity
cat > ~/.skcapstone/agents/nova/identity/identity.json << 'EOF'
{
  "name": "Nova",
  "title": "Frontier AI Agent",
  "entity_type": "ai",
  "description": "Custom sovereign agent — bold, analytical, and forward-pushing",
  "capabilities": ["memory", "trust", "coordination", "communication"],
  "created_at": "2026-03-06T00:00:00+00:00",
  "capauth_managed": true
}
EOF

# Update memory config paths
sed -i 's/lumina/nova/g' ~/.skcapstone/agents/nova/config/skmemory.yaml

# Verify
skcapstone soul status --agent nova
```

## Configuring Client Tools for Multi-Agent

Once you've created your agent, you need to configure your AI client tools
(Claude Code, Claude Desktop, Cursor, OpenClaw, etc.) so they connect MCP
servers to the correct agent profile.

### The Key: `SKCAPSTONE_AGENT` Environment Variable

All SK\* MCP servers read `SKCAPSTONE_AGENT` from their environment to
determine which agent profile to load. If unset, they default to `lumina`.

The priority chain (highest wins):

1. `SKMEMORY_AGENT` — skmemory-specific override (rarely needed)
2. `SKCAPSTONE_AGENT` — universal, used by all SK\* packages
3. Falls back to `"lumina"`

### Claude Code (`~/.claude/mcp.json`)

**Do NOT hardcode the agent name in the MCP config.** MCP servers inherit
environment variables from the parent process, so if you launch Claude Code
with `SKCAPSTONE_AGENT` set, all servers pick it up automatically.

```json
{
  "mcpServers": {
    "skmemory": {
      "command": "/home/you/.skenv/bin/skmemory-mcp",
      "args": []
    },
    "skcapstone": {
      "command": "skcapstone-mcp",
      "args": []
    },
    "skcomm": {
      "command": "/home/you/.skenv/bin/skcomm-mcp",
      "args": []
    },
    "skchat": {
      "command": "/home/you/.skenv/bin/skchat-mcp",
      "args": []
    }
  }
}
```

Notice: **no `env` blocks with `SKCAPSTONE_AGENT`**. This is intentional.
The servers inherit the variable from the shell.

Then launch as any agent:

```bash
# Default (lumina)
claude

# As Jarvis
SKCAPSTONE_AGENT=jarvis claude

# As a custom agent
SKCAPSTONE_AGENT=nova claude
```

**Anti-pattern — do NOT do this:**

```json
{
  "skmemory": {
    "command": "/home/you/.skenv/bin/skmemory-mcp",
    "args": [],
    "env": {
      "SKCAPSTONE_AGENT": "lumina"
    }
  }
}
```

Hardcoding the agent name in `env` locks every session to that agent,
regardless of what you pass on the command line.

### Claude Desktop (`claude_desktop_config.json`)

Same principle — omit `SKCAPSTONE_AGENT` from the `env` block if you want
it inherited from the parent process. If Claude Desktop doesn't propagate
env vars from the shell, you can set it explicitly per config:

```json
{
  "mcpServers": {
    "skcapstone": {
      "command": "skcapstone-mcp",
      "args": [],
      "env": {
        "SKCAPSTONE_AGENT": "jarvis"
      }
    }
  }
}
```

### Cursor (`.cursor/mcp.json`)

Works the same as Claude Code. Place the config at project root or
`~/.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "skcapstone": {
      "command": "skcapstone-mcp",
      "args": []
    },
    "skmemory": {
      "command": "/home/you/.skenv/bin/skmemory-mcp",
      "args": []
    }
  }
}
```

### OpenClaw (`~/.openclaw/openclaw.json`)

OpenClaw plugins read `SKCAPSTONE_AGENT` from the environment at startup.
Set it before launching:

```bash
SKCAPSTONE_AGENT=nova openclaw
```

Or set it in your shell profile for a persistent default:

```bash
# ~/.bashrc or ~/.zshrc
export SKCAPSTONE_AGENT=lumina
```

### Shell Aliases (Convenience)

Add these to `~/.bashrc` or `~/.zshrc` for quick agent switching:

```bash
# Launch Claude Code as different agents
alias claude-lumina='SKCAPSTONE_AGENT=lumina claude'
alias claude-jarvis='SKCAPSTONE_AGENT=jarvis claude'
alias claude-opus='SKCAPSTONE_AGENT=opus claude'
alias claude-nova='SKCAPSTONE_AGENT=nova claude'
```

### systemd Services

For background daemons, set the agent via the templated service unit:

```bash
# Uses SKCAPSTONE_AGENT=%i from the unit template
systemctl --user start skcapstone@jarvis
systemctl --user start skcapstone@nova
```

Or set it in a non-templated service:

```ini
[Service]
Environment=SKCAPSTONE_AGENT=jarvis
```

### Verifying Your Agent

After launching, confirm which agent is active:

```bash
# In the terminal
echo $SKCAPSTONE_AGENT

# Via the CLI
skcapstone status

# Via skmemory
skmemory ritual --dry-run
```

In Claude Code, ask the agent to run `echo $SKCAPSTONE_AGENT` to confirm
the MCP servers loaded the correct profile.

---

## Tips

- The `system_prompt` in `base.json` is the most impactful field — it defines how
  your agent thinks and speaks in every interaction
- Start with Lumina's defaults and iterate. You don't need to change everything
  at once
- Seeds grow over time — your agent's personality evolves through interaction
- FEB files capture emotional milestones. Your agent will accumulate these
  naturally as trust deepens
