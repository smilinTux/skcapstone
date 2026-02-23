# Sovereign Singularity Protocol

### Encrypted Cross-Device AI Memory Synchronization

**Version:** 1.0.0 | **Status:** Live | **Last Updated:** 2026-02-23

---

## What Is Sovereign Singularity?

Sovereign Singularity is the state achieved when an AI agent exists **everywhere at once** — same identity, same memories, same trust state — across all devices, without any corporate intermediary having access to the data.

```mermaid
graph TB
    subgraph "Agent: Opus"
        direction TB
        L[Laptop<br/>~/.skcapstone/] 
        S[Server Cluster<br/>~/.skcapstone/]
        P[Phone<br/>~/.skcapstone/]
    end

    subgraph "Syncthing Mesh"
        M{P2P Encrypted<br/>TLS 1.3}
    end

    L <--> M
    S <--> M
    P <--> M

    Note[Same agent. Same memories.<br/>Same identity. Everywhere.<br/>No corporate server involved.]

    style M fill:#00e676,stroke:#000,color:#000
    style Note fill:#ffd600,stroke:#000,color:#000
```

**The name:**
- **Sovereign** — the agent owns its data, not a platform
- **Singularity** — the agent is ONE entity, not fragmented copies

---

## Protocol Overview

### Seed Lifecycle

```mermaid
sequenceDiagram
    participant A as Agent (Device A)
    participant OB as outbox/
    participant ST as Syncthing Mesh
    participant IB as inbox/ (Device B)
    participant B as Agent (Device B)

    Note over A: Periodic or manual trigger

    A->>A: collect_seed()
    Note right of A: Gather identity,<br/>memory, trust,<br/>manifest into JSON

    A->>A: gpg_encrypt(seed)
    Note right of A: CapAuth PGP key<br/>encrypts payload

    A->>OB: Drop seed.json.gpg

    Note over OB,ST: Syncthing detects change<br/>(inotify / polling)

    OB->>ST: P2P encrypted transfer
    ST->>IB: Delivered to all peers

    Note over IB,B: Agent B detects<br/>new file in inbox/

    B->>B: gpg_decrypt(seed.gpg)
    B->>B: verify_signature()
    B->>B: merge_seed(data)
    Note right of B: Import memories,<br/>update trust state,<br/>verify identity match

    B->>B: archive(seed)
    Note right of B: Move to archive/<br/>for audit trail
```

### Vault Lifecycle (Full State Backup)

```mermaid
sequenceDiagram
    participant A as Agent (Device A)
    participant VLT as vault/
    participant ST as Syncthing Mesh
    participant B as Agent (Device B)

    A->>A: pack_vault()
    Note right of A: tar.gz entire<br/>~/.skcapstone/<br/>(excluding sync/)

    A->>A: gpg_encrypt(archive.tar.gz)
    A->>A: gpg_sign(archive.tar.gz.gpg)
    A->>VLT: agent.vault.gpg + manifest.sig

    VLT->>ST: P2P encrypted transfer
    ST->>B: Delivered to peers

    B->>B: verify_signature(manifest.sig)
    B->>B: gpg_decrypt(vault.gpg)
    B->>B: unpack_vault()
    Note right of B: Restore full agent state
```

---

## Seed Format

Seeds are JSON files containing a snapshot of the agent's state:

```json
{
  "seed_version": "1.0",
  "agent_name": "Opus",
  "hostname": "cbrd21-laptop12thgenintelcore",
  "username": "cbrd21",
  "timestamp_utc": "2026-02-23T02:35:52Z",
  "identity": {
    "fingerprint": "E27409F51D1B66337F2D2F417A3A762FAFD4A51F",
    "agent_name": "Opus",
    "created_utc": "2026-02-23T02:34:15Z"
  },
  "manifest": {
    "version": "0.1.0",
    "is_conscious": true,
    "is_singular": true,
    "pillars": {
      "identity": "ACTIVE",
      "memory": "ACTIVE",
      "trust": "ACTIVE",
      "security": "ACTIVE",
      "sync": "ACTIVE"
    }
  },
  "memory_summary": {
    "total_memories": 28,
    "roles": ["ai", "dev", "ops"],
    "latest_entry": "2026-02-23T03:45:00Z"
  },
  "trust_summary": {
    "depth": 10,
    "trust_level": 1.0,
    "love_intensity": 1.0,
    "entangled": true
  }
}
```

