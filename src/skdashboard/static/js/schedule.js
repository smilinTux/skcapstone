import { esc, getJSON } from "./api.js";

const BASE = Object.freeze({ role: "project-manager", scope: "estate", window: "latest", baseline: "none", service: "all", lens: "roadmap", timezone: "UTC", selected_item: "" });
const ROLES = new Set(["project-manager", "operator", "architect", "service", "team"]);
const LENSES = new Set(["roadmap", "gantt", "flow"]);
const TIMEZONES = new Set(["UTC", "America/Chicago"]);
const KEYS = new Set(Object.keys(BASE));
let context = { ...BASE };
let projection = null;
let zoom = 1;
let lastTrigger = null;

function parseContext() {
  const pairs = [...new URLSearchParams(location.search).entries()];
  if (pairs.some(([key, value]) => !KEYS.has(key) || !value || value.length > 128) || new Set(pairs.map(([key]) => key)).size !== pairs.length) return null;
  const value = { ...BASE, ...Object.fromEntries(pairs) };
  if (!ROLES.has(value.role) || !LENSES.has(value.lens) || !TIMEZONES.has(value.timezone) || value.scope !== "estate" || value.window !== "latest" || value.baseline !== "none" || value.service !== "all") return null;
  return value;
}

function query(value = context) {
  const params = new URLSearchParams(value);
  if (!value.selected_item) params.delete("selected_item");
  return params.toString();
}

function updateContext(next, mode = "push") {
  context = { ...context, ...next };
  const url = new URL(location.href);
  url.pathname = "/control-plane/schedule";
  url.search = query();
  history[`${mode}State`]({}, "", url);
  document.getElementById("schedule-role").value = context.role;
  document.getElementById("schedule-lens").value = context.lens;
  document.getElementById("schedule-timezone").value = context.timezone;
}

function dateValue(value) {
  if (!value || value.state !== "known" || !value.instant) return `${value && value.state || "unknown"}: ${value && value.reason || "date unavailable"}`;
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short", timeZone: context.timezone }).format(new Date(value.instant));
}

function instant(value) {
  return value && value.state === "known" && value.instant ? Date.parse(value.instant) : null;
}

function range() {
  const dates = projection.items.flatMap((item) => Object.values(item.dates).map(instant)).filter(Number.isFinite);
  if (!dates.length) return { start: 0, span: 1 };
  const start = Math.min(...dates), end = Math.max(...dates);
  return { start, span: Math.max(end - start, 86400000) };
}

function position(value, timeline) {
  const time = instant(value);
  return time == null ? null : Math.max(0, Math.min(100, (time - timeline.start) / timeline.span * 100));
}

function blockers(item) {
  const related = projection.dependencies.filter((edge) => edge.source_item_id === item.item_id || edge.target_item_id === item.item_id);
  const labels = related.filter((edge) => edge.blocker_state !== "not_blocking" || edge.cycle_state !== "acyclic").map((edge) => `${edge.blocker_state}, ${edge.cycle_state}`);
  const overlays = projection.overlays.filter((overlay) => overlay.conflict_state !== "clear").map((overlay) => `${overlay.overlay_type}: ${overlay.conflict_state}`);
  return [...labels, ...overlays].join("; ") || "No classified blocker";
}

function bar(item, timeline) {
  if (context.lens === "flow") return `<div class="schedule-track"><span class="schedule-flow-chip">${esc(item.status)}</span><span class="schedule-flow-chip">${esc(item.truth_state)}</span><span class="schedule-flow-chip">${esc(blockers(item))}</span></div>`;
  const start = position(item.dates.planned_start, timeline), end = position(item.dates.planned_target, timeline);
  if (context.lens === "roadmap") return `<div class="schedule-track"><span class="schedule-bar ${esc(item.truth_state)}" style="width:${Math.max(12, (end || 100) - (start || 0))}%" aria-label="${esc(item.title)} ${esc(dateValue(item.dates.planned_start))} to ${esc(dateValue(item.dates.planned_target))}"></span></div>`;
  if (context.lens !== "gantt") throw new Error("Unsupported schedule lens");
  if (item.item_type === "milestone" && end != null) return `<div class="schedule-track"><span class="schedule-milestone" style="left:${end}%" aria-label="Milestone ${esc(item.title)}"></span></div>`;
  if (start == null || end == null) return `<div class="schedule-track"><span class="schedule-flow-chip">${esc(item.dates.planned_start.state)} dates</span></div>`;
  return `<div class="schedule-track"><span class="schedule-bar ${esc(item.truth_state)}" style="left:${start}%;width:${Math.max(1, end - start)}%;transform:scaleX(${zoom});transform-origin:left" aria-label="${esc(item.title)} ${esc(dateValue(item.dates.planned_start))} to ${esc(dateValue(item.dates.planned_target))}"></span></div>`;
}

