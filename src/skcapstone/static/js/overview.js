// Overview home: operational summary tiles + active work + recent activity +
// agent health, from one /api/overview call. Live-refreshes over SSE.
import { esc, getJSON, timeShort, avatarColor } from "./api.js";

const SEV_VAR = { sev1: "sev1", sev2: "sev2", sev3: "sev3", sev4: "sev4" };

async function load() {
  let d;
  try { d = await getJSON("/api/overview"); }
  catch (e) { document.getElementById("tiles").innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`; return; }
  renderTiles(d);
  renderActive(d.active_tasks || []);
  renderActivity(d.activity || []);
  renderHealth(d.agent || {});
}

function renderTiles(d) {
  const k = d.kanban || {}, itil = (d.itil || {}), kp = itil.kpis || {}, cm = d.cmdb || {};
  const health = cm.health || {};
  const wipOver = (k.wip_over || []).length;
  const sev = kp.sev1 ? `${kp.sev1} SEV1` : (kp.sev2 ? `${kp.sev2} SEV2` : "");
  document.getElementById("tiles").innerHTML = `
    <a class="tile" href="/board">
      <div class="th"><span class="ic">🗂️</span> Kanban</div>
      <div class="tn">${k.active || 0} <small>active</small></div>
      <div class="tsub">${(k.by_column && k.by_column.doing) || 0} in progress
        ${wipOver ? `<span class="chip warn">${wipOver} WIP over</span>` : `<span class="chip ok">WIP ok</span>`}</div>
    </a>
    <a class="tile ${kp.sev1 || kp.sev2 ? "alert" : ""}" href="/cockpit">
      <div class="th"><span class="ic">🚨</span> Incidents</div>
      <div class="tn">${kp.open_incidents || 0} <small>open</small></div>
      <div class="tsub">${sev ? `<span class="chip crit">${esc(sev)}</span>` : ""}
        ${itil.breaches ? `<span class="chip warn">${itil.breaches} past SLA</span>` : ""}</div>
    </a>
    <a class="tile" href="/cockpit">
      <div class="th"><span class="ic">🔁</span> Change / SLA</div>
      <div class="tn mono">${esc(kp.mttr || "-")} <small>MTTR</small></div>
      <div class="tsub">MTTA ${esc(kp.mtta || "-")} ${itil.cab ? `<span class="chip warn">${itil.cab} awaiting CAB</span>` : ""}</div>
    </a>
    <a class="tile ${health.down ? "alert" : ""}" href="/cmdb">
      <div class="th"><span class="ic">🖥️</span> Assets</div>
      <div class="tn">${cm.total || 0} <small>CIs</small></div>
      <div class="tsub">${health.down ? `<span class="chip crit">${health.down} down</span>` : ""}
        ${health.degraded ? `<span class="chip warn">${health.degraded} degraded</span>` : `<span class="chip ok">all healthy</span>`}</div>
    </a>`;
}

function renderActive(tasks) {
  const el = document.getElementById("active-tasks");
  if (!tasks.length) { el.innerHTML = `<div style="color:var(--ink3);font-size:12px">Nothing in progress</div>`; return; }
  el.innerHTML = tasks.map((t) => {
    const ai = t.ai ? `<span class="ai-chip ${t.ai === "needs-review" ? "review" : ""}">🤖 ${esc(t.ai)}</span>` : "";
    const own = t.owner ? `<span class="ava" style="background:${avatarColor(t.owner)}" title="${esc(t.owner)}">${esc(t.owner[0].toUpperCase())}</span>` : "";
    return `<div class="at-item" data-id="${esc(t.id)}">
      <span class="kbadge ${esc(t.kind)}">${esc(t.kind)}</span>
      <span class="att">${esc(t.title)}</span>${own}${ai}</div>`;
  }).join("");
  el.querySelectorAll(".at-item").forEach((n) => n.addEventListener("click", () => { location.href = "/board"; }));
}

function renderActivity(list) {
  const icon = { escalated: "🔴", resolved: "✅", acknowledged: "👀", created: "🆕", voted: "🗳️", deployed: "🚀", verified: "✅" };
  document.getElementById("activity").innerHTML = list.length
    ? list.map((e) => `<div class="fitem"><span class="ftime">${esc(timeShort(e.ts))}</span>
        <span class="fic">${icon[e.action] || "•"}</span>
        <span class="fbody"><span class="w">${esc(e.record || "")}</span> ${esc(e.action || "")}${e.note ? " · " + esc((e.note || "").slice(0, 60)) : ""}</span></div>`).join("")
    : `<div style="color:var(--ink3);font-size:12px">No recent activity</div>`;
}

function renderHealth(agent) {
  const el = document.getElementById("agent-health");
  const pillars = agent.pillars || {};
  const mem = agent.memory || {};
  const csc = agent.consciousness || {};
  const dot = (v) => (v === true || v === "ok" || v === "healthy" || v === "active") ? "ok"
    : (v === false || v === "error" || v === "down") ? "bad" : "warn";
  const pillarHtml = Object.keys(pillars).length
    ? `<div class="pillars">${Object.entries(pillars).map(([k, v]) =>
        `<div class="pillar"><span class="pd ${dot(typeof v === "object" ? (v.status || v.ok) : v)}"></span><span class="pn">${esc(k)}</span></div>`).join("")}</div>`
    : `<div style="color:var(--ink3);font-size:12px">agent health unavailable</div>`;
  const stats = `<div style="margin-top:12px">
    ${mem.total != null ? `<span class="hstat"><span class="hn mono">${mem.total}</span><span class="hl">memories</span></span>` : ""}
    ${csc.level != null ? `<span class="hstat"><span class="hn mono">${esc(String(csc.level))}</span><span class="hl">consciousness</span></span>` : ""}
    ${agent.name ? `<span class="hstat"><span class="hn">${esc(agent.name)}</span><span class="hl">agent</span></span>` : ""}
  </div>`;
  el.innerHTML = pillarHtml + stats;
}

function connectSSE() {
  const dot = document.getElementById("live-dot"), text = document.getElementById("live-text");
  let deb = null;
  const es = new EventSource("/api/events");
  const refresh = () => { clearTimeout(deb); deb = setTimeout(load, 400); };
  es.addEventListener("open", () => { dot.classList.add("on"); text.textContent = "live"; });
  es.addEventListener("board_changed", refresh);
  es.addEventListener("card_changed", refresh);
  es.addEventListener("error", () => { dot.classList.remove("on"); text.textContent = "reconnecting"; });
}

load();
connectSSE();
setInterval(load, 30000);
