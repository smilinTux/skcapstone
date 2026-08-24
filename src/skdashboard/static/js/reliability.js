import { esc, getJSON } from "./api.js";

const BASE = Object.freeze({ role: "operator", scope: "estate", window: "latest", baseline: "none", service: "all" });
const ROLES = new Set(["operator", "architect", "service-owner"]);
const KEYS = new Set(Object.keys(BASE));
let context = { ...BASE };
let projection = null;

function parseContext() {
  const pairs = [...new URLSearchParams(location.search).entries()];
  if (pairs.some(([key, value]) => !KEYS.has(key) || !value || value.length > 128) || new Set(pairs.map(([key]) => key)).size !== pairs.length) return null;
  const value = { ...BASE, ...Object.fromEntries(pairs) };
  return ROLES.has(value.role) && value.scope === "estate" && value.window === "latest" && value.baseline === "none" && value.service === "all" ? value : null;
}

function query() { return new URLSearchParams(context).toString(); }

function updateContext(role, mode = "push") {
  context = { ...context, role };
  const url = new URL(location.href);
  url.pathname = "/control-plane/reliability";
  url.search = query();
  history[`${mode}State`]({}, "", url);
  document.getElementById("reliability-role").value = role;
}

function value(metric) {
  if (!metric || metric.value == null) return "Unknown";
  return `${metric.value}${metric.unit === "percent" ? "%" : ` ${metric.unit}`}`;
}

function metric(id) { return projection.metrics.find((item) => item.metric_id === id); }
function yn(value) { return value ? "Recorded" : "Unknown"; }

function render() {
  document.getElementById("reliability-status").textContent = `${projection.truth_state} | ${projection.projection_hash}`;
  const summary = [
    ["service.availability_sli", "User-facing measurement only."],
    ["service.slo_target", "No target is inferred."],
    ["service.error_budget_remaining", "Requires SLI and approved SLO."],
    ["itil.change_success_rate", "Verified success versus failed only."],
  ];
  document.getElementById("reliability-summary").innerHTML = summary.map(([id, note]) => {
    const item = metric(id);
    return `<article><span>${esc(item.label)}</span><strong>${esc(value(item))}</strong><small>${esc(note)} ${esc(item.truth_state)}.</small></article>`;
  }).join("");

  document.getElementById("reliability-metric-rows").innerHTML = projection.metrics.map((item) => `<tr><th scope="row">${esc(item.label)}<small class="mono">${esc(item.metric_id)}</small></th><td><strong>${esc(value(item))}</strong><small>${esc(item.truth_state)}</small></td><td>${esc(item.numerator ?? "Unknown")} / ${esc(item.denominator ?? "Unknown")}</td><td>${esc(item.sample_size)} records<small>${esc(item.window)}</small></td><td>${esc(item.classification)}</td><td>${esc(item.exclusions.join(" ") || "None recorded")}</td><td>INC ${esc(item.legacy_coverage.incident_aliases)}/${esc(item.legacy_coverage.incident_records)}; PRB ${esc(item.legacy_coverage.problem_aliases)}/${esc(item.legacy_coverage.problem_records)}; CHG ${esc(item.legacy_coverage.change_aliases)}/${esc(item.legacy_coverage.change_records)}<small>${esc(item.evidence_refs.join(", ") || "Evidence unavailable")}</small></td></tr>`).join("");

  const breaches = projection.items.breach_risk || [];
  const breachMetric = metric("itil.open_sla_breaches");
  document.getElementById("breach-count").textContent = `${value(breachMetric)} across ${breachMetric.denominator} eligible`;
  document.getElementById("reliability-breach-rows").innerHTML = breaches.map((item) => `<tr><th scope="row" class="mono">${esc(item.id)}</th><td>${esc(item.title)}</td><td>${esc(item.severity)}</td><td>${esc(item.service || "Unknown")}</td><td>${esc(item.remaining_min)} minutes</td><td>${item.over ? "Breached" : "At risk"}</td></tr>`).join("") || `<tr><td colspan="6">No eligible open exception.</td></tr>`;

  const problems = new Map(projection.items.problems.map((item) => [item.id, item]));
  const incidents = projection.items.incidents.map((item) => `<tr><th scope="row" class="mono">${esc(item.legacy_alias)}</th><td>Incident; ${esc(item.status)}<small>${esc(item.title)}</small></td><td>Problem ${esc(item.problem_id || "Unknown")}</td><td>Not applicable</td><td>Acknowledged ${yn(item.acknowledged_at)}; resolved ${yn(item.resolved_at)}</td><td>Not applicable</td></tr>`);
  const changes = projection.items.changes.map((item) => {
    const problem = problems.get(item.problem_id);
    return `<tr><th scope="row" class="mono">${esc(item.legacy_alias)}</th><td>Change; ${esc(item.status)}; outcome ${esc(item.outcome)}<small>${esc(item.title)}</small></td><td>Problem ${esc(item.problem_id || "Unknown")}; incidents ${esc(problem ? problem.incident_ids.length : "Unknown")}</td><td>Validation ${esc(item.validation)}; CAB ${item.cab_required ? `${esc(item.cab_votes)} vote(s)` : "not required"}</td><td>Scheduled ${yn(item.scheduled)}; deployed ${yn(item.deployed)}; verified ${yn(item.verified)}</td><td>PIR ${yn(item.pir_recorded)}; rollback plan ${yn(item.rollback_plan_recorded)}; rollback event ${yn(item.rollback_event_recorded)}</td></tr>`;
  });
  document.getElementById("reliability-lineage-rows").innerHTML = [...incidents, ...changes].join("") || `<tr><td colspan="6">No visible lifecycle record.</td></tr>`;
  document.getElementById("reliability-kedb-rows").innerHTML = projection.items.kedb.map((item) => `<tr><th scope="row" class="mono">${esc(item.id)}</th><td>${esc(item.title)}</td><td>${esc(item.problem_id || "Unknown")}</td><td>${esc(item.change_id || "Unknown")}</td><td>${yn(item.root_cause_recorded)}</td><td>${yn(item.workaround_recorded)}</td></tr>`).join("") || `<tr><td colspan="6">No visible KEDB entry.</td></tr>`;
}

async function load() {
  document.getElementById("reliability-status").textContent = "Loading";
  try {
    projection = await getJSON(`/api/v1/reliability/projection?${query()}`);
    render();
  } catch (error) {
    document.getElementById("reliability-status").textContent = `Unavailable: ${error.message}`;
    document.getElementById("reliability-metric-rows").innerHTML = `<tr><td colspan="7">No reliability value is inferred.</td></tr>`;
  }
}

function initialize() {
  const parsed = parseContext();
  if (!parsed) { document.getElementById("reliability-status").textContent = "Unavailable: unsupported or protected scope"; return; }
  context = parsed;
  updateContext(context.role, "replace");
  document.getElementById("reliability-context").addEventListener("change", () => { updateContext(document.getElementById("reliability-role").value); load(); });
  window.addEventListener("popstate", () => { const next = parseContext(); if (next) { context = next; updateContext(context.role, "replace"); load(); } });
  load();
}

initialize();
