import { esc, getJSON } from "./api.js";

const BASE = Object.freeze({ role: "project-manager", scope: "estate", window: "latest", baseline: "none", service: "all", report_type: "all" });
const ROLES = new Set(["project-manager", "operator", "architect", "auditor"]);
const TYPES = new Set(["all", "daily_operations", "weekly_portfolio", "sprint_flow", "monthly_service", "monthly_ai_economy", "quarterly_strategy", "ad_hoc_evidence"]);
const KEYS = new Set([...Object.keys(BASE), "snapshot", "compare"]);
let context = { ...BASE };
let projection = null;

function parseContext() {
  const pairs = [...new URLSearchParams(location.search).entries()];
  if (pairs.some(([key, value]) => !KEYS.has(key) || !value || value.length > 128) || new Set(pairs.map(([key]) => key)).size !== pairs.length) return null;
  const value = { ...BASE, ...Object.fromEntries(pairs) };
  return ROLES.has(value.role) && TYPES.has(value.report_type) && value.scope === "estate" && value.window === "latest" && ["none", "previous"].includes(value.baseline) && value.service === "all" ? value : null;
}
function query() { return new URLSearchParams(context).toString(); }
function show(value, fallback = "Unknown") { return value == null || value === "" ? fallback : String(value); }
function value(metric) { return metric.value == null ? "Unknown" : `${metric.value}${metric.unit === "percent" ? "%" : ` ${metric.unit}`}`; }
function metrics(snapshot) { return snapshot ? snapshot.sections.flatMap((section) => section.metric_results) : []; }
function insights(snapshot) { return snapshot ? snapshot.sections.flatMap((section) => section.insights) : []; }

function updateContext(changes, mode = "push") {
  context = { ...context, ...changes };
  Object.keys(context).forEach((key) => { if (context[key] == null || context[key] === "") delete context[key]; });
  const url = new URL(location.href); url.pathname = "/control-plane/reports"; url.search = query(); history[`${mode}State`]({}, "", url);
  document.getElementById("reports-role").value = context.role;
  document.getElementById("reports-type").value = context.report_type;
}

