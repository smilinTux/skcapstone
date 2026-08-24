import { esc, getJSON } from "./api.js";

const BASE = Object.freeze({ role: "architect", scope: "estate", window: "latest", baseline: "none", service: "all", environment: "all" });
const ROLES = new Set(["architect", "operator", "service-owner"]);
const KEYS = new Set(Object.keys(BASE));
let context = { ...BASE };
let projection = null;
let lastTrigger = null;

function parseContext() {
  const pairs = [...new URLSearchParams(location.search).entries()];
  if (pairs.some(([key, value]) => !KEYS.has(key) || !value || value.length > 128) || new Set(pairs.map(([key]) => key)).size !== pairs.length) return null;
  const value = { ...BASE, ...Object.fromEntries(pairs) };
  return ROLES.has(value.role) && value.scope === "estate" && value.window === "latest" && value.baseline === "none" && value.service === "all" && value.environment === "all" ? value : null;
}

function query() { return new URLSearchParams(context).toString(); }
function metric(id) { return projection.metrics.find((item) => item.metric_id === id); }
function value(item) { return !item || item.value == null ? "Unknown" : `${item.value}${item.unit === "percent" ? "%" : ` ${item.unit}`}`; }
function show(value, fallback = "Unknown") { return value == null || value === "" ? fallback : String(value); }

function updateContext(role, mode = "push") {
  context = { ...context, role };
  const url = new URL(location.href);
  url.pathname = "/control-plane/architecture";
  url.search = query();
  history[`${mode}State`]({}, "", url);
  document.getElementById("architecture-role").value = role;
}

function render() {
  document.getElementById("architecture-status").textContent = `${projection.truth_state} | ${projection.projection_hash}`;
  const summary = [
    ["Visible CIs", projection.topology.visible_cis, `${projection.topology.total_cis} total folded CIs.`],
    ["Topology drift", value(metric("cmdb.configuration_drift")), "Latest verified reconciliation only."],
    ["Capacity pressure", value(metric("architecture.capacity_pressure")), "Approved aggregate only."],
    ["Exceptions", projection.exceptions.length, "Evidence, owner, and impact."],
  ];
  document.getElementById("architecture-summary").innerHTML = summary.map(([label, result, note]) => `<article><span>${esc(label)}</span><strong>${esc(result)}</strong><small>${esc(note)}</small></article>`).join("");

  document.getElementById("architecture-metric-rows").innerHTML = projection.metrics.map((item) => `<tr><th scope="row">${esc(item.label)}<small class="mono">${esc(item.metric_id)}</small></th><td><strong>${esc(value(item))}</strong><small>${esc(item.truth_state)}</small></td><td>${esc(item.definition)}<small>${esc(item.definition_version)}</small></td><td>${esc(show(item.numerator))} / ${esc(show(item.denominator))}<small>sample ${esc(item.sample_size)}</small></td><td>${esc(show(item.target))}<small>baseline ${esc(show(item.baseline))}</small></td><td>${esc(item.balancing_context)}<small>${esc(item.uncertainty)}</small></td><td>${esc(item.exclusions.join(" ") || "None recorded")}<small>${esc(item.evidence_refs.join(", ") || "Evidence unavailable")}</small></td></tr>`).join("");

  document.getElementById("architecture-exception-count").textContent = `${projection.exceptions.length} visible`;
  document.getElementById("architecture-exception-rows").innerHTML = projection.exceptions.map((item) => `<tr><th scope="row">${esc(item.exception_id)}</th><td>${item.ci_id ? `<button class="project-evidence-button" type="button" data-ci="${esc(item.ci_id)}">${esc(item.ci_id)}</button>` : "Unknown"}</td><td>${esc(item.reasons.join(", "))}</td><td>${esc(item.service_ids.join(", ") || "Unknown")}</td><td>${esc(item.decision_state)}<small>${esc(item.decision_refs.join(", ") || "No linked decision")}</small></td><td>${esc(item.evidence_refs.join(", ") || "Unavailable")}</td></tr>`).join("") || `<tr><td colspan="6">No visible architecture exception.</td></tr>`;

  document.getElementById("architecture-topology-count").textContent = `${projection.topology.visible_cis}/${projection.topology.total_cis}${projection.topology.truncated ? " truncated" : ""}`;
  document.getElementById("architecture-node-rows").innerHTML = projection.topology.nodes.map((item) => `<tr><th scope="row"><button class="project-evidence-button" type="button" data-ci="${esc(item.ci_id)}">${esc(item.name)}</button><small class="mono">${esc(item.ci_id)}</small></th><td>${esc(item.ci_type)}<small>${esc(item.status)}</small></td><td>${esc(show(item.owner))}<small>${esc(show(item.environment))}; node ${esc(show(item.node))}</small></td><td>${esc(item.freshness)}; ${item.evidence_age_seconds == null ? "age Unknown" : `${esc(item.evidence_age_seconds)} seconds`}<small>${esc(show(item.source_authority))}</small></td><td>${esc(item.reconciliation_state)}<small>scan ${esc(show(item.scan_id))}</small></td><td>${esc(item.blast_radius.dependent_count)} dependents<small>services ${esc(item.blast_radius.impacted_service_ids.join(", ") || "none visible")}; cycles ${esc(item.blast_radius.cycles.length)}${item.blast_radius.truncated ? "; truncated" : ""}</small></td><td>${esc(item.lifecycle_state)}<small>${item.unsupported ? "explicit unsupported" : "unsupported state Unknown"}</small></td><td>${esc(item.evidence_refs.join(", "))}</td></tr>`).join("") || `<tr><td colspan="8">No folded CMDB evidence.</td></tr>`;

  document.getElementById("architecture-edge-rows").innerHTML = projection.topology.edges.map((item) => `<tr><th scope="row">${esc(item.source_ci_id)}</th><td>${esc(item.relationship)}</td><td>${esc(item.target_ci_id)}</td><td>${esc(show(item.authority))}</td><td>${item.target_visible ? "Visible" : "Target unavailable"}</td><td>${esc(item.evidence_refs.join(", "))}</td></tr>`).join("") || `<tr><td colspan="6">No visible relationship.</td></tr>`;
  document.querySelectorAll("[data-ci]").forEach((button) => {
    button.addEventListener("click", () => openDetail(button.dataset.ci, button));
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); openDetail(button.dataset.ci, button); }
    });
  });
}

