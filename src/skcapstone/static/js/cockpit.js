// ITIL cockpit: overview KPIs, breach-risk, CAB queue, discipline tables, and
// record detail (stepper + lineage + timeline). Live-refreshes over SSE.
import { esc, getJSON, toast, timeShort } from "./api.js";
import { renderAIComposer, wireAIComposer } from "./ai_compose.js";

const SEV_VAR = { sev1: "sev1", sev2: "sev2", sev3: "sev3", sev4: "sev4" };
const CHANGE_STEPS = ["proposed", "reviewing", "approved", "implementing", "deployed", "verified"];

// ---- overview ----
async function loadOverview() {
  let d;
  try { d = await getJSON("/api/itil/overview"); }
  catch (e) { document.getElementById("breach").innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`; return; }
  if (d.error) { document.getElementById("breach").innerHTML = `<div class="emptymsg">${esc(d.error)}</div>`; return; }
  renderKPIs(d.kpis);
  renderSevBar(d.by_severity);
  renderBreach(d.breach_risk);
  renderCAB(d.cab_queue);
  renderActivity(d.activity);
  renderServices(d.services);
}

function renderKPIs(k) {
  const el = document.getElementById("kpis");
  const sev = k.sev1 ? `· ${k.sev1} SEV1` : (k.sev2 ? `· ${k.sev2} SEV2` : "");
  el.innerHTML = `
    <div class="kpi${k.sev1 || k.sev2 ? " alert" : ""}"><div class="l">Open Incidents</div><div class="n">${k.open_incidents} <small>${esc(sev)}</small></div><div class="s">${k.sev2} SEV2</div></div>
    <div class="kpi"><div class="l">MTTA</div><div class="n mono">${esc(k.mtta)}</div><div class="s">7d avg</div></div>
    <div class="kpi"><div class="l">MTTR</div><div class="n mono">${esc(k.mttr)}</div><div class="s">7d avg</div></div>
    <div class="kpi"><div class="l">Change Success</div><div class="n mono">${k.change_success == null ? "-" : k.change_success + "<small>%</small>"}</div><div class="s">fail ${k.change_fail == null ? "-" : k.change_fail + "%"}</div></div>
    <div class="kpi"><div class="l">Awaiting CAB</div><div class="n mono">${k.awaiting_cab}</div><div class="s">changes in review</div></div>`;
}

function renderSevBar(bs) {
  const max = Math.max(1, ...Object.values(bs));
  document.getElementById("sevbar").innerHTML = ["sev1", "sev2", "sev3", "sev4"].map((s) => {
    const n = bs[s] || 0;
    return `<div class="sevrow"><span class="lab" style="color:var(--${SEV_VAR[s]})">${s.toUpperCase()}</span>
      <span class="track"><span class="fill" style="width:${(n / max) * 100}%;background:var(--${SEV_VAR[s]})"></span></span>
      <span class="ct">${n}</span></div>`;
  }).join("");
}

function humanMin(m) {
  m = Math.abs(m);
  if (m < 60) return `${Math.round(m)}m`;
  if (m < 1440) return `${(m / 60).toFixed(1)}h`;
  return `${Math.round(m / 1440)}d`;
}

function fmtRemaining(m) {
  if (m < 0) return { cls: "over", txt: `${humanMin(m)} over` };
  if (m < 30) return { cls: "warn", txt: `${Math.round(m)}m left` };
  return { cls: "", txt: `${humanMin(m)} left` };
}

function renderBreach(list) {
  const el = document.getElementById("breach");
  if (!list.length) { el.innerHTML = `<div class="emptymsg">No open incidents 🎉</div>`; return; }
  el.innerHTML = list.map((b) => {
    const r = fmtRemaining(b.remaining_min);
    return `<div class="brow" data-inc="${esc(b.id)}">
      <span class="sev" style="background:var(--${SEV_VAR[b.severity]})">${esc(b.severity.toUpperCase())}</span>
      <span class="t" title="${esc(b.title)}">${esc(b.title)}</span>
      <span class="countdown ${r.cls}">${esc(r.txt)}</span></div>`;
  }).join("");
  el.querySelectorAll("[data-inc]").forEach((r) => r.addEventListener("click", () => openRecord("incident", r.dataset.inc)));
}

function renderCAB(list) {
  const el = document.getElementById("cab");
  if (!list.length) { el.innerHTML = `<div class="emptymsg">No changes awaiting CAB</div>`; return; }
  el.innerHTML = list.map((c) => {
    const pips = Array(Math.max(0, c.approve)).fill('<span class="pip y">✓</span>').join("") +
      Array(Math.max(0, c.reject)).fill('<span class="pip n">✕</span>').join("");
    return `<div class="cabrow" data-chg="${esc(c.id)}">
      <div class="cabtop"><span class="chgkind ${esc(c.change_type)}">${esc(c.change_type)}</span>
        <span class="ct">${esc(c.title)}</span>
        <span class="votes"><span class="pips">${pips || '<span class="pip" style="background:var(--hair)">?</span>'}</span> ${c.approve}✓ ${c.reject}✕</span></div>
      <div class="cabmeta"><span>risk: ${esc(c.risk)}</span>${c.rollback ? `<span>rollback: ${esc(c.rollback.slice(0, 50))}</span>` : ""}${c.voters.length ? `<span>voters: ${esc(c.voters.join(", "))}</span>` : ""}</div>
    </div>`;
  }).join("");
  el.querySelectorAll("[data-chg]").forEach((r) => r.addEventListener("click", () => openRecord("change", r.dataset.chg)));
}

function renderActivity(list) {
  const icon = { escalated: "🔴", resolved: "✅", acknowledged: "👀", created: "🆕", voted: "🗳️", deployed: "🚀", verified: "✅" };
  document.getElementById("activity").innerHTML = (list || []).map((e) =>
    `<div class="fitem"><span class="ftime">${esc(timeShort(e.ts))}</span><span class="fic">${icon[e.action] || "•"}</span>
      <span class="fbody"><span class="w">${esc(e.record)}</span> ${esc(e.action)}${e.note ? " · " + esc(e.note.slice(0, 60)) : ""}</span></div>`).join("")
    || `<div class="emptymsg">No recent activity</div>`;
}

function renderServices(list) {
  document.getElementById("services").innerHTML = (list || []).map((s) =>
    `<span class="svc"><span class="d" style="background:var(--${SEV_VAR[s.severity]})"></span>${esc(s.service)} <span style="color:var(--ink3)">${esc(s.severity)}</span></span>`).join("")
    || `<span style="color:var(--ink3);font-size:12px">All services healthy</span>`;
}

// ---- discipline tables ----
async function loadIncidents() {
  const d = await getJSON("/api/itil/incidents");
  const rows = d.incidents.map((i) => `<tr data-rec="incident:${esc(i.id)}">
    <td><span class="sev" style="background:var(--${SEV_VAR[i.severity]})">${esc(i.severity.toUpperCase())}</span></td>
    <td>${esc(i.title)}</td><td><span class="pill ${esc(i.status)}">${esc(i.status)}</span></td>
    <td>${esc(i.service || "-")}</td><td class="mono">${esc(i.age)}</td><td class="mono">${esc(i.mttr)}</td>
    <td>${i.problem ? esc(i.problem) : "-"}</td></tr>`).join("");
  document.getElementById("inc-body").innerHTML =
    `<table class="itil"><thead><tr><th>Sev</th><th>Title</th><th>Status</th><th>Service</th><th>Age</th><th>MTTR</th><th>Problem</th></tr></thead><tbody>${rows}</tbody></table>`;
  wireRows("inc-body");
}

async function loadProblems() {
  const d = await getJSON("/api/itil/problems");
  const rows = d.problems.map((p) => `<tr data-rec="problem:${esc(p.id)}">
    <td class="mono">${esc(p.id)}</td><td>${esc(p.title)}</td><td><span class="pill ${esc(p.status)}">${esc(p.status)}</span></td>
    <td>${p.incidents} inc</td><td>${p.workaround ? "yes" : "-"}</td><td>${p.kedb ? esc(p.kedb) : "-"}</td></tr>`).join("");
  document.getElementById("prb-body").innerHTML = `
    <div class="kedbbox"><input id="kedb-q" placeholder="Search KEDB (known errors + workarounds)…"><button class="btn" id="kedb-go">Search</button></div>
    <div id="kedb-results"></div>
    <table class="itil"><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Incidents</th><th>Workaround</th><th>KEDB</th></tr></thead><tbody>${rows}</tbody></table>`;
  wireRows("prb-body");
  const go = async () => {
    const q = document.getElementById("kedb-q").value.trim();
    const r = await getJSON("/api/itil/kedb?q=" + encodeURIComponent(q));
    document.getElementById("kedb-results").innerHTML = (r.results || []).map((e) =>
      `<div class="keitem"><div class="ket">${esc(e.title)} <span class="mono" style="color:var(--ink3);font-size:10px">${esc(e.id)}</span></div>
       ${e.root_cause ? `<div class="kew"><b>cause:</b> ${esc(e.root_cause)}</div>` : ""}
       ${e.workaround ? `<div class="kew"><b>workaround:</b> ${esc(e.workaround)}</div>` : ""}</div>`).join("")
      || `<div style="color:var(--ink3);font-size:12px;padding:4px 0">no matches</div>`;
  };
  document.getElementById("kedb-go").addEventListener("click", go);
  document.getElementById("kedb-q").addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
}

async function loadChanges() {
  const d = await getJSON("/api/itil/changes");
  const rows = d.changes.map((c) => `<tr data-rec="change:${esc(c.id)}">
    <td class="mono">${esc(c.id)}</td><td>${esc(c.title)}</td><td><span class="pill ${esc(c.status)}">${esc(c.status)}</span></td>
    <td>${esc(c.change_type)}</td><td>${esc(c.risk)}</td><td>${c.problem ? esc(c.problem) : "-"}</td></tr>`).join("");
  document.getElementById("chg-body").innerHTML =
    `<table class="itil"><thead><tr><th>ID</th><th>Title</th><th>Status</th><th>Type</th><th>Risk</th><th>Problem</th></tr></thead><tbody>${rows}</tbody></table>`;
  wireRows("chg-body");
}

function wireRows(bodyId) {
  document.querySelectorAll(`#${bodyId} tr[data-rec]`).forEach((tr) => {
    tr.addEventListener("click", () => { const [k, id] = tr.dataset.rec.split(":"); openRecord(k, id); });
  });
}

