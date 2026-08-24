// Overview home: operational summary tiles + active work + recent activity +
// agent health, from one /api/overview call. Live-refreshes over SSE.
import { esc, getJSON, timeShort, avatarColor } from "./api.js";
import { openCard, initPanel } from "./editor.js";

const IS_ID = (s) => /^(inc-|prb-|chg-|[0-9a-f]{6,})/i.test(s || "");

const SEV_VAR = { sev1: "sev1", sev2: "sev2", sev3: "sev3", sev4: "sev4" };

let loadEpoch = 0;

async function load() {
  const epoch = ++loadEpoch;
  const protectedReady = await loadQuality(epoch);
  if (protectedReady !== true) return;
  let d;
  try { d = await getJSON("/api/overview"); }
  catch (e) {
    if (epoch === loadEpoch) clearLegacyOverview(`Legacy overview unavailable: ${e.message}`);
    return;
  }
  if (epoch !== loadEpoch) return;
  renderTiles(d);
  renderActive(d.active_tasks || []);
  renderActivity(d.activity || []);
  renderHealth(d.agent || {});
}

const QUALITY_ICON = {
  current: "✓", stale: "◷", partial: "◐", unavailable: "!",
  unreachable: "×", unknown: "?", not_applicable: "○",
};

const ESTATE_SILOS = [
  { id: "portfolio", label: "Portfolio and projects", adapters: ["skcapstone.portfolio"], metric: "portfolio.blocked_objectives@1.0.0" },
  { id: "flow", label: "Agile flow", adapters: ["skcoord.flow", "skcoord.agent_presence"], metric: "flow.review_coverage@1.0.0" },
  { id: "itil", label: "ITIL and SRE", adapters: ["skcapstone.itil"], metric: "itil.change_classification_coverage@1.0.0" },
  { id: "delivery", label: "Engineering delivery", adapters: ["skcapstone.service_release"], metric: "engineering.delivery_signals_current@1.0.0" },
  { id: "architecture", label: "Architecture and CMDB", adapters: ["cmdb.configuration"], metric: "architecture.drift_signals@1.0.0" },
  { id: "fleet", label: "Fleet runtime", adapters: ["skcapstone.fleet"], metric: "fleet.reporting_nodes@1.0.0" },
  { id: "ai", label: "AI and models", adapters: ["skcounter.harness", "skgateway.observed"], metric: "ai.accepted_outcome_rate@1.0.0" },
  { id: "economy", label: "Economy", adapters: ["skperf.aggregate", "skjoule.wallet"], metric: "economy.cost_per_accepted_outcome@1.0.0", metricSource: "skcounter.harness" },
  { id: "governance", label: "Governance and data quality", adapters: ["capauth.policy"], metric: "governance.definition_coverage@1.0.0" },
  { id: "legal", label: "Legal program", adapters: ["sklegal.global"], metric: "legal.global_program_status@1.0.0" },
  { id: "corpus", label: "Corpus pipeline", adapters: ["hammertime.pipeline"], metric: "corpus.approved_release_health@1.0.0" },
  { id: "operator", label: "Operator and shell", adapters: ["atlas.conditions", "skos.discovery"], metric: "operator.ready_condition_forecast@1.0.0" },
];

const STATE_ORDER = { unavailable: 0, unreachable: 1, unknown: 2, partial: 3, stale: 4, not_applicable: 5, current: 6 };
let estateEvidence = new Map();

function initializeContext() {
  const query = new URLSearchParams(location.search);
  const role = ["operator", "project-manager", "architect"].includes(query.get("role")) ? query.get("role") : "operator";
  document.getElementById("now-role").value = role;
  const writeUrl = () => {
    const url = new URL(location.href);
    url.pathname = "/control-plane/now";
    url.search = new URLSearchParams({
      role: document.getElementById("now-role").value,
      scope: "estate", window: "latest", baseline: "none", service: "all",
    }).toString();
    history.replaceState({}, "", url);
  };
  document.getElementById("now-context").addEventListener("change", () => {
    writeUrl();
    load();
  });
  writeUrl();
}

function combinedState(items) {
  const states = [...new Set(items.map((item) => item.truth_state))];
  if (states.length === 1) return states[0];
  if (states.includes("partial")) return "partial";
  if (states.includes("stale") && states.every((state) => ["current", "stale", "not_applicable"].includes(state))) return "stale";
  if (states.some((state) => ["current", "stale"].includes(state)) && states.some((state) => STATE_ORDER[state] <= STATE_ORDER.unknown)) return "partial";
  return states.reduce((worst, state) => STATE_ORDER[state] < STATE_ORDER[worst] ? state : worst, "current");
}

function aggregateValue(item, key) {
  const value = item && item.aggregate && item.aggregate[key];
  return value == null ? "Unknown" : String(value);
}

