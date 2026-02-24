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

Usage:
    skcapstone dashboard              # opens localhost:7778
    skcapstone dashboard --port 9000  # custom port
"""

from __future__ import annotations

import json
import logging
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
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


class DashboardHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the sovereign agent dashboard.

    Serves the HTML page and JSON API endpoints.
    """

    home: Path = Path.home() / ".skcapstone"

    def do_GET(self):
        """Handle GET requests."""
        if self.path == "/" or self.path == "/index.html":
            self._serve_html()
        elif self.path == "/api/status":
            self._serve_json(_get_agent_status(self.home))
        elif self.path == "/api/doctor":
            self._serve_json(_get_doctor_report(self.home))
        elif self.path == "/api/board":
            self._serve_json(_get_board_state(self.home))
        elif self.path == "/api/memory":
            self._serve_json(_get_memory_stats(self.home))
        else:
            self.send_error(404, "Not found")

    def _serve_html(self):
        """Serve the self-contained HTML dashboard."""
        content = _DASHBOARD_HTML.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _serve_json(self, data: dict):
        """Serve a JSON API response.

        Args:
            data: Dict to serialize as JSON.
        """
        body = json.dumps(data, indent=2, default=str).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        """Suppress default stderr logging — use logger instead."""
        logger.debug("Dashboard: %s", format % args)


def start_dashboard(home: Path, port: int = DEFAULT_DASHBOARD_PORT) -> HTTPServer:
    """Start the dashboard HTTP server.

    Args:
        home: Agent home directory.
        port: Port to listen on.

    Returns:
        HTTPServer: The running server (call serve_forever() or
            handle in a thread).
    """
    DashboardHandler.home = home

    server = HTTPServer(("127.0.0.1", port), DashboardHandler)
    logger.info("Dashboard running at http://127.0.0.1:%d", port)
    return server
