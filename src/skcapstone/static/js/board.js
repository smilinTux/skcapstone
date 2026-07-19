// SKDashboard interactive kanban board: render + drag-drop + filters + live SSE.
import { esc, getJSON, mutate, toast, avatarColor } from "./api.js";
import { initPanel, openCard, refreshIfOpen, closePanel } from "./editor.js";

const COL_LABEL = { backlog: "Backlog", ready: "Ready", doing: "In Progress", review: "Review", done: "Done" };
const LANE_LABEL = {
  feature: ["Feature", "task"], bug: ["Bug", "task"], security: ["Security", "task"],
  expedite: ["Expedite", "incident"], change: ["Change", "change"], problem: ["Problem", "problem"],
};

let _board = null;         // last fetched board
const _sortables = [];     // active Sortable instances
let _dragging = false;

const filters = { text: "", owner: "", kind: "", priority: "" };

async function loadBoard() {
  try {
    _board = await getJSON("/api/kanban");
    render();
  } catch (e) {
    document.getElementById("board").innerHTML =
      `<div class="emptymsg">Failed to load board: ${esc(e.message)}</div>`;
  }
}

function passesFilter(c) {
  if (filters.owner && c.owner !== filters.owner) return false;
  if (filters.kind && c.kind !== filters.kind) return false;
  if (filters.priority && c.priority !== filters.priority) return false;
  if (filters.text) {
    const q = filters.text.toLowerCase();
    if (!(c.title || "").toLowerCase().includes(q) && !(c.id || "").toLowerCase().includes(q)) return false;
  }
  return true;
}

function cardHTML(c) {
  const kindCls = ["incident", "change", "problem"].includes(c.kind) ? ` k-${c.kind}` : "";
  const sev = c.severity ? `<span class="sev" style="background:var(--${sevVar(c.severity)})">${esc(c.severity.toUpperCase())}</span>` : "";
  let foot = "";
  const bits = [];
  if (c.owner) bits.push(`<span class="ava" style="background:${avatarColor(c.owner)}" title="${esc(c.owner)}">${esc(c.owner[0].toUpperCase())}</span>`);
  else if (c.labels && c.labels.length) bits.push(`<span class="tagchip">${esc(c.labels[0])}</span>`);
  if (c.ai) bits.push(`<span class="ai-chip ${esc(c.ai === "needs-review" ? "review" : c.ai === "failed" ? "failed" : "")}">🤖 ${esc(c.ai)}</span>`);
  if (bits.length) foot = `<div class="kfoot">${bits.join("")}</div>`;
  return `<div class="kc p-${esc(c.priority)}${kindCls}" data-id="${esc(c.id)}" tabindex="0">
    <div class="kctop"><span class="kbadge ${esc(c.kind)}">${esc(c.kind)}</span>${sev}<span class="kid mono">#${esc(c.id)}</span></div>
    <div class="ktitle">${esc(c.title)}</div>${foot}</div>`;
}

function sevVar(s) { return { sev1: "sev1", sev2: "sev2", sev3: "sev3", sev4: "sev4" }[s] || "med"; }

function render() {
  const board = document.getElementById("board");
  const cols = _board.columns;
  const wip = _board.wip || {};

  let html = '<div class="corner"></div>';
  for (const col of cols) {
    const w = wip[col] || {};
    const label = w.limit != null ? `${w.count} / ${w.limit}` : (col === "backlog" || col === "done" ? tally(col) : "");
    const over = w.over ? " over" : "";
    const donecls = col === "done" ? " donecol" : "";
    html += `<div class="colhead${donecls}"><span class="name">${esc(COL_LABEL[col])}</span><span class="wip mono${over}">${esc(label)}</span></div>`;
  }

  for (const lane of _board.lanes) {
    const meta = LANE_LABEL[lane.key] || [lane.key, ""];
    html += `<div class="lanelabel"><span class="lname">${esc(meta[0])}</span><span class="lkind">${esc(meta[1])}</span></div>`;
    for (const col of cols) {
      const expe = lane.key === "expedite" ? " expedite" : "";
      const cards = (lane.columns[col] || []).filter(passesFilter);
      html += `<div class="cell${expe}" data-col="${esc(col)}" data-lane="${esc(lane.key)}">${cards.map(cardHTML).join("")}</div>`;
    }
  }
  board.innerHTML = html;
  wireSortables();
  refreshOwnerFilter();
}

function tally(col) {
  let n = 0;
  for (const lane of _board.lanes) n += (lane.columns[col] || []).length;
  return String(n);
}

function wireSortables() {
  _sortables.forEach((s) => s.destroy());
  _sortables.length = 0;
  document.querySelectorAll(".cell").forEach((cell) => {
    _sortables.push(new Sortable(cell, {
      group: "kanban",
      animation: 130,
      ghostClass: "sortable-ghost",
      dragClass: "sortable-drag",
      onStart: () => { _dragging = true; },
      onEnd: async (evt) => {
        _dragging = false;
        const id = evt.item.dataset.id;
        const toCol = evt.to.dataset.col;
        const fromCol = evt.from.dataset.col;
        if (!id || (toCol === fromCol && evt.oldIndex === evt.newIndex)) return;
        try {
          await mutate(id, "move", { column: toCol, order: evt.newIndex });
          toast(`moved #${id} → ${toCol}`);
          await refreshIfOpen(id);
        } catch (e) {
          toast(e.message, true);
          loadBoard(); // reconcile on failure
        }
      },
    }));
  });
  // click a card (that wasn't a drag) opens the detail panel
  document.querySelectorAll(".kc").forEach((el) => {
    el.addEventListener("click", () => { if (!_dragging) openCard(el.dataset.id); });
    el.addEventListener("keydown", (e) => { if (e.key === "Enter") openCard(el.dataset.id); });
  });
}

function refreshOwnerFilter() {
  const sel = document.getElementById("f-owner");
  const owners = new Set();
  for (const lane of _board.lanes)
    for (const col of _board.columns)
      for (const c of (lane.columns[col] || [])) if (c.owner) owners.add(c.owner);
  const cur = sel.value;
  sel.innerHTML = '<option value="">any owner</option>' +
    [...owners].sort().map((o) => `<option value="${esc(o)}"${o === cur ? " selected" : ""}>${esc(o)}</option>`).join("");
}

// ---- SSE live updates ----
function connectSSE() {
  const dot = document.getElementById("live-dot");
  const text = document.getElementById("live-text");
  let debounce = null;
  const es = new EventSource("/api/events");
  const refresh = () => { clearTimeout(debounce); debounce = setTimeout(() => { if (!_dragging) loadBoard(); }, 250); };
  es.addEventListener("open", () => { dot.classList.add("on"); text.textContent = "live"; });
  es.addEventListener("board_changed", refresh);
  es.addEventListener("card_changed", (e) => { refresh(); try { refreshIfOpen(JSON.parse(e.data).id); } catch (_) {} });
  es.addEventListener("error", () => { dot.classList.remove("on"); text.textContent = "reconnecting"; });
}

// ---- filters wiring ----
function wireFilters() {
  const bind = (id, key) => {
    const el = document.getElementById(id);
    el.addEventListener("input", () => { filters[key] = el.value; render(); });
    el.addEventListener("change", () => { filters[key] = el.value; render(); });
  };
  bind("f-text", "text"); bind("f-owner", "owner"); bind("f-kind", "kind"); bind("f-priority", "priority");
  document.getElementById("btn-refresh").addEventListener("click", loadBoard);
}

initPanel(() => loadBoard());
wireFilters();
loadBoard();
connectSSE();
