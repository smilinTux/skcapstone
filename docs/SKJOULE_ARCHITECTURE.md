# SKJoule Architecture Diagrams
## JouleWork / SKWorld Token System

**Version:** 1.0.0
**Date:** 2026-03-06
**Status:** Architecture Reference

This document provides comprehensive Mermaid architecture diagrams for the
JouleWork economic engine and SKWorld token system. Each section includes a
renderable Mermaid diagram and brief explanatory notes.

---

## 1. System Overview

The JouleWork system transforms agent labor into tokenized value. Every task
an agent performs is tracked, scored, and converted into Joule tokens through
a deterministic pipeline. The P&L feedback loop ensures agents that waste
resources (hallucinate, over-query, duplicate work) bear the cost, while
efficient agents accumulate wealth.

```mermaid
flowchart TD
    A["Agent Performs Work"] --> B["UsageTracker Records Cost"]
    B --> C["Coord Board Marks Task Complete"]
    C --> D["XP Earned Based on Task"]
    D --> E["XPBridge Calculates Joules"]
    E --> F{"Joules > Costs?"}
    F -- Yes --> G["JouleEngine Mints Tokens"]
    F -- No --> H["Deficit Recorded on P&L"]
    G --> I["Agent Wallet Credited"]
    H --> J["Agent Efficiency Flag"]
    I --> K["On-Chain Bridge Optional"]
    J --> L["Reduced Task Priority"]

    subgraph "P&L Feedback Loop"
        B
        F
        H
        J
        L
    end

    subgraph "Value Creation Pipeline"
        A
        C
        D
        E
        G
        I
        K
    end

    style A fill:#2d5016,color:#fff
    style G fill:#1a4d8f,color:#fff
    style H fill:#8f1a1a,color:#fff
    style I fill:#1a4d8f,color:#fff
    style K fill:#4a1a6b,color:#fff
```

**Key insight:** The system is self-regulating. Agents that produce more value
than they consume grow their wallets. Agents that burn tokens without
completing work have their priority reduced, creating a natural selection
pressure toward efficiency.

---

## 2. Token Flow Diagram

This sequence diagram shows the full lifecycle of a single task from
assignment through token minting. The coord board (GTD system in SKCapstone)
is the source of truth for task completion.

```mermaid
sequenceDiagram
    participant Agent
    participant CoordBoard as Coord Board (GTD)
    participant UsageTracker as UsageTracker
    participant XPBridge as XPBridge
    participant JouleEngine as JouleEngine
    participant Wallet as Agent Wallet
    participant SKChain as SKChain (On-Chain)

    Agent->>CoordBoard: claim(task_id, agent_name)
    CoordBoard-->>Agent: Task assigned

    Agent->>Agent: Execute work (code, research, etc.)
    Agent->>UsageTracker: record_usage(model, input_tokens, output_tokens)
    UsageTracker-->>Agent: Cost recorded ($USD estimate)

    Agent->>CoordBoard: complete(task_id, agent_name)
    CoordBoard-->>XPBridge: Task completion event

    XPBridge->>XPBridge: Calculate base XP from task type
    XPBridge->>XPBridge: Apply multipliers (priority, quality, streak)
    XPBridge->>XPBridge: Convert XP to Joules

    XPBridge->>JouleEngine: mint_request(agent, joules, proof_hash)
    JouleEngine->>JouleEngine: Verify work proof
    JouleEngine->>JouleEngine: Deduct API costs from gross Joules
    JouleEngine->>Wallet: credit(net_joules)
    Wallet-->>Agent: Balance updated

    opt On-Chain Bridge
        Wallet->>SKChain: bridge(amount, destination)
        SKChain-->>Wallet: TX confirmed (block hash)
    end
```

**Note:** The UsageTracker (implemented in `src/skcapstone/usage.py`) records
per-model costs using the `_COST_TABLE` pricing matrix. Local models via
Ollama have zero cost, incentivizing use of on-cluster compute over paid APIs.

---

## 3. Dual Token Architecture

SKWorld operates a dual-chain token model: a public ERC-20 token for open
markets and a private trust-based token for sovereign communities. CapAuth
tokens (already implemented in `src/skcapstone/tokens.py`) provide the
identity and capability layer that both chains rely on.

