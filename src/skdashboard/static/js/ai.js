import { esc, getJSON } from "./api.js";
import { DEFAULT_CONTEXT, apiUrl, normalizedContext, parseUrl, responseMatches, safeSearch } from "./control_plane_scope.js";

const SOURCES = Object.freeze([
  { id: "skcounter.harness", label: "Harness-reported", unit: "tokens and USD" },
  { id: "skgateway.observed", label: "Gateway-observed", unit: "tokens and USD" },
  { id: "skjoule.wallet", label: "SKJoule accounting", unit: "Joules" },
]);
const QUALITY = Object.freeze([
  ["Accepted outcome", "accepted decision with exact outcome evidence"],
  ["Recommendation acceptance", "typed recommendation disposition"],
  ["Verified effect", "post-decision effect verification"],
  ["Evaluation quality", "versioned evaluation result and sample"],
  ["Citation coverage", "eligible claims and verified citations"],
  ["Rework", "typed rework event and cause"],
  ["Override", "typed human override decision"],
  ["Abstention", "typed abstention reason and eligible population"],
  ["Denial handling", "policy denial outcome and follow-up state"],
  ["Budget", "approved budget amount, period, and owner"],
  ["Cost per accepted outcome", "estimated USD and accepted outcome denominator"],
]);
const DRILLDOWNS = Object.freeze(["Model", "Client", "Provider", "Node", "Route", "Queue", "Cache", "Tool error", "Quality", "Cost"]);
let context = normalizedContext({ ...DEFAULT_CONTEXT });
let epoch = 0;
let lastTrigger = null;

function coverage(source) {
  const value = source.coverage || {};
  return Number.isInteger(value.reporting) && Number.isInteger(value.expected)
    ? `${value.reporting} of ${value.expected} reporting`
    : "Unknown population coverage";
}

function observed(source) {
  return source.observed_at || "Not observed";
}

function value(source, key, unit) {
  const aggregate = source.aggregate;
  if (!aggregate || aggregate[key] == null) return '<span class="ai-unknown">Unknown</span>';
  return `${esc(aggregate[key])} ${esc(unit)}`;
}

function laneCard(spec, source) {
  if (spec.id === "skjoule.wallet") {
    return `<article class="ai-lane" data-lane="${esc(spec.id)}"><h3>${esc(spec.label)}</h3><p>${esc(source.truth_state)} | ${esc(coverage(source))}</p><dl class="ai-measures"><div><dt>Joule state</dt><dd>${value(source, "total_supply", "Joules")}</dd></div><div><dt>Active agents</dt><dd>${value(source, "active_agents", "agents")}</dd></div><div><dt>Tokens</dt><dd class="ai-unknown">Not applicable</dd></div><div><dt>USD</dt><dd class="ai-unknown">Not applicable</dd></div><div><dt>Latency</dt><dd class="ai-unknown">Not projected</dd></div><div><dt>Quality</dt><dd class="ai-unknown">Unknown</dd></div><div><dt>Value</dt><dd class="ai-unknown">Unknown</dd></div></dl></article>`;
  }
  return `<article class="ai-lane" data-lane="${esc(spec.id)}"><h3>${esc(spec.label)}</h3><p>${esc(source.truth_state)} | ${esc(coverage(source))}</p><dl class="ai-measures"><div><dt>Tokens</dt><dd>${value(source, "tokens_total", "tokens")}</dd></div><div><dt>Estimated cost</dt><dd>${value(source, "cost_usd", "USD")}</dd></div><div><dt>Cost state</dt><dd>${source.aggregate ? esc(source.aggregate.cost_state) : '<span class="ai-unknown">Unknown</span>'}</dd></div><div><dt>Pricing revision</dt><dd class="ai-unknown">Not projected</dd></div><div><dt>Cost confidence</dt><dd class="ai-unknown">Not reported</dd></div><div><dt>Latency</dt><dd class="ai-unknown">Not projected</dd></div><div><dt>Quality</dt><dd class="ai-unknown">Unknown</dd></div><div><dt>Value</dt><dd class="ai-unknown">Unknown</dd></div></dl></article>`;
}

function unavailableSource(id) {
  return { adapter_id: id, owner: "Unknown", population: "Unknown", adapter_version: "Unknown", truth_state: "unavailable", observed_at: null, watermark: null, coverage: {}, aggregate: null };
}

