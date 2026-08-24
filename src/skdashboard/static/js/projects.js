import { esc, getJSON } from "./api.js";
import {
  DEFAULT_CONTEXT, apiUrl, normalizedContext, parseUrl, responseMatches, safeSearch,
} from "./control_plane_scope.js";

const UNKNOWN_SAMPLE = "Metric sample unavailable. Adapter coverage is source reporting coverage, not a work-item sample.";
const LATEST = "Latest source observation only";
const OWNER_GAP = "Typed owner records are not projected by the protected API.";
const MISSING_WINDOW = "Unavailable: required historical measurement window not observed";

const SNAPSHOTS = [
  { id: "portfolio-total", area: "Portfolio snapshot", label: "Current portfolio total", family: "portfolio", adapter: "skcapstone.portfolio", key: "total", definition: "Count in the source portfolio snapshot.", exclusions: "No objective, benefit, value, or investment meaning is inferred." },
  { id: "portfolio-open", area: "Portfolio snapshot", label: "Current portfolio open", family: "portfolio", adapter: "skcapstone.portfolio", key: "open", definition: "Count with source status open at the latest snapshot.", exclusions: "Every other status and every unprojected record." },
  { id: "portfolio-progress", area: "Portfolio snapshot", label: "Current portfolio in-progress", family: "portfolio", adapter: "skcapstone.portfolio", key: "in_progress", definition: "Count with source status exactly in_progress at the latest snapshot.", exclusions: "Every other status and every unprojected record." },
  { id: "portfolio-done", area: "Portfolio snapshot", label: "Current portfolio done", family: "portfolio", adapter: "skcapstone.portfolio", key: "done", definition: "Count with source status done at the latest snapshot.", exclusions: "No completion window or throughput is inferred from this stock count." },
  { id: "flow-progress", area: "Agile flow context", label: "Current in-progress", family: "flow", adapter: "skcoord.flow", key: "in_progress", definition: "Count of source work items with status exactly in_progress at the latest observation.", exclusions: "Every other status and unprojected record. This is not canonical WIP or productivity." },
  { id: "flow-blocked", area: "Agile flow context", label: "Current blocked work items", family: "flow", adapter: "skcoord.flow", key: "blocked", definition: "Count of source work items with status exactly blocked at the latest observation.", exclusions: "Resolved intervals and duration. This is not blocked time." },
];

