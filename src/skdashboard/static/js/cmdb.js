// CMDB view: CIs by type + health, CI detail panel with relationships + impact.
import { esc, getJSON, toast, authHeaders } from "./api.js";
import { createDetailPanel, wireDetailControl } from "./detail_panel.js";

const TYPE_IC = { service: "⚙️", host: "🖥️", agent: "🤖", credential: "🔑", port: "🔌", datastore: "🗄️", network: "🌐" };
const detailPanel = createDetailPanel(document.getElementById("panel"), document.getElementById("overlay"));

async function load() {
  let d;
  try { d = await getJSON("/api/cmdb/overview"); }
  catch (e) { document.getElementById("cmdb-body").innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`; return; }
  renderHealth(d);
  renderSyncthing(d);
  renderCoverage(d);
  const body = document.getElementById("cmdb-body");
  if (!d.total) {
    body.innerHTML = `<div class="emptymsg">No configuration items yet.<br>Plan discovery, review the result, then apply the validated local scope.</div>`;
    return;
  }
  body.innerHTML = d.types.map((g) => `
    <div class="ci-group">
      <h2>${TYPE_IC[g.type] || "•"} ${esc(g.type)}s <span class="ct">${g.items.length}</span></h2>
      <div class="ci-grid">${g.items.map((c) => `
        <button class="ci s-${esc(c.status)}" type="button" data-ci="${esc(c.id)}">
          <div class="cn">${esc(c.name)}</div>
          <div class="cm"><span class="cstat ${esc(c.status)}">${esc(c.status)}</span>
            ${c.node ? `<span>on ${esc(c.node)}</span>` : ""}${c.rels ? `<span>${c.rels} rel</span>` : ""}</div>
        </button>`).join("")}</div>
    </div>`).join("");
  body.querySelectorAll(".ci").forEach((el) => wireDetailControl(el, () => openCI(el.dataset.ci)));
}

function searchParams() {
  const form = new FormData(document.getElementById("cmdb-search"));
  const params = new URLSearchParams({ limit: "100" });
  for (const [key, value] of form.entries()) if (String(value).trim()) params.set(key, String(value).trim());
  return params;
}

async function searchCMDB() {
  const body = document.getElementById("cmdb-body");
  const params = searchParams();
  if ([...params.keys()].every((key) => key === "limit")) { await load(); return; }
  body.innerHTML = `<div class="emptymsg">Searching CMDB…</div>`;
  try {
    const d = await getJSON(`/api/cmdb/search?${params.toString()}`);
    if (!d.total) {
      body.innerHTML = `<div class="emptymsg">No configuration items match the current search and filters.</div>`;
      return;
    }
    body.innerHTML = `<div class="ci-group"><h2>Search results <span class="ct">${d.total}</span></h2>
      <div class="ci-grid">${d.items.map((c) => `
        <button class="ci s-${esc(c.status)}" type="button" data-ci="${esc(c.id)}">
          <div class="cn">${esc(c.name)}</div>
          <div class="cm"><span class="cstat ${esc(c.status)}">${esc(c.status)}</span>
            <span>${esc(c.type)}</span>${c.node ? `<span>on ${esc(c.node)}</span>` : ""}
            <span>${esc(c.staleness)}</span>${c.owner ? `<span>${esc(c.owner)}</span>` : ""}</div>
        </button>`).join("")}</div></div>`;
    body.querySelectorAll(".ci").forEach((el) => wireDetailControl(el, () => openCI(el.dataset.ci)));
  } catch (e) { body.innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`; }
}

function renderSyncthing(d) {
  const el = document.getElementById("cmdb-syncthing");
  const sync = d.syncthing || {};
  const nodes = sync.nodes || [];
  if (!nodes.length) {
    el.innerHTML = `<div class="sync-empty">No current node-qualified Syncthing evidence is recorded.</div>`;
    return;
  }
  const states = sync.states || {};
  el.innerHTML = `<div class="sync-head"><div><strong>Syncthing fleet</strong><span>${nodes.length} nodes · ${states.healthy || 0} healthy · ${states.syncing || 0} syncing · ${states.degraded || 0} degraded · ${states.down || 0} down</span></div>
    <div>${sync.pending_items || 0} pending · ${sync.pull_errors || 0} pull errors · ${sync.system_errors || 0} system errors</div></div>
    <div class="sync-grid">${nodes.map((node) => `
      <button class="sync-node state-${esc(node.health)}" type="button" data-ci="${esc(node.id)}">
        <span class="sync-name">${esc(node.node)}</span><span class="sync-state">${esc(node.health)}</span>
        <span>${node.folder_count} folders · ${node.connected_devices} peers</span>
        <span>${node.pending_items} pending · ${node.pull_errors} errors</span>
        <span class="sync-evidence">${esc(node.freshness)} evidence · ${node.evidence_age_seconds == null ? "age unknown" : `${node.evidence_age_seconds}s old`}</span>
      </button>`).join("")}</div>`;
  el.querySelectorAll("[data-ci]").forEach((node) => wireDetailControl(node, () => openCI(node.dataset.ci)));
}

