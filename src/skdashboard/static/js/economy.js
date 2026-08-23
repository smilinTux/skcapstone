// Economy workspace: SKCounter AI usage, Autopilot cost, and Joule value.
// Units and overlapping measurement lanes remain explicitly separate.
import { esc, getJSON } from "./api.js";

const TOKEN_FIELDS = ["input", "output", "cache_read", "cache_write"];
const BREAKDOWN_LABELS = {
  models: ["model", "Model"],
  clients: ["client", "Client"],
  providers: ["provider", "Provider"],
  nodes: ["node_id", "Node"],
  agents: ["agent", "Agent"],
  workspaces: ["workspace", "Workspace"],
  sessions: ["session", "Session"],
  tasks: ["task", "Task"],
};

let currentData = null;
let breakdownDimension = "models";

function fmtInt(value) {
  return Math.round(Number(value) || 0).toLocaleString();
}

function fmtCompact(value) {
  return new Intl.NumberFormat(undefined, { notation: "compact", maximumFractionDigits: 2 }).format(Number(value) || 0);
}

function fmtUsd(value) {
  return "$" + (Number(value) || 0).toFixed(2);
}

function fmtPct(value, digits = 1) {
  return ((Number(value) || 0) * 100).toFixed(digits) + "%";
}

function fmtDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  const hours = Math.floor(total / 3600);
  return `${hours}h ${Math.floor((total % 3600) / 60)}m`;
}

function fmtAge(seconds) {
  const age = Math.max(0, Number(seconds) || 0);
  if (age < 60) return "just now";
  if (age < 3600) return `${Math.floor(age / 60)}m ago`;
  if (age < 86400) return `${Math.floor(age / 3600)}h ago`;
  return `${Math.floor(age / 86400)}d ago`;
}

function empty(message) {
  return `<div class="emptymsg">${esc(message)}</div>`;
}

function buildQuery() {
  const params = new URLSearchParams();
  const controls = {
    lane: "filter-lane",
    node: "filter-node",
    client: "filter-client",
    provider: "filter-provider",
    model: "filter-model",
    from: "filter-from",
    to: "filter-to",
  };
  Object.entries(controls).forEach(([key, id]) => {
    const value = document.getElementById(id).value;
    if (value) params.set(key, value);
  });
  return params.toString();
}

function fillSelect(id, values, emptyLabel) {
  const select = document.getElementById(id);
  const prior = select.value;
  select.replaceChildren();
  if (emptyLabel) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = emptyLabel;
    select.appendChild(option);
  }
  values.forEach((value) => {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  });
  if ([...select.options].some((option) => option.value === prior)) select.value = prior;
}

function populateFilters(usage) {
  const selectedLane = usage.selected_lane || "harness_reported";
  fillSelect("filter-lane", ["harness_reported", "gateway_observed"], null);
  document.getElementById("filter-lane").value = selectedLane;
  fillSelect("filter-node", usage.facets?.nodes || [], "All nodes");
  fillSelect("filter-client", usage.facets?.clients || [], "All clients");
  fillSelect("filter-provider", usage.facets?.providers || [], "All providers");
  fillSelect("filter-model", usage.facets?.models || [], "All models");
}

function renderErrors(data) {
  const errors = [...(data.errors || []), ...((data.ai_usage || {}).errors || [])];
  const element = document.getElementById("eco-errors");
  if (!errors.length) {
    element.hidden = true;
    element.textContent = "";
    return;
  }
  element.hidden = false;
  element.textContent = `Warning: ${errors.join(" | ")}`;
}

function renderFreshness(usage) {
  const element = document.getElementById("eco-freshness");
  const collectors = usage.collectors || [];
  if (!collectors.length) {
    element.className = "eco-freshness empty";
    element.textContent = "No reporting collectors";
    return;
  }
  const freshest = collectors.slice().sort((a, b) => a.age_seconds - b.age_seconds)[0];
  element.className = `eco-freshness ${freshest.status}`;
  element.textContent = `Latest report ${fmtAge(freshest.age_seconds)}`;
}

