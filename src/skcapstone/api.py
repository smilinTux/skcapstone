"""
SKCapstone REST API — FastAPI application with OpenAPI documentation.

Exposes all daemon /api/v1/* endpoints as a proper REST API with:
- Pydantic response models for automatic schema generation
- API key security scheme (X-API-Key header)
- CapAuth Bearer token security for privileged endpoints
- Swagger UI at /docs
- ReDoc at /redoc
- OpenAPI JSON at /openapi.json

Usage (standalone docs server):
    uvicorn skcapstone.api:app --host 127.0.0.1 --port 7779 --reload

Usage (programmatic, from daemon):
    from skcapstone.api import init_api, app
    init_api(state=state, config=config, consciousness=consciousness)
    # Then run with uvicorn in a background thread.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import subprocess
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional

logger = logging.getLogger("skcapstone.api")

# ── FastAPI import guard ──────────────────────────────────────────────────────

try:
    from fastapi import (
        Depends,
        FastAPI,
        HTTPException,
        Path as FPath,
        Query,
        Request,
        Security,
        WebSocket,
        WebSocketDisconnect,
        status,
    )
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse, StreamingResponse
    from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
    from pydantic import BaseModel, Field
except ImportError as _exc:
    raise ImportError(
        "FastAPI is required for the REST API module.  "
        "Install with: pip install skcapstone[api]"
    ) from _exc

# ── Security schemes ─────────────────────────────────────────────────────────

_api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description=(
        "Optional API key for the SKCapstone REST API.  "
        "Set the SKCAPSTONE_API_KEY environment variable to enforce key validation.  "
        "When the env var is absent, the daemon operates in unauthenticated local mode."
    ),
)

_bearer_scheme = HTTPBearer(
    auto_error=False,
    description=(
        "CapAuth bearer token required for privileged streaming endpoints "
        "(e.g. GET /api/v1/logs WebSocket).  Tokens are issued by the CapAuth "
        "identity system and verified against the agent's PGP key."
    ),
)


# ── Module-level daemon context ───────────────────────────────────────────────

_ctx: Dict[str, Any] = {}  # Populated by init_api()


def init_api(
    state: Any,
    config: Any,
    consciousness: Any = None,
    runtime: Any = None,
) -> None:
    """Bind daemon runtime objects to the FastAPI application context.

    Call this once before starting the FastAPI server so that request
    handlers can access daemon state, configuration, and the consciousness
    loop without global imports.

    Args:
        state: DaemonState instance from daemon.py.
        config: DaemonConfig instance from daemon.py.
        consciousness: Optional ConsciousnessLoop instance (may be None).
        runtime: Optional AgentRuntime instance (may be None).
    """
    _ctx["state"] = state
    _ctx["config"] = config
    _ctx["consciousness"] = consciousness
    _ctx["runtime"] = runtime
    logger.info("FastAPI context initialised — docs at /docs")


def _get_ctx() -> Dict[str, Any]:
    """Return the current daemon context dict."""
    return _ctx


# ── Pydantic response models ──────────────────────────────────────────────────


class HealthResponse(BaseModel):
    """Daemon health and liveness summary."""

    status: str = Field(..., description="'ok' when daemon is running, 'stopped' otherwise.")
    uptime_seconds: float = Field(..., description="Seconds since daemon start.")
    daemon_pid: Optional[int] = Field(None, description="OS process ID of the daemon.")
    consciousness_enabled: bool = Field(
        ..., description="True when the consciousness loop is active."
    )
    self_healing_last_run: Optional[str] = Field(
        None, description="ISO-8601 timestamp of the last self-healing cycle."
    )
    self_healing_issues_found: int = Field(
        0, description="Number of issues found in the last self-healing cycle."
    )
    self_healing_auto_fixed: int = Field(
        0, description="Number of issues automatically fixed in the last cycle."
    )
    backend_health: Dict[str, Any] = Field(
        default_factory=dict,
        description="Per-transport liveness flags (e.g. {skcomm: true}).",
    )
    disk_free_gb: float = Field(0.0, description="Free disk space in gigabytes.")
    memory_usage_mb: float = Field(0.0, description="Current RSS memory usage in MB.")


class ComponentSnapshot(BaseModel):
    """Health record for a single daemon subsystem component."""

    name: str = Field(..., description="Component identifier (e.g. 'poll', 'consciousness').")
    status: str = Field(
        ..., description="One of: pending, alive, dead, restarting, disabled."
    )
    auto_restart: bool = Field(
        ..., description="True when the watchdog will auto-restart this component."
    )
    started_at: Optional[str] = Field(
        None, description="ISO-8601 timestamp when the component last started."
    )
    last_heartbeat: Optional[str] = Field(
        None, description="ISO-8601 timestamp of the last heartbeat pulse."
    )
    heartbeat_age_seconds: Optional[int] = Field(
        None, description="Seconds since the last heartbeat."
    )
    restart_count: int = Field(0, description="Number of automatic restarts.")
    last_error: Optional[str] = Field(
        None, description="Last recorded error message, if any."
    )


class ComponentsResponse(BaseModel):
    """Snapshot of all daemon subsystem component health records."""

    components: List[ComponentSnapshot] = Field(
        ..., description="List of component health snapshots."
    )


class AgentIdentitySummary(BaseModel):
    """Minimal agent identity fields surfaced in the dashboard."""

    name: str = Field("", description="Agent display name.")
    fingerprint: str = Field("", description="PGP key fingerprint (hex).")
    status: str = Field("", description="Identity pillar status.")


class MemorySummary(BaseModel):
    """Memory layer statistics."""

    total: int = Field(0, description="Total memory entries across all layers.")
    short_term: int = Field(0, description="Entries in short-term memory.")
    mid_term: int = Field(0, description="Entries in mid-term memory.")
    long_term: int = Field(0, description="Entries in long-term memory.")
    status: str = Field("", description="Memory pillar status string.")


class DaemonSummary(BaseModel):
    """Daemon process runtime metrics."""

    running: bool = Field(True, description="True when the daemon process is alive.")
    pid: Optional[int] = Field(None, description="Daemon OS process ID.")
    uptime_seconds: float = Field(0.0, description="Seconds since daemon start.")
    messages_received: int = Field(0, description="Total messages received since start.")
    syncs_completed: int = Field(0, description="Total vault syncs completed since start.")
    error_count: int = Field(0, description="Count of errors recorded since start.")
    inflight_count: int = Field(0, description="Messages currently being processed.")


class SystemStats(BaseModel):
    """Host system resource metrics."""

    disk_total_gb: float = Field(0.0)
    disk_used_gb: float = Field(0.0)
    disk_free_gb: float = Field(0.0)
    memory_total_mb: int = Field(0)
    memory_used_mb: int = Field(0)
    memory_free_mb: int = Field(0)


class DashboardResponse(BaseModel):
    """Full dashboard snapshot returned by GET /api/v1/dashboard."""

    agent: Dict[str, Any] = Field(
        default_factory=dict, description="Agent identity and pillar summary."
    )
    daemon: DaemonSummary = Field(
        default_factory=DaemonSummary, description="Daemon process metrics."
    )
    consciousness: Dict[str, Any] = Field(
        default_factory=dict, description="Consciousness loop stats (if enabled)."
    )
    backends: Dict[str, Any] = Field(
        default_factory=dict, description="LLM and transport backend availability."
    )
    conversations: List[Dict[str, Any]] = Field(
        default_factory=list, description="Recent conversation summaries."
    )
    system: SystemStats = Field(
        default_factory=SystemStats, description="Host system resource metrics."
    )
    recent_errors: List[str] = Field(
        default_factory=list, description="Recent daemon error messages."
    )


class BoardSummary(BaseModel):
    """Coordination board task counts."""

    total: int = Field(0)
    done: int = Field(0)
    in_progress: int = Field(0)
    claimed: int = Field(0)
    open: int = Field(0)


class ActiveTask(BaseModel):
    """An in-progress or claimed coordination task."""

    id: str = Field(..., description="8-character task ID.")
    title: str = Field(..., description="Task title.")
    priority: str = Field(..., description="Task priority (critical/high/medium/low).")
    status: str = Field(..., description="Task status (claimed/in_progress).")
    claimed_by: Optional[str] = Field(None, description="Agent name that claimed the task.")


class CapstoneResponse(BaseModel):
    """Full capstone snapshot: pillars, memory, board, consciousness."""

    agent: Dict[str, Any] = Field(default_factory=dict, description="Agent identity summary.")
    pillars: Dict[str, str] = Field(
        default_factory=dict,
        description="Pillar name → status string (active/degraded/missing).",
    )
    memory: MemorySummary = Field(
        default_factory=MemorySummary, description="Memory layer statistics."
    )
    board: Dict[str, Any] = Field(
        default_factory=dict, description="Coordination board summary and active tasks."
    )
    consciousness: Dict[str, Any] = Field(
        default_factory=dict, description="Consciousness loop statistics."
    )


class AgentHeartbeat(BaseModel):
    """Live heartbeat data for a household agent."""

    alive: bool = Field(False, description="True when heartbeat is within its TTL.")
    status: str = Field("", description="Agent-reported status string.")
    timestamp: Optional[str] = Field(None, description="ISO-8601 heartbeat timestamp.")
    ttl_seconds: int = Field(300, description="Heartbeat TTL in seconds.")


class HouseholdAgent(BaseModel):
    """Summary of a single agent in the shared household."""

    name: str = Field(..., description="Agent directory name.")
    status: str = Field("unknown", description="Derived liveness status.")
    identity: Optional[Dict[str, Any]] = Field(
        None, description="Agent identity.json contents."
    )
    heartbeat: Optional[Dict[str, Any]] = Field(
        None, description="Most recent heartbeat record."
    )
    consciousness: Optional[Dict[str, Any]] = Field(
        None, description="Consciousness stats from the serving agent (if available)."
    )


class HouseholdAgentsResponse(BaseModel):
    """List of all agents known in the shared household."""

    agents: List[HouseholdAgent] = Field(
        ..., description="All agents found in the shared agents directory."
    )


class ConversationSummary(BaseModel):
    """Brief summary of a conversation thread."""

    peer: str = Field(..., description="Peer agent or user name.")
    message_count: int = Field(0, description="Total messages in the thread.")
    last_message_time: Optional[str] = Field(
        None, description="ISO-8601 timestamp of the most recent message."
    )
    last_message_preview: str = Field(
        "", description="First 120 characters of the last message."
    )


class ConversationsResponse(BaseModel):
    """List of all conversation threads."""

    conversations: List[ConversationSummary] = Field(
        ..., description="All conversation threads, sorted by most recently active."
    )


class MessageEntry(BaseModel):
    """A single message in a conversation thread."""

    sender: Optional[str] = Field(None)
    recipient: Optional[str] = Field(None)
    content: Optional[str] = Field(None)
    timestamp: Optional[str] = Field(None)


class ConversationHistoryResponse(BaseModel):
    """Full message history for a conversation with a specific peer."""

    peer: str = Field(..., description="Peer name this conversation belongs to.")
    messages: List[Dict[str, Any]] = Field(
        ..., description="Full message list (raw envelope dicts)."
    )


class SendMessageRequest(BaseModel):
    """Request body for posting a message to a peer."""

    content: str = Field(..., min_length=1, description="Message text to send.")


class SendMessageResponse(BaseModel):
    """Confirmation after a message is dispatched to a peer."""

    status: str = Field("sent", description="Always 'sent' on success.")
    message_id: str = Field(..., description="UUID of the created message envelope.")


class DeleteConversationResponse(BaseModel):
    """Confirmation after a conversation thread is deleted."""

    status: str = Field("deleted", description="Always 'deleted' on success.")
    peer: str = Field(..., description="Name of the peer whose thread was removed.")


class MetricsResponse(BaseModel):
    """Consciousness loop runtime metrics."""

    loops_completed: int = Field(0, description="Total consciousness loop iterations.")
    messages_processed: int = Field(0, description="Messages processed by the loop.")
    last_loop_at: Optional[str] = Field(
        None, description="ISO-8601 timestamp of the most recent loop execution."
    )
    average_loop_ms: float = Field(0.0, description="Average loop duration in milliseconds.")
    errors: int = Field(0, description="Total errors encountered in the loop.")


class LegacyStatusResponse(BaseModel):
    """Legacy /status endpoint response."""

    running: bool = Field(True)
    pid: Optional[int] = Field(None)
    uptime_seconds: float = Field(0.0)
    messages_received: int = Field(0)
    syncs_completed: int = Field(0)
    started_at: Optional[str] = Field(None)
    recent_errors: List[str] = Field(default_factory=list)
    inflight_count: int = Field(0)


class PingResponse(BaseModel):
    """Response from the liveness ping endpoint."""

    pong: bool = Field(True)
    pid: Optional[int] = Field(None, description="Daemon OS process ID.")


class ArgoCDApp(BaseModel):
    """ArgoCD Application entry parsed from skstacks manifests."""

    name: str = Field(..., description="ArgoCD Application name.")
    project: str = Field("", description="ArgoCD project name.")
    namespace: str = Field("argocd", description="Kubernetes namespace.")
    source_path: str = Field("", description="Git source path in the repo.")
    repo_url: str = Field("", description="Git repository URL.")
    target_revision: str = Field("", description="Target branch or revision.")
    sync_status: str = Field("Unknown", description="Sync status (Synced/OutOfSync/Unknown).")
    health_status: str = Field(
        "Unknown", description="Health status (Healthy/Degraded/Progressing/Unknown)."
    )
    color: str = Field("gray", description="Dashboard color hint (green/yellow/red/gray).")
    last_synced: Optional[str] = Field(
        None, description="ISO-8601 timestamp of last successful sync."
    )
    manifest_file: str = Field("", description="Source YAML filename.")


class ArgoCDSummary(BaseModel):
    """ArgoCD app count summary."""

    total: int = Field(0)
    synced: int = Field(0)
    out_of_sync: int = Field(0)
    unknown: int = Field(0)
    healthy: int = Field(0)
    degraded: int = Field(0)


class ArgoCDStatusResponse(BaseModel):
    """ArgoCD application status list from skstacks/v2 manifests."""

    source: str = Field(
        "yaml", description="Data source: 'yaml' (static) or 'yaml+kubectl' (live)."
    )
    checked_at: str = Field(..., description="ISO-8601 timestamp of this response.")
    skstacks_root: str = Field("", description="Resolved path to skstacks v2 root.")
    apps: List[ArgoCDApp] = Field(
        default_factory=list, description="List of ArgoCD applications."
    )
    summary: ArgoCDSummary = Field(
        default_factory=ArgoCDSummary, description="App count summary."
    )


# ── Security dependency ───────────────────────────────────────────────────────

_PEER_NAME_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-@\.]")


def _sanitize_peer(peer: str) -> str:
    """Sanitize a peer name for safe filesystem use.

    Args:
        peer: Raw peer name from the URL path.

    Returns:
        Safe, filesystem-friendly peer identifier (max 64 chars).
    """
    if not peer or not isinstance(peer, str):
        return ""
    sanitized = peer.replace("\x00", "").replace("/", "").replace("\\", "")
    sanitized = _PEER_NAME_SAFE_RE.sub("", sanitized)
    sanitized = sanitized.strip(".")
    return sanitized[:64]


def _check_api_key(api_key: Optional[str] = Security(_api_key_header)) -> Optional[str]:
    """Validate the optional X-API-Key header.

    When the SKCAPSTONE_API_KEY environment variable is set, the provided
    key must match it exactly.  When the variable is absent the daemon
    operates in unauthenticated local mode and any (or no) key is accepted.

    Args:
        api_key: Value from the X-API-Key request header.

    Returns:
        The validated key string, or None in unauthenticated mode.

    Raises:
        HTTPException: 401 Unauthorized when key validation fails.
    """
    expected = os.environ.get("SKCAPSTONE_API_KEY")
    if expected and api_key != expected:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key.  Pass the key in the X-API-Key header.",
        )
    return api_key


def _check_bearer(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer_scheme),
) -> str:
    """Validate a CapAuth Bearer token for privileged endpoints.

    Attempts CapAuth validation first (if skcomm is installed), then falls
    back to skcapstone signed token verification.

    Args:
        credentials: HTTP Authorization Bearer credentials.

    Returns:
        PGP fingerprint of the authenticated identity.

    Raises:
        HTTPException: 401 Unauthorized when token is missing or invalid.
    """
    token_str: Optional[str] = None
    if credentials and credentials.credentials:
        token_str = credentials.credentials

    fingerprint: Optional[str] = None
    config = _ctx.get("config")

    try:
        from skcomm.capauth_validator import CapAuthValidator

        fingerprint = CapAuthValidator(require_auth=True).validate(token_str)
    except ImportError:
        if token_str and config:
            try:
                from .tokens import import_token, verify_token

                tok = import_token(token_str)
                if verify_token(tok, home=config.home):
                    fingerprint = tok.payload.issuer
            except Exception:
                fingerprint = None

    if fingerprint is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=(
                "CapAuth bearer token required.  "
                "Obtain a token with: skcapstone token issue"
            ),
        )
    return fingerprint


# ── Helper: system stats ──────────────────────────────────────────────────────


def _collect_system_stats() -> SystemStats:
    """Collect disk and memory metrics from the host OS.

    Returns:
        SystemStats populated from /proc/meminfo and shutil.disk_usage.
    """
    import shutil

    data: Dict[str, Any] = {}
    try:
        usage = shutil.disk_usage("/")
        data["disk_total_gb"] = round(usage.total / (1024**3), 1)
        data["disk_used_gb"] = round(usage.used / (1024**3), 1)
        data["disk_free_gb"] = round(usage.free / (1024**3), 1)
    except Exception:
        data.update(disk_total_gb=0.0, disk_used_gb=0.0, disk_free_gb=0.0)
    try:
        meminfo: Dict[str, int] = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) >= 2:
                    meminfo[parts[0].rstrip(":")] = int(parts[1])
        total_kb = meminfo.get("MemTotal", 0)
        avail_kb = meminfo.get("MemAvailable", 0)
        data["memory_total_mb"] = round(total_kb / 1024)
        data["memory_used_mb"] = round((total_kb - avail_kb) / 1024)
        data["memory_free_mb"] = round(avail_kb / 1024)
    except Exception:
        data.update(memory_total_mb=0, memory_used_mb=0, memory_free_mb=0)
    return SystemStats(**data)


def _hb_alive(hb: dict) -> bool:
    """Return True if a heartbeat dict is within its TTL.

    Args:
        hb: Heartbeat dict with 'timestamp' and optional 'ttl_seconds'.

    Returns:
        True when the heartbeat is fresh, False when expired or unparseable.
    """
    ts_str = hb.get("timestamp", "")
    ttl = hb.get("ttl_seconds", 300)
    if not ts_str:
        return False
    try:
        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) <= ts + timedelta(seconds=ttl)
    except Exception:
        return False


# ── FastAPI application ───────────────────────────────────────────────────────

app = FastAPI(
    title="SKCapstone Agent API",
    description=(
        "Sovereign agent REST API for the SKCapstone framework.\n\n"
        "Exposes daemon health, memory, coordination board, consciousness, "
        "household agent directory, and conversation management endpoints.\n\n"
        "## Authentication\n\n"
        "Most endpoints are unauthenticated in local daemon mode.  Set the "
        "`SKCAPSTONE_API_KEY` environment variable to enable API key enforcement.  "
        "Privileged streaming endpoints (e.g. `GET /api/v1/logs`) require a "
        "CapAuth Bearer token issued by `skcapstone token issue`.\n\n"
        "## Security Schemes\n\n"
        "- **ApiKeyAuth** — `X-API-Key` request header, validated when "
        "`SKCAPSTONE_API_KEY` env var is set.\n"
        "- **BearerAuth** — `Authorization: Bearer <capauth-token>` for privileged "
        "streaming endpoints."
    ),
    version="0.9.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    contact={
        "name": "smilinTux.org",
        "url": "https://smilintux.org",
        "email": "hello@smilintux.org",
    },
    license_info={
        "name": "GPL-3.0-or-later",
        "url": "https://www.gnu.org/licenses/gpl-3.0.html",
    },
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)


# ── Custom OpenAPI schema: inject BearerAuth security scheme ─────────────────

def _custom_openapi() -> Dict[str, Any]:
    """Return a customised OpenAPI schema with both security schemes registered.

    FastAPI auto-registers ``APIKeyHeader`` from the ``X-API-Key`` dependency,
    but the ``BearerAuth`` scheme (used by the ``/api/v1/logs`` WebSocket
    endpoint) must be injected manually because WebSocket routes are not
    included in the OpenAPI 3.0 spec.

    Registered security schemes:
    - **APIKeyHeader** — ``apiKey`` in header ``X-API-Key`` (optional, see SKCAPSTONE_API_KEY)
    - **BearerAuth** — HTTP Bearer token issued by CapAuth (required for /api/v1/logs WS)
    """
    if app.openapi_schema:
        return app.openapi_schema

    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
        contact=app.contact,
        license_info=app.license_info,
    )

    # Inject BearerAuth (HTTP Bearer) security scheme
    schema.setdefault("components", {}).setdefault("securitySchemes", {})
    schema["components"]["securitySchemes"]["BearerAuth"] = {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "CapAuth",
        "description": (
            "CapAuth bearer token required for privileged streaming endpoints "
            "(GET /api/v1/logs WebSocket).  Issue a token with: "
            "``skcapstone token issue``"
        ),
    }

    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]

# ── /api/v1/health ────────────────────────────────────────────────────────────


@app.get(
    "/api/v1/health",
    response_model=HealthResponse,
    summary="Daemon health check",
    tags=["Health"],
    responses={
        200: {"description": "Daemon is running and healthy."},
        503: {"description": "Daemon is stopped or unreachable."},
    },
)
async def get_health(
    _key: Optional[str] = Depends(_check_api_key),
) -> HealthResponse:
    """Return a comprehensive health snapshot of the running daemon.

    Includes uptime, consciousness status, self-healing metrics, backend
    transport availability, and host system resource usage.  Returns HTTP 503
    when the daemon context has not been initialised (daemon not running).
    """
    state = _ctx.get("state")
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    snap = state.snapshot()
    healing = snap.get("self_healing", {})
    sys_stats = _collect_system_stats()
    consciousness = _ctx.get("consciousness")
    c_enabled = bool(consciousness and consciousness.stats.get("enabled", False))
    return HealthResponse(
        status="ok" if snap.get("running", True) else "stopped",
        uptime_seconds=snap.get("uptime_seconds", 0.0),
        daemon_pid=snap.get("pid"),
        consciousness_enabled=c_enabled,
        self_healing_last_run=healing.get("timestamp"),
        self_healing_issues_found=healing.get("still_broken", 0),
        self_healing_auto_fixed=healing.get("auto_fixed", 0),
        backend_health=snap.get("transport_health", {}),
        disk_free_gb=sys_stats.disk_free_gb,
        memory_usage_mb=float(sys_stats.memory_used_mb),
    )


# ── /api/v1/components ────────────────────────────────────────────────────────


@app.get(
    "/api/v1/components",
    response_model=ComponentsResponse,
    summary="Daemon subsystem component health",
    tags=["Health"],
    responses={
        200: {"description": "All component health records."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def get_components(
    _key: Optional[str] = Depends(_check_api_key),
) -> ComponentsResponse:
    """Return health snapshots for all registered daemon subsystem components.

    Components include the poll loop, vault sync, transport health checker,
    consciousness loop, and the self-healer watchdog.  Each record includes
    status, heartbeat age, and restart history.
    """
    service = _ctx.get("state")
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    # The component manager is on the DaemonService object, not DaemonState.
    # Access it via _ctx["state"] which may expose _component_mgr.
    daemon_service = _ctx.get("service")
    if daemon_service and hasattr(daemon_service, "_component_mgr"):
        raw = daemon_service._component_mgr.snapshot()
    else:
        raw = []
    return ComponentsResponse(components=[ComponentSnapshot(**c) for c in raw])


# ── /api/v1/dashboard ────────────────────────────────────────────────────────


@app.get(
    "/api/v1/dashboard",
    response_model=DashboardResponse,
    summary="Full daemon dashboard snapshot",
    tags=["Dashboard"],
    responses={
        200: {"description": "Complete dashboard data."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def get_dashboard(
    _key: Optional[str] = Depends(_check_api_key),
) -> DashboardResponse:
    """Return the complete dashboard data used by the HTML UI and Flutter app.

    Assembles agent identity, daemon runtime metrics, consciousness status,
    LLM/transport backend availability, recent conversations, host system
    stats, and recent error messages into a single JSON snapshot.
    """
    state = _ctx.get("state")
    config = _ctx.get("config")
    if state is None or config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    consciousness = _ctx.get("consciousness")
    snap = state.snapshot()
    sys_stats = _collect_system_stats()

    # Agent identity
    agent: Dict[str, Any] = {}
    try:
        runtime = _ctx.get("runtime")
        if runtime:
            m = runtime.manifest
            agent = {
                "name": m.identity.name,
                "fingerprint": m.identity.fingerprint or "",
                "consciousness": m.consciousness.value if hasattr(m, "consciousness") else "",
                "version": m.version,
            }
    except Exception as exc:
        logger.warning("Failed to read agent identity from runtime manifest: %s", exc)
    if not agent:
        try:
            identity_path = config.home / "identity" / "identity.json"
            if identity_path.exists():
                agent = json.loads(identity_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read identity.json for API status: %s", exc)

    # Consciousness stats
    c_stats: Dict[str, Any] = {}
    if consciousness:
        try:
            c_stats = dict(consciousness.stats)
        except Exception as exc:
            logger.warning("Failed to read consciousness stats: %s", exc)

    # Recent conversations
    conversations: List[Dict[str, Any]] = []
    try:
        conv_dir = config.shared_root / "conversations"
        if conv_dir.exists():
            for cf in sorted(conv_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:5]:
                msgs = json.loads(cf.read_text(encoding="utf-8"))
                if isinstance(msgs, list) and msgs:
                    last = msgs[-1]
                    preview = (last.get("content") or last.get("message", ""))[:80]
                    conversations.append({
                        "peer": cf.stem,
                        "count": len(msgs),
                        "last": last.get("timestamp"),
                        "preview": preview,
                    })
    except Exception as exc:
        logger.warning("Failed to list recent conversations for API status: %s", exc)

    daemon_summary = DaemonSummary(
        running=snap.get("running", True),
        pid=snap.get("pid"),
        uptime_seconds=snap.get("uptime_seconds", 0.0),
        messages_received=snap.get("messages_received", 0),
        syncs_completed=snap.get("syncs_completed", 0),
        error_count=len(snap.get("recent_errors", [])),
        inflight_count=snap.get("inflight_count", 0),
    )

    return DashboardResponse(
        agent=agent,
        daemon=daemon_summary,
        consciousness=c_stats,
        backends=snap.get("transport_health", {}),
        conversations=conversations,
        system=sys_stats,
        recent_errors=snap.get("recent_errors", []),
    )


# ── /api/v1/capstone ─────────────────────────────────────────────────────────


@app.get(
    "/api/v1/capstone",
    response_model=CapstoneResponse,
    summary="Capstone pillars, memory, board, and consciousness",
    tags=["Dashboard"],
    responses={
        200: {"description": "Full capstone pillar snapshot."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def get_capstone(
    _key: Optional[str] = Depends(_check_api_key),
) -> CapstoneResponse:
    """Return pillars, memory stats, coordination board summary, and consciousness.

    This is the primary endpoint consumed by the vanilla-JS dashboard and
    Flutter app for a high-level sovereign-agent state overview.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    consciousness = _ctx.get("consciousness")

    # Pillars via runtime
    agent: Dict[str, Any] = {}
    pillars: Dict[str, str] = {}
    try:
        runtime = _ctx.get("runtime")
        if runtime:
            m = runtime.manifest
            agent = {"name": m.identity.name, "fingerprint": m.identity.fingerprint or ""}
            pillars = {k: v.value for k, v in m.pillar_summary.items()}
    except Exception as exc:
        logger.warning("Failed to read agent pillars from runtime manifest: %s", exc)

    # Memory stats
    memory = MemorySummary()
    try:
        from .memory_engine import get_stats as _mem_stats

        ms = _mem_stats(config.home)
        memory = MemorySummary(
            total=ms.total_memories,
            short_term=ms.short_term,
            mid_term=ms.mid_term,
            long_term=ms.long_term,
            status=ms.status.value,
        )
    except Exception as exc:
        logger.warning("Failed to collect memory stats for capstone API: %s", exc)

    # Coordination board
    board: Dict[str, Any] = {"summary": {}, "active": []}
    try:
        from .coordination import Board

        brd = Board(config.home)
        views = brd.get_task_views()
        board = {
            "summary": {
                "total": len(views),
                "done": sum(1 for v in views if v.status.value == "done"),
                "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
                "claimed": sum(1 for v in views if v.status.value == "claimed"),
                "open": sum(1 for v in views if v.status.value == "open"),
            },
            "active": [
                {
                    "id": v.task.id,
                    "title": v.task.title,
                    "priority": v.task.priority.value,
                    "status": v.status.value,
                    "claimed_by": v.claimed_by,
                }
                for v in views
                if v.status.value in ("in_progress", "claimed")
            ],
        }
    except Exception as exc:
        logger.warning("Failed to collect coordination board data for capstone API: %s", exc)

    # Consciousness stats
    c_stats: Dict[str, Any] = {}
    if consciousness:
        try:
            c_stats = dict(consciousness.stats)
        except Exception as exc:
            logger.warning("Failed to read consciousness stats for capstone API: %s", exc)

    return CapstoneResponse(
        agent=agent,
        pillars=pillars,
        memory=memory,
        board=board,
        consciousness=c_stats,
    )