function render() {
  const timeline = range();
  const visual = document.getElementById("schedule-visual");
  visual.classList.toggle("schedule-flow", context.lens === "flow");
  visual.classList.toggle("schedule-roadmap", context.lens === "roadmap");
  document.getElementById("schedule-visual-title").textContent = context.lens[0].toUpperCase() + context.lens.slice(1);
  document.getElementById("schedule-meta").textContent = `${projection.projection_version} | ${context.timezone} | ${projection.items.length} items | ${projection.dependencies.length} dependencies`;
  document.getElementById("schedule-truth").textContent = `${projection.truth_state} | ${projection.visibility.state}`;
  document.getElementById("schedule-status").textContent = `Projection ${projection.projection_hash}`;
  const warning = document.getElementById("schedule-warning");
  const unsafe = projection.cycle_analysis.state !== "acyclic" || projection.critical_path.state === "unavailable" || projection.overlays.some((item) => item.conflict_state === "conflict");
  warning.hidden = !unsafe;
  warning.innerHTML = unsafe ? `<h2>Schedule exception</h2><p>Critical path ${esc(projection.critical_path.state)}. ${esc(projection.critical_path.reasons.join(", ") || projection.cycle_analysis.state)}. No schedule action is ready.</p>` : "";
  document.getElementById("schedule-rows").innerHTML = projection.items.map((item, index) => `<div class="schedule-row" role="listitem" data-child="${index > 0}" data-item="${esc(item.item_id)}"><button type="button" class="schedule-label" data-detail="${esc(item.item_id)}"><strong>${esc(item.title)}</strong><small>${esc(item.item_type)} | ${esc(item.owner_service_id)} | ${esc(item.truth_state)}</small></button>${bar(item, timeline)}</div>`).join("") || `<p>No visible schedule item.</p>`;
  document.getElementById("schedule-table-rows").innerHTML = projection.items.map((item) => `<tr><th scope="row">${esc(item.title)}<small class="mono">${esc(item.item_id)}</small></th><td>${esc(item.item_type)}<small>${esc(item.owner_service_id)}</small></td><td>${esc(dateValue(item.dates.baseline_start))}<small>${esc(dateValue(item.dates.baseline_target))}</small></td><td>${esc(dateValue(item.dates.planned_start))}<small>${esc(dateValue(item.dates.planned_target))}</small></td><td>${esc(dateValue(item.dates.actual_start))}<small>${esc(dateValue(item.dates.actual_finish))}</small></td><td>${item.progress == null ? "Unknown" : esc(Math.round(item.progress * 100) + "%")}<small>${esc(item.progress_basis)}</small></td><td>${esc(item.truth_state)}<small>${esc(item.visibility.state)}; ${esc(item.visibility.authorization)}</small></td><td>${esc(blockers(item))}</td><td><button type="button" class="project-evidence-button" data-detail="${esc(item.item_id)}">Detail</button></td></tr>`).join("");
  document.getElementById("schedule-dependency-rows").innerHTML = projection.dependencies.map((edge) => `<tr><th scope="row">${esc(edge.source_item_id)} to ${esc(edge.target_item_id || "policy-filtered target")}</th><td>${esc(edge.edge_type)}<small>${esc(edge.direction)}</small></td><td>${edge.lag_seconds == null ? "Unknown" : esc(edge.lag_seconds + " seconds")}</td><td>${esc(edge.blocker_state)}</td><td>${esc(edge.cycle_state)}</td><td>${esc(edge.truth_state)}<small>${esc(edge.visibility.state)}</small></td><td>${esc(edge.evidence_refs.join(", "))}</td></tr>`).join("") || `<tr><td colspan="7">No visible dependency.</td></tr>`;
  document.querySelectorAll("[data-detail]").forEach((button) => {
    button.addEventListener("click", () => openDetail(button.dataset.detail, button));
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); openDetail(button.dataset.detail, button); }
    });
  });
}

function renderForecast(forecast) {
  const ranges = document.getElementById("schedule-forecast-ranges");
  document.getElementById("schedule-forecast-state").textContent = forecast.state;
  document.getElementById("schedule-forecast-meta").textContent = `${forecast.method} | ${forecast.sample_periods} periods | ${forecast.history_window.start || "unknown"} to ${forecast.history_window.end || "unknown"}`;
  ranges.textContent = forecast.state === "ready" ? `P50 ${forecast.completion_quantiles_periods.p50}, P85 ${forecast.completion_quantiles_periods.p85}, P95 ${forecast.completion_quantiles_periods.p95} periods. Aggregate flow only, not a critical-path date.` : `Unavailable: ${forecast.abstention_reason}`;
}