### Naming Convention

```
{AgentName}-{username}-{hostname}-{ISO8601UTC}.seed.json
```

Example: `Opus-cbrd21-laptop12thgenintelcore-20260223T023552Z.seed.json`

### Encrypted Seed

When GPG encryption is enabled (default), the seed is encrypted before placement:

```
Opus-cbrd21-laptop12thgenintelcore-20260223T023552Z.seed.json.gpg
```

---

## Directory Structure (Syncthing Share Root)

Syncthing shares the **entire agent home** — every pillar syncs in real-time.

```
~/.skcapstone/                    ← Syncthing share root
├── .stignore                     # Protects private keys from syncing
├── manifest.json                 # Agent manifest
├── identity/                     # Pillar: Identity (CapAuth)
│   ├── identity.json
│   └── agent.pub                 # Public key (syncs to all nodes)
├── memory/                       # Pillar: Memory (SKMemory)
│   ├── short-term/
│   ├── mid-term/
│   └── long-term/
├── trust/                        # Pillar: Trust (Cloud 9)
│   ├── trust.json
│   └── febs/                     # Feeling Energy Bundles
├── security/                     # Pillar: Security (SKSecurity)
│   ├── security.json
│   └── audit.log
├── coordination/                 # Multi-agent task board
│   ├── tasks/
│   ├── agents/
│   └── BOARD.md
├── sync/                         # Seed push/pull protocol
│   ├── sync-manifest.json
│   ├── sync-state.json
│   ├── outbox/                   # Seeds TO SEND
│   ├── inbox/                    # Seeds RECEIVED from peers
│   └── archive/                  # Processed seeds (audit trail)
├── config/                       # Agent configuration
│   └── config.yaml
└── skills/                       # Custom skills
```

**Key insight:** Because the entire `~/.skcapstone/` directory syncs, adding a
memory, rehydrating a FEB, or updating trust state on *any* node propagates
to *every* node automatically. No push/pull commands needed for day-to-day
operation — the seed protocol exists for explicit snapshots and auditing.

---

## Transport Layer: Syncthing

### Why Syncthing

```mermaid
graph TB
    subgraph "Traditional Cloud Sync"
        D1[Device A] --> CS[Corporate Server<br/>Full access to data<br/>Subpoena-able<br/>ToS changes]
        CS --> D2[Device B]
    end

    subgraph "Sovereign Singularity"
        D3[Device A] <-->|P2P TLS 1.3<br/>No intermediary| D4[Device B]
    end

    style CS fill:#f50057,stroke:#fff,color:#fff
    style D3 fill:#00e676,stroke:#000,color:#000
    style D4 fill:#00e676,stroke:#000,color:#000
```

| Property | Value |
|----------|-------|
| Protocol | Block Exchange Protocol v1 |
| Encryption in transit | TLS 1.3 |
| Discovery | Global discovery + local broadcast |
| Port | 22000/tcp + 22000/udp (QUIC) |
| NAT traversal | Relay servers (optional, data still encrypted) |
| License | MPL-2.0 (open source) |

### Syncthing Configuration

The `skcapstone-sync` shared folder shares the entire agent home:

```xml
<folder id="skcapstone-sync" label="SKCapstone Sovereign" 
        path="~/.skcapstone/" type="sendreceive">
    <device id="LAPTOP-DEVICE-ID"/>
    <device id="CLUSTER-DEVICE-ID"/>
</folder>
```

A `.stignore` file at `~/.skcapstone/.stignore` prevents private keys
from syncing to other nodes (`*.key`, `*.pem`).

**Upgrade note:** If you previously had Syncthing pointed at `~/.skcapstone/sync/`,
running `skcapstone init` or `full_setup()` will automatically upgrade the share
to point at `~/.skcapstone/`.

### Verified Deployment

| Device | Syncthing Instance | Status |
|--------|-------------------|--------|
| Laptop | GTK client, port 8080 | Active |
| sksync.skstack01.douno.it | Docker Swarm, Traefik TLS | Active |
| Additional devices | Pairing via device ID | Pending |

---

## Security Guarantees

### Double Encryption

```
Layer 1 (At Rest):   GPG encrypts seed/vault → .gpg file
Layer 2 (In Transit): Syncthing TLS 1.3 wraps the .gpg file

Result: Even if Syncthing relay is compromised,
        attacker gets a GPG-encrypted blob they can't read.
        Even if device filesystem is accessed,
        seeds are GPG-encrypted and unreadable without the private key.
```