# ── /api/v1/activity (SSE) ────────────────────────────────────────────────────


@app.get(
    "/api/v1/activity",
    summary="Server-Sent Events activity stream",
    tags=["Streaming"],
    responses={
        200: {
            "description": "SSE stream of daemon activity events.",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
    },
)
async def get_activity_stream(
    request: Request,
    _key: Optional[str] = Depends(_check_api_key),
) -> StreamingResponse:
    """Stream daemon activity events as Server-Sent Events (SSE).

    Replays the recent activity history on connect, then streams live
    events.  Sends a ``: heartbeat`` comment every 15 seconds to keep
    the connection alive through proxies.

    Returns a ``text/event-stream`` response; use ``EventSource`` in
    browsers or ``httpx-sse`` in Python clients.
    """
    from . import activity as _activity

    q: queue.Queue = queue.Queue(maxsize=200)
    _activity.register_client(q)

    async def event_generator() -> AsyncIterator[bytes]:
        # Replay history so late-joining clients see context
        try:
            for chunk in _activity.get_history_encoded():
                yield chunk
        except Exception as exc:
            logger.warning("Failed to replay activity stream history: %s", exc)
        # Stream live events; yield keep-alive comments on timeout
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    # Poll the queue with a short timeout to allow disconnect checks
                    chunk = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: q.get(timeout=15)
                    )
                    yield chunk
                except Exception:
                    yield b": heartbeat\n\n"
        finally:
            _activity.unregister_client(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ── /api/v1/household/agents ──────────────────────────────────────────────────


@app.get(
    "/api/v1/household/agents",
    response_model=HouseholdAgentsResponse,
    summary="List all household agents",
    tags=["Household"],
    responses={
        200: {"description": "All agents found in the shared household directory."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def list_household_agents(
    _key: Optional[str] = Depends(_check_api_key),
) -> HouseholdAgentsResponse:
    """Return a list of all agents known to the shared household.

    Reads agent identity files and heartbeats from the shared root and
    enriches each entry with liveness status.  The calling agent's
    consciousness stats are attached where available.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    consciousness = _ctx.get("consciousness")
    agents_dir = config.shared_root / "agents"
    heartbeats_dir = config.shared_root / "heartbeats"
    agents: List[HouseholdAgent] = []

    if agents_dir.exists():
        for agent_dir in sorted(agents_dir.iterdir()):
            if not agent_dir.is_dir():
                continue
            agent_name = agent_dir.name
            entry: Dict[str, Any] = {"name": agent_name}

            identity_path = agent_dir / "identity" / "identity.json"
            if identity_path.exists():
                try:
                    entry["identity"] = json.loads(identity_path.read_text(encoding="utf-8"))
                except Exception as exc:
                    logger.warning("Failed to read identity for agent %s: %s", agent_name, exc)

            hb: Optional[Dict[str, Any]] = None
            hb_path = heartbeats_dir / f"{agent_name.lower()}.json"
            if hb_path.exists():
                try:
                    hb = json.loads(hb_path.read_text(encoding="utf-8"))
                    alive = _hb_alive(hb)
                    hb["alive"] = alive
                    entry["heartbeat"] = hb
                    entry["status"] = hb.get("status", "unknown") if alive else "stale"
                except Exception as exc:
                    logger.warning("Failed to read heartbeat for agent %s: %s", agent_name, exc)
                    entry["status"] = "unknown"
            else:
                entry["status"] = "no_heartbeat"

            if consciousness:
                entry["consciousness"] = consciousness.stats

            agents.append(HouseholdAgent(**entry))

    return HouseholdAgentsResponse(agents=agents)


# ── /api/v1/household/agent/{name} ───────────────────────────────────────────


@app.get(
    "/api/v1/household/agent/{name}",
    response_model=HouseholdAgent,
    summary="Get details for a specific household agent",
    tags=["Household"],
    responses={
        200: {"description": "Agent details including identity, heartbeat, and memory count."},
        400: {"description": "Agent name is missing or invalid."},
        404: {"description": "Agent not found in the household directory."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def get_household_agent(
    name: str = FPath(..., description="Agent directory name (e.g. 'opus')."),
    _key: Optional[str] = Depends(_check_api_key),
) -> HouseholdAgent:
    """Return detailed information about a specific household agent.

    Loads the agent's identity file, most recent heartbeat, memory count
    across all layers, and a list of recent conversation threads.  The
    agent name must match the directory name under the shared ``agents/``
    root.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    if not name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Agent name required.")

    agent_dir = config.shared_root / "agents" / name
    if not agent_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent '{name}' not found.",
        )

    consciousness = _ctx.get("consciousness")
    entry: Dict[str, Any] = {"name": name}

    identity_path = agent_dir / "identity" / "identity.json"
    if identity_path.exists():
        try:
            entry["identity"] = json.loads(identity_path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("Failed to read identity for agent %s: %s", name, exc)

    hb_path = config.shared_root / "heartbeats" / f"{name.lower()}.json"
    if hb_path.exists():
        try:
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            alive = _hb_alive(hb)
            hb["alive"] = alive
            entry["heartbeat"] = hb
            entry["status"] = hb.get("status", "unknown") if alive else "stale"
        except Exception as exc:
            logger.warning("Failed to read heartbeat for agent %s: %s", name, exc)

    # Memory count
    memory_dir = agent_dir / "memory"
    if memory_dir.exists():
        count = 0
        for layer in ("short-term", "mid-term", "long-term"):
            layer_dir = memory_dir / layer
            if layer_dir.exists():
                count += sum(1 for _ in layer_dir.glob("*.json"))
        entry["memory_count"] = count

    if consciousness:
        entry["consciousness"] = consciousness.stats

    return HouseholdAgent(**entry)


# ── /api/v1/conversations ─────────────────────────────────────────────────────


@app.get(
    "/api/v1/conversations",
    response_model=ConversationsResponse,
    summary="List all conversation threads",
    tags=["Conversations"],
    responses={
        200: {"description": "All conversation threads, most recently active first."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def list_conversations(
    _key: Optional[str] = Depends(_check_api_key),
) -> ConversationsResponse:
    """Return a summary of all conversation threads in the shared conversations directory.

    Each entry includes the peer name, message count, timestamp of the last
    message, and a 120-character preview of the last message content.
    Threads are sorted by most recently modified file.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    conversations: List[ConversationSummary] = []
    conv_dir = config.shared_root / "conversations"
    if conv_dir.exists():
        for cf in sorted(conv_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                msgs = json.loads(cf.read_text(encoding="utf-8"))
                if isinstance(msgs, list):
                    last = msgs[-1] if msgs else {}
                    last_content = last.get("content", last.get("message", ""))
                    conversations.append(
                        ConversationSummary(
                            peer=cf.stem,
                            message_count=len(msgs),
                            last_message_time=last.get("timestamp") if msgs else None,
                            last_message_preview=(last_content or "")[:120],
                        )
                    )
            except Exception as exc:
                logger.warning("Failed to read conversation file %s: %s", cf, exc)
    return ConversationsResponse(conversations=conversations)


# ── /api/v1/conversations/{peer} ─────────────────────────────────────────────


@app.get(
    "/api/v1/conversations/{peer}",
    response_model=ConversationHistoryResponse,
    summary="Get conversation history with a peer",
    tags=["Conversations"],
    responses={
        200: {"description": "Full message history for the conversation."},
        400: {"description": "Peer name is empty or invalid."},
        404: {"description": "No conversation found with this peer."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def get_conversation(
    peer: str = FPath(
        ..., description="Peer agent or user name (alphanumeric, dashes, underscores)."
    ),
    _key: Optional[str] = Depends(_check_api_key),
) -> ConversationHistoryResponse:
    """Return the full message history for a conversation with the named peer.

    The peer parameter is sanitised (path-traversal prevention) before
    constructing the file path.  Returns 404 when no conversation file
    exists for the given peer.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    safe_peer = _sanitize_peer(peer)
    if not safe_peer:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Peer name required.")

    conv_file = config.shared_root / "conversations" / f"{safe_peer}.json"
    if not conv_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No conversation with '{safe_peer}'.",
        )
    try:
        msgs = json.loads(conv_file.read_text(encoding="utf-8"))
        return ConversationHistoryResponse(peer=safe_peer, messages=msgs)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── POST /api/v1/conversations/{peer}/send ────────────────────────────────────


@app.post(
    "/api/v1/conversations/{peer}/send",
    response_model=SendMessageResponse,
    status_code=status.HTTP_200_OK,
    summary="Send a message to a peer",
    tags=["Conversations"],
    responses={
        200: {"description": "Message accepted and dispatched."},
        400: {"description": "Peer name is invalid or message content is empty."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def send_message(
    peer: str = FPath(..., description="Target peer agent or user name."),
    body: SendMessageRequest = ...,
    _key: Optional[str] = Depends(_check_api_key),
) -> SendMessageResponse:
    """Send a message to a named peer.

    Writes the message envelope to the SKComm outbox for delivery by the
    transport layer.  If the consciousness loop is running the message is
    also processed inline to generate a reply.

    The ``content`` field in the request body is required and must be
    non-empty.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    safe_peer = _sanitize_peer(peer)
    if not safe_peer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid peer name."
        )

    message_id = str(uuid.uuid4())
    ts = datetime.now(timezone.utc).isoformat()
    envelope = {
        "message_id": message_id,
        "sender": "api",
        "recipient": safe_peer,
        "timestamp": ts,
        "payload": {"content": body.content, "content_type": "text"},
    }

    try:
        outbox = config.shared_root / "sync" / "comms" / "outbox"
        outbox.mkdir(parents=True, exist_ok=True)
        (outbox / f"{message_id}.skc.json").write_text(
            json.dumps(envelope, indent=2), encoding="utf-8"
        )
    except Exception as exc:
        logger.warning("Outbox write failed for %s: %s", safe_peer, exc)

    # Process through consciousness loop if available
    consciousness = _ctx.get("consciousness")
    if consciousness and getattr(consciousness, "_config", None) and consciousness._config.enabled:
        try:
            from types import SimpleNamespace

            fake_payload = SimpleNamespace(
                content=body.content,
                content_type=SimpleNamespace(value="text"),
            )
            fake_env = SimpleNamespace(sender=safe_peer, payload=fake_payload)
            threading.Thread(
                target=consciousness.process_envelope,
                args=(fake_env,),
                daemon=True,
            ).start()
        except Exception as exc:
            logger.debug("Consciousness process skipped: %s", exc)

    return SendMessageResponse(status="sent", message_id=message_id)


# ── DELETE /api/v1/conversations/{peer} ───────────────────────────────────────


@app.delete(
    "/api/v1/conversations/{peer}",
    response_model=DeleteConversationResponse,
    summary="Delete a conversation thread",
    tags=["Conversations"],
    responses={
        200: {"description": "Conversation deleted."},
        400: {"description": "Peer name is invalid."},
        404: {"description": "No conversation found with this peer."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def delete_conversation(
    peer: str = FPath(..., description="Peer name whose conversation to delete."),
    _key: Optional[str] = Depends(_check_api_key),
) -> DeleteConversationResponse:
    """Permanently delete the conversation history for a named peer.

    The peer parameter is sanitised before constructing the file path.
    Returns 404 when no conversation file exists.  This operation is
    irreversible — back up the file first if needed.
    """
    config = _ctx.get("config")
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    safe_peer = _sanitize_peer(peer)
    if not safe_peer:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid peer name."
        )

    conv_file = config.shared_root / "conversations" / f"{safe_peer}.json"
    if not conv_file.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No conversation with '{safe_peer}'.",
        )
    try:
        conv_file.unlink()
        return DeleteConversationResponse(status="deleted", peer=safe_peer)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── /api/v1/metrics ───────────────────────────────────────────────────────────


@app.get(
    "/api/v1/metrics",
    response_model=MetricsResponse,
    summary="Consciousness loop runtime metrics",
    tags=["Metrics"],
    responses={
        200: {"description": "Consciousness loop metrics."},
        503: {"description": "Consciousness loop is not loaded."},
    },
)
async def get_metrics(
    _key: Optional[str] = Depends(_check_api_key),
) -> MetricsResponse:
    """Return runtime statistics for the consciousness loop.

    Includes loop count, messages processed, average loop duration, and
    error count.  Returns HTTP 503 when consciousness has not been loaded
    (daemon started without consciousness, or not yet initialised).
    """
    consciousness = _ctx.get("consciousness")
    if consciousness is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Consciousness loop is not loaded.",
        )
    try:
        raw = consciousness.metrics.to_dict()
        return MetricsResponse(**{k: v for k, v in raw.items() if k in MetricsResponse.model_fields})
    except Exception:
        return MetricsResponse()


# ── /api/v1/skstacks/argocd/status helpers ────────────────────────────────────


def _find_skstacks_root() -> Path:
    """Locate the skstacks v2 root directory.

    Resolution order:
    1. ``SKSTACKS_V2_ROOT`` environment variable.
    2. ``<cwd>/skstacks/v2`` (works when run from the project root).
    3. Relative to this file: ``../../../../skstacks/v2``
       (works from an installed editable package).

    Returns:
        Resolved Path to the skstacks v2 directory (may not exist).
    """
    env = os.environ.get("SKSTACKS_V2_ROOT")
    if env:
        return Path(env)
    cwd_candidate = Path.cwd() / "skstacks" / "v2"
    if cwd_candidate.exists():
        return cwd_candidate
    pkg_candidate = Path(__file__).resolve().parents[3] / "skstacks" / "v2"
    if pkg_candidate.exists():
        return pkg_candidate
    return cwd_candidate


def _load_first_argocd_doc(path: Path) -> Optional[dict]:
    """Return a normalised dict for the first ArgoCD Application in a YAML file.

    Args:
        path: Path to the YAML manifest.

    Returns:
        dict with keys ``name``, ``namespace``, ``project``,
        ``source_path``, ``repo_url``, ``target_revision``,
        ``sync_policy``, ``manifest_file``, or ``None`` on failure.
    """
    try:
        import yaml as _yaml

        content = path.read_text(encoding="utf-8")
        for doc in _yaml.safe_load_all(content):
            if doc and isinstance(doc, dict) and doc.get("kind") == "Application":
                meta = doc.get("metadata", {}) or {}
                spec = doc.get("spec", {}) or {}
                source = spec.get("source", {}) or {}
                return {
                    "name": meta.get("name", ""),
                    "namespace": meta.get("namespace", "argocd"),
                    "project": spec.get("project", ""),
                    "source_path": source.get("path", ""),
                    "repo_url": source.get("repoURL", ""),
                    "target_revision": source.get("targetRevision", ""),
                    "sync_policy": spec.get("syncPolicy", {}),
                    "manifest_file": path.name,
                }
    except Exception as exc:
        logger.warning("Failed to parse ArgoCD manifest %s: %s", path, exc)
    return None


def _argocd_color(sync_status: str, health_status: str) -> str:
    """Map ArgoCD sync + health status to a dashboard colour name.

    Args:
        sync_status: ArgoCD sync status string.
        health_status: ArgoCD health status string.

    Returns:
        One of ``"green"``, ``"yellow"``, ``"red"``, or ``"gray"``.
    """
    if sync_status == "OutOfSync" or health_status == "Degraded":
        return "red"
    if sync_status == "Synced" and health_status == "Healthy":
        return "green"
    if health_status == "Progressing":
        return "yellow"
    return "gray"


def _get_argocd_status() -> dict:
    """Parse skstacks ArgoCD manifests and optionally fetch live cluster status.

    Reads ``skstacks/v2/cicd/argocd/app-of-apps.yaml`` and all YAMLs under
    ``skstacks/v2/cicd/argocd/apps/`` to build the app list.  If ``kubectl``
    is available and the cluster is reachable it enriches each entry with live
    ``sync_status`` / ``health_status`` from the ArgoCD Application CRD.

    Returns:
        dict suitable for constructing an ``ArgoCDStatusResponse``.
    """
    skstacks_root = _find_skstacks_root()
    argocd_dir = skstacks_root / "cicd" / "argocd"
    apps_dir = argocd_dir / "apps"

    # ── Parse static YAML manifests ──────────────────────────────────────────
    apps_by_name: Dict[str, dict] = {}

    root_yaml = argocd_dir / "app-of-apps.yaml"
    if root_yaml.exists():
        doc = _load_first_argocd_doc(root_yaml)
        if doc and doc["name"]:
            apps_by_name[doc["name"]] = doc

    if apps_dir.exists():
        for app_yaml in sorted(apps_dir.glob("*.yaml")):
            doc = _load_first_argocd_doc(app_yaml)
            if doc and doc["name"]:
                apps_by_name[doc["name"]] = doc

    # ── Try live status via kubectl ──────────────────────────────────────────
    source = "yaml"
    live_status: Dict[str, dict] = {}
    try:
        result = subprocess.run(
            [
                "kubectl",
                "get",
                "applications.argoproj.io",
                "--all-namespaces",
                "-o",
                "json",
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if result.returncode == 0:
            kubectl_data = json.loads(result.stdout)
            for item in kubectl_data.get("items", []):
                name = (item.get("metadata") or {}).get("name", "")
                if not name:
                    continue
                item_status = item.get("status") or {}
                live_status[name] = {
                    "sync_status": (item_status.get("sync") or {}).get("status", "Unknown"),
                    "health_status": (item_status.get("health") or {}).get("status", "Unknown"),
                    "last_synced": (item_status.get("operationState") or {}).get("finishedAt"),
                }
            source = "yaml+kubectl"
    except Exception as exc:
        logger.warning("kubectl ArgoCD status query failed (using yaml only): %s", exc)

    # ── Merge and build output ───────────────────────────────────────────────
    apps = []
    for name, app in apps_by_name.items():
        ls = live_status.get(name, {})
        sync_status = ls.get("sync_status", "Unknown")
        health_status = ls.get("health_status", "Unknown")
        apps.append(
            ArgoCDApp(
                name=name,
                project=app.get("project", ""),
                namespace=app.get("namespace", "argocd"),
                source_path=app.get("source_path", ""),
                repo_url=app.get("repo_url", ""),
                target_revision=app.get("target_revision", ""),
                sync_status=sync_status,
                health_status=health_status,
                color=_argocd_color(sync_status, health_status),
                last_synced=ls.get("last_synced"),
                manifest_file=app.get("manifest_file", ""),
            )
        )

    # Root app-of-apps first, then alphabetical
    apps.sort(key=lambda a: (0 if a.name == "skstacks-apps" else 1, a.name))

    return {
        "source": source,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "skstacks_root": str(skstacks_root),
        "apps": apps,
        "summary": ArgoCDSummary(
            total=len(apps),
            synced=sum(1 for a in apps if a.sync_status == "Synced"),
            out_of_sync=sum(1 for a in apps if a.sync_status == "OutOfSync"),
            unknown=sum(1 for a in apps if a.sync_status == "Unknown"),
            healthy=sum(1 for a in apps if a.health_status == "Healthy"),
            degraded=sum(1 for a in apps if a.health_status == "Degraded"),
        ),
    }


# ── /api/v1/skstacks/argocd/status ───────────────────────────────────────────


@app.get(
    "/api/v1/skstacks/argocd/status",
    response_model=ArgoCDStatusResponse,
    summary="ArgoCD application status from skstacks/v2 manifests",
    tags=["SKStacks"],
    responses={
        200: {
            "description": (
                "ArgoCD app list parsed from skstacks/v2/cicd/argocd/. "
                "Live sync/health enriched via kubectl when available."
            )
        },
        500: {"description": "Failed to parse manifests."},
    },
)
async def get_argocd_status(
    _key: Optional[str] = Depends(_check_api_key),
) -> ArgoCDStatusResponse:
    """Return ArgoCD Applications defined in the skstacks/v2 manifests.

    Parses ``skstacks/v2/cicd/argocd/app-of-apps.yaml`` (the root
    *App of Apps*) and every YAML file under
    ``skstacks/v2/cicd/argocd/apps/``.

    If ``kubectl`` is present and a cluster is reachable, each entry is
    enriched with live ``sync_status`` and ``health_status`` from the
    ArgoCD Application CRD (``applications.argoproj.io``).  Otherwise all
    statuses are reported as ``Unknown`` and ``source`` is ``"yaml"``.

    Override the skstacks v2 path via the ``SKSTACKS_V2_ROOT`` environment
    variable.
    """
    try:
        raw = _get_argocd_status()
        return ArgoCDStatusResponse(**raw)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=str(exc),
        ) from exc


# ── /api/v1/logs (WebSocket, CapAuth required) ────────────────────────────────


@app.websocket("/api/v1/logs")
async def websocket_logs(
    websocket: WebSocket,
) -> None:
    """Stream live daemon log lines over a WebSocket connection.

    **Authentication:** A valid CapAuth Bearer token must be passed in the
    ``Authorization`` header during the WebSocket upgrade handshake.  The
    token is validated via CapAuth (or skcapstone signed tokens as fallback).
    The connection is closed with code 4401 if the token is missing or invalid.

    **Protocol:** Each message is a JSON object with ``{"type": "line", "line": "..."}``
    for log entries.  The last 50 lines from the current ``daemon.log`` are
    replayed on connect before streaming live tails.

    **Tags:** Streaming, Auth
    """
    # Validate CapAuth token from the Authorization header
    token_str: Optional[str] = None
    auth_header = websocket.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token_str = auth_header[7:].strip()

    fingerprint: Optional[str] = None
    config = _ctx.get("config")

    try:
        from skcomm.capauth_validator import CapAuthValidator

        fingerprint = CapAuthValidator(require_auth=True).validate(token_str)
    except ImportError:
        if token_str and config:
            try:
                from .tokens import import_token, verify_token

                tok = import_token(token_str)
                if verify_token(tok, home=config.home):
                    fingerprint = tok.payload.issuer
            except Exception:
                fingerprint = None

    if fingerprint is None:
        await websocket.close(code=4401, reason="CapAuth token required")
        return

    await websocket.accept()
    log_file: Optional[Path] = config.log_file if config else None

    # Replay the last 50 log lines
    if log_file and log_file.exists():
        try:
            from collections import deque

            with open(log_file, encoding="utf-8", errors="replace") as fh:
                tail_lines = list(deque(fh, maxlen=50))
            for line in tail_lines:
                await websocket.send_json({"type": "line", "line": line.rstrip("\n")})
        except Exception as exc:
            logger.warning("Failed to replay log tail history over websocket: %s", exc)

    # Tail the log file and stream new lines
    try:
        offset = log_file.stat().st_size if log_file and log_file.exists() else 0
        while True:
            if log_file and log_file.exists():
                try:
                    with open(log_file, encoding="utf-8", errors="replace") as fh:
                        fh.seek(offset)
                        chunk = fh.read()
                        if chunk:
                            for ln in chunk.splitlines():
                                await websocket.send_json({"type": "line", "line": ln})
                        offset = fh.tell()
                except Exception as exc:
                    logger.warning("Log tail websocket read error: %s", exc)
            await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        pass


# ── Legacy endpoints ──────────────────────────────────────────────────────────


@app.get(
    "/status",
    response_model=LegacyStatusResponse,
    summary="Legacy daemon status (deprecated)",
    tags=["Legacy"],
    deprecated=True,
    responses={
        200: {"description": "Daemon runtime snapshot."},
        503: {"description": "Daemon context not initialised."},
    },
)
async def legacy_status(
    _key: Optional[str] = Depends(_check_api_key),
) -> LegacyStatusResponse:
    """Return the legacy daemon status snapshot.

    **Deprecated.** Use ``GET /api/v1/health`` or ``GET /api/v1/dashboard``
    instead.  This endpoint is retained for backward compatibility with
    older connectors and the dashboard polling widget.
    """
    state = _ctx.get("state")
    if state is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Daemon is not running.",
        )
    snap = state.snapshot()
    return LegacyStatusResponse(
        running=snap.get("running", True),
        pid=snap.get("pid"),
        uptime_seconds=snap.get("uptime_seconds", 0.0),
        messages_received=snap.get("messages_received", 0),
        syncs_completed=snap.get("syncs_completed", 0),
        started_at=snap.get("started_at"),
        recent_errors=snap.get("recent_errors", []),
        inflight_count=snap.get("inflight_count", 0),
    )


@app.get(
    "/consciousness",
    response_model=Dict[str, Any],
    summary="Legacy consciousness stats (deprecated)",
    tags=["Legacy"],
    deprecated=True,
    responses={
        200: {"description": "Raw consciousness loop stats dict."},
    },
)
async def legacy_consciousness(
    _key: Optional[str] = Depends(_check_api_key),
) -> Dict[str, Any]:
    """Return raw consciousness loop statistics.

    **Deprecated.** Use ``GET /api/v1/capstone`` instead.
    """
    consciousness = _ctx.get("consciousness")
    if consciousness:
        return consciousness.stats
    return {"enabled": False, "reason": "not loaded"}


@app.get(
    "/ping",
    response_model=PingResponse,
    summary="Liveness ping",
    tags=["Health"],
    responses={
        200: {"description": "Pong response confirming daemon is alive."},
    },
)
async def ping(_key: Optional[str] = Depends(_check_api_key)) -> PingResponse:
    """Lightweight liveness check.

    Returns ``{"pong": true, "pid": <daemon-pid>}`` immediately.  Use this
    to confirm the API server is reachable before making heavier requests.
    """
    return PingResponse(pong=True, pid=os.getpid())


# ── Server factory ────────────────────────────────────────────────────────────


def start_api_server(
    state: Any,
    config: Any,
    consciousness: Any = None,
    runtime: Any = None,
    host: str = "127.0.0.1",
    port: int = 7779,
) -> threading.Thread:
    """Start the FastAPI server in a background daemon thread.

    Calls :func:`init_api` to bind the daemon context, then starts uvicorn
    in a dedicated thread.  The thread is a daemon thread so it will be
    killed automatically when the main process exits.

    Args:
        state: DaemonState instance.
        config: DaemonConfig instance.
        consciousness: Optional ConsciousnessLoop.
        runtime: Optional AgentRuntime.
        host: Bind address (default ``127.0.0.1``).
        port: Listen port (default ``7779``).

    Returns:
        The started background thread.

    Raises:
        ImportError: When uvicorn is not installed.
    """
    try:
        import uvicorn
    except ImportError as exc:
        raise ImportError(
            "uvicorn is required to start the FastAPI server.  "
            "Install with: pip install skcapstone[api]"
        ) from exc

    init_api(state=state, config=config, consciousness=consciousness, runtime=runtime)

    def _run() -> None:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
        )

    t = threading.Thread(target=_run, name="fastapi-api", daemon=True)
    t.start()
    logger.info(
        "FastAPI API server started — http://%s:%d  docs: http://%s:%d/docs",
        host,
        port,
        host,
        port,
    )
    return t