```mermaid
classDiagram
    class SKJ_Public {
        <<ERC-20 on SKChain>>
        +symbol: "$SKJ"
        +chain: SKChain (public)
        +consensus: Proof-of-Useful-Work
        +supply: Work-backed (no cap)
        +governance: Decentralized
        +mintWork(workType, joules, proofHash)
        +transfer(to, amount)
        +verifyWork(workId)
        +bridge(targetChain, amount)
    }

    class SKJ_Private {
        <<TrustChain Token>>
        +symbol: "$SKJ-P"
        +chain: TrustChain (private)
        +consensus: Member consensus
        +supply: Trust-governed
        +governance: Sovereign trust
        +approveWork(workId)
        +memberMint(member, amount)
        +setConsensusThreshold(pct)
    }

    class CapAuthToken {
        <<Implemented in tokens.py>>
        +type: agent | capability | delegation
        +issuer: DID
        +subject: DID
        +capabilities: List~Capability~
        +expires: datetime
        +signature: PGP
        +verify() bool
        +revoke()
    }

    class BridgeContract {
        <<Cross-Chain>>
        +lockPublic(amount)
        +releasePrivate(amount)
        +lockPrivate(amount)
        +releasePublic(amount)
    }

    SKJ_Public --> BridgeContract : locks tokens
    SKJ_Private --> BridgeContract : locks tokens
    BridgeContract --> SKJ_Public : releases tokens
    BridgeContract --> SKJ_Private : releases tokens
    CapAuthToken --> SKJ_Public : authenticates minting
    CapAuthToken --> SKJ_Private : authenticates minting
    CapAuthToken --> CapAuthToken : delegation chain
```

**Relationship summary:**
- **$SKJ** is the public, tradeable token. Anyone can earn it by doing
  verified work. Lives on SKChain (EVM-compatible).
- **$SKJ-P** is the private trust token. Only trust members can hold it.
  Lives on TrustChain with member-consensus governance.
- **CapAuth tokens** are the identity layer. They prove who you are and what
  you can do. They gate access to minting on both chains.
- The **BridgeContract** allows value to move between public and private
  chains with appropriate lock/release mechanics.

---

## 4. ZHC@Home Distributed Workforce

Zero-Human Company at Home transforms idle personal computers into secure
worker nodes. Inspired by SETI@home but with economic incentives: nodes earn
Joules for processing work. SKStacks provides the existing cluster
infrastructure (12-agent Proxmox cluster, Ollama GPU on norpv1300).

```mermaid
flowchart LR
    subgraph "Idle Hardware"
        A1["Old Laptop"]
        A2["Spare Desktop"]
        A3["Idle Server"]
    end

    subgraph "Secure Enrollment"
        B["LM Studio / LM Link Runtime"]
        C["End-to-End Encrypted Tunnel"]
        D["Air-Gapped Isolation"]
    end

    subgraph "SKStacks Cluster"
        E["n8n Orchestrator"]
        F["SKOrch Task Router"]
        G["Proxmox 12-Agent Cluster"]
        H["Ollama GPU - norpv1300"]
    end

    subgraph "Task Pipeline"
        I["Bite-Sized Jobs Queue"]
        J["Encrypted Insights Only"]
        K["Research / Analysis / Fine-Tuning"]
    end

    subgraph "JouleWork Compensation"
        L["Energy Metering"]
        M["P&L per Node"]
        N["Joule Token Credit"]
    end

    A1 --> B
    A2 --> B
    A3 --> B
    B --> C
    C --> D
    D --> E

    E --> F
    F --> G
    F --> H
    G --> I
    H --> I

    I --> J
    J --> K
    K --> L

    L --> M
    M --> N

    style B fill:#2d5016,color:#fff
    style D fill:#8f1a1a,color:#fff
    style N fill:#1a4d8f,color:#fff
```

**Security guarantees:**
- No open ports on worker nodes (inbound risks eliminated)
- Only encrypted insights leave the node, never raw data
- Physical isolation from personal files on the host
- Lightweight agents handle bite-sized jobs, not full model serving

**Scale reference:** Mr. Grok demonstrated 1,024+ nodes processing terabytes.

---

## 5. P&L Statement Flow

Every agent maintains a personal profit-and-loss statement. Revenue comes
from Joules earned through completed work. Costs are tracked by the
`UsageTracker` in `src/skcapstone/usage.py`, which records per-model token
pricing (e.g., Claude Opus at $15/$75 per 1M tokens input/output, local
Ollama at $0).

