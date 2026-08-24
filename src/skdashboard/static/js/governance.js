import { esc, getJSON } from "./api.js";

const BASE = Object.freeze({ role: "governance", scope: "estate", window: "latest", baseline: "none", service: "all" });
const ROLES = new Set(["governance", "auditor", "operator"]);
const KEYS = new Set(Object.keys(BASE));
let context = { ...BASE };

function parseContext() {
  const pairs = [...new URLSearchParams(location.search).entries()];
  if (pairs.some(([key, value]) => !KEYS.has(key) || !value || value.length > 128) || new Set(pairs.map(([key]) => key)).size !== pairs.length) return null;
  const value = { ...BASE, ...Object.fromEntries(pairs) };
  return ROLES.has(value.role) && value.scope === "estate" && value.window === "latest" && value.baseline === "none" && value.service === "all" ? value : null;
}

function query() { return new URLSearchParams(context).toString(); }
function show(value, fallback = "Unknown") { return value == null || value === "" ? fallback : String(value); }
function coverage(value) { return value.expected == null || value.reporting == null ? "Unknown" : `${value.reporting}/${value.expected}`; }

function updateContext(role, mode = "push") {
  context = { ...context, role };
  const url = new URL(location.href);
  url.pathname = "/control-plane/governance";
  url.search = query();
  history[`${mode}State`]({}, "", url);
  document.getElementById("governance-role").value = role;
}

function render(projection) {
  document.getElementById("governance-status").textContent = `${projection.truth_state} | ${projection.projection_hash}`;
  const quality = projection.data_quality.coverage;
  const summary = [
    ["Metric definitions", projection.registry.definition_hashes ? Object.keys(projection.registry.definition_hashes).length : "Unknown", projection.registry.registry_version],
    ["Source coverage", quality.percent == null ? "Unknown" : `${quality.percent}%`, `${quality.reporting}/${quality.expected} sources reporting.`],
    ["Policy state", projection.policy.available === true ? "Available" : "Unknown", `${show(projection.policy.denials)} aggregate denials; ${projection.policy.source_truth_state}.`],
    ["Findings", projection.findings.length, "Evidence and preview only."],
  ];
  document.getElementById("governance-summary").innerHTML = summary.map(([label, result, note]) => `<article><span>${esc(label)}</span><strong>${esc(result)}</strong><small>${esc(note)}</small></article>`).join("");

  document.getElementById("finding-count").textContent = `${projection.findings.length} visible`;
  document.getElementById("finding-rows").innerHTML = projection.findings.map((item) => `<tr><th scope="row" class="mono">${esc(item.finding_id)}</th><td>${esc(item.category)}<small>${esc(item.truth_state)}</small></td><td>${esc(show(item.owner))}<small>${esc(item.severity)}</small></td><td>${esc(item.due_state)}</td><td>${esc(item.safe_detail)}</td><td>${esc(item.evidence_refs.join(", ") || "Unavailable")}</td><td>${esc(item.remediation_preview.kind)}<small>Preview only; dispatch ${esc(item.remediation_preview.dispatch_authorized)}</small></td></tr>`).join("") || `<tr><td colspan="7">No visible governance finding.</td></tr>`;

  document.getElementById("lineage-count").textContent = `${projection.metric_lineage.length} definitions`;
  document.getElementById("lineage-rows").innerHTML = projection.metric_lineage.map((item) => `<tr><th scope="row">${esc(item.label)}<small class="mono">${esc(item.metric_id)}</small></th><td>${esc(item.definition_version)}<small class="mono">${esc(item.definition_hash)}</small></td><td>${esc(item.authoritative_owner)}<small class="mono">${esc(item.adapter_id)}</small></td><td>${esc(item.classification)}<small>${esc(show(item.classification_policy_decision_ref, "No protected decision required"))}</small></td><td>${esc(item.calculation.method)}<small>${esc(item.calculation.expression)}; ${esc(item.calculation.calculation_ref)}</small></td><td>${esc(item.source_truth_state)}<small class="mono">${esc(show(item.source_watermark.value))}</small></td><td>${esc(item.human_review.state)}<small>${esc(item.human_review.reason)} ${esc(item.history.state)}</small></td></tr>`).join("");

  document.getElementById("source-count").textContent = `${projection.source_lineage.length} declared`;
  document.getElementById("source-rows").innerHTML = projection.source_lineage.map((item) => `<tr><th scope="row" class="mono">${esc(item.adapter_id)}@${esc(item.adapter_version)}</th><td>${esc(item.authoritative_owner)}<small>${esc(item.population)}</small></td><td>${esc(item.classification)}<small>${esc(item.visibility.state)}; ${esc(item.visibility.authorization)}</small></td><td>${esc(item.truth_state)}<small>${item.age_seconds == null ? "Age Unknown" : `${esc(item.age_seconds)} seconds`}; TTL ${esc(item.ttl_seconds)}</small></td><td>${esc(coverage(item.coverage))}</td><td class="mono">${esc(show(item.watermark.value))}</td><td>${esc(item.safe_errors.map((value) => `${value.code}: ${value.message}`).join("; ") || "None")}</td></tr>`).join("");

  document.getElementById("history-count").textContent = `${projection.correction_history.length} records`;
  document.getElementById("history-rows").innerHTML = projection.correction_history.map((item) => `<tr><th scope="row" class="mono">${esc(item.record_id)}</th><td class="mono">${esc(item.target_id)}</td><td>${esc(item.kind)}<small>${esc(item.action)}</small></td><td>${esc(item.attributed_to)}</td><td>${esc(item.recorded_at)}</td><td class="mono">${esc(item.append_only_ref)}</td></tr>`).join("") || `<tr><td colspan="6">No correction or supersession event is visible.</td></tr>`;
}

async function load() {
  document.getElementById("governance-status").textContent = "Loading";
  try {
    render(await getJSON(`/api/v1/governance/projection?${query()}`));
  } catch (error) {
    document.getElementById("governance-status").textContent = `Unavailable: ${error.message}`;
    document.getElementById("finding-rows").innerHTML = `<tr><td colspan="7">No governance value is inferred.</td></tr>`;
  }
}

function initialize() {
  const parsed = parseContext();
  if (!parsed) { document.getElementById("governance-status").textContent = "Unavailable: unsupported or protected scope"; return; }
  context = parsed;
  updateContext(context.role, "replace");
  document.getElementById("governance-context").addEventListener("change", () => { updateContext(document.getElementById("governance-role").value); load(); });
  window.addEventListener("popstate", () => { const next = parseContext(); if (next) { context = next; updateContext(context.role, "replace"); load(); } });
  load();
}

initialize();
