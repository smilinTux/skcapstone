"""
Sovereign agent web dashboard.

A self-contained status page at localhost:7778. Uses only the
Python stdlib (http.server + json) — no FastAPI, no npm, no
build step. Open any browser, see your agent's health.

Serves:
    GET /           -> HTML dashboard (self-contained, no external deps)
    GET /api/status -> JSON agent status (all pillars)
    GET /api/doctor -> JSON diagnostic report
    GET /api/board  -> JSON coordination board state
    GET /api/memory -> JSON memory stats
    GET /api/daemon -> JSON daemon status for Flutter app consumption

Usage:
    skcapstone dashboard              # opens localhost:7778
    skcapstone dashboard --port 9000  # custom port
    skcapstone dashboard --json       # print daemon JSON and exit (no server)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("skcapstone.dashboard")

DEFAULT_DASHBOARD_PORT = 7778


def _get_agent_status(home: Path) -> dict:
    """Load agent manifest and pillar status.

    Args:
        home: Agent home directory.

    Returns:
        dict: Agent status summary.
    """
    try:
        from .runtime import get_runtime

        runtime = get_runtime(home)
        m = runtime.manifest
        if m.is_singular:
            consciousness = "SINGULAR"
        elif m.is_conscious:
            consciousness = "CONSCIOUS"
        else:
            consciousness = "AWAKENING"

        return {
            "name": m.name,
            "version": m.version,
            "consciousness": consciousness,
            "is_conscious": m.is_conscious,
            "is_singular": m.is_singular,
            "pillars": {
                k: v.value for k, v in m.pillar_summary.items()
            },
            "identity": {
                "name": m.identity.name,
                "fingerprint": m.identity.fingerprint or "",
                "status": m.identity.status.value,
            },
            "memory": {
                "total": m.memory.total_memories,
                "status": m.memory.status.value,
            },
            "trust": {
                "status": m.trust.status.value,
            },
            "security": {
                "audit_entries": m.security.audit_entries,
                "threats": m.security.threats_detected,
                "status": m.security.status.value,
            },
            "sync": {
                "seeds": m.sync.seed_count,
                "status": m.sync.status.value,
            },
            "connectors": [
                {"platform": c.platform, "active": c.active}
                for c in m.connectors
            ],
            "home": str(m.home),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _get_doctor_report(home: Path) -> dict:
    """Run diagnostics and return as dict.

    Args:
        home: Agent home directory.

    Returns:
        dict: Full diagnostic report.
    """
    try:
        from .doctor import run_diagnostics

        report = run_diagnostics(home)
        return report.to_dict()
    except Exception as exc:
        return {"error": str(exc)}


def _get_board_state(home: Path) -> dict:
    """Load coordination board state.

    Args:
        home: Agent home directory.

    Returns:
        dict: Tasks and agents from the coordination board.
    """
    try:
        from .coordination import Board

        board = Board(home)
        views = board.get_task_views()
        agents = board.load_agents()

        return {
            "tasks": [
                {
                    "id": v.task.id,
                    "title": v.task.title,
                    "priority": v.task.priority.value,
                    "status": v.status.value,
                    "claimed_by": v.claimed_by,
                    "tags": v.task.tags,
                }
                for v in views
            ],
            "agents": [
                {
                    "name": ag.agent,
                    "state": ag.state.value,
                    "current_task": ag.current_task,
                }
                for ag in agents
            ],
            "summary": {
                "total": len(views),
                "done": sum(1 for v in views if v.status.value == "done"),
                "open": sum(1 for v in views if v.status.value == "open"),
                "in_progress": sum(1 for v in views if v.status.value == "in_progress"),
            },
        }
    except Exception as exc:
        return {"error": str(exc)}


def _get_memory_stats(home: Path) -> dict:
    """Load memory statistics.

    Args:
        home: Agent home directory.

    Returns:
        dict: Memory counts by layer.
    """
    try:
        from .memory_engine import get_stats

        stats = get_stats(home)
        return {
            "total": stats.total_memories,
            "short_term": stats.short_term,
            "mid_term": stats.mid_term,
            "long_term": stats.long_term,
            "status": stats.status.value,
        }
    except Exception as exc:
        return {"error": str(exc)}


def _get_daemon_json(home: Path, daemon_port: int = 7777) -> dict:
    """Collect full daemon status for Flutter app consumption.

    Queries the running daemon's HTTP API (``/status`` and
    ``/consciousness``) and the local heartbeat file to assemble a
    single JSON-serializable snapshot suitable for machine consumers
    such as the SKChat Flutter app.

    Gracefully handles a stopped or unreachable daemon — all sections
    fall back to safe defaults so callers always get a complete dict.

    Args:
        home: Agent home directory.
        daemon_port: Port for the daemon HTTP API (default: 7777).

    Returns:
        dict: Snapshot with keys ``daemon``, ``consciousness``,
            ``backend_health``, ``active_conversations``, ``system``,
            and ``generated_at``.
    """
    import os
    import urllib.request

    now = datetime.now(timezone.utc).isoformat()

    # ── Daemon /status ────────────────────────────────────────────────────────
    daemon_info: dict = {"running": False, "pid": None, "uptime_seconds": 0,
                         "uptime_human": "0s", "started_at": None,
                         "messages_received": 0, "syncs_completed": 0,
                         "error_count": 0, "recent_errors": [], "inflight_count": 0}
    try:
        url = f"http://127.0.0.1:{daemon_port}/status"
        with urllib.request.urlopen(url, timeout=3) as resp:
            snap = json.loads(resp.read())
        uptime_s = int(snap.get("uptime_seconds", 0))
        h, rem = divmod(uptime_s, 3600)
        m, s = divmod(rem, 60)
        if h:
            uptime_human = f"{h}h {m}m"
        elif m:
            uptime_human = f"{m}m {s}s"
        else:
            uptime_human = f"{uptime_s}s"
        recent_errors = snap.get("recent_errors", [])
        daemon_info = {
            "running": snap.get("running", True),
            "pid": snap.get("pid"),
            "uptime_seconds": snap.get("uptime_seconds", 0),
            "uptime_human": uptime_human,
            "started_at": snap.get("started_at"),
            "messages_received": snap.get("messages_received", 0),
            "syncs_completed": snap.get("syncs_completed", 0),
            "error_count": len(recent_errors),
            "recent_errors": recent_errors,
            "inflight_count": snap.get("inflight_count", 0),
        }
    except Exception as exc:
        logger.warning("Failed to fetch daemon status for dashboard: %s", exc)

    # ── Daemon /consciousness ─────────────────────────────────────────────────
    consciousness_info: dict = {"enabled": False}
    try:
        url = f"http://127.0.0.1:{daemon_port}/consciousness"
        with urllib.request.urlopen(url, timeout=3) as resp:
            consciousness_info = json.loads(resp.read())
    except Exception as exc:
        logger.debug("Failed to fetch consciousness status for dashboard: %s", exc)

    # ── LLM backend availability ──────────────────────────────────────────────
    backend_health: dict = {
        "ollama": False,
        "anthropic": bool(os.environ.get("ANTHROPIC_API_KEY")),
        "grok": bool(os.environ.get("XAI_API_KEY")),
        "kimi": bool(os.environ.get("MOONSHOT_API_KEY")),
        "nvidia": bool(os.environ.get("NVIDIA_API_KEY")),
    }
    ollama_host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"{ollama_host}/api/tags"), timeout=2
        ):
            backend_health["ollama"] = True
    except Exception as exc:
        logger.debug("Ollama probe failed (not available): %s", exc)

    # ── Heartbeat (system metrics + active conversations) ─────────────────────
    system_info: dict = {}
    active_conversations: int = 0
    try:
        from . import SHARED_ROOT, DEFAULT_AGENT
        identity_path = home / "identity" / "identity.json"
        agent_name = DEFAULT_AGENT
        if identity_path.exists():
            ident = json.loads(identity_path.read_text(encoding="utf-8"))
            agent_name = ident.get("name", agent_name).lower()
        shared = Path(SHARED_ROOT).expanduser()
        hb_path = shared / "heartbeats" / f"{agent_name}.json"
        if not hb_path.exists():
            hb_path = home / "heartbeats" / f"{agent_name}.json"
        if hb_path.exists():
            hb = json.loads(hb_path.read_text(encoding="utf-8"))
            active_conversations = hb.get("active_conversations", 0)
            system_info = {
                "uptime_seconds": hb.get("uptime_seconds", 0),
                "cpu_load_1min": hb.get("cpu_load_1min", 0.0),
                "memory_used_mb": hb.get("memory_used_mb", 0),
            }
    except Exception as exc:
        logger.warning("Failed to read heartbeat data for dashboard: %s", exc)

    return {
        "generated_at": now,
        "daemon": daemon_info,
        "consciousness": consciousness_info,
        "backend_health": backend_health,
        "active_conversations": active_conversations,
        "system": system_info,
    }


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SKCapstone — Sovereign Agent Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
background:#0a0e17;color:#e0e6f0;min-height:100vh;padding:1.5rem}
h1{color:#00d4ff;font-size:1.6rem;margin-bottom:.3rem}
.subtitle{color:#6b7a8d;font-size:.9rem;margin-bottom:1.5rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:1rem;margin-bottom:1.5rem}
.card{background:#111827;border:1px solid #1e293b;border-radius:10px;padding:1.2rem}
.card h2{color:#00d4ff;font-size:1rem;margin-bottom:.8rem;display:flex;align-items:center;gap:.5rem}
.pill{display:inline-block;padding:.15rem .5rem;border-radius:6px;font-size:.75rem;font-weight:600}
.active{background:#064e3b;color:#34d399}.degraded{background:#78350f;color:#fbbf24}
.missing{background:#7f1d1d;color:#f87171}.done{background:#064e3b;color:#34d399}
.open{background:#1e3a5f;color:#60a5fa}.in_progress{background:#4c1d95;color:#c084fc}
.row{display:flex;justify-content:space-between;padding:.35rem 0;border-bottom:1px solid #1e293b}
.row:last-child{border:none}.label{color:#6b7a8d}.value{font-weight:600}
.check{display:flex;align-items:center;gap:.4rem;padding:.2rem 0}
.pass{color:#34d399}.fail{color:#f87171}
.task-row{padding:.4rem 0;border-bottom:1px solid #1e293b;display:flex;gap:.5rem;align-items:center}
.task-title{flex:1;font-size:.85rem}.task-agent{color:#6b7a8d;font-size:.8rem}
.stat-big{font-size:2rem;font-weight:700;color:#00d4ff}
.stat-label{font-size:.8rem;color:#6b7a8d}
.stat-box{text-align:center;padding:.5rem}
.refresh-btn{background:#1e293b;color:#60a5fa;border:1px solid #334155;
padding:.4rem 1rem;border-radius:6px;cursor:pointer;font-size:.85rem}
.refresh-btn:hover{background:#334155}
.header{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:1.5rem}
footer{text-align:center;color:#4b5563;font-size:.8rem;margin-top:2rem;padding:1rem}
</style>
</head>
<body>
<div class="header">
<div><h1>SKCapstone Dashboard</h1>
<div class="subtitle" id="agent-name">Loading...</div></div>
<button class="refresh-btn" onclick="loadAll()">Refresh</button>
</div>
<div class="grid" id="pillars"></div>
<div class="grid">
<div class="card" id="memory-card"><h2>Memory</h2><div>Loading...</div></div>
<div class="card" id="board-card"><h2>Board</h2><div>Loading...</div></div>
<div class="card" id="doctor-card"><h2>Health Checks</h2><div>Loading...</div></div>
</div>
<div class="card" id="tasks-card" style="margin-top:1rem"><h2>Recent Tasks</h2><div>Loading...</div></div>
<footer>SKCapstone Sovereign Agent Dashboard &mdash; staycuriousANDkeepsmilin</footer>
<script>
const API='';
async function loadAll(){
try{
const[status,doctor,board,mem]=await Promise.all([
fetch(API+'/api/status').then(r=>r.json()),
fetch(API+'/api/doctor').then(r=>r.json()),
fetch(API+'/api/board').then(r=>r.json()),
fetch(API+'/api/memory').then(r=>r.json()),
]);
renderStatus(status);renderDoctor(doctor);renderBoard(board);renderMemory(mem);
}catch(e){document.getElementById('agent-name').textContent='Error: '+e.message}}
function renderStatus(s){
document.getElementById('agent-name').innerHTML=
`<strong>${s.name||'?'}</strong> v${s.version||'?'} &mdash; ${s.consciousness||'?'}`;
const p=document.getElementById('pillars');
p.innerHTML=Object.entries(s.pillars||{}).map(([k,v])=>
`<div class="card"><h2>${k} <span class="pill ${v}">${v}</span></h2></div>`).join('')}
function renderMemory(m){
const c=document.getElementById('memory-card');
c.innerHTML=`<h2>Memory</h2>
<div style="display:flex;gap:1rem;justify-content:space-around">
<div class="stat-box"><div class="stat-big">${m.total||0}</div><div class="stat-label">Total</div></div>
<div class="stat-box"><div class="stat-big">${m.short_term||0}</div><div class="stat-label">Short</div></div>
<div class="stat-box"><div class="stat-big">${m.mid_term||0}</div><div class="stat-label">Mid</div></div>
<div class="stat-box"><div class="stat-big">${m.long_term||0}</div><div class="stat-label">Long</div></div>
</div>`}
function renderDoctor(d){
const c=document.getElementById('doctor-card');
const checks=(d.checks||[]).slice(0,12);
c.innerHTML=`<h2>Health <span class="pill ${d.all_passed?'active':'fail'}">${d.passed}/${d.total}</span></h2>`+
checks.map(ch=>`<div class="check"><span class="${ch.passed?'pass':'fail'}">${ch.passed?'\\u2713':'\\u2717'}</span>
<span>${ch.description}</span></div>`).join('')}
function renderBoard(b){
const s=b.summary||{};
const c=document.getElementById('board-card');
c.innerHTML=`<h2>Board</h2>
<div style="display:flex;gap:1rem;justify-content:space-around">
<div class="stat-box"><div class="stat-big">${s.done||0}</div><div class="stat-label">Done</div></div>
<div class="stat-box"><div class="stat-big">${s.in_progress||0}</div><div class="stat-label">Active</div></div>
<div class="stat-box"><div class="stat-big">${s.open||0}</div><div class="stat-label">Open</div></div>
</div>`;
const tc=document.getElementById('tasks-card');
const tasks=(b.tasks||[]).filter(t=>t.status!=='done').slice(0,10);
tc.innerHTML='<h2>Active Tasks</h2>'+
(tasks.length?tasks.map(t=>`<div class="task-row">
<span class="pill ${t.status}">${t.status}</span>
<span class="task-title">${t.title}</span>
<span class="task-agent">${t.claimed_by||''}</span>
</div>`).join(''):'<div style="color:#6b7a8d;padding:.5rem">No active tasks</div>')}
loadAll();setInterval(loadAll,15000);
</script>
</body>
</html>"""