const UNKNOWN_ROWS = [
  ["owner-trace", "Outcomes and ownership", "Owner record traceability", "portfolio", "Portfolio outcomes, projects, epics, tasks, risks, dependencies, decisions, and investment trace to typed owner records.", OWNER_GAP, "Content, raw events, metadata, links, and unapproved labels are excluded."],
  ["objectives", "Outcomes and value", "Objectives", "portfolio", "Typed objectives with owner, target, result, and evidence.", OWNER_GAP, "Current portfolio status counts are not objectives."],
  ["projects", "Outcomes and ownership", "Explicit project records", "portfolio", "Folded records carrying the exact project classification.", OWNER_GAP, "No project is inferred from title, activity, team, or priority."],
  ["epics", "Outcomes and ownership", "Native epic records", "portfolio", "Folded records whose native kind is exactly epic.", OWNER_GAP, "No epic is inferred from labels or title."],
  ["tasks", "Outcomes and ownership", "Native task records", "portfolio", "Folded records whose native kind is exactly task.", OWNER_GAP, "No task value or productivity is inferred."],
  ["decisions", "Outcomes and ownership", "Explicit decision records", "portfolio", "Folded records carrying the exact decision classification.", OWNER_GAP, "A decision record does not prove decision latency or approval."],
  ["blocked-objectives", "Outcomes and value", "Blocked objectives", "portfolio", "Registered metric portfolio.blocked_objectives@1.0.0 requires an objective numerator.", "Objective records and blocked-objective numerator are absent.", "Blocked tasks are not blocked objectives."],
  ["benefits", "Outcomes and value", "Explicit benefit records", "portfolio", "Folded records carrying the exact benefit classification.", OWNER_GAP, "A classified record does not supply expected or realized benefit value."],
  ["current-value", "Outcomes and value", "Current value", "portfolio", "Owner-defined current value with unit, as-of time, and evidence.", OWNER_GAP, "No monetary or outcome value is inferred."],
  ["unrealized-value", "Outcomes and value", "Unrealized value", "portfolio", "Owner-defined target value less verified realized value.", OWNER_GAP, "No target or realized value is projected."],
  ["investment", "Outcomes and value", "Investment", "portfolio", "Authorized investment records with unit, period, and owner.", OWNER_GAP, "Cards, tokens, cost, commits, and Joules are not investment proxies."],
  ["cost-delay", "Outcomes and value", "Cost of delay", "portfolio", "Owner-defined value loss per unit time for a delayed outcome.", OWNER_GAP, "Does not default to WSJF and is not inferred from priority."],
  ["decision-latency", "Decisions", "Decision latency", "portfolio", "Elapsed time from decision-required or request timestamp to recorded decision timestamp.", "Typed decision events and timestamps are absent.", "Work status changes are not decision events."],
  ["canonical-wip", "Agile flow", "Current CardStore WIP context", "flow", "Count of authorized active folded records whose native status is exactly doing or review at projected_at.", "This is not a team productivity measure.", "Backlog, ready, Done, archived, policy-filtered, malformed, and unprojected records."],
  ["throughput", "Agile flow", "Throughput", "flow", "Count entering Done in a bounded interval.", "Completion events and a bounded measurement window are absent.", "Current done stock is not throughput."],
  ["work-age", "Agile flow", "Open record age P50, P85, P95", "flow", "Point-in-time elapsed UTC days from immutable record creation to projected_at for active non-Done folded records.", "Workflow-state entry time remains unavailable.", "Done, archived, policy-filtered, malformed, and future-created records. This is not workflow-state age or cycle time."],
  ["cycle", "Agile flow", "Cycle time P50, P85, P95", "flow", "Percentiles of start-to-Done duration for a completed sample in the window.", "Start and finish transition history is absent.", "No percentile is calculated from current counts."],
  ["blocked-time", "Agile flow", "Blocked time", "flow", "Sum of blocked intervals in the sample and window.", "Blocked and unblocked transition events are absent.", "Current blocked stock is not duration."],
  ["efficiency", "Agile flow", "Flow efficiency", "flow", "Active time divided by active plus waiting time.", "Active and waiting intervals are absent.", "No ratio is inferred from status counts."],
  ["review-coverage", "Agile flow", "Review coverage", "flow", "Registered metric flow.review_coverage@1.0.0 requires reviewed-item numerator and eligible denominator.", "Review numerator and eligible denominator are absent.", "Adapter coverage is not review coverage."],
  ["churn", "Agile flow", "Churn", "flow", "Adds, removes, reopens, and scope changes against a committed baseline in the window.", "Baseline and version history are absent.", "Current open count is not churn."],
  ["rollover", "Agile flow", "Rollover", "flow", "Sprint-committed items not Done at close and moved forward.", "Sprint commitment and close records are absent.", "No sprint is inferred from tags or current state."],
  ["sprint-goal", "Agile flow", "Sprint goal result", "flow", "Typed sprint goal result with supporting evidence.", "Goal and result records are absent.", "Completion counts do not prove a goal result."],
  ["velocity", "Agile flow", "Velocity", "flow", "Local planning context for one stable team's own estimation system.", "Sprint commitment, estimates, and close records are absent.", "Never compare teams or rank people."],
  ["dependency-stale", "Dependencies and milestones", "Stale unresolved record-activity paths", "portfolio", "Unresolved existing target whose latest valid folded record activity is older than 30 UTC days at projected_at under dependency-unresolved-30d@1.0.0.", "A missing, malformed, or future endpoint timestamp remains freshness unknown.", "Done paths are excluded. This is record-activity staleness, not verified dependency age or work-item age."],
  ["dependency-orphan", "Dependencies and milestones", "Orphaned dependency paths", "portfolio", "Dependency whose two authorized endpoint owner records cannot both be resolved.", OWNER_GAP, "Missing endpoints cannot be inferred from aggregate counts."],
  ["dependency-conflict", "Dependencies and milestones", "Conflicted dependency paths", "portfolio", "Path with a dependency cycle or explicit folded record claim-conflict evidence.", "Dependency version contradiction evidence is not projected.", "No dependency-version or owner contradiction is inferred."],
  ["dependency-gate", "Dependencies and milestones", "Human-gated paths", "portfolio", "Dependency path containing an explicit incomplete human gate record.", "Explicit gate records and paths are absent.", "Priority or blocked status is not human authorization."],
  ["milestones", "Dependencies and milestones", "Milestones", "portfolio", "Typed milestone target, owner, status, and evidence.", OWNER_GAP, "No date or forecast is inferred."],
  ["risk", "Risk", "Project and portfolio risk", "portfolio", "Typed risk with owner, exposure, response, and review state.", OWNER_GAP, "Blocked counts and partial sources are not risk scores."],
  ["forecast", "Forecast inputs", "Forecast inputs", "flow", "Throughput and cycle distributions, dependency paths, milestones, and calibration required for forecasting.", "All historical distributions, paths, dates, and calibration are absent.", "Current status counts are contextual inputs only. No forecast range is produced."],
].map(([id, area, label, family, definition, reason, exclusions]) => ({ id, area, label, family, definition, reason, exclusions }));

