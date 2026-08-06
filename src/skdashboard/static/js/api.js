// Shared API + UI helpers for the SKDashboard board.

export const ACTOR = "operator";

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
  const r = await fetch(`/api/card/${encodeURIComponent(cardId)}/${action}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-SK-Actor": ACTOR },
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