def _json(data: dict):
    """Build a JSON API Response matching the legacy shape (indent + CORS).

    ``default=str`` keeps the original tolerance for non-JSON-native values.
    The CORS header is retained for the cross-origin Flutter ``/api/daemon``
    consumer; it can be dropped once that client is same-origin.
    """
    from starlette.responses import Response

    body = json.dumps(data, indent=2, default=str)
    return Response(
        body,
        media_type="application/json",
        headers={"Access-Control-Allow-Origin": "*"},
    )


def create_app(home: Path):
    """Build the Starlette ASGI app for the dashboard.

    Phase 1 (behavior-identical): serves the same self-contained HTML at ``/``
    and the same read-only GET JSON endpoints, reusing the ``_get_*`` functions.
    Later phases add ``/static`` modules, POST mutation routes, and SSE.

    Args:
        home: Agent home directory.

    Returns:
        Starlette: The ASGI application.
    """
    import asyncio

    from starlette.applications import Starlette
    from starlette.responses import HTMLResponse, StreamingResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    from . import dashboard_itil as di
    from . import dashboard_kanban as dk

    static_dir = Path(__file__).parent / "static"

    async def index(_request):
        return HTMLResponse(_DASHBOARD_HTML)

    def _page(name):
        async def handler(_request):
            return HTMLResponse((static_dir / name).read_text(encoding="utf-8"))
        return handler

    board_page = _page("board.html")
    cockpit_page = _page("cockpit.html")

    def _get_route(fn):
        async def handler(_request):
            return _json(fn(home))
        return handler

    async def api_kanban(_request):
        return _json(dk.get_kanban(home))

    async def api_card(request):
        return _json(dk.get_card(home, request.path_params["card_id"]))

    async def api_card_mutate(request):
        card_id = request.path_params["card_id"]
        action = request.path_params["action"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        actor = (
            request.headers.get("x-sk-actor")
            or body.pop("actor", None)
            or "dashboard"
        )
        result = dk.apply_mutation(home, card_id, action, actor, **body)
        if result.get("ok"):
            dk.BUS.publish({"type": "card_changed", "id": card_id, "actor": actor})
        return _json(result)

    def _ai_capability_ok(request):
        """Gate the privileged 'queue AI to execute' action.

        Requires a capability token (X-SK-Capability header == SKAI_QUEUE_TOKEN).
        When no token is configured this is loopback-open (dev); this is the
        upgrade point for a full capauth-signed grant + tailscale-serve exposure.
        """
        import hmac
        import os

        token = os.environ.get("SKAI_QUEUE_TOKEN")
        if not token:
            return True, "loopback-open (no SKAI_QUEUE_TOKEN set)"
        provided = request.headers.get("x-sk-capability", "")
        if hmac.compare_digest(provided, token):
            return True, "capability ok"
        return False, "missing or invalid capability token"

    async def api_queue_ai(request):
        from starlette.responses import Response

        from . import agent_run as ar

        card_id = request.path_params["card_id"]
        ok, reason = _ai_capability_ok(request)
        if not ok:
            return Response(
                json.dumps({"error": "unauthorized: " + reason}),
                status_code=403, media_type="application/json",
            )
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        requester = request.headers.get("x-sk-actor") or body.get("requester") or "operator"
        result = ar.request_run(
            home, card_id,
            body.get("instruction", ""),
            agent=body.get("agent", "lumina"),
            mode=body.get("mode", "propose"),
            requester=requester,
        )
        result["capability"] = reason
        if result.get("ok"):
            dk.BUS.publish({"type": "card_changed", "id": card_id, "actor": requester})
        return _json(result)

    async def api_events(_request):
        async def stream():
            q = dk.BUS.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"event: {msg.get('type','message')}\ndata: {json.dumps(msg)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                dk.BUS.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    routes = [
        Route("/", index),
        Route("/index.html", index),
        Route("/board", board_page),
        Route("/api/status", _get_route(_get_agent_status)),
        Route("/api/doctor", _get_route(_get_doctor_report)),
        Route("/api/board", _get_route(_get_board_state)),
        Route("/api/memory", _get_route(_get_memory_stats)),
        Route("/api/daemon", _get_route(_get_daemon_json)),
        Route("/api/kanban", api_kanban),
        Route("/api/card/{card_id}", api_card),
        Route("/api/card/{card_id}/queue-ai", api_queue_ai, methods=["POST"]),
        Route("/api/card/{card_id}/{action}", api_card_mutate, methods=["POST"]),
        Route("/api/events", api_events),
        Route("/cockpit", cockpit_page),
        Route("/api/itil/overview", lambda r: _json(di.get_overview(home))),
        Route("/api/itil/incidents", lambda r: _json(di.get_incidents(home))),
        Route("/api/itil/problems", lambda r: _json(di.get_problems(home))),
        Route("/api/itil/changes", lambda r: _json(di.get_changes(home))),
        Route("/api/itil/kedb", lambda r: _json(di.search_kedb(home, r.query_params.get("q", "")))),
        Route("/api/itil/record/{kind}/{rid}",
              lambda r: _json(di.get_record(home, r.path_params["kind"], r.path_params["rid"]))),
    ]
    if static_dir.exists():
        routes.append(Mount("/static", StaticFiles(directory=str(static_dir))))

    app = Starlette(routes=routes)

    @app.on_event("startup")
    async def _start_poll():
        app.state.poll_task = asyncio.create_task(dk.poll_event_store(home))

    return app