// ---- record detail panel ----
async function openRecord(kind, id) {
  const panel = document.getElementById("panel");
  panel.classList.add("open"); panel.setAttribute("aria-hidden", "false");
  document.getElementById("overlay").classList.add("open");
  panel.innerHTML = `<div class="psec"><div class="st">Loading ${esc(id)}…</div></div>`;
  try {
    const d = await getJSON(`/api/itil/record/${kind}/${encodeURIComponent(id)}`);
    if (d.error) { panel.innerHTML = `<div class="psec">${esc(d.error)}</div>`; return; }
    renderRecord(panel, d);
  } catch (e) { panel.innerHTML = `<div class="psec">${esc(e.message)}</div>`; }
}

function renderRecord(panel, d) {
  const r = d.record;
  const kindCls = { incident: "incident", problem: "problem", change: "change" }[d.kind];
  let stepper = "";
  if (d.kind === "change") {
    const idx = CHANGE_STEPS.indexOf(r.status);
    const failed = r.status === "failed" || r.status === "rejected";
    stepper = `<div class="stepper">${CHANGE_STEPS.map((s, i) => {
      const cls = failed ? "" : i < idx ? "done" : i === idx ? "cur" : "";
      return `<span class="step ${cls}">${s}</span>${i < CHANGE_STEPS.length - 1 ? '<span class="steparr">›</span>' : ""}`;
    }).join("")}${failed ? `<span class="steparr">›</span><span class="step fail">${esc(r.status)}</span>` : ""}</div>`;
  }
  const lineage = (d.lineage && d.lineage.length > 1) ? `<div class="psec"><div class="st">Lineage</div><div class="lineage">${
    d.lineage.map((n, i) => `${i ? '<span class="larrow">→</span>' : ""}<div class="lnode ${n.kind.slice(0,3)}" data-rec="${esc(n.kind)}:${esc(n.id)}"><div class="k">${esc(n.kind)} · ${esc(n.state)}</div><div class="v">${esc(n.id)}</div></div>`).join("")
  }</div></div>` : "";
  const tl = (d.timeline || []).slice().reverse().map((e) =>
    `<div class="aitem"><span class="atype ${esc(e.action || "event")}">${esc((e.action || "").replace("_", " "))}</span>
     <span class="abody"><span class="w">${esc(e.agent || "")}</span> ${esc(e.note || "")}</span><span class="atime">${esc(timeShort(e.ts))}</span></div>`).join("");

  panel.innerHTML = `
    <div class="phead"><div class="pkind"><span class="kbadge ${kindCls}">${esc(d.kind)}</span>
      <span class="kid mono">#${esc(r.id)}</span><button class="pclose">×</button></div>
      <div class="ptitle">${esc(r.title)}</div></div>
    ${stepper ? `<div class="psec"><div class="st">Lifecycle</div>${stepper}</div>` : ""}
    <div class="psec"><div class="st">Details</div>
      ${d.kind === "incident" ? `<div class="notes">severity <b>${esc(r.severity)}</b> · status <b>${esc(r.status)}</b> · services ${esc((r.affected_services||[]).join(", ") || "-")}${r.impact ? "<br>impact: " + esc(r.impact) : ""}${r.resolution_summary ? "<br>resolution: " + esc(r.resolution_summary) : ""}</div>` : ""}
      ${d.kind === "problem" ? `<div class="notes">status <b>${esc(r.status)}</b>${r.root_cause ? "<br>root cause: " + esc(r.root_cause) : ""}${r.workaround ? "<br>workaround: " + esc(r.workaround) : ""}</div>` : ""}
      ${d.kind === "change" ? `<div class="notes">type <b>${esc(r.change_type)}</b> · risk <b>${esc(r.risk)}</b>${r.rollback_plan ? "<br>rollback: " + esc(r.rollback_plan) : ""}</div>` : ""}
    </div>
    ${lineage}
    <div class="psec"><div class="st">Timeline · ${(d.timeline||[]).length} entries</div><div class="act">${tl || '<span style="color:var(--ink3);font-size:11px">no entries</span>'}</div></div>
    ${renderAIComposer((r.meta && r.meta.agent_run) || null)}`;
  panel.querySelector(".pclose").addEventListener("click", closePanel);
  panel.querySelectorAll(".lnode[data-rec]").forEach((n) => n.addEventListener("click", () => { const [k, id] = n.dataset.rec.split(":"); openRecord(k, id); }));
  wireAIComposer(panel, r.id, () => openRecord(d.kind, r.id));
}

