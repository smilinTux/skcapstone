// Shared API + UI helpers for the SKDashboard board.

// The operator identity + capability token this page presents on every
// privileged (write) call (Unified Consent Plane P1.3, coord card
// a638b490). Fetched once from GET /api/auth/capability and cached: today
// that endpoint hands the value back on loopback trust alone, nothing
// stronger - see its docstring in dashboard.py for exactly what does and
// does not authenticate the handout. A fetch failure (or an unconfigured
// seat) degrades to {actor: "unattributed", capability: null} rather than
// the hardcoded "operator" this card retires; "unattributed" is the fleet
// convention for a claim that cannot be backed (design doc
// 2026-08-13-unified-consent-plane-arch.md section 2.3).
let _capPromise = null;

async function getCapability() {
  if (!_capPromise) {
    _capPromise = fetch("/api/auth/capability", { headers: { "Accept": "application/json" } })
      .then((r) => (r.ok ? r.json() : {}))
      .catch(() => ({}));
  }
  const cap = await _capPromise;
  return { actor: cap.actor || "unattributed", capability: cap.capability || null };
}

// Headers every mutating call attaches: X-SK-Actor (the PDP subject) and,
// when this seat is configured with one, X-SK-Capability (queue_authz's
// staged token/pdp/both gate reads it). `extra` is merged in (e.g.
// Content-Type) so callers do not need a second object spread.
export async function authHeaders(extra) {
  const { actor, capability } = await getCapability();
  const headers = { "X-SK-Actor": actor, ...(extra || {}) };
  if (capability) headers["X-SK-Capability"] = capability;
  return headers;
}

export function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

export async function getJSON(url) {
  const r = await fetch(url, { headers: { "Accept": "application/json" } });
  if (!r.ok) throw new Error(url + " -> " + r.status);
  return r.json();
}

// POST a mutation to /api/card/<id>/<action>. Returns the JSON result.
export async function mutate(cardId, action, body) {
  const headers = await authHeaders({ "Content-Type": "application/json" });
  const r = await fetch(`/api/card/${encodeURIComponent(cardId)}/${action}`, {
    method: "POST",
    headers,
    body: JSON.stringify(body || {}),
  });
  const data = await r.json().catch(() => ({}));
  if (!r.ok || data.error) throw new Error(data.error || (action + " failed"));
  return data;
}

let _toastTimer = null;
export function toast(msg, isError) {
  const el = document.getElementById("toast");
  if (!el) return;
  el.textContent = msg;
  el.style.background = isError ? "var(--crit)" : "var(--ink)";
  el.style.color = isError ? "#fff" : "var(--bg)";
  el.classList.add("show");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove("show"), 2600);
}

export function avatarColor(name) {
  if (!name) return "var(--med)";
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) & 0xffff;
  return `hsl(${h % 360} 45% 45%)`;
}

export function timeShort(ts) {
  if (!ts) return "";
  try {
    const d = new Date(ts);
    return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  } catch (_) { return ""; }
}