function renderUsageKpis(usage) {
  const summary = usage.summary || {};
  const tokens = summary.tokens || {};
  const coverage = usage.coverage || {};
  const costState = summary.cost_state || "unavailable";
  const costValue = costState === "unavailable" ? "Unavailable" : fmtUsd(summary.cost_usd);
  const costMeta = costState === "estimated"
    ? "provider-price estimate"
    : costState === "billed"
      ? "provider-billed amount"
      : costState === "mixed"
        ? "mixed estimate and billing"
        : "no pricing evidence";
  const coverageValue = coverage.percent == null
    ? `${fmtInt(coverage.reporting_nodes)} reporting`
    : `${Number(coverage.percent).toFixed(1)}%`;

  document.getElementById("usage-kpi").innerHTML = `
    <div class="kpi"><div class="l">Total tokens</div><div class="n mono">${fmtCompact(tokens.total)}</div><div class="cap-label">${fmtInt(tokens.total)} exact count</div></div>
    <div class="kpi"><div class="l">Input / output</div><div class="n mono">${fmtCompact(tokens.input)} / ${fmtCompact(tokens.output)}</div><div class="cap-label">reasoning ${fmtCompact(tokens.reasoning)}</div></div>
    <div class="kpi"><div class="l">USD cost</div><div class="n mono eco-kpi-text">${esc(costValue)}</div><div class="cap-label">${esc(costMeta)}</div></div>
    <div class="kpi"><div class="l">Cache ratio</div><div class="n mono">${fmtPct(summary.cache_ratio)}</div><div class="cap-label">${fmtCompact(tokens.cache_read)} cache-read tokens</div></div>
    <div class="kpi"><div class="l">Messages</div><div class="n mono">${fmtCompact(summary.message_count)}</div><div class="cap-label">${fmtInt(summary.sample_count)} timed samples</div></div>
    <div class="kpi"><div class="l">Fleet coverage</div><div class="n mono eco-kpi-text">${esc(coverageValue)}</div><div class="cap-label">${fmtInt(coverage.fresh_collectors)} fresh, ${fmtInt(coverage.stale_collectors)} stale</div></div>`;
}

function renderUsageChart(series) {
  const element = document.getElementById("usage-chart");
  document.getElementById("usage-series-badge").textContent = `${series.length} buckets`;
  if (!series.length || !series.some((item) => item.tokens?.total > 0)) {
    element.innerHTML = empty("No daily usage observations match these filters.");
    return;
  }

  const points = series.slice(-60);
  const W = 960;
  const H = 250;
  const padX = 12;
  const padBottom = 14;
  const gap = 2;
  const plotHeight = H - padBottom;
  const max = Math.max(1, ...points.map((item) => TOKEN_FIELDS.reduce((sum, field) => sum + (item.tokens?.[field] || 0), 0)));
  const slot = (W - padX * 2) / points.length;
  const barWidth = Math.max(1, slot - gap);
  const classes = { input: "usage-input", output: "usage-output", cache_read: "usage-cache-read", cache_write: "usage-cache-write" };
  const bars = points.map((item, index) => {
    const total = TOKEN_FIELDS.reduce((sum, field) => sum + (item.tokens?.[field] || 0), 0);
    let cursor = H - padBottom;
    const segments = TOKEN_FIELDS.map((field) => {
      const height = total ? ((item.tokens?.[field] || 0) / max) * plotHeight : 0;
      cursor -= height;
      return `<rect class="${classes[field]}" x="${(padX + slot * index).toFixed(1)}" y="${cursor.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${Math.max(0, height).toFixed(1)}"></rect>`;
    }).join("");
    const title = `${item.bucket}: ${fmtInt(item.tokens?.total)} tokens, ${fmtUsd(item.cost_usd)}`;
    return `<g>${segments}<title>${esc(title)}</title></g>`;
  }).join("");

  element.innerHTML = `<div class="usage-chart-wrap"><svg class="usage-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Daily token usage stacked by token type"><line class="eco-axis" x1="0" y1="${H - padBottom}" x2="${W}" y2="${H - padBottom}"></line>${bars}</svg></div><div class="eco-chart-legend"><span>${esc(points[0].bucket)}</span><span>${esc(points[points.length - 1].bucket)}</span></div><div class="usage-legend"><span class="input">Input</span><span class="output">Output</span><span class="cache-read">Cache read</span><span class="cache-write">Cache write</span></div>`;
}