function renderHealth(d) {
  const h = d.health || {};
  const e = d.evidence_health || {};
  const coverage = d.coverage || {};
  const el = document.getElementById("cmdb-health");
  const tile = (label, n, cls) => `<div class="kpi"><div class="l">${label}</div><div class="n${cls ? " " + cls : ""}">${n || 0}</div></div>`;
  el.innerHTML =
    tile("Total CIs", d.total) +
    tile("Operational", h.operational) +
    `<div class="kpi${h.degraded ? " alert" : ""}"><div class="l">Degraded</div><div class="n" style="${h.degraded ? "color:var(--high)" : ""}">${h.degraded || 0}</div></div>` +
    `<div class="kpi${h.down ? " alert" : ""}"><div class="l">Down</div><div class="n">${h.down || 0}</div></div>` +
    tile("Stale", e.stale) +
    tile("Unreachable", e.unreachable, e.unreachable ? "risk" : "") +
    tile("Coverage", `${coverage.coverage_percent || 0}%`);
}

function renderCoverage(d) {
  const el = document.getElementById("cmdb-coverage");
  const coverage = d.coverage || {};
  const history = d.reconciliation_history || [];
  if (!coverage.scan_id && !history.length) {
    el.innerHTML = `<div class="coverage-empty">No verified reconciliation run has been recorded.</div>`;
    return;
  }
  const nodes = (coverage.nodes || []).map((node) => `
    <div class="coverage-node ${node.complete ? "complete" : "partial"}">
      <div><strong>${esc(node.node)}</strong><span>${node.coverage_percent}% complete</span></div>
      <div class="coverage-meta">${node.collectors_complete}/${node.collectors_expected} collectors · ${node.findings} findings${node.failures.length ? ` · ${node.failures.length} failure` : ""}</div>
      <div class="collector-list">${(node.collectors || []).map((collector) => `<span class="collector ${esc(collector.status)}">${esc(collector.collector)}: ${esc(collector.status)}</span>`).join("")}</div>
      <div class="coverage-source">${node.provenance.map(esc).join(", ") || "source unknown"}</div>
    </div>`).join("");
  const runs = history.slice(0, 5).map((run) => `
    <div class="run-row"><span class="run-state ${run.complete ? "ok" : "partial"}">${run.complete ? "complete" : "partial"}</span>
      <span class="mono">${esc(run.scan_id || "unknown")}</span><span>${esc(run.ended_at || "time unknown")}</span>
      <span>${run.drift} drift</span><span>${run.applied ? "applied" : "preview"}</span></div>`).join("");
  el.innerHTML = `<details><summary>Coverage and reconciliation history <span>${esc(d.last_successful_reconciliation || "no successful run")}</span></summary>
    <div class="coverage-grid">${nodes || '<div class="coverage-empty">No node coverage in the latest run.</div>'}</div>
    <div class="run-list">${runs}</div></details>`;
}

function renderActionState(d) {
  const el = document.getElementById("cmdb-action-state");
  const authorization = d.authorization || {};
  el.innerHTML = `<strong>${esc(d.preview ? "Preview" : "Apply")}: ${esc(d.execution_state || "unknown")}</strong>
    <span>${authorization.authorized ? "authorized" : "not authorized"} via ${esc(authorization.via || "unknown")}</span>
    <span>${esc(authorization.reason || "no authorization reason")}</span>`;
}

async function openCI(ciId) {
  const panel = document.getElementById("panel");
  detailPanel.open("Loading…");
  try {
    const d = await getJSON(`/api/cmdb/ci/${encodeURIComponent(ciId)}`);
    if (d.error) {
      panel.innerHTML = `<div class="psec" tabindex="-1" role="alert">${esc(d.error)}</div>`;
      detailPanel.focusFirst();
      return;
    }
    renderCI(panel, d);
    detailPanel.focusFirst();
  } catch (e) {
    panel.innerHTML = `<div class="psec" tabindex="-1" role="alert">${esc(e.message)}</div>`;
    detailPanel.focusFirst();
  }
}

