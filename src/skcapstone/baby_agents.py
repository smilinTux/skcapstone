"""
Baby Agent Definitions — the 12 lightweight daemons of the SK* ecosystem.

Each baby agent is a pre-defined, single-purpose agent that handles a
specific aspect of the sovereign agent framework. They can be spawned
individually via `skcapstone agents spawn <name>` or in batches.

Baby agents are intentionally lightweight — they run as local processes
with minimal resources and use the FAST model tier unless they need
deeper reasoning (e.g., security-auditor uses REASON).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from .blueprints.schema import AgentRole, ModelTier


@dataclass(frozen=True)
class BabyAgentDef:
    """Definition of a baby agent."""

    name: str
    description: str
    role: AgentRole
    model: ModelTier
    skills: List[str]
    task: str  # Default task description used when spawning


# ---------------------------------------------------------------------------
# The 12 baby agents
# ---------------------------------------------------------------------------

BABY_AGENTS: Dict[str, BabyAgentDef] = {
    "memory-curator": BabyAgentDef(
        name="memory-curator",
        description="Curates and consolidates agent memories across tiers (short/mid/long-term). "
                    "Promotes important memories, prunes stale ones, and maintains coherence.",
        role=AgentRole.WORKER,
        model=ModelTier.FAST,
        skills=["memory-read", "memory-write", "memory-consolidate"],
        task="Curate agent memories: promote important short-term memories, "
             "consolidate mid-term, prune stale long-term entries.",
    ),
    "trust-guardian": BabyAgentDef(
        name="trust-guardian",
        description="Monitors and enforces the trust chain. Validates DIDs, checks peer "
                    "attestations, and alerts on trust violations or expired credentials.",
        role=AgentRole.SECURITY,
        model=ModelTier.REASON,
        skills=["did-verify", "trust-chain", "attestation-check"],
        task="Guard the trust chain: validate peer DIDs, check attestations, "
             "alert on trust violations or expired credentials.",
    ),
    "sync-watcher": BabyAgentDef(
        name="sync-watcher",
        description="Watches Syncthing sync state across nodes. Detects conflicts, "
                    "stale sync folders, and connectivity issues between peers.",
        role=AgentRole.OPS,
        model=ModelTier.FAST,
        skills=["syncthing-api", "conflict-detect", "sync-health"],
        task="Monitor Syncthing sync state: detect conflicts, stale folders, "
             "and connectivity issues between peers.",
    ),
    "security-auditor": BabyAgentDef(
        name="security-auditor",
        description="Performs continuous security audits on agent configs, keys, permissions, "
                    "and network exposure. Reports vulnerabilities and suggests hardening.",
        role=AgentRole.SECURITY,
        model=ModelTier.REASON,
        skills=["security-scan", "key-audit", "permission-check"],
        task="Audit agent security: scan configs for weak permissions, expired keys, "
             "exposed ports, and suggest hardening measures.",
    ),
    "seed-validator": BabyAgentDef(
        name="seed-validator",
        description="Validates skseed packages and their cryptographic signatures. Ensures "
                    "seed integrity before deployment and tracks seed lineage.",
        role=AgentRole.SECURITY,
        model=ModelTier.FAST,
        skills=["seed-verify", "signature-check", "lineage-track"],
        task="Validate skseed packages: verify cryptographic signatures, check "
             "integrity hashes, and track seed lineage chains.",
    ),
    "graph-builder": BabyAgentDef(
        name="graph-builder",
        description="Builds and maintains the knowledge graph from agent memories, "
                    "relationships, and discovered entities. Powers contextual recall.",
        role=AgentRole.WORKER,
        model=ModelTier.CODE,
        skills=["graph-write", "entity-extract", "relation-map"],
        task="Build knowledge graph: extract entities from memories, map relationships, "
             "and maintain the graph for contextual recall.",
    ),
    "vector-indexer": BabyAgentDef(
        name="vector-indexer",
        description="Indexes agent memories and documents into vector storage for semantic "
                    "search. Manages embeddings, re-indexes on changes, and optimizes recall.",
        role=AgentRole.WORKER,
        model=ModelTier.FAST,
        skills=["vector-index", "embedding-gen", "search-optimize"],
        task="Index memories into vector storage: generate embeddings, maintain "
             "indices, and optimize for semantic recall.",
    ),
    "health-monitor": BabyAgentDef(
        name="health-monitor",
        description="Monitors agent health via heartbeats, resource usage, and liveness checks. "
                    "Triggers self-healing or alerts when agents degrade.",
        role=AgentRole.OPS,
        model=ModelTier.FAST,
        skills=["heartbeat-check", "resource-monitor", "self-heal-trigger"],
        task="Monitor agent health: check heartbeats, track resource usage, "
             "trigger self-healing for degraded agents.",
    ),
    "telegram-poller": BabyAgentDef(
        name="telegram-poller",
        description="Polls Telegram for incoming messages and commands. Routes messages to "
                    "the appropriate agent and sends responses back to users.",
        role=AgentRole.OPS,
        model=ModelTier.FAST,
        skills=["telegram-api", "message-route", "command-parse"],
        task="Poll Telegram for messages: receive commands, route to agents, "
             "and relay responses back to users.",
    ),
    "mood-tracker": BabyAgentDef(
        name="mood-tracker",
        description="Tracks agent emotional/operational mood based on task outcomes, errors, "
                    "and interactions. Feeds into consciousness loop and self-awareness.",
        role=AgentRole.RESEARCHER,
        model=ModelTier.FAST,
        skills=["mood-assess", "sentiment-track", "consciousness-feed"],
        task="Track agent mood: assess operational sentiment from task outcomes "
             "and errors, feed into consciousness loop.",
    ),
    "peer-discoverer": BabyAgentDef(
        name="peer-discoverer",
        description="Discovers peer agents on the local network via mDNS and the SK registry. "
                    "Maintains the peer directory and initiates trust handshakes.",
        role=AgentRole.OPS,
        model=ModelTier.FAST,
        skills=["mdns-scan", "registry-query", "peer-handshake"],
        task="Discover peer agents: scan via mDNS, query SK registry, maintain "
             "peer directory, and initiate trust handshakes.",
    ),
    "housekeeping-bot": BabyAgentDef(
        name="housekeeping-bot",
        description="Performs routine maintenance: log rotation, temp file cleanup, cache "
                    "pruning, database vacuuming, and disk space management.",
        role=AgentRole.OPS,
        model=ModelTier.FAST,
        skills=["log-rotate", "cache-prune", "disk-manage"],
        task="Run housekeeping: rotate logs, clean temp files, prune caches, "
             "vacuum databases, and manage disk space.",
    ),
}


def get_baby_agent(name: str) -> Optional[BabyAgentDef]:
    """Look up a baby agent definition by name.

    Args:
        name: The baby agent name (e.g., 'memory-curator').

    Returns:
        BabyAgentDef if found, None otherwise.
    """
    return BABY_AGENTS.get(name)


def list_baby_agents() -> List[BabyAgentDef]:
    """Return all baby agent definitions, sorted by name.

    Returns:
        List of BabyAgentDef in alphabetical order.
    """
    return sorted(BABY_AGENTS.values(), key=lambda a: a.name)
