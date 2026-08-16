"""
Sovereign agent web dashboard.

A self-contained status page at localhost:7778. Uses only the
Python stdlib (http.server + json) - no FastAPI, no npm, no
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
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger("skcapstone.dashboard")

DEFAULT_DASHBOARD_PORT = 7778

#: Cache window for the doctor report. ``run_diagnostics`` walks the entire
#: agent home (a whole-tree rglob for Syncthing conflict files); recomputing it
#: on every ``/api/doctor`` poll blocks the dashboard's asyncio event loop. The
#: health picture does not change second-to-second, so a short TTL is safe.
_DOCTOR_CACHE_TTL = 30.0
_doctor_cache: dict = {"ts": 0.0, "home": None, "report": None}

# ---------------------------------------------------------------------------
# Capability names for the write routes that are NOT queue-a-run and NOT a
# change.* PEP (card 9d37d53d).
#
# These three routes used to reach the coordination store, the CMDB, and the
# gateway allowlist without passing through the gate at all, so the loopback
# bind was their only control. They now go through the same staged
# token/pdp/both path (`_capability_gate` -> `queue_authz.authorize_capability`)
# as every other write route in this module.
#
# Capability CHOICE follows the precedent already set twice in the fleet:
# reuse an ALREADY-SEEDED capauth row rather than ship an ungranted new one
# that would deny every caller the moment the gate goes live.
#   * `api_change_verify` in this file reuses `change.validate` rather than
#     inventing `change.verify`.
#   * skchat's `dataplane_auth.py` maps this exact `POST /api/card/{id}/{action}`
#     route to an interim already-granted capability, with a note that it
#     migrates to `skboard.write` with the rest of the board family (L1.8).
#
# `agentrun.queue` is capauth's seeded ATTESTED write-class row (authz.py
# `_AGENTRUN_RULES`) and is the capability a dashboard operator already holds;
# it is the interim carrier for the two coordination-store writes until a
# `skboard.write` / `cmdb.*` row exists in capauth. `skgateway.admin` is NOT
# interim: capauth seeds it (VERIFIED) with the description "Mutate the gateway
# model catalog / advertise allowlist / routing", which is exactly and only what
# `/api/models/advertise` does.

#: Interim capability for board-card mutations (`POST /api/card/{id}/{action}`).
#: Migrates to `skboard.write` when capauth seeds that row (L1.8).
_CAP_CARD_MUTATE = "agentrun.queue"

#: Interim capability for the CMDB reseed (`POST /api/cmdb/seed`). Rebuilds the
#: CI inventory under the agent home; write-class. Migrates to a dedicated
#: `cmdb.*` row when capauth seeds one.
_CAP_CMDB_SEED = "agentrun.queue"

#: Capability for the gateway advertise-allowlist write
#: (`POST /api/models/advertise`). Not interim: this is capauth's own
#: `skgateway.admin` row, whose seeded description is this action.
_CAP_MODELS_ADVERTISE = "skgateway.admin"

# ---------------------------------------------------------------------------
# Change management (CM P2.3): the Validate button's `gh` helpers.
#
# Bare module-level functions (not closures inside create_app) so tests can
# monkeypatch them directly instead of shelling out to a real `gh` binary /
# network - the same isolation principle queue_authz's injectable `decide_fn`
# uses for the PDP call.
# ---------------------------------------------------------------------------

#: Default CI workflow file name used to nudge a draft PR that has no checks
#: yet. Overridable per-repo since SKWorld repos do not all name their
#: default workflow the same thing.
_CM_DEFAULT_WORKFLOW = "ci.yml"

#: ASAP schedule grace window (design doc section 4.3: "ASAP is not a special
#: case: it is window_start = now, window_end = now + a default grace").
#: Mirrors skcapstone's itil_change_schedule MCP tool (CM P1.2).
_CM_SCHEDULE_GRACE_HOURS = 4


def _gh_pr_checks(pr_url: str) -> dict:
    """Run ``gh pr checks <pr_url> --json ...`` and normalize the verdict.

    Args:
        pr_url: The draft PR's URL (``Change.prepared_pr["url"]``).

    Returns:
        dict: ``{"started": bool, "passed": bool, "checks": [...], "error":
        str|None}``. ``started`` is False when `gh` reports no checks yet (a
        draft PR whose CI has not fired); ``passed`` is only meaningful when
        ``started`` is True. Never raises: a `gh` failure (missing binary,
        no network, no checks) folds into a `started: False` result so the
        caller can fall through to :func:`_gh_trigger_checks`.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["gh", "pr", "checks", pr_url, "--json", "name,state,bucket,link"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"started": False, "passed": False, "checks": [], "error": str(exc)}

    try:
        checks = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        checks = []
    if not isinstance(checks, list):
        checks = []
    started = bool(checks)
    passed = started and all(c.get("bucket") == "pass" for c in checks)
    error = None if started else ((proc.stderr or "").strip() or None)
    return {"started": started, "passed": passed, "checks": checks, "error": error}


