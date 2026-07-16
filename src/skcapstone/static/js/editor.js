// Card detail panel: fetch a card, render editable fields + the activity log,
// and issue mutations. The activity log is the card's own event stream.
import { esc, getJSON, mutate, toast, avatarColor, timeShort } from "./api.js";

const KIND_LABEL = { task: "task", epic: "epic", incident: "incident", problem: "problem", change: "change" };
const PRIORITIES = ["critical", "high", "medium", "low"];
const OWNERS = ["", "lumina", "opus", "jarvis", "chef", "cleanup-sweep"];

let _current = null;      // card id currently open
let _onChange = null;     // callback(cardId) after a mutation

export function initPanel(onChange) {
  _onChange = onChange;
  document.getElementById("overlay").addEventListener("click", closePanel);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closePanel(); });
}

export function closePanel() {
  _current = null;
  document.getElementById("panel").classList.remove("open");
  document.getElementById("panel").setAttribute("aria-hidden", "true");
  document.getElementById("overlay").classList.remove("open");
}

export async function openCard(cardId) {
  _current = cardId;
  const panel = document.getElementById("panel");
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");
  document.getElementById("overlay").classList.add("open");
  panel.innerHTML = `<div class="psec"><div class="st">Loading ${esc(cardId)}…</div></div>`;
  try {
    const data = await getJSON(`/api/card/${encodeURIComponent(cardId)}`);
    if (data.error) { panel.innerHTML = `<div class="psec">${esc(data.error)}</div>`; return; }
    renderPanel(panel, data);
  } catch (e) {
    panel.innerHTML = `<div class="psec">Failed to load: ${esc(e.message)}</div>`;
  }
}

// Refresh the panel if it is showing this card (used on SSE echo).
export async function refreshIfOpen(cardId) {
  if (_current && (!cardId || cardId === _current)) await openCard(_current);
}

export function currentCard() { return _current; }

function renderPanel(panel, data) {
  const c = data.card;
  const kind = c.kind || "task";
  const ownerOpts = OWNERS.map((o) =>
    `<option value="${esc(o)}"${o === (c.owner || "") ? " selected" : ""}>${esc(o || "unassigned")}</option>`).join("");
  const prioOpts = PRIORITIES.map((p) =>
    `<option value="${p}"${p === c.priority ? " selected" : ""}>${p}</option>`).join("");
  const labels = (c.labels || []).map((l) =>
    `<span class="lchip">${esc(l)}<button data-rmlabel="${esc(l)}" title="remove">×</button></span>`).join("");

  panel.innerHTML = `
    <div class="phead">
      <div class="pkind">
        <span class="kbadge ${esc(kind)}">${esc(KIND_LABEL[kind] || kind)}</span>
        <span class="kid mono">#${esc(c.id)}</span>
        <button class="pclose" title="close (Esc)">×</button>
      </div>
      <div class="ptitle">${esc(c.title)}</div>
    </div>

    <div class="psec">
      <div class="frow"><label>Column</label>
        <select id="e-col">${["backlog","ready","doing","review","done"].map((k)=>`<option value="${k}"${k===c.status?" selected":""}>${k}</option>`).join("")}</select></div>
      <div class="frow"><label>Owner</label><select id="e-owner">${ownerOpts}</select></div>
      <div class="frow"><label>Priority</label><select id="e-prio">${prioOpts}</select></div>
    </div>

    <div class="psec">
      <div class="st">Labels</div>
      <div class="labels" id="e-labels">${labels || '<span class="fl" style="color:var(--ink3);font-size:11px">none</span>'}</div>
      <div class="addbox"><input id="e-newlabel" placeholder="add label…" autocomplete="off"><button class="btn" id="e-addlabel">Add</button></div>
    </div>

    ${c.description ? `<div class="psec"><div class="st">Description</div><div class="notes">${esc(c.description)}</div></div>` : ""}

    <div class="psec">
      <div class="st">Add note</div>
      <textarea class="notebox" id="e-note" placeholder="Leave a note on this card…"></textarea>
      <div style="margin-top:8px;text-align:right"><button class="btn primary" id="e-addnote">Add note</button></div>
    </div>

    ${renderAISection(c)}

    <div class="psec">
      <div class="st">Activity · ${(data.activity || []).length} events</div>
      <div class="act">${renderActivity(data.activity || [])}</div>
    </div>
  `;

  panel.querySelector(".pclose").addEventListener("click", closePanel);
  panel.querySelector("#e-col").addEventListener("change", (e) => act(c.id, "move", { column: e.target.value }));
  panel.querySelector("#e-owner").addEventListener("change", (e) =>
    e.target.value ? act(c.id, "assign", { owner: e.target.value }) : act(c.id, "unassign", {}));
  panel.querySelector("#e-prio").addEventListener("change", (e) => act(c.id, "priority", { priority: e.target.value }));
  panel.querySelectorAll("[data-rmlabel]").forEach((b) =>
    b.addEventListener("click", () => act(c.id, "remove_label", { label: b.dataset.rmlabel })));
  const addLabel = () => {
    const v = panel.querySelector("#e-newlabel").value.trim();
    if (v) act(c.id, "add_label", { label: v });
  };
  panel.querySelector("#e-addlabel").addEventListener("click", addLabel);
  panel.querySelector("#e-newlabel").addEventListener("keydown", (e) => { if (e.key === "Enter") addLabel(); });
  panel.querySelector("#e-addnote").addEventListener("click", () => {
    const v = panel.querySelector("#e-note").value.trim();
    if (v) act(c.id, "note", { text: v });
  });
  wireAISection(panel, c);
}