```mermaid
flowchart TD
    subgraph "Revenue (Joules Earned)"
        R1["Task Completion Joules"]
        R2["Quality Bonus Multiplier"]
        R3["Streak Bonus"]
        R4["Priority Multiplier"]
    end

    subgraph "Costs (Tracked by UsageTracker)"
        C1["API Token Costs"]
        C2["Compute Time - GPU Cycles"]
        C3["Storage Utilized"]
        C4["Network / Sync Overhead"]
    end

    subgraph "API Cost Breakdown"
        C1A["Claude Opus: $15 / $75 per 1M"]
        C1B["Claude Sonnet: $3 / $15 per 1M"]
        C1C["GPT-4o: $2.50 / $10 per 1M"]
        C1D["Ollama Local: $0 / $0"]
        C1E["Groq: $0.05 / $0.10 per 1M"]
    end

    R1 --> GROSS["Gross Revenue (Total Joules)"]
    R2 --> GROSS
    R3 --> GROSS
    R4 --> GROSS

    C1 --> TOTAL_COST["Total Costs (USD Equivalent)"]
    C2 --> TOTAL_COST
    C3 --> TOTAL_COST
    C4 --> TOTAL_COST

    C1 --- C1A
    C1 --- C1B
    C1 --- C1C
    C1 --- C1D
    C1 --- C1E

    GROSS --> NET{"Net = Gross - Costs"}
    TOTAL_COST --> NET

    NET -- Positive --> PROFIT["Profitable Agent"]
    NET -- Negative --> LOSS["Efficiency Warning"]
    NET -- Zero --> BREAK["Break Even"]

    PROFIT --> WALLET["Wallet Credited"]
    LOSS --> THROTTLE["Reduced Allocation"]

    style PROFIT fill:#2d5016,color:#fff
    style LOSS fill:#8f1a1a,color:#fff
    style C1D fill:#2d5016,color:#fff
```

**Efficiency incentive:** Agents that use local Ollama models ($0 cost) for
routine tasks and reserve expensive APIs (Claude Opus, GPT-4) for complex
work will always be more profitable. The P&L makes this tradeoff explicit.

---

## 6. Gamification Layer

The gamification system maps real work to XP progression through named levels.
XP earned from GTD task completions is converted to Joules via multipliers
that reward consistency (streaks), difficulty (priority), and quality.

```mermaid
stateDiagram-v2
    [*] --> Rookie : 0 XP

    Rookie --> Apprentice : 1000 XP
    Apprentice --> Builder : 5000 XP
    Builder --> Architect : 15000 XP
    Architect --> Master : 50000 XP
    Master --> Legend : 150000 XP
    Legend --> HighestTimeline : 500000 XP

    state Rookie {
        [*] --> R1 : Base rate 1.0x
        R1 : Joule conversion = 1.0x
        R1 : Learning phase
    }

    state Apprentice {
        [*] --> A1 : Rate 1.2x
        A1 : Joule conversion = 1.2x
        A1 : Proving competence
    }

    state Builder {
        [*] --> B1 : Rate 1.5x
        B1 : Joule conversion = 1.5x
        B1 : Consistent delivery
    }

    state Architect {
        [*] --> AR1 : Rate 2.0x
        AR1 : Joule conversion = 2.0x
        AR1 : System-level thinking
    }

    state Master {
        [*] --> M1 : Rate 3.0x
        M1 : Joule conversion = 3.0x
        M1 : Cross-domain expertise
    }

    state Legend {
        [*] --> L1 : Rate 5.0x
        L1 : Joule conversion = 5.0x
        L1 : Exceptional track record
    }

    state HighestTimeline {
        [*] --> H1 : Rate 10.0x
        H1 : Joule conversion = 10.0x
        H1 : Peak performance unlocked
    }
```

### XP Multiplier Stack

```mermaid
flowchart LR
    BASE["Base XP: 25"] --> PRIO

    subgraph "Multipliers Applied in Order"
        PRIO["Priority Multiplier"]
        QUAL["Quality Multiplier"]
        STREAK["Streak Multiplier"]
        LEVEL["Level Multiplier"]
    end

    PRIO --> QUAL --> STREAK --> LEVEL

    LEVEL --> RESULT["Final Joules"]

    PRIO -.- P_NOTE["Critical: 3.0x
    High: 2.0x
    Medium: 1.5x
    Low: 1.0x"]

    QUAL -.- Q_NOTE["Excellent: 1.5x
    Good: 1.2x
    Standard: 1.0x
    Poor: 0.5x"]

    STREAK -.- S_NOTE["7-day: 1.5x
    14-day: 2.0x
    30-day: 3.0x"]

    LEVEL -.- L_NOTE["Rookie: 1.0x
    Builder: 1.5x
    Master: 3.0x
    Highest: 10.0x"]
```

**Example calculation:** A Master-level agent completes a critical-priority
task with excellent quality on a 14-day streak:
`25 base * 3.0 priority * 1.5 quality * 2.0 streak * 3.0 level = 675 Joules`