def _gh_trigger_checks(pr_url: str, branch: str | None) -> bool:
    """Best-effort nudge for a draft PR whose checks have not started yet.

    `gh pr checks` reports nothing until CI actually runs; re-requesting the
    default workflow is the standard trigger. The workflow file name is
    configurable (``SKDASHBOARD_CM_WORKFLOW``, default ``ci.yml``).

    Args:
        pr_url: The draft PR's URL (used only for error messages; `gh
            workflow run` itself is keyed by repo + ref, resolved from the
            current working directory / ``--repo`` the way every other `gh`
            call in this codebase already assumes).
        branch: The PR's branch (``Change.prepared_pr["branch"]``).

    Returns:
        bool: Whether the trigger command exited zero. The caller always
        re-polls :func:`_gh_pr_checks` afterward regardless, so a repo with
        no matching workflow simply keeps surfacing ``started: False``
        rather than hanging.
    """
    import os
    import subprocess

    if not branch:
        return False
    workflow = os.environ.get("SKDASHBOARD_CM_WORKFLOW", _CM_DEFAULT_WORKFLOW)
    try:
        proc = subprocess.run(
            ["gh", "workflow", "run", workflow, "--ref", branch],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _gh_pr_head_sha(pr_url: str) -> str | None:
    """Fetch the PR's CURRENT head SHA (``gh pr view --json headRefOid``).

    Recorded on the validation event so the (later, Phase 3) deploy
    executor's freshness check can refuse a stale verdict whose ``head_sha``
    no longer matches the PR's actual head (design doc section 5.2 step 4).
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["gh", "pr", "view", pr_url, "--json", "headRefOid"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return data.get("headRefOid")


def _summarize_checks(checks: list[dict]) -> str:
    """One-line per-check summary for the validation event ('N/M passed')."""
    if not checks:
        return "no checks reported"
    passed = sum(1 for c in checks if c.get("bucket") == "pass")
    return f"{passed}/{len(checks)} checks passed"


def _pir_draft(chg) -> str:
    """Assemble a deterministic PIR (post-implementation review) draft note
    from a folded ``skcoord.itil.Change`` record (CM P3.3, design doc
    docs/specs/2026-08-13-change-management-cab-ai-arch.md section 3:
    "deployed -> verified: post-implementation review (smoke checks + PIR
    note)").

    Pure text assembly from ``chg.prepared_pr`` / ``chg.validation`` / the
    ``->deployed`` timeline entry / ``chg.rollback_plan`` - the required
    floor for "the AI drafts the PIR" (no inference call needed to prefill
    the Verify box; a client is free to further edit or replace this text
    before submitting it as the actual PIR note).

    Args:
        chg: A folded ``Change`` record (``skcoord.itil.ITILManager``'s
            ``_fold_record`` output).

    Returns:
        str: A multi-line, deterministic draft (same record in, same string
        out - no randomness, no clock read beyond what is already on the
        record).
    """
    lines = [f"PIR draft for {chg.id}: {chg.title}"]

    pr = chg.prepared_pr or {}
    if pr.get("url"):
        lines.append(f"Prepared PR: {pr['url']} (branch {pr.get('branch') or 'unknown'})")

    validation = chg.validation or {}
    if validation:
        verdict = "PASSED" if validation.get("passed") else "FAILED"
        lines.append(
            f"Pre-deploy validation: {verdict} ({validation.get('summary') or 'no summary'})"
        )

    deployed_rows = [
        row
        for row in chg.timeline
        if row.get("action", "").endswith("->deployed") and not row.get("conflicted")
    ]
    if deployed_rows:
        row = deployed_rows[-1]
        lines.append(
            f"Deployed by {row.get('agent') or 'unknown'} at {row.get('ts') or 'unknown'}"
        )
        if row.get("note"):
            lines.append(f"Deploy note: {row['note']}")

    lines.append("Smoke checks: TODO, fill in before verifying.")
    lines.append("Rollback plan on file: " + (chg.rollback_plan or "none recorded"))
    return "\n".join(lines)


def _cm_asap_window() -> tuple[str, str]:
    """(window_start, window_end) for an ASAP schedule: now + a grace window."""
    from datetime import timedelta

    start = datetime.now(timezone.utc)
    end = start + timedelta(hours=_CM_SCHEDULE_GRACE_HOURS)
    return start.isoformat(), end.isoformat()


def _get_agent_status(home: Path) -> dict:
    """Load agent manifest and pillar status.

    Args:
        home: Agent home directory.

    Returns:
        dict: Agent status summary.
    """
    try:
        from skcapstone.runtime import get_runtime

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
            "pillars": {k: v.value for k, v in m.pillar_summary.items()},
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
            "connectors": [{"platform": c.platform, "active": c.active} for c in m.connectors],
            "home": str(m.home),
        }
    except Exception as exc:
        return {"error": str(exc)}


def _get_doctor_report(home: Path) -> dict:
    """Run diagnostics and return as dict, cached for ``_DOCTOR_CACHE_TTL``.

    The report is served from an in-process cache so the expensive whole-tree
    diagnostic scan runs at most once per TTL window regardless of how often
    the dashboard polls ``/api/doctor``. Transient errors are not cached.

    Args:
        home: Agent home directory.

    Returns:
        dict: Full diagnostic report.
    """
    now = time.monotonic()
    cache = _doctor_cache
    if (
        cache["report"] is not None
        and cache["home"] == home
        and now - cache["ts"] < _DOCTOR_CACHE_TTL
    ):
        return cache["report"]
    try:
        from skcapstone.doctor import run_diagnostics

        report = run_diagnostics(home).to_dict()
    except Exception as exc:
        return {"error": str(exc)}
    cache.update(ts=now, home=home, report=report)
    return report


def _get_board_state(home: Path) -> dict:
    """Load coordination board state.

    Args:
        home: Agent home directory.

    Returns:
        dict: Tasks and agents from the coordination board.
    """
    try:
        from skcoord.coordination import Board

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
        from skcapstone.memory_engine import get_stats

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

    Gracefully handles a stopped or unreachable daemon - all sections
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
    daemon_info: dict = {
        "running": False,
        "pid": None,
        "uptime_seconds": 0,
        "uptime_human": "0s",
        "started_at": None,
        "messages_received": 0,
        "syncs_completed": 0,
        "error_count": 0,
        "recent_errors": [],
        "inflight_count": 0,
    }
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
        with urllib.request.urlopen(urllib.request.Request(f"{ollama_host}/api/tags"), timeout=2):
            backend_health["ollama"] = True
    except Exception as exc:
        logger.debug("Ollama probe failed (not available): %s", exc)

    # ── Heartbeat (system metrics + active conversations) ─────────────────────
    system_info: dict = {}
    active_conversations: int = 0
    try:
        from skcapstone import DEFAULT_AGENT, SHARED_ROOT

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
<title>SKCapstone - Sovereign Agent Dashboard</title>
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


def _trust_graph_dict(home: Path) -> dict:
    """Build the trust graph as a plain dict for the ``/api/trust/graph`` endpoint.

    Delegates to :func:`capauth.trust.graph.build_trust_graph` and reuses its
    ``format_json`` renderer so the wire shape stays in one place. Any failure
    (missing sources, unreadable home) degrades to an empty graph rather than a
    500, so the dashboard panel always renders.

    Args:
        home: Agent home directory (``~/.skcapstone``).

    Returns:
        A dict with ``nodes``, ``edges``, ``stats`` and (on failure) a ``note``.
    """
    from capauth.trust import graph as tg

    try:
        graph = tg.build_trust_graph(home)
        return json.loads(tg.format_json(graph))
    except Exception as exc:  # noqa: BLE001 - never 500 the panel
        return {
            "agent": "unknown",
            "nodes": [],
            "edges": [],
            "stats": {"nodes": 0, "edges": 0, "by_type": {}},
            "note": f"trust graph unavailable: {exc}",
        }


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
    from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    from . import consent as consent_plane
    from . import dashboard_itil as di
    from . import dashboard_kanban as dk
    from . import queue_authz
    from .surface_registry import resolve_card_id

    def _cmdb():
        from . import dashboard_cmdb as dc

        return dc

    def _overview_home(h):
        from . import dashboard_overview as do

        return do.get_overview_home(h)

    static_dir = Path(__file__).parent / "static"

    def _page(name):
        async def handler(_request):
            return HTMLResponse((static_dir / name).read_text(encoding="utf-8"))

        return handler

    index = _page("overview.html")
    board_page = _page("board.html")
    cockpit_page = _page("cockpit.html")

    def _get_route(fn):
        async def handler(_request):
            return _json(fn(home))

        return handler

    async def api_kanban(_request):
        return _json(dk.get_kanban(home))

    # GTD list view: next-actions (default), inbox, waiting-for, etc. Each item
    # carries a card_id (gtd-<id>) so the client can drive the existing
    # /api/card/{id}/ai-suggestions + queue-ai routes with no new backend.
    _GTD_LIST_FILES = {
        "inbox": "inbox.json",
        "next-actions": "next-actions.json",
        "projects": "projects.json",
        "waiting-for": "waiting-for.json",
        "someday-maybe": "someday-maybe.json",
    }

    async def api_gtd_list(request):
        import json as _json_mod

        which = request.query_params.get("list", "next-actions")
        fname = _GTD_LIST_FILES.get(which, "next-actions.json")
        path = home / "coordination" / "gtd" / fname
        try:
            items = _json_mod.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError, OSError):
            items = []
        out = []
        for it in items if isinstance(items, list) else []:
            if not isinstance(it, dict) or not it.get("id"):
                continue
            out.append(
                {
                    "id": it["id"],
                    "card_id": f"gtd-{it['id']}",
                    "text": it.get("text", ""),
                    "context": it.get("context"),
                    "priority": it.get("priority"),
                    "status": it.get("status"),
                    "source": it.get("source"),
                    "created_at": it.get("created_at"),
                }
            )
        return _json({"list": which, "items": out})

    async def api_card(request):
        return _json(dk.get_card(home, request.path_params["card_id"]))

    async def api_card_consent(request):
        """GET /api/card/{card_id}/consent - who consented to what, and when.

        Answers straight from the card's own event log (Unified Consent Plane
        Phase 2, acceptance criterion: "a query answers who consented to this
        action and when from the object's own event log").
        """
        card_id = request.path_params["card_id"]
        events = consent_plane.consent_history_for_card(home, card_id)
        return _json({"id": card_id, "events": events})

    async def api_ai_suggestions(request):
        from skcapstone import agent_run as ar

        use_llm = request.query_params.get("llm", "1") != "0"
        # The LLM (auto-routed, thinking-on) can take ~15s; give it headroom when
        # explicitly asked (the client fetches heuristics first, then upgrades).
        timeout = 35.0 if use_llm else 1.0
        return _json(
            ar.suggest_next_steps(
                home, request.path_params["card_id"], use_llm=use_llm, timeout=timeout
            )
        )

    def _capability_gate(request, *, resource, capability, actor):
        """Staged authz for ONE privileged capability. The single gate body.

        Preserves today's dev behavior (loopback-open) ONLY while neither
        ``SKAI_AUTHZ`` nor ``SKAI_QUEUE_TOKEN`` is configured, so a live seat that
        never set a secret does not suddenly break. The moment either is set, the
        decision routes through ``queue_authz.authorize_capability`` (token /
        capauth PDP / both), which is fail-closed on every branch.

        Every write route in this module funnels through here, so "is this route
        gated?" is answered by one grep for ``_capability_gate`` rather than by
        reading each handler. ``_queue_gate`` (mode-derived ``agentrun.*``) and
        ``_change_gate`` (explicit ``change.*``) are thin wrappers that differ
        only in how they name the capability.

        Returns:
            dict: ``{ok, reason, via, obligations}``.
        """
        import os

        if not os.environ.get("SKAI_AUTHZ") and not os.environ.get("SKAI_QUEUE_TOKEN"):
            return {
                "ok": True,
                "reason": "loopback-open (no SKAI_AUTHZ/SKAI_QUEUE_TOKEN set)",
                "via": "none",
            }
        return queue_authz.authorize_capability(
            token=request.headers.get("x-sk-capability"),
            resource=resource,
            capability=capability,
            actor=actor,
        )

    def _gate_deny(reason: str, status_code: int = 403):
        """The 403 body every gated route returns when its gate says no."""
        from starlette.responses import Response

        return Response(
            json.dumps({"error": "unauthorized: " + reason}),
            status_code=status_code,
            media_type="application/json",
        )

    async def api_card_mutate(request):
        """POST /api/card/{card_id}/{action} - board mutation, gated.

        note / move / assign / priority / label all write the shared skcoord
        card store, so this is a write-class route and goes through the same
        staged gate as the queue and change.* routes (card 9d37d53d: before
        that card it was reachable by anything that could open the loopback
        port). Capability ``_CAP_CARD_MUTATE`` - see that constant for why it
        is an interim reuse rather than a new capauth row.

        ``actor`` is still resolved BEFORE the gate so the PDP sees the same
        subject that will land on the card record. That subject comes from the
        unauthenticated ``X-SK-Actor`` header (or a body fallback); this gate
        does not change that, and cannot. Authenticating the actor is the
        Unified Consent Plane's job (capauth ``x-sk-capability`` epic
        a150c9c0), not this route's.
        """
        card_id = request.path_params["card_id"]
        action = request.path_params["action"]
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        actor = request.headers.get("x-sk-actor") or body.pop("actor", None) or "dashboard"
        decision = _capability_gate(
            request, resource=card_id, capability=_CAP_CARD_MUTATE, actor=actor
        )
        if not decision["ok"]:
            return _gate_deny(decision["reason"])
        result = dk.apply_mutation(home, card_id, action, actor, **body)
        if result.get("ok"):
            dk.BUS.publish({"type": "card_changed", "id": card_id, "actor": actor})
        return _json(result)

    def _queue_gate(request, *, resource, mode, actor):
        """Staged authz for the privileged 'queue AI' action.

        Thin wrapper over :func:`_capability_gate` that derives the capability
        from the run ``mode`` exactly as ``queue_authz.authorize_queue`` does
        (``capability_for``: ``agentrun.execute`` for ``mode="execute"``,
        otherwise ``agentrun.queue``). Returns a dict {ok, reason, via}.
        """
        return _capability_gate(
            request,
            resource=resource,
            capability=queue_authz.capability_for(mode),
            actor=actor,
        )

    def _ai_capability_ok(request):
        """Coarse boolean form of the queue gate (used by the assistant surface)."""
        d = _queue_gate(request, resource="assistant", mode="propose", actor=None)
        return d["ok"], d["reason"]

    def _persist_card_consent(request, *, card_id, capability, decision):
        """Best-effort ``consent.granted`` write for the queue-AI PEP.

        Provenance must never be the reason a gated write fails: any error
        persisting the event is logged and swallowed, exactly like
        ``fleet_adapter.py``'s attribution-must-not-block-availability
        posture (Unified Consent Plane Phase 2 / SPE section 7).
        """
        try:
            actor = consent_plane.resolve_consent_actor(request)
            consent_plane.record_card_consent(
                home, card_id, actor=actor, capability=capability, decision=decision
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("consent.granted write failed for card %s: %s", card_id, exc)

    async def _queue_run(request, card_id):
        """Shared queue-a-run body used by the card and surface routes."""
        from skcapstone import agent_run as ar
        from starlette.responses import Response

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        requester = request.headers.get("x-sk-actor") or body.get("requester") or "operator"
        mode = body.get("mode", "propose")
        decision = _queue_gate(request, resource=card_id, mode=mode, actor=requester)
        if not decision["ok"]:
            return Response(
                json.dumps({"error": "unauthorized: " + decision["reason"]}),
                status_code=403,
                media_type="application/json",
            )
        _persist_card_consent(
            request,
            card_id=card_id,
            capability=queue_authz.capability_for(mode),
            decision=decision,
        )
        result = ar.request_run(
            home,
            card_id,
            body.get("instruction", ""),
            agent=body.get("agent", "lumina"),
            mode=mode,
            requester=requester,
        )
        result["capability"] = decision["reason"]
        result["authz_via"] = decision["via"]
        if result.get("ok"):
            dk.BUS.publish({"type": "card_changed", "id": card_id, "actor": requester})
        return _json(result)

    async def api_queue_ai(request):
        return await _queue_run(request, request.path_params["card_id"])

    # ── Change management (CM P2.3): validate / schedule / arm PEPs ──
    #
    # Mirrors _queue_gate exactly (same dev-loopback-open carve-out, same
    # fail-closed staged token/pdp/both decision) but for an explicit
    # change.* capability instead of one derived from an agentrun mode - see
    # queue_authz.authorize_capability, the generalization this and
    # _queue_gate both now share.

    def _change_gate(request, *, resource, capability, actor):
        """Staged authz for a change.* PEP (validate/schedule/deploy).

        Design doc docs/specs/2026-08-13-change-management-cab-ai-arch.md
        section 7. A named alias for :func:`_capability_gate` (same body, same
        return shape) kept so the change routes read as change.* PEPs.
        """
        return _capability_gate(
            request, resource=resource, capability=capability, actor=actor
        )

    def _change_actor(request) -> str:
        """Resolve the authenticated actor for a change.* PEP.

        Same source of truth ``_queue_run`` uses for ``requester`` (the
        ``X-SK-Actor`` header set by the authenticating layer in front of
        this dashboard) - but, unlike ``_queue_run``, never falls back to a
        client-supplied JSON body field. The change.* routes are act-class
        (schedule/deploy) or write-class (validate) PEPs recording who
        validated/scheduled/armed a fleet change on the record itself; that
        identity must come only from the authenticated request context, not
        something the POST body claims.
        """
        return request.headers.get("x-sk-actor") or "operator"

    def _change_deny(reason: str, status_code: int = 403):
        """Named alias for :func:`_gate_deny`, kept for the change.* call sites."""
        return _gate_deny(reason, status_code)

    def _persist_change_consent(request, mgr, *, rid, capability, decision):
        """Best-effort ``consent.granted`` write for a change.* PEP.

        Called AFTER the change id has been resolved (``_resolve_change_or_404``),
        so ``target.id`` in the envelope is the real record id, never a
        redirect stub - the same target-validation-before-mutation ordering
        PROVENANCE_AND_MUTATION_STANDARD.md section 4 requires. Never blocks
        the underlying PEP: any error is logged and swallowed.
        """
        try:
            actor = consent_plane.resolve_consent_actor(request)
            consent_plane.record_change_consent(
                mgr, rid, actor=actor, capability=capability, decision=decision
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("consent.granted write failed for change %s: %s", rid, exc)

    def _resolve_change_or_404(mgr, change_id: str):
        """Resolve a change id (following redirect stubs); 404 Response if unknown.

        Returns:
            tuple: ``(resolved_id, None)`` on success, or ``(None, Response)``
            when the change does not exist.
        """
        from starlette.responses import Response

        rid = mgr._resolve_id(mgr.changes_dir, change_id)
        if mgr._load_core(mgr.changes_dir, rid) is None:
            return None, Response(
                json.dumps({"error": f"change {change_id} not found"}),
                status_code=404,
                media_type="application/json",
            )
        return rid, None

    async def api_change_consent(request):
        """GET /api/change/{id}/consent - who consented to what, and when.

        Answers straight from the change's own event log, mirroring
        ``api_card_consent`` for the ITILManager-backed store.
        """
        from skcoord.itil import ITILManager

        change_id = request.path_params["id"]
        mgr = ITILManager(home)
        rid, err = _resolve_change_or_404(mgr, change_id)
        if err is not None:
            return err
        events = consent_plane.consent_history_for_change(mgr, rid)
        return _json({"id": rid, "events": events})

    async def api_change_validate(request):
        """POST /api/change/{id}/validate - PEP change.validate (attested).

        Refuses (409) when the change has no ``prepared_pr`` (nothing to
        validate). Runs ``gh pr checks`` on the draft PR head, triggering the
        workflow first when checks have not started; appends the
        `validation` event with the verdict, a per-check summary, and the
        PR's current head SHA. A PASS while the change is still `proposed`
        needs no extra event: ``_fold_change`` (skcoord.itil) already
        auto-advances proposed -> reviewing the moment it replays a passing
        `validation` event, so appending a second, redundant `status` event
        here would itself fold as a conflicted transition (reviewing ->
        reviewing is not a legal edge). Reuses skcoord's folding rather than
        reimplementing it, mirroring skcapstone's itil_change_validate MCP
        tool (CM P1.2).
        """
        from skcoord.itil import Change, ITILManager

        change_id = request.path_params["id"]
        actor = _change_actor(request)
        decision = _change_gate(
            request, resource=change_id, capability="change.validate", actor=actor
        )
        if not decision["ok"]:
            return _change_deny(decision["reason"])

        mgr = ITILManager(home)
        rid, err = _resolve_change_or_404(mgr, change_id)
        if err is not None:
            return err
        _persist_change_consent(
            request, mgr, rid=rid, capability="change.validate", decision=decision
        )
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if chg is None or not (chg.prepared_pr and chg.prepared_pr.get("url")):
            from starlette.responses import Response

            return Response(
                json.dumps({"error": "change has no prepared_pr; nothing to validate"}),
                status_code=409,
                media_type="application/json",
            )

        pr_url = chg.prepared_pr["url"]
        branch = chg.prepared_pr.get("branch")
        result = _gh_pr_checks(pr_url)
        if not result["started"]:
            _gh_trigger_checks(pr_url, branch)
            result = _gh_pr_checks(pr_url)
        head_sha = _gh_pr_head_sha(pr_url) or chg.prepared_pr.get("head_sha")

        mgr._append_event(
            mgr.changes_dir,
            rid,
            actor,
            "validation",
            passed=bool(result["passed"]),
            head_sha=head_sha,
            url=pr_url,
            summary=_summarize_checks(result["checks"]),
            checks=result["checks"],
        )
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        dk.BUS.publish({"type": "card_changed", "id": chg.id, "actor": actor})
        return _json(
            {
                "validated": True,
                "id": chg.id,
                "status": chg.status.value,
                "validation": chg.validation,
            }
        )

    async def api_change_schedule(request):
        """POST /api/change/{id}/schedule - PEP change.schedule (verified).

        Body ``{window_start, window_end, asap, deploy_mode, note}`` appends
        a `schedule` event (fold-enforced: only valid while `approved`); body
        ``{unschedule: true}`` appends `unschedule` instead. ``deploy_mode``
        is locked to `confirm` for now (design doc section 9, Phase 3a) - any
        other value is rejected outright rather than silently coerced.
        """
        from skcoord.itil import Change, ITILManager

        change_id = request.path_params["id"]
        actor = _change_actor(request)
        decision = _change_gate(
            request, resource=change_id, capability="change.schedule", actor=actor
        )
        if not decision["ok"]:
            return _change_deny(decision["reason"])

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}

        mgr = ITILManager(home)
        rid, err = _resolve_change_or_404(mgr, change_id)
        if err is not None:
            return err
        _persist_change_consent(
            request, mgr, rid=rid, capability="change.schedule", decision=decision
        )

        if body.get("unschedule"):
            was_scheduled = (
                mgr._fold_record(mgr.changes_dir, rid, Change).status.value == "scheduled"
            )
            mgr._append_event(mgr.changes_dir, rid, actor, "unschedule", note=body.get("note", ""))
            chg = mgr._fold_record(mgr.changes_dir, rid, Change)
            dk.BUS.publish({"type": "card_changed", "id": chg.id, "actor": actor})
            return _json({"unscheduled": was_scheduled, "id": chg.id, "status": chg.status.value})

        deploy_mode = body.get("deploy_mode") or "confirm"
        if deploy_mode != "confirm":
            from starlette.responses import Response

            return Response(
                json.dumps({"error": "deploy_mode is locked to 'confirm' for now"}),
                status_code=400,
                media_type="application/json",
            )

        asap = bool(body.get("asap", False))
        window_start = body.get("window_start")
        window_end = body.get("window_end")
        if asap:
            window_start, window_end = _cm_asap_window()
        elif not (window_start and window_end):
            from starlette.responses import Response

            return Response(
                json.dumps(
                    {"error": "window_start and window_end are required unless asap is true"}
                ),
                status_code=400,
                media_type="application/json",
            )

        mgr._append_event(
            mgr.changes_dir,
            rid,
            actor,
            "schedule",
            window_start=window_start,
            window_end=window_end,
            asap=asap,
            deploy_mode=deploy_mode,
            note=body.get("note", ""),
        )
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if chg.status.value != "scheduled":
            from starlette.responses import Response

            return Response(
                json.dumps(
                    {
                        "scheduled": False,
                        "id": chg.id,
                        "status": chg.status.value,
                        "reason": (
                            "schedule is only valid while the change is 'approved' "
                            "(fold refused the transition)"
                        ),
                    }
                ),
                status_code=409,
                media_type="application/json",
            )
        dk.BUS.publish({"type": "card_changed", "id": chg.id, "actor": actor})
        return _json(
            {
                "scheduled": True,
                "id": chg.id,
                "status": chg.status.value,
                "scheduled_window": chg.scheduled_window,
            }
        )

    async def api_change_arm(request):
        """POST /api/change/{id}/arm - PEP change.deploy (verified).

        Writes the per-agent arm file ``cab-decisions/<chg>-<agent>.arm.json``
        (mirrors ``ITILManager.submit_cab_vote``'s own per-agent-file
        pattern: conflict-free, disjoint write sets). Consumed by the (later,
        Phase 3) deploy runner as the human-arm precondition for
        ``deploy_mode == "confirm"``; harmless standalone until that runner
        exists.
        """
        from skcoord.atomic_io import atomic_write_text
        from skcoord.itil import ITILManager

        change_id = request.path_params["id"]
        actor = _change_actor(request)
        decision = _change_gate(
            request, resource=change_id, capability="change.deploy", actor=actor
        )
        if not decision["ok"]:
            return _change_deny(decision["reason"])

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}

        mgr = ITILManager(home)
        rid, err = _resolve_change_or_404(mgr, change_id)
        if err is not None:
            return err
        _persist_change_consent(
            request, mgr, rid=rid, capability="change.deploy", decision=decision
        )

        mgr.ensure_dirs()
        arm = {
            "change_id": rid,
            "agent": actor,
            "armed": True,
            "armed_at": datetime.now(timezone.utc).isoformat(),
            "note": body.get("note", ""),
        }
        path = mgr.cab_dir / f"{rid}-{actor}.arm.json"
        atomic_write_text(path, json.dumps(arm, indent=2) + "\n")
        dk.BUS.publish({"type": "card_changed", "id": rid, "actor": actor})
        return _json({"armed": True, "id": rid, "agent": actor, "path": str(path)})

    async def api_change_verify(request):
        """POST /api/change/{id}/verify - PEP change.validate (attested).

        The PIR (post-implementation review) lifecycle route (CM P3.3,
        design doc docs/specs/2026-08-13-change-management-cab-ai-arch.md
        section 3: "deployed -> verified: post-implementation review (smoke
        checks + PIR note)"). Body ``{note}``: the PIR / smoke-check
        summary.

        Fail-closed at two layers:
          - This PEP refuses (409) when the change is not currently
            ``deployed`` - nothing to verify - and refuses (400) when the
            note is empty, before ever touching the event log.
          - skcoord's fold guard independently refuses the SAME edge
            (``_fold_change``: deployed -> verified without a note folds as
            a conflict entry, never a silent pass), so a change that raced
            out of ``deployed`` between our precondition read and the
            append still cannot slip a bare pass through; the post-append
            re-fold check below surfaces that race as a 409 too.

        Mirrors ``api_change_validate``'s staged authz + PEP shape exactly,
        reusing ``change.validate`` (the design doc lists ``verify / close``
        under the same ``change.propose`` tier as PIR-adjacent operator
        actions; this route reuses the already-seeded write-class
        ``change.validate`` capability rather than inventing a new
        ``change.verify`` capability row, matching the "mirror the existing
        route's authz helper exactly" instruction for this card).
        """
        from skcoord.itil import Change, ITILManager

        change_id = request.path_params["id"]
        actor = _change_actor(request)
        decision = _change_gate(
            request, resource=change_id, capability="change.validate", actor=actor
        )
        if not decision["ok"]:
            return _change_deny(decision["reason"])

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}

        mgr = ITILManager(home)
        rid, err = _resolve_change_or_404(mgr, change_id)
        if err is not None:
            return err

        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if chg is None or chg.status.value != "deployed":
            from starlette.responses import Response

            return Response(
                json.dumps(
                    {
                        "error": "change is not deployed; nothing to verify",
                        "status": chg.status.value if chg is not None else None,
                    }
                ),
                status_code=409,
                media_type="application/json",
            )

        note = (body.get("note") or "").strip()
        if not note:
            from starlette.responses import Response

            return Response(
                json.dumps({"error": "a PIR note is required to verify a deployed change"}),
                status_code=400,
                media_type="application/json",
            )

        mgr._append_event(mgr.changes_dir, rid, actor, "status", to="verified", note=note)
        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        if chg.status.value != "verified":
            from starlette.responses import Response

            return Response(
                json.dumps(
                    {
                        "verified": False,
                        "id": chg.id,
                        "status": chg.status.value,
                        "reason": (
                            "verify is only valid on a deployed change with a non-empty "
                            "PIR note (fold refused the transition)"
                        ),
                    }
                ),
                status_code=409,
                media_type="application/json",
            )
        dk.BUS.publish({"type": "card_changed", "id": chg.id, "actor": actor})
        return _json(
            {
                "verified": True,
                "id": chg.id,
                "status": chg.status.value,
                "pir_note": note,
            }
        )

    async def api_change_pir_draft(request):
        """GET /api/change/{id}/pir-draft - PEP change.validate (attested).

        The "AI drafts the PIR" helper: returns ``{draft}``, a deterministic
        text assembly (see ``_pir_draft``) from the folded change record, so
        a client can prefill the Verify box before an operator edits and
        submits it. Read-only, so it carries no status precondition of its
        own (the mutating ``/verify`` route above is what enforces
        ``deployed`` + non-empty note).
        """
        from skcoord.itil import Change, ITILManager

        change_id = request.path_params["id"]
        actor = _change_actor(request)
        decision = _change_gate(
            request, resource=change_id, capability="change.validate", actor=actor
        )
        if not decision["ok"]:
            return _change_deny(decision["reason"])

        mgr = ITILManager(home)
        rid, err = _resolve_change_or_404(mgr, change_id)
        if err is not None:
            return err

        chg = mgr._fold_record(mgr.changes_dir, rid, Change)
        return _json({"id": chg.id, "status": chg.status.value, "draft": _pir_draft(chg)})

    async def api_surface_suggest(request):
        """Generalized suggestions for ANY fleet surface: resolve (surface, id)
        to a shadow card, then reuse the card suggestion engine. Read; gated at
        the proxy layer like /api/card/*/ai-suggestions."""
        from skcapstone import agent_run as ar

        surface = request.path_params["surface"]
        item_id = request.path_params["id"]
        card_id = resolve_card_id(surface, item_id)
        if card_id is None:
            return _json({"error": f"unknown surface/id: {surface}/{item_id}", "suggestions": []})
        use_llm = request.query_params.get("llm", "1") != "0"
        timeout = 35.0 if use_llm else 1.0
        return _json(ar.suggest_next_steps(home, card_id, use_llm=use_llm, timeout=timeout))

    async def api_surface_queue(request):
        """Generalized 'queue AI to work an item' for ANY fleet surface."""
        from starlette.responses import Response

        surface = request.path_params["surface"]
        item_id = request.path_params["id"]
        card_id = resolve_card_id(surface, item_id)
        if card_id is None:
            return Response(
                json.dumps({"error": f"unknown surface/id: {surface}/{item_id}"}),
                status_code=404,
                media_type="application/json",
            )
        return await _queue_run(request, card_id)

    async def api_assistant(request):
        from . import dashboard_assistant as da

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        prompt = (body.get("prompt") or "").strip()
        actor = request.headers.get("x-sk-actor") or "operator"
        cap_ok, _ = _ai_capability_ok(request)
        if not prompt:
            return _json({"error": "prompt required"})
        gen = da.stream_answer(home, prompt, actor=actor, capability_ok=cap_ok)
        return StreamingResponse(
            gen,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def well_known_manifest(request):
        """skdashboard's SKWorld module manifest (public discovery metadata, no bearer).

        The umbrella shell reads this to learn the Board's entry, nav, and required
        auth audience/scopes before it has a token. URLs are origin-relative to the
        request, so they resolve against wherever the dashboard actually answers.
        Mirrors skchat's webui.py and skcode's daemon.py well-known route.
        """
        from .skdashboard_manifest import skdashboard_module_manifest

        return JSONResponse(skdashboard_module_manifest(str(request.base_url)))

    async def api_events(_request):
        async def stream():
            q = dk.BUS.subscribe()
            try:
                yield ": connected\n\n"
                while True:
                    try:
                        msg = await asyncio.wait_for(q.get(), timeout=20)
                        yield f"event: {msg.get('type', 'message')}\ndata: {json.dumps(msg)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": keep-alive\n\n"
            finally:
                dk.BUS.unsubscribe(q)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # ── Model-management console (card e7cde8f1) ──
    # The SKDashboard surface of the model-enablement picker. Proxies the
    # gateway's loopback advertise allowlist (same source of truth the skchat
    # app "Manage models" screen writes), so both surfaces stay in sync.
    import os as _os

    _gateway_admin = _os.environ.get("SKGATEWAY_URL", "http://localhost:18780").rstrip("/")

    async def api_models_get(_request):
        """Full discovered catalog + `advertised` flags (gateway /admin/models)."""
        import urllib.request

        try:
            with urllib.request.urlopen(f"{_gateway_admin}/admin/models", timeout=3) as r:
                return _json(json.loads(r.read().decode("utf-8")))
        except Exception as exc:  # gateway down: empty catalog, never 500 the page
            return _json({"object": "list", "data": [], "error": str(exc)})

    async def api_models_advertise(request):
        """Persist the enabled set to the gateway allowlist (PUT /admin/models/advertise).

        Gated on ``skgateway.admin`` (card 9d37d53d). This route is a write
        proxy onto skgateway's OWN admin surface: it changes which models the
        whole fleet is offered. capauth seeds ``skgateway.admin`` at the
        VERIFIED floor for exactly that action, so this dashboard must not be
        the door around it. Before this card the route had no gate at all.
        """
        import urllib.request

        decision = _capability_gate(
            request,
            resource="models/advertise",
            capability=_CAP_MODELS_ADVERTISE,
            actor=request.headers.get("x-sk-actor") or "operator",
        )
        if not decision["ok"]:
            return _gate_deny(decision["reason"])

        raw = await request.body()
        try:
            parsed = json.loads(raw or b"{}")
            enabled = parsed.get("enabled", [])
            if not isinstance(enabled, list) or not all(isinstance(x, str) for x in enabled):
                raise ValueError("enabled must be a list of strings")
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        req = urllib.request.Request(
            f"{_gateway_admin}/admin/models/advertise",
            data=json.dumps({"enabled": enabled}).encode("utf-8"),
            method="PUT",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return _json(json.loads(r.read().decode("utf-8")))
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=502)

    async def api_cmdb_seed(request):
        """POST /api/cmdb/seed - rebuild the CI inventory, gated.

        A write: it (re)creates configuration-item records under the agent
        home. Capability ``_CAP_CMDB_SEED``. Before card 9d37d53d this was a
        bare ``lambda`` in the route table with no gate at all, which is also
        why it is a named handler now: a lambda in the route list is exactly
        the shape that hides an ungated write.
        """
        decision = _capability_gate(
            request,
            resource="cmdb",
            capability=_CAP_CMDB_SEED,
            actor=request.headers.get("x-sk-actor") or "operator",
        )
        if not decision["ok"]:
            return _gate_deny(decision["reason"])
        return _json(_cmdb().seed(home))

    # ── Economy: fleet-wide cost (autopilot cost ledger) + joule wealth ──
    async def api_economy(_request):
        from . import dashboard_economy as deco

        return _json(deco.get_economy(home))

    # ── Fleet drift: install-profile drift per node (epic 3bbf39ea, card d1c6d605) ──
    # Reads published inventories out of the fleet tree, so this is the same
    # answer `skfleet node doctor --all` gives and costs no ssh. The alert gate
    # runs here rather than on a timer because this handler IS the poll loop;
    # dashboard_fleet.maybe_alert is what keeps the poll rate off Chef's phone.
    async def api_fleet_drift(_request):
        from . import dashboard_fleet as dfleet

        return _json(dfleet.get_drift(home))

    routes = [
        Route("/", index),
        Route("/index.html", index),
        Route("/models", _page("models.html")),
        Route("/api/models", api_models_get),
        Route("/api/models/advertise", api_models_advertise, methods=["POST"]),
        Route("/.well-known/skworld-module.json", well_known_manifest),
        Route("/board", board_page),
        Route("/api/status", _get_route(_get_agent_status)),
        Route("/api/overview", lambda r: _json(_overview_home(home))),
        Route("/api/doctor", _get_route(_get_doctor_report)),
        Route("/api/board", _get_route(_get_board_state)),
        Route("/api/memory", _get_route(_get_memory_stats)),
        Route("/api/daemon", _get_route(_get_daemon_json)),
        Route("/api/kanban", api_kanban),
        Route("/api/gtd", api_gtd_list),
        Route("/api/card/{card_id}", api_card),
        Route("/api/card/{card_id}/ai-suggestions", api_ai_suggestions),
        Route("/api/card/{card_id}/queue-ai", api_queue_ai, methods=["POST"]),
        Route("/api/card/{card_id}/consent", api_card_consent),
        Route("/api/change/{id}/consent", api_change_consent),
        Route("/api/change/{id}/validate", api_change_validate, methods=["POST"]),
        Route("/api/change/{id}/schedule", api_change_schedule, methods=["POST"]),
        Route("/api/change/{id}/arm", api_change_arm, methods=["POST"]),
        Route("/api/change/{id}/verify", api_change_verify, methods=["POST"]),
        Route("/api/change/{id}/pir-draft", api_change_pir_draft, methods=["GET"]),
        Route("/api/suggest/{surface}/{id}", api_surface_suggest),
        Route("/api/queue/{surface}/{id}", api_surface_queue, methods=["POST"]),
        Route("/api/card/{card_id}/{action}", api_card_mutate, methods=["POST"]),
        Route("/api/events", api_events),
        Route("/api/assistant", api_assistant, methods=["POST"]),
        Route("/assistant", _page("assistant.html")),
        Route("/cockpit", cockpit_page),
        Route("/api/itil/overview", lambda r: _json(di.get_overview(home))),
        Route("/api/itil/incidents", lambda r: _json(di.get_incidents(home))),
        Route("/api/itil/problems", lambda r: _json(di.get_problems(home))),
        Route("/api/itil/changes", lambda r: _json(di.get_changes(home))),
        Route(
            "/api/itil/kedb", lambda r: _json(di.search_kedb(home, r.query_params.get("q", "")))
        ),
        Route(
            "/api/itil/record/{kind}/{rid}",
            lambda r: _json(di.get_record(home, r.path_params["kind"], r.path_params["rid"])),
        ),
        Route("/cmdb", _page("cmdb.html")),
        Route("/api/cmdb/overview", lambda r: _json(_cmdb().get_overview(home))),
        Route(
            "/api/cmdb/ci/{ci_id}", lambda r: _json(_cmdb().get_ci(home, r.path_params["ci_id"]))
        ),
        Route("/api/cmdb/seed", api_cmdb_seed, methods=["POST"]),
        Route("/trust", _page("trust.html")),
        Route("/api/trust/graph", lambda r: _json(_trust_graph_dict(home))),
        Route("/economy", _page("economy.html")),
        Route("/api/economy", api_economy),
        Route("/fleet", _page("fleet.html")),
        Route("/api/fleet/drift", api_fleet_drift),
    ]
    if static_dir.exists():
        routes.append(Mount("/static", StaticFiles(directory=str(static_dir))))

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app):
        # Starlette removed on_event in 0.36; the lifespan context is the
        # supported replacement for startup/shutdown work.
        app.state.poll_task = asyncio.create_task(dk.poll_event_store(home))
        try:
            yield
        finally:
            task = getattr(app.state, "poll_task", None)
            if task is not None:
                task.cancel()

    app = Starlette(routes=routes, lifespan=_lifespan)

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
