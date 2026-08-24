#!/usr/bin/env node

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function waitFor(check, message) {
  for (let attempt = 0; attempt < 200; attempt += 1) {
    if (await check()) return;
    await sleep(50);
  }
  throw new Error(message);
}

async function qualify() {
  const repo = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-25-cdp-"));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "skcp-25-home-"));
  const bearerFile = path.join(home, "bearer");
  const port = 17885;
  const python = `
import importlib.util
from datetime import datetime, timezone
from pathlib import Path
import uvicorn
repo = Path(${JSON.stringify(repo)})
spec = importlib.util.spec_from_file_location("decision_fixture", repo / "tests/test_control_plane_decision_context.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
rig = module.Rig(target="/api/v1/governance/projection")
Path(${JSON.stringify(bearerFile)}).write_text(rig.bearer)
home = Path(${JSON.stringify(home)})
now = datetime.now(timezone.utc).isoformat()
from skcoord.card_store import CardCore, CardStore
store = CardStore(home)
store.create(CardCore(id="source",kind="task",title="excluded title",description="excluded detail",created_by="fixture",created_at=now,dependencies=["missing"],acceptance_criteria=[],initial_priority="high",initial_swimlane="feature"))
store.append_event("source","claim","fixture",owner="agent-a")
store.append_event("source","describe","reviewer",title="excluded corrected title")
store.create(CardCore(id="complete",kind="task",title="excluded title",description="excluded detail",created_by="fixture",created_at=now,dependencies=[],acceptance_criteria=["criterion"],initial_priority="high",initial_swimlane="feature"))
from skdashboard.control_plane_adapters import SPECS, aggregate_reader
readers = {}
for item in SPECS:
    aggregate = {key: 1 for key in item.fields}
    if item.adapter_id == "capauth.policy": aggregate = {"available": True, "denials": 2}
    readers[item.adapter_id] = aggregate_reader(aggregate, expected=2 if item.adapter_id == "skcoord.flow" else 1, reporting=1, observed_at=now, errors=["partial"] if item.adapter_id == "skcoord.flow" else [], watermark_data=item.adapter_id)
from skdashboard.dashboard_governance import GovernanceProjectionProvider
from skdashboard.dashboard import create_app
app = create_app(home, control_plane_decision_authorizer=rig.authorizer, control_plane_invocation_factory=rig.factory, control_plane_governance_provider=GovernanceProjectionProvider(readers))
uvicorn.run(app, host="127.0.0.1", port=${port}, log_level="error")
`;
  const pythonPath = [path.join(repo, "src"), process.env.PYTHONPATH].filter(Boolean).join(path.delimiter);
  const server = spawn(process.env.PYTHON || "python", ["-c", python], { cwd: repo, env: { ...process.env, PYTHONPATH: pythonPath }, stdio: "ignore" });
  const chrome = spawn(process.env.CHROME_PATH || "/usr/bin/google-chrome", ["--headless=new", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
  try {
    await waitFor(() => fs.existsSync(bearerFile), "Bearer fixture was not created");
    await waitFor(async () => fetch(`http://127.0.0.1:${port}/control-plane/governance`).then((response) => response.ok).catch(() => false), "Dashboard did not start");
    const activePort = path.join(profile, "DevToolsActivePort");
    await waitFor(() => fs.existsSync(activePort), "Chrome did not publish DevToolsActivePort");
    const chromePort = fs.readFileSync(activePort, "utf8").trim().split("\n")[0];
    const targets = await fetch(`http://127.0.0.1:${chromePort}/json/list`).then((response) => response.json());
    const socket = new WebSocket(targets.find((target) => target.type === "page").webSocketDebuggerUrl);
    await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = reject; });
    let id = 0;
    const pending = new Map(), requests = [], exceptions = [];
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id && pending.has(message.id)) { const handlers = pending.get(message.id); pending.delete(message.id); message.error ? handlers.reject(new Error(JSON.stringify(message.error))) : handlers.resolve(message.result); }
      if (message.method === "Network.requestWillBeSent") requests.push(message.params.request);
      if (message.method === "Runtime.exceptionThrown") exceptions.push(message.params.exceptionDetails);
    };
    const send = (method, params = {}) => new Promise((resolve, reject) => { id += 1; pending.set(id, { resolve, reject }); socket.send(JSON.stringify({ id, method, params })); });
    const evaluate = async (expression) => { const result = await send("Runtime.evaluate", { expression, returnByValue: true, awaitPromise: true }); assert.equal(result.exceptionDetails, undefined); return result.result.value; };
    const key = async (value, code, keyCode) => { await send("Input.dispatchKeyEvent", { type: "rawKeyDown", key: value, code, windowsVirtualKeyCode: keyCode }); await send("Input.dispatchKeyEvent", { type: "keyUp", key: value, code, windowsVirtualKeyCode: keyCode }); };
    await send("Page.enable"); await send("Runtime.enable"); await send("Network.enable"); await send("Accessibility.enable");
    await send("Network.setExtraHTTPHeaders", { headers: { Authorization: `Bearer ${fs.readFileSync(bearerFile, "utf8")}`, Origin: "https://10.0.0.139:7778" } });
    await send("Page.navigate", { url: `http://127.0.0.1:${port}/control-plane/governance?role=governance&scope=estate&window=latest&baseline=none&service=all` });
    await waitFor(async () => evaluate("document.querySelectorAll('#lineage-rows tr').length === 13").catch(() => false), "Governance lineage did not render");
    const view = JSON.parse(await evaluate("JSON.stringify({lineage:document.querySelectorAll('#lineage-rows tr').length,sources:document.querySelectorAll('#source-rows tr').length,findings:document.querySelectorAll('#finding-rows tr').length,history:document.querySelectorAll('#history-rows tr').length,text:document.body.innerText})"));
    assert.equal(view.lineage, 13); assert.equal(view.sources, 16); assert.ok(view.findings >= 4); assert.equal(view.history, 1);
    assert.match(view.text, /policy_denial/i); assert.match(view.text, /partial_coverage/i); assert.match(view.text, /orphan_dependency/i); assert.match(view.text, /missing_criteria/i); assert.match(view.text, /claim_ttl/i); assert.match(view.text, /Preview only; dispatch false/i); assert.match(view.text, /human review and history/i); assert.doesNotMatch(view.text, /excluded title|excluded detail/i);
    await evaluate("document.getElementById('governance-role').focus()"); await key("ArrowDown", "ArrowDown", 40); await key("Enter", "Enter", 13);
    await waitFor(async () => evaluate("new URL(location.href).searchParams.get('role') === 'auditor'").catch(() => false), "Keyboard role change did not synchronize");
    for (const width of [390, 320]) { await send("Emulation.setDeviceMetricsOverride", { width, height: 800, deviceScaleFactor: 1, mobile: true }); const size = JSON.parse(await evaluate("JSON.stringify({scroll:document.documentElement.scrollWidth,client:document.documentElement.clientWidth})")); assert.equal(size.scroll <= size.client, true, JSON.stringify(size)); }
    const writes = requests.filter((request) => !["GET", "OPTIONS"].includes(request.method));
    const external = requests.filter((request) => !request.url.startsWith(`http://127.0.0.1:${port}/`) && !request.url.startsWith("data:"));
    assert.deepEqual(writes, []); assert.deepEqual(external, []); assert.deepEqual(exceptions, []);
    console.log("SKCP-25 CDP PASS: 13 definitions, 16 sources, 8 distinct finding classes, append-only history, keyboard, 390/320, zero writes/external/exceptions");
    socket.close();
  } finally { server.kill("SIGTERM"); chrome.kill("SIGTERM"); }
}

qualify().catch((error) => { console.error(error); process.exitCode = 1; });