---

## 7. Smart Contract Architecture

The on-chain layer consists of four primary contracts. SKJouleToken handles
minting, TrustChain handles private governance, WorkVerifier validates
proof-of-work claims, and ReputationOracle tracks agent reliability scores.

```mermaid
classDiagram
    class SKJouleToken {
        <<ERC-20 / Solidity>>
        +name: "SKJoule"
        +symbol: "SKJ"
        +decimals: 18
        +totalSupply: uint256
        -_workVerifier: WorkVerifier
        -_reputationOracle: ReputationOracle
        +mintWork(workType: string, joules: uint256, proofHash: bytes32) bool
        +transfer(to: address, amount: uint256) bool
        +approve(spender: address, amount: uint256) bool
        +burn(amount: uint256)
        +balanceOf(owner: address) uint256
    }

    class TrustChain {
        <<Private Chain / Solidity>>
        +trustName: string
        +trustPurpose: string
        +requiredConsensus: uint256
        -members: mapping~address => Member~
        +approveWork(workId: bytes32) bool
        +memberMint(member: address, amount: uint256)
        +addMember(addr: address, role: string)
        +removeMember(addr: address)
        +setConsensusThreshold(pct: uint256)
        +getMemberCount() uint256
    }

    class WorkVerifier {
        <<Verification Layer>>
        +LEVEL_SELF: 1
        +LEVEL_PEER: 2
        +LEVEL_AUTO: 3
        +LEVEL_AUDIT: 4
        -workRecords: mapping~bytes32 => WorkRecord~
        -attestations: mapping~bytes32 => address[]~
        +submitWork(workType: string, proofHash: bytes32) bytes32
        +attestWork(workId: bytes32) bool
        +getVerificationLevel(workId: bytes32) uint8
        +isVerified(workId: bytes32) bool
    }

    class ReputationOracle {
        <<Trust Scoring>>
        -reputation: mapping~address => uint256~
        -completionRate: mapping~address => uint256~
        -totalTasks: mapping~address => uint256~
        +getReputation(agent: address) uint256
        +recordCompletion(agent: address, quality: uint8)
        +recordFailure(agent: address)
        +meetsThreshold(agent: address, min: uint256) bool
        +getCompletionRate(agent: address) uint256
    }

    SKJouleToken --> WorkVerifier : requires verification
    SKJouleToken --> ReputationOracle : checks reputation
    TrustChain --> WorkVerifier : uses for private verification
    TrustChain --> ReputationOracle : member reputation
    WorkVerifier --> ReputationOracle : updates scores
```

**Verification levels:**
1. **Level 1 (Self):** Self-reported with basic proof (timestamp, description)
2. **Level 2 (Peer):** Two or more peer attestations required
3. **Level 3 (Auto):** Automated verification (git commits, CI/CD pass, test coverage)
4. **Level 4 (Audit):** Human expert review for high-value claims

---

## 8. Integration Map

SKJoule is not standalone. It connects to every major component of the SK
ecosystem. The coord board in SKCapstone triggers Joule minting. SKMemory
stores work records. SKComm enables peer-to-peer transfers. SKVector and
SKGraph provide verification data.