let context = normalizedContext({ ...DEFAULT_CONTEXT, role: "project-manager" });
let loadEpoch = 0;
let blocked = false;
let evidence = new Map();

function sourceCoverage(item) {
  const coverage = item && item.coverage || {};
  if (!Number.isInteger(coverage.reporting) || !Number.isInteger(coverage.expected)) return "Source coverage unavailable";
  return `${coverage.reporting} of ${coverage.expected} reporting in ${item.population}; not a metric sample`;
}

function projectCoverage(project) {
  const counts = project && project.population_counts || {};
  return `${counts.authorized_ids ?? "Unknown"} authorized IDs; ${counts.folded ?? "Unknown"} folded; ${counts.emitted_records ?? "Unknown"} records emitted${project && project.truncated ? "; bounded output is partial" : ""}`;
}

const PROJECT_SIGNALS = new Set([
  "owner-trace", "objectives", "projects", "epics", "tasks", "decisions", "benefits", "investment", "dependency-orphan",
  "canonical-wip", "dependency-stale", "dependency-conflict", "dependency-gate", "milestones", "risk",
]);

function projectCount(project, row) {
  const records = project.records || [];
  const populations = project.population_counts || {};
  const edges = project.dependency_edges || [];
  const classifications = (name) => records.filter((record) => (record.classifications || []).includes(name)).length;
  const kinds = (name) => records.filter((record) => record.kind === name).length;
  const conditions = (name) => edges.filter((edge) => (edge.conditions || []).includes(name)).length;
  const resolutions = (name) => edges.filter((edge) => edge.resolution === name).length;
  const counts = {
    "owner-trace": populations.emitted_records ?? records.length,
    objectives: classifications("objective"),
    projects: classifications("project"),
    epics: kinds("epic"),
    tasks: kinds("task"),
    decisions: classifications("decision"),
    benefits: classifications("benefit"),
    investment: classifications("investment"),
    "dependency-stale": conditions("stale"),
    "dependency-orphan": resolutions("orphaned"),
    "dependency-conflict": conditions("conflicted"),
    "dependency-gate": conditions("human_gated"),
    milestones: populations.explicit_milestones ?? classifications("milestone"),
    risk: classifications("risk"),
    "canonical-wip": records.filter((record) => ["doing", "review"].includes(record.status)).length,
  };
  return counts[row.id];
}

function requiredContract(row) {
  if (["benefits", "current-value", "unrealized-value", "cost-delay", "blocked-objectives"].includes(row.id)) return "Typed outcome and value owner-record contract";
  if (row.id === "decision-latency") return "Typed decision event and measurement-window contract";
  if (["rollover", "sprint-goal", "velocity"].includes(row.id)) return "Typed sprint commitment, estimate, goal, and close contract";
  if (row.id === "forecast") return "Typed calibrated forecast-input contract";
  return "Typed flow transition and measurement-window contract";
}