function renderTokenMix(summary) {
  const element = document.getElementById("usage-mix");
  const tokens = summary.tokens || {};
  const total = TOKEN_FIELDS.reduce((sum, field) => sum + (tokens[field] || 0), 0);
  if (!total) {
    element.innerHTML = empty("No token composition is available.");
    return;
  }
  const labels = { input: "Input", output: "Output", cache_read: "Cache read", cache_write: "Cache write" };
  const classes = { input: "usage-input", output: "usage-output", cache_read: "usage-cache-read", cache_write: "usage-cache-write" };
  const segments = TOKEN_FIELDS.map((field) => `<span class="${classes[field]}" style="width:${((tokens[field] || 0) / total * 100).toFixed(4)}%" title="${esc(labels[field])}: ${fmtInt(tokens[field])}"></span>`).join("");
  const rows = TOKEN_FIELDS.map((field) => `<div class="mix-row"><span class="mix-dot ${classes[field]}"></span><span>${esc(labels[field])}</span><strong>${fmtCompact(tokens[field])}</strong><small>${fmtPct((tokens[field] || 0) / total)}</small></div>`).join("");
  element.innerHTML = `<div class="mix-bar">${segments}</div><div class="mix-list">${rows}</div><div class="mix-reasoning"><span>Reasoning tokens</span><strong>${fmtCompact(tokens.reasoning)}</strong><small>reported separately</small></div>`;
}

function renderActivity(summary) {
  const speed = summary.ms_per_1k_tokens == null ? "Unavailable" : `${Number(summary.ms_per_1k_tokens).toFixed(1)} ms`;
  document.getElementById("usage-activity").innerHTML = `<div class="activity-grid">
    <div><span>Active time</span><strong>${fmtDuration(summary.active_seconds)}</strong></div>
    <div><span>Longest continuous</span><strong>${fmtDuration(summary.longest_continuous_seconds)}</strong></div>
    <div><span>Peak concurrency</span><strong>${fmtInt(summary.max_concurrent)}</strong></div>
    <div><span>Timing coverage</span><strong>${fmtPct(summary.token_coverage)}</strong></div>
    <div><span>Per 1K timed tokens</span><strong>${esc(speed)}</strong></div>
    <div><span>Measured duration</span><strong>${fmtDuration((summary.duration_ms || 0) / 1000)}</strong></div>
  </div>`;
}

