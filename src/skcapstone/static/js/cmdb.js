// CMDB view: CIs by type + health, CI detail panel with relationships + impact.
import { esc, getJSON, toast } from "./api.js";

const TYPE_IC = { service: "⚙️", host: "🖥️", agent: "🤖", credential: "🔑", port: "🔌", datastore: "🗄️", network: "🌐" };

async function load() {
  let d;
  try { d = await getJSON("/api/cmdb/overview"); }
  catch (e) { document.getElementById("cmdb-body").innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`; return; }
  renderHealth(d);
  const body = document.getElementById("cmdb-body");
  if (!d.total) {
    body.innerHTML = `<div class="emptymsg">No configuration items yet.<br>Click <b>Seed from inventory</b> to populate CIs from the fleet + ITIL data.</div>`;
    return;
  }
  body.innerHTML = d.types.map((g) => `
    <div class="ci-group">
      <h2>${TYPE_IC[g.type] || "•"} ${esc(g.type)}s <span class="ct">${g.items.length}</span></h2>
      <div class="ci-grid">${g.items.map((c) => `
        <div class="ci s-${esc(c.status)}" data-ci="${esc(c.id)}">
          <div class="cn">${esc(c.name)}</div>
          <div class="cm"><span class="cstat ${esc(c.status)}">${esc(c.status)}</span>
            ${c.node ? `<span>on ${esc(c.node)}</span>` : ""}${c.rels ? `<span>${c.rels} rel</span>` : ""}</div>
        </div>`).join("")}</div>
    </div>`).join("");
  body.querySelectorAll(".ci").forEach((el) => el.addEventListener("click", () => openCI(el.dataset.ci)));
}

function renderHealth(d) {
  const h = d.health || {};
  const el = document.getElementById("cmdb-health");
  const tile = (label, n, cls) => `<div class="kpi"><div class="l">${label}</div><div class="n${cls ? " " + cls : ""}">${n || 0}</div></div>`;
  el.innerHTML =
    tile("Total CIs", d.total) +
    tile("Operational", h.operational) +
    `<div class="kpi${h.degraded ? " alert" : ""}"><div class="l">Degraded</div><div class="n" style="${h.degraded ? "color:var(--high)" : ""}">${h.degraded || 0}</div></div>` +
    `<div class="kpi${h.down ? " alert" : ""}"><div class="l">Down</div><div class="n">${h.down || 0}</div></div>` +
    tile("Retired", h.retired);
}

async function openCI(ciId) {
  const panel = document.getElementById("panel");
  panel.classList.add("open"); panel.setAttribute("aria-hidden", "false");
  document.getElementById("overlay").classList.add("open");
  panel.innerHTML = `<div class="psec"><div class="st">Loading…</div></div>`;
  try {
    const d = await getJSON(`/api/cmdb/ci/${encodeURIComponent(ciId)}`);
    if (d.error) { panel.innerHTML = `<div class="psec">${esc(d.error)}</div>`; return; }
    renderCI(panel, d);
  } catch (e) { panel.innerHTML = `<div class="psec">${esc(e.message)}</div>`; }
}

function renderCI(panel, d) {
  const ci = d.ci;
  const attrs = Object.entries(ci.attributes || {}).map(([k, v]) =>
    `<div class="attr"><span class="ak">${esc(k)}</span><span>${esc(String(v))}</span></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">none</span>';
  const rels = (d.relationships || []).map((r) =>
    `<div class="relrow"><span class="reltag">${esc(r.rel_type)}</span><span data-ci="${esc(r.target)}" style="cursor:pointer;color:var(--accent)">${esc(r.target_name)}</span></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">none</span>';
  const deps = (d.dependents || []).map((x) =>
    `<div class="relrow"><span class="reltag">${esc(x.rel)}</span><span data-ci="${esc(x.id)}" style="cursor:pointer;color:var(--accent)">${esc(x.name)}</span></div>`).join("")
    || '<span style="color:var(--ink3);font-size:11px">nothing depends on this</span>';
  const incs = (d.open_incidents || []).map((i) =>
    `<div class="impact-inc"><span class="sev" style="background:var(--${i.severity})">${esc(i.severity.toUpperCase())}</span>
      <span style="flex:1">${esc(i.title)}</span><span class="pill ${esc(i.status)}">${esc(i.status)}</span></div>`).join("")
    || '<span style="color:var(--done);font-size:11px">no open incidents 🎉</span>';

  panel.innerHTML = `
    <div class="phead"><div class="pkind"><span class="kbadge task">${esc(ci.ci_type)}</span>
      <span class="kid mono">${esc(ci.id)}</span><button class="pclose">×</button></div>
      <div class="ptitle">${esc(ci.name)} <span class="cstat ${esc(ci.status)}" style="font-size:9px;vertical-align:middle">${esc(ci.status)}</span></div></div>
    ${ci.description ? `<div class="psec"><div class="notes">${esc(ci.description)}</div></div>` : ""}
    <div class="psec"><div class="st">Attributes</div>${attrs}</div>
    <div class="psec"><div class="st">Depends on / runs on</div>${rels}</div>
    <div class="psec"><div class="st">Impact — what depends on this</div>${deps}</div>
    <div class="psec"><div class="st">Open incidents affecting this</div>${incs}</div>`;
  panel.querySelector(".pclose").addEventListener("click", closePanel);
  panel.querySelectorAll("[data-ci]").forEach((n) => n.addEventListener("click", () => openCI(n.dataset.ci)));
}

function closePanel() {
  document.getElementById("panel").classList.remove("open");
  document.getElementById("panel").setAttribute("aria-hidden", "true");
  document.getElementById("overlay").classList.remove("open");
}

document.getElementById("overlay").addEventListener("click", closePanel);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
document.getElementById("btn-seed").addEventListener("click", async () => {
  try {
    const r = await fetch("/api/cmdb/seed", { method: "POST" });
    const d = await r.json();
    toast(`seeded · ${d.cis} CIs`);
    load();
  } catch (e) { toast(e.message, true); }
});

load();