function rowState(row, byId, project) {
  if (row.adapter) {
    const source = byId.get(row.adapter);
    const available = source.aggregate && source.aggregate[row.key] != null;
    return {
      result: available ? String(source.aggregate[row.key]) : "Unknown",
      truth: available ? source.truth_state : "unknown",
      sample: `${UNKNOWN_SAMPLE} ${sourceCoverage(source)}.`,
      window: LATEST,
      sourceLabel: row.adapter,
      evidenceType: "adapter",
      sources: [source],
    };
  }
  if (PROJECT_SIGNALS.has(row.id) && project.truth_state !== "unavailable") {
    const count = projectCount(project, row);
    const result = project.truncated || project.classification_complete === false ? `Observed ${count} in emitted subset` : String(count);
    return {
      result,
      truth: project.truth_state,
      sample: projectCoverage(project),
      window: "Current folded record projection; no historical metric window",
      sourceLabel: "SKCoord CardStore folded records",
      evidenceType: "project",
      project,
    };
  }
  return {
    result: "Unknown",
    truth: "unknown",
    sample: `${UNKNOWN_SAMPLE} Required owner population is unavailable.`,
    window: MISSING_WINDOW,
    sourceLabel: requiredContract(row),
    evidenceType: "missing",
    contract: requiredContract(row),
  };
}

function applyContext(next, historyMode) {
  context = normalizedContext({ ...next, saved_view: "" });
  blocked = false;
  document.getElementById("project-role").value = context.role;
  document.getElementById("project-silo").value = context.selected_silo;
  document.getElementById("project-truth").value = context.truth;
  const url = new URL(location.href);
  url.pathname = "/control-plane/portfolio";
  url.search = safeSearch(context, { includeSavedView: false });
  if (historyMode === "push") history.pushState({}, "", url);
  if (historyMode === "replace") history.replaceState({}, "", url);
}

function clearWorkspace(message, loading = false) {
  evidence = new Map();
  const dialog = document.getElementById("project-evidence");
  if (dialog.open) dialog.close();
  document.getElementById("project-evidence-title").textContent = "Measurement evidence unavailable";
  document.getElementById("project-evidence-body").replaceChildren();
  document.getElementById("project-rows").innerHTML = loading
    ? `<tr><td colspan="7"><div class="spinner" aria-label="Loading portfolio evidence"></div></td></tr>`
    : `<tr><td colspan="7" class="quality-empty">${esc(message)} No result is assumed healthy or zero.</td></tr>`;
  document.getElementById("project-status").textContent = loading ? "Loading" : "Unavailable";
  document.getElementById("record-rows").innerHTML = `<tr><td colspan="8" class="quality-empty">${loading ? "Loading owner records." : "Protected owner records are unavailable."}</td></tr>`;
  document.getElementById("dependency-rows").innerHTML = `<tr><td colspan="8" class="quality-empty">${loading ? "Loading dependency paths." : "Protected dependency paths are unavailable."}</td></tr>`;
  document.getElementById("milestone-rows").innerHTML = `<tr><td colspan="6" class="quality-empty">${loading ? "Loading milestone records." : "Protected milestone records are unavailable."}</td></tr>`;
  document.getElementById("record-status").textContent = loading ? "Loading" : "Unavailable";
  document.getElementById("dependency-status").textContent = loading ? "Loading" : "Unavailable";
  for (const id of ["summary-in-progress", "summary-blocked", "summary-history", "summary-dependencies"]) {
    document.getElementById(id).textContent = loading ? "Loading" : "Unavailable";
  }
}

function blockContext(state, message) {
  loadEpoch += 1;
  blocked = true;
  context = normalizedContext({ ...DEFAULT_CONTEXT, role: "project-manager" });
  const url = new URL(location.href);
  url.pathname = "/control-plane/portfolio";
  url.search = safeSearch(context, { includeSavedView: false });
  history.replaceState({}, "", url);
  clearWorkspace(`${state}: ${message} No protected value was retained.`);
}

function rowTruth(row, byId, project) {
  return rowState(row, byId, project).truth;
}