function signalFor(id, items) {
  const first = items[0];
  const second = items[1];
  const signals = {
    portfolio: () => `${aggregateValue(first, "open")} open, ${aggregateValue(first, "in_progress")} in progress, ${aggregateValue(first, "done")} done`,
    flow: () => `${aggregateValue(first, "blocked")} blocked, ${aggregateValue(first, "in_progress")} in progress, ${aggregateValue(second, "active_agents")} active agents`,
    itil: () => `${aggregateValue(first, "open_incidents")} open incidents, SEV1 ${aggregateValue(first, "sev1")}, SEV2 ${aggregateValue(first, "sev2")}, ${aggregateValue(first, "awaiting_cab")} awaiting CAB`,
    delivery: () => `${aggregateValue(first, "services")} services, ${aggregateValue(first, "releases")} release observations`,
    architecture: () => `${aggregateValue(first, "total")} CIs, ${aggregateValue(first, "degraded")} degraded, ${aggregateValue(first, "stale")} stale`,
    fleet: () => `${aggregateValue(first, "graded")} graded, ${aggregateValue(first, "error")} errors, ${aggregateValue(first, "warn")} warnings`,
    ai: () => `Harness ${aggregateValue(first, "observation_count")} observations; gateway ${aggregateValue(second, "observation_count")} observations`,
    economy: () => `${aggregateValue(first, "regressions")} performance regressions; ${aggregateValue(second, "total_supply")} Joule supply`,
    governance: () => `${aggregateValue(first, "denials")} policy denials; policy evidence ${aggregateValue(first, "available")}`,
    legal: () => first.aggregate ? `${aggregateValue(first, "matters")} matter-free aggregate records; deadline pressure ${aggregateValue(first, "deadline_pressure")}` : "Policy-filtered aggregate unavailable",
    corpus: () => `${aggregateValue(first, "approved_releases")} approved releases; ${aggregateValue(first, "pipeline_failures")} pipeline failures`,
    operator: () => `${aggregateValue(first, "open_conditions")} open conditions, ${aggregateValue(first, "ready_actions")} ready-action observations; ${aggregateValue(second, "discovered")} SKOS modules`,
  };
  return signals[id]();
}

function coverageFor(items) {
  return items.map((item) => {
    const coverage = item.coverage || {};
    const sample = Number.isInteger(coverage.reporting) && Number.isInteger(coverage.expected)
      ? `${coverage.reporting} of ${coverage.expected}` : "unavailable";
    return `${item.adapter_id} (${item.population}): ${sample}`;
  }).join("; ");
}

function renderEstate(items) {
  const observations = items.filter((item) => item.adapter_id);
  const byId = new Map(observations.map((item) => [item.adapter_id, item]));
  const complete = ESTATE_SILOS.every((silo) => silo.adapters.every((adapter) => byId.has(adapter)));
  const rows = document.getElementById("estate-rows");
  if (!complete || observations.length !== 16) {
    return false;
  }
  estateEvidence = new Map();
  rows.innerHTML = ESTATE_SILOS.map((silo) => {
    const sources = silo.adapters.map((adapter) => byId.get(adapter));
    const state = combinedState(sources);
    const owners = [...new Set(sources.map((item) => item.owner))].join(" + ");
    const visibility = sources.some((item) => item.visibility.state === "policy_filtered") ? "Policy filtered" : "Visible";
    const metricSource = silo.metricSource || silo.adapters[0];
    const metricSourceHere = silo.adapters.includes(metricSource);
    estateEvidence.set(silo.id, { ...silo, sources, state, owners, visibility, metricSource, metricSourceHere });
    return `<tr data-silo="${esc(silo.id)}" data-source-count="${sources.length}">
      <td><strong>${esc(silo.label)}</strong><small>Owner: ${esc(owners)}</small></td>
      <td><span class="truth-badge ${esc(state)}"><b aria-hidden="true">${QUALITY_ICON[state]}</b>${esc(state.replace("_", " "))}</span><small>${esc(visibility)}</small></td>
      <td><strong>${esc(signalFor(silo.id, sources))}</strong><small>Source aggregate only; no AI inference</small></td>
      <td><span class="mono">${esc(silo.metric)}</span><small>definition only; result not projected</small><small>scope estate; window latest; registry source ${esc(metricSource)}${metricSourceHere ? "" : "; source observation appears in another silo"}</small><small>${esc(coverageFor(sources))}</small></td>
      <td><strong>Unknown</strong><small>No comparable baseline is projected</small></td>
      <td><button class="quality-preview-button estate-evidence-button" type="button" data-silo="${esc(silo.id)}" aria-label="Evidence for ${esc(silo.label)}">Evidence</button></td>
    </tr>`;
  }).join("");
  document.getElementById("estate-count").textContent = "12 silos | 16 sources";
  rows.querySelectorAll(".estate-evidence-button").forEach((button) => button.addEventListener("click", () => openEstateEvidence(button.dataset.silo, button)));
  return true;
}