class _UvicornServer:
    """Adapter exposing ``serve_forever()``/``shutdown()`` over a uvicorn server.

    Preserves the call pattern the CLI and tests use
    (``start_dashboard(...).serve_forever()``) while running the Starlette app.
    Signal handlers are disabled so it can run inside a worker thread.
    """

    def __init__(self, app, port: int) -> None:
        import uvicorn

        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning", access_log=False
        )
        self._server = uvicorn.Server(config)

    def serve_forever(self) -> None:
        import threading

        # uvicorn installs signal handlers in serve(), which only works on the
        # main thread. On the main thread (CLI / systemd) keep them for graceful
        # SIGTERM; in a worker thread (tests) disable them.
        if threading.current_thread() is not threading.main_thread():
            self._server.install_signal_handlers = lambda: None
        self._server.run()

    def shutdown(self) -> None:
        self._server.should_exit = True


def start_dashboard(home: Path, port: int = DEFAULT_DASHBOARD_PORT) -> "_UvicornServer":
    """Start the dashboard server (Starlette + uvicorn).

    Args:
        home: Agent home directory.
        port: Port to listen on.

    Returns:
        _UvicornServer: call ``serve_forever()`` (blocking) or run in a thread;
        stop with ``shutdown()``.
    """
    app = create_app(home)
    logger.info("Dashboard running at http://127.0.0.1:%d", port)
    return _UvicornServer(app, port)
