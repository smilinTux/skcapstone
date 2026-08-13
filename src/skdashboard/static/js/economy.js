// Economy view: fleet-wide cost (autopilot cost ledger) + joule wealth
// (skjoule wallets) from one /api/economy call. No external chart lib -- the
// cost-over-time chart is a hand-rolled inline SVG bar chart (same approach
// trust.html uses for its force-directed graph).
import { esc, getJSON } from "./api.js";

function fmtInt(n) {
  return Math.round(n || 0).toLocaleString();
}

function fmtUsd(n) {
  return "$" + (n || 0).toFixed(2);
}

async function load() {
  let d;
  try { d = await getJSON("/api/economy"); }
  catch (e) {
    document.getElementById("eco-kpi").innerHTML = `<div class="emptymsg">${esc(e.message)}</div>`;
    return;
  }
  renderErrors(d.errors || []);
  renderKpis(d.autopilot_cost || {}, d.joule_economy || {});
  renderChart(d.cost_series || []);
  renderByRepo((d.autopilot_cost || {}).by_repo || {});
  renderWallets((d.joule_economy || {}).agents || []);
  renderSettlements(d.settlements || []);
}

function renderErrors(errors) {
  const el = document.getElementById("eco-errors");
  if (!errors.length) { el.hidden = true; el.textContent = ""; return; }
  el.hidden = false;
  el.textContent = "⚠ " + errors.join(" · ");
}

function renderKpis(cost, joules) {
  const today = cost.today || {};
  const allTime = cost.all_time || {};
  const pct = cost.today_pct_of_cap;
  const pctClamped = pct == null ? null : Math.min(100, Math.max(0, pct));
  const pctCls = pctClamped == null ? "" : pctClamped >= 100 ? "crit" : pctClamped >= 75 ? "warn" : "";
  const capLine = cost.cap_usd != null
    ? `<div class="capbar"><div class="capfill ${pctCls}" style="width:${pctClamped}%"></div></div>
       <div class="cap-label">${pctClamped.toFixed(1)}% of ${fmtUsd(cost.cap_usd)}/day cap</div>`
    : `<div class="cap-label">no daily cap configured</div>`;

  document.getElementById("eco-kpi").innerHTML = `
    <div class="kpi">
      <div class="l">Today's cost</div>
      <div class="n mono">${fmtInt(today.joules)}J <small>(${fmtUsd(today.cost_usd)})</small></div>
      ${capLine}
    </div>
    <div class="kpi">
      <div class="l">All-time cost</div>
      <div class="n mono">${fmtInt(allTime.joules)}J <small>(${fmtUsd(allTime.cost_usd)})</small></div>
      <div class="cap-label">${fmtInt(allTime.runs)} runs</div>
    </div>
    <div class="kpi">
      <div class="l">Total joule supply</div>
      <div class="n mono">${fmtInt(joules.total_supply)}J</div>
      <div class="cap-label">fleet-wide circulating balance</div>
    </div>
    <div class="kpi">
      <div class="l">Active agents</div>
      <div class="n mono">${fmtInt(joules.active_agents)}</div>
      <div class="cap-label">with a wallet balance or mint history</div>
    </div>`;
}

function renderChart(series) {
  const el = document.getElementById("eco-chart");
  if (!series.length || !series.some((s) => s.joules > 0)) {
    el.innerHTML = `<div class="emptymsg">No cost data yet.</div>`;
    return;
  }
  const W = 900, H = 160, padL = 6, padR = 6, padB = 6;
  const max = Math.max(1, ...series.map((s) => s.joules));
  const n = series.length;
  const barGap = 2;
  const barW = Math.max(1, (W - padL - padR) / n - barGap);
  const bars = series.map((s, i) => {
    const h = Math.max(s.joules > 0 ? 1 : 0, (s.joules / max) * (H - padB));
    const x = padL + i * ((W - padL - padR) / n);
    const y = H - padB - h;
    const title = `${s.date}: ${fmtInt(s.joules)}J (${fmtUsd(s.cost_usd)}, ${fmtInt(s.runs)} runs)`;
    return `<rect class="eco-bar" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}"><title>${esc(title)}</title></rect>`;
  }).join("");
  el.innerHTML = `<div class="eco-chart-wrap">
    <svg class="eco-chart-svg" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <line class="eco-axis" x1="0" y1="${H - padB}" x2="${W}" y2="${H - padB}"/>
      ${bars}
    </svg></div>
    <div class="eco-chart-legend"><span>${esc(series[0].date)}</span><span>${esc(series[series.length - 1].date)}</span></div>`;
}

function renderByRepo(byRepo) {
  const el = document.getElementById("eco-by-repo");
  const rows = Object.entries(byRepo).map(([repo, v]) => ({ repo, ...v }));
  if (!rows.length) { el.innerHTML = `<div class="emptymsg">No runs recorded yet.</div>`; return; }
  rows.sort((a, b) => b.joules - a.joules);
  el.innerHTML = `<table class="eco">
    <thead><tr><th>Repo</th><th class="num">Joules</th><th class="num">$</th><th class="num">Tokens</th><th class="num">Runs</th></tr></thead>
    <tbody>${rows.map((r) => `<tr>
      <td>${esc(r.repo)}</td>
      <td class="num">${fmtInt(r.joules)}</td>
      <td class="num">${fmtUsd(r.cost_usd)}</td>
      <td class="num">${fmtInt(r.tokens)}</td>
      <td class="num">${fmtInt(r.runs)}</td>
    </tr>`).join("")}</tbody></table>`;
}

function renderWallets(agents) {
  const el = document.getElementById("eco-wallets");
  if (!agents.length) { el.innerHTML = `<div class="emptymsg">No agent wallets found.</div>`; return; }
  const sorted = agents.slice().sort((a, b) => b.balance - a.balance);
  el.innerHTML = `<table class="eco">
    <thead><tr><th>Agent</th><th class="num">Balance</th><th>Level</th></tr></thead>
    <tbody>${sorted.map((a) => `<tr>
      <td>${esc(a.agent)}</td>
      <td class="num">${fmtInt(a.balance)}J</td>
      <td><span class="lvl-badge">${esc(a.level)}</span></td>
    </tr>`).join("")}</tbody></table>`;
}

function renderSettlements(rows) {
  const el = document.getElementById("eco-settlements");
  if (!rows.length) { el.innerHTML = `<div class="emptymsg">No settlements recorded yet.</div>`; return; }
  el.innerHTML = `<table class="eco">
    <thead><tr><th>Date</th><th>Card</th><th>Agent</th><th class="num">Minted</th><th class="num">Spent</th><th class="num">Net</th><th class="num">Balance after</th></tr></thead>
    <tbody>${rows.map((r) => {
      const net = r.net_joules || 0;
      const netCls = net >= 0 ? "net-pos" : "net-neg";
      const netStr = (net >= 0 ? "+" : "") + fmtInt(net);
      return `<tr>
        <td>${esc((r.ts || "").slice(0, 10))}</td>
        <td>${esc(r.card_id || "")}</td>
        <td>${esc(r.agent || "")}</td>
        <td class="num">+${fmtInt(r.minted)}</td>
        <td class="num">-${fmtInt(r.spent_joules)}</td>
        <td class="num ${netCls}">${netStr}</td>
        <td class="num">${fmtInt(r.balance_after)}J</td>
      </tr>`;
    }).join("")}</tbody></table>`;
}

document.getElementById("btn-refresh").addEventListener("click", load);
load();
setInterval(load, 30000);
