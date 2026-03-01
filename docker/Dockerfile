# Sovereign Agent Container Image
#
# Base image for skcapstone AI agent containers deployed by DockerProvider.
# Each container runs an AI session (via claude CLI) with the full sovereign
# stack wired in: skcapstone MCP, SKComm transport, soul blueprint.
#
# Build (from repo root):
#   docker build -f skcapstone/docker/Dockerfile -t skcapstone-agent:latest .
#
# Environment variables expected at runtime:
#   AGENT_NAME          — unique agent instance name
#   TEAM_NAME           — parent team name
#   AGENT_ROLE          — role (manager, worker, coder, …)
#   AGENT_MODEL         — model name or tier (fast, code, reason, nuance)
#   SOUL_BLUEPRINT      — path or slug of soul blueprint (optional)
#   SKCAPSTONE_MCP_HOST — host:port of skcapstone MCP server (optional)
#   SKCAPSTONE_MCP_SOCKET — unix socket path for MCP (optional)
#   SKCOMM_HOME         — comms root directory (default: /skcomm)
#
# Volumes:
#   /agent   — persistent agent state (config, memory, session state)
#   /skcomm  — SKComm comms directory (bind-mount from host)

FROM python:3.12-slim

# --- System deps -----------------------------------------------------------
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
        gnupg2 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# --- Node.js (for claude CLI) ----------------------------------------------
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# --- Claude CLI ------------------------------------------------------------
RUN npm install -g @anthropic-ai/claude-code

# --- Python sovereign stack (local source) ----------------------------------
COPY skcapstone /src/skcapstone
COPY skcomm /src/skcomm

RUN pip install --no-cache-dir pydantic pyyaml httpx \
    && pip install --no-cache-dir -e /src/skcapstone \
    && pip install --no-cache-dir -e /src/skcomm

# --- Directories -----------------------------------------------------------
RUN mkdir -p /agent/memory /agent/scratch /skcomm /run/skcapstone

VOLUME ["/agent", "/skcomm"]

WORKDIR /agent

# --- Entrypoint ------------------------------------------------------------
COPY skcapstone/docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