function closePanel() {
  document.getElementById("panel").classList.remove("open");
  document.getElementById("panel").setAttribute("aria-hidden", "true");
  document.getElementById("overlay").classList.remove("open");
}

// ---- wiring ----
document.getElementById("overlay").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });

const discLoaders = { incidents: loadIncidents, problems: loadProblems, changes: loadChanges };
const discLoaded = {};
document.querySelectorAll(".disc button").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll(".disc button").forEach((x) => x.classList.remove("on"));
  document.querySelectorAll(".disc-view").forEach((x) => x.classList.remove("on"));
  b.classList.add("on");
  document.getElementById("disc-" + b.dataset.disc).classList.add("on");
  if (!discLoaded[b.dataset.disc]) { discLoaded[b.dataset.disc] = true; discLoaders[b.dataset.disc](); }
}));

function connectSSE() {
  const dot = document.getElementById("live-dot"), text = document.getElementById("live-text");
  let deb = null;
  const es = new EventSource("/api/events");
  const refresh = () => { clearTimeout(deb); deb = setTimeout(() => { loadOverview(); Object.keys(discLoaded).forEach((k) => discLoaders[k]()); }, 400); };
  es.addEventListener("open", () => { dot.classList.add("on"); text.textContent = "live"; });
  es.addEventListener("board_changed", refresh);
  es.addEventListener("itil_changed", refresh);
  es.addEventListener("error", () => { dot.classList.remove("on"); text.textContent = "reconnecting"; });
}

loadOverview();
loadIncidents(); discLoaded.incidents = true;
connectSSE();