const AGENTS = ["lumina", "opus", "jarvis"];

function renderAISection(c) {
  const run = (c.meta && c.meta.agent_run) || null;
  let status = "";
  if (run) {
    const acts = (run.activity || []).slice(-6).map((a) =>
      `<div class="aitem"><span class="atype ${esc(a.atype || "action")}">${esc(a.atype || "")}</span>
       <span class="abody">${esc(a.text || "")}</span></div>`).join("");
    status = `<div style="margin-bottom:9px;font-size:11.5px">
      <span class="runstate ${esc(run.state)}">● ${esc(run.state)}</span>
      <span style="color:var(--ink3)"> ${esc(run.agent || "")} · ${esc(run.mode || "")}</span>
      ${run.links && run.links.pr ? `· <a href="#">${esc(run.links.pr)}</a>` : ""}</div>
      <div class="act">${acts || ""}</div>`;
  }
  const agentOpts = AGENTS.map((a) => `<option value="${a}">${a}</option>`).join("");
  return `<div class="psec ai-sec">
    <div class="st">🤖 AI next steps</div>
    ${status}
    <textarea class="notebox" id="e-instruction" placeholder="Describe the next steps for an AI agent to execute…"></textarea>
    <div style="display:flex;gap:7px;align-items:center;margin-top:8px;flex-wrap:wrap">
      <div class="seg" id="e-mode">
        <span class="on" data-mode="propose">propose</span><span data-mode="dry-run">dry-run</span><span data-mode="execute">execute</span>
      </div>
      <select class="picker" id="e-agent">${agentOpts}</select>
      <span style="flex:1"></span>
      <button class="btn ai" id="e-queue">🔒 Queue for AI</button>
    </div>
    <div class="locknote">🔒 capauth-gated · execute on a change ticket needs a CAB vote first</div>
  </div>`;
}

function wireAISection(panel, c) {
  let mode = "propose";
  panel.querySelectorAll("#e-mode span").forEach((s) => s.addEventListener("click", () => {
    panel.querySelectorAll("#e-mode span").forEach((x) => x.classList.remove("on"));
    s.classList.add("on"); mode = s.dataset.mode;
  }));
  panel.querySelector("#e-queue").addEventListener("click", async () => {
    const instruction = panel.querySelector("#e-instruction").value.trim();
    if (!instruction) { toast("instruction required", true); return; }
    const agent = panel.querySelector("#e-agent").value;
    try {
      const r = await fetch(`/api/card/${encodeURIComponent(c.id)}/queue-ai`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-SK-Actor": "operator" },
        body: JSON.stringify({ instruction, agent, mode }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || "queue failed");
      toast(`queued for ${agent} (${mode}) ✓`);
      await openCard(c.id);
      if (_onChange) _onChange(c.id);
    } catch (e) { toast(e.message, true); }
  });
}

async function act(cardId, action, body) {
  try {
    await mutate(cardId, action, body);
    toast(action.replace("_", " ") + " ✓");
    await openCard(cardId);          // re-render the panel with fresh state
    if (_onChange) _onChange(cardId); // let the board refresh the card face
  } catch (e) {
    toast(e.message, true);
  }
}

function renderActivity(events) {
  if (!events.length) return '<span class="fl" style="color:var(--ink3);font-size:11px">no events</span>';
  // newest first
  const rows = events.slice().reverse().map((e) => {
    const t = e.action || "event";
    return `<div class="aitem">
      <span class="atype ${esc(t)}">${esc(t.replace("_", " "))}</span>
      <span class="abody">${activityText(e)}</span>
      <span class="atime">${esc(timeShort(e.ts))}</span>
    </div>`;
  });
  return rows.join("");
}

function activityText(e) {
  const w = e.writer ? `<span class="w">${esc(e.writer)}</span>` : "";
  switch (e.action) {
    case "move": return `${w} → <b>${esc(e.column || "")}</b>`;
    case "assign": return `${w} assigned <b>${esc(e.owner || "")}</b>`;
    case "unassign": return `${w} unassigned`;
    case "claim": return `${w} claimed`;
    case "complete": return `${w} completed`;
    case "priority": return `${w} set priority <b>${esc(e.priority || "")}</b>`;
    case "add_label": return `${w} +label <b>${esc(e.label || "")}</b>`;
    case "remove_label": return `${w} -label <b>${esc(e.label || "")}</b>`;
    case "note": return `${w}: ${esc(e.text || "")}`;
    case "link": return `${w} linked ${esc(e.link_key || "")} = ${esc(e.link_value || "")}`;
    case "archive": return `${w} archived`;
    case "reopen": return `${w} reopened`;
    default: return `${w} ${esc(e.action || "")}`;
  }
}