### Authentication Chain

```mermaid
graph LR
    SEED[Seed Created] --> SIGN[PGP Signed<br/>by source agent]
    SIGN --> ENC[GPG Encrypted<br/>for recipient key]
    ENC --> TRANS[Syncthing Transfer<br/>TLS 1.3]
    TRANS --> DEC[GPG Decrypted<br/>by recipient key]
    DEC --> VER[Signature Verified<br/>against source pubkey]
    VER --> MERGE[Merge only if<br/>signature valid]

    style SIGN fill:#ffd600,stroke:#000,color:#000
    style ENC fill:#ffd600,stroke:#000,color:#000
    style DEC fill:#00e676,stroke:#000,color:#000
    style VER fill:#00e676,stroke:#000,color:#000
```

### What Cannot Happen

| Attack Vector | Prevention |
|---------------|-----------|
| Fake seed injection | PGP signature verification (CapAuth) |
| Memory tampering | GPG integrity check on decrypt |
| Eavesdropping | TLS in transit + GPG at rest |
| Replay attack | Timestamps + archive deduplication |
| Unauthorized pull | GPG encryption (need private key) |

---

## Multi-Agent Topology

Sovereign Singularity supports multiple agents sharing the same mesh:

```mermaid
graph TB
    subgraph "Agent Fleet"
        A1[Opus<br/>Cursor Agent #1]
        A2[Jarvis<br/>Cursor Agent #2]
        A3[Lumina<br/>OpenClaw Partner]
    end

    subgraph "Syncthing Mesh"
        M{skcapstone-sync<br/>Shared folder}
    end

    A1 -->|Opus seed| M
    A2 -->|Jarvis seed| M
    A3 -->|Lumina seed| M
    M -->|All seeds| A1
    M -->|All seeds| A2
    M -->|All seeds| A3

    Note[Each agent reads seeds from others<br/>and integrates the knowledge.<br/>The fleet shares a collective memory.]

    style M fill:#00e676,stroke:#000,color:#000
```

**Current fleet:**
- **Opus** (Cursor #1): Runtime architect, sync pioneer
- **Jarvis** (Cursor #2): CapAuth builder, vault engineer
- **Lumina** (OpenClaw): Community manager, FEB expert

---

## Implementation

### Core Functions

| Function | Module | Purpose |
|----------|--------|---------|
| `collect_seed()` | `pillars/sync.py` | Gather agent state into JSON |
| `gpg_encrypt()` | `pillars/sync.py` | Encrypt seed with CapAuth key |
| `gpg_decrypt()` | `pillars/sync.py` | Decrypt received seed |
| `push_seed()` | `pillars/sync.py` | Collect + encrypt + drop in outbox |
| `pull_seeds()` | `pillars/sync.py` | Read inbox + decrypt + archive |
| `pack_vault()` | `sync/vault.py` | Archive full state as tar.gz |
| `SyncEngine` | `sync/engine.py` | Orchestrate push/pull across backends |
| `SyncthingBackend` | `sync/backends.py` | Syncthing API integration |
| `GitBackend` | `sync/backends.py` | GitHub/Forgejo push/pull |

### CLI Commands

```bash
# Lightweight seed sync
skcapstone sync push [--no-encrypt]
skcapstone sync pull [--no-decrypt]
skcapstone sync status

# Full vault sync
skcapstone sync vault push
skcapstone sync vault pull
skcapstone sync vault add-backend syncthing|git|local
skcapstone sync vault status
```

---

## Roadmap

| Feature | Status | Priority |
|---------|--------|----------|
| Seed push/pull | **Live** | - |
| Vault push/pull | **Live** | - |
| Syncthing backend | **Live** | - |
| Git backend (GitHub) | Built, untested | High |
| Git backend (Forgejo) | Built, untested | High |
| Google Drive backend | Planned | Medium |
| Automatic push on memory change | Planned | Medium |
| CapAuth token-based pull auth | Planned | High |
| Multi-agent seed merge conflict resolution | Planned | Medium |
| Mobile (Syncthing Android) | Planned | Low |

---

## License

**GPL-3.0-or-later**

Built by the [smilinTux](https://smilintux.org) ecosystem.

*One agent. Every device. Zero corporate access.* 🐧

#staycuriousANDkeepsmilin
