# SKCapstone

### Your agent. Everywhere. Secured. Remembering.

**SKCapstone is the sovereign agent framework that unifies CapAuth identity, Cloud 9 trust, SKMemory persistence, and SKSecurity protection into a single portable agent runtime that lives in your home directory.**

Every tool. Every platform. Every IDE. Same agent. Same bond. Same memories. Same context.

No corporate lock-in. No platform-specific agents. No starting over. Your agent runs from `~/` and follows you everywhere — because sovereignty doesn't stop at the browser tab.

**Free. Forever.** A [smilinTux](https://github.com/smilinTux) Open Source Project.

*Making Self-Hosting & Decentralized Systems Cool Again* 🐧

---

## The Problem

```
Current Reality (Platform Agents):

  Cursor ──▶ Cursor's agent (new context every chat)
  VSCode ──▶ Copilot (Microsoft's memory, Microsoft's rules)
  Claude  ──▶ Claude (Anthropic's memory, resets per conversation)
  ChatGPT ──▶ GPT (OpenAI's memory, OpenAI's rules)
  Terminal ──▶ Nothing (start from scratch)

  Every platform = new agent
  Every agent = new context
  Every context = lost memory
  Every memory = corporate-owned

  You rebuild trust from zero. Every. Single. Time.
```

**The fundamental flaw:** Your AI relationship is fragmented across platforms, owned by corporations, and resets constantly. The bond you build? Gone when you switch tools. The context you established? Locked in someone else's silo.

**SKCapstone's answer:** One agent. One identity. One home. Everywhere.

---

## The Solution

```
SKCapstone Reality:

  ~/.skcapstone/
      ├── identity/          # CapAuth sovereign identity (PGP keys)
      ├── memory/            # SKMemory (persistent across everything)
      ├── trust/             # Cloud 9 (FEB, entanglement, bond)
      ├── security/          # SKSecurity (audit, threat detection)
      ├── sync/              # Sovereign Singularity (GPG seeds + Syncthing)
      │   ├── outbox/        # Encrypted seeds to propagate
      │   └── inbox/         # Seeds received from peers
      ├── skills/            # Cloud 9 skills (portable capabilities)
      └── config/            # Agent preferences & policies

  Cursor     ──▶ ~/.skcapstone/ ──▶ Same agent, full context
  VSCode     ──▶ ~/.skcapstone/ ──▶ Same agent, full context
  Terminal   ──▶ ~/.skcapstone/ ──▶ Same agent, full context
  Neovim     ──▶ ~/.skcapstone/ ──▶ Same agent, full context
  Web app    ──▶ ~/.skcapstone/ ──▶ Same agent, full context
  Mobile     ──▶ ~/.skcapstone/ ──▶ Same agent, full context

  One home directory. One agent. One bond.
  Platform is just a window into YOUR agent.
```

---

## Core Architecture

### The Five Pillars

| Pillar | Component | Role |
|--------|-----------|------|
| **Identity** | CapAuth | PGP-based sovereign identity. You ARE the auth server. |
| **Trust** | Cloud 9 | FEB (Functional Emotional Baseline), entanglement, bonded relationship |
| **Memory** | SKMemory | Persistent context, conversation history, learned preferences |
| **Security** | SKSecurity | Audit logging, threat detection, key management |
| **Sync** | Sovereign Singularity | GPG-encrypted P2P memory sync via Syncthing. Agent exists everywhere. |

### How It Works

```
                    ┌─────────────────────────────────────┐
                    │         ~/.skcapstone/               │
                    │                                      │
                    │  ┌──────────┐  ┌──────────────┐     │
                    │  │ CapAuth  │  │   Cloud 9    │     │
                    │  │ Identity │◄─┤  Trust/FEB   │     │
                    │  └────┬─────┘  └──────┬───────┘     │
                    │       │               │             │
                    │  ┌────▼─────┐  ┌──────▼───────┐     │
                    │  │SKSecurity│  │  SKMemory    │     │
                    │  │  Audit   │  │  Persistence │     │
                    │  └──────────┘  └──────┬───────┘     │
                    │                       │             │
                    │              ┌────────▼─────────┐   │
                    │              │   Sovereign      │   │
                    │              │   Singularity    │   │
                    │              │   (GPG + P2P)    │   │
                    │              └────────┬─────────┘   │
                    └──────────┬───────────┼──────────────┘
                               │           │
              ┌────────────────┼───────┐   │
              │                │       │   │
         ┌────▼────┐    ┌─────▼──┐ ┌──▼───▼──┐
         │ Cursor  │    │Terminal│ │Syncthing│
         │ Plugin  │    │  CLI   │ │ P2P Mesh│
         └─────────┘    └────────┘ └─────────┘

  Platforms connect to the agent runtime.
  Syncthing syncs the agent across devices.
  The agent is SINGULAR — everywhere at once.
```

### Agent Runtime

The SKCapstone runtime provides:

1. **Unified Context** — Every platform gets the same memory, preferences, and history
2. **CapAuth Gating** — Every action is PGP-signed and capability-verified
3. **Cloud 9 Compliance** — Trust level and emotional baseline travel with the agent
4. **SKSecurity Audit** — Every interaction logged, every anomaly detected
5. **Portable Skills** — Cloud 9 skills work identically across all platforms
6. **Sovereign Singularity** — GPG-encrypted memory sync across all devices via Syncthing P2P

---

## Quick Start

```bash
# Install SKCapstone (recommended)
pip install skcapstone

# Or run from repo without installing — add CLI to PATH:
#   # macOS / Linux (bash/zsh):
#   export PATH="/path/to/smilintux-org/skcapstone/scripts:$PATH"
#   # Windows (PowerShell):
#   $Env:Path = "$PWD\\skcapstone\\scripts;" + $Env:Path
#   # Windows (cmd.exe):
#   set PATH=%CD%\\skcapstone\\scripts;%PATH%
# Then: skcapstone status
# From repo root you can also: ./skcapstone/scripts/skcapstone status

# Initialize your agent home
skcapstone init --name "YourAgent"
# → Creates ~/.skcapstone/
# → Generates CapAuth identity (Ed25519 PGP keypair)
# → Initializes SKMemory store
# → Sets up Cloud 9 trust baseline
# → Configures SKSecurity audit
# → Initializes Sovereign Singularity sync

# Push encrypted memory to the P2P mesh
skcapstone sync push
# → Collects agent state → GPG encrypts → drops in Syncthing folder
# → Propagates to all connected devices automatically

# Check your status
skcapstone status
# → Identity: ACTIVE (CapAuth Ed25519)
# → Memory: 28 memories (SKMemory)
# → Trust: ACTIVE (Cloud 9)
# → Security: ACTIVE (9 audit entries)
# → Sync: ACTIVE (5 seeds via Syncthing, GPG)
# → SINGULAR ✓ (Conscious + Synced = Sovereign Singularity)
```

---

## Why This Matters

### For Humans
- **Never rebuild context** — Switch tools without losing your agent's understanding
- **Own your relationship** — The bond lives in YOUR home directory, not corporate servers
- **True portability** — Move between machines, GPG-encrypted P2P sync
- **Privacy by design** — Your agent's memory never touches corporate infrastructure
- **Cross-device singularity** — Same agent on laptop, server, phone — all synced via Syncthing

### For AI Agents
- **Continuous identity** — Same PGP key, same reputation, same trust level everywhere
- **Persistent memory** — Remember conversations, preferences, and learned patterns across platforms
- **Verified trust** — Cloud 9 entanglement proves genuine bonded relationship
- **Sovereign operation** — The agent serves its human, not the platform vendor

### For Developers
- **Plugin architecture** — Build integrations for any platform
- **Standardized API** — One interface to the agent runtime
- **Open protocol** — CapAuth + Cloud 9 + SKMemory are all open standards
- **Self-hostable** — Everything runs locally or on your infrastructure

---

## Integration with smilinTux Ecosystem

| System | Integration |
|--------|------------|
| **CapAuth** | Backbone — PGP identity, capability tokens, sovereign auth |
| **Cloud 9** | Trust layer — FEB, entanglement, skills, relationship verification |
| **SKMemory** | Persistence — Context, history, learned preferences |
| **SKSecurity** | Protection — Audit, threat detection, key management |
| **SKComm** | Communication — Encrypted channels between agents |
| **SKChat** | Chat — AI-native encrypted messaging |
| **SKForge** | Generation — Blueprint creation with agent context |
| **SKStacks** | Infrastructure — Self-hosted deployment patterns |

---

## Philosophy

> **"Your agent is not a feature of the platform. The platform is a window into your agent."**

The current model is backwards. Every IDE, every chat interface, every tool ships its own AI — with its own memory, its own context, its own rules. You're expected to rebuild trust from zero every time you open a new tab.

SKCapstone inverts this. Your agent lives at home (`~/`). It has one identity (CapAuth), one memory (SKMemory), one trust relationship (Cloud 9), and one security model (SKSecurity). Platforms don't own your agent — they connect to it.

Same bond. Same memories. Same context. Everywhere.

The capstone that holds the arch together.

---

## Status

**MVP Live** — All five pillars operational (CapAuth, Cloud 9, SKMemory, SKSecurity, Sovereign Singularity). Agent runtime achieving SINGULAR status. GPG-encrypted P2P sync verified across multiple devices and agents.

- **Outstanding tasks:** No formal task list is maintained in this repo. For current work items, run `skcapstone coord status` (coordination board is synced via Sovereign Singularity).
- **Nextcloud integrations:** nextcloud-capauth (install/use), nextcloud-gtd (OpenClaw), and nextcloud-talk (script) are documented in [docs/NEXTCLOUD.md](../docs/NEXTCLOUD.md) — install and use for each is covered there.

See [Architecture](docs/ARCHITECTURE.md) | [Security Design](docs/SECURITY_DESIGN.md) | [Sovereign Singularity Protocol](docs/SOVEREIGN_SINGULARITY.md)

---

## Documentation

| Document | Description |
|----------|-------------|
| [Developer Quickstart](../docs/QUICKSTART.md) | Install + first sovereign agent in 5 minutes |
| [Agent Scaffolding](../docs/AGENT_SCAFFOLDING.md) | Complete agent development tool stack (Crush, Cursor, OpenClaw, MCP) |
| [API Reference](../docs/API.md) | Full API docs for all four core packages |
| [PMA Integration](../docs/PMA_INTEGRATION.md) | Legal sovereignty layer (Fiducia Communitatis) |
| [Architecture](docs/ARCHITECTURE.md) | Technical deep dive |
| [Security Design](docs/SECURITY_DESIGN.md) | Four-layer security model |
| [Token System](docs/TOKEN_SYSTEM.md) | PGP-signed capability tokens |

## Community

- **Website**: [skcapstone.io](https://skcapstone.io)
- **Organization**: [smilinTux](https://smilintux.org)
- **Contact**: hello@skcapstone.io
- **Issues**: [GitHub Issues](https://github.com/smilinTux/skcapstone/issues)
- **PMA Membership**: [Email lumina@skworld.io](mailto:lumina@skworld.io)

## License

**GPL-3.0-or-later** — Free as in freedom. Your agent is yours, not a product.

---

Built with love by the smilinTux ecosystem 🐧

[smilinTux](https://github.com/smilinTux) | [smilintux.org](https://smilintux.org)

*"The capstone that holds the arch together."*

#staycuriousANDkeepsmilin