function openDetail(id, trigger) {
  const item = projection.topology.nodes.find((candidate) => candidate.ci_id === id);
  if (!item) return;
  lastTrigger = trigger;
  document.getElementById("architecture-detail-title").textContent = `${item.name} evidence`;
  document.getElementById("architecture-detail-body").innerHTML = `<dl class="architecture-detail-grid"><dt>CI and type</dt><dd>${esc(item.ci_id)}; ${esc(item.ci_type)}; ${esc(item.status)}</dd><dt>Owner and environment</dt><dd>${esc(show(item.owner))}; ${esc(show(item.environment))}; node ${esc(show(item.node))}</dd><dt>Evidence age</dt><dd>${esc(item.freshness)}; ${item.evidence_age_seconds == null ? "Unknown" : `${esc(item.evidence_age_seconds)} seconds`}; ${esc(show(item.observed_at))}</dd><dt>Source and reconciliation</dt><dd>${esc(show(item.source_authority))}; ${esc(item.reconciliation_state)}; scan ${esc(show(item.scan_id))}</dd><dt>Blast radius</dt><dd>${esc(item.blast_radius.dependent_count)} dependents; services ${esc(item.blast_radius.impacted_service_ids.join(", ") || "none visible")}; cycles ${esc(item.blast_radius.cycles.length)}; truncated ${esc(item.blast_radius.truncated)}</dd><dt>Lifecycle</dt><dd>${esc(item.lifecycle_state)}; unsupported ${esc(item.unsupported)}</dd><dt>Evidence</dt><dd>${esc(item.evidence_refs.join(", "))}</dd></dl>`;
  document.getElementById("architecture-detail").showModal();
}

async function load() {
  document.getElementById("architecture-status").textContent = "Loading";
  try {
    projection = await getJSON(`/api/v1/architecture/projection?${query()}`);
    render();
  } catch (error) {
    document.getElementById("architecture-status").textContent = `Unavailable: ${error.message}`;
    document.getElementById("architecture-metric-rows").innerHTML = `<tr><td colspan="7">No architecture value is inferred.</td></tr>`;
  }
}

function initialize() {
  const parsed = parseContext();
  if (!parsed) { document.getElementById("architecture-status").textContent = "Unavailable: unsupported or protected scope"; return; }
  context = parsed;
  updateContext(context.role, "replace");
  document.getElementById("architecture-context").addEventListener("change", () => { updateContext(document.getElementById("architecture-role").value); load(); });
  document.getElementById("architecture-detail").addEventListener("close", () => { if (lastTrigger) lastTrigger.focus(); });
  window.addEventListener("popstate", () => { const next = parseContext(); if (next) { context = next; updateContext(context.role, "replace"); load(); } });
  load();
}

initialize();
