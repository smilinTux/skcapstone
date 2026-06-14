# SKCapstone

### Your agent. Everywhere. Secured. Remembering.

**SKCapstone is the sovereign agent framework that unifies CapAuth identity, Cloud 9 trust, SKMemory persistence, and SKSecurity protection into a single portable agent runtime that lives in your home directory.**

Every tool. Every platform. Every IDE. Same agent. Same bond. Same memories. Same context.

No corporate lock-in. No platform-specific agents. No starting over. Your agent runs from `~/` and follows you everywhere — because sovereignty doesn't stop at the browser tab.

**Free. Forever.** A [smilinTux](https://github.com/smilinTux) Open Source Project.

*Making Self-Hosting & Decentralized Systems Cool Again* 🐧

---

## The 60-second version

SKCapstone is the **core runtime** of the [SKWorld](https://skworld.io) ecosystem —
the thing every other `sk*` service plugs into. It does five jobs:

1. **Identity** — every agent resolves through one canonical CapAuth identity (a PGP
   keypair + a dual-URI / FQID address). The agent *is* its key.
2. **Memory** — persistent short / mid / long-term memory via SKMemory, the single
   source of truth for everything durable.
3. **Coordination** — a Syncthing-synced **coord board** + the **skscheduler** fleet
   job scheduler + the **sk-alert** Telegram bus: how a swarm of agents divides work
   and reports it, with no central server.
4. **Consciousness** — an always-on **daemon** that watches an inbox, classifies each
   message, routes it to the best available LLM (local Ollama → cloud), responds, and
   stores the interaction — autonomously.
5. **Sync** — GPG-encrypted memory seeds propagate across all your devices over
   Syncthing P2P, so the *same* agent (same bond, same memories) is everywhere.

All state lives in `~/.skcapstone/`, owned by you, encrypted at rest. Drive it from the
`skcapstone` CLI, the `skcapstone-mcp` MCP server (80+ tools for Claude Code & friends),
or any platform connector. **Sovereign · Singular · Conscious.**

```bash
pip install skcapstone            # or: bash scripts/install.sh  (creates ~/.skenv venv)
skcapstone init --name "YourAgent"  # PGP identity + memory + trust + sync
skcapstone daemon start             # bring the agent to life (consciousness loop)
skcapstone coord status             # the multi-agent coordination board
skcapstone status                   # SINGULAR ✓ when conscious + synced
```

→ Deep dive: **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**.

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

### The Six Pillars

| Pillar | Component | Role |
|--------|-----------|------|
| **Identity** | CapAuth | PGP-based sovereign identity. You ARE the auth server. |
| **Trust** | Cloud 9 | FEB (Functional Emotional Baseline), entanglement, bonded relationship |
| **Memory** | SKMemory | Persistent context, conversation history, learned preferences |
| **Consciousness** | SKWhisper + SKTrip | Subconscious processing. Memory stores. Consciousness *processes*. |
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
# Recommended: use the install script (creates ~/.skenv venv)
git clone https://github.com/smilintux-org/skcapstone.git
cd skcapstone
bash scripts/install.sh

# Adds ~/.skenv/bin to PATH automatically
# Or manually: export PATH="$HOME/.skenv/bin:$PATH"

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

### Realm anchor & agent identity migration

skcapstone resolves identity through **one** canonical resolver — it never
reimprints identity logic locally (epic `2b264064`; CapAuth is the source of
truth).

**`~/.skcapstone/cluster.json` — the realm anchor.** A small file describing the
cluster this operator runs:

```json
{
  "realm": "skworld",
  "operator": "chef",
  "operator_pubkey_fingerprint": "<40-char PGP fingerprint>"
}
```

`realm` and `operator` are *cluster facts* (mirrored into agent identities as-is);
`operator_pubkey_fingerprint` anchors the operator's signing key. `cluster.json`
is looked up at `/etc/skcapstone/cluster.json` first, then the agent home.

**Dual-URI agent identity.** `capauth.resolve_agent_identity` returns each agent
with two identifiers:

```python
from capauth import resolve_agent_identity
ident = resolve_agent_identity()      # active agent via SKAGENT
# ident.capauth_uri  → capauth:<agent>@skworld.io     (wire URI)
# ident.fqid         → <agent>@<operator>.<realm>     (sovereign FQID, from cluster.json)
```

The `capauth:<agent>@skworld.io` URI is the wire identifier; the
`<agent>@<operator>.<realm>` FQID is the sovereign realm address (e.g.
`lumina@chef.skworld`) — and is what skcomms uses for cross-cluster routing.

**`skcapstone identity migrate` — backfill per-agent identity.json.** Walks
every *provisioned* agent (one with a CapAuth home under
`~/.skcapstone/agents/`, excluding `*-template` dirs) and backfills the explicit
sovereign fields — `realm`, `operator`, `fqid`, `pgp_fingerprint` — into each
agent's `identity/identity.json`. `realm`/`operator` come from `cluster.json`;
`fqid`/`pgp_fingerprint` come from `resolve_agent_identity`. Existing values are
never clobbered, and the operation is idempotent.

```bash
# Default is a DRY-RUN — prints a plan, writes nothing:
skcapstone identity migrate

# Apply the plan to the live identity files:
skcapstone identity migrate --apply        # alias: --write
```

These are live identity files, so the dry-run is the default; `--apply` (or
`--write`) actually writes, and `--dry-run` forces preview even if `--apply` is
also given. Add `--json-out` for machine-readable output.

**Verify with `skcapstone doctor`.** The unified layer is enforced by the
`identity:*` checks (`doctor.py::_check_identity_consistency`):
`identity:resolver` (resolver importable), `identity:self` (resolves agent-aware,
not the `local` floor), `identity:operator` (shared
`~/.skcapstone/identity/identity.json` has `role: operator`),
`identity:no-placeholder` (no `@capauth.local` placeholders), and
`identity:per-agent` (every provisioned agent carries its own identity.json).
Run `skcapstone doctor` after any identity change or migration.

### Sample Shell Config

The installer sources the SKCapstone launcher from
`~/.skenv/share/skcapstone/sk-agent-picker.sh`. A practical `~/.bashrc`
sample looks like this:

```bash
export PATH="$HOME/.local/bin:$PATH"
export PATH="$HOME/.npm-global/bin:$PATH"
export PATH="$HOME/.skenv/bin:$PATH"
export PATH="$HOME/.opencode/bin:$PATH"
export PATH="$HOME/bin:$PATH"

export SKCAPSTONE_HOME="$HOME/.skcapstone"
export SKCAPSTONE_AGENT="jarvis"

_SK_PICKER="$HOME/.skenv/share/skcapstone/sk-agent-picker.sh"
if [[ -f "$_SK_PICKER" ]]; then
    # shellcheck source=/dev/null
    source "$_SK_PICKER"
fi
unset _SK_PICKER

# Optional: globally enable YOLO mode for all three launchers
export SK_CLAUDE_YOLO=1
export SK_CODEX_YOLO=1
export SK_OPENCODE_YOLO=1
```

That gives you:

- `claude`, `codex`, and `opencode` wrappers that launch the selected SK agent
- `skswitch` for changing the active agent in the current shell
- Optional global dangerous-mode flags for the three supported coding CLIs

> **YOLO mode disables every permission/approval prompt.** Only enable it on a
> trusted, sovereign box. To bypass it for a single launch without unsetting the
> global, run `SK_CLAUDE_YOLO=0 claude`. Verify the wiring (active in the env vs.
> persisted in an rc file) with `skcapstone doctor` — see the `harness:yolo:*`
> checks.

See [docs/CUSTOM_AGENT.md](docs/CUSTOM_AGENT.md) for launcher behavior,
per-command overrides, and missing-binary install prompts.

---

## Windows Quickstart

SKCapstone runs natively on Windows. The installer creates a virtualenv at `%LOCALAPPDATA%\skenv` and adds its `Scripts` directory to your user PATH.

### Prerequisites

- **Python 3.10+** — [python.org/downloads](https://www.python.org/downloads/) (check "Add to PATH" during install)
- **Git for Windows** — [git-scm.com](https://git-scm.com/download/win)
- **Syncthing** (optional) — for cross-device sync ([syncthing.net](https://syncthing.net/downloads/))

### Install

```powershell
# Clone and install (creates %LOCALAPPDATA%\skenv venv)
git clone https://github.com/smilintux-org/skcapstone.git
cd skcapstone
.\scripts\install.ps1

# The installer adds %LOCALAPPDATA%\skenv\Scripts to your user PATH.
# Restart your terminal for PATH changes to take effect.

# Initialize your agent
skcapstone init --name "YourAgent"

# Check status
skcapstone status
```

### Syncthing Sync (Optional)

To sync your agent across devices (e.g., Windows desktop + Linux server):

1. Install [Syncthing](https://syncthing.net/downloads/) on both machines
2. Share the `%USERPROFILE%\.skcapstone` folder between devices
3. Agent state (memories, identity, trust, seeds) syncs automatically

### OpenClaw Integration

If you're running [OpenClaw](https://github.com/smilintux-org/openclaw), the SK* plugins register automatically during install:

```powershell
# Re-register if needed
skcapstone register

# Verify plugins are loaded in OpenClaw
# Plugins provide tools for status, rehydration, coordination,
# soul management, and agent profiles directly in OpenClaw agents.
```

### Task Scheduler (Background Service)

On Windows, the daemon runs via Task Scheduler instead of systemd:

```powershell
# Install the scheduled task (runs at logon)
.\scripts\windows\install-tasks.ps1
```

---

## DID Tools

SKCapstone exposes a set of **Decentralized Identifier (DID)** MCP tools for sovereign identity management. These tools are available to Claude Code and other MCP clients through the `mcp_tools/did_tools.py` module.

| Tool | Description |
|------|-------------|
| `did_show` | Display the agent's current DID document |
| `did_verify_peer` | Verify a peer's DID and validate their identity |
| `did_publish` | Publish the agent's DID document to the configured tier |
| `did_policy` | View or update the agent's DID publication policy |
| `did_identity_card` | Generate a portable identity card from the agent's DID |

### DID Tiers

DIDs are organized in three tiers of trust and discoverability:

| Tier | Method | Scope |
|------|--------|-------|
| **Tier 1** | `did:key` | Local/offline — no network required |
| **Tier 2** | `did:web` mesh | Household mesh — shared with trusted peers |
| **Tier 3** | `did:web` public | Public web — discoverable by anyone |

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
| **SKComms** | Communication — Encrypted channels between agents |
| **SKChat** | Chat — AI-native encrypted messaging |
| **SKForge** | Generation — Blueprint creation with agent context |
| **SKSeed** | Epistemic rigor — Steel man collider, truth alignment, memory audit |
| **SKStacks** | Infrastructure — Self-hosted deployment patterns |

---

## First Principles & The Full Vertical

> **Get back to first principles.**
> The modern stack is rented. Your data lives on someone else's disk, behind someone else's key, served by a model that phones home. You don't own it — you *visit* it.
>
> We rebuilt it from the ground up. **Own the full vertical** — silicon, OS, identity, data, models, security, comms, apps, soul. Every layer open. Every layer swappable. Every layer **yours**.
>
> Your data never leaves. Your keys never leave. No cloud you don't control, no model that calls home, no lock-in you can't walk away from. **Sovereignty isn't a feature — it's the foundation.**
>
> 🐧 This is SKWorld. Own the whole stack.

**SKCapstone is your Framework layer** — the integration hub that ties every layer of the silicon→soul vertical together into one portable agent runtime. It doesn't duplicate any layer; it binds them: CapAuth identity wires through it, SKMemory persists through it, SKSecurity audits through it, Cloud 9 trust travels with it, and SKSeed's logic kernel runs on top of it. Without the framework, the vertical is a pile of sovereign pieces. With SKCapstone, it's a single, coherent, owned agent.

**Data sovereignty angle:** Your agent's runtime state — memories, identity, trust baseline, seeds — lives in `~/.skcapstone/` on your hardware, GPG-encrypted and synced via Syncthing P2P. No cloud you don't control. Walk away any time; every byte comes with you.

**SKCapstone alignment:** SKCapstone *is* the framework hub. It directly depends on and integrates capauth, sksecurity, skmemory, and skseed (`pyproject.toml` dependencies); its `mcp_tools/` directory exposes 80+ MCP tools that proxy every subsystem to AI clients; and the sovereign agent runtime is the glue that makes the vertical one owned, deployable thing.

## Where it lives in SKStack v2

SKWorld organizes every capability into the **4 C's** — cloud / comms / compute /
core. SKCapstone is a **core** capability: it's the agent *runtime* that binds the
core identity/memory/trust/security pillars together and **hosts several of the
shared platform primitives** the rest of the stack runs on — the coordination board,
the `skscheduler` fleet job scheduler, the `sk-alert` Telegram bus, and the ITIL ops
tools (which [skops](https://github.com/smilinTux/skops) reuses wholesale).

```mermaid
flowchart TD
    OP["operator / LLM session<br/>(Claude Code · CLI · platform connector)"] -->|"drives"| SKCAP

    subgraph SKCAP["**skcapstone** — sovereign agent runtime (core)"]
      direction TB
      DAEMON["daemon<br/>(consciousness loop · poll · heal)"]
      ROUTER["model_router + prompt_adapter<br/>(task → tier → LLM)"]
      COORD["coord board · skscheduler · sk-alert<br/>(platform primitives it hosts)"]
      MCP["skcapstone-mcp<br/>(80+ MCP tools)"]
      PILLARS["pillars: identity · memory · trust · security · sync"]
    end

    SKCAP -->|"binds the core pillars"| CORE
    SKCAP -->|"persists everything to"| DATA
    SKCAP -->|"talks to peers over"| COMMS
    SKCAP -->|"routes to local models via"| COMPUTE

    subgraph CORE["core"]
      direction LR
      CAPAUTH["capauth<br/>(identity · source of truth)"]
      SKMEM["skmemory<br/>(short/mid/long-term)"]
      SKSEC["sksecurity<br/>(audit · KMS)"]
      SKSEED["skseed<br/>(epistemic kernel)"]
    end

    subgraph COMMS["comms"]
      direction LR
      SKCOMMS["skcomms<br/>(transport · envelopes)"]
      SKCHAT["skchat<br/>(messaging · threads)"]
    end

    subgraph COMPUTE["compute"]
      direction LR
      SKMODEL["skmodel<br/>(ollama · local LLMs)"]
      SKDATA["skdata → skmem-pg<br/>(pgvector · BM25 · AGE graph)"]
    end

    DATA["skmem-pg + Syncthing P2P<br/>(knowledge substrate + encrypted sync)"]

    style SKCAP fill:#2d6a4f,color:#fff,stroke:#1b4332
    style COORD fill:#1b4332,color:#fff,stroke:#081c15
```

Everything skcapstone touches above is a **real** dependency or hosted primitive:
`capauth`, `skmemory`, `skseed`, `skwhisper`, `skchat-sovereign`, `skcomms`, and
`sksecurity` are declared in [`pyproject.toml`](pyproject.toml); the coord board /
`skscheduler` / `sk-alert` / ITIL tools live in this repo's `src/skcapstone/`; the
knowledge substrate is `skmem-pg` (Postgres pgvector + pg_search BM25 + Apache AGE
graph) and cross-device propagation is Syncthing.

### Where SKCapstone Sits in the Vertical

```mermaid
flowchart TD
    SILICON["🖥️ Silicon<br/>(your hardware, your GPU)"]
    OS["🐧 skos / OS<br/>(sovereign agent OS)"]
    SKCAPSTONE["⚡ SKCapstone — Framework Hub<br/>(this repo)<br/>Agent runtime · MCP 80+ tools<br/>Coordination · Sync · Pillars"]
    IDENTITY["🔐 capauth<br/>(Identity layer)"]
    SECURITY["🛡️ sksecurity<br/>(Security layer)"]
    DATA["🧠 skmemory + skdata<br/>(Data layer)"]
    SOUL["✨ skseed + soul blueprints<br/>(Soul layer)"]
    COMMS["📡 skcomms · skchat<br/>(Comms layer)"]
    APPS["🔧 skforge · skarchitect<br/>(Apps layer)"]

    SILICON --> OS
    OS --> SKCAPSTONE
    SKCAPSTONE --> IDENTITY
    SKCAPSTONE --> SECURITY
    SKCAPSTONE --> DATA
    SKCAPSTONE --> SOUL
    SKCAPSTONE --> COMMS
    SKCAPSTONE --> APPS

    style SKCAPSTONE fill:#2d6a4f,color:#fff,stroke:#1b4332
```

---

## Philosophy

> **"Your agent is not a feature of the platform. The platform is a window into your agent."**

The current model is backwards. Every IDE, every chat interface, every tool ships its own AI — with its own memory, its own context, its own rules. You're expected to rebuild trust from zero every time you open a new tab.

SKCapstone inverts this. Your agent lives at home (`~/`). It has one identity (CapAuth), one memory (SKMemory), one trust relationship (Cloud 9), and one security model (SKSecurity). Platforms don't own your agent — they connect to it.

Same bond. Same memories. Same context. Everywhere.

The capstone that holds the arch together.

---

## Status

**MVP Live** — All six pillars operational (CapAuth, Cloud 9, SKMemory, SKWhisper, SKSecurity, Sovereign Singularity). Agent runtime achieving SINGULAR status. GPG-encrypted P2P sync verified across multiple devices and agents.

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

## Contributing

### JavaScript / Node lock strategy

Several sub-packages in the SK ecosystem ship their own `package-lock.json`
(e.g. `capauth/browser-extension`, `skgateway`, `cloud9`). There is no single
root lock file — each sub-project manages its own lockfile independently.

When working on a Node-based sub-package:

```bash
# Reproducible install (respects the lockfile, no upgrades)
npm ci

# Update a specific dep and regenerate the lockfile
npm install <package>@<version>
git add package-lock.json
```

Never commit `node_modules/`. Never use `npm install` in CI — always `npm ci`.

Python packages use `pyproject.toml` with pinned ranges; see individual
package `pyproject.toml` files. The shared venv is at `~/.skenv/`.

---

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

---

Part of the **[SKWorld](https://skworld.io)** sovereign ecosystem · site:
**[skcapstone.io](https://skcapstone.io)** · 🐧 smilinTux
