// Shared AI next-steps composer: recommended options + instruction + mode +
// agent picker + capauth-gated queue. Used by the board card panel and the
// cockpit ITIL record panel alike.
import { esc, getJSON, toast } from "./api.js";

const AGENTS = ["lumina", "opus", "jarvis"];

// Render the composer HTML. `run` is the card's current meta.agent_run (or null).
export function renderAIComposer(run) {
  let status = "";
  if (run) {
    const acts = (run.activity || []).slice(-6).map((a) =>
      `<div class="aitem"><span class="atype ${esc(a.atype || "action")}">${esc(a.atype || "")}</span>
       <span class="abody">${esc(a.text || "")}</span></div>`).join("");
    status = `<div style="margin-bottom:9px;font-size:11.5px">
      <span class="runstate ${esc(run.state)}">● ${esc(run.state)}</span>
      <span style="color:var(--ink3)"> ${esc(run.agent || "")} · ${esc(run.mode || "")}</span>
      ${run.links && run.links.pr ? `· ${esc(run.links.pr)}` : ""}</div><div class="act">${acts}</div>`;
  }
  const agentOpts = AGENTS.map((a) => `<option value="${a}">${a}</option>`).join("");
  return `<div class="psec ai-sec">
    <div class="st">🤖 AI next steps</div>
    ${status}
    <div class="ai-suggest" id="e-suggest"><span class="fl" style="color:var(--ink3);font-size:11px">loading recommendations…</span></div>
    <textarea class="notebox" id="e-instruction" placeholder="Describe the next steps, or pick a recommendation above…"></textarea>
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

// Wire the composer inside `panel` for `cardId`; onQueued() fires after a queue.
export function wireAIComposer(panel, cardId, onQueued) {
  let mode = "propose";
  const setMode = (m) => {
    mode = m;
    panel.querySelectorAll("#e-mode span").forEach((x) => x.classList.toggle("on", x.dataset.mode === m));
  };
  panel.querySelectorAll("#e-mode span").forEach((s) => s.addEventListener("click", () => setMode(s.dataset.mode)));

  loadSuggestions(panel, cardId, setMode);

  panel.querySelector("#e-queue").addEventListener("click", async () => {
    const instruction = panel.querySelector("#e-instruction").value.trim();
    if (!instruction) { toast("instruction required", true); return; }
    const agent = panel.querySelector("#e-agent").value;
    try {
      const r = await fetch(`/api/card/${encodeURIComponent(cardId)}/queue-ai`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-SK-Actor": "operator" },
        body: JSON.stringify({ instruction, agent, mode }),
      });
      const d = await r.json();
      if (!r.ok || d.error) throw new Error(d.error || "queue failed");
      toast(`queued for ${agent} (${mode}) ✓`);
      if (onQueued) onQueued();
    } catch (e) { toast(e.message, true); }
  });
}

function renderChips(panel, box, sugg, source, setMode, tailoring) {
  if (!sugg.length) { box.innerHTML = ""; return; }
  const label = source === "llm" ? "tailored" : (tailoring ? "suggested · tailoring…" : "suggested");
  box.innerHTML = `<div class="fl" style="color:var(--ink3);font-size:10px;margin-bottom:5px">RECOMMENDED (${label}) · click to use</div>` +
    sugg.map((s, i) => `<button class="suggest-chip" data-i="${i}"><span class="sm ${esc(s.mode)}">${esc(s.mode)}</span>${esc(s.text)}</button>`).join("");
  box.querySelectorAll(".suggest-chip").forEach((b) => b.addEventListener("click", () => {
    const s = sugg[+b.dataset.i];
    panel.querySelector("#e-instruction").value = s.text;
    setMode(s.mode);
    panel.querySelector("#e-instruction").focus();
  }));
}

async function loadSuggestions(panel, cardId, setMode) {
  const box = panel.querySelector("#e-suggest");
  if (!box) return;
  const url = (llm) => `/api/card/${encodeURIComponent(cardId)}/ai-suggestions?llm=${llm}`;
  // 1) instant heuristics
  try {
    const d = await getJSON(url(0));
    renderChips(panel, box, d.suggestions || [], "heuristic", setMode, true);
  } catch (_) { box.innerHTML = ""; }
  // 2) upgrade to LLM-tailored in the background (skip if the box is gone)
  try {
    const d = await getJSON(url(1));
    if (d.source === "llm" && (d.suggestions || []).length && document.body.contains(box)) {
      renderChips(panel, box, d.suggestions, "llm", setMode, false);
    } else if (document.body.contains(box)) {
      // strip the "tailoring…" hint if the LLM didn't land
      const hint = box.querySelector(".fl");
      if (hint) hint.textContent = hint.textContent.replace(" · tailoring…", "");
    }
  } catch (_) { /* keep heuristics */ }
}