function renderBreakdown(usage) {
  const element = document.getElementById("usage-breakdown");
  const [field, label] = BREAKDOWN_LABELS[breakdownDimension];
  const rows = usage.breakdowns?.[breakdownDimension] || [];
  document.querySelectorAll("[data-breakdown]").forEach((button) => {
    const active = button.dataset.breakdown === breakdownDimension;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (!rows.length) {
    element.innerHTML = empty(`No ${label.toLowerCase()} breakdown is available in this source view.`);
    return;
  }
  element.innerHTML = `<table class="eco"><caption class="sr-only">Usage grouped by ${esc(label)}</caption><thead><tr><th>${esc(label)}</th><th class="num">Total</th><th class="num">Input</th><th class="num">Output</th><th class="num">Cache read</th><th class="num">Messages</th><th class="num">USD cost</th><th>Cost state</th></tr></thead><tbody>${rows.map((row) => `<tr><td class="eco-primary-cell">${esc(row[field] || "unknown")}</td><td class="num">${fmtCompact(row.tokens?.total)}</td><td class="num">${fmtCompact(row.tokens?.input)}</td><td class="num">${fmtCompact(row.tokens?.output)}</td><td class="num">${fmtCompact(row.tokens?.cache_read)}</td><td class="num">${fmtInt(row.message_count)}</td><td class="num">${row.cost_state === "unavailable" ? "-" : fmtUsd(row.cost_usd)}</td><td><span class="eco-state ${esc(row.cost_state)}">${esc(row.cost_state)}</span></td></tr>`).join("")}</tbody></table>`;
}

function renderContributions(series) {
  const element = document.getElementById("usage-contributions");
  if (!series.length) {
    element.innerHTML = empty("No daily contribution history is available.");
    return;
  }
  const points = series.slice(-90);
  const max = Math.max(1, ...points.map((item) => item.tokens?.total || 0));
  element.innerHTML = `<div class="contribution-grid" role="img" aria-label="Daily token contribution intensity">${points.map((item) => {
    const ratio = (item.tokens?.total || 0) / max;
    const level = ratio === 0 ? 0 : Math.min(5, Math.max(1, Math.ceil(ratio * 5)));
    return `<span class="contribution-cell l${level}" title="${esc(item.bucket)}: ${fmtInt(item.tokens?.total)} tokens"></span>`;
  }).join("")}</div><div class="contribution-legend"><span>Less</span>${[0, 1, 2, 3, 4, 5].map((level) => `<i class="contribution-cell l${level}"></i>`).join("")}<span>More</span></div>`;
}

function renderHourly(hourly) {
  const element = document.getElementById("usage-hourly");
  document.getElementById("usage-hourly-badge").textContent = `${hourly.length} buckets`;
  if (!hourly.length) {
    element.innerHTML = empty("No hourly view has been reported.");
    return;
  }
  const rows = hourly.slice(-24).reverse();
  element.innerHTML = `<table class="eco"><caption class="sr-only">Most recent hourly token usage</caption><thead><tr><th>Hour</th><th class="num">Tokens</th><th class="num">Messages</th><th class="num">USD</th></tr></thead><tbody>${rows.map((row) => `<tr><td>${esc(row.bucket.replace("T", " "))}:00</td><td class="num">${fmtCompact(row.tokens?.total)}</td><td class="num">${fmtInt(row.message_count)}</td><td class="num">${row.cost_state === "unavailable" ? "-" : fmtUsd(row.cost_usd)}</td></tr>`).join("")}</tbody></table>`;
}

function renderCollectors(usage) {
  const coverage = usage.coverage || {};
  const summary = document.getElementById("coverage-summary");
  summary.innerHTML = `<span><strong>${fmtInt(coverage.reporting_nodes)}</strong> reporting nodes</span><span><strong>${fmtInt(coverage.fresh_collectors)}</strong> fresh</span><span><strong>${fmtInt(coverage.stale_collectors)}</strong> stale</span>${coverage.percent == null ? "" : `<span><strong>${Number(coverage.percent).toFixed(1)}%</strong> eligible coverage</span>`}`;
  const element = document.getElementById("usage-collectors");
  const collectors = usage.collectors || [];
  if (!collectors.length) {
    const missing = coverage.missing_nodes?.length ? ` Missing nodes: ${coverage.missing_nodes.join(", ")}.` : "";
    element.innerHTML = empty(`No collectors have reported in this lane.${missing}`);
    return;
  }
  element.innerHTML = `<table class="eco"><caption class="sr-only">SKCounter collector coverage</caption><thead><tr><th>Node</th><th>Principal</th><th>Status</th><th>Last report</th><th>SKCounter</th><th>Backend</th></tr></thead><tbody>${collectors.map((collector) => `<tr><td class="eco-primary-cell">${esc(collector.node_id)}</td><td>${esc(collector.principal_id)}</td><td><span class="eco-state ${esc(collector.status)}">${esc(collector.status)}</span></td><td>${esc(fmtAge(collector.age_seconds))}</td><td>${esc(collector.facade_version)}</td><td>${esc(collector.backend)} ${esc(collector.backend_version)}</td></tr>`).join("")}</tbody></table>${coverage.missing_nodes?.length ? `<div class="coverage-missing"><strong>Missing eligible nodes:</strong> ${esc(coverage.missing_nodes.join(", "))}</div>` : ""}`;
}

function renderUsage(usage) {
  const status = usage.status || "empty";
  const statusElement = document.getElementById("usage-status");
  statusElement.className = `eco-status ${esc(status)}`;
  statusElement.textContent = status;
  populateFilters(usage);
  renderFreshness(usage);
  renderUsageKpis(usage);
  renderUsageChart(usage.series || []);
  renderTokenMix(usage.summary || {});
  renderActivity(usage.summary || {});
  renderBreakdown(usage);
  renderContributions(usage.series || []);
  renderHourly(usage.hourly || []);
  renderCollectors(usage);
}

function renderAutopilot(data) {
  const cost = data.autopilot_cost || {};
  const today = cost.today || {};
  const last7 = cost.last_7_days || {};
  const last30 = cost.last_30_days || {};
  const allTime = cost.all_time || {};
  const pct = cost.today_pct_of_cap;
  const capLine = cost.cap_usd == null
    ? "No daily USD cap configured"
    : `${Number(pct || 0).toFixed(1)}% of ${fmtUsd(cost.cap_usd)} daily cap`;
  document.getElementById("auto-kpi").innerHTML = `
    <div class="kpi"><div class="l">Today</div><div class="n mono">${fmtInt(today.joules)}J</div><div class="cap-label">${fmtUsd(today.cost_usd)} | ${fmtInt(today.runs)} runs</div></div>
    <div class="kpi"><div class="l">Last 7 days</div><div class="n mono">${fmtInt(last7.joules)}J</div><div class="cap-label">${fmtUsd(last7.cost_usd)} | ${fmtInt(last7.runs)} runs</div></div>
    <div class="kpi"><div class="l">Last 30 days</div><div class="n mono">${fmtInt(last30.joules)}J</div><div class="cap-label">${fmtUsd(last30.cost_usd)} | ${fmtInt(last30.runs)} runs</div></div>
    <div class="kpi"><div class="l">All time</div><div class="n mono">${fmtInt(allTime.joules)}J</div><div class="cap-label">${fmtUsd(allTime.cost_usd)} | ${fmtInt(allTime.runs)} runs</div></div>
    <div class="kpi eco-span-2"><div class="l">Daily guardrail</div><div class="n mono eco-kpi-text">${esc(capLine)}</div><div class="cap-label">Autopilot policy, not SKCounter pricing</div></div>`;
  renderAutopilotChart(data.cost_series || []);
  renderByRepo(cost.by_repo || {});
  renderSettlements(data.settlements || []);
}

function renderAutopilotChart(series) {
  const element = document.getElementById("auto-chart");
  if (!series.length || !series.some((item) => item.joules > 0)) {
    element.innerHTML = empty("No Autopilot cost data yet.");
    return;
  }
  const W = 900;
  const H = 180;
  const max = Math.max(1, ...series.map((item) => item.joules));
  const slot = W / series.length;
  const bars = series.map((item, index) => {
    const height = Math.max(item.joules > 0 ? 1 : 0, item.joules / max * (H - 10));
    const title = `${item.date}: ${fmtInt(item.joules)}J, ${fmtUsd(item.cost_usd)}, ${fmtInt(item.runs)} runs`;
    return `<rect class="eco-joule-bar" x="${(index * slot + 1).toFixed(1)}" y="${(H - height).toFixed(1)}" width="${Math.max(1, slot - 2).toFixed(1)}" height="${height.toFixed(1)}"><title>${esc(title)}</title></rect>`;
  }).join("");
  element.innerHTML = `<div class="usage-chart-wrap"><svg class="usage-chart" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="Autopilot Joule cost by day">${bars}</svg></div><div class="eco-chart-legend"><span>${esc(series[0].date)}</span><span>${esc(series[series.length - 1].date)}</span></div>`;
}

function renderByRepo(byRepo) {
  const element = document.getElementById("auto-by-repo");
  const rows = Object.entries(byRepo).map(([repo, value]) => ({ repo, ...value })).sort((a, b) => b.joules - a.joules);
  if (!rows.length) {
    element.innerHTML = empty("No governed runs recorded yet.");
    return;
  }
  element.innerHTML = `<table class="eco"><caption class="sr-only">Autopilot cost by repository</caption><thead><tr><th>Repository</th><th class="num">Joules</th><th class="num">USD</th><th class="num">Tokens</th><th class="num">Runs</th></tr></thead><tbody>${rows.map((row) => `<tr><td class="eco-primary-cell">${esc(row.repo)}</td><td class="num">${fmtInt(row.joules)}</td><td class="num">${fmtUsd(row.cost_usd)}</td><td class="num">${fmtCompact(row.tokens)}</td><td class="num">${fmtInt(row.runs)}</td></tr>`).join("")}</tbody></table>`;
}

function renderSettlements(rows) {
  const element = document.getElementById("auto-settlements");
  if (!rows.length) {
    element.innerHTML = empty("No settlements recorded yet.");
    return;
  }
  element.innerHTML = `<table class="eco"><caption class="sr-only">Recent Joule settlements</caption><thead><tr><th>Date</th><th>Card</th><th>Agent</th><th class="num">Net</th><th class="num">Balance</th></tr></thead><tbody>${rows.map((row) => {
    const net = Number(row.net_joules) || 0;
    return `<tr><td>${esc((row.ts || "").slice(0, 10))}</td><td>${esc(row.card_id || "")}</td><td>${esc(row.agent || "")}</td><td class="num ${net >= 0 ? "net-pos" : "net-neg"}">${net >= 0 ? "+" : ""}${fmtInt(net)}</td><td class="num">${fmtInt(row.balance_after)}J</td></tr>`;
  }).join("")}</tbody></table>`;
}

function renderJoule(data) {
  const economy = data.joule_economy || {};
  const agents = economy.agents || [];
  const average = economy.active_agents ? economy.total_supply / economy.active_agents : 0;
  document.getElementById("joule-kpi").innerHTML = `
    <div class="kpi"><div class="l">Total supply</div><div class="n mono">${fmtInt(economy.total_supply)}J</div><div class="cap-label">fleet circulating balance</div></div>
    <div class="kpi"><div class="l">Active agents</div><div class="n mono">${fmtInt(economy.active_agents)}</div><div class="cap-label">wallet or mint history</div></div>
    <div class="kpi"><div class="l">Average balance</div><div class="n mono">${fmtInt(average)}J</div><div class="cap-label">descriptive, not a target</div></div>`;
  const element = document.getElementById("joule-wallets");
  if (!agents.length) {
    element.innerHTML = empty("No agent wallets found.");
    return;
  }
  element.innerHTML = `<table class="eco"><caption class="sr-only">Agent Joule wallet balances</caption><thead><tr><th>Agent</th><th class="num">Balance</th><th>Level</th><th class="num">Supply share</th></tr></thead><tbody>${agents.map((agent) => `<tr><td class="eco-primary-cell">${esc(agent.agent)}</td><td class="num">${fmtInt(agent.balance)}J</td><td><span class="lvl-badge">${esc(agent.level)}</span></td><td class="num">${fmtPct(economy.total_supply ? agent.balance / economy.total_supply : 0)}</td></tr>`).join("")}</tbody></table>`;
}

function setSection(section) {
  const chosen = ["ai-usage", "autopilot", "joule"].includes(section) ? section : "ai-usage";
  document.querySelectorAll(".eco-subtab").forEach((button) => {
    const active = button.dataset.section === chosen;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".eco-section").forEach((panel) => {
    panel.hidden = panel.id !== `eco-${chosen}`;
  });
  history.replaceState(null, "", `#${chosen}`);
}

async function load() {
  const query = buildQuery();
  try {
    currentData = await getJSON(`/api/economy${query ? `?${query}` : ""}`);
  } catch (error) {
    document.getElementById("usage-kpi").innerHTML = empty(error.message);
    document.getElementById("eco-freshness").textContent = "Dashboard request failed";
    return;
  }
  renderErrors(currentData);
  renderUsage(currentData.ai_usage || {});
  renderAutopilot(currentData);
  renderJoule(currentData);
}

document.querySelectorAll(".eco-subtab").forEach((button) => button.addEventListener("click", () => setSection(button.dataset.section)));
document.querySelectorAll("[data-breakdown]").forEach((button) => button.addEventListener("click", () => {
  breakdownDimension = button.dataset.breakdown;
  if (currentData) renderBreakdown(currentData.ai_usage || {});
}));
document.getElementById("btn-refresh").addEventListener("click", load);
document.getElementById("btn-apply-filters").addEventListener("click", load);
document.getElementById("filter-lane").addEventListener("change", load);
document.getElementById("btn-clear-filters").addEventListener("click", () => {
  ["filter-node", "filter-client", "filter-provider", "filter-model", "filter-from", "filter-to"].forEach((id) => { document.getElementById(id).value = ""; });
  document.getElementById("filter-lane").value = "harness_reported";
  load();
});

setSection(location.hash.replace(/^#/, "") || "ai-usage");
load();
setInterval(load, 60000);