function renderCI(panel, d) {
  const ci = d.ci;
  const attrs = Object.entries(ci.attributes || {}).map(([k, v]) =>
    `<div class="attr"><span class="ak">${esc(k)}</span><span>${esc(String(v))}</span></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">none</span>';
  const rels = (d.relationships || []).map((r) =>
    `<div class="relrow"><span class="reltag">${esc(r.rel_type)}</span><button class="detail-link" type="button" data-ci="${esc(r.target)}">${esc(r.target_name)}</button></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">none</span>';
  const deps = (d.dependents || []).map((x) =>
    `<div class="relrow"><span class="reltag">${esc(x.rel)}</span><button class="detail-link" type="button" data-ci="${esc(x.id)}">${esc(x.name)}</button></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">nothing depends on this</span>';
  const incs = (d.open_incidents || []).map((i) =>
    `<div class="impact-inc"><span class="sev" style="background:var(--${i.severity})">${esc(i.severity.toUpperCase())}</span>
      <span style="flex:1">${esc(i.title)}</span><span class="pill ${esc(i.status)}">${esc(i.status)}</span></div>`).join("")
    || '<span style="color:var(--done);font-size:11px">no open incidents 🎉</span>';
  const provenance = Object.entries(d.provenance || {}).map(([k, v]) =>
    `<div class="attr"><span class="ak">${esc(k)}</span><span>${esc(Array.isArray(v) ? v.join(", ") : String(v ?? ""))}</span></div>`).join("");
  const endpoints = Object.entries(d.endpoints || {}).map(([k, v]) =>
    `<div class="attr"><span class="ak">${esc(k)}</span><span>${esc(String(v))}</span></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">none</span>';
  const health = (d.health_history || []).map((item) =>
    `<div class="relrow"><span class="cstat ${esc(item.status)}">${esc(item.status)}</span><span>${esc(item.at || "")}</span><span>${esc(item.note || "")}</span></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">no status transitions</span>';

  panel.innerHTML = `
    <div class="phead"><div class="pkind"><span class="kbadge task">${esc(ci.ci_type)}</span>
      <span class="kid mono">${esc(ci.id)}</span><button class="pclose" type="button" aria-label="Close details">×</button></div>
      <div class="ptitle">${esc(ci.name)} <span class="cstat ${esc(ci.status)}" style="font-size:9px;vertical-align:middle">${esc(ci.status)}</span></div></div>
    ${ci.description ? `<div class="psec"><div class="notes">${esc(ci.description)}</div></div>` : ""}
    <div class="psec"><div class="st">Ownership and last seen</div><div class="attr"><span class="ak">owner</span><span>${esc(d.owner)}</span></div><div class="attr"><span class="ak">last seen</span><span>${esc(d.last_seen || "unknown")}</span></div></div>
    <div class="psec"><div class="st">Provenance</div>${provenance}</div>
    <div class="psec"><div class="st">Endpoints</div>${endpoints}</div>
    <div class="psec"><div class="st">Attributes</div>${attrs}</div>
    <div class="psec"><div class="st">Relationships</div>${rels}</div>
    <div class="psec"><div class="st">Impact: what depends on this</div>${deps}</div>
    <div class="psec"><div class="st">Open incidents affecting this</div>${incs}</div>`;
  panel.innerHTML += `<div class="psec"><div class="st">Health history</div>${health}</div>`;
  panel.querySelector(".pclose").addEventListener("click", closePanel);
  panel.querySelectorAll("[data-ci]").forEach((n) => wireDetailControl(n, () => openCI(n.dataset.ci)));
}

const closePanel = detailPanel.close;

document.getElementById("cmdb-search").addEventListener("submit", (e) => {
  e.preventDefault();
  searchCMDB();
});
document.getElementById("cmdb-clear").addEventListener("click", () => {
  document.getElementById("cmdb-search").reset();
  load();
});
document.getElementById("btn-plan").addEventListener("click", async () => {
  try {
    const d = await getJSON("/api/cmdb/plan");
    renderActionState(d);
    const auth = d.authorization.authorized ? "apply authorized" : "apply not authorized";
    toast(`preview only · ${d.counts.created} create · ${d.counts.updated} update · ${auth}`);
  } catch (e) { toast(e.message, true); }
});
document.getElementById("btn-apply").addEventListener("click", async () => {
  if (!window.confirm("Apply the validated declared + local CMDB discovery plan?")) return;
  try {
    const r = await fetch("/api/cmdb/apply", { method: "POST", headers: await authHeaders() });
    const d = await r.json();
    if (!r.ok || !d.applied) throw new Error(d.error || "CMDB apply was refused");
    toast(`${d.execution_state} · ${d.counts.created} create · ${d.counts.updated} update`);
    await load();
    renderActionState(d);
  } catch (e) { toast(e.message, true); }
});

load();
