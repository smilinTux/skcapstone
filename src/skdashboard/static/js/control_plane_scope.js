export const SCOPE_SCHEMA = "1.0.0";
export const REGISTRY_VERSION = "1.0.0";
export const REGISTRY_HASH = "sha256:198f5cfb5b42a67e52cdb00f17f8620fdc1dd4f3e28fb7bc9eb1f893e5baacfb";
export const STORAGE_KEY = "skdashboard.control-plane.saved-views.v1";
export const VIEW_TTL_MS = 24 * 60 * 60 * 1000;
export const MAX_CLOCK_SKEW_MS = 60 * 1000;
export const MAX_VIEWS = 8;
export const SILOS = ["portfolio", "flow", "itil", "delivery", "architecture", "fleet", "ai", "economy", "governance", "legal", "corpus", "operator"];
export const TRUTH_STATES = ["current", "stale", "partial", "unavailable", "unreachable", "unknown", "not_applicable"];
export const DEFAULT_CONTEXT = Object.freeze({
  role: "operator", scope: "estate", window: "latest", baseline: "none", service: "all",
  selected_silo: "", truth: "", saved_view: "",
});

const ROLES = new Set(["operator", "project-manager", "architect"]);
const URL_KEYS = new Set(["role", "scope", "window", "baseline", "service", "selected_silo", "truth", "saved_view"]);
const PROTECTED_KEYS = new Set(["tenant_id", "matter_id"]);
const SECRET_KEY = /(authorization|bearer|capability|secret|token|password|policy|credential|session)/i;
const VIEW_ID = /^sv-[0-9a-f]{32}$/;
const VIEW_KEYS = ["schema_version", "id", "label", "created_at", "expires_at", "route", "context", "filters", "presentation", "registry_version", "registry_hash"];

function exactKeys(value, expected) {
  return value && typeof value === "object" && !Array.isArray(value)
    && Object.keys(value).sort().join("|") === [...expected].sort().join("|");
}

export function normalizedContext(value = {}) {
  return {
    role: value.role || DEFAULT_CONTEXT.role,
    scope: value.scope || DEFAULT_CONTEXT.scope,
    window: value.window || DEFAULT_CONTEXT.window,
    baseline: value.baseline || DEFAULT_CONTEXT.baseline,
    service: value.service || DEFAULT_CONTEXT.service,
    selected_silo: value.selected_silo || "",
    truth: value.truth || "",
    saved_view: value.saved_view || "",
  };
}

export function validateContext(value) {
  const context = normalizedContext(value);
  if (!ROLES.has(context.role) || context.scope !== "estate" || context.window !== "latest"
    || context.baseline !== "none" || context.service !== "all") return false;
  if (context.selected_silo && !SILOS.includes(context.selected_silo)) return false;
  if (context.truth && !TRUTH_STATES.includes(context.truth)) return false;
  return !context.saved_view || VIEW_ID.test(context.saved_view);
}

export function safeSearch(context, { includeSavedView = true } = {}) {
  const value = normalizedContext(context);
  const query = new URLSearchParams({
    role: value.role, scope: value.scope, window: value.window,
    baseline: value.baseline, service: value.service,
  });
  if (value.selected_silo) query.set("selected_silo", value.selected_silo);
  if (value.truth) query.set("truth", value.truth);
  if (includeSavedView && value.saved_view) query.set("saved_view", value.saved_view);
  return query.toString();
}

export function apiUrl(context) {
  return `/api/v1/overview?${safeSearch(context)}`;
}

export function shareUrl(context) {
  const url = new URL(location.origin + "/control-plane/now");
  url.search = safeSearch(context, { includeSavedView: false });
  return url.toString();
}

function rawViews(storage) {
  try {
    const value = JSON.parse(storage.getItem(STORAGE_KEY) || "[]");
    return Array.isArray(value) ? value : null;
  } catch (_error) {
    return null;
  }
}