function rowVisible(row, byId, project) {
  if (context.selected_silo && row.family !== context.selected_silo) return false;
  return !context.truth || rowTruth(row, byId, project) === context.truth;
}

function evidenceButton(id, label) {
  return `<button class="quality-preview-button project-evidence-button" type="button" data-evidence="${esc(id)}" aria-label="Evidence for ${esc(label)}">Evidence</button>`;
}

function bindEvidenceButtons() {
  document.querySelectorAll(".project-evidence-button").forEach((button) => {
    button.addEventListener("click", () => openEvidence(button.dataset.evidence, button));
    button.addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); openEvidence(button.dataset.evidence, button); }
    });
  });
}

function renderRecords(project) {
  const records = project.records || [];
  document.getElementById("record-rows").innerHTML = records.map((record) => {
    const id = `record:${record.record_id}`;
    evidence.set(id, { evidenceType: "record", label: `Record ${record.record_id}`, record, project });
    return `<tr><td class="mono">${esc(record.record_id)}<small>${esc(record.source_ref)}</small></td><td>${esc(record.kind)}<small>${esc(record.classifications.join(", ") || "No allowed classification")}</small></td><td>${esc(record.status)}<small>Priority ${esc(record.priority)}</small></td><td>${esc(record.owner || "Unassigned")}</td><td>${esc(record.archived ? "Archived evidence" : "Active record")}</td><td>${esc(record.visible_dependency_count)} visible dependencies</td><td>${esc(record.created_at || "Unavailable")}<small>${esc(record.updated_at || "Unavailable")}</small></td><td>${evidenceButton(id, `record ${record.record_id}`)}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="quality-empty">No folded owner record is projected in this authorized population.</td></tr>`;
  document.getElementById("record-status").textContent = `${projectCoverage(project)} | ${project.truth_state}`;
}

function renderPaths(project) {
  const edges = project.dependency_edges || [];
  document.getElementById("dependency-rows").innerHTML = edges.map((edge, index) => {
    const id = `edge:${index}`;
    evidence.set(id, { evidenceType: "edge", label: `Dependency ${edge.from_record_id} to ${edge.to_record_id || "policy-filtered target"}`, edge, project });
    const conditions = edge.conditions || [];
    return `<tr><td class="mono">${esc(edge.from_record_id)} to ${esc(edge.to_record_id)}<small>${esc((edge.path_record_ids || []).join(" to ") || "No resolved path")}</small></td><td>${esc(edge.source_owner || "Unassigned")} to ${esc(edge.target_owner || "Unresolved")}</td><td>${esc(edge.target_status || "Unavailable")}<small>${esc(edge.resolution)}</small></td><td>${conditions.includes("stale") ? `Yes: ${esc(edge.stale_rule)}` : (conditions.includes("freshness_unknown") ? "Unknown: timestamp unavailable" : "No")}</td><td>${edge.resolution === "orphaned" ? "Yes" : "No"}</td><td>${conditions.includes("conflicted") ? "Yes" : "No"}</td><td>${conditions.includes("human_gated") ? "Yes" : "No"}</td><td>${evidenceButton(id, `dependency ${edge.from_record_id} to ${edge.to_record_id}`)}</td></tr>`;
  }).join("") || `<tr><td colspan="8" class="quality-empty">No direct dependency edge is projected in this authorized population.</td></tr>`;
  const milestones = project.milestones || [];
  document.getElementById("milestone-rows").innerHTML = milestones.map((record) => {
    const id = `milestone:${record.record_id}`;
    evidence.set(id, { evidenceType: "milestone", label: `Milestone ${record.record_id}`, record, project });
    const path = record.dependency_path_summary || {};
    const conditions = path.conditions || {};
    const conditionText = Object.entries(conditions).filter(([, count]) => count).map(([name, count]) => `${name}: ${count}`).join(", ") || "No classified path condition";
    return `<tr><td class="mono">${esc(record.record_id)}<small>${esc((path.path_record_ids || []).join(" to ") || "No dependency path")}</small></td><td>${esc(record.owner || "Unassigned")}</td><td>${esc(record.status)}<small>${esc(conditionText)}</small></td><td>${esc(record.created_at || "Unavailable")}</td><td>${esc(record.updated_at || "Unavailable")}</td><td>${evidenceButton(id, `milestone ${record.record_id}`)}</td></tr>`;
  }).join("") || `<tr><td colspan="6" class="quality-empty">No explicitly classified milestone record is projected.</td></tr>`;
  const counts = project.population_counts || {};
  document.getElementById("dependency-status").textContent = `${counts.visible_edges ?? edges.length} visible paths | ${counts.explicit_milestones ?? milestones.length} milestones | ${project.truth_state}`;
}

function render(response) {
  const byId = new Map(response.items.filter((item) => item.adapter_id).map((item) => [item.adapter_id, item]));
  const project = response.items.find((item) => item.projection_type === "project_records");
  if (!byId.has("skcapstone.portfolio") || !byId.has("skcoord.flow")) throw new Error("Required portfolio and flow sources are unavailable");
  if (!project || project.truth_state === "unavailable" || !responseMatches({ scope: project.scope }, context) || !Array.isArray(project.records) || !Array.isArray(project.dependency_edges) || !Array.isArray(project.milestones)) throw new Error("Required bounded project record projection is unavailable or scope-mismatched");
  evidence = new Map();
  const rows = [...SNAPSHOTS, ...UNKNOWN_ROWS].filter((row) => rowVisible(row, byId, project));
  document.getElementById("project-rows").innerHTML = rows.map((row) => {
    const state = rowState(row, byId, project);
    evidence.set(row.id, { ...row, ...state });
    return `<tr data-signal="${esc(row.id)}" data-family="${esc(row.family)}">
      <td><span class="project-area">${esc(row.area)}</span><strong>${esc(row.label)}</strong></td>
      <td><strong>${esc(state.result)}</strong><small class="truth-badge ${esc(state.truth)}">${esc(state.truth.replace("_", " "))}</small>${row.reason && state.evidenceType !== "project" ? `<small>${esc(row.reason)}</small>` : ""}</td>
      <td>${esc(row.definition)}</td><td>${esc(state.sample)}</td><td>${esc(state.window)}</td><td>${esc(row.exclusions)}</td>
      <td><small>${esc(state.sourceLabel)}</small>${evidenceButton(row.id, row.label)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="7" class="quality-empty">No project signal matches this presentation filter. No hidden result is inferred.</td></tr>`;
  renderRecords(project);
  renderPaths(project);
  bindEvidenceButtons();
  const flow = byId.get("skcoord.flow");
  const showFlow = (!context.selected_silo || context.selected_silo === "flow") && (!context.truth || context.truth === flow.truth_state);
  document.getElementById("summary-in-progress").textContent = showFlow && flow.aggregate && flow.aggregate.in_progress != null ? String(flow.aggregate.in_progress) : "Unavailable";
  document.getElementById("summary-blocked").textContent = showFlow && flow.aggregate && flow.aggregate.blocked != null ? String(flow.aggregate.blocked) : "Unavailable";
  document.getElementById("summary-history").textContent = "Unknown";
  const edgeCount = (project.dependency_edges || []).length;
  document.getElementById("summary-dependencies").textContent = project.truncated ? `At least ${edgeCount}` : String(edgeCount);
  document.getElementById("project-status").textContent = `${rows.length} signals | ${new Set(rows.map((row) => row.family)).size} areas`;
}

function openEvidence(id, trigger) {
  const item = evidence.get(id);
  if (!item) return;
  document.getElementById("project-evidence-title").textContent = `${item.label} evidence`;
  if (item.evidenceType === "adapter") {
    const provenance = item.sources.map((source) => `<tr><th scope="row" class="mono">${esc(source.adapter_id)}@${esc(source.adapter_version)}</th><td>${esc(source.owner)}</td><td>${esc(source.truth_state)}</td><td>${esc(source.observed_at || "Not observed")}</td><td class="mono">${esc(source.watermark && source.watermark.value || "Unavailable")}</td><td>${esc(sourceCoverage(source))}</td></tr>`).join("");
    document.getElementById("project-evidence-body").innerHTML = `<dl><div><dt>Result and truth</dt><dd>${esc(item.result)}; ${esc(item.truth)}</dd></div><div><dt>Workspace definition</dt><dd>${esc(item.definition)}</dd></div><div><dt>Metric sample</dt><dd>${UNKNOWN_SAMPLE}</dd></div><div><dt>Window</dt><dd>${esc(item.window)}; no comparable baseline</dd></div><div><dt>Exclusions</dt><dd>${esc(item.exclusions)}</dd></div></dl><div class="project-table-wrap"><table><caption>Protected source provenance</caption><thead><tr><th scope="col">Source</th><th scope="col">Owner</th><th scope="col">Truth</th><th scope="col">Observed</th><th scope="col">Watermark</th><th scope="col">Source coverage</th></tr></thead><tbody>${provenance}</tbody></table></div>`;
  } else if (item.evidenceType === "missing") {
    document.getElementById("project-evidence-body").innerHTML = `<dl><div><dt>Result and truth</dt><dd>Unknown; unknown</dd></div><div><dt>Required source contract</dt><dd>${esc(item.contract)}</dd></div><div><dt>Missing input</dt><dd>${esc(item.reason)}</dd></div><div><dt>Window</dt><dd>${esc(item.window)}</dd></div><div><dt>Exclusions</dt><dd>${esc(item.exclusions)}</dd></div></dl><p>No adapter is cited because the current aggregate readers do not observe this question.</p>`;
  } else {
    const project = item.project;
    const detail = item.evidenceType === "edge" ? item.edge : item.record;
    document.getElementById("project-evidence-body").innerHTML = `<dl><div><dt>Projection</dt><dd>SKCoord CardStore folded records ${esc(project.schema_version)}</dd></div><div><dt>Truth and coverage</dt><dd>${esc(project.truth_state)}; ${esc(projectCoverage(project))}</dd></div><div><dt>Observed</dt><dd>${esc(project.observed_at || "Unavailable")}</dd></div><div><dt>Projected</dt><dd>${esc(project.projected_at || "Unavailable")}</dd></div><div><dt>Watermark</dt><dd class="mono">${esc(project.watermark && project.watermark.value || "Unavailable")}</dd></div><div><dt>Safe record evidence</dt><dd><pre>${esc(JSON.stringify(detail, null, 2))}</pre></dd></div></dl><p>Content fields, raw events, metadata, links, and unapproved labels are excluded.</p>`;
  }
  const dialog = document.getElementById("project-evidence");
  dialog._trigger = trigger;
  dialog.showModal();
}

async function load() {
  if (blocked) return;
  const epoch = ++loadEpoch;
  clearWorkspace("Loading", true);
  try {
    const response = await getJSON(apiUrl(context));
    if (epoch !== loadEpoch) return;
    if (!responseMatches(response, context)) throw new Error("Response scope did not match the requested scope");
    render(response);
  } catch (error) {
    if (epoch === loadEpoch) clearWorkspace(`Protected project evidence is unavailable: ${error.message}.`);
  }
}

function parseLocation() {
  if (!location.search) return { ok: true, context: normalizedContext({ ...DEFAULT_CONTEXT, role: "project-manager" }) };
  const parsed = parseUrl(location.search);
  if (parsed.ok && parsed.context.saved_view) return { ok: false, state: "stale", message: "Saved views are not available for this workspace." };
  return parsed;
}

function initialize() {
  const parsed = parseLocation();
  if (!parsed.ok) { blockContext(parsed.state, parsed.message); return false; }
  applyContext(parsed.context, "replace");
  document.getElementById("project-context").addEventListener("change", () => {
    applyContext({ ...context, role: document.getElementById("project-role").value, selected_silo: document.getElementById("project-silo").value, truth: document.getElementById("project-truth").value }, "push");
    load();
  });
  window.addEventListener("popstate", () => {
    const next = parseLocation();
    if (!next.ok) blockContext(next.state, next.message);
    else { applyContext(next.context, "none"); load(); }
  });
  const dialog = document.getElementById("project-evidence");
  dialog.addEventListener("close", () => { if (dialog._trigger) dialog._trigger.focus(); });
  return true;
}

if (initialize()) load();
setInterval(load, 30000);