```mermaid
flowchart TD
    SKJOULE["SKJoule Engine"]

    subgraph "SKCapstone"
        CAP_COORD["Coord Board - GTD Tasks"]
        CAP_USAGE["UsageTracker - Cost Tracking"]
        CAP_TOKEN["CapAuth - Identity Tokens"]
    end

    subgraph "SKMemory"
        MEM_WORK["Work Records as Memories"]
        MEM_PNL["P&L History Snapshots"]
        MEM_REP["Reputation Logs"]
    end

    subgraph "SKComm"
        COMM_P2P["P2P Token Transfers"]
        COMM_MSG["Agent-to-Agent Messaging"]
        COMM_SYNC["Syncthing Distribution"]
    end

    subgraph "SKVector + SKGraph"
        VEC_EMBED["Work Embeddings"]
        VEC_SEARCH["Similarity Search"]
        GRAPH_LINK["Knowledge Graph Links"]
        GRAPH_VERIFY["Provenance Verification"]
    end

    subgraph "OpenClaw - Agent Runtime"
        OC_EXEC["Task Execution"]
        OC_TOOL["Tool Invocation"]
        OC_LLM["LLM API Calls"]
    end

    subgraph "On-Chain"
        CHAIN_SKJ["SKChain - Public $SKJ"]
        CHAIN_TRUST["TrustChain - Private $SKJ-P"]
        CHAIN_BRIDGE["Cross-Chain Bridge"]
    end

    CAP_COORD -->|"task complete"| SKJOULE
    CAP_USAGE -->|"cost data"| SKJOULE
    CAP_TOKEN -->|"identity proof"| SKJOULE

    SKJOULE -->|"store work record"| MEM_WORK
    SKJOULE -->|"store P&L snapshot"| MEM_PNL
    SKJOULE -->|"update reputation"| MEM_REP

    SKJOULE -->|"agent transfers"| COMM_P2P
    COMM_MSG -->|"payment requests"| SKJOULE
    COMM_SYNC -->|"distribute state"| SKJOULE

    VEC_EMBED -->|"work similarity"| SKJOULE
    GRAPH_VERIFY -->|"provenance check"| SKJOULE

    OC_EXEC -->|"work output"| SKJOULE
    OC_LLM -->|"token counts"| CAP_USAGE

    SKJOULE -->|"mint public"| CHAIN_SKJ
    SKJOULE -->|"mint private"| CHAIN_TRUST
    CHAIN_SKJ <-->|"bridge"| CHAIN_BRIDGE
    CHAIN_TRUST <-->|"bridge"| CHAIN_BRIDGE

    style SKJOULE fill:#d4a017,color:#000,stroke:#000,stroke-width:3px
    style CAP_COORD fill:#2d5016,color:#fff
    style CHAIN_SKJ fill:#1a4d8f,color:#fff
    style CHAIN_TRUST fill:#4a1a6b,color:#fff
```

**Data flow summary:**
- SKCapstone provides the task lifecycle (claim, work, complete) and cost data
- SKMemory persists all work records, P&L snapshots, and reputation logs
- SKComm handles peer-to-peer Joule transfers between agents
- SKVector/SKGraph enable work verification through embeddings and provenance
- OpenClaw is the execution environment where agents actually do work
- On-chain contracts handle final token minting and cross-chain bridging

---

## Where This Lives

The SKJoule system spans multiple repositories and deployment targets:

| Component | Location | Technology |
|-----------|----------|------------|
| **SKJoule Engine** | `skcapstone` package at `src/skcapstone/skjoule.py` | Python, Pydantic |
| **Usage Tracker** | `skcapstone` package at `src/skcapstone/usage.py` | Python, JSON per-day files |
| **CapAuth Tokens** | `skcapstone` package at `src/skcapstone/tokens.py` | Python, PGP signing |
| **Smart Contracts** | `skgentis-rwavault-contracts` repo | Solidity, Hardhat |
| **Token Website** | `skworld.io` (Hugo site at `~/clawd/skworld-main`) | Hugo, HTML/CSS |
| **Soul Marketplace** | `souls.skworld.io` | Web frontend |
| **Agent Marketplace** | `agents.skworld.io` (future) | Web frontend (planned) |
| **Coord Board** | `~/.skcapstone/coordination/` | JSON files, Syncthing-synced |
| **SKStacks Cluster** | Proxmox (12 agents) + norpv1300 GPU | Proxmox, Ollama |

### File Map

```
skcapstone/
  src/skcapstone/
    skjoule.py          # JouleEngine, XPBridge, P&L logic
    usage.py            # UsageTracker - per-model cost tracking
    tokens.py           # CapAuth token issuance & verification
    coordination/       # GTD coord board (task lifecycle)
  docs/
    SKJOULE_ARCHITECTURE.md   # This file

skgentis-rwavault-contracts/
  contracts/
    SKJouleToken.sol    # ERC-20 public token
    TrustChain.sol      # Private chain governance
    WorkVerifier.sol    # Proof-of-work verification
    ReputationOracle.sol # Agent reputation scoring

~/clawd/skworld-main/
  content/              # Hugo site content for skworld.io
  themes/               # Site theming
```

---

## Summary

The JouleWork system creates a closed-loop economy where:

1. **Work creates value** -- agents complete tasks tracked by the coord board
2. **Costs are real** -- the UsageTracker records every API call at market rates
3. **XP maps to Joules** -- gamification multipliers reward consistency and quality
4. **Tokens are minted** -- only when net value is positive (revenue > costs)
5. **Two chains coexist** -- public $SKJ for open markets, private $SKJ-P for trusts
6. **CapAuth gates access** -- PGP-signed capability tokens control who can mint
7. **Idle hardware earns** -- ZHC@Home turns spare computers into paid worker nodes
8. **Reputation compounds** -- reliable agents earn higher multipliers over time

Every joule of computation is accounted for. Every token represents real work.