function render() {
  const selected = projection.selected;
  const selectedMetrics = metrics(selected);
  document.getElementById("reports-status").textContent = projection.truth_state;
  const summary = [
    ["Snapshots", projection.reports.length, "Immutable local evidence."],
    ["Selected quality", selected ? selected.quality_statement.truth_state : "Unknown", selected ? selected.quality_statement.summary : "No selected report."],
    ["Metrics", selectedMetrics.length, "Frozen values and definitions."],
    ["Review", selected ? selected.review_state.state : "Unknown", "Separate from metric truth."],
  ];
  document.getElementById("reports-summary").innerHTML = summary.map(([label, result, note]) => `<article><span>${esc(label)}</span><strong>${esc(result)}</strong><small>${esc(note)}</small></article>`).join("");
  document.getElementById("snapshot-count").textContent = `${projection.reports.length} visible`;
  document.getElementById("snapshot-rows").innerHTML = projection.reports.map((item) => `<tr><th scope="row" class="mono">${esc(item.snapshot_id)}</th><td>${esc(item.report_type)}<small>${esc(item.as_of)}</small></td><td>${esc(JSON.stringify(item.scope))}<small>baseline ${esc(show(item.baseline))}</small></td><td>${esc(item.truth_state)}<small>${esc(item.quality_summary)}</small></td><td>${esc(item.metric_count)}</td><td>${esc(item.review_state.state)}<small>${esc(show(item.review_state.reviewer, "No reviewer"))}</small></td><td>${esc(show(item.supersedes, "Original"))}</td><td class="mono">${esc(item.report_hash)}</td><td><button type="button" class="project-evidence-button" data-view="${esc(item.snapshot_id)}">View</button>${selected && selected.snapshot_id !== item.snapshot_id ? `<button type="button" class="project-evidence-button" data-compare="${esc(item.snapshot_id)}">Compare</button>` : ""}</td></tr>`).join("") || `<tr><td colspan="9">No immutable report snapshot is visible.</td></tr>`;
  document.querySelectorAll("[data-view]").forEach((button) => {
    const activate = () => { updateContext({ snapshot: button.dataset.view, compare: null }); load(); };
    button.addEventListener("click", activate);
    button.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); activate(); } });
  });
  document.querySelectorAll("[data-compare]").forEach((button) => {
    const activate = () => { updateContext({ compare: button.dataset.compare }); load(); };
    button.addEventListener("click", activate);
    button.addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); activate(); } });
  });

  document.getElementById("metric-count").textContent = `${selectedMetrics.length} frozen`;
  document.getElementById("metric-rows").innerHTML = selectedMetrics.map((item) => `<tr><th scope="row">${esc(item.label || item.metric_id)}<small class="mono">${esc(item.metric_id)}@${esc(item.definition_version)}</small></th><td>${esc(value(item))}<small>${esc(item.truth_state)}</small></td><td>${esc(item.measurement_kind)}</td><td class="mono">${esc(item.calculation.definition_hash)}</td><td>${esc(item.calculation.method)}<small>${esc(show(item.calculation.calculation_ref))}</small></td><td class="mono">${esc(item.source.watermarks.map((watermark) => `${watermark.source}: ${watermark.value}`).join("; ") || "Unavailable")}</td><td>${esc(item.data_quality.errors.join("; ") || "No errors")}<small>${esc(item.data_quality.exclusions.join("; ") || "No exclusions")}</small></td></tr>`).join("") || `<tr><td colspan="7">No frozen metric is selected.</td></tr>`;

  const comparison = projection.comparison;
  document.getElementById("comparison-state").textContent = comparison ? comparison.state : "No comparison";
  document.getElementById("comparison-rows").innerHTML = comparison && comparison.metric_changes.length ? comparison.metric_changes.map((item) => `<tr><th scope="row" class="mono">${esc(item.metric_ref)}</th><td>${esc(show(item.current_value))}</td><td>${esc(show(item.baseline_value))}</td><td>${esc(item.current_truth_state)} / ${esc(item.baseline_truth_state)}</td><td>${esc(item.definition_changed)}</td><td>${esc(item.comparable)}</td><td>${esc(show(item.delta, "Not comparable"))}</td></tr>`).join("") : `<tr><td colspan="7">${comparison ? esc(comparison.state) : "Choose a comparison from a snapshot row."}</td></tr>`;

  const selectedInsights = insights(selected);
  document.getElementById("narrative-count").textContent = `${selectedInsights.length} insights`;
  document.getElementById("narrative-rows").innerHTML = selectedInsights.map((item) => `<tr><th scope="row" class="mono">${esc(item.insight_id)}</th><td>${esc(item.status)}<small>${esc(item.kind)}</small></td><td>${esc(item.metric_refs.join(", "))}<small>${esc(item.calculation_refs.join(", "))}</small></td><td>${esc(item.evidence_refs.join(", "))}</td><td>${esc(item.uncertainty.join("; "))}<small>${esc(item.exclusions.join("; "))}</small></td><td>${esc(item.model_provenance.logical_route)}<small>${esc(item.model_provenance.served_model)} @ ${esc(item.model_provenance.model_revision)}</small></td><td class="mono">${esc(item.policy_decision_ref)}</td></tr>`).join("") || `<tr><td colspan="7">No AI narrative is recorded in this snapshot.</td></tr>`;
}

async function load() {
  document.getElementById("reports-status").textContent = "Loading";
  try { projection = await getJSON(`/api/v1/reports/projection?${query()}`); render(); }
  catch (error) { document.getElementById("reports-status").textContent = `Unavailable: ${error.message}`; document.getElementById("snapshot-rows").innerHTML = `<tr><td colspan="9">No report value is inferred.</td></tr>`; }
}

function initialize() {
  const parsed = parseContext();
  if (!parsed) { document.getElementById("reports-status").textContent = "Unavailable: unsupported or protected scope"; return; }
  context = parsed; updateContext({}, "replace");
  document.getElementById("reports-context").addEventListener("change", () => { updateContext({ role: document.getElementById("reports-role").value, report_type: document.getElementById("reports-type").value, snapshot: null, compare: null }); load(); });
  window.addEventListener("popstate", () => { const next = parseContext(); if (next) { context = next; updateContext({}, "replace"); load(); } });
  load();
}
initialize();