function openEstateEvidence(siloId, trigger) {
  const evidence = estateEvidence.get(siloId);
  if (!evidence) return;
  const sourceRows = evidence.sources.map((item) => `<tr><th scope="row" class="mono">${esc(item.adapter_id)}@${esc(item.adapter_version)}</th><td>${esc(item.truth_state)}</td><td>${esc(item.observed_at || "Not observed")}</td><td class="mono">${esc((item.watermark && item.watermark.value) || "Unavailable")}</td><td>${esc((item.errors || []).map((error) => `${error.code}: ${error.message}`).join("; ") || "None")}</td></tr>`).join("");
  document.getElementById("estate-evidence-title").textContent = `${evidence.label} evidence`;
  document.getElementById("estate-evidence-body").innerHTML = `<dl>
    <div><dt>Metric definition</dt><dd class="mono">${esc(evidence.metric)}</dd></div>
    <div><dt>Metric registry source</dt><dd class="mono">${esc(evidence.metricSource)}${evidence.metricSourceHere ? "" : "; projected under another silo, so no result is associated here"}</dd></div>
    <div><dt>Scope and window</dt><dd>Whole authorized estate; latest source observation; no comparable baseline</dd></div>
    <div><dt>Truth and visibility</dt><dd>${esc(evidence.state)}; ${esc(evidence.visibility)}</dd></div>
    <div><dt>Sample</dt><dd>${esc(coverageFor(evidence.sources))}</dd></div>
    <div><dt>Uncertainty</dt><dd>Material change and causality are not projected. Conflicting or missing source evidence remains unresolved.</dd></div>
  </dl><div class="estate-table-wrap"><table><caption>Source provenance</caption><thead><tr><th scope="col">Source</th><th scope="col">Truth</th><th scope="col">Observed</th><th scope="col">Watermark</th><th scope="col">Errors</th></tr></thead><tbody>${sourceRows}</tbody></table></div>`;
  const dialog = document.getElementById("estate-evidence");
  dialog._trigger = trigger;
  dialog.showModal();
}

function coverageText(coverage) {
  if (!coverage || coverage.percent == null) return "Coverage unavailable";
  if (coverage.population === "declared_sources") {
    return `${coverage.reporting} of ${coverage.expected} sources observed (${coverage.percent}%)`;
  }
  return `${coverage.reporting} of ${coverage.expected} reporting (${coverage.percent}%)`;
}

function clearLegacyOverview(message) {
  document.getElementById("tiles").innerHTML = `<div class="emptymsg">${esc(message)}</div>`;
  document.getElementById("active-tasks").innerHTML = `<div class="emptymsg">Unavailable</div>`;
  document.getElementById("activity").innerHTML = `<div class="emptymsg">Unavailable</div>`;
  document.getElementById("agent-health").innerHTML = `<div class="emptymsg">Unavailable</div>`;
}

function clearProtectedEstate(message) {
  estateEvidence = new Map();
  for (const id of ["estate-evidence", "quality-preview"]) {
    const dialog = document.getElementById(id);
    if (dialog.open) dialog.close();
  }
  document.getElementById("estate-evidence-title").textContent = "Estate evidence unavailable";
  document.getElementById("estate-evidence-body").replaceChildren();
  document.getElementById("quality-preview-body").replaceChildren();
  document.getElementById("estate-rows").innerHTML = `<tr><td colspan="6" class="quality-empty">${esc(message)} No silo is assumed healthy.</td></tr>`;
  document.getElementById("estate-count").textContent = "Unavailable";
  document.getElementById("quality-summary").innerHTML = `<span class="truth-badge unavailable"><b aria-hidden="true">!</b> Unavailable</span><span>${esc(message)}</span>`;
  document.getElementById("quality-issues").innerHTML = `<p class="quality-empty">Protected data-quality evidence is unavailable. No source is assumed healthy.</p>`;
  clearLegacyOverview("Protected estate evidence unavailable");
}

async function loadQuality(epoch) {
  try {
    const response = await getJSON("/api/v1/overview");
    if (epoch !== loadEpoch) return null;
    const quality = response.items.find((item) => item.projection_type === "data_quality");
    if (!quality) throw new Error("Data-quality projection unavailable");
    if (!renderEstate(response.items)) throw new Error("Expected 16 bounded adapter observations");
    renderQuality(quality);
    return true;
  } catch (error) {
    if (epoch !== loadEpoch) return null;
    clearProtectedEstate(`Protected estate evidence is unavailable: ${error.message}.`);
    return false;
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

initializeContext();
document.getElementById("ai-boundary-button").addEventListener("click", (event) => {
  const dialog = document.getElementById("ai-boundary");
  dialog._trigger = event.currentTarget;
  dialog.showModal();
});
for (const dialog of document.querySelectorAll("dialog")) {
  dialog.addEventListener("close", () => {
    if (dialog._trigger) dialog._trigger.focus();
  });
}
initPanel(() => load());   // card detail panel (edit/notes/AI); reload on change
load();
connectSSE();
setInterval(load, 30000);
