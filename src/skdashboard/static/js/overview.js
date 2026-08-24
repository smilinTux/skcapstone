// Overview home: operational summary tiles + active work + recent activity +
// agent health, from one /api/overview call. Live-refreshes over SSE.
import { esc, getJSON, timeShort, avatarColor } from "./api.js";
import { openCard, initPanel } from "./editor.js";

const IS_ID = (s) => /^(inc-|prb-|chg-|[0-9a-f]{6,})/i.test(s || "");

const SEV_VAR = { sev1: "sev1", sev2: "sev2", sev3: "sev3", sev4: "sev4" };

async function load() {
  loadQuality();
  let d;
  try { d = await getJSON("/api/overview"); }
  catch (e) { document.getElementById("tiles").innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`; return; }
  renderTiles(d);
  renderActive(d.active_tasks || []);
  renderActivity(d.activity || []);
  renderHealth(d.agent || {});
}

const QUALITY_ICON = {
  current: "✓", stale: "◷", partial: "◐", unavailable: "!",
  unreachable: "×", unknown: "?", not_applicable: "○",
};

function coverageText(coverage) {
  if (!coverage || coverage.percent == null) return "Coverage unavailable";
  if (coverage.population === "declared_sources") {
    return `${coverage.reporting} of ${coverage.expected} sources observed (${coverage.percent}%)`;
  }
  return `${coverage.reporting} of ${coverage.expected} reporting (${coverage.percent}%)`;
}

async function loadQuality() {
  const summary = document.getElementById("quality-summary");
  const issues = document.getElementById("quality-issues");
  try {
    const response = await getJSON("/api/v1/overview");
    const quality = response.items.find((item) => item.projection_type === "data_quality");
    if (!quality) throw new Error("Data-quality projection unavailable");
    renderQuality(quality);
  } catch (error) {
    summary.innerHTML = `<span class="truth-badge unavailable"><b>!</b> Unavailable</span><span>${esc(error.message)}</span>`;
    issues.innerHTML = `<p class="quality-empty">Data-quality evidence could not be observed. No source is assumed healthy.</p>`;
  }
}

function renderQuality(quality) {
  const summary = document.getElementById("quality-summary");
  const issues = document.getElementById("quality-issues");
  const states = ["current", "stale", "partial", "unavailable", "unreachable", "unknown", "not_applicable"];
  const labels = { not_applicable: "not applicable" };
  summary.innerHTML = `<div class="quality-coverage">
      <strong>${esc(coverageText(quality.coverage))}</strong>
      <span>${quality.source_count} sources · ${quality.metric_registry.definition_count} metric definitions · registry ${esc(quality.metric_registry.registry_version)}</span>
    </div>
    <div class="truth-counts" aria-label="Truth state counts">${states.map((state) =>
      `<span class="truth-badge ${state}"><b aria-hidden="true">${QUALITY_ICON[state]}</b>${esc(labels[state] || state)} ${quality.state_counts[state]}</span>`
    ).join("")}</div>`;
  issues.innerHTML = quality.issues.length ? quality.issues.map((issue) => {
    const watermark = issue.watermark && issue.watermark.value ? issue.watermark.value : "Unavailable";
    const observed = issue.last_observation || "Not observed";
    const reason = issue.safe_provenance.map((item) => `${item.code}: ${item.message}`).join("; ");
    return `<article class="quality-issue" id="quality-source-${esc(issue.source.adapter_id)}">
      <div class="quality-issue-title">
        <span class="truth-badge ${esc(issue.truth_state)}"><b aria-hidden="true">${QUALITY_ICON[issue.truth_state]}</b>${esc(issue.truth_state)}</span>
        <strong>${esc(issue.owner)}</strong>
      </div>
      <dl>
        <div><dt>Source</dt><dd class="mono">${esc(issue.source.adapter_id)}@${esc(issue.source.adapter_version)}</dd></div>
        <div><dt>Coverage</dt><dd>${esc(coverageText(issue.coverage))}</dd></div>
        <div><dt>Watermark</dt><dd class="mono">${esc(watermark)}</dd></div>
        <div><dt>Last observation</dt><dd>${esc(observed)}</dd></div>
        <div><dt>Safe provenance</dt><dd>${esc(reason)}</dd></div>
      </dl>
      <button class="quality-preview-button" data-issue="${esc(issue.issue_id)}">${esc(issue.safe_next_step.label)}</button>
    </article>`;
  }).join("") : `<p class="quality-empty">No reconciliation issues are visible.</p>`;
  issues.querySelectorAll(".quality-preview-button").forEach((button) => button.addEventListener("click", () => {
    const issue = quality.issues.find((candidate) => candidate.issue_id === button.dataset.issue);
    openQualityPreview(issue);
  }));
}

function openQualityPreview(issue) {
  const dialog = document.getElementById("quality-preview");
  document.getElementById("quality-preview-body").innerHTML = `<dl>
    <div><dt>Owner</dt><dd>${esc(issue.owner)}</dd></div>
    <div><dt>Source</dt><dd class="mono">${esc(issue.source.adapter_id)}</dd></div>
    <div><dt>Current truth</dt><dd>${esc(issue.truth_state)}</dd></div>
    <div><dt>Required check</dt><dd>Re-read the bounded aggregate and compare its next watermark.</dd></div>
  </dl>`;
  dialog.showModal();
}

function renderTiles(d) {
  const k = d.kanban || {}, itil = (d.itil || {}), kp = itil.kpis || {}, cm = d.cmdb || {};
  const itilAvailable = itil.available === true;
  const cmdbAvailable = cm.available === true;
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
    <a class="tile ${itilAvailable && (kp.sev1 || kp.sev2) ? "alert" : ""}" href="/cockpit">
      <div class="th"><span class="ic">🚨</span> Incidents</div>
      <div class="tn">${itilAvailable ? kp.open_incidents : "Unknown"} <small>${itilAvailable ? "open" : "source unavailable"}</small></div>
      <div class="tsub">${itilAvailable && sev ? `<span class="chip crit">${esc(sev)}</span>` : ""}
        ${itil.breaches ? `<span class="chip warn">${itil.breaches} past SLA</span>` : ""}</div>
    </a>
    <a class="tile" href="/cockpit">
      <div class="th"><span class="ic">🔁</span> Change / SLA</div>
      <div class="tn mono">${itilAvailable ? esc(kp.mttr || "-") : "Unknown"} <small>${itilAvailable ? "MTTR" : "source unavailable"}</small></div>
      <div class="tsub">${itilAvailable ? `MTTA ${esc(kp.mtta || "-")}` : "ITIL evidence unavailable"} ${itil.cab ? `<span class="chip warn">${itil.cab} awaiting CAB</span>` : ""}</div>
    </a>
    <a class="tile ${cmdbAvailable && health.down ? "alert" : ""}" href="/cmdb">
      <div class="th"><span class="ic">🖥️</span> Assets</div>
      <div class="tn">${cmdbAvailable ? cm.total : "Unknown"} <small>${cmdbAvailable ? "CIs" : "source unavailable"}</small></div>
      <div class="tsub">${cmdbAvailable && health.down ? `<span class="chip crit">${health.down} down</span>` : ""}
        ${!cmdbAvailable ? `<span class="chip warn">health unknown</span>` : health.degraded ? `<span class="chip warn">${health.degraded} degraded</span>` : `<span class="chip ok">all healthy</span>`}</div>
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
  el.querySelectorAll(".at-item").forEach((n) => n.addEventListener("click", () => openCard(n.dataset.id)));
}

function renderActivity(list) {
  const icon = { escalated: "🔴", resolved: "✅", acknowledged: "👀", created: "🆕", voted: "🗳️", deployed: "🚀", verified: "✅" };
  const el = document.getElementById("activity");
  el.innerHTML = list.length
    ? list.map((e) => `<div class="fitem${IS_ID(e.record) ? " clickable" : ""}" data-rec="${esc(e.record || "")}"><span class="ftime">${esc(timeShort(e.ts))}</span>
        <span class="fic">${icon[e.action] || "•"}</span>
        <span class="fbody"><span class="w">${esc(e.record || "")}</span> ${esc(e.action || "")}${e.note ? " · " + esc((e.note || "").slice(0, 60)) : ""}</span></div>`).join("")
    : `<div style="color:var(--ink3);font-size:12px">No recent activity</div>`;
  el.querySelectorAll(".fitem.clickable").forEach((n) => n.addEventListener("click", () => openCard(n.dataset.rec)));
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

initPanel(() => load());   // card detail panel (edit/notes/AI); reload on change
load();
connectSSE();
setInterval(load, 30000);