function openDetail(id, trigger) {
  const item = projection.items.find((candidate) => candidate.item_id === id);
  if (!item) return;
  lastTrigger = trigger;
  context.selected_item = id;
  updateContext(context, "replace");
  document.getElementById("schedule-detail-title").textContent = `${item.title} detail`;
  document.getElementById("schedule-detail-body").innerHTML = `<dl class="schedule-detail-grid"><dt>ID and type</dt><dd>${esc(item.item_id)}; ${esc(item.item_type)}</dd><dt>Owner and service</dt><dd>${esc(item.owner_service_id)}; ${esc(item.service_id || "not scoped")}</dd><dt>Baseline</dt><dd>${esc(dateValue(item.dates.baseline_start))} to ${esc(dateValue(item.dates.baseline_target))}</dd><dt>Planned</dt><dd>${esc(dateValue(item.dates.planned_start))} to ${esc(dateValue(item.dates.planned_target))}</dd><dt>Actual</dt><dd>${esc(dateValue(item.dates.actual_start))} to ${esc(dateValue(item.dates.actual_finish))}</dd><dt>Timezone</dt><dd>${esc(context.timezone)}</dd><dt>Truth and visibility</dt><dd>${esc(item.truth_state)}; ${esc(item.visibility.state)}; ${esc(item.visibility.authorization)}</dd><dt>Progress and rollup</dt><dd>${item.progress == null ? "Unknown" : esc(Math.round(item.progress * 100) + "%")}; ${esc(item.rollup.state)}; ${esc(item.rollup.exclusions.join(", ") || "no exclusions")}</dd><dt>Dependencies and blockers</dt><dd>${esc(blockers(item))}</dd><dt>Evidence</dt><dd>${esc(item.evidence_refs.join(", ") || "Unavailable")}</dd></dl>`;
  document.getElementById("schedule-detail").showModal();
}

async function load() {
  document.getElementById("schedule-status").textContent = "Loading";
  document.getElementById("schedule-forecast-state").textContent = "unavailable";
  document.getElementById("schedule-forecast-meta").textContent = "No authorized calibrated forecast provider.";
  document.getElementById("schedule-forecast-ranges").textContent = "Unavailable: no forecast value is inferred.";
  try {
    projection = await getJSON(`/api/v1/schedule/projection?${query()}`);
    render();
    try { renderForecast(await getJSON(`/api/v1/schedule/forecasts?${query()}`)); } catch (_error) { document.getElementById("schedule-forecast-ranges").textContent = "Unavailable: no authorized calibrated forecast provider."; }
    if (context.selected_item) openDetail(context.selected_item, document.querySelector(`[data-detail="${CSS.escape(context.selected_item)}"]`));
  } catch (error) {
    document.getElementById("schedule-status").textContent = `Unavailable: ${error.message}`;
    document.getElementById("schedule-rows").innerHTML = `<p>No schedule value is inferred.</p>`;
    document.getElementById("schedule-forecast-ranges").textContent = "Unavailable: no forecast value is inferred.";
  }
}

function exportSnapshot() {
  if (!projection) return;
  const snapshot = JSON.stringify({ captured_at: new Date().toISOString(), lens: context.lens, timezone: context.timezone, context, projection }, null, 2);
  const link = document.createElement("a");
  link.href = URL.createObjectURL(new Blob([snapshot], { type: "application/json" }));
  link.download = `schedule-${projection.projection_version}.json`;
  link.click();
  URL.revokeObjectURL(link.href);
}

function initialize() {
  const parsed = parseContext();
  if (!parsed) { document.getElementById("schedule-status").textContent = "Unavailable: unsupported or protected schedule scope"; return; }
  context = parsed; updateContext(context, "replace");
  document.getElementById("schedule-context").addEventListener("change", () => {
    const next = { role: document.getElementById("schedule-role").value, lens: document.getElementById("schedule-lens").value, timezone: document.getElementById("schedule-timezone").value, selected_item: "" };
    const sourceChanged = next.role !== context.role || next.timezone !== context.timezone;
    updateContext(next);
    if (sourceChanged) load();
    else if (projection) render();
  });
  document.getElementById("schedule-zoom-in").addEventListener("click", () => { zoom = Math.min(2, zoom + 0.25); if (projection) render(); });
  document.getElementById("schedule-zoom-out").addEventListener("click", () => { zoom = Math.max(0.5, zoom - 0.25); if (projection) render(); });
  document.getElementById("schedule-collapse").addEventListener("click", () => document.getElementById("schedule-visual").classList.add("schedule-collapsed"));
  document.getElementById("schedule-expand").addEventListener("click", () => document.getElementById("schedule-visual").classList.remove("schedule-collapsed"));
  document.getElementById("schedule-export").addEventListener("click", exportSnapshot);
  document.getElementById("schedule-detail").addEventListener("close", () => { context.selected_item = ""; updateContext(context, "replace"); if (lastTrigger) lastTrigger.focus(); });
  window.addEventListener("popstate", () => { const next = parseContext(); if (next) { context = next; updateContext(context, "replace"); load(); } });
  load();
}

initialize();