function validateView(view, now) {
  if (!exactKeys(view, VIEW_KEYS) || view.schema_version !== SCOPE_SCHEMA
    || !VIEW_ID.test(view.id) || typeof view.label !== "string" || view.label.length > 80
    || view.route !== "/control-plane/now" || view.registry_version !== REGISTRY_VERSION
    || view.registry_hash !== REGISTRY_HASH || !exactKeys(view.context, ["role", "scope", "window", "baseline", "service"])
    || !exactKeys(view.filters, ["selected_silo", "truth"])
    || !exactKeys(view.presentation, ["workspace"]) || view.presentation.workspace !== "now") return "stale";
  const context = normalizedContext({ ...view.context, ...view.filters, saved_view: view.id });
  if (!validateContext(context)) return "stale";
  const created = Date.parse(view.created_at), expires = Date.parse(view.expires_at);
  if (!Number.isFinite(created) || !Number.isFinite(expires) || expires - created !== VIEW_TTL_MS) return "stale";
  if (created > now + MAX_CLOCK_SKEW_MS) return "stale";
  if (expires <= now) return "expired";
  return "valid";
}

export function listViews(storage = localStorage, now = Date.now()) {
  const views = rawViews(storage);
  if (!views) return [];
  return views.slice(0, MAX_VIEWS).filter((view) => validateView(view, now) === "valid");
}

export function parseUrl(search, storage = localStorage, now = Date.now()) {
  const query = new URLSearchParams(search);
  const pairs = [...query.entries()];
  const counts = new Map();
  for (const [key, value] of pairs) {
    counts.set(key, (counts.get(key) || 0) + 1);
    if (PROTECTED_KEYS.has(key) || SECRET_KEY.test(key)) return { ok: false, state: "unauthorized", message: "Protected scope is unavailable." };
    if (!URL_KEYS.has(key) || !value || value.length > 128) return { ok: false, state: "stale", message: "This deep link is unsupported or stale." };
  }
  if ([...counts.values()].some((count) => count !== 1)) return { ok: false, state: "stale", message: "This deep link is unsupported or stale." };
  const context = normalizedContext(Object.fromEntries(pairs));
  if (!validateContext(context)) return { ok: false, state: "stale", message: "This deep link is unsupported or stale." };
  if (!context.saved_view) return { ok: true, context };

  const views = rawViews(storage);
  if (!views) return { ok: false, state: "stale", message: "The saved view is invalid." };
  const view = views.find((candidate) => candidate && candidate.id === context.saved_view);
  if (!view) return { ok: false, state: "unavailable", message: "The saved view is not available in this browser." };
  const state = validateView(view, now);
  if (state !== "valid") return { ok: false, state, message: state === "expired" ? "The saved view expired." : "The saved view is stale." };
  const stored = normalizedContext({ ...view.context, ...view.filters, saved_view: view.id });
  if (safeSearch(stored) !== safeSearch(context)) return { ok: false, state: "stale", message: "The saved view does not match this deep link." };
  return { ok: true, context: stored, view };
}

function viewId() {
  return `sv-${crypto.randomUUID().replaceAll("-", "")}`;
}

export function saveView(context, storage = localStorage, now = Date.now()) {
  const normalized = normalizedContext({ ...context, saved_view: "" });
  if (!validateContext(normalized)) throw new Error("Cannot save an unsupported scope");
  const created = new Date(now);
  const selected = normalized.selected_silo || "whole estate";
  const truth = normalized.truth ? `, ${normalized.truth}` : "";
  const view = {
    schema_version: SCOPE_SCHEMA,
    id: viewId(),
    label: `${selected}${truth}`,
    created_at: created.toISOString(),
    expires_at: new Date(now + VIEW_TTL_MS).toISOString(),
    route: "/control-plane/now",
    context: {
      role: normalized.role, scope: normalized.scope, window: normalized.window,
      baseline: normalized.baseline, service: normalized.service,
    },
    filters: { selected_silo: normalized.selected_silo, truth: normalized.truth },
    presentation: { workspace: "now" },
    registry_version: REGISTRY_VERSION,
    registry_hash: REGISTRY_HASH,
  };
  const existing = listViews(storage, now).filter((candidate) => candidate.id !== view.id);
  storage.setItem(STORAGE_KEY, JSON.stringify([view, ...existing].slice(0, MAX_VIEWS)));
  return view;
}

export function removeView(id, storage = localStorage) {
  const views = rawViews(storage) || [];
  storage.setItem(STORAGE_KEY, JSON.stringify(views.filter((view) => view && view.id !== id).slice(0, MAX_VIEWS)));
}

export function responseMatches(response, context) {
  const expected = Object.fromEntries(new URLSearchParams(safeSearch(context)));
  const actual = response && response.scope;
  return exactKeys(actual, Object.keys(expected))
    && Object.entries(expected).every(([key, value]) => actual[key] === value);
}