function render(response) {
  if (!responseMatches(response, context)) throw new Error("response scope does not match the requested scope");
  const byId = new Map((response.items || []).filter((item) => item.adapter_id).map((item) => [item.adapter_id, item]));
  const sources = SOURCES.map((spec) => [spec, byId.get(spec.id) || unavailableSource(spec.id)]);
  const quality = (response.items || []).find((item) => item.projection_type === "data_quality");
  const registry = quality && quality.metric_registry || {};
  const registryText = registry.registry_version && registry.registry_hash
    ? `Metric registry ${registry.registry_version} | ${registry.registry_hash}`
    : "Metric registry version and hash unavailable";
  document.getElementById("ai-lanes").innerHTML = sources.map(([spec, source]) => laneCard(spec, source)).join("");
  const provenance = `${sources.map(([spec, source]) => `${source.adapter_id || spec.id}@${source.adapter_version || "Unknown"}; ${source.truth_state}; scope estate; window latest; watermark ${source.watermark && source.watermark.value || "Unavailable"}`).join(" | ")} | ${registryText}`;
  document.getElementById("ai-registry").textContent = registryText;
  document.getElementById("ai-quality-rows").innerHTML = QUALITY.map(([label, required]) => `<tr><td><strong>${esc(label)}</strong></td><td><span class="truth-badge unknown">Unknown</span></td><td>${label === "Cost per accepted outcome" ? "USD per accepted outcome" : "distinct typed outcome measure"}</td><td>${esc(required)}</td><td>${esc(provenance)}</td></tr>`).join("");
  document.getElementById("ai-drilldown-rows").innerHTML = DRILLDOWNS.map((dimension) => {
    const harness = dimension === "Cost" ? value(sources[0][1], "cost_usd", "USD") : '<span class="ai-unknown">Unknown</span>';
    const gateway = dimension === "Cost" ? value(sources[1][1], "cost_usd", "USD") : '<span class="ai-unknown">Unknown</span>';
    return `<tr><td><strong>${esc(dimension)}</strong><button class="quality-preview-button ai-detail-button" type="button" data-dimension="${esc(dimension)}" aria-label="Open ${esc(dimension)} drilldown">Evidence</button></td><td>${harness}<small>${esc(sources[0][1].adapter_id)} | ${esc(sources[0][1].truth_state)}</small></td><td>${gateway}<small>${esc(sources[1][1].adapter_id)} | ${esc(sources[1][1].truth_state)}</small></td><td>Whole authorized estate, latest window. Detailed values are not projected by the bounded overview.</td></tr>`;
  }).join("");
  document.getElementById("ai-provenance-rows").innerHTML = sources.map(([spec, source]) => `<tr><td class="mono">${esc(source.adapter_id || spec.id)}@${esc(source.adapter_version || "Unknown")}</td><td>${esc(source.owner)}<small>${esc(source.population)}</small></td><td><span class="truth-badge ${esc(source.truth_state)}">${esc(source.truth_state)}</span></td><td>${esc(observed(source))}</td><td>${esc(coverage(source))}</td><td class="mono">${esc(source.watermark && source.watermark.value || "Unavailable")}</td></tr>`).join("");
  document.getElementById("ai-status").textContent = `${sources.length} separate lanes | ${response.freshness && response.freshness.truth_state || "unknown"}`;
  document.querySelectorAll(".ai-detail-button").forEach((button) => button.addEventListener("click", () => {
    lastTrigger = button;
    document.getElementById("ai-detail-title").textContent = `${button.dataset.dimension} drilldown`;
    document.getElementById("ai-detail-body").innerHTML = `<dl><div><dt>Harness-reported</dt><dd>${esc(sources[0][1].adapter_id)} | ${esc(sources[0][1].truth_state)} | ${esc(coverage(sources[0][1]))}</dd></div><div><dt>Gateway-observed</dt><dd>${esc(sources[1][1].adapter_id)} | ${esc(sources[1][1].truth_state)} | ${esc(coverage(sources[1][1]))}</dd></div><div><dt>Scope</dt><dd>Whole authorized estate | latest window | no baseline</dd></div><div><dt>Metric registry</dt><dd>${esc(registryText)}</dd></div><div><dt>Boundary</dt><dd>Detailed values remain Unknown unless the bounded overview projects them.</dd></div></dl>`;
    document.getElementById("ai-detail").showModal();
  }));
}

function clear(message, status = "Unavailable") {
  epoch += 1;
  const dialog = document.getElementById("ai-detail");
  if (dialog.open) dialog.close();
  document.getElementById("ai-detail-title").textContent = "Operational evidence unavailable";
  document.getElementById("ai-detail-body").replaceChildren();
  lastTrigger = null;
  document.getElementById("ai-status").textContent = status;
  document.getElementById("ai-lanes").innerHTML = `<p>${esc(message)} No value is inferred as zero or healthy.</p>`;
  document.getElementById("ai-quality-rows").innerHTML = '<tr><td colspan="5">Outcome and evaluation evidence is unavailable.</td></tr>';
  document.getElementById("ai-drilldown-rows").innerHTML = '<tr><td colspan="4">Operational drilldowns are unavailable.</td></tr>';
  document.getElementById("ai-registry").textContent = "Metric registry version and hash unavailable.";
  document.getElementById("ai-provenance-rows").innerHTML = '<tr><td colspan="6">Source provenance is unavailable.</td></tr>';
}

async function load() {
  clear("Loading protected AI outcome evidence.", "Loading");
  const current = epoch;
  try {
    const response = await getJSON(apiUrl(context));
    if (current === epoch) render(response);
  } catch (error) {
    if (current === epoch) clear(`Protected AI outcome evidence is unavailable: ${error.message}.`);
  }
}

function apply(next, mode = "replace") {
  context = normalizedContext({ ...next, selected_silo: "", truth: "", saved_view: "" });
  document.getElementById("ai-role").value = context.role;
  const url = new URL(location.href);
  url.pathname = "/control-plane/ai";
  url.search = safeSearch(context, { includeSavedView: false });
  history[`${mode}State`]({}, "", url);
}

function initialize() {
  const parsed = parseUrl(location.search);
  if (!parsed.ok || parsed.context.saved_view || parsed.context.selected_silo || parsed.context.truth) {
    clear("The requested AI workspace scope is unsupported or stale.");
    return;
  }
  apply(parsed.context);
  document.getElementById("ai-context").addEventListener("change", () => {
    apply({ ...context, role: document.getElementById("ai-role").value }, "push");
    load();
  });
  document.getElementById("ai-detail").addEventListener("close", () => {
    if (lastTrigger && document.contains(lastTrigger)) lastTrigger.focus();
  });
  window.addEventListener("popstate", () => {
    const next = parseUrl(location.search);
    if (!next.ok || next.context.saved_view || next.context.selected_silo || next.context.truth) clear("The requested AI workspace scope is unsupported or stale.");
    else { apply(next.context); load(); }
  });
  load();
}

initialize();
