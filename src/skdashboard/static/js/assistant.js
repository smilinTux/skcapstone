// Assistant console: POST a prompt, stream the answer over SSE (read from the
// fetch body), render tokens live, and show any gated action result.
import { esc, toast } from "./api.js";

const chat = document.getElementById("chat");
const form = document.getElementById("ask-form");
const input = document.getElementById("ask");
let busy = false;

function addMsg(role, html) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const ava = role === "user" ? "you" : "SK";
  wrap.innerHTML = `<div class="ava">${role === "user" ? "🧑" : "🐧"}</div><div class="bubble"></div>`;
  wrap.querySelector(".bubble").innerHTML = html;
  chat.appendChild(wrap);
  chat.scrollIntoView(false);
  window.scrollTo(0, document.body.scrollHeight);
  return wrap.querySelector(".bubble");
}

async function ask(prompt) {
  if (busy || !prompt.trim()) return;
  busy = true;
  input.value = "";
  addMsg("user", esc(prompt));
  const bubble = addMsg("assistant", '<span class="thinking">thinking…</span>');
  let text = "";
  bubble.classList.add("cursor-blink");
  try {
    const resp = await fetch("/api/assistant", {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-SK-Actor": "operator" },
      body: JSON.stringify({ prompt }),
    });
    if (!resp.ok || !resp.body) throw new Error("assistant unavailable");
    const reader = resp.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const frames = buf.split("\n\n");
      buf = frames.pop();               // keep the incomplete tail
      for (const frame of frames) handleFrame(frame, bubble, (t) => { text += t; });
    }
  } catch (e) {
    toast(e.message, true);
    if (!text) bubble.textContent = "Sorry, I could not reach the model.";
  } finally {
    bubble.classList.remove("cursor-blink");
    if (!text.trim() && !bubble.querySelector(".action-result")) {
      bubble.textContent = bubble.textContent || "(no response)";
    }
    busy = false;
    input.focus();
  }
}

function handleFrame(frame, bubble, onText) {
  let ev = "message", data = "";
  for (const line of frame.split("\n")) {
    if (line.startsWith("event:")) ev = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  }
  if (!data) return;
  let obj;
  try { obj = JSON.parse(data); } catch (_) { return; }
  if (ev === "token") {
    if (bubble.querySelector(".thinking")) bubble.innerHTML = "";
    // strip a trailing ACTION line from the visible text
    bubble._raw = (bubble._raw || "") + obj.text;
    const visible = bubble._raw.replace(/ACTION\s*\{[\s\S]*$/, "").trim();
    bubble.innerHTML = esc(visible);
    onText(obj.text);
    window.scrollTo(0, document.body.scrollHeight);
  } else if (ev === "action") {
    const ok = obj.ok;
    const msg = ok
      ? `${esc(obj.tool)} on ${esc(obj.card_id)}${obj.run_id ? " · " + esc(obj.run_id) : ""}`
      : esc(obj.error || "action failed");
    const el = document.createElement("div");
    el.className = `action-result ${ok ? "ok" : "err"}`;
    el.innerHTML = `<span class="ic">${ok ? "✅" : "⚠️"}</span><span>${ok ? "Did: " : ""}${msg}</span>`;
    bubble.appendChild(el);
  }
}

form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
document.querySelectorAll(".ex").forEach((b) => b.addEventListener("click", () => ask(b.textContent)));
input.focus();
